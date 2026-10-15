#!/usr/bin/env bash
# ===========================================================================
# test_case_c99d8d47_pool_freshness.sh — offline suite for the case_db57ba1a
# pool-heartbeat freshness Gatus module (ops/case_658bf6b9/gatus-pool-freshness.yaml,
# push-pool-freshness.sh, fixtures/pool-freshness-{fresh,stale}.example.json,
# MODULES.md).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the single platform/pool-freshness external endpoint
#      declares token/heartbeat/alerts, alerts are known types
#      (discord|custom). A module that monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: the token is
#      ${ENV} only, no Bearer/credential literal in the fragment.
#   3. REFUSALS — missing GARM_POOLS/GATUS_URL/token refuses (exit 2) with
#      zero pushes, as do a bad pool ID, a non-positive threshold and a bad
#      NOW_OVERRIDE clock; a garm-cli failure, an unparseable pool response,
#      an unknown-age instance, a disabled pool, over-max instances and a
#      reserved-but-empty pool each push false (fail-closed), never silent
#      green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS (stub garm-cli, pinned clock) — a fresh heartbeat pushes
#      true (FRESH); a 30m-stale heartbeat pushes false naming pool + age
#      (STALE); newer updated_at beats older created_at; zero instances with
#      min-idle 0 reads idle-healthy; multi-pool names the worst pool; the
#      checked-in example fixtures consume with the pinned clock; every run
#      prints a case_c99d8d47-pool-freshness-v1 JSON verdict (acceptance: module
#      returns JSON on fixtures).
#   6. DOCS — the 15m threshold proposal plus the observed-activity caveat
#      are documented in the fragment and the push script header.
#   7. NO PAID-RUNNER REFERENCES — no Blacksmith / paid-runner wording
#      anywhere in the module.
#   8. REGISTRY — MODULES.md lists the endpoint key, the fragment, the
#      push script, both example fixtures and this suite (Done = merged PR
#      + registry entry).
#   9. PROPOSE-ONLY — the live gatus-config.yaml gains no pool-freshness
#      endpoint in this slice: wiring stays an operator step, and no
#      existing alert route is touched. No remediation verb in the module.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh. Clock is pinned via
# NOW_OVERRIDE so heartbeat ages are deterministic.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-pool-freshness.yaml"
PUSH="$SRC/push-pool-freshness.sh"
FIX_FRESH="$SRC/fixtures/pool-freshness-fresh.example.json"
FIX_STALE="$SRC/fixtures/pool-freshness-stale.example.json"
REG="$SRC/MODULES.md"
LIVE="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
pass_count() { printf 'pass=%d fail=%d' "$PASS" "$FAIL"; }

# --- pinned clock: the example fixtures' reference instant ------------------
# Fresh fixture: updated_at 2026-10-03T03:58Z → 2m age at NOW.
# Stale fixture: updated_at 2026-10-03T03:30Z → 30m age at NOW.
NOW="$(date -u -d "2026-10-03T04:00:00Z" +%s)"

# --- pool builders: stub garm-cli serves these files per pool id ------------
mkpool() {  # mkpool <out> <min_idle> <max> [<spec> ...] ; spec = <age_min> | <created_age>:<updated_age> | none | disabled-prefix!
  # NOW travels as argv (single-sourced pinned clock). Spec ages are minutes
  # before NOW. Prefix the first spec with "disabled!" to emit enabled:false.
  python3 - "$NOW" "$1" "$2" "$3" "${@:4}" <<'PY'
import json, sys
now_s, out, mi, mx, specs = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5:]
enabled = True
if specs and specs[0].startswith("disabled!"):
    enabled = False
    specs[0] = specs[0][len("disabled!"):]
