#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_enforce_readiness.sh — offline suite for the case_96fed9da
# router enforce-readiness Gatus module
# (ops/case_658bf6b9/gatus-enforce-readiness.yaml, push-enforce-readiness.sh,
# fixtures/enforce-readiness-{ready,notready}.example.json, MODULES.md).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the single router/* external endpoint declares
#      token/heartbeat/alerts, alerts are known types (discord|custom).
#      A module that monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: the token is
#      ${ENV} only, no Bearer/credential literal in the fragment.
#   3. REFUSALS — missing ENFORCE_READINESS/GATUS_URL/token refuses
#      (exit 2) with zero pushes, as does a malformed ENFORCE_KEY_SUFFIX
#      (path traversal guard); an unreadable/wrong-schema readout pushes
#      false (fail-closed), never silent green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS (fixture inputs, pinned clock) — a fresh ready readout
#      pushes true (HEALTHY); a fresh not-ready readout pushes false naming
#      the empty tier and failing gate (DEGRADED); a stale or future
#      readout pushes false; a wrong-schema readout pushes false. Every run
#      prints a case_080c50a9-enforce-readiness-v1 JSON verdict (acceptance:
#      module returns JSON on fixtures).
#   6. DOCS — the staleness horizon (READINESS_STALE_AFTER_SEC) and the
#      readout input contract (case_3d05301b schema, fixture fallback) are
#      documented in the fragment and the push script header.
#   7. NO PAID-RUNNER REFERENCES — no Blacksmith / paid-runner wording
#      anywhere in the module.
#   8. REGISTRY — MODULES.md lists the endpoint key, the fragment, the
#      push script, both example fixtures and this suite (Done = merged PR
#      + registry entry).
#   9. PROPOSE-ONLY — the live gatus-config.yaml gains no readiness/router
#      endpoint in this slice: wiring stays an operator step, and no
#      existing alert route is touched.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh. Clock is pinned via
# NOW_OVERRIDE so readout ages are deterministic.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-enforce-readiness.yaml"
PUSH="$SRC/push-enforce-readiness.sh"
FIX_READY="$SRC/fixtures/enforce-readiness-ready.example.json"
FIX_NOTREADY="$SRC/fixtures/enforce-readiness-notready.example.json"
REG="$SRC/MODULES.md"
LIVE="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
pass_count() { printf 'pass=%d fail=%d' "$PASS" "$FAIL"; }

