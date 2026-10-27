#!/usr/bin/env bash
# op-request <json-file> — file an approval-queue request (Part B groundwork B3).
# Validates the JSON (requires what/why/rollback, optional command key) and copies
# it into /var/lib/paperclip-operator/queue/. Executing queued items is NOT done
# here. Only accepts files owned by paperclip-operator (no exfiltration of other
# users' files through the root copy).
set -uo pipefail
LOG="/var/log/example/operator-actions.log"
QUEUE="/var/lib/paperclip-operator/queue"
refuse() { echo "op-request REFUSED: $1" >&2; echo "$(date -u +%FT%TZ) op-wrapper: op-request REFUSED $1" >> "$LOG"; exit 3; }
[ $# -eq 1 ] || { echo "usage: op-request <json-file>" >&2; exit 2; }
SRC="$1"
[ -f "$SRC" ] || refuse "not-a-file"
[ "$(stat -c %s "$SRC")" -le 16384 ] || refuse "too-large"
OP_UID="$(id -u paperclip-operator)"
[ "$(stat -c %u "$SRC")" = "$OP_UID" ] || refuse "not-operator-owned"
python3 - "$SRC" <<'PYEOF' || exit 3
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"op-request REFUSED: invalid-json {e}", file=sys.stderr); sys.exit(3)
for k in ("what", "why", "rollback"):
    v = d.get(k)
    if not isinstance(v, str) or not (1 <= len(v) <= 2000):
        print(f"op-request REFUSED: bad-or-missing-key {k}", file=sys.stderr); sys.exit(3)
if "command" in d and (not isinstance(d["command"], str) or not (1 <= len(d["command"]) <= 500)):
    print("op-request REFUSED: bad-command-key", file=sys.stderr); sys.exit(3)
print("request-ok")
PYEOF
[ -d "$QUEUE" ] || refuse "queue-missing"
DEST="$QUEUE/req-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
cp -- "$SRC" "$DEST" && chown root:paperclip-operator "$DEST" && chmod 0640 "$DEST"
echo "queued $DEST"
echo "$(date -u +%FT%TZ) op-wrapper: op-request ok dest=$(basename "$DEST")" >> "$LOG"
