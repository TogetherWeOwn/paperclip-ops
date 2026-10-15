#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_shadow_window.sh — offline suite for the case_f3715a38
# router shadow-log freshness + window-landing gauge Gatus module
# (ops/case_658bf6b9/gatus-shadow-window.yaml, push-shadow-window.sh,
# fixtures/window-landing-gauge.example.json, MODULES.md registry).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the two router/* external endpoints declare
#      token/heartbeat/alerts, alerts are known types (discord|custom).
#      A module that monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: the token is
#      ${ENV} only, no Bearer/credential literal in the fragment.
#   3. REFUSALS — missing SHADOW_DECISIONS/WINDOW_GAUGE/GATUS_URL/token
#      refuses (exit 2) with zero pushes, as does a malformed
#      SHADOW_KEY_SUFFIX (path traversal guard); an unreadable/malformed
#      shadow log pushes shadow-false and an unreadable/wrong-schema gauge
#      pushes landing-false (fail-closed), never silent green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS (fixture inputs, pinned clock) — a fresh shadow record
#      pushes shadow-true; a record older than SHADOW_STALE_AFTER_SEC pushes
#      shadow-false naming staleness; a log with no shadow record yet pushes
#      shadow-true "warming up"; a malformed line or future timestamp pushes
#      shadow-false (a torn FINAL line with no trailing newline is tolerated
#      only after an earlier complete record in that same newest shard); an
#      on-track gauge pushes landing-true; an off-band, null-projection or
#      wrong-schema gauge pushes
#      landing-false. Every run prints a case_e0da19dc-shadow-window-v1 JSON verdict
#      (acceptance: module returns JSON on fixtures).
#   5b. GAUGE STALENESS (case_9059b389) — the gauge's own evaluatedAt (epoch ms)
#      must be within WINDOW_GAUGE_MAX_AGE_SEC: an ancient evaluatedAt with
#      onTrack98_100 true goes RED (landing false, state "stale-gauge"), as do
#      a missing/non-numeric/future one; a fixture-marked gauge is always
#      rejected and can never appear green on the live landing key. Mutants
#      that drop the age or fixture guard or crash one evaluator prove the
#      checks can fail.
#   5c. SPLIT EVALUATORS — a garbled gauge never takes freshness DOWN, a bad
#      freshness horizon or crash never takes landing DOWN.
#   6. DOCS — streamed file/shard input (1,048,576-character line bound),
#      bounded warm-up, same-shard torn-tail rules, staleness horizons
#      (SHADOW_STALE_AFTER_SEC, SHADOW_WARMUP_MAX_SEC,
#      WINDOW_GAUGE_MAX_AGE_SEC), unconditional fixture refusal, and the gauge
#      input contract (case_6572bf23 schema) are documented in the fragment and
#      push script header.
#   7. NO PAID-RUNNER REFERENCES — no Blacksmith / paid-runner wording
#      anywhere in the module.
#   8. REGISTRY — MODULES.md lists both endpoint keys, the fragment, the
#      push script, the example fixture and this suite (Done = merged PR +
#      registry entry).
#   9. PROPOSE-ONLY — the live gatus-config.yaml gains no shadow/window/
#      router endpoint in this slice: wiring stays an operator step, and no
#      existing alert route is touched.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh. Clock is pinned via
# NOW_OVERRIDE so freshness ages are deterministic.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-shadow-window.yaml"
PUSH="$SRC/push-shadow-window.sh"
FIX="$SRC/fixtures/window-landing-gauge.example.json"
REG="$SRC/MODULES.md"
LIVE="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$FIX" ]] || { echo "REFUSE: $FIX not found"; exit 3; }
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
PORT="${case_e0da19dc_TEST_PORT:-8802}"
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