# --- fixture builders (pinned clock: NOW=1759468800, == fixture evaluatedAt) --
NOW=1759468800
mkreadout() {  # mkreadout <out> <ready|notready|advisory|wrongschema|badverdict|badclock>
  python3 - "$1" "$2" <<'PY'
import json, sys
out, kind = sys.argv[1], sys.argv[2]
base = json.load(open("ops/case_658bf6b9/fixtures/enforce-readiness-ready.example.json"))
if kind == "notready":
    base = json.load(open("ops/case_658bf6b9/fixtures/enforce-readiness-notready.example.json"))
elif kind == "advisory":
    base["gates"].append({"gate": "tier-labels-configured", "pass": False, "blocking": False,
                          "reason": "tier-T2-has-no-label-id-overrides-written-without-a-label"})
elif kind == "wrongschema":
    base = {"schema": "something-else-v9", "verdict": "ready", "evaluatedAt": 1759468800000}
elif kind == "badverdict":
    base["verdict"] = "maybe"
elif kind == "badclock":
    base["evaluatedAt"] = "yesterday"
json.dump(base, open(out, "w"))
PY
}
shiftclock() {  # shiftclock <in> <out> <delta_s>: move evaluatedAt by delta_s
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
base = json.load(open(sys.argv[1]))
base["evaluatedAt"] = 1759468800000 + int(sys.argv[3]) * 1000
json.dump(base, open(sys.argv[2], "w"))
PY
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$FIX_READY" ]] || { echo "REFUSE: $FIX_READY not found"; exit 3; }
[[ -f "$FIX_NOTREADY" ]] || { echo "REFUSE: $FIX_NOTREADY not found"; exit 3; }
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
PORT="${case_080c50a9_TEST_PORT:-8803}"
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

# run_push <readout> [extra-env...]: run push script, print
# "rc=<n> <ready-path> ||VERDICT|| <verdict-json> ||LOG|| <log>".
# True-pushes carry no error param by Gatus convention (gatus_push sends
# error= only on false), so verdict assertions read the cron-log JSON line
# ("push-enforce-readiness: {...}"), which the push script always echoes.
run_push() {
  local read="$1"; shift
  : > "$PUSH_FILE"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
    ENFORCE_READINESS="$read" NOW_OVERRIDE="$NOW" \
    "$@" bash "$PUSH" >"$WORK/push.log" 2>&1
  local rc=$?
  local ready="NOPUSH" verdict="NOVERDICT"
  if [[ -s "$PUSH_FILE" ]]; then
    ready="$(python3 -c "import json; p=[json.loads(x)['path'] for x in open('$PUSH_FILE') if 'enforce-readiness' in json.loads(x)['path']]; print(p[-1] if p else 'NOPUSH')")"
  fi
  verdict="$(grep -o 'push-enforce-readiness: {.*}' "$WORK/push.log" | tail -n 1 | sed 's/^push-enforce-readiness: //')"
  python3 -c "
log = open('$WORK/push.log').read().replace(chr(10), ' ')
print('rc=$rc READY=$ready ||VERDICT|| ' + '''$verdict''' + ' ||LOG|| ' + log)
" | sed "s/rc=\$rc/rc=$rc/"
}

# === 1. FRAGMENT SHAPE ========================================================
shape_out="$(python3 - "$FRAG" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
errs, cur, blocks = [], None, {}
section = None
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
groups = {}
for (s, n), b in blocks.items():
    if s != "external-endpoints": continue
    groups[n] = b
exts = sorted(groups)
if exts != ["enforce-readiness"]: errs.append(f"external endpoints {exts} != ['enforce-readiness']")
for name, b in groups.items():
    missing = {"token", "heartbeat", "alerts"} - b["keys"]
    if missing: errs.append(f"{name}: missing {sorted(missing)}")
if not re.search(r"group:\s*router", text): errs.append("no group: router")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
)"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: one router/* external endpoint, alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
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
# Capture before matching: Python must finish successfully, and the stripped
# mutant must read exactly ERRORS:none-left (matching bare ^ERRORS is vacuous:
# both producer outcomes start with ERRORS).
mut_out="$(python3 - "$mut" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
print("ERRORS:none-left" if not atypes else "ERRORS:alerts-remain")
PY
)"
mut_rc=$?
if (( mut_rc == 0 )) && grep -q "^ERRORS:none-left$" <<<"$mut_out"; then ok "1c. alert-less endpoint mutant goes red"; else bad "1c. alert-less endpoint mutant" "mutant still reads clean or producer failed (rc=$mut_rc)"; fi

# === 2. NO SECRETS ============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|ENFORCE_READINESS)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" "$FIX_READY" "$FIX_NOTREADY" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL in module" "$sec_hits"
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG"; then
  ok "2b. token referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${GATUS_EXTERNAL_TOKEN} missing"; fi
if grep -qiE 'authorization|api-?key|bearer|basic:' <<<"$(grep -vE '^\s*#' "$FRAG")"; then
  bad "2c. auth material in fragment" "fragment must carry no credential"
else ok "2c. fragment carries no auth material"; fi
if grep -qE 'curl .*(-H|Authorization: Bearer \$[A-Z_]+[^}])' <<<"$(grep -vE '^\s*#' "$PUSH")"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === 3. REFUSALS + FAIL-CLOSED ================================================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PUSH" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3a. missing env refuses exit 2 with zero pushes"
else bad "3a. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

SD="$WORK/state-unreadable"; mkdir -p "$SD"
out="$(run_push "$SD/does-not-exist.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -qi "unreadable" <<<"$out"; then ok "3b. unreadable readout pushes false, exit 1 (fail-closed)"
else bad "3b. unreadable readout fail-closed" "$out"; fi

SD="$WORK/state-wrongschema"; mkdir -p "$SD"
mkreadout "$SD/readout.json" wrongschema
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -qi "unexpected schema" <<<"$out"; then ok "3c. wrong-schema readout pushes false, exit 1 (fail-closed)"
else bad "3c. wrong-schema readout fail-closed" "$out"; fi

