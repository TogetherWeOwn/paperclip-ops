#!/usr/bin/env bash
# coolify_capacity_headroom.sh — source-only Coolify decommission capacity check.
#
# case_4dbfd258 (child of case_e1870ec1, under case_a00f6d6e): case_a00f6d6e orders CI off the
# Coolify host (8 vCPU / ~22 GiB, 96% disk, production co-resident) but
# requires capacity confirmed elsewhere before decommission. This tool answers
# the capacity half from read-only GitHub Actions job data. It changes nothing:
# no host command, no runner mutation, no credential, no network call. The
# caller supplies the jobs JSON (captured separately via an authorized
# read path); this script only measures it.
#
# THE ONE COUNTING RULE (case_f19c6787): only runner-assigned execution minutes
# are compute. A job with no `runner_name` — queued behind no runner, or a
# cancelled run that never landed — contributes timestamp span but zero
# compute. The 12h reference sample held ~17,037 runner minutes against
# ~15,730 no-runner timestamp minutes; counting the latter as load would
# nearly double the apparent demand off a queue artifact. This tool counts
# (completed_at - started_at) only when `runner_name` is nonempty and both
# clocks parse, and reports everything it excluded, by cause.
#
# HOST CLASSES (case_a00f6d6e baseline + case_bec120d3): `coolify-vps-*` is the
# decommission candidate; `ci-node7b1ff338-iso-*` / `ci-node7b1ff338-*` is the node7b1ff338 static
# fleet; `garm-*` is GARM ephemeral (node7b1ff338 pool `garm-two`, node575e13db `garm-old`).
# GARM slots are logical shared slots, not dedicated cores; defaults
# (--node7b1ff338-max 2, --node575e13db-max 4) are the CEO-set pool caps from
# garm-canary.yml, total 6 concurrent VM slots. Never raise them from a queue
# count alone (case_a00f6d6e).
#
# VERDICT RULES: a measured sample needs >=24h span AND >=10 matched completed
# attempts per arm (case_1fe7979a trial rule, cited in github-runner/garm/README.md);
# anything thinner is INCONCLUSIVE, never GO. GO means the observed Coolify
# peak fits inside total GARM slots. NO-GO means it does not. Either is a
# measured answer (exit 0). Exit 2 is refusal: unreadable input, zero
# runner-assigned jobs, or no parseable clocks — an unmeasured pool is never
# reported healthy.
#
# Usage:
#   ./coolify_capacity_headroom.sh --jobs <jobs.json>
#     [--node7b1ff338-max 2] [--node575e13db-max 4] [--min-hours 24] [--min-attempts 10]
#   jobs.json is a JSON array of job objects, or an object with a `.jobs`
#   array (the `gh api .../jobs` shape). Each job needs `runner_name`,
#   `started_at`, `completed_at` (ISO-8601 UTC `Z`); `name`, `conclusion`
#   and `labels` are carried for cohort notes only.
set -uo pipefail

JOBS=""; node7525e5a1_MAX=2; node8a3e959c_MAX=4; MIN_HOURS=24; MIN_ATTEMPTS=10
while (($# > 0)); do
  case "$1" in
    --jobs) JOBS="${2:-}"; shift 2 ;;
    --node7b1ff338-max) node7525e5a1_MAX="${2:-}"; shift 2 ;;
    --node575e13db-max) node8a3e959c_MAX="${2:-}"; shift 2 ;;
    --min-hours) MIN_HOURS="${2:-}"; shift 2 ;;
    --min-attempts) MIN_ATTEMPTS="${2:-}"; shift 2 ;;
    *) echo "coolify_capacity_headroom: refusing — unknown argument '$1'" >&2; exit 2 ;;
  esac
done

refuse() { echo "coolify_capacity_headroom: refusing — $1" >&2; exit 2; }

[[ -n "$JOBS" ]] || refuse "no --jobs file given"
[[ -f "$JOBS" ]] || refuse "jobs file '$JOBS' not readable"
[[ "$node7525e5a1_MAX" =~ ^[0-9]+$ && "$node8a3e959c_MAX" =~ ^[0-9]+$ ]] \
  || refuse "pool caps must be whole numbers (got node7b1ff338='$node7525e5a1_MAX' node575e13db='$node8a3e959c_MAX')"
[[ "$MIN_HOURS" =~ ^[0-9]+$ && "$MIN_ATTEMPTS" =~ ^[0-9]+$ ]] \
  || refuse "adequacy bounds must be whole numbers"
command -v jq >/dev/null 2>&1 || refuse "jq required"
command -v python3 >/dev/null 2>&1 || refuse "python3 required"

TS_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
to_epoch() {
  [[ "$1" =~ $TS_RE ]] || return 1
  date -u -d "$1" +%s 2>/dev/null
}

# Normalize to flat unit-separator rows: name \037 runner \037 started \037
# completed. The separator is ASCII US (non-IFS-whitespace) deliberately: with
# a tab separator, `read` collapses the empty runner_name field and a
# queued-no-runner job shifts its timestamp into the runner slot, silently
# promoting a queue artifact into compute. The runner_name emptiness test is
# the case_f19c6787 boundary and lives here, not in a caller filter, so no caller
# can silently widen it.
SEP=$'\037'
ROWS="$(jq -r --arg sep "$SEP" '
  (if type == "object" and has("jobs") then .jobs else . end)
  | if type != "array" then error("not-array") else . end
  | .[] | [.name // "?", (.runner_name // ""), (.started_at // ""), (.completed_at // "")] | join($sep)' \
  "$JOBS" 2>/dev/null)" || refuse "jobs file is not a JSON array (or {jobs:[...]})"

