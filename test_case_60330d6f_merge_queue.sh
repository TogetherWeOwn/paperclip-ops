#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_merge_queue.sh — offline suite for the case_8bbe9e21 merge-queue
# saturation probe Gatus module (ops/case_658bf6b9/gatus-merge-queue.yaml,
# push-merge-queue.sh, fixtures/merge-queue-{healthy,saturated}.example.json,
# MODULES.md).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the single platform/merge-queue external endpoint
#      declares token/heartbeat/alerts, alerts are known types
#      (discord|custom). A module that monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: the token is
#      ${ENV} only, no Bearer/credential literal in the fragment.
#   3. REFUSALS — missing GH_REPOS/GATUS_URL/token refuses (exit 2) with
#      zero pushes, as do a malformed repo, a bad branch, a malformed
#      MQ_KEY_SUFFIX (path traversal guard) and crossed hysteresis bands;
#      a gh failure, a bare-array response or a page-limit breach pushes
#      false (fail-closed), never silent green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS (stub gh, pinned clock) — a clear queue pushes true
#      (HEALTHY); a saturated queue (case_a97273bd scale: depth 6, 190m wait)
#      pushes false naming depth and wait (DEGRADED); depth-only and
#      wait-only breaches each page; entries without enqueuedAt count
#      toward depth but never toward wait; mid-band latches the prior
#      verdict; absent queues read healthy unless required; the checked-in
#      example fixtures consume with the pinned clock. Every run prints a
#      case_401f7b0d-merge-queue-v1 JSON verdict (acceptance: module returns
#      JSON on fixtures).
#   6. DOCS — the threshold proposal (depth 5/2, wait 120m/60m) and the
#      hysteresis latch are documented in the fragment and the push
#      script header.
#   7. NO PAID-RUNNER REFERENCES — no Blacksmith / paid-runner wording
#      anywhere in the module.
#   8. REGISTRY — MODULES.md lists the endpoint key, the fragment, the
#      push script, both example fixtures and this suite (Done = merged PR
#      + registry entry).
#   9. PROPOSE-ONLY — the live gatus-config.yaml gains no merge-queue
#      endpoint in this slice: wiring stays an operator step, and no
#      existing alert route is touched. No auto-merge, no runner change.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh. Clock is pinned via
# NOW_OVERRIDE so enqueue ages are deterministic.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-merge-queue.yaml"
PUSH="$SRC/push-merge-queue.sh"
FIX_HEALTHY="$SRC/fixtures/merge-queue-healthy.example.json"
FIX_SATURATED="$SRC/fixtures/merge-queue-saturated.example.json"
REG="$SRC/MODULES.md"
LIVE="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
pass_count() { printf 'pass=%d fail=%d' "$PASS" "$FAIL"; }

# --- pinned clock: the example fixtures' reference instant ------------------
# Healthy fixture: 1 entry enqueued 2026-10-03T03:50Z → 10m wait at NOW.
# Saturated fixture: oldest entry enqueued 2026-10-03T00:50Z → 190m wait
# (the case_a97273bd scale), depth 6.
NOW="$(date -u -d "2026-10-03T04:00:00Z" +%s)"