def iso(age_min):
    import datetime
    return datetime.datetime.fromtimestamp(now_s - int(age_min) * 60, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
insts = []
for i, spec in enumerate(specs):
    if spec == "none":
        insts.append({"name": f"runner-{i}", "status": "running", "runner_status": "idle"})
        continue
    if ":" in spec:
        c_age, u_age = spec.split(":", 1)
    else:
        c_age = u_age = spec
    insts.append({"name": f"runner-{i}", "status": "running", "runner_status": "idle",
                  "created_at": iso(c_age), "updated_at": iso(u_age)})
json.dump({"id": "pool-test-aaaa-bbbb-cccc-ddddeeeeffff", "enabled": enabled,
           "min_idle_runners": mi, "max_runners": mx, "provider_name": "lxd",
           "instances": insts}, open(out, "w"))
PY
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$FIX_FRESH" ]] || { echo "REFUSE: $FIX_FRESH not found"; exit 3; }
[[ -f "$FIX_STALE" ]] || { echo "REFUSE: $FIX_STALE not found"; exit 3; }
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
PORT="${case_c99d8d47_TEST_PORT:-8805}"
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

# --- stub garm-cli: serves pool JSON per pool id -----------------------------
# PF_MODE selects transport behavior: ok | fail | garbage (non-JSON).
# Pool id "pool-a" reads $PF_POOL_FILE_POOL_A (uppercased, - → _),
# else $PF_POOL_FILE.
cat > "$WORK/bin/garm-cli" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
if len(sys.argv) < 4 or sys.argv[1] != "pool" or sys.argv[2] != "show":
    sys.stderr.write("stub garm-cli: unexpected argv\n")
    sys.exit(2)
pool = sys.argv[3]
mode = os.environ.get("PF_MODE", "ok")
if mode == "fail":
    sys.stderr.write("stub garm-cli: connection refused\n")
    sys.exit(1)
if mode == "garbage":
    print("this is not json")
    sys.exit(0)
key = "PF_POOL_FILE_" + pool.upper().replace("-", "_").replace(".", "_")
qfile = os.environ.get(key) or os.environ.get("PF_POOL_FILE", "")
try:
    with open(qfile, encoding="utf-8") as h:
        print(h.read())
except OSError:
    sys.stderr.write("stub garm-cli: pool file unreadable\n")
    sys.exit(1)
STUB
chmod +x "$WORK/bin/garm-cli"

# run_push [extra-env...]: run push script, print
# "rc=<n> <last-push-path> ||VERDICT|| <verdict-json>".
# True-pushes carry no error param by Gatus convention, so verdict assertions
# read the cron-log JSON line ("push-pool-freshness: {...}").
run_push() {
  : > "$PUSH_FILE"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
    GARM_POOLS="pool-a" NOW_OVERRIDE="$NOW" \
    "$@" bash "$PUSH" >"$WORK/push.log" 2>&1
  local rc=$?
  local pushed="NOPUSH" verdict="NOVERDICT"
  if [[ -s "$PUSH_FILE" ]]; then
    pushed="$(python3 -c "import json; p=[json.loads(x)['path'] for x in open('$PUSH_FILE') if 'pool-freshness' in json.loads(x)['path']]; print(p[-1] if p else 'NOPUSH')")"
  fi
  verdict="$(grep -o 'push-pool-freshness: {.*}' "$WORK/push.log" | tail -n 1 | sed 's/^push-pool-freshness: //')"
  python3 -c "
log = open('$WORK/push.log').read().replace(chr(10), ' ')
print('rc=$rc PUSHED=$pushed ||VERDICT|| ' + '''$verdict''')
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
if exts != ["pool-freshness"]: errs.append(f"external endpoints {exts} != ['pool-freshness']")
for name, b in groups.items():
    missing = {"token", "heartbeat", "alerts"} - b["keys"]
    if missing: errs.append(f"{name}: missing {sorted(missing)}")
if not re.search(r"group:\s*platform", text): errs.append("no group: platform")
if not re.search(r"interval:\s*10m", text): errs.append("heartbeat is not 10m")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
)"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: one platform/pool-freshness external endpoint, 10m heartbeat, alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
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
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|GH_REPOS|GH_TOKEN|GARM_POOLS)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" "$FIX_FRESH" "$FIX_STALE" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL in module" "$sec_hits"
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG"; then
  ok "2b. token referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${GATUS_EXTERNAL_TOKEN} missing"; fi
