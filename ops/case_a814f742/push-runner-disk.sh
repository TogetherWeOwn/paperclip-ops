#!/usr/bin/env bash
# push-runner-disk.sh — cron: runner-disk pressure → Gatus `platform_runner-disk`.
#
# case_71e7a4e9 (thresholds from the case_a814f742 document, parent case_66436e78). The
# case_231b8224 class (host disk filled to 99%, CI queued 12h+ with the evidence
# sitting unread in annotations) gets a continuous versioned Gatus signal
# instead of a hub-UI-only rule. Source-only: the operator installs the cron
# line separately on a CEO Operator card; nothing here deploys itself.
#
# What it does, once per run (cron: */5 * * * * /opt/status-page/current/ops/case_a814f742/push-runner-disk.sh):
#   1. Runs the read-only `runner_disk_triage.sh --format json` (case_534bd668:
#      host `df`, `df_inode`, suspects, LXD pool used/total).
#   2. Runs `lxc exec <vm> -- df -P /` for every VM in LXC_VMS (same default
#      list `push-lxc-state.sh` uses: "ci-node7b1ff338 ci-node7b1ff338-iso"). case_91efe922 found
#      `platform_lxc-ci-vms` is state-only; this is the per-VM disk signal.
#   3. Feeds the combined document to `evaluate-runner-disk.sh` (pure: warn
#      80 / crit 90, env-overridable) and pushes the verdict to the single
#      boolean endpoint `platform_runner-disk` via ../case_658bf6b9/_gatus_push.sh.
#      WARNING pushes DOWN so the page fires early; the message escalates
#      warning → critical → unknown (same shape as the keeper-ingest module).
#
# FAIL-CLOSED: triage missing/failed, any `lxc exec` failed, malformed JSON,
# or an UNKNOWN verdict all push success=false (exit 1) — a blind monitor
# never reads green. A missed 10-minute heartbeat fails closed server-side
# too. Missing Gatus env, bad thresholds, or a missing helper/evaluator
# refuses (exit 2) before any health claim.
#
# READ-ONLY / NO SECRETS: every probe is a read (df, lxc list/info/exec df);
# nothing is deleted, reclaimed, or restarted — reclaiming is operator-owned
# under the runbook. Push messages carry percentages + the bound only, plus
# OWNER and RUNBOOK per the case_9b48cd0a convention. The Gatus token travels in
# a 0600 curl config via the shared helper, never argv.
#
# Env:
#   GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh)
#   LXC_VMS              space-separated CI VMs (default "ci-node7b1ff338 ci-node7b1ff338-iso")
#   LXC_BIN              lxc binary (default lxc). Exported so the triage
#                      probes the same binary as the per-VM checks below.
#   TRIAGE_SKIP_SIZE_SCAN  skip the triage du scan (default 1: the evaluator
#                      never reads sizes; unset to restore the full scan)
#   TRIAGE_BIN           triage script (default <repo>/runner_disk_triage.sh)
#   RUNNER_DISK_WARN_PCT warn bound, percent (default 80)
#   RUNNER_DISK_CRIT_PCT crit bound, percent (default 90)
#   GATUS_PUSH_TIMEOUT   Gatus push timeout, seconds (default 10)
#
# Exit: 0 healthy pushed; 1 DOWN pushed (pressure or unmeasurable);
# 2 refusal (bad env, bad thresholds, missing helper) before any claim.
# `push-runner-disk.sh [-h|--help]`
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-runner-disk: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck disable=SC1090
. "$GATUS_HELPER"

for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "push-runner-disk: refusing — unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

EVAL="$HERE/evaluate-runner-disk.sh"
[[ -x "$EVAL" ]] \
  || { echo "push-runner-disk: refusing — evaluator $EVAL missing" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-runner-disk: refusing — python3 required" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-runner-disk: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-runner-disk: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

TRIAGE_BIN="${TRIAGE_BIN:-$HERE/../../runner_disk_triage.sh}"
LXC_BIN="${LXC_BIN:-lxc}"
export LXC_BIN
# The evaluator scores df/inodes/pools only; skip the du size scan so the
# push does not scan the host it guards (Paperclip Review on fd2aae77).
export TRIAGE_SKIP_SIZE_SCAN="${TRIAGE_SKIP_SIZE_SCAN:-1}"
VMS="${LXC_VMS:-ci-node7b1ff338 ci-node7b1ff338-iso}"
WARN_PCT="${RUNNER_DISK_WARN_PCT:-80}"
CRIT_PCT="${RUNNER_DISK_CRIT_PCT:-90}"
KEY="platform_runner-disk"
OWNER_RUNBOOK="OWNER devops. RUNBOOK docs/runbooks/runner-disk-triage.md"

for n in "$WARN_PCT" "$CRIT_PCT"; do
  [[ "$n" =~ ^[1-9][0-9]?$ ]] \
    || { echo "push-runner-disk: refusing — thresholds must be integers 1-99" >&2; exit 2; }
done
[[ "$WARN_PCT" -lt "$CRIT_PCT" ]] \
  || { echo "push-runner-disk: refusing — warn ($WARN_PCT) must be below crit ($CRIT_PCT)" >&2; exit 2; }

down() { gatus_push "$KEY" false "$1" || true; exit 1; }

WORK="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/runner_disk.XXXXXXXX")" \
  || { echo "push-runner-disk: refusing — no private temp dir" >&2; exit 2; }