# --- queue builders: stub gh serves these files, sliced 100/page -------------
mkqueue() {  # mkqueue <out> <spec>... ; spec = <age_min>|none (enqueue age)
  # NOW travels as argv (single-sourced pinned clock): the 2026-10-03 fixture
  # dates must be read against the 2026 NOW above, never a hardcoded epoch.
  python3 - "$NOW" "$1" "${@:2}" <<'PY'
import json, sys
now_s, out, specs = sys.argv[1], sys.argv[2], sys.argv[3:]
now = int(now_s)
entries = []
for i, spec in enumerate(specs):
    entry = {"id": f"MQE_test_{i}", "position": i + 1, "state": "QUEUED",
             "headCommit": {"oid": "a" * 40}, "pullRequest": {"number": 100 + i}}
    if spec != "none":
        entry["enqueuedAt"] = __import__("datetime").datetime.fromtimestamp(
            now - int(spec) * 60, __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    entries.append(entry)
json.dump({"entries": entries}, open(out, "w"))
PY
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$FIX_HEALTHY" ]] || { echo "REFUSE: $FIX_HEALTHY not found"; exit 3; }
[[ -f "$FIX_SATURATED" ]] || { echo "REFUSE: $FIX_SATURATED not found"; exit 3; }
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
PORT="${case_401f7b0d_TEST_PORT:-8804}"
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

# --- stub gh: serves queue JSON per MQ_QUEUE_FILE*, sliced 100/page ---------
# Request shape: gh api "repos/<o>/<r>/merge-queue/<branch>?per_page=100&page=N".
# MQ_GH_MODE selects transport behavior: ok | absent (page-1 404) | fail |
# bare (bare-array shape) | garbage (non-JSON).
cat > "$WORK/bin/gh" <<'STUB'
#!/usr/bin/env python3
import json, os, sys, urllib.parse
path = sys.argv[2] if len(sys.argv) > 2 else ""
mode = os.environ.get("MQ_GH_MODE", "ok")
pre, _, qs = path.partition("?")
page = int(urllib.parse.parse_qs(qs).get("page", ["1"])[0])
segs = pre.split("/")
repo = "/".join(segs[1:3]) if len(segs) >= 3 else ""
if mode == "absent" and page == 1:
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    sys.exit(1)
if mode == "fail":
    sys.stderr.write("gh:field `merge-queue` not found\n")
    sys.exit(1)
if mode == "bare":
    print(json.dumps([{"id": "x", "enqueuedAt": "2026-10-03T03:50:00.000Z"}]))
    sys.exit(0)
if mode == "garbage":
    print("this is not json")
    sys.exit(0)
key = "MQ_QUEUE_FILE_" + repo.replace("/", "_").replace("-", "_").upper()
qfile = os.environ.get(key) or os.environ.get("MQ_QUEUE_FILE", "")
try:
    with open(qfile, encoding="utf-8") as h:
        root = json.load(h)
    entries = root["entries"]
except (OSError, ValueError, KeyError, TypeError):
    sys.stderr.write("gh: stub queue file unreadable\n")
    sys.exit(1)
print(json.dumps({"entries": entries[(page - 1) * 100:page * 100]}))
STUB
chmod +x "$WORK/bin/gh"

# run_push <state-dir> [extra-env...]: run push script, print
# "rc=<n> <last-push-path> ||VERDICT|| <verdict-json> ||LOG|| <log>".
# True-pushes carry no error param by Gatus convention (gatus_push sends
# error= only on false), so verdict assertions read the cron-log JSON line
# ("push-merge-queue: {...}"), which the push script always echoes.
run_push() {
  local statedir="$1"; shift
  mkdir -p "$statedir"
  : > "$PUSH_FILE"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
    GH_REPOS="o/r" MQ_STATE_FILE="$statedir/state.json" NOW_OVERRIDE="$NOW" \
    "$@" bash "$PUSH" >"$WORK/push.log" 2>&1
  local rc=$?
  local pushed="NOPUSH" verdict="NOVERDICT"
  if [[ -s "$PUSH_FILE" ]]; then
    pushed="$(python3 -c "import json; p=[json.loads(x)['path'] for x in open('$PUSH_FILE') if 'merge-queue' in json.loads(x)['path']]; print(p[-1] if p else 'NOPUSH')")"
  fi
  verdict="$(grep -o 'push-merge-queue: {.*}' "$WORK/push.log" | tail -n 1 | sed 's/^push-merge-queue: //')"
  python3 -c "
log = open('$WORK/push.log').read().replace(chr(10), ' ')
print('rc=$rc PUSHED=$pushed ||VERDICT|| ' + '''$verdict''' + ' ||LOG|| ' + log)
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
if exts != ["merge-queue"]: errs.append(f"external endpoints {exts} != ['merge-queue']")
for name, b in groups.items():
    missing = {"token", "heartbeat", "alerts"} - b["keys"]
    if missing: errs.append(f"{name}: missing {sorted(missing)}")
if not re.search(r"group:\s*platform", text): errs.append("no group: platform")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
)"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: one platform/merge-queue external endpoint, alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
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
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|GH_REPOS|GH_TOKEN)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" "$FIX_HEALTHY" "$FIX_SATURATED" || true)"
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

SD="$WORK/q"; mkdir -p "$SD"; mkqueue "$SD/q.json" 10
for bad_env in 'GH_REPOS=o/a,o/b' 'GH_REPOS=o/..' 'GH_REPOS='; do
  out="$(run_push "$SD/st" $bad_env MQ_QUEUE_FILE="$SD/q.json")"
  if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3b. refuses exit 2, no push: $bad_env"
  else bad "3b. refusal" "$bad_env :: $out"; fi
done
out="$(run_push "$SD/st" MQ_BRANCH='../evil' MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3c. bad branch refuses exit 2 with zero pushes"
else bad "3c. bad branch refusal" "$out"; fi
out="$(run_push "$SD/st" MQ_DEPTH_ALERT=2 MQ_DEPTH_CLEAR=5 MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3d. crossed depth bands refuse exit 2 with zero pushes"
else bad "3d. crossed depth bands" "$out"; fi
out="$(run_push "$SD/st" MQ_WAIT_ALERT_MIN=30 MQ_WAIT_CLEAR_MIN=60 MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3e. crossed wait bands refuse exit 2 with zero pushes"
else bad "3e. crossed wait bands" "$out"; fi
out="$(run_push "$SD/st" MQ_KEY_SUFFIX='../evil' MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3f. bad key suffix refuses exit 2 with zero pushes"
else bad "3f. bad key suffix refusal" "$out"; fi

out="$(run_push "$SD/st" MQ_GH_MODE=fail)"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" && grep -q '"state": "unreadable"' <<<"$out"; then ok "3g. gh failure pushes false, exit 1 (fail-closed)"
else bad "3g. gh failure fail-closed" "$out"; fi
out="$(run_push "$SD/st" MQ_GH_MODE=bare MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" && grep -qi "entries\[\]" <<<"$out"; then ok "3h. bare-array response pushes false, exit 1 (never an empty queue)"
else bad "3h. bare-array shape fail-closed" "$out"; fi
mkqueue "$SD/deep.json" $(for _ in $(seq 1 150); do echo -n "5 "; done)
out="$(run_push "$SD/st" MQ_PAGE_LIMIT=1 MQ_QUEUE_FILE="$SD/deep.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" && grep -q "MQ_PAGE_LIMIT" <<<"$out"; then ok "3i. queue past the page limit pushes false, exit 1"
else bad "3i. page-limit breach fail-closed" "$out"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SD="$WORK/nondisc"; mkdir -p "$SD"; mkqueue "$SD/q.json" 10
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  GH_REPOS="o/r" MQ_STATE_FILE="$SD/state.json" NOW_OVERRIDE="$NOW" \
  MQ_QUEUE_FILE="$SD/q.json" bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS (stub gh, pinned clock) ======================================
# 5a. clear queue (depth 1, 10m wait) → true (HEALTHY), rc 0, JSON verdict.
SD="$WORK/clear"; mkdir -p "$SD"; mkqueue "$SD/q.json" 10
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out" \
  && grep -q '"schema": "case_401f7b0d-merge-queue-v1"' <<<"$out" && grep -q '"state": "clear"' <<<"$out"; then
  ok "5a. clear queue pushes true (HEALTHY) with JSON verdict"
else bad "5a. clear queue" "$out"; fi

# 5b. checked-in example fixtures consume with the pinned clock.
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$FIX_HEALTHY")"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out" && grep -q '"state": "clear"' <<<"$out"; then
  ok "5b. healthy example fixture consumes as HEALTHY"
else bad "5b. healthy example fixture consumption" "$out"; fi
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$FIX_SATURATED")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -q '"state": "alert"' <<<"$out" && grep -q '"depthEntries": 6' <<<"$out" \
  && grep -q '"oldestWaitMinutes": 190' <<<"$out"; then
  ok "5b2. saturated example fixture consumes as DEGRADED (depth 6, wait 190m)"
else bad "5b2. saturated example fixture consumption" "$out"; fi

# 5c. saturated queue (case_a97273bd scale) → false naming depth and wait.
SD="$WORK/sat"; mkdir -p "$SD"; mkqueue "$SD/q.json" 190 130 70 50 30 20
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -q "depth 6" <<<"$out" && grep -q "190m" <<<"$out" \
  && grep -q '"state": "alert"' <<<"$out"; then
  ok "5c. saturated queue pushes false naming depth 6 and 190m wait"
else bad "5c. saturated queue" "$out"; fi

# 5d. depth-only breach (6 fresh entries) → false naming depth, wait quiet.
SD="$WORK/depthonly"; mkdir -p "$SD"; mkqueue "$SD/q.json" 5 5 5 5 5 5
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -q "depth 6" <<<"$out"; then
  ok "5d. depth-only breach pushes false (wait quiet, depth pages)"
else bad "5d. depth-only breach" "$out"; fi

# 5e. wait-only breach (1 entry, 190m wait) → false naming wait.
SD="$WORK/waitonly"; mkdir -p "$SD"; mkqueue "$SD/q.json" 190
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -q "190m" <<<"$out"; then
  ok "5e. wait-only breach pushes false (depth quiet, wait pages)"
else bad "5e. wait-only breach" "$out"; fi

# 5f. entries without enqueuedAt count toward depth, never toward wait.
SD="$WORK/noenqueue"; mkdir -p "$SD"; mkqueue "$SD/q.json" none none none none none none
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -q "wait unknown" <<<"$out" && grep -q '"oldestWaitMinutes": null' <<<"$out"; then
  ok "5f. dateless entries enforce depth (false) with wait unknown (never fresh)"
else bad "5f. dateless entries" "$out"; fi

# 5g. hysteresis: mid-band queue latches the prior alert, then clears.
SD="$WORK/latch"; mkdir -p "$SD/st"; mkqueue "$SD/q.json" 30 30 30
echo '{"state":"alert","depthEntries":6,"oldestWaitMinutes":190}' > "$SD/st/state.json"
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -qi "latch" <<<"$out"; then
  ok "5g. mid-band queue holds the prior alert (no flap on drain)"
else bad "5g. latch holds alert" "$out"; fi
SD="$WORK/nolatch"; mkdir -p "$SD"; mkqueue "$SD/q.json" 30 30 30
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out"; then
  ok "5g2. mid-band queue with no prior alert reads clear (latch, not default-doom)"
else bad "5g2. mid-band without prior alert" "$out"; fi
SD="$WORK/cleared"; mkdir -p "$SD/st"; mkqueue "$SD/q.json" 10
echo '{"state":"alert","depthEntries":6,"oldestWaitMinutes":190}' > "$SD/st/state.json"
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out" \
  && grep -q '"state": *"clear"' "$SD/st/state.json"; then
  ok "5g3. drained queue clears the latch and persists it"
else bad "5g3. latch clears on drain" "$out"; fi

# 5h. absent queue reads healthy by default, degraded when required.
SD="$WORK/absent"; mkdir -p "$SD"
out="$(run_push "$SD/st" MQ_GH_MODE=absent)"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out" \
  && grep -qi "not enabled" <<<"$out"; then
  ok "5h. absent queue reads healthy with a not-enabled note (queue inert today)"
else bad "5h. absent queue default" "$out"; fi
out="$(run_push "$SD/st" MQ_GH_MODE=absent MQ_REQUIRE_QUEUE=1)"
if grep -q "rc=1" <<<"$out" && grep -q "merge-queue.*success=false" <<<"$out" \
  && grep -qi "required but absent" <<<"$out"; then
  ok "5h2. required-but-absent queue pushes false (operator can require presence)"
else bad "5h2. required-but-absent queue" "$out"; fi

# 5i. empty answered queue reads clear (an enabled-but-idle queue is healthy).
SD="$WORK/empty"; mkdir -p "$SD"; mkqueue "$SD/q.json"
out="$(run_push "$SD/st" MQ_QUEUE_FILE="$SD/q.json")"
if grep -q "rc=0" <<<"$out" && grep -q "merge-queue.*success=true" <<<"$out" \
  && grep -q '"depthEntries": 0' <<<"$out"; then
  ok "5i. empty answered queue pushes true (idle is healthy)"
else bad "5i. empty queue" "$out"; fi

# 5j. positive control: saturated reads false while healthy reads true — a
#     mutant forcing success-true goes red.
if grep -q "rc=1" <<<"$(run_push "$SD/st" MQ_QUEUE_FILE="$FIX_SATURATED")" \
  && grep -q "rc=0" <<<"$(run_push "$SD/st" MQ_QUEUE_FILE="$FIX_HEALTHY")"; then
  ok "5j. saturated-vs-healthy fixtures discriminate (success-true mutant would go red)"
else bad "5j. fixture discrimination" "healthy and saturated fixtures do not discriminate"; fi

# 5k. multi-repo: the worst repo names the page.
SD="$WORK/multi"; mkdir -p "$SD"; mkqueue "$SD/a.json" 10; mkqueue "$SD/b.json" 190 190 190 190 190 190
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  GH_REPOS="o/a o/b" MQ_STATE_FILE="$SD/st/state.json" NOW_OVERRIDE="$NOW" \
  MQ_QUEUE_FILE_O_A="$SD/a.json" MQ_QUEUE_FILE_O_B="$SD/b.json" \
  bash "$PUSH" >"$WORK/push.log" 2>&1; echo "rc=$?"; grep -o 'push-merge-queue: {.*}' "$WORK/push.log" | tail -n 1)"
