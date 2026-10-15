#!/usr/bin/env bash
# case_b22b58a0 — verify the capability tools are actually reachable on this host.
#
# WHY THIS IS A SCRIPT AND NOT THREE TYPED COMMANDS
#   The defect it checks for was invisible for nine days precisely because
#   "is the capability pathway working?" was never a mechanical question with
#   one answer. It is now. Same input, same output, forever.
#
#   --pre   record the state BEFORE the config edit (expect: capability GONE)
#   --post  assert the state AFTER  the config edit (expect: capability LIVE)
#
# Reads only. Touches no config, restarts nothing. Safe to run at any time.
set -uo pipefail

CONFIG="${ORG_MCP_CONFIG:-/etc/org-request-mcp/config.json}"
MODE="${1:---post}"
UNIT="${ORG_MCP_UNIT:-org-request-mcp}"
fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=1; }

printf 'case_b22b58a0 capability-pathway gate [%s]\n' "${MODE#--}"
printf 'config: %s\n\n' "$CONFIG"

# G1 — the config exists and is readable by the operator running this.
if [ -r "$CONFIG" ]; then
  pass "G1 config is readable"
else
  bad "G1 cannot read $CONFIG (run as the operator user that owns it, or set ORG_MCP_CONFIG)"
  printf '\nRESULT: gate could not run\n'; exit 2
fi

# G2 — what the config SAYS. Parsed with python3, never grep: a commented-out
# or string-shadowed key must not read as set.
capscript="$(python3 -c '
import json,sys
try: cfg=json.load(open(sys.argv[1]))
except Exception as e: print("PARSE_ERROR:%s"%e); raise SystemExit(0)
v=cfg.get("capabilityScript")
print(v if isinstance(v,str) and v else "")
' "$CONFIG" 2>/dev/null)"

case "$capscript" in
  PARSE_ERROR:*) bad "G2 config is not valid JSON (${capscript#PARSE_ERROR:})" ;;
  "")            [ "$MODE" = "--pre" ] && pass "G2 capabilityScript is unset (the documented pre-state)" \
                                       || bad  "G2 capabilityScript is UNSET — this is the case_b22b58a0 defect; set it and restart" ;;
  *)             if [ "$(basename "$capscript")" = "capability_gate.sh" ]; then
                   pass "G2 capabilityScript -> $capscript"
                 else
                   bad "G2 capabilityScript basename is $(basename "$capscript"), not capability_gate.sh — the server will REFUSE to start"
                 fi
                 # G3 — the target actually exists and is executable.
                 if [ -x "$capscript" ]; then pass "G3 gate script exists and is executable"
                 else bad "G3 $capscript is missing or not executable — the server will refuse to start"; fi ;;
esac
[ -n "$capscript" ] || pass "G3 skipped (no capabilityScript configured)"

# G4 — what the RUNNING server reports. The config is a claim; this is the fact.
# Parse the newest structured listening event. The tools array is the actual
# advertised surface; capabilityToolsAdvertised is retained as a compatibility
# signal and must agree when both are present.
adv="$(journalctl -u "$UNIT" -n 200 --no-pager 2>/dev/null | python3 -c '
import json,sys
required={"submit_capability_request","review_capability_request","countersign_capability_request"}
latest=""
for raw in sys.stdin:
    start=raw.find("{")
    if start < 0: continue
    try: event=json.loads(raw[start:])
    except Exception: continue
    if event.get("event") != "listening": continue
    tools=event.get("tools")
    listed=None
    if isinstance(tools,list) and all(isinstance(v,str) for v in tools):
        present=required.intersection(tools)
        listed="true" if present == required else "false" if not present else "INCONSISTENT"
    flag=event.get("capabilityToolsAdvertised")
    flagged=str(flag).lower() if isinstance(flag,bool) else None
    if listed == "INCONSISTENT" or (listed is not None and flagged is not None and listed != flagged):
        latest="INCONSISTENT"
    else:
        latest=listed if listed is not None else flagged if flagged is not None else ""
print(latest)
')"
if [ -z "$adv" ]; then
  printf '  UNKNOWN G4 no usable listening event in journal for unit %s (try: ORG_MCP_UNIT=<name> %s %s)\n' "$UNIT" "$0" "$MODE"
  printf '          the running capability surface was not measured; this is not a passing gate.\n'
  [ "$fail" -ne 0 ] || fail=2
elif [ "$adv" = "INCONSISTENT" ]; then
  bad "G4 listening event is internally inconsistent — capability tools were only partly listed or the tools array disagrees with capabilityToolsAdvertised"
else
  case "$MODE:$adv" in
    --post:true)  pass "G4 running server advertises the capability tools" ;;
    --post:false) bad  "G4 running server is NOT advertising them — config edited but not restarted?" ;;
    --pre:false)  pass "G4 running server withholds them (the documented pre-state)" ;;
    --pre:true)   bad  "G4 already advertising — the premise has changed; STOP and re-read the card" ;;
  esac
fi

printf '\n'
if [ "$fail" -eq 0 ]; then printf 'RESULT: all gates passed [%s]\n' "${MODE#--}"; exit 0; fi
if [ "$fail" -eq 2 ]; then printf 'RESULT: UNKNOWN [%s]\n' "${MODE#--}"; exit 2; fi
printf 'RESULT: FAILED [%s]\n' "${MODE#--}"; exit 1