# shellcheck disable=SC2064
trap "rm -rf '$WORK'" EXIT
TRIAGE_JSON="$WORK/triage.json"
COMBINED="$WORK/combined.json"

# --- 1. host triage (missing/failed output pushes DOWN, never green) ----------
if [[ -x "$TRIAGE_BIN" ]] && "$TRIAGE_BIN" --format json >"$TRIAGE_JSON" 2>/dev/null; then
  :
else
  down "UNKNOWN runner_disk_triage produced no JSON ($OWNER_RUNBOOK)"
fi

# --- 2. per-VM root disks (a failed exec marks that VM, never green) ----------
VM_SNIPPETS="$WORK/vms.txt"
: > "$VM_SNIPPETS"
while read -r vm; do
  [[ -n "$vm" ]] || continue
  if [[ "$vm" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    if df_out="$("$LXC_BIN" exec "$vm" -- df -P / 2>/dev/null)"; then
      python3 - "$VM_SNIPPETS" "$vm" "$df_out" <<'PY'
import json, sys
log, name, text = sys.argv[1], sys.argv[2], sys.argv[3]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"vm": name, "df": text}) + "\n")
PY
    else
      python3 - "$VM_SNIPPETS" "$vm" <<'PY'
import json, sys
log, name = sys.argv[1], sys.argv[2]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"vm": name, "error": "lxc exec df failed"}) + "\n")
PY
    fi
  else
    python3 - "$VM_SNIPPETS" "$vm" <<'PY'
import json, sys
log, name = sys.argv[1], sys.argv[2]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"vm": name, "error": "bad vm name refused"}) + "\n")
PY
  fi
done <<<"${VMS// /$'\n'}"

python3 - "$TRIAGE_JSON" "$VM_SNIPPETS" "$COMBINED" <<'PY'
import json, sys
triage_path, vms_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    doc = json.load(open(triage_path, encoding="utf-8"))
except Exception as exc:
    print(f"push-runner-disk: triage JSON unparseable: {exc}")
    sys.exit(3)
if not isinstance(doc, dict):
    print("push-runner-disk: triage JSON is not an object")
    sys.exit(3)
vms = {}
for line in open(vms_path, encoding="utf-8"):
    line = line.strip()
    if line:
        row = json.loads(line)
        name = row.pop("vm")
        vms[name] = row
doc["vms"] = vms
json.dump(doc, open(out_path, "w", encoding="utf-8"))
PY
[[ "$?" == "0" ]] || down "UNKNOWN triage JSON malformed ($OWNER_RUNBOOK)"

# --- 3. evaluate + push --------------------------------------------------------
verdict_json="$("$EVAL" --input "$COMBINED" --warn-pct "$WARN_PCT" --crit-pct "$CRIT_PCT" 2>/dev/null)" || true
if [[ -z "${verdict_json:-}" ]]; then
  down "UNKNOWN evaluator produced no verdict ($OWNER_RUNBOOK)"
fi
verdict="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("verdict",""))' <<<"$verdict_json")"
detail="$(python3 -c 'import json,sys; print("; ".join(json.load(sys.stdin).get("reasons",[]))[:400])' <<<"$verdict_json")"

case "$verdict" in
  healthy)
    gatus_push "$KEY" true || exit 1
    exit 0
    ;;
  warning)
    down "$detail ($OWNER_RUNBOOK)"
    ;;
  critical)
    down "$detail ($OWNER_RUNBOOK)"
    ;;
  *)
    down "UNKNOWN ${detail:-evaluator verdict unparseable} ($OWNER_RUNBOOK)"
    ;;
esac