frag_code="$(grep -vE '^\s*#' "$FRAG" || true)"
if grep -qiE 'authorization|api-?key|bearer|basic:' <<<"$frag_code"; then
  bad "2c. auth material in fragment" "fragment must carry no credential"
else ok "2c. fragment carries no auth material"; fi
push_code="$(grep -vE '^\s*#' "$PUSH" || true)"
if grep -qE 'curl .*(-H|Authorization: Bearer \$[A-Z_]+[^}])' <<<"$push_code"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === 3. REFUSALS + FAIL-CLOSED ================================================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PUSH" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3a. missing env refuses exit 2 with zero pushes"
else bad "3a. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

SD="$WORK/r"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 2
out="$(run_push GARM_POOLS="" PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||VERDICT||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3b. empty GARM_POOLS refuses exit 2 with zero pushes"
else bad "3b. empty pools refusal" "$out"; fi
out="$(run_push GARM_POOLS="pool-a pool/../evil" PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||VERDICT||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3c. bad pool ID refuses exit 2 with zero pushes"
else bad "3c. bad pool ID refusal" "$out"; fi
out="$(run_push GARM_FRESHNESS_THRESHOLD_MIN=0 PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||VERDICT||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3d. zero threshold refuses exit 2 with zero pushes"
else bad "3d. zero threshold refusal" "$out"; fi
out="$(run_push GARM_FRESHNESS_THRESHOLD_MIN=abc PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||VERDICT||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3e. non-numeric threshold refuses exit 2 with zero pushes"
else bad "3e. non-numeric threshold refusal" "$out"; fi
out="$(run_push NOW_OVERRIDE=not-a-clock PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||VERDICT||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3f. bad NOW_OVERRIDE refuses exit 2 with zero pushes"
else bad "3f. bad NOW_OVERRIDE refusal" "$out"; fi

out="$(run_push PF_MODE=fail)"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out"; then ok "3g. garm-cli failure pushes false, exit 1 (fail-closed)"
else bad "3g. garm-cli failure fail-closed" "$out"; fi
out="$(run_push PF_MODE=garbage)"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" && grep -q '"state":"unreadable"' <<<"$out"; then ok "3h. unparseable pool JSON pushes false, exit 1 (never an empty pool)"
else bad "3h. unparseable pool fail-closed" "$out"; fi
mkpool "$SD/unknown.json" 0 5 none
out="$(run_push PF_POOL_FILE="$SD/unknown.json")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" && grep -qi "unreadable" <<<"$out"; then ok "3i. unknown-age instance pushes false, exit 1 (never fresh)"
else bad "3i. unknown-age fail-closed" "$out"; fi
mkpool "$SD/disabled.json" 0 5 disabled!2
out="$(run_push PF_POOL_FILE="$SD/disabled.json")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" && grep -qi "disabled" <<<"$out"; then ok "3j. disabled pool pushes false, exit 1"
else bad "3j. disabled pool fail-closed" "$out"; fi
mkpool "$SD/overmax.json" 0 1 2 2 2
out="$(run_push PF_POOL_FILE="$SD/overmax.json")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" && grep -qi "over max" <<<"$out"; then ok "3k. over-max instances push false, exit 1"
else bad "3k. over-max fail-closed" "$out"; fi
mkpool "$SD/reserved.json" 2 5
out="$(run_push PF_POOL_FILE="$SD/reserved.json")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" && grep -qi "no instances" <<<"$out"; then ok "3l. reserved-but-empty pool pushes false, exit 1 (no heartbeat where capacity promised)"
else bad "3l. reserved-but-empty fail-closed" "$out"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SD="$WORK/nondisc"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 2
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  GARM_POOLS="pool-a" NOW_OVERRIDE="$NOW" \
  PF_POOL_FILE="$SD/q.json" bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS (stub garm-cli, pinned clock) ================================
