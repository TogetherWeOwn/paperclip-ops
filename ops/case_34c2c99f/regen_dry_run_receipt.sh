#!/usr/bin/env bash
# regen_dry_run_receipt.sh — reviewed dry-run wrapper for prune_operator_regen_dirs.py (case_90645769).
#
# Replaces the unsafe queued wrapper (req-20261011T003115Z-1704370), which was
# never run because: `tr -d [:space:]` joins two custody UUIDs into one
# invalid line, the promised EXIT trap restoring PAUSE was absent, and a
# `grep fail_closed` rejects a valid `"fail_closed": null` success receipt.
#
# What this wrapper guarantees:
#   1. PAUSE starts present (paused pre-state is proven, not assumed) and is
#      restored on EVERY exit path via an EXIT trap set before PAUSE is
#      removed — success, refusal, timeout, and crash alike.
#   2. The tool NEVER runs with --apply through this wrapper: only an
#      explicit allowlist passes through (--runs-root, --max-remove,
#      --time-budget, each with its value, or the --opt=value spelling), so
#      --apply, its argparse abbreviations (--a, --ap, --appl) and any other
#      flag are refused before the tool ever runs. Defense in depth with the
#      pruner's allow_abbrev=False.
#   3. The JSON contract is validated with python, not grep: the real success
#      receipt carries `"fail_closed": null`, and null-or-absent is NOT an
#      abort, while any nonempty fail_closed MUST refuse. A `paused: true`
#      receipt (no custody_held, no kept_by_reason, no manifest) is refused:
#      it proves nothing was examined. The archive accounting keys must be
#      present (an old tool without them cannot prove the gate), and an
#      empty plan is refused: a dry run that examined nothing is not a
#      receipt, however valid its nulls.
#   4. custody_held is compared against an independent split-at-# parse of the
#      keep file (one UUID per line, optional `# annotation`), never against
#      whitespace-stripped concatenation, so multiple annotated holds count
#      exactly.
#
# Exit codes: 0 = valid zero-delete receipt; 2 = usage or precondition
# (PAUSE missing, unreadable keep file, tool missing); 3 = contract refused
# (timeout, tool error, fail_closed set, removed != 0, custody mismatch,
# paused-only receipt, unparseable summary).
#
# Usage:
#   regen_dry_run_receipt.sh --script <pruner.py> --pause <PAUSE> --keep <keep.txt>
#       [--timeout SEC] [--expect-holds N] [-- <extra tool args, never --apply>]
set -uo pipefail

SCRIPT=""; PAUSE=""; KEEP=""; TIMEOUT=900; EXPECT_HOLDS=""; TOOL_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --script) SCRIPT="${2:-}"; shift 2;;
    --pause) PAUSE="${2:-}"; shift 2;;
    --keep) KEEP="${2:-}"; shift 2;;
    --timeout) TIMEOUT="${2:-}"; shift 2;;
    --expect-holds) EXPECT_HOLDS="${2:-}"; shift 2;;
    --) shift; TOOL_ARGS=("$@"); break;;
    *) echo "receipt: unknown argument [$1]" >&2; exit 2;;
  esac