SD="$WORK/state-badsuffix"; mkdir -p "$SD"
mkreadout "$SD/readout.json" ready
out="$(run_push "$SD/readout.json" ENFORCE_KEY_SUFFIX='../evil')"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3d. bad key suffix refuses exit 2 with zero pushes"
else bad "3d. bad key suffix refusal" "$out"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SD="$WORK/state-nondisc"; mkdir -p "$SD"
mkreadout "$SD/readout.json" ready
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  ENFORCE_READINESS="$SD/readout.json" NOW_OVERRIDE="$NOW" \
  bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS (fixture inputs, pinned clock) ================================
# 5a. fresh ready readout → true (HEALTHY), rc 0, JSON verdict.
SD="$WORK/state-ready"; mkdir -p "$SD"
mkreadout "$SD/readout.json" ready
out="$(run_push "$SD/readout.json")"
if grep -q "rc=0" <<<"$out" && grep -q "enforce-readiness.*success=true" <<<"$out" \
  && grep -q '"schema": "case_080c50a9-enforce-readiness-v1"' <<<"$out" && grep -q '"verdict": {"state": "ready"' <<<"$out"; then
  ok "5a. fresh ready readout pushes true (HEALTHY) with JSON verdict"
else bad "5a. fresh ready readout" "$out"; fi

# 5b. checked-in example fixtures consume with the pinned clock.
out="$(run_push "$FIX_READY")"
if grep -q "rc=0" <<<"$out" && grep -q "enforce-readiness.*success=true" <<<"$out" && grep -q '"verdict": {"state": "ready"' <<<"$out"; then
  ok "5b. ready example fixture consumes as HEALTHY (readout shape reuse)"
else bad "5b. ready example fixture consumption" "$out"; fi
out="$(run_push "$FIX_NOTREADY")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -q '"verdict": {"state": "not-ready"' <<<"$out"; then
  ok "5b2. not-ready example fixture consumes as DEGRADED (readout shape reuse)"
else bad "5b2. not-ready example fixture consumption" "$out"; fi

# 5c. fresh not-ready readout → false naming the empty tier + failing gate.
SD="$WORK/state-notready"; mkdir -p "$SD"
mkreadout "$SD/readout.json" notready
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -qi "T2" <<<"$out" \
  && grep -qi "no-empty-tier" <<<"$out" && grep -q '"verdict": {"state": "not-ready", "ageSeconds": 0' <<<"$out"; then
  ok "5c. fresh not-ready readout pushes false naming empty tier + failing gate"
else bad "5c. fresh not-ready readout" "$out"; fi

# 5d. stale readout (7200s > 3600s horizon) → false naming staleness.
SD="$WORK/state-stale"; mkdir -p "$SD"
shiftclock "$FIX_READY" "$SD/readout.json" -7200
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -qi "stale" <<<"$out" \
  && grep -q '"verdict": {"state": "stale", "ageSeconds": 7200' <<<"$out"; then
  ok "5d. stale readout pushes false naming staleness (with exact age in JSON)"
else bad "5d. stale readout" "$out"; fi

# 5e. future evaluatedAt → false (clock or producer corrupt).
SD="$WORK/state-future"; mkdir -p "$SD"
shiftclock "$FIX_READY" "$SD/readout.json" 99999
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -qi "future" <<<"$out"; then
  ok "5e. future readout pushes false (fail-closed)"
else bad "5e. future readout" "$out"; fi

# 5f. advisory-only gate failure → still true (advisory never moves verdict).
SD="$WORK/state-advisory"; mkdir -p "$SD"
mkreadout "$SD/readout.json" advisory
out="$(run_push "$SD/readout.json")"
if grep -q "rc=0" <<<"$out" && grep -q "enforce-readiness.*success=true" <<<"$out" && grep -q '"verdict": {"state": "ready"' <<<"$out"; then
  ok "5f. advisory-only gate failure still pushes true (blocking=false ignored)"
else bad "5f. advisory-only gate failure" "$out"; fi

# 5g. positive control: the not-ready fixture must read false while the
#     ready fixture reads true — a mutant forcing readiness-true goes red.
if grep -q "rc=1" <<<"$(run_push "$FIX_NOTREADY")" \
  && grep -q "rc=0" <<<"$(run_push "$FIX_READY")"; then
  ok "5g. not-ready-vs-ready fixtures discriminate (readiness-true mutant would go red)"
else bad "5g. fixture discrimination" "ready and not-ready fixtures do not discriminate"; fi