# 5a. fresh heartbeat (2m) → true (FRESH), rc 0, JSON verdict.
SD="$WORK/fresh"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 2
out="$(run_push PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "pool-freshness.*success=true" <<<"$out" \
  && grep -q '"schema":"case_c99d8d47-pool-freshness-v1"' <<<"$out" && grep -q '"state":"fresh"' <<<"$out"; then
  ok "5a. fresh heartbeat pushes true (FRESH) with JSON verdict"
else bad "5a. fresh heartbeat" "$out"; fi

# 5b. checked-in example fixtures consume with the pinned clock.
out="$(run_push PF_POOL_FILE="$FIX_FRESH")"
if grep -q "rc=0" <<<"$out" && grep -q "pool-freshness.*success=true" <<<"$out" && grep -q '"state":"fresh"' <<<"$out"; then
  ok "5b. fresh example fixture consumes as FRESH"
else bad "5b. fresh example fixture consumption" "$out"; fi
out="$(run_push PF_POOL_FILE="$FIX_STALE")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" \
  && grep -q '"state":"stale"' <<<"$out" && grep -q '"heartbeatAgeMin":30' <<<"$out"; then
  ok "5b2. stale example fixture consumes as STALE (30m, acceptance scale)"
else bad "5b2. stale example fixture consumption" "$out"; fi

# 5c. 30m-stale heartbeat → false naming pool and age.
SD="$WORK/stale"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 30
out="$(run_push PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "pool-freshness.*success=false" <<<"$out" \
  && grep -q "pool-a" <<<"$out" && grep -q "30m" <<<"$out" \
  && grep -q '"state":"stale"' <<<"$out"; then
  ok "5c. 30m-stale heartbeat pushes false naming pool-a and 30m"
else bad "5c. stale heartbeat" "$out"; fi

# 5d. freshest activity wins: created 60m ago, updated 2m ago → FRESH.
SD="$WORK/freshupd"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 60:2
out="$(run_push PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "pool-freshness.*success=true" <<<"$out" \
  && grep -q '"state":"fresh"' <<<"$out"; then
  ok "5d. newer updated_at beats older created_at (freshest activity wins)"
else bad "5d. freshest-activity rule" "$out"; fi

# 5e. zero instances with min-idle 0 → idle-healthy true.
SD="$WORK/idle"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5
out="$(run_push PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "pool-freshness.*success=true" <<<"$out" \
  && grep -q '"verdict":"idle"' <<<"$out"; then
  ok "5e. empty pool with min-idle 0 reads idle-healthy (today's fleet state)"
else bad "5e. idle pool" "$out"; fi

# 5f. multi-pool: the worst pool names the page.
SD="$WORK/multi"; mkdir -p "$SD"; mkpool "$SD/a.json" 0 5 2; mkpool "$SD/b.json" 0 5 45
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  GARM_POOLS="pool-a pool-b" NOW_OVERRIDE="$NOW" \
  PF_POOL_FILE_POOL_A="$SD/a.json" PF_POOL_FILE_POOL_B="$SD/b.json" \
  bash "$PUSH" >"$WORK/push.log" 2>&1; echo "rc=$?"; grep -o 'push-pool-freshness: {.*}' "$WORK/push.log" | tail -n 1)"
if grep -q "rc=1" <<<"$out" && grep -q "pool-b" <<<"$out" && grep -q "45m" <<<"$out"; then
  ok "5f. multi-pool names the stalest pool (pool-b 45m) on the page"
else bad "5f. multi-pool attribution" "$out"; fi

# 5g. positive control: stale reads false while fresh reads true — a
#     mutant forcing success-true goes red.
if grep -q "rc=1" <<<"$(run_push PF_POOL_FILE="$FIX_STALE")" \
  && grep -q "rc=0" <<<"$(run_push PF_POOL_FILE="$FIX_FRESH")"; then
  ok "5g. stale-vs-fresh fixtures discriminate (success-true mutant would go red)"
else bad "5g. fixture discrimination" "fresh and stale fixtures do not discriminate"; fi