done
[[ -n "$SCRIPT" && -n "$PAUSE" && -n "$KEEP" ]] || { echo "receipt: --script, --pause and --keep are required" >&2; exit 2; }
[[ -f "$SCRIPT" ]] || { echo "receipt: tool missing [$SCRIPT]" >&2; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+$ && "$TIMEOUT" -gt 0 ]] || { echo "receipt: --timeout must be a positive integer" >&2; exit 2; }
[[ -z "$EXPECT_HOLDS" || "$EXPECT_HOLDS" =~ ^[0-9]+$ ]] || { echo "receipt: --expect-holds must be a non-negative integer" >&2; exit 2; }
ALLOW_THROUGH=("--runs-root" "--max-remove" "--time-budget")
FILTERED=()
i=0
while [[ $i -lt ${#TOOL_ARGS[@]} ]]; do
  token="${TOOL_ARGS[$i]}"
  name="${token%%=*}"
  allowed=0
  for opt in "${ALLOW_THROUGH[@]}"; do
    [[ "$name" == "$opt" ]] && allowed=1
  done
  if [[ $allowed -eq 0 ]]; then
    echo "receipt: refused pass-through [$token] (allowed: ${ALLOW_THROUGH[*]})" >&2
    exit 2
  fi
  if [[ "$token" == *"="* ]]; then
    FILTERED+=("$token"); i=$((i+1))
  else
    [[ $((i+1)) -lt ${#TOOL_ARGS[@]} ]] || { echo "receipt: [$token] needs a value" >&2; exit 2; }
    FILTERED+=("$token" "${TOOL_ARGS[$((i+1))]}"); i=$((i+2))
  fi
done
# Paused pre-state is proven, not assumed: without PAUSE present there is no
# evidence the timer was held, so there is no dry run.
[[ -f "$PAUSE" ]] || { echo "receipt: PAUSE missing [$PAUSE]; refusing to run unpaused" >&2; exit 2; }

REMOVED_PAUSE=0
restore_pause() {
  if [[ "$REMOVED_PAUSE" == "1" ]]; then
    : > "$PAUSE" 2>/dev/null || printf '' >> "$PAUSE" 2>/dev/null || true
  fi
}
trap restore_pause EXIT

rm -f "$PAUSE"
REMOVED_PAUSE=1

WORK="$(mktemp -d "${TMPDIR:-/tmp}/regen-receipt.XXXXXXXX")" || { echo "receipt: cannot create work dir" >&2; exit 2; }
trap 'restore_pause; rm -rf "$WORK"' EXIT

TIMEOUT_RC=124
# FILTERED may be empty: expand to zero args, not one empty string
# ("${FILTERED[@]:-}" passes "" which argparse rejects). The ${+...}
# form is also safe under `set -u` on bash < 4.4.
timeout "${TIMEOUT}s" python3 "$SCRIPT" ${FILTERED[@]+"${FILTERED[@]}"} > "$WORK/tool.out" 2> "$WORK/tool.err"
TOOL_RC=$?
if [[ "$TOOL_RC" -eq "$TIMEOUT_RC" ]]; then
  echo "receipt: REFUSED — tool timed out after ${TIMEOUT}s (PAUSE restored)" >&2; exit 3
fi
if [[ "$TOOL_RC" -ne 0 ]]; then
  echo "receipt: REFUSED — tool exited $TOOL_RC (PAUSE restored)" >&2
  tail -c 2000 "$WORK/tool.out" >&2 || true
  exit 3
fi

KEEP_PATH="$KEEP" EXPECT_HOLDS="$EXPECT_HOLDS" python3 - "$WORK/tool.out" <<'PYEOF'
import json
import os
import re
import sys

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

def refuse(msg):
    print(f"receipt: REFUSED — {msg} (PAUSE restored)")
    sys.exit(3)

out_path = sys.argv[1]
try:
    with open(out_path, encoding="utf-8", errors="surrogateescape") as fh:
        lines = fh.read().splitlines()
except OSError as exc:
    refuse(f"tool output unreadable: {exc}")
if not lines:
    refuse("tool printed no summary line")
try:
    summary = json.loads(lines[-1])
except ValueError:
    refuse("last tool output line is not JSON")
if not isinstance(summary, dict):
    refuse("tool summary is not a JSON object")
if summary.get("paused") is True:
    refuse("paused-only receipt proves nothing was examined")
if summary.get("mode") != "dry-run":
    refuse(f"mode is {summary.get('mode')!r}, not 'dry-run'")
# Archive accounting must be present: a summary from a tool without the
# gate cannot prove coverage, and an empty plan examined nothing.
for key in ("planned", "manifest", "archive_verified_runs", "archive_pending_runs"):
    if key not in summary:
        refuse(f"summary has no {key}; not a gated-tool receipt")
for key in ("planned", "archive_verified_runs", "archive_pending_runs"):
    if not isinstance(summary[key], int) or summary[key] < 0:
        refuse(f"{key} is not a non-negative integer: {summary[key]!r}")
if summary["planned"] == 0:
    refuse("empty plan proves nothing was examined")
if not summary.get("manifest"):
    refuse("non-empty plan without a manifest proves nothing was kept")
# The real contract: fail_closed null (or absent) is success; any nonempty
# value is an abort that must refuse. A grep for the key rejects valid null.
fail_closed = summary.get("fail_closed")
if fail_closed:
    refuse(f"fail_closed is set: {fail_closed}")
if summary.get("removed", 0) != 0:
    refuse(f"removed is {summary.get('removed')}, dry run must remove 0")
if "custody_held" not in summary:
    refuse("summary has no custody_held count")

keep_path = os.environ["KEEP_PATH"]
try:
    with open(keep_path, encoding="utf-8", errors="surrogateescape") as fh:
        text = fh.read()
except OSError as exc:
    refuse(f"keep file unreadable: {exc}")
held = set()
for line in text.splitlines():
    prefix, _, _ = line.partition("#")
    entry = prefix.strip()
    if not entry:
        continue
    if not UUID_RE.fullmatch(entry):
        refuse(f"keep file holds a non-UUID prefix: {entry[:40]!r}")
    held.add(entry)
if summary.get("custody_held") != len(held):
    refuse(f"custody_held is {summary.get('custody_held')}, keep file holds {len(held)}")
expected = os.environ.get("EXPECT_HOLDS", "")
if expected != "" and int(expected) != len(held):
    refuse(f"keep file holds {len(held)}, expected {expected}")
print(f"receipt: VALID — mode=dry-run removed=0 fail_closed=null custody_held={len(held)} "
      f"planned={summary['planned']} "
      f"archive_verified={summary['archive_verified_runs']} "
      f"archive_pending={summary['archive_pending_runs']}")
PYEOF