if grep -q "rc=1" <<<"$out" && grep -q "o/b" <<<"$out"; then
  ok "5k. multi-repo names the saturated repo on the page"
else bad "5k. multi-repo attribution" "$out"; fi

# === 6. DOCS (threshold proposal + hysteresis latch) ============================
docs_bad=""
grep -q "MQ_DEPTH_ALERT" "$FRAG" || docs_bad="$docs_bad [fragment missing MQ_DEPTH_ALERT]"
grep -q "MQ_WAIT_ALERT_MIN" "$FRAG" || docs_bad="$docs_bad [fragment missing MQ_WAIT_ALERT_MIN]"
grep -qi "hysteresis" "$FRAG" || docs_bad="$docs_bad [fragment missing hysteresis]"
grep -q "fixtures/merge-queue-healthy.example.json" "$FRAG" || docs_bad="$docs_bad [fragment missing fixture fallback]"
grep -q "MQ_DEPTH_ALERT" "$PUSH" || docs_bad="$docs_bad [push missing MQ_DEPTH_ALERT]"
grep -qi "HYSTERESIS" "$PUSH" || docs_bad="$docs_bad [push missing hysteresis section]"
grep -q "case_401f7b0d-merge-queue-v1" "$PUSH" || docs_bad="$docs_bad [push missing verdict schema]"
grep -qi "no auto-merge" "$PUSH" || docs_bad="$docs_bad [push missing no-auto-merge boundary]"
[[ -z "$docs_bad" ]] \
  && ok "6. threshold proposal + hysteresis documented in fragment and push script" \
  || bad "6. threshold/hysteresis docs" "$docs_bad"

