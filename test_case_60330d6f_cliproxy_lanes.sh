#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_cliproxy_lanes.sh — offline suite for the case_9f650ce2
# CLIProxy per-lane health + quota-window Gatus module
# (ops/case_658bf6b9/gatus-cliproxy-lanes.yaml, push-cliproxy-lanes.sh,
# MODULES.md registry).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — every pull endpoint declares url/conditions/alerts,
#      every external endpoint token/heartbeat/alerts, alerts are known types
#      (discord|custom). A fragment that monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: URLs and tokens
#      are ${ENV} only, no Bearer/credential literal, no auth headers in the
#      fragment (probes are bare GETs).
#   3. REFUSALS — missing GATUS_URL/token/TELEMETRY refuses (exit 2) with
#      zero pushes; unreadable or malformed telemetry pushes success=false
#      for ALL FOUR lanes (fail-closed), never silent green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS — healthy telemetry pushes 4x true; an exhausted window or
#      non-healthy account pushes that lane false (naming account + window)
#      while the rest stay true; a lane with no telemetry record is DOWN.
#   6. REGISTRY — MODULES.md lists every endpoint key, the fragment, the push
#      script and this suite (Done = merged PR + registry entry).
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-cliproxy-lanes.yaml"
PUSH="$SRC/push-cliproxy-lanes.sh"
REG="$SRC/MODULES.md"
LANES="claude codex zai opencode-go"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$REG" ]] || { echo "REFUSE: $REG not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# --- stub Gatus server: records pushes to $WORK/pushes.jsonl ----------------
cat > "$WORK/server.py" <<'PY'
import http.server, json, os, sys
PUSH = os.environ["PUSH_FILE"]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        with open(PUSH, "a") as f:
            f.write(json.dumps({"path": self.path,
                                "auth": self.headers.get("Authorization")}) + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
PORT="${case_a7d80f32_TEST_PORT:-8798}"
PUSH_FILE="$WORK/pushes.jsonl"
export PUSH_FILE
: > "$PUSH_FILE"
python3 "$WORK/server.py" "$PORT" & SRV_PID=$!
sleep 0.5
kill -0 "$SRV_PID" 2>/dev/null || { echo "REFUSE: stub server did not start"; exit 3; }

# --- curl shim: records argv, execs real curl --------------------------------
REAL_CURL="$(command -v curl)"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$WORK/argv.log"
exec "$REAL_CURL" "\$@"
SHIM
chmod +x "$WORK/bin/curl"
: > "$WORK/argv.log"

pass_count() { printf 'pass=%d fail=%d' "$PASS" "$FAIL"; }

# === 1. FRAGMENT SHAPE ========================================================
# Same structural-scan rationale as the pilot suite: STDLIB ONLY, no PyYAML.
# Keyed by (section, name) because pull and external endpoints share names.
shape_check() {  # <file> → SHAPE-OK or ERRORS:... ; exit 0 either way
  python3 - "$1" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
errs, cur, section, blocks = [], None, None, {}
for ln, raw in enumerate(lines, 1):
    if "\t" in raw: errs.append(f"L{ln}: tab"); continue
    m = re.match(r"^([A-Za-z0-9_.-]+):\s*$", raw)
    if m and m.group(1) in ("endpoints", "external-endpoints"):
        section, cur = m.group(1), None
        continue
    m = re.match(r"^\s*-\s*name:\s*[\"']?([^\"'#]+?)[\"']?\s*(?:#.*)?$", raw)
    if m:
        cur = (section, m.group(1).strip()); blocks[cur] = {"line": ln, "keys": set()}
        continue
    m = re.match(r"^(\s*)([A-Za-z0-9_.-]+):", raw)
    if not m: continue
    ind, key = len(m.group(1)), m.group(2)
    if ind == 0: cur = None
    elif cur is not None and ind == 4 and key in ("url", "conditions", "alerts", "token", "heartbeat"):
        blocks[cur]["keys"].add(key)
text = "\n".join(lines)
if text.count("{") != text.count("}"): errs.append("unbalanced braces")
if not blocks: errs.append("no named endpoint blocks found")
for (section, name), b in blocks.items():
    need = {"url", "conditions", "alerts"} if section == "endpoints" else {"token", "heartbeat", "alerts"}
    missing = need - b["keys"]
    if missing: errs.append(f"{section}/{name} (L{b['line']}): missing {sorted(missing)}")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
pulls = sorted(n for (s, n) in blocks if s == "endpoints")
exts = sorted(n for (s, n) in blocks if s == "external-endpoints")
want = ["lane-claude", "lane-codex", "lane-opencode-go", "lane-zai"]
if pulls != want: errs.append(f"pull lanes {pulls} != {want}")
if exts != want: errs.append(f"external lanes {exts} != {want}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
}
shape_out="$(shape_check "$FRAG")"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: 4 pull + 4 external lane endpoints, all alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
# positive control: an alert-less endpoint must be caught
mut="$WORK/nalert.yaml"
python3 - "$FRAG" "$mut" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
out, i, dropped = [], 0, False
while i < len(lines):
    if not dropped and re.match(r"^    alerts:\s*$", lines[i]):
        dropped = True
        i += 1
        while i < len(lines) and re.match(r"^      ", lines[i]):
            i += 1
        continue
    out.append(lines[i]); i += 1
open(sys.argv[2], "w").write("\n".join(out) + "\n")
PY
if grep -q "^ERRORS" <<<"$(shape_check "$mut")"; then ok "1c. alert-less endpoint mutant goes red"; else bad "1c. alert-less endpoint mutant" "mutant still reads clean"; fi

# === 2. NO SECRETS ============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|CLIPROXY_[A-Z_]*)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL in module" "$sec_hits"
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${CLIPROXY_LANE_CLAUDE_HEALTH_URL}' "$FRAG" \
  && grep -q '\${CLIPROXY_LANE_CODEX_HEALTH_URL}' "$FRAG" \
  && grep -q '\${CLIPROXY_LANE_ZAI_HEALTH_URL}' "$FRAG" \
  && grep -q '\${CLIPROXY_LANE_OPENCODE_GO_HEALTH_URL}' "$FRAG" \
  && grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG"; then
  ok "2b. URLs and token referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${...} references missing"; fi
# probes are bare GETs: no auth header, no credential flag in the fragment
if grep -qiE 'authorization|api-?key|bearer|basic:' <<<"$(grep -vE '^\s*#' "$FRAG")"; then
  bad "2c. auth material in fragment" "fragment must carry no credential"
else ok "2c. fragment carries no auth material (bare-GET probes)"; fi
if grep -qE 'curl .*(-H|Authorization: Bearer \$[A-Z_]+[^}])' <<<"$(grep -vE '^\s*#' "$PUSH")"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === telemetry fixtures =======================================================
mktele() {  # <out> <mode>: healthy | exhausted-codex | unhealthy-zai | missing-claude
  python3 - "$1" "$2" <<'PY'
import json, sys
def win(name, role, util, reset, weight=1.0, secs=18000):
    return {"name": name, "role": role, "utilization": util,
            "resets_at": reset, "window_seconds": secs, "allowance_weight": weight}
FAR = "2030-01-01T00:00:00Z"
mode = sys.argv[2]
recs = []
def rec(provider, key, health, wins):
    return {"account_key": key, "auth_key": key + "-auth", "provider": provider,
            "plan": "pro", "plan_weight": 1.0, "health": health,
            "recent_burn_units_per_hour": 1.0, "windows": wins,
            "stale_after_seconds": 600}
recs.append(rec("claude", "claude-a", "healthy",
                [win("five-hour", "allowance", 0.2, FAR)]))
recs.append(rec("codex", "codex-a", "healthy",
                [win("five-hour", "allowance", 1.0 if mode == "exhausted-codex" else 0.3, FAR)]))
recs.append(rec("zai", "zai-a", "unavailable" if mode == "unhealthy-zai" else "healthy",
                [win("five-hour", "allowance", 0.1, FAR)]))
if mode != "missing-claude":
    recs.append(rec("opencode-go", "go-a", "healthy",
                    [win("five-hour", "allowance", 0.4, FAR),
                     win("weekly", "allowance", 0.2, FAR),
                     win("monthly", "allowance", 0.1, FAR)]))
else:
    recs = [r for r in recs if r["provider"] != "claude"]
    recs.append(rec("opencode-go", "go-a", "healthy",
                    [win("five-hour", "allowance", 0.4, FAR),
                     win("weekly", "allowance", 0.2, FAR),
                     win("monthly", "allowance", 0.1, FAR)]))
doc = {"contract_version": 4, "observedAt": "2026-10-03T00:00:00Z", "records": recs}
open(sys.argv[1], "w").write(json.dumps(doc))
PY
}
mktele "$WORK/healthy.json" healthy
mktele "$WORK/exhausted.json" exhausted-codex
mktele "$WORK/unhealthy.json" unhealthy-zai
mktele "$WORK/missing.json" missing-claude
echo '{not json' > "$WORK/broken.json"

T="PATH=$WORK/bin:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T"
lane_push() {  # <lane> → prints success=<bool> of that lane's last push, or NOPUSH
  python3 -c "
import json,sys
lanes = {}
for line in open('$PUSH_FILE'):
    p = json.loads(line)
    path = p['path']
    m = [k for k in ('lane-claude','lane-codex','lane-zai','lane-opencode-go') if k in path]
    if m: lanes[m[0]] = ('true' if 'success=true' in path else 'false') + ' | ' + path
print(lanes.get('lane-$1', 'NOPUSH'))"
}

# === 3. REFUSALS + FAIL-CLOSED ================================================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PUSH" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3a. missing env refuses exit 2 with zero pushes"
else bad "3a. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

: > "$PUSH_FILE"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  CLIPROXY_TELEMETRY="$WORK/no-such-file.json" bash "$PUSH" >/dev/null 2>&1
rc=$?
all_down=true
# shellcheck disable=SC2086
for lane in $LANES; do
  [[ "$(lane_push "$lane")" == true* ]] && all_down=false
done
if (( rc == 1 )) && $all_down; then ok "3b. unreadable telemetry: all 4 lanes DOWN, exit 1 (fail-closed)"
else bad "3b. unreadable telemetry fail-closed" "rc=$rc claude=$(lane_push claude) codex=$(lane_push codex)"; fi

: > "$PUSH_FILE"
env $T CLIPROXY_TELEMETRY="$WORK/broken.json" bash "$PUSH" >/dev/null 2>&1
rc=$?
all_down=true
# shellcheck disable=SC2086
for lane in $LANES; do
  [[ "$(lane_push "$lane")" == true* ]] && all_down=false
done
if (( rc == 1 )) && $all_down; then ok "3c. malformed telemetry: all 4 lanes DOWN, exit 1 (fail-closed)"
else bad "3c. malformed telemetry fail-closed" "rc=$rc claude=$(lane_push claude)"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  CLIPROXY_TELEMETRY="$WORK/healthy.json" bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS ===============================================================
: > "$PUSH_FILE"
env $T CLIPROXY_TELEMETRY="$WORK/healthy.json" bash "$PUSH" >/dev/null 2>&1
rc=$?
all_up=true
# shellcheck disable=SC2086
for lane in $LANES; do
  [[ "$(lane_push "$lane")" == true* ]] || all_up=false
done
if (( rc == 0 )) && $all_up; then ok "5a. healthy telemetry pushes 4x success=true, exit 0"
else bad "5a. healthy telemetry" "rc=$rc claude=$(lane_push claude) codex=$(lane_push codex) zai=$(lane_push zai) opencode-go=$(lane_push opencode-go)"; fi

: > "$PUSH_FILE"
env $T CLIPROXY_TELEMETRY="$WORK/exhausted.json" bash "$PUSH" >/dev/null 2>&1
rc=$?
codex="$(lane_push codex)"; claude="$(lane_push claude)"
if (( rc == 1 )) && [[ "$codex" == false* ]] && [[ "$codex" == *"codex-a"* ]] && [[ "$claude" == true* ]]; then
  ok "5b. exhausted codex window pushes lane-codex false (naming account), rest true"
else bad "5b. exhausted window" "rc=$rc codex=$codex claude=$claude"; fi

: > "$PUSH_FILE"
env $T CLIPROXY_TELEMETRY="$WORK/unhealthy.json" bash "$PUSH" >/dev/null 2>&1
zai="$(lane_push zai)"; claude="$(lane_push claude)"
if [[ "$zai" == false* ]] && [[ "$zai" == *"health%3Dunavailable"* ]] && [[ "$claude" == true* ]]; then
  ok "5c. unavailable zai account pushes lane-zai false with health, rest true"
else bad "5c. unhealthy lane" "zai=$zai claude=$claude"; fi

: > "$PUSH_FILE"
env $T CLIPROXY_TELEMETRY="$WORK/missing.json" bash "$PUSH" >/dev/null 2>&1
claude="$(lane_push claude)"; codex2="$(lane_push codex)"
if [[ "$claude" == false* ]] && [[ "$codex2" == true* ]]; then
  ok "5d. lane with no telemetry record pushes false, rest true"
else bad "5d. missing lane record" "claude=$claude codex=$codex2"; fi

# === 6. REGISTRY ================================================================
reg_bad=""
for key in cliproxy_lane-claude cliproxy_lane-codex cliproxy_lane-zai cliproxy_lane-opencode-go; do
  grep -qF "$key" "$REG" || reg_bad="$reg_bad [missing key $key]"
done
for name in gatus-cliproxy-lanes.yaml push-cliproxy-lanes.sh test_case_60330d6f_cliproxy_lanes.sh; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
for lane in "cliproxy/lane-claude" "cliproxy/lane-codex" "cliproxy/lane-zai" "cliproxy/lane-opencode-go"; do
  grep -qF "$lane" "$REG" || reg_bad="$reg_bad [missing endpoint $lane]"
done
[[ -z "$reg_bad" ]] \
  && ok "6. registry lists all 8 endpoints, fragment, push script and suite" \
  || bad "6. registry coverage" "$reg_bad"

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
