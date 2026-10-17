#!/usr/bin/env bash
# push-ci-main.sh — cron: main-branch conclusion + job runtime vs baseline → Gatus.
#
# case_c542accd (child of case_1ec443c8; slice of case_66436e78 deliverable 3, CI
# coverage remainder). Sibling case_b24794c2 owns GARM pool health; case_73259b15
# owns Coolify prod-app health; open PR #549 owns per-pool GARM endpoints.
# This leaf owns exactly three things, nothing else:
#   1. main-branch conclusion per active repo (red main means one alert);
#   2. job runtime above CI_RUNTIME_FACTOR x the in-repo baseline table;
#   3. Coolify runner-service (systemd unit) liveness.
# Source-only: no host deploy rides this change; the operator installs the
# cron line separately (see OPERATOR.md).
#
# What it does, once per run (cron: */5 * * * *):
#   1. Reads the check-runs API for `main` on every repo in GH_REPOS
#      (checks:read — the same class gh_ci_status.sh needs; the actions
#      runs/jobs APIs are NOT used and need no actions:read).
#   2. Pushes one Gatus external endpoint per repo for conclusion
#      (`ci_main-<owner>-<repo>`, success=false naming the failed checks)
#      and one per repo for runtime (`ci_runtime-<owner>-<repo>`).
#   3. Pushes `ci_runner-units` from `systemctl is-active` over RUNNER_UNITS.
# All pushes go through the shared ../case_658bf6b9/_gatus_push.sh helper, so the
# existing Gatus custom provider carries CI alerts into Akmatori.
#
# CONCLUSION rule: any completed check whose conclusion is failure,
# timed_out, cancelled, action_required or startup_failure makes the repo
# red. Skipped/neutral/success stay green. Checks still queued or
# in_progress do NOT page — the conclusion is unknown, and the stuck-queue
# case is already owned by push-ci-queue-age.sh. ci.yml never cancels main
# runs (cancel-in-progress is pull_request-only, case_d0cad227), so a cancelled
# check on main is genuinely red, not a superseded PR run. Exactly one push
# per endpoint per run: a red main is one alert no matter how many checks
# failed (the names ride the error text, truncated by the helper).
#
# RUNTIME rule: every completed check with valid timestamps is timed
# (completed_at - started_at); every non-completed check is aged
# (now - started_at). Duration or age STRICTLY greater than
# CI_RUNTIME_FACTOR x baseline breaches, naming the job and both seconds
# counts. Baselines come from the reviewed CI_BASELINES_FILE (default sibling
# job_baselines.json, which the public snapshot intentionally does not carry);
# a job or repo with no entry falls back to fallback_s — a backstop for
# pathological hangs, not a tuned threshold. Repo matching is case-insensitive,
# so GH_REPOS case never changes which baseline a job resolves to. Skipped/neutral
# checks never ran, so they are excluded from timing (a 0s skipped job
# against any baseline is noise). Small negative spans/ages (-60s..0) are
# a GitHub timestamp artefact measured on healthy mains and clamp to 0 —
# they cannot hide a breach, which needs a large positive span; grossly
# negative spans still fail closed as corrupt data.
#
# FAIL CLOSED: an unreadable check-runs API, unparseable JSON, zero
# check-runs on an active repo's main (the case_b730ec29 vacuous case: "all zero
# runs succeeded" is trivially true and must never read green), an
# unparseable timestamp, or an inactive/missing runner unit all push
# success=false. Refusals (exit 2, zero pushes, heartbeat miss pages):
# missing GATUS_URL/token, GH_REPOS, baselines file, gh/jq binaries, bad
# factor, missing systemctl.
#
# REJECTED alternative: the runners API (org or repo scope) answers 403 on
# the broker token class this fleet mints (measured 2026-10-03), so unit
# liveness is read from systemd on the host, not from GitHub.
#
# Env:
#   push mode:  GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh),
#               GH_REPOS (whitespace owner/repo list, mandatory),
#               GH_BIN (default gh), CI_RUNTIME_FACTOR (default 2, positive
#               integer), CI_BASELINES_FILE (default sibling
#               job_baselines.json), SYSTEMCTL_BIN (default systemctl),
#               RUNNER_UNITS (default the seven coolify-vps-N units).
# Exit: 0 every evaluated endpoint healthy and pushed; 1 at least one
# endpoint unhealthy or unpushable; 2 refusal before any health claim.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-ci-main: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
. "$GATUS_HELPER"

