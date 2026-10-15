#!/usr/bin/env bash
# push-operator-runs-disk.sh — cron: Operator SSH run-folder footprint → Gatus.
#
# case_34c2c99f (slice of the /home incident). Source-only: no host deploy or
# cron install rides this change; the operator installs the cron line and the
# Gatus fragment separately (see RUNBOOK.md).
#
# Runs prune_operator_run_dirs.py in dry-run (it removes nothing without
# --apply), reads `runs_total_gib` — the WHOLE run-folder footprint, kept
# (dirty / unpushed / unverifiable) folders included, so a pruner that cannot
# verify anything still reports the disk it cannot reclaim — and pushes one
# boolean to Gatus `platform_operator-runs-disk`: success=true when the
# footprint is below warn, success=false naming the figure otherwise. A
# missing or non-numeric figure is NEVER read as 0: a pruner refusal
# (exit != 0), an unparseable summary, a summary without the figure, a
# missing PAPERCLIP_API_* env, an unpushable result, or any transport failure
# is a failure push (exit 1) — run-folder invisibility on the host that
# filled /home in 13h is itself the incident.
#
# stdout and stderr are read SEPARATELY: the pruner prints its JSON summary on
# stdout and notices (kill switch, ...) on stderr, so a notice can never
# corrupt the summary. A tripped kill switch is reported in the message of a
# breach and does not itself turn the monitor red.
#
# Run it as ROOT from cron (the Gatus token lives in the root-only env file)
# with OPERATOR_RUNS_USER set to the user that owns the run folders: the
# pruner is then started as that user (setpriv), because git refuses repos
# owned by another uid ("dubious ownership") and a root-run pruner would
# keep every folder as unverifiable. That process gets a SCRUBBED environment
# (PATH, HOME, locale, PAPERCLIP_API_*, the kill switch): the Gatus token and
# every other secret in the sourced root-only env file stay out of a process
# whose /proc/<pid>/environ the folder owner can read. PAPERCLIP_API_* is
# inherited, never passed on argv.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh);
# PAPERCLIP_API_URL, PAPERCLIP_COMPANY_ID, PAPERCLIP_API_KEY (read-only key;
# the pruner needs run statuses from the control plane); thresholds pass
# through (OPERATOR_RUNS_WARN_GIB default 20, OPERATOR_RUNS_PAGE_GIB default
# 50). OPERATOR_RUNS_USER (optional, see above). Seams for the offline suite:
# OPERATOR_PRUNE_TOOL (default: the sibling pruner), GATUS_HELPER,
# OPERATOR_RUNS_ROOT, OPERATOR_RUNS_DROP_CMD (default setpriv). Cron
# (operator, every 15m against a 30m Gatus heartbeat, like push-garm-reaper):
#   */15 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/.../ops/case_34c2c99f/push-operator-runs-disk.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="${GATUS_HELPER:-$HERE/../case_658bf6b9/_gatus_push.sh}"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-operator-runs-disk: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck source=/dev/null
. "$GATUS_HELPER"