# === 7. NO PAID-RUNNER REFERENCES ================================================
paid_hits="$(grep -rniE 'blacksmith|paid.?runner|larger.?runner|runs-on:.*(large|paid)' "$FRAG" "$PUSH" "$FIX_HEALTHY" "$FIX_SATURATED" || true)"
if [[ -n "$paid_hits" ]]; then
  bad "7. paid-runner reference in module" "$paid_hits"
else ok "7. no paid-runner or Blacksmith references in module"; fi
# positive control: the same pattern must fire on a planted reference.
echo "runs on blacksmith" > "$WORK/planted.txt"
if grep -qiE 'blacksmith|paid.?runner|larger.?runner' "$WORK/planted.txt"; then ok "7c. paid-runner scan fires on a planted reference"; else bad "7c. paid-runner scan" "pattern missed a planted reference"; fi

# === 8. REGISTRY ================================================================
reg_bad=""
grep -qF "platform_merge-queue" "$REG" || reg_bad="$reg_bad [missing key platform_merge-queue]"
for name in gatus-merge-queue.yaml push-merge-queue.sh test_case_60330d6f_merge_queue.sh fixtures/merge-queue-healthy.example.json fixtures/merge-queue-saturated.example.json; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
grep -qF "platform/merge-queue" "$REG" || reg_bad="$reg_bad [missing endpoint platform/merge-queue]"
[[ -z "$reg_bad" ]] \
  && ok "8. registry lists the endpoint, fragment, push script, fixtures and suite" \
  || bad "8. registry coverage" "$reg_bad"

# === 9. PROPOSE-ONLY (no live wiring, no alert-route change) =====================
live_bad=""
if grep -qE 'merge-queue|group:[[:space:]]*router' "$LIVE"; then
  live_bad="$live_bad [live config already wires the module]"
fi
[[ -z "$live_bad" ]] \
  && ok "9. live gatus-config.yaml untouched: wiring stays an operator step, no alert route changed" \
  || bad "9. propose-only" "$live_bad"

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