[[ -n "$ROWS" ]] || refuse "jobs file holds zero jobs — nothing measured"

total_min="0"; coolify_min="0"; node7b1ff338_min="0"; garm_min="0"; other_min="0"
excl_no_runner=0; excl_clock=0; matched=0
min_s=""; max_e=""
intervals=""; coolify_intervals=""

while IFS="$SEP" read -r name runner started completed; do
  if [[ -z "$runner" ]]; then
    excl_no_runner=$((excl_no_runner + 1)); continue  # case_f19c6787: queue/cancel artifact, not compute
  fi
  s_epoch="$(to_epoch "$started" 2>/dev/null)" || s_epoch=""
  e_epoch="$(to_epoch "$completed" 2>/dev/null)" || e_epoch=""
  if [[ -z "$s_epoch" || -z "$e_epoch" ]] || (( e_epoch <= s_epoch )); then
    excl_clock=$((excl_clock + 1)); continue  # unparseable or skewed clocks are excluded, never zero-filled
  fi
  mins="$(python3 -c "print(f'{( $e_epoch - $s_epoch ) / 60:.2f}')" 2>/dev/null)" \
    || refuse "arithmetic failed for job '$name'"
  matched=$((matched + 1))
  total_min="$(python3 -c "print(f'{float($total_min) + float($mins):.2f}')" )"
  case "$runner" in
    coolify-vps-*) coolify_min="$(python3 -c "print(f'{float($coolify_min) + float($mins):.2f}')" )"
      coolify_intervals+="$s_epoch $e_epoch"$'\n' ;;
    ci-node7b1ff338-*) node7b1ff338_min="$(python3 -c "print(f'{float($node7b1ff338_min) + float($mins):.2f}')" )" ;;
    garm-*) garm_min="$(python3 -c "print(f'{float($garm_min) + float($mins):.2f}')" )" ;;
    *) other_min="$(python3 -c "print(f'{float($other_min) + float($mins):.2f}')" )" ;;
  esac
  intervals+="$s_epoch $e_epoch"$'\n'
  { [[ -z "$min_s" ]] || (( s_epoch < min_s )); } && min_s="$s_epoch"
  { [[ -z "$max_e" ]] || (( e_epoch > max_e )); } && max_e="$e_epoch"
done <<<"$ROWS"

(( matched > 0 )) || refuse "zero runner-assigned jobs — sample is all queue/cancel artifacts"

peak_of() {
  # Sweep-line peak concurrency over "start end" lines. Empty input peaks 0.
  python3 -c "
import sys
events = []
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line: continue
    s, e = line.split()
    events.append((int(s), 1)); events.append((int(e), -1))
events.sort(key=lambda t: (t[0], t[1]))
peak = cur = 0
for _, d in events:
    cur += d
    peak = max(peak, cur)
print(peak)"
}
peak_all="$(peak_of <<<"$intervals")"
peak_coolify="$(peak_of <<<"$coolify_intervals")"
span_h="$(python3 -c "print(f'{($max_e - $min_s) / 3600:.2f}')" )"
slots=$(( 10#$node7525e5a1_MAX + 10#$node8a3e959c_MAX ))

verdict=""; reason=""
if ! python3 -c "exit(0 if float($span_h) >= float($MIN_HOURS) else 1)"; then
  verdict="INCONCLUSIVE"; reason="span ${span_h}h below ${MIN_HOURS}h adequacy bound"
elif (( matched < 10#$MIN_ATTEMPTS )); then
  verdict="INCONCLUSIVE"; reason="matched ${matched} below ${MIN_ATTEMPTS} adequacy bound"
elif (( 10#$peak_coolify <= slots )); then
  verdict="GO"; reason="coolify peak ${peak_coolify} fits ${slots} GARM slots (node7b1ff338 ${node7525e5a1_MAX}+node575e13db ${node8a3e959c_MAX})"
else
  verdict="NO-GO"; reason="coolify peak ${peak_coolify} exceeds ${slots} GARM slots (node7b1ff338 ${node7525e5a1_MAX}+node575e13db ${node8a3e959c_MAX})"
fi

fmt2() { python3 -c "print(f'{float($1):.2f}')"; }
printf 'coolify_capacity_headroom: runner_minutes_total=%s coolify=%s node7b1ff338=%s garm=%s other=%s excluded_no_runner=%d excluded_clock=%d span_h=%s matched=%d peak_all=%s coolify_peak=%s garm_slots=%d verdict=%s reason=%s\n' \
  "$(fmt2 "$total_min")" "$(fmt2 "$coolify_min")" "$(fmt2 "$node7b1ff338_min")" "$(fmt2 "$garm_min")" "$(fmt2 "$other_min")" \
  "$excl_no_runner" "$excl_clock" "$span_h" "$matched" \
  "$peak_all" "$peak_coolify" "$slots" "$verdict" "$reason"