KEY="platform_operator-runs-disk"
PRUNE_TOOL="${OPERATOR_PRUNE_TOOL:-$HERE/prune_operator_run_dirs.py}"
[[ -f "$PRUNE_TOOL" ]] \
  || { echo "push-operator-runs-disk: refusing — pruner $PRUNE_TOOL missing" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-operator-runs-disk: refusing — python3 required" >&2; exit 2; }

RUNS_ROOT="${OPERATOR_RUNS_ROOT:-/home/paperclip-operator/workspace/.paperclip-runtime/runs}"
WARN_GIB="${OPERATOR_RUNS_WARN_GIB:-20}"
PAGE_GIB="${OPERATOR_RUNS_PAGE_GIB:-50}"
[[ "$WARN_GIB" =~ ^[0-9]+$ && "$PAGE_GIB" =~ ^[0-9]+$ && "$WARN_GIB" -lt "$PAGE_GIB" ]] \
  || { echo "push-operator-runs-disk: refusing — thresholds must be integers with warn < page" >&2; exit 2; }

# The pruner cannot read run statuses without the control-plane env; naming
# the variable (never its value) beats an opaque exit 2 in the Gatus message.
for var in PAPERCLIP_API_URL PAPERCLIP_COMPANY_ID PAPERCLIP_API_KEY; do
  if [[ -z "${!var:-}" ]]; then
    gatus_push "$KEY" false "pruner env incomplete: $var is unset (cron env file must carry PAPERCLIP_API_*)"
    exit 1
  fi
done

cmd=("$PRUNE_TOOL" --runs-root "$RUNS_ROOT")
RUN_AS="${OPERATOR_RUNS_USER:-}"
if [[ -n "$RUN_AS" ]]; then
  [[ "$RUN_AS" =~ ^[a-z_][a-z0-9_-]*$ ]] \
    || { echo "push-operator-runs-disk: refusing — OPERATOR_RUNS_USER is not a plain user name" >&2; exit 2; }
  run_gid="$(id -g "$RUN_AS" 2>/dev/null)" \
    || { echo "push-operator-runs-disk: refusing — OPERATOR_RUNS_USER '$RUN_AS' does not exist" >&2; exit 2; }
  run_home="$(getent passwd "$RUN_AS" | cut -d: -f6)"
  [[ -n "$run_home" ]] \
    || { echo "push-operator-runs-disk: refusing — no home directory for '$RUN_AS'" >&2; exit 2; }
  # HOME is set so git does not read the (root) caller's unreadable config.
  cmd=("${OPERATOR_RUNS_DROP_CMD:-setpriv}" --reuid "$RUN_AS" --regid "$run_gid" \
       --init-groups -- env "HOME=$run_home" "${cmd[@]}")
fi

# Subshell-only: unset every exported variable the dropped pruner does not need.
scrub_child_env() {
  local v
  for v in $(compgen -e); do
    case "$v" in
      PATH|HOME|LANG|LC_ALL|TMPDIR|PAPERCLIP_API_URL|PAPERCLIP_COMPANY_ID|PAPERCLIP_API_KEY|OPERATOR_RUN_PRUNE_DISABLED) ;;
      *) unset "$v" 2>/dev/null ;;
    esac
  done
}

err_file="$(umask 077; mktemp "${TMPDIR:-/tmp}/operator-runs-disk.XXXXXXXX")" \
  || { echo "push-operator-runs-disk: could not create a temp file" >&2; exit 2; }
trap 'rm -f "$err_file"' EXIT
out="$( [[ -n "$RUN_AS" ]] && scrub_child_env; "${cmd[@]}" 2>"$err_file")" && rc=0 || rc=$?
if (( rc != 0 )); then
  detail="$(head -c 200 "$err_file" | tr '\n' ' ')"
  [[ -n "${detail// }" ]] || detail="$(head -c 200 <<<"$out" | tr '\n' ' ')"
  gatus_push "$KEY" false "pruner refused (exit $rc): $detail"
  exit 1
fi

# Missing/non-numeric/negative figure -> parse failure. Never default to 0:
# a healthy push built from a figure nobody measured is the false green.
parsed="$(python3 -c 'import json,sys
d = json.loads(sys.stdin.read())
v = d["runs_total_gib"]
if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
    raise ValueError("runs_total_gib not a non-negative number")
print("%.1f %s" % (float(v), "killed" if d.get("kill_switch") else "live"))' <<<"$out" 2>/dev/null)" \
  || { gatus_push "$KEY" false "pruner summary unparseable or missing runs_total_gib"; exit 1; }

figure="${parsed%% *}"
note=""
[[ "${parsed##* }" == "killed" ]] && note=" (prune kill switch is ON: nothing is being reclaimed)"

[[ "$figure" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { gatus_push "$KEY" false "run-folder figure is not a number"; exit 1; }

over_warn="$(python3 -c "print(float('$figure') >= float('$WARN_GIB'))")"
over_page="$(python3 -c "print(float('$figure') >= float('$PAGE_GIB'))")"
if [[ "$over_page" == "True" ]]; then
  gatus_push "$KEY" false "page: operator run folders use ${figure} GiB (CEO desk escalated)$note" || exit 1
  exit 1
fi
if [[ "$over_warn" == "True" ]]; then
  gatus_push "$KEY" false "warn: operator run folders use ${figure} GiB$note" || exit 1
  exit 1
fi

# A push that cannot authenticate must fail loudly so cron reports it,
# never exit 0 over an unpushed metric.
gatus_push "$KEY" true || exit 1
exit 0