# --- fixture builders (pinned clock: NOW=1759468800) --------------------------
NOW=1759468800
mklog() {  # mklog <out> <spec>... — spec: shadow:<age_s> | host:<age_s> | bad | future:<age_s> | torn (partial JSON, no newline) | tornnl (same, newline-terminated) | shadownonl:<age_s> (valid, no trailing newline) | joined (two valid JSON records concatenated without a newline)
  local out="$1"; shift
  python3 - "$out" "$NOW" "$@" <<'PY'
import datetime, json, sys
out, now = sys.argv[1], int(sys.argv[2])
def ts(age): return datetime.datetime.fromtimestamp(now - age, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def record(age): return {"schema": "case_577a5ee0-decision-v1", "writer": "plugin-shadow", "issueId": "x", "ts": ts(age)}
with open(out, "w") as h:
    for spec in sys.argv[3:]:
        kind, _, arg = spec.partition(":")
        if kind == "bad":
            h.write("not json\n")
        elif kind in ("shadow", "host"):
            h.write(json.dumps({"schema": "case_577a5ee0-decision-v1", "writer": ("plugin-shadow" if kind == "shadow" else "host"), "issueId": "x", "ts": ts(int(arg))}) + "\n")
        elif kind == "future":
            h.write(json.dumps({"schema": "case_577a5ee0-decision-v1", "writer": "plugin-shadow", "issueId": "x", "ts": ts(-int(arg))}) + "\n")
        elif kind in ("torn", "tornnl"):
            h.write('{"schema": "case_577a5ee0-decision-v1", "writer": "plugin-sh' + ("\n" if kind == "tornnl" else ""))
        elif kind == "shadownonl":
            h.write(json.dumps(record(int(arg))))
        elif kind == "joined":
            h.write(json.dumps(record(300)) + json.dumps(record(120)))
PY
}
shard_stamp() {  # shard_stamp <hours-ago> -> YYYY-MM-DD-HHZ
  python3 - "$NOW" "$1" <<'PY'
import datetime, sys
now, hours = int(sys.argv[1]), int(sys.argv[2])
print(datetime.datetime.fromtimestamp(now - hours * 3600, datetime.timezone.utc).strftime("%Y-%m-%d-%HZ"))
PY
}
# The base is a LIVE-shaped gauge: the example fixture minus its fixture marker,
# evaluated 60s before the pinned clock. kind selects the variation:
#   ontrack | offband | nullproj | wrongschema | ancient (evaluatedAt=1, onTrack
#   true) | past (601s old) | fractionalpast (600.9s old) | future |
#   fractionalfuture (0.5s in the future) | missing | nonnumeric | garbled
#   (projection is a list) | hugeproj (projection too large for a float) |
#   hugeeval (evaluatedAt too large for a float) | ontracknull (onTrack true,
#   projection null) | fixture | fixturebad (marked fixture with invalid schema)
mkgauge() {  # mkgauge <out> <kind>
  python3 - "$1" "$2" "$NOW" <<'PY'
import json, sys
out, kind, now = sys.argv[1], sys.argv[2], int(sys.argv[3])
base = json.load(open("ops/case_658bf6b9/fixtures/window-landing-gauge.example.json"))
if kind not in ("fixture", "fixturebad"):
    base.pop("fixture", None)
    base["evaluatedAt"] = (now - 60) * 1000
if kind == "ancient":
    base["evaluatedAt"] = 1
elif kind == "past":
    base["evaluatedAt"] = (now - 601) * 1000
elif kind == "fractionalpast":
    base["evaluatedAt"] = (now - 600) * 1000 - 900
elif kind == "future":
    base["evaluatedAt"] = (now + 120) * 1000
elif kind == "fractionalfuture":
    base["evaluatedAt"] = now * 1000 + 500
elif kind == "missing":
    del base["evaluatedAt"]
elif kind == "nonnumeric":
    base["evaluatedAt"] = "2025-10-03T05:20:00Z"
elif kind == "garbled":
    base["joint"]["projectedLandingPct"] = [98.0]
elif kind == "hugeproj":
    base["joint"]["projectedLandingPct"] = 10 ** 400
elif kind == "hugeeval":
    base["evaluatedAt"] = 10 ** 400
elif kind == "ontracknull":
    base["joint"]["projectedLandingPct"] = None
elif kind == "offband":
    base["joint"].update({"projectedLandingPct": 150.0, "status": "overrun-risk", "onTrack98_100": False})
elif kind == "nullproj":
    base["joint"].update({"projectedLandingPct": None, "status": "not-started", "onTrack98_100": False})
elif kind == "wrongschema":
    base = {"schema": "something-else-v9", "joint": {"projectedLandingPct": 99, "status": "on-track", "onTrack98_100": True}}
elif kind == "fixturebad":
    base = {"fixture": True, "schema": "something-else-v9"}
json.dump(base, open(out, "w"))
PY
}

# run_push <log> <gauge> [extra-env...]: run push script, print
# "rc=<n> <fresh-path> <land-path> ||VERDICT|| <verdict-json> ||LOG|| <log>".
# True-pushes carry no error param by Gatus convention (gatus_push sends
# error= only on false), so verdict assertions read the cron-log JSON line
# ("push-shadow-window: {...}"), which the push script always echoes.
run_push() {
  local log="$1" gauge="$2"; shift 2
  : > "$PUSH_FILE"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
    SHADOW_DECISIONS="$log" WINDOW_GAUGE="$gauge" NOW_OVERRIDE="$NOW" \
    "$@" bash "$PUSH" >"$WORK/push.log" 2>&1
  local rc=$?
  local fresh="NOPUSH" land="NOPUSH" verdict="NOVERDICT"
  if [[ -s "$PUSH_FILE" ]]; then
    fresh="$(python3 -c "import json; p=[json.loads(x)['path'] for x in open('$PUSH_FILE') if 'shadow-freshness' in json.loads(x)['path']]; print(p[-1] if p else 'NOPUSH')")"
    land="$(python3 -c "import json; p=[json.loads(x)['path'] for x in open('$PUSH_FILE') if 'window-landing' in json.loads(x)['path']]; print(p[-1] if p else 'NOPUSH')")"
  fi
  verdict="$(grep -o 'push-shadow-window: {.*}' "$WORK/push.log" | tail -n 1 | sed 's/^push-shadow-window: //')"
  python3 -c "
log = open('$WORK/push.log').read().replace(chr(10), ' ')
print('rc=$rc FRESH=$fresh LAND=$land ||VERDICT|| ' + '''$verdict''' + ' ||LOG|| ' + log)
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
if exts != ["shadow-freshness", "window-landing"]: errs.append(f"external endpoints {exts} != ['shadow-freshness', 'window-landing']")
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
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: two router/* external endpoints, all alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
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
mut_verdict="$(python3 - "$mut" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
print("ERRORS:none-left" if not atypes else "ERRORS:alerts-remain")
PY
)"
if grep -q "^ERRORS" <<<"$mut_verdict"; then ok "1c. alert-less endpoint mutant goes red"; else bad "1c. alert-less endpoint mutant" "mutant still reads clean"; fi

# === 2. NO SECRETS ============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|SHADOW_DECISIONS|WINDOW_GAUGE)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" "$FIX" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL in module" "$sec_hits"
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG"; then
  ok "2b. token referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${GATUS_EXTERNAL_TOKEN} missing"; fi
frag_code="$(grep -vE '^\s*#' "$FRAG")"
if grep -qiE 'authorization|api-?key|bearer|basic:' <<<"$frag_code"; then
  bad "2c. auth material in fragment" "fragment must carry no credential"
else ok "2c. fragment carries no auth material"; fi
push_code="$(grep -vE '^\s*#' "$PUSH")"
if grep -qE 'curl .*(-H|Authorization: Bearer \$[A-Z_]+[^}])' <<<"$push_code"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === 3. REFUSALS + FAIL-CLOSED ================================================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PUSH" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3a. missing env refuses exit 2 with zero pushes"
else bad "3a. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

SD="$WORK/state-malformed"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" bad
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" && grep -qi "malformed" <<<"$out"; then ok "3b. malformed shadow log pushes shadow-false, exit 1 (fail-closed)"
else bad "3b. malformed shadow log fail-closed" "$out"; fi

SD="$WORK/state-badgauge"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
out="$(run_push "$SD/decisions.jsonl" "$SD/does-not-exist.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -qi "unreadable" <<<"$out"; then ok "3c. unreadable gauge pushes landing-false, exit 1 (fail-closed)"
else bad "3c. unreadable gauge fail-closed" "$out"; fi

SD="$WORK/state-wrongschema"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" wrongschema
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -qi "unexpected schema" <<<"$out"; then ok "3d. wrong-schema gauge pushes landing-false, exit 1 (fail-closed)"
else bad "3d. wrong-schema gauge fail-closed" "$out"; fi

SD="$WORK/state-badsuffix"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" SHADOW_KEY_SUFFIX='../evil')"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3e. bad key suffix refuses exit 2 with zero pushes"
else bad "3e. bad key suffix refusal" "$out"; fi

SD="$WORK/state-no-shards"; mkdir -p "$SD/shards"
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/shards" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" && grep -qi "no decisions.*shards" <<<"$out"; then
  ok "3f. empty shard directory pushes shadow-false (fail-closed)"
else bad "3f. empty shard directory" "$out"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SD="$WORK/state-nondisc"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  SHADOW_DECISIONS="$SD/decisions.jsonl" WINDOW_GAUGE="$SD/gauge.json" NOW_OVERRIDE="$NOW" \
  bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS (fixture inputs, pinned clock) ================================
# 5a. fresh shadow record + on-track gauge → both true, rc 0, JSON verdict.
SD="$WORK/state-fresh"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" host:900 shadow:300
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=0" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" && grep -q "window-landing.*success=true" <<<"$out" \
  && grep -q '"schema": "case_e0da19dc-shadow-window-v1"' <<<"$out" && grep -q '"freshness": {"state": "fresh"' <<<"$out" && grep -q '"landing": {"state": "ontrack"' <<<"$out"; then
  ok "5a. fresh log + on-track gauge push true/true with JSON verdict"
else bad "5a. fresh + on-track" "$out"; fi

# 5b. Every fixture-marked gauge is refused on the live landing key, even if
#     the obsolete opt-in variable is supplied. The checked-in fixture is stale,
#     so use a separate fresh marked copy to prove rejection is marker-based.
SD="$WORK/state-example"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mklog "$SD/decisions-host.jsonl" host:300   # no shadow record, so freshness warms independently
out="$(run_push "$SD/decisions-host.jsonl" "$FIX" WINDOW_GAUGE_ALLOW_FIXTURE=1 NOW_OVERRIDE=)"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" \
  && grep -q '"landing": {"state": "fixture-refused"' <<<"$out" && grep -q '"source": "fixture"' <<<"$out" \
  && grep -q "not%20live%20telemetry" <<<"$out"; then
  ok "5b. checked-in example fixture is always refused, including with the legacy opt-in variable"
else bad "5b. example fixture refusal" "$out"; fi

python3 - "$FIX" "$SD/fresh-fixture.json" "$NOW" <<'PY'
import json, sys
g = json.load(open(sys.argv[1])); g["evaluatedAt"] = (int(sys.argv[3]) - 60) * 1000
json.dump(g, open(sys.argv[2], "w"))
PY
fresh_fixture="$(run_push "$SD/decisions-host.jsonl" "$SD/fresh-fixture.json" WINDOW_GAUGE_ALLOW_FIXTURE=1)"
if grep -q "rc=1" <<<"$fresh_fixture" && grep -q "window-landing.*success=false" <<<"$fresh_fixture" \
  && grep -q '"landing": {"state": "fixture-refused"' <<<"$fresh_fixture"; then
  ok "5b2. fresh fixture-marked gauge is refused regardless of its live-shaped values"
else bad "5b2. fresh fixture refusal" "$fresh_fixture"; fi

# A true fixture marker takes precedence over schema validation, so malformed
# sample data is still identified as fixture-refused rather than live input.
mkgauge "$SD/malformed-fixture.json" fixturebad
malformed_fixture="$(run_push "$SD/decisions-host.jsonl" "$SD/malformed-fixture.json")"
if grep -q "rc=1" <<<"$malformed_fixture" && grep -q "window-landing.*success=false" <<<"$malformed_fixture" \
  && grep -q '"landing": {"state": "fixture-refused"' <<<"$malformed_fixture"; then
  ok "5b2a. fixture marker is refused before malformed-schema validation"
else bad "5b2a. malformed fixture refusal" "$malformed_fixture"; fi

# Positive control: removing the fixture guard lets that same fresh marked
# gauge reach the live on-track branch, so the refusal assertion above is not
# vacuous.
mkdir -p "$WORK/mut-no-fixture-guard"
cp "$SRC/_gatus_push.sh" "$WORK/mut-no-fixture-guard/"
python3 - "$PUSH" "$WORK/mut-no-fixture-guard/push-shadow-window.sh" <<'PY'
import sys
source = open(sys.argv[1]).read()
needle = "if fixture:"
assert source.count(needle) == 1, "fixture-guard mutation anchor moved"
open(sys.argv[2], "w").write(source.replace(needle, "if False and fixture:", 1))
PY
chmod +x "$WORK/mut-no-fixture-guard/push-shadow-window.sh"
PUSH_BEFORE_FIXTURE_MUTANT="$PUSH"
PUSH="$WORK/mut-no-fixture-guard/push-shadow-window.sh"
fixture_mutant="$(run_push "$SD/decisions-host.jsonl" "$SD/fresh-fixture.json" WINDOW_GAUGE_ALLOW_FIXTURE=1)"
PUSH="$PUSH_BEFORE_FIXTURE_MUTANT"
if grep -q "rc=0" <<<"$fixture_mutant" && grep -q "window-landing.*success=true" <<<"$fixture_mutant" \
  && grep -q '"landing": {"state": "ontrack"' <<<"$fixture_mutant"; then
  ok "5b3. fixture-guard mutant reproduces green live success for a fresh marked fixture"
else bad "5b3. fixture-guard mutant" "$fixture_mutant"; fi

# 5b4. a copy of the fixture with the marker stripped is a live-shaped gauge
#      with a frozen (ancient) evaluatedAt: stale-gauge, never silently live.
python3 - "$FIX" "$SD/unmarked.json" <<'PY'
import json, sys
g = json.load(open(sys.argv[1])); g.pop("fixture", None)
json.dump(g, open(sys.argv[2], "w"))
PY
out="$(run_push "$SD/decisions-host.jsonl" "$SD/unmarked.json" WINDOW_GAUGE_ALLOW_FIXTURE=1 NOW_OVERRIDE=)"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -q '"landing": {"state": "stale-gauge"' <<<"$out"; then
  ok "5b4. fixture with the marker stripped reads stale-gauge, DOWN (cannot pose as live)"
else bad "5b4. unmarked fixture" "$out"; fi

# 5c. stale record (7200s > 3600s horizon) → shadow-false naming staleness.
SD="$WORK/state-stale"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:7200
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" && grep -qi "stale" <<<"$out" \
  && grep -q '"freshness": {"state": "stale", "ageSeconds": 7200' <<<"$out"; then
  ok "5c. stale shadow record pushes false naming staleness (with exact age in JSON)"
else bad "5c. stale shadow record" "$out"; fi

# 5d. host-only log at the warm-up boundary → bounded shadow-true warm-up.
SD="$WORK/state-warm"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" host:600
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=0" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" && grep -qi "warming up for 600s of 600s" <<<"$out"; then
  ok "5d. shadow-less log warms only through SHADOW_WARMUP_MAX_SEC"
else bad "5d. bounded warm-up" "$out"; fi

# 5d2. host-only log beyond the warm-up horizon → DOWN, not green silence.
SD="$WORK/state-warm-expired"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" host:601
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" \
  && grep -qi "warm-up" <<<"$out" && grep -q '"freshness": {"state": "warming-expired"' <<<"$out"; then
  ok "5d2. host-only log past the warm-up horizon pushes false"
else bad "5d2. expired warm-up" "$out"; fi

# 5d3. a physically empty file ages out from its mtime too.
SD="$WORK/state-empty-expired"; mkdir -p "$SD"
: > "$SD/decisions.jsonl"
touch -d "@$((NOW - 601))" "$SD/decisions.jsonl"
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" \
  && grep -qi "warm-up" <<<"$out" && grep -q '"freshness": {"state": "warming-expired"' <<<"$out"; then
  ok "5d3. stale empty log cannot remain green indefinitely"
else bad "5d3. empty-log warm-up expiry" "$out"; fi

# 5e. future timestamp → shadow-false (clock or log corrupt).
SD="$WORK/state-future"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" future:99999
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" && grep -qi "future" <<<"$out"; then
  ok "5e. future shadow timestamp pushes false (fail-closed)"
else bad "5e. future timestamp" "$out"; fi

# 5f. off-band gauge (joint 150%) → landing-false naming the landing.
SD="$WORK/state-offband"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" offband
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -qi "150" <<<"$out" \
  && grep -q '"landing": {"state": "off-band", "projectedLandingPct": 150.0' <<<"$out"; then
  ok "5f. off-band joint landing pushes false naming the projection"
else bad "5f. off-band gauge" "$out"; fi

# 5g. null projection (window not started) → landing-false, never a null read as zero.
SD="$WORK/state-nullproj"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" nullproj
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -qi "not-started" <<<"$out" \
  && grep -q '"landing": {"state": "not-started", "projectedLandingPct": null' <<<"$out"; then
  ok "5g. null projection pushes false (not-started is DOWN, never zero)"
else bad "5g. null projection" "$out"; fi

# 5h. positive control: the stale fixture (5c) must read false while the
#     fresh fixture (5a) reads true — a mutant forcing shadow-true goes red.
if grep -q "rc=1" <<<"$(run_push "$WORK/state-stale/decisions.jsonl" "$WORK/state-stale/gauge.json")" \
  && grep -q "rc=0" <<<"$(run_push "$WORK/state-fresh/decisions.jsonl" "$WORK/state-fresh/gauge.json")"; then
  ok "5h. stale-vs-fresh fixtures discriminate (shadow-true mutant would go red)"
else bad "5h. fixture discrimination" "stale and fresh fixtures do not discriminate"; fi

# === 5i. GAUGE STALENESS (case_9059b389) ============================================
# 5i. An ancient evaluatedAt (=1) with onTrack98_100 true must stay DOWN with
#     state stale-gauge; freshness is unaffected. The base already has the age
#     guard, while this branch preserves it and tightens precision/state tests.
SD="$WORK/state-ancient"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ancient
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
  && grep -q '"landing": {"state": "stale-gauge", "projectedLandingPct": null' <<<"$out" && grep -q "error=window%20gauge%20stale" <<<"$out"; then
  ok "5i. ancient evaluatedAt + onTrack true goes RED: landing false, state stale-gauge (freshness stays true)"
else bad "5i. ancient evaluatedAt with onTrack true" "$out"; fi

# 5i2. the age boundary: the base gauge is 60s old. Max age 60 -> fresh (true);
#      max age 59 -> stale. 601s old against the 600s default -> stale.
SD="$WORK/state-agebound"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
at60="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" WINDOW_GAUGE_MAX_AGE_SEC=60)"
at59="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" WINDOW_GAUGE_MAX_AGE_SEC=59)"
mkgauge "$SD/past.json" past
past="$(run_push "$SD/decisions.jsonl" "$SD/past.json")"
if grep -q "rc=0" <<<"$at60" && grep -q '"landing": {"state": "ontrack", "projectedLandingPct": 98.0, "note": "on-track", "gaugeAgeSeconds": 60' <<<"$at60" \
  && grep -q "rc=1" <<<"$at59" && grep -q '"state": "stale-gauge"' <<<"$at59" \
  && grep -q "rc=1" <<<"$past" && grep -q '"state": "stale-gauge"' <<<"$past" && grep -q '"gaugeAgeSeconds": 601' <<<"$past"; then
  ok "5i2. age boundary: age == max is fresh (gaugeAgeSeconds reported), age > max is stale; default 600s"
else bad "5i2. age boundary" "at60=$at60 | at59=$at59 | past=$past"; fi

# Fractional seconds must be compared before display rounding: 600.9s is stale,
# and even a sub-second future timestamp is not fresh.
mkgauge "$SD/fractionalpast.json" fractionalpast
fractional_past="$(run_push "$SD/decisions.jsonl" "$SD/fractionalpast.json")"
mkgauge "$SD/fractionalfuture.json" fractionalfuture
fractional_future="$(run_push "$SD/decisions.jsonl" "$SD/fractionalfuture.json")"
if grep -q "rc=1" <<<"$fractional_past" && grep -q '"state": "stale-gauge"' <<<"$fractional_past" \
  && grep -q '"gaugeAgeSeconds": 601' <<<"$fractional_past" \
  && grep -q "rc=1" <<<"$fractional_future" && grep -q '"state": "stale-gauge"' <<<"$fractional_future" \
  && grep -q "evaluatedAt is in the future by 0.500s" <<<"$fractional_future"; then
  ok "5i2a. fractional stale and future gauge ages fail closed before rounding"
else bad "5i2a. fractional gauge age" "past=$fractional_past | future=$fractional_future"; fi

# 5i3. missing, non-numeric (incl. an int too large for a float) and future
#      evaluatedAt are stale too (fail-closed).
for kind in missing nonnumeric hugeeval future; do
  SD="$WORK/state-evaluated-$kind"; mkdir -p "$SD"
  mklog "$SD/decisions.jsonl" shadow:300
  mkgauge "$SD/gauge.json" "$kind"
  out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
  case "$kind" in missing) want="evaluatedAt missing";; nonnumeric|hugeeval) want="evaluatedAt is not a number";; future) want="evaluatedAt is in the future";; esac
  if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" \
    && grep -q '"landing": {"state": "stale-gauge"' <<<"$out" && grep -q "$want" <<<"$out"; then
    ok "5i3. $kind evaluatedAt pushes false, state stale-gauge ($want)"
  else bad "5i3. $kind evaluatedAt" "$out"; fi
done

# 5i4. an off-band stale gauge reads stale-gauge, not off-band: a frozen value
#      is never reported as a landing at all.
SD="$WORK/state-stale-offband"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" offband
python3 - "$SD/gauge.json" <<'PY'
import json, sys
g = json.load(open(sys.argv[1])); g["evaluatedAt"] = 1
json.dump(g, open(sys.argv[1], "w"))
PY
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q '"landing": {"state": "stale-gauge", "projectedLandingPct": null' <<<"$out" && ! grep -q "off-band" <<<"$out"; then
  ok "5i4. stale + off-band reads stale-gauge (staleness outranks the band; no frozen projection reported)"
else bad "5i4. stale outranks band" "$out"; fi

# 5i5. a bad WINDOW_GAUGE_MAX_AGE_SEC is landing's own fail-closed DOWN.
for bad_age in abc 0 -5 1.5; do
  SD="$WORK/state-badmax"; mkdir -p "$SD"
  mklog "$SD/decisions.jsonl" shadow:300
  mkgauge "$SD/gauge.json" ontrack
  out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" WINDOW_GAUGE_MAX_AGE_SEC="$bad_age")"
  if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
    && grep -q '"landing": {"state": "bad-config"' <<<"$out"; then
    ok "5i5. WINDOW_GAUGE_MAX_AGE_SEC='$bad_age' pushes landing-false (bad-config), freshness unaffected"
  else bad "5i5. bad WINDOW_GAUGE_MAX_AGE_SEC='$bad_age'" "$out"; fi
done

# 5i6. MUTANT control: delete the age comparison from a copy of the push script
#      and the ancient gauge (5i) must read landing TRUE, proving the guard
#      assertion can go red. The base already has this guard. Run via a copy:
#      the real script stays untouched.
MUT="$WORK/mut-noage"; mkdir -p "$MUT"
cp "$SRC/_gatus_push.sh" "$MUT/"
python3 - "$PUSH" "$MUT/push-shadow-window.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = "elif age_exact > max_age:"
assert s.count(old) == 1, "mutation anchor moved: update the 5i6 mutant"
open(sys.argv[2], "w").write(s.replace(old, "elif False:"))
PY
chmod +x "$MUT/push-shadow-window.sh"
PUSH_REAL="$PUSH"; PUSH="$MUT/push-shadow-window.sh"
out="$(run_push "$WORK/state-ancient/decisions.jsonl" "$WORK/state-ancient/gauge.json")"
PUSH="$PUSH_REAL"
if grep -q "rc=0" <<<"$out" && grep -q "window-landing.*success=true" <<<"$out"; then
  ok "5i6. mutant without the age comparison pushes the ancient gauge TRUE (5i can go red)"
else bad "5i6. no-age mutant" "mutant did not reproduce the bug: $out"; fi

# 5i7. real clock (NOW_OVERRIDE unset): a gauge evaluated just now reads fresh
#      (evaluatedAt really is epoch MILLISECONDS), the same gauge 10 minutes
#      later reads stale. Host-only log so freshness is "warming up" any date.
SD="$WORK/state-realclock"; mkdir -p "$SD"
python3 - "$SD/decisions.jsonl" <<'PY'
import datetime, json, sys
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
with open(sys.argv[1], "w") as h:
    h.write(json.dumps({"schema": "case_577a5ee0-decision-v1", "writer": "host", "issueId": "x", "ts": stamp}) + "\n")
PY
mkgauge "$SD/gauge.json" ontrack
python3 - "$SD/gauge.json" "$SD/gauge-old.json" <<'PY'
import json, sys, time
g = json.load(open(sys.argv[1])); now_ms = int(time.time() * 1000)
g["evaluatedAt"] = now_ms - 5_000
json.dump(g, open(sys.argv[1], "w"))
g["evaluatedAt"] = now_ms - 1_200_000
json.dump(g, open(sys.argv[2], "w"))
PY
live="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" NOW_OVERRIDE=)"
old="$(run_push "$SD/decisions.jsonl" "$SD/gauge-old.json" NOW_OVERRIDE=)"
if grep -q "rc=0" <<<"$live" && grep -q '"landing": {"state": "ontrack"' <<<"$live" \
  && grep -q "rc=1" <<<"$old" && grep -q '"landing": {"state": "stale-gauge"' <<<"$old"; then
  ok "5i7. real clock: evaluatedAt (epoch ms) 5s old is fresh, 20min old is stale-gauge"
else bad "5i7. real-clock gauge age" "live=$live | old=$old"; fi

# === 5j. SPLIT EVALUATORS ========================================================
# 5j. A malformed numeric projection takes ONLY landing DOWN; freshness stays
#     true. Null projection remains the ordinary not-started state below.
for kind in garbled hugeproj; do
  SD="$WORK/state-$kind"; mkdir -p "$SD"
  mklog "$SD/decisions.jsonl" shadow:300
  mkgauge "$SD/gauge.json" "$kind"
  out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
  if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
    && grep -q '"landing": {"state": "unreadable"' <<<"$out" && grep -q '"freshness": {"state": "fresh"' <<<"$out"; then
    ok "5j. $kind gauge: landing false (unreadable), freshness stays true"
  else bad "5j. $kind gauge" "$out"; fi
done

# A null projection is not-started even when the on-track bit is contradictory.
SD="$WORK/state-ontracknull"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontracknull
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "window-landing.*success=false" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
  && grep -q '"landing": {"state": "not-started"' <<<"$out" && grep -q '"freshness": {"state": "fresh"' <<<"$out"; then
  ok "5j1. null projection stays not-started and only landing goes DOWN"
else bad "5j1. on-track null projection" "$out"; fi

# 5j2. a bad SHADOW_STALE_AFTER_SEC takes ONLY freshness DOWN; landing stays true.
SD="$WORK/state-badhorizon"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" SHADOW_STALE_AFTER_SEC=abc)"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" && grep -q "window-landing.*success=true" <<<"$out" \
  && grep -q '"freshness": {"state": "bad-config"' <<<"$out"; then
  ok "5j2. bad SHADOW_STALE_AFTER_SEC takes only freshness DOWN (landing stays true)"
else bad "5j2. bad freshness horizon" "$out"; fi

# 5j3. MUTANTS: crash one evaluator outright. Each takes only ITS endpoint DOWN
#      ("evaluator failure"); the other keeps its own true.
crash_mutant() {  # crash_mutant <outdir> <anchor> — inject a RuntimeError after <anchor>
  mkdir -p "$1"; cp "$SRC/_gatus_push.sh" "$1/"
  python3 - "$PUSH" "$1/push-shadow-window.sh" "$2" <<'PY'
import sys
s = open(sys.argv[1]).read(); anchor = sys.argv[3]
assert s.count(anchor) == 1, "mutation anchor moved: update the 5j3 mutant"
at = s.index(anchor); indent = s[s.rfind("\n", 0, at) + 1:at]
open(sys.argv[2], "w").write(s.replace(anchor, anchor + "\n" + indent + 'raise RuntimeError("boom")'))
PY
  chmod +x "$1/push-shadow-window.sh"
}
SD="$WORK/state-crash"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
crash_mutant "$WORK/mut-crash-land" 'gauge_path, max_age_s = sys.argv[2:4]'
crash_mutant "$WORK/mut-crash-fresh" 'dec_path, stale_s = sys.argv[2:4]'
PUSH="$WORK/mut-crash-land/push-shadow-window.sh"
land_crash="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" 2>&1)"
PUSH="$WORK/mut-crash-fresh/push-shadow-window.sh"
fresh_crash="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json" 2>&1)"
PUSH="$PUSH_REAL"
if grep -q "window-landing.*success=false" <<<"$land_crash" && grep -q "shadow-freshness.*success=true" <<<"$land_crash" \
  && grep -q '"landing": {"state": "evaluator-failed"' <<<"$land_crash" && grep -q '"freshness": {"state": "fresh"' <<<"$land_crash" \
  && grep -q "shadow-freshness.*success=false" <<<"$fresh_crash" && grep -q "window-landing.*success=true" <<<"$fresh_crash" \
  && grep -q '"freshness": {"state": "evaluator-failed"' <<<"$fresh_crash" && grep -q '"landing": {"state": "ontrack"' <<<"$fresh_crash"; then
  ok "5j3. a crashed evaluator takes only its own endpoint DOWN (landing-crash and freshness-crash mutants)"
else bad "5j3. evaluator isolation" "land_crash=$land_crash | fresh_crash=$fresh_crash"; fi

# === 5k. TORN TAIL (final line only) =============================================
# 5k. An unterminated malformed final line is tolerated only after at least
#     one complete record was parsed in the same newest shard; it cannot borrow
#     the prerequisite from an older shard.
SD="$WORK/state-torn"; mkdir -p "$SD"
mklog "$SD/decisions.jsonl" shadow:300 torn
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=0" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
  && grep -q '"freshness": {"state": "fresh", "ageSeconds": 300, "note": null, "tornTail": true}' <<<"$out"; then
  ok "5k. torn final line after a complete record is ignored and noted"
else bad "5k. torn final line" "$out"; fi

# 5k2. A lone malformed tail, concatenated records, and a concatenated final
#      line after a valid record are malformed/DOWN. Other controls keep
#      newline-terminated/middle corruption DOWN, stale history stale, and a
#      valid unterminated record countable.
SD="$WORK/state-torn-ctl"; mkdir -p "$SD"
mkgauge "$SD/gauge.json" ontrack
mklog "$SD/lone.jsonl" torn
mklog "$SD/joined.jsonl" joined
mklog "$SD/prefixed-joined.jsonl" shadow:300 joined
touch -d "@$NOW" "$SD/lone.jsonl" "$SD/joined.jsonl"
mklog "$SD/terminated.jsonl" shadow:300 tornnl
mklog "$SD/middle.jsonl" shadow:300 tornnl shadow:120
mklog "$SD/stale.jsonl" shadow:7200 torn
mklog "$SD/nonl.jsonl" shadow:7200 shadownonl:120
t_lone="$(run_push "$SD/lone.jsonl" "$SD/gauge.json")"
t_joined="$(run_push "$SD/joined.jsonl" "$SD/gauge.json")"
t_prefixed_joined="$(run_push "$SD/prefixed-joined.jsonl" "$SD/gauge.json")"
t_nl="$(run_push "$SD/terminated.jsonl" "$SD/gauge.json")"
t_mid="$(run_push "$SD/middle.jsonl" "$SD/gauge.json")"
t_stale="$(run_push "$SD/stale.jsonl" "$SD/gauge.json")"
t_nonl="$(run_push "$SD/nonl.jsonl" "$SD/gauge.json")"
if grep -q '"freshness": {"state": "malformed"' <<<"$t_lone" && grep -q "shadow-freshness.*success=false" <<<"$t_lone" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$t_joined" && grep -q "shadow-freshness.*success=false" <<<"$t_joined" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$t_prefixed_joined" && grep -q "line 2 is not JSON" <<<"$t_prefixed_joined" \
  && ! grep -q '"tornTail": true' <<<"$t_prefixed_joined" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$t_nl" && grep -q "shadow-freshness.*success=false" <<<"$t_nl" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$t_mid" && grep -q "line 2 is not JSON" <<<"$t_mid" \
  && grep -q '"freshness": {"state": "stale"' <<<"$t_stale" && grep -q '"tornTail": true' <<<"$t_stale" \
  && grep -q '"freshness": {"state": "fresh", "ageSeconds": 120, "note": null}' <<<"$t_nonl"; then
  ok "5k2. only an incomplete final tail is tolerated; lone/joined/terminated/middle corruption stays DOWN"
else bad "5k2. torn-tail controls" "lone=$t_lone | joined=$t_joined | prefixed_joined=$t_prefixed_joined | terminated=$t_nl | middle=$t_mid | stale=$t_stale | nonl=$t_nonl"; fi

# Positive control: removing the torn-tail prerequisites recreates both
# false-warming cases, proving the malformed-input assertions exercise the guard.
mkdir -p "$WORK/mut-unconditional-torn"
cp "$SRC/_gatus_push.sh" "$WORK/mut-unconditional-torn/"
python3 - "$PUSH" "$WORK/mut-unconditional-torn/push-shadow-window.sh" <<'PY'
import sys
source = open(sys.argv[1]).read()
needle = 'and saw_complete_line and exc.msg != "Extra data"):'
assert source.count(needle) == 1, "torn-tail mutation anchor moved"
open(sys.argv[2], "w").write(source.replace(needle, '):', 1))
PY
chmod +x "$WORK/mut-unconditional-torn/push-shadow-window.sh"
PUSH_BEFORE_TORN_MUTANT="$PUSH"
PUSH="$WORK/mut-unconditional-torn/push-shadow-window.sh"
lone_mutant="$(run_push "$SD/lone.jsonl" "$SD/gauge.json")"
joined_mutant="$(run_push "$SD/joined.jsonl" "$SD/gauge.json")"
PUSH="$PUSH_BEFORE_TORN_MUTANT"
if grep -q "rc=0" <<<"$lone_mutant" && grep -q '"freshness": {"state": "warming"' <<<"$lone_mutant" \
  && grep -q '"tornTail": true' <<<"$lone_mutant" \
  && grep -q "rc=0" <<<"$joined_mutant" && grep -q '"freshness": {"state": "warming"' <<<"$joined_mutant" \
  && grep -q '"tornTail": true' <<<"$joined_mutant"; then
  ok "5k3. unconditional-torn mutant recreates warming/green for lone and concatenated malformed logs"
else bad "5k3. unconditional-torn mutant" "lone=$lone_mutant | joined=$joined_mutant"; fi

# Positive control: dropping the Extra data rejection makes a joined tail
# after a valid record falsely read fresh and green.
mkdir -p "$WORK/mut-extra-data-torn"
cp "$SRC/_gatus_push.sh" "$WORK/mut-extra-data-torn/"
python3 - "$PUSH" "$WORK/mut-extra-data-torn/push-shadow-window.sh" <<'PY'
import sys
source = open(sys.argv[1]).read()
needle = 'and exc.msg != "Extra data"):'
assert source.count(needle) == 1, "Extra data mutation anchor moved"
open(sys.argv[2], "w").write(source.replace(needle, "):", 1))
PY
chmod +x "$WORK/mut-extra-data-torn/push-shadow-window.sh"
PUSH_BEFORE_EXTRA_DATA_MUTANT="$PUSH"
PUSH="$WORK/mut-extra-data-torn/push-shadow-window.sh"
extra_data_mutant="$(run_push "$SD/prefixed-joined.jsonl" "$SD/gauge.json")"
PUSH="$PUSH_BEFORE_EXTRA_DATA_MUTANT"
if grep -q "rc=0" <<<"$extra_data_mutant" && grep -q '"freshness": {"state": "fresh"' <<<"$extra_data_mutant" \
  && grep -q '"tornTail": true' <<<"$extra_data_mutant"; then
  ok "5k4. removing Extra data guard reproduces false-green concatenated tail"
else bad "5k4. Extra data mutant" "$extra_data_mutant"; fi

# === 5l. HOURLY SHARDS ============================================================
# 5l. Select the newest plugin-shadow record across shard rollover.
SD="$WORK/state-shards-fresh"; mkdir -p "$SD/shards"
mklog "$SD/shards/decisions-$(shard_stamp 2).jsonl" shadow:7200
mklog "$SD/shards/decisions-$(shard_stamp 0).jsonl" host:60 shadow:300
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/shards" "$SD/gauge.json")"
if grep -q "rc=0" <<<"$out" && grep -q "shadow-freshness.*success=true" <<<"$out" \
  && grep -q '"freshness": {"state": "fresh", "ageSeconds": 300' <<<"$out"; then
  ok "5l. shard directory selects newest plugin-shadow record"
else bad "5l. fresh hourly shards" "$out"; fi

# 5l2. A host-only newest shard cannot hide an old shadow record.
SD="$WORK/state-shards-stale"; mkdir -p "$SD/shards"
mklog "$SD/shards/decisions-$(shard_stamp 2).jsonl" shadow:7200
mklog "$SD/shards/decisions-$(shard_stamp 0).jsonl" host:60
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/shards" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" \
  && grep -qi "stale" <<<"$out" && grep -q '"freshness": {"state": "stale", "ageSeconds": 7200' <<<"$out"; then
  ok "5l2. previous shard shadow record expires despite newer host records"
else bad "5l2. stale hourly shards" "$out"; fi

# 5l3. A valid record in an older shard cannot authorize a torn tail in the
#      newest shard; torn-tail tolerance is local to that newest shard.
SD="$WORK/state-shards-torn"; mkdir -p "$SD/shards"
mklog "$SD/shards/decisions-$(shard_stamp 2).jsonl" shadow:300
printf '{"schema":"case_577a5ee0-decision-v1","writer":"plugin-shadow"' > "$SD/shards/decisions-$(shard_stamp 0).jsonl"
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/shards" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$out" && grep -q 'line 1 is not JSON' <<<"$out" \
  && ! grep -q '"tornTail": true' <<<"$out"; then
  ok "5l3. older shard cannot authorize newest-shard torn-tail tolerance"
else bad "5l3. cross-shard torn tail" "$out"; fi

# 5l3b. A torn tail in an older shard is corruption, even after a complete
#       record and with fresh shadow activity in the newest shard.
SD="$WORK/state-shards-older-torn"; mkdir -p "$SD/shards"
OLDER_SHARD="decisions-$(shard_stamp 2).jsonl"
mklog "$SD/shards/$OLDER_SHARD" shadow:7200 torn
mklog "$SD/shards/decisions-$(shard_stamp 0).jsonl" shadow:300
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/shards" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q "shadow-freshness.*success=false" <<<"$out" \
  && grep -q "window-landing.*success=true" <<<"$out" \
  && grep -q '"freshness": {"state": "malformed"' <<<"$out" \
  && grep -Fq "$OLDER_SHARD line 2 is not JSON" <<<"$out" \
  && ! grep -q '"tornTail"' <<<"$out"; then
  ok "5l3b. older-shard torn tail stays malformed/DOWN despite fresh newest shard"
else bad "5l3b. older-shard torn tail" "$out"; fi

# Positive control: removing the newest-shard guard makes the same corrupt
# history falsely read fresh and green.
mkdir -p "$WORK/mut-older-shard-torn"
cp "$SRC/_gatus_push.sh" "$WORK/mut-older-shard-torn/"
python3 - "$PUSH" "$WORK/mut-older-shard-torn/push-shadow-window.sh" <<'PY'
import sys
source = open(sys.argv[1]).read()
needle = 'path_index == len(paths) - 1 and unterminated'
assert source.count(needle) == 1, "newest-shard mutation anchor moved"
open(sys.argv[2], "w").write(source.replace(needle, "unterminated", 1))
PY
chmod +x "$WORK/mut-older-shard-torn/push-shadow-window.sh"
PUSH_BEFORE_OLDER_SHARD_MUTANT="$PUSH"
PUSH="$WORK/mut-older-shard-torn/push-shadow-window.sh"
older_shard_mutant="$(run_push "$SD/shards" "$SD/gauge.json")"
PUSH="$PUSH_BEFORE_OLDER_SHARD_MUTANT"
if grep -q "rc=0" <<<"$older_shard_mutant" && grep -q "shadow-freshness.*success=true" <<<"$older_shard_mutant" \
  && grep -q '"freshness": {"state": "fresh", "ageSeconds": 300' <<<"$older_shard_mutant" \
  && grep -q '"tornTail": true' <<<"$older_shard_mutant"; then
  ok "5l3c. removing newest-shard guard reproduces false-green older torn tail"
else bad "5l3c. newest-shard mutant" "$older_shard_mutant"; fi

# 5l4. A large supported single-file log is streamed under a bounded address
#      space; whole-file read/split would exceed this process limit.
SD="$WORK/state-streamed-log"; mkdir -p "$SD"
python3 - "$SD/decisions.jsonl" "$NOW" <<'PY'
import datetime, json, sys
out, now = sys.argv[1], int(sys.argv[2])
ts = datetime.datetime.fromtimestamp(now - 120, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
record = {"schema": "case_577a5ee0-decision-v1", "writer": "plugin-shadow",
          "issueId": "x", "ts": ts, "padding": "x" * 2048}
with open(out, "w") as h:
    for _ in range(32768):
        h.write(json.dumps(record, separators=(",", ":")) + "\n")
PY
mkgauge "$SD/gauge.json" ontrack
(
  ulimit -v $((96 * 1024)) || exit 2
  run_push "$SD/decisions.jsonl" "$SD/gauge.json"
) > "$SD/out" 2>&1
out="$(<"$SD/out")"
if grep -q "rc=0" <<<"$out" && grep -q '"freshness": {"state": "fresh", "ageSeconds": 120' <<<"$out"; then
  ok "5l4. large single-file shadow log is processed within 96 MiB address space"
else bad "5l4. bounded-memory shadow stream" "$out"; fi

# 5l5. A single line over the parser bound fails closed before JSON parsing.
SD="$WORK/state-long-line"; mkdir -p "$SD"
python3 - "$SD/decisions.jsonl" <<'PY'
import sys
with open(sys.argv[1], "w") as h:
    h.write("x" * (1024 * 1024 + 1))
PY
mkgauge "$SD/gauge.json" ontrack
out="$(run_push "$SD/decisions.jsonl" "$SD/gauge.json")"
if grep -q "rc=1" <<<"$out" && grep -q '"freshness": {"state": "malformed"' <<<"$out" \
  && grep -q "exceeds 1048576 characters" <<<"$out"; then
  ok "5l5. overlong JSONL record fails closed at the parser bound"
else bad "5l5. overlong JSONL record" "$out"; fi

# === 6. DOCS (staleness horizon + gauge contract) ================================
docs_bad=""
grep -q "SHADOW_STALE_AFTER_SEC" "$FRAG" || docs_bad="$docs_bad [fragment missing SHADOW_STALE_AFTER_SEC]"
grep -qi "stale" "$FRAG" || docs_bad="$docs_bad [fragment missing staleness]"
grep -q "case_e8e340c1-window-landing-gauge-v1" "$FRAG" || docs_bad="$docs_bad [fragment missing gauge schema]"
grep -q "fixtures/window-landing-gauge.example.json" "$FRAG" || docs_bad="$docs_bad [fragment missing fixture fallback]"
grep -q "SHADOW_STALE_AFTER_SEC" "$PUSH" || docs_bad="$docs_bad [push missing SHADOW_STALE_AFTER_SEC]"
grep -q "case_e8e340c1-window-landing-gauge-v1" "$PUSH" || docs_bad="$docs_bad [push missing gauge schema]"
grep -qi "FRESHNESS THRESHOLD" "$PUSH" || docs_bad="$docs_bad [push missing freshness section]"
grep -q "case_e0da19dc-shadow-window-v1" "$PUSH" || docs_bad="$docs_bad [push missing verdict schema]"
for token in WINDOW_GAUGE_MAX_AGE_SEC stale-gauge evaluatedAt SHADOW_WARMUP_MAX_SEC decisions-YYYY-MM-DD-HHZ.jsonl tornTail fixture-refused 1,048,576; do
  grep -q "$token" "$FRAG" || docs_bad="$docs_bad [fragment missing $token]"
  grep -q "$token" "$PUSH" || docs_bad="$docs_bad [push missing $token]"
done
grep -qi "same shard" "$FRAG" "$PUSH" "$REG" || docs_bad="$docs_bad [same-shard torn-tail rule missing from docs]"
grep -qi "GAUGE FRESHNESS" "$PUSH" || docs_bad="$docs_bad [push missing gauge-freshness section]"
grep -qi "FIXTURE REJECTION" "$PUSH" || docs_bad="$docs_bad [push missing unconditional fixture-rejection section]"
if grep -q "WINDOW_GAUGE_ALLOW_FIXTURE" "$FRAG" "$PUSH" "$REG" "$FIX"; then
  docs_bad="$docs_bad [production docs retain the unsafe fixture opt-in]"
fi
grep -q '"fixture": true' "$FIX" || docs_bad="$docs_bad [example fixture missing its fixture marker]"
[[ -z "$docs_bad" ]] \
  && ok "6. staleness horizon + gauge contract documented in fragment and push script" \
  || bad "6. horizon/gauge docs" "$docs_bad"

# === 7. NO PAID-RUNNER REFERENCES ================================================
paid_hits="$(grep -rniE 'blacksmith|paid.?runner|larger.?runner|runs-on:.*(large|paid)|self-?hosted.*(large|paid|gpu)' "$FRAG" "$PUSH" "$FIX" || true)"
if [[ -n "$paid_hits" ]]; then
  bad "7. paid-runner reference in module" "$paid_hits"
else ok "7. no paid-runner or Blacksmith references in module"; fi
# positive control: the same pattern must fire on a planted reference.
echo "runs on blacksmith" > "$WORK/planted.txt"
if grep -qiE 'blacksmith|paid.?runner|larger.?runner' "$WORK/planted.txt"; then ok "7c. paid-runner scan fires on a planted reference"; else bad "7c. paid-runner scan" "pattern missed a planted reference"; fi

# === 8. REGISTRY ================================================================
reg_bad=""
grep -qF "router_shadow-freshness" "$REG" || reg_bad="$reg_bad [missing key router_shadow-freshness]"
grep -qF "router_window-landing" "$REG" || reg_bad="$reg_bad [missing key router_window-landing]"
for name in gatus-shadow-window.yaml push-shadow-window.sh test_case_60330d6f_shadow_window.sh fixtures/window-landing-gauge.example.json; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
grep -qF "router/shadow-freshness" "$REG" || reg_bad="$reg_bad [missing endpoint router/shadow-freshness]"
grep -qF "router/window-landing" "$REG" || reg_bad="$reg_bad [missing endpoint router/window-landing]"
for token in WINDOW_GAUGE_MAX_AGE_SEC fixture-refused stale-gauge; do
  grep -qF "$token" "$REG" || reg_bad="$reg_bad [missing $token]"
done
[[ -z "$reg_bad" ]] \
  && ok "8. registry lists both endpoints, fragment, push script, fixture and suite" \
  || bad "8. registry coverage" "$reg_bad"

# === 9. PROPOSE-ONLY (no live wiring, no alert-route change) =====================
live_bad=""
if grep -qE 'shadow-freshness|window-landing|group:[[:space:]]*router' "$LIVE"; then
  live_bad="$live_bad [live config already wires the module]"
fi
[[ -z "$live_bad" ]] \
  && ok "9. live gatus-config.yaml untouched: wiring stays an operator step, no alert route changed" \
  || bad "9. propose-only" "$live_bad"

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