[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-ci-main: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-ci-main: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

BASELINES_FILE="${CI_BASELINES_FILE:-$HERE/job_baselines.json}"
FACTOR="${CI_RUNTIME_FACTOR:-2}"
GH_BIN="${GH_BIN:-gh}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
DEFAULT_UNITS="actions.runner.TogetherWeOwn.coolify-vps-1.service actions.runner.TogetherWeOwn.coolify-vps-2.service actions.runner.TogetherWeOwn.coolify-vps-3.service actions.runner.TogetherWeOwn.coolify-vps-4.service actions.runner.TogetherWeOwn.coolify-vps-5.service actions.runner.TogetherWeOwn.coolify-vps-6.service actions.runner.TogetherWeOwn.coolify-vps-7.service"
UNITS="${RUNNER_UNITS:-$DEFAULT_UNITS}"

[[ "$FACTOR" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-ci-main: refusing — CI_RUNTIME_FACTOR must be a positive integer" >&2; exit 2; }
[[ -r "$BASELINES_FILE" ]] \
  || { echo "push-ci-main: refusing — cannot read baselines $BASELINES_FILE" >&2; exit 2; }
command -v "$GH_BIN" >/dev/null 2>&1 \
  || { echo "push-ci-main: refusing — no '$GH_BIN' on PATH" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-ci-main: refusing — jq required" >&2; exit 2; }
command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 \
  || { echo "push-ci-main: refusing — no '$SYSTEMCTL_BIN' on PATH (operator install?)" >&2; exit 2; }
read -r -a REPOS <<<"${GH_REPOS:-}"
(( ${#REPOS[@]} > 0 )) \
  || { echo "push-ci-main: refusing — GH_REPOS (whitespace owner/repo list) must be set" >&2; exit 2; }
for repo in "${REPOS[@]}"; do
  [[ "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ && "${repo#*/}" != "." && "${repo#*/}" != ".." ]] \
    || { echo "push-ci-main: refusing — bad repo '$repo' (want owner/repo)" >&2; exit 2; }
done
FALLBACK="$(jq -r '.fallback_s' "$BASELINES_FILE" 2>/dev/null)" \
  || { echo "push-ci-main: refusing — baselines file does not parse" >&2; exit 2; }
[[ "$FALLBACK" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-ci-main: refusing — fallback_s must be a positive integer" >&2; exit 2; }

# Endpoint key derivation (same as case_658bf6b9): <group>_<name>, separators to
# '-'. Group is ci; owner/repo slashes become dashes (repos are allowlisted
# above, so the key alphabet holds by construction). Dots also become
# dashes: the allowlist admits dotted repo names but the shared helper's
# key alphabet (^[A-Za-z0-9][A-Za-z0-9_-]*$) does not, so an unmapped dot
# would refuse every push for that repo. Repos without dots keep unchanged keys.
slug_of() { local s="${1//\//-}"; tr '[:upper:]' '[:lower:]' <<<"${s//./-}"; }

now="$(date +%s)"
overall=0
push_endpoint() {  # <key> <true|false> [reason]; records the round verdict
  if [[ "$2" == true ]]; then
    gatus_push "$1" true || overall=1
  else
    gatus_push "$1" false "$3" || overall=1
    overall=1
  fi
}

for repo in "${REPOS[@]}"; do
  slug="$(slug_of "$repo")"
  main_key="ci_main-$slug"
  runtime_key="ci_runtime-$slug"
  payload="$("$GH_BIN" api "repos/$repo/commits/main/check-runs?per_page=100" 2>&1)" \
    || { push_endpoint "$main_key" false "check-runs API unreadable for $repo";
         push_endpoint "$runtime_key" false "check-runs API unreadable for $repo";
         continue; }
  n_checks="$(jq -r '.check_runs | length' <<<"$payload" 2>/dev/null)" \
    || { push_endpoint "$main_key" false "unparseable check-runs for $repo";
         push_endpoint "$runtime_key" false "unparseable check-runs for $repo";
         continue; }
  # Zero check-runs on an active repo's main is the case_b730ec29 vacuous case:
  # unknown, never green.
  if [[ "$n_checks" == "0" ]]; then
    push_endpoint "$main_key" false "no-signal: zero check-runs on $repo main (CI may not have run yet — not a pass)"
    push_endpoint "$runtime_key" false "no-signal: zero check-runs on $repo main (nothing timed — not healthy)"
    continue
  fi

  # --- conclusion ---------------------------------------------------------
  red="$(jq -r '[.check_runs[]
            | select(.status == "completed")
            | select((.conclusion // "") | IN("failure","timed_out","cancelled","action_required","startup_failure"))
            | .name] | join(", ")' <<<"$payload" 2>/dev/null)" \
    || { push_endpoint "$main_key" false "unparseable check-runs for $repo";
         push_endpoint "$runtime_key" false "unparseable check-runs for $repo";
         continue; }
  if [[ -n "$red" ]]; then
    count="$(jq -r '[.check_runs[]
              | select(.status == "completed")
              | select((.conclusion // "") | IN("failure","timed_out","cancelled","action_required","startup_failure"))] | length' <<<"$payload")"
    push_endpoint "$main_key" false "red main on $repo ($count): $red"
  else
    push_endpoint "$main_key" true
  fi

  # --- runtime -------------------------------------------------------------
  # Completed checks are timed; live ones are aged against now. Skipped and
  # neutral never ran and are excluded. Anything with an unreadable or
  # missing timestamp fails closed: an untimed check is not a fast check.
  breaches=()
  unreadable=0
  tsv="$(jq -r '.check_runs[] | [.name, .status, (.conclusion // ""), (.started_at // ""), (.completed_at // "")] | @tsv' <<<"$payload" 2>/dev/null)" \
    || unreadable=1
  while IFS=$'\t' read -r cname cstatus cconcl cstart cend; do
    [[ -n "$cname" ]] || continue
    if [[ "$cconcl" == "skipped" || "$cconcl" == "neutral" ]]; then continue; fi
    baseline="$(jq -r --arg r "$repo" --arg n "$cname" \
      '(.repos | with_entries(.key |= ascii_downcase))[$r | ascii_downcase].jobs[$n].baseline_s // .fallback_s' "$BASELINES_FILE" 2>/dev/null)" \
      || { unreadable=1; continue; }
    [[ "$baseline" =~ ^[1-9][0-9]*$ ]] || { unreadable=1; continue; }
    # F1: date -d "" SUCCEEDS (today 00:00), so an empty started_at must be
    # rejected before it reaches date — otherwise a queued check that never
    # ran computes a time-of-day-dependent age instead of failing closed.
    [[ -n "$cstart" && "$cstart" != "null" ]] || { unreadable=1; continue; }
    start_s="$(date -d "$cstart" +%s 2>/dev/null)" || { unreadable=1; continue; }
    if [[ "$cstatus" == "completed" ]]; then
      [[ -n "$cend" && "$cend" != "null" ]] || { unreadable=1; continue; }
      end_s="$(date -d "$cend" +%s 2>/dev/null)" || { unreadable=1; continue; }
      span=$(( end_s - start_s ))
      # Small negative spans are a GitHub timestamp artefact on healthy
      # mains: clamp them to 0, which can never hide a breach (a breach needs
      # span > FACTOR x baseline). Grossly negative spans are corrupt
      # data and still fail closed.
      if (( span < 0 )); then
        (( span >= -60 )) && span=0 || { unreadable=1; continue; }
      fi
      (( span > 10#$FACTOR * 10#$baseline )) && breaches+=("$cname ${span}s > ${FACTOR}x${baseline}s")
    else
      age=$(( now - start_s ))
      if (( age < 0 )); then
        (( age >= -60 )) && age=0 || { unreadable=1; continue; }
      fi
      (( age > 10#$FACTOR * 10#$baseline )) && breaches+=("$cname in-flight ${age}s > ${FACTOR}x${baseline}s")
    fi
  done <<<"$tsv"
  if (( unreadable > 0 )); then
    push_endpoint "$runtime_key" false "unparseable check timestamps on $repo (an untimed check is not a fast check)"
  elif (( ${#breaches[@]} > 0 )); then
    push_endpoint "$runtime_key" false "slow job(s) on $repo: ${breaches[*]}"
  else
    push_endpoint "$runtime_key" true
  fi
done

# --- runner units -------------------------------------------------------------
read -r -a UNIT_LIST <<<"$UNITS"
(( ${#UNIT_LIST[@]} > 0 )) \
  || { echo "push-ci-main: refusing — RUNNER_UNITS is empty" >&2; exit 2; }
dead=()
for unit in "${UNIT_LIST[@]}"; do
  [[ "$unit" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || { echo "push-ci-main: refusing — bad unit name '$unit'" >&2; exit 2; }
  "$SYSTEMCTL_BIN" is-active --quiet "$unit" 2>/dev/null \
    || dead+=("$unit")
done
if (( ${#dead[@]} == 0 )); then
  push_endpoint "ci_runner-units" true
else
  push_endpoint "ci_runner-units" false "runner unit(s) not active: ${dead[*]}"
fi

exit "$overall"