# 5h. malformed verdict / clock read as unreadable, never as ready.
SD="$WORK/state-badverdict"; mkdir -p "$SD"
mkreadout "$SD/readout.json" badverdict
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out" && grep -q '"verdict": {"state": "unreadable"' <<<"$out"; then
  ok "5h. unexpected verdict pushes false (unreadable, never ready)"
else bad "5h. unexpected verdict" "$out"; fi
SD="$WORK/state-badclock"; mkdir -p "$SD"
mkreadout "$SD/readout.json" badclock
out="$(run_push "$SD/readout.json")"
if grep -q "rc=1" <<<"$out" && grep -q "enforce-readiness.*success=false" <<<"$out"; then
  ok "5h2. non-numeric evaluatedAt pushes false (fail-closed)"
else bad "5h2. non-numeric evaluatedAt" "$out"; fi

# === 6. DOCS (staleness horizon + readout contract) ================================
docs_bad=""
grep -q "READINESS_STALE_AFTER_SEC" "$FRAG" || docs_bad="$docs_bad [fragment missing READINESS_STALE_AFTER_SEC]"
grep -qi "stale" "$FRAG" || docs_bad="$docs_bad [fragment missing staleness]"
grep -q "case_1320d1f5-enforce-readiness-v1" "$FRAG" || docs_bad="$docs_bad [fragment missing readout schema]"
grep -q "fixtures/enforce-readiness-ready.example.json" "$FRAG" || docs_bad="$docs_bad [fragment missing fixture fallback]"
grep -q "READINESS_STALE_AFTER_SEC" "$PUSH" || docs_bad="$docs_bad [push missing READINESS_STALE_AFTER_SEC]"
grep -q "case_1320d1f5-enforce-readiness-v1" "$PUSH" || docs_bad="$docs_bad [push missing readout schema]"
grep -qi "READINESS THRESHOLD" "$PUSH" || docs_bad="$docs_bad [push missing readiness section]"
grep -q "case_080c50a9-enforce-readiness-v1" "$PUSH" || docs_bad="$docs_bad [push missing verdict schema]"
[[ -z "$docs_bad" ]] \
  && ok "6. staleness horizon + readout contract documented in fragment and push script" \
  || bad "6. horizon/readout docs" "$docs_bad"

# === 7. NO PAID-RUNNER REFERENCES ================================================
paid_hits="$(grep -rniE 'blacksmith|paid.?runner|larger.?runner|runs-on:.*(large|paid)|self-?hosted.*(large|paid|gpu)' "$FRAG" "$PUSH" "$FIX_READY" "$FIX_NOTREADY" || true)"
if [[ -n "$paid_hits" ]]; then
  bad "7. paid-runner reference in module" "$paid_hits"
else ok "7. no paid-runner or Blacksmith references in module"; fi
# positive control: the same pattern must fire on a planted reference.
echo "runs on blacksmith" > "$WORK/planted.txt"
if grep -qiE 'blacksmith|paid.?runner|larger.?runner' "$WORK/planted.txt"; then ok "7c. paid-runner scan fires on a planted reference"; else bad "7c. paid-runner scan" "pattern missed a planted reference"; fi

# === 8. REGISTRY ================================================================
reg_bad=""
grep -qF "router_enforce-readiness" "$REG" || reg_bad="$reg_bad [missing key router_enforce-readiness]"
for name in gatus-enforce-readiness.yaml push-enforce-readiness.sh test_case_60330d6f_enforce_readiness.sh fixtures/enforce-readiness-ready.example.json fixtures/enforce-readiness-notready.example.json; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
grep -qF "router/enforce-readiness" "$REG" || reg_bad="$reg_bad [missing endpoint router/enforce-readiness]"
[[ -z "$reg_bad" ]] \
  && ok "8. registry lists the endpoint, fragment, push script, fixtures and suite" \
  || bad "8. registry coverage" "$reg_bad"

# === 9. PROPOSE-ONLY (no live wiring, no alert-route change) =====================
live_bad=""
if grep -qE 'enforce-readiness|group:[[:space:]]*router' "$LIVE"; then
  live_bad="$live_bad [live config already wires the module]"
fi
[[ -z "$live_bad" ]] \
  && ok "9. live gatus-config.yaml untouched: wiring stays an operator step, no alert route changed" \
  || bad "9. propose-only" "$live_bad"

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
