#!/usr/bin/env bash
# run-recovery-writer.sh — the single clock for host recovery writes.
#
# One timer, one service, one writer. Detectors run first (read-only,
# measuring only) and print JSONL proposals; the single recovery writer then
# decides and applies under one policy. Nothing else on this host writes
# recovery actions.
#
# WHY THE MODE BIT LIVES HERE, ON ARGV. The sweep-auto drift was a unit
# labelled dry-run whose mode came from the environment, so an
# EnvironmentFile meant for credentials flipped a detector into a writer.
# Here --apply is passed literally below, in reviewed bundle bytes no env
# file can supply. Whether the cycle may write is therefore decided by this
# script, and how much it may write by PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS
# (0 = observe-only: decide and report, provably incapable of a write).
#
# Refusal contract: missing detector config, a failed detector, an
# unparseable detector line, or zero proposals all abort LOUDLY (nonzero)
# before the writer runs. A cycle that measured nothing must never read as
# a clean bill of health.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
WRITER="$SCRIPT_DIR/recovery_writer.py"

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

[[ -x "$WRITER" ]] || refuse "writer is not executable: $WRITER"
[[ -n "${PAPERCLIP_API_URL:-}" ]] || refuse "PAPERCLIP_API_URL is unset (env file not loaded?)"
[[ -n "${PAPERCLIP_API_KEY:-}" ]] || refuse "PAPERCLIP_API_KEY is unset (env file not loaded?)"
[[ -n "${PAPERCLIP_RECOVERY_WRITER_DETECTOR_CMD:-}" ]] \
  || refuse "PAPERCLIP_RECOVERY_WRITER_DETECTOR_CMD is unset -- no detector configured, refusing to report a clean cycle"

MAX_REPAIRS="${PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS:-0}"
[[ "$MAX_REPAIRS" =~ ^[0-9]+$ ]] || refuse "PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS='$MAX_REPAIRS' is not a non-negative integer"
DETECTOR_NAME="${PAPERCLIP_RECOVERY_WRITER_DETECTOR_NAME:-detector}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
PROPOSALS="$WORK_DIR/proposals.jsonl"

# Detectors print JSONL; type=proposal records feed the writer, cycle/summary
# lines pass through only as context. A line that is not JSON aborts: detector
# stdout is the writer's input, and corrupt input must not become a clean run.
if ! bash -c "$PAPERCLIP_RECOVERY_WRITER_DETECTOR_CMD" | DETECTOR_NAME="$DETECTOR_NAME" python3 -c '
import json, os, sys
name = os.environ["DETECTOR_NAME"]
count = 0
for lineno, line in enumerate(sys.stdin, 1):
    line = line.strip()
    if not line:
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        print(f"detector line {lineno} is not JSON: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(record, dict):
        print(f"detector line {lineno} is not an object", file=sys.stderr)
        raise SystemExit(1)
    if record.get("type", "proposal") == "proposal":
        record.setdefault("detector", name)
        print(json.dumps(record, sort_keys=True))
        count += 1
print(f"detector emitted {count} proposal(s)", file=sys.stderr)
'; then
  printf 'REFUSED: detector pipeline failed -- writer not run\n' >&2
  exit 1
fi > "$PROPOSALS"

if [[ ! -s "$PROPOSALS" ]]; then
  printf 'REFUSED: detector emitted zero proposals -- measured nothing, writer not run\n' >&2
  exit 1
fi

ARGS=(
  --apply
  --proposals "$PROPOSALS"
  --max-repairs "$MAX_REPAIRS"
)
[[ -n "${PAPERCLIP_RECOVERY_WRITER_CEO_AGENT_ID:-}" ]] \
  && ARGS+=(--ceo-agent-id "$PAPERCLIP_RECOVERY_WRITER_CEO_AGENT_ID")
[[ -n "${PAPERCLIP_RECOVERY_WRITER_OWNER_USER_ID:-}" ]] \
  && ARGS+=(--owner-user-id "$PAPERCLIP_RECOVERY_WRITER_OWNER_USER_ID")
[[ -n "${PAPERCLIP_RECOVERY_WRITER_OWNER_AGENT_ID:-}" ]] \
  && ARGS+=(--owner-agent-id "$PAPERCLIP_RECOVERY_WRITER_OWNER_AGENT_ID")
[[ -n "${PAPERCLIP_RECOVERY_WRITER_RECEIPT_OUT:-}" ]] \
  && ARGS+=(--receipt-out "$PAPERCLIP_RECOVERY_WRITER_RECEIPT_OUT")

# Credentials travel by inherited environment, never on argv: /proc cmdline
# is world-readable and every company on the host shares it.
exec /usr/bin/python3 "$WRITER" "${ARGS[@]}"