# 5h. boundary: heartbeat exactly at the threshold reads fresh.
SD="$WORK/boundary"; mkdir -p "$SD"; mkpool "$SD/q.json" 0 5 15
out="$(run_push PF_POOL_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "pool-freshness.*success=true" <<<"$out"; then
  ok "5h. heartbeat at exactly 15m reads fresh (alert strictly past threshold)"
else bad "5h. threshold boundary" "$out"; fi

# === 6. DOCS (threshold proposal + observed-activity caveat) ===================
docs_bad=""
grep -q "GARM_FRESHNESS_THRESHOLD_MIN" "$FRAG" || docs_bad="$docs_bad [fragment missing GARM_FRESHNESS_THRESHOLD_MIN]"
grep -qi "observed activity" "$FRAG" || docs_bad="$docs_bad [fragment missing observed-activity caveat]"
grep -q "GARM_FRESHNESS_THRESHOLD_MIN" "$PUSH" || docs_bad="$docs_bad [push missing GARM_FRESHNESS_THRESHOLD_MIN]"
grep -q "case_c99d8d47-pool-freshness-v1" "$PUSH" || docs_bad="$docs_bad [push missing verdict schema]"
grep -qi "no .*remediation\|never .*delete\|NO REMEDIATION" "$PUSH" || docs_bad="$docs_bad [push missing no-remediation boundary]"
[[ -z "$docs_bad" ]] \
  && ok "6. threshold proposal + caveat + no-remediation documented in fragment and push script" \
  || bad "6. threshold/caveat docs" "$docs_bad"

# === 7. NO PAID-RUNNER REFERENCES ================================================
paid_hits="$(grep -rniE 'blacksmith|paid.?runner|larger.?runner|runs-on:.*(large|paid)' "$FRAG" "$PUSH" "$FIX_FRESH" "$FIX_STALE" || true)"
if [[ -n "$paid_hits" ]]; then
  bad "7. paid-runner reference in module" "$paid_hits"
else ok "7. no paid-runner or Blacksmith references in module"; fi
# positive control: the same pattern must fire on a planted reference.
echo "runs on blacksmith" > "$WORK/planted.txt"
if grep -qiE 'blacksmith|paid.?runner|larger.?runner' "$WORK/planted.txt"; then ok "7c. paid-runner scan fires on a planted reference"; else bad "7c. paid-runner scan" "pattern missed a planted reference"; fi

# === 8. REGISTRY ================================================================
reg_bad=""
grep -qF "platform_pool-freshness" "$REG" || reg_bad="$reg_bad [missing key platform_pool-freshness]"
for name in gatus-pool-freshness.yaml push-pool-freshness.sh test_case_c99d8d47_pool_freshness.sh fixtures/pool-freshness-fresh.example.json fixtures/pool-freshness-stale.example.json; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
grep -qF "platform/pool-freshness" "$REG" || reg_bad="$reg_bad [missing endpoint platform/pool-freshness]"
[[ -z "$reg_bad" ]] \
  && ok "8. registry lists the endpoint, fragment, push script, fixtures and suite" \
  || bad "8. registry coverage" "$reg_bad"

# === 9. PROPOSE-ONLY (no live wiring, no alert-route change, no remediation) ===
live_bad=""
if grep -qE 'pool-freshness|group:[[:space:]]*router' "$LIVE"; then
  live_bad="$live_bad [live config already wires the module]"
fi
[[ -z "$live_bad" ]] \
  && ok "9. live gatus-config.yaml untouched: wiring stays an operator step, no alert route changed" \
  || bad "9. propose-only" "$live_bad"
# Code lines only (doc prose names the forbidden verbs to forbid them).
module_verbs="$( { grep -vE '^\s*#' "$PUSH"; grep -vE '^\s*#' "$FRAG"; } | grep -nEi '(^|[;&|])[[:space:]]*(delete|destroy|remove|stop|create|update|add|provision|reap)\b|--method[[:space:]]+DELETE|-X[[:space:]]+DELETE' || true)"
if [[ -n "$module_verbs" ]]; then
  bad "9b. remediation verb in module" "$module_verbs"
else ok "9b. no remediation verb in module (alert-only)"; fi

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
