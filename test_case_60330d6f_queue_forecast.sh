#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_queue_forecast.sh — offline suite for the case_cce0de98
# Next-repo CI queue-wait forecast Gatus module
# (ops/case_658bf6b9/gatus-queue-forecast.yaml, push-queue-forecast.sh,
# MODULES.md registry).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the external endpoint declares token/heartbeat/alerts,
#      alerts are known types (discord|custom). A fragment that monitors
#      nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped strings in the module: the token is
#      ${ENV} only, no Bearer/credential literal in the fragment.
#   3. REFUSALS — missing GATUS_URL/token/GH_REPO refuses (exit 2) with zero
#      pushes, as does a malformed FORECAST_KEY_SUFFIX (path traversal guard);
#      a `gh` failure, an unreadable/unparseable samples file, or an
#      unwritable state dir pushes success=false (fail-closed), never silent
#      green.
#   4. NON-DISCLOSURE — the Bearer token never reaches curl's argv.
#   5. SEMANTICS (fixture samples) — a rising age trend below threshold pushes
#      false naming the forecast crossing; a flat/draining trend pushes true;
#      an already-saturated queue pushes true naming the queue-age alert (no
#      double-page); a stale samples gap pushes false; <2 samples pushes true
#      "warming up"; an empty queue samples age 0. SAMPLING IS JOB-LEVEL, the
#      same measurement push-ci-queue-age.sh alerts on (shared
#      _ci_queue_age.sh): a queued run whose jobs all started samples 0, the
#      needs-blocked drain job is ignored, an unreadable jobs API fails
#      closed, a server-side --created filter surfaces a stale job buried
#      behind 60 fresh runs, and age exactly at the threshold is still the
#      forecaster's (the alert's comparison is strictly greater). Four
#      mutants (5q) prove those pins can go red.
#   6. DOCS — the horizon (FORECAST_HORIZON_MIN) and staleness handling
#      (FORECAST_STALE_AFTER_MIN) are documented in the fragment and the push
#      script header.
#   7. NO PAID-RUNNER REFERENCES — no Blacksmith / paid-runner / larger-runner
#      wording anywhere in the module.
#   8. REGISTRY — MODULES.md lists the endpoint key, the fragment, the push
#      script and this suite (Done = merged PR + registry entry).
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Same stub-server /
# curl-shim pattern as test_case_60330d6f_status_pilot.sh.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-queue-forecast.yaml"
PUSH="$SRC/push-queue-forecast.sh"
HELPER="$SRC/_ci_queue_age.sh"
REG="$SRC/MODULES.md"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$HELPER" ]] || { echo "REFUSE: $HELPER not found"; exit 3; }
[[ -f "$REG" ]] || { echo "REFUSE: $REG not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required"; exit 3; }

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
PORT="${case_5c2ba534_TEST_PORT:-8801}"
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

# --- stub gh: job-level fixtures (case_cce0de98 review: the sampler must read the
# same runner-waiting JOB age the queue-age alert does, not a run-level age).
# `run list` returns runs (databaseId, createdAt) filtered by --created "<X",
# newest-first, cut at --limit like the real API; `api .../runs/<id>/jobs`
# returns that run's jobs. GH_FIXTURE:
#   empty       no runs
#   age-N       one run, its one job queued N minutes on the shared clock
#   old         one 2020 run holding a 2020 queued job (saturated)
#   fail        every call exits 1
#   draining    case_22859569 shape: a run listed `queued` 90m ago while every
#               job already started — a run-level age reads 90, job-level 0
#   drain-only  only the needs-blocked "Failure log drain" job is queued
#   jobs-fail   an old run whose jobs API fails (blind, never green)
#   young       a run newer than any cutoff whose jobs API fails: passes only
#               if the script never asks
#   burst       60 fresh runs plus two stale runs (7, 8) holding queued jobs
#               75m / 65m old (case_6bb27f43: a newest-first limit buries them)
# GH_CALL_LOG, when set, records every call.
mkdir -p "$WORK/tools"
# Freeze fixture timestamps, history samples and the detector's date +%s at
# one epoch. Live gh timestamps are generated after the detector samples now;
# even a one-second rollover floors the burst's 75m job to 74m. Keep real date
# parsing/formatting so production timestamp handling is still exercised.
FIXTURE_NOW=1767225600  # 2026-01-01T00:00:00Z
REAL_DATE="$(command -v date)"
cat > "$WORK/tools/date" <<SHIM
#!/usr/bin/env bash
if [[ \$# == 1 && \$1 == +%s ]]; then
  printf '%s\n' "$FIXTURE_NOW"
else
  exec "$REAL_DATE" "\$@"
fi
SHIM
chmod +x "$WORK/tools/date"
cat > "$WORK/tools/gh" <<'SH'
#!/usr/bin/env bash
[[ -n "${GH_CALL_LOG:-}" ]] && echo "$*" >> "$GH_CALL_LOG"
ago() { date -u -d "@$(( $(date +%s) - $1 ))" +%Y-%m-%dT%H:%M:%SZ; }
job() { printf '{"name":"%s","status":"%s","created_at":"%s"}' "$1" "$2" "$3"; }
fx="${GH_FIXTURE:-empty}"
if [[ "$fx" == fail ]]; then echo "gh: boom" >&2; exit 1; fi
if [[ "$1" == run ]]; then
  limit=20 created=""
  while (( $# )); do
    case "$1" in --limit) limit="$2"; shift ;; --created) created="${2#<}"; shift ;; esac
    shift
  done
  case "$fx" in
    empty) runs='[]' ;;
    age-*) n="${fx#age-}"; runs="[{\"databaseId\":1,\"createdAt\":\"$(ago $(( (n + 10) * 60 )))\"}]" ;;
    old) runs='[{"databaseId":1,"createdAt":"2020-01-01T00:00:00Z"}]' ;;
    draining|drain-only|jobs-fail) runs="[{\"databaseId\":1,\"createdAt\":\"$(ago 5400)\"}]" ;;
    young) runs='[{"databaseId":1,"createdAt":"2999-01-01T00:00:00Z"}]' ;;
    burst) runs="$(jq -cn --arg a "$(ago 4800)" --arg b "$(ago 4200)" \
             '[range(60) | {databaseId: (100 + .), createdAt: "2999-01-01T00:00:00Z"}]
              + [{databaseId: 7, createdAt: $a}, {databaseId: 8, createdAt: $b}]')" ;;
  esac
  jq -c --argjson n "$limit" --arg c "$created" \
    'map(select($c == "" or .createdAt < $c)) | sort_by(.createdAt) | reverse | .[:$n]' <<<"$runs"
  exit 0
fi
# gh api repos/<repo>/actions/runs/<id>/jobs?per_page=100
rid="${2#*/runs/}"; rid="${rid%%/*}"
case "$fx" in
  age-*) n="${fx#age-}"; echo "{\"jobs\":[$(job check queued "$(ago $(( n * 60 )))")]}" ;;
  old) echo "{\"jobs\":[$(job check queued 2020-01-01T00:00:00Z)]}" ;;
  draining) echo "{\"jobs\":[$(job 'Offline suites' in_progress "$(ago 5400)"),$(job 'Privilege ceiling suites' completed "$(ago 5400)")]}" ;;
  drain-only) echo "{\"jobs\":[$(job 'Failure log drain' queued "$(ago 5400)"),$(job 'Offline suites' completed "$(ago 5400)")]}" ;;
  jobs-fail|young) echo "gh: boom" >&2; exit 1 ;;
  burst) case "$rid" in
           7) echo "{\"jobs\":[$(job check queued "$(ago 4500)")]}" ;;
           8) echo "{\"jobs\":[$(job check queued "$(ago 3900)")]}" ;;
           *) echo '{"jobs":[]}' ;;
         esac ;;
esac
SH
chmod +x "$WORK/tools/gh"

# mksamples <out> <"minutes_ago:age" ...>: one "<epoch> <age>" line each.
mksamples() {
  local out="$1"; shift
  python3 - "$out" "$FIXTURE_NOW" "$@" <<'PY'
import sys
now = int(sys.argv[2])
with open(sys.argv[1], "w") as h:
    for spec in sys.argv[3:]:
        ago, age = spec.split(":")
        h.write(f"{now - int(ago) * 60} {age}\n")
PY
}

# run_forecast <state-dir> [extra-env...]: run push script, print
# "rc=<n> <path> ||MSG|| <wire-error> ||LOG|| <stdout+stderr>".
# True-pushes carry no error param by Gatus convention (gatus_push sends
# error= only on false), so true-verdict assertions read the cron-log line
# ("push-queue-forecast: <verdict>"), which the push script always echoes.
run_forecast() {
  local sd="$1"; shift
  : > "$PUSH_FILE"
  env -i PATH="$WORK/bin:$WORK/tools:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
    GH_BIN="$WORK/tools/gh" GH_REPO="o/r" FORECAST_STATE_DIR="$sd" \
    "$@" bash "${PUSH_UNDER_TEST:-$PUSH}" >"$WORK/forecast.log" 2>&1
  local rc=$?
  local path="NOPUSH"
  if [[ -s "$PUSH_FILE" ]]; then
    path="$(python3 -c "import json; print([json.loads(x)['path'] for x in open('$PUSH_FILE')][-1])")"
  fi
  # Decode the error query param for message assertions; append the log.
  python3 -c "
import urllib.parse
log = open('$WORK/forecast.log').read().replace(chr(10), ' ')
p = '''$path'''.replace(chr(10), '')
q = urllib.parse.parse_qs(urllib.parse.urlparse(p).query)
print('rc=\$rc ' + p + ' ||MSG|| ' + q.get('error', [''])[0] + ' ||LOG|| ' + log)
" | sed "s/rc=\$rc/rc=$rc/"
}
parse_push() {
  [[ -s "$PUSH_FILE" ]] || { echo "EMPTY"; return; }
  python3 -c "import json; l=[json.loads(x) for x in open('$PUSH_FILE')]; p=l[-1]; print(p['path'], '|', p['auth'])"
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
exts = sorted(n for (s, n) in blocks if s == "external-endpoints")
if exts != ["ci-queue-forecast"]: errs.append(f"external endpoints {exts} != ['ci-queue-forecast']")
for (section, name), b in blocks.items():
    if section != "external-endpoints": continue
    missing = {"token", "heartbeat", "alerts"} - b["keys"]
    if missing: errs.append(f"{section}/{name}: missing {sorted(missing)}")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
)"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. fragment shape: ci-queue-forecast external endpoint, all alerted ($shape_out)"; else bad "1. fragment shape" "$shape_out"; fi
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
py_prog='import re, sys
text = open(sys.argv[1]).read()
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
print("ERRORS:none-left" if not atypes else "ERRORS:alerts-remain")'
py_out="$(python3 - "$mut" <<<"$py_prog")"
if grep -q "^ERRORS" <<<"$py_out"; then ok "1c. alert-less endpoint mutant goes red"; else bad "1c. alert-less endpoint mutant" "mutant still reads clean"; fi

# === 2. NO SECRETS ============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN|GH_REPO)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" "$HELPER" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL in module" "$sec_hits"
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG"; then
  ok "2b. token referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${GATUS_EXTERNAL_TOKEN} missing"; fi
frag_nc="$(grep -vE '^\s*#' "$FRAG" || true)"
if grep -qiE 'authorization|api-?key|bearer|basic:' <<<"$frag_nc"; then
  bad "2c. auth material in fragment" "fragment must carry no credential"
else ok "2c. fragment carries no auth material"; fi
push_nc="$(grep -vE '^\s*#' "$PUSH" || true)"
if grep -qE 'curl .*(-H|Authorization: Bearer \$[A-Z_]+[^}])' <<<"$push_nc"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === 3. REFUSALS + FAIL-CLOSED ================================================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PUSH" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3a. missing env refuses exit 2 with zero pushes"
else bad "3a. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

SD="$WORK/state-ghfail"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=fail)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out"; then ok "3b. gh failure pushes false, exit 1 (fail-closed)"
else bad "3b. gh failure fail-closed" "$out"; fi

SD="$WORK/state-malformed"; mkdir -p "$SD"
echo "not-a-sample-line" > "$SD/queue-age-samples.log"
out="$(run_forecast "$SD" GH_FIXTURE=empty)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -qi "unparseable" <<<"$out"; then ok "3c. malformed samples push false, exit 1 (fail-closed)"
else bad "3c. malformed samples fail-closed" "$out"; fi

SD="$WORK/state-outofrange"; mkdir -p "$SD"
echo "99999999999 -5" > "$SD/queue-age-samples.log"
out="$(run_forecast "$SD" GH_FIXTURE=empty)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out"; then ok "3d. out-of-range samples push false, exit 1 (fail-closed)"
else bad "3d. out-of-range samples fail-closed" "$out"; fi

SD="$WORK/state-badsuffix"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=empty FORECAST_KEY_SUFFIX='../evil')"
if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out"; then ok "3e. bad key suffix refuses exit 2 with zero pushes"
else bad "3e. bad key suffix refusal" "$out"; fi

# 3f. malformed repo / minutes / run limit refuse exit 2 with zero pushes, and
#     a gh that would answer never gets asked (same shape checks as the alert).
for bad_env in 'GH_REPO=o/..' 'GH_REPO=o/a,o/b' 'QUEUE_AGE_THRESHOLD_MIN=1h' \
               'FORECAST_MIN_AGE_MIN=x' 'FORECAST_STALE_AFTER_MIN=-5' 'QUEUE_AGE_RUN_LIMIT=0'; do
  SD="$WORK/state-refuse"; mkdir -p "$SD"; : > "$WORK/gh-calls.log"
  out="$(run_forecast "$SD" GH_FIXTURE=empty GH_CALL_LOG="$WORK/gh-calls.log" "$bad_env")"
  if grep -q "rc=2" <<<"${out%%||LOG||*}" && grep -q "NOPUSH" <<<"$out" && [[ ! -s "$WORK/gh-calls.log" ]]; then
    ok "3f. refuses exit 2, no push, no gh call: $bad_env"
  else bad "3f. refusal" "$bad_env => $out"; fi
done

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SD="$WORK/state-nondisc"; mkdir -p "$SD"
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:$WORK/tools:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  GH_BIN="$WORK/tools/gh" GH_REPO="o/r" GH_FIXTURE=empty FORECAST_STATE_DIR="$SD" \
  bash "$PUSH" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi

# === 5. SEMANTICS (fixture samples) =============================================
# 5a. rising trend below threshold → forecast false naming the crossing.
SD="$WORK/state-rising"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "60:5" "40:15" "20:28" "5:40"
out="$(run_forecast "$SD" GH_FIXTURE=age-45)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -qi "forecast" <<<"$out" && grep -qi "crosses" <<<"$out"; then
  ok "5a. rising trend forecasts saturation (false, names crossing)"
else bad "5a. rising trend forecast" "$out"; fi

# 5b. flat trend → true, no forecast (verdict in the cron log: true-pushes
#     carry no error param by Gatus convention).
SD="$WORK/state-flat"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "60:5" "40:5" "20:6" "5:5"
out="$(run_forecast "$SD" GH_FIXTURE=age-5)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" && grep -qi "no saturation forecast" <<<"${out##*||LOG||}"; then
  ok "5b. flat trend pushes true (no saturation forecast)"
else bad "5b. flat trend" "$out"; fi

# 5c. draining trend → true, no forecast.
SD="$WORK/state-draining"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "60:20" "40:15" "20:10" "5:7"
out="$(run_forecast "$SD" GH_FIXTURE=age-5)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" && grep -qi "no saturation forecast" <<<"${out##*||LOG||}"; then
  ok "5c. draining trend pushes true (no saturation forecast)"
else bad "5c. draining trend" "$out"; fi

# 5d. already saturated (even with a stale history: fresh reading wins) → true
#     naming the queue-age alert, so one incident never pages twice.
SD="$WORK/state-saturated"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "120:5" "100:8"
out="$(run_forecast "$SD" GH_FIXTURE=old)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" && grep -qi "queue-age alert owns" <<<"${out##*||LOG||}"; then
  ok "5d. saturated queue pushes true naming queue-age alert (no double-page)"
else bad "5d. saturated queue" "$out"; fi

# 5e. stale gap (last sample 100m ago, current below threshold) → false stale.
SD="$WORK/state-stale"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "120:5" "100:8"
out="$(run_forecast "$SD" GH_FIXTURE=age-5)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -qi "stale" <<<"$out"; then
  ok "5e. stale samples gap pushes false (trend blind, fail-closed)"
else bad "5e. stale samples gap" "$out"; fi

# 5f. warming up (empty state dir) → true, no history is not an incident.
SD="$WORK/state-warm"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=age-5)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" && grep -qi "warming up" <<<"${out##*||LOG||}"; then
  ok "5f. first sample pushes true (warming up, no history is not an incident)"
else bad "5f. warming up" "$out"; fi

# 5g. empty queue drains to age 0 and is still sampled.
SD="$WORK/state-emptyq"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=empty)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out"; then
  ok "5g. empty queue pushes true (drained, sampled as age 0)"
else bad "5g. empty queue" "$out"; fi
# positive control: a flat-line suite must catch a forecast — the rising
# fixture (5a) has to read false, so a mutant that forces true goes red here.
if grep -q "rc=1" <<<"$(run_forecast "$WORK/state-rising" GH_FIXTURE=age-45)" \
  && grep -q "success=true" <<<"$(run_forecast "$WORK/state-flat" GH_FIXTURE=age-5)"; then
  ok "5h. rising-vs-flat fixtures discriminate (forecast mutant would go red)"
else bad "5h. fixture discrimination" "rising and flat fixtures do not discriminate"; fi

# --- 5i..5q: JOB-LEVEL sampling, the same number the queue-age alert reads
# (case_cce0de98 review: a run-level `gh run list --status queued` age is the
# pre-case_22859569 measure that main already moved off in #462 and #478).
last_sample_age() { tail -n 1 "$1/queue-age-samples.log" 2>/dev/null | awk '{print $2}'; }

# 5i. a run listed `queued` for 90m whose jobs have all started is draining,
#     not waiting: job-level age is 0 and nothing is "saturated". A run-level
#     sampler reads 90 here and wrongly hands the incident to the alert.
SD="$WORK/state-draining-run"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=draining)"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" \
   && [[ "$(last_sample_age "$SD")" == "0" ]] && ! grep -qi "saturated" <<<"${out##*||LOG||}"; then
  ok "5i. queued run with all jobs running samples age 0 (job-level, not run-level)"
else bad "5i. queued-run-with-running-jobs" "sample=$(last_sample_age "$SD") $out"; fi

# 5j. only the needs-blocked drain job queued → it waits on jobs, not the fleet.
SD="$WORK/state-drain-only"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=drain-only)"
if grep -q "rc=0" <<<"$out" && [[ "$(last_sample_age "$SD")" == "0" ]]; then
  ok "5j. needs-blocked drain job alone samples age 0"
else bad "5j. drain-only" "sample=$(last_sample_age "$SD") $out"; fi

# 5k. an unreadable jobs API is blind, never a drained-queue sample.
SD="$WORK/state-jobsfail"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=jobs-fail)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -qi "jobs API unreadable" <<<"$out" \
   && [[ ! -s "$SD/queue-age-samples.log" ]]; then
  ok "5k. unreadable jobs API pushes false, exit 1, and records no sample"
else bad "5k. jobs API failure" "$out"; fi

# 5l. a run younger than the sample floor is never inspected (5k is its control).
SD="$WORK/state-young"; mkdir -p "$SD"; : > "$WORK/gh-calls.log"
out="$(run_forecast "$SD" GH_FIXTURE=young GH_CALL_LOG="$WORK/gh-calls.log")"
if grep -q "rc=0" <<<"$out" && ! grep -q '/jobs' "$WORK/gh-calls.log"; then
  ok "5l. run younger than the floor skips the jobs lookup, pushes true"
else bad "5l. young run" "$out | calls: $(tr '\n' ' ' < "$WORK/gh-calls.log")"; fi

# 5m. 60 fresh runs bury two stale runs holding waiting jobs (case_6bb27f43). A
#     newest-first --limit keeps only the fresh runs; the server-side
#     --created filter must surface the stale job and read it as saturated.
#     QUEUE_AGE_RUN_LIMIT=50 sits below the 62-run total, so an unfiltered
#     list would fill it and fail closed instead.
SD="$WORK/state-burst"; mkdir -p "$SD"; : > "$WORK/gh-calls.log"
out="$(run_forecast "$SD" GH_FIXTURE=burst QUEUE_AGE_RUN_LIMIT=50 GH_CALL_LOG="$WORK/gh-calls.log")"
if grep -q "rc=0" <<<"$out" && grep -q "success=true" <<<"$out" \
   && grep -q "saturated now (75m" <<<"${out##*||LOG||}" && grep -qi "queue-age alert owns" <<<"${out##*||LOG||}"; then
  ok "5m. stale waiting job behind 60 fresh runs is seen (saturated, 75m)"
else bad "5m. burst-buried stale job" "$out"; fi
sample="$(cat "$SD/queue-age-samples.log" 2>/dev/null)"
[[ "$sample" == "$FIXTURE_NOW 75" ]] \
  && ok "5m1. burst sample uses the shared epoch and exact 75-minute age" \
  || bad "5m1. shared fixture/detector clock" "sample=$sample; expected=$FIXTURE_NOW 75"
n_jobs="$(grep -c '/jobs' "$WORK/gh-calls.log")"
(( n_jobs == 1 )) && ok "5m2. oldest-first walk stops after one jobs call" \
  || bad "5m2. jobs calls" "n=$n_jobs: $(grep /jobs "$WORK/gh-calls.log" | tr '\n' ' ')"

# 5n. a run list that fills QUEUE_AGE_RUN_LIMIT hides the oldest runs → false.
SD="$WORK/state-truncated"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=burst QUEUE_AGE_RUN_LIMIT=1)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -q "QUEUE_AGE_RUN_LIMIT" <<<"$out"; then
  ok "5n. run list at QUEUE_AGE_RUN_LIMIT pushes false, exit 1"
else bad "5n. truncated run list" "$out"; fi

# 5o. exactly at the threshold the alert (`age > threshold`) is still green, so
#     the forecaster must still be watching: rising history + age == 60 is a
#     crossing in ~0m, not "saturated, alert owns it" (that would leave the
#     minute owned by neither).
SD="$WORK/state-boundary"; mkdir -p "$SD"
mksamples "$SD/queue-age-samples.log" "30:40" "20:50" "10:55"
out="$(run_forecast "$SD" GH_FIXTURE=age-60)"
if grep -q "rc=1" <<<"$out" && grep -q "success=false" <<<"$out" && grep -qi "crosses" <<<"$out"; then
  ok "5o. age exactly at threshold forecasts a crossing (alert still green there)"
else bad "5o. threshold boundary" "$out"; fi

# 5p. a leading-zero floor is decimal minutes, not octal (case_0b2d9007 class):
#     08 unfixed would fail the cutoff expansion and fail open.
SD="$WORK/state-octal"; mkdir -p "$SD"
out="$(run_forecast "$SD" GH_FIXTURE=old FORECAST_MIN_AGE_MIN=08)"
if grep -q "rc=0" <<<"$out" && grep -qi "queue-age alert owns" <<<"${out##*||LOG||}"; then
  ok "5p. leading-zero FORECAST_MIN_AGE_MIN=08 reads decimal, stale job still seen"
else bad "5p. leading-zero floor" "$out"; fi

# --- 5q: POSITIVE CONTROLS. Each mutant is staged beside copies of its sibling
# helpers (the scripts source them by their own path) and must change the
# verdict the test above pins, or the pin is vacuous. An anchor that no longer
# matches fails loudly instead of passing as an unchanged copy.
MUT="$WORK/mut"; mkdir -p "$MUT"
cp "$SRC/_gatus_push.sh" "$SRC/_ci_queue_age.sh" "$SRC/push-queue-forecast.sh" "$MUT/"
mutate() {  # <file> <sed-expr> <label> — apply, assert the file changed
  local f="$MUT/$1"; cp "$SRC/$1" "$f"
  sed -i "$2" "$f"
  if cmp -s "$SRC/$1" "$f"; then bad "5q. $3: mutation anchor missing" "sed '$2' left $1 unchanged"; return 1; fi
}
# M1: saturation uses >= (the pre-review comparison) → 5o must flip to "saturated".
if mutate push-queue-forecast.sh 's/^if age > thr:/if age >= thr:/' "age>=thr mutant"; then
  SD="$WORK/state-boundary-m"; mkdir -p "$SD"
  mksamples "$SD/queue-age-samples.log" "30:40" "20:50" "10:55"
  out="$(PUSH_UNDER_TEST="$MUT/push-queue-forecast.sh" run_forecast "$SD" GH_FIXTURE=age-60)"
  if grep -qi "queue-age alert owns" <<<"${out##*||LOG||}"; then ok "5q1. age>=thr mutant flips 5o to saturated (boundary pin is live)"
  else bad "5q1. age>=thr mutant" "5o would not notice: $out"; fi
fi
# M2: count any non-completed job as waiting (a run-level-style read) → 5i must sample 90.
if mutate _ci_queue_age.sh 's/select(.status == "queued" and/select(.status != "completed" and/' "status-filter mutant"; then
  SD="$WORK/state-draining-m"; mkdir -p "$SD"
  out="$(PUSH_UNDER_TEST="$MUT/push-queue-forecast.sh" run_forecast "$SD" GH_FIXTURE=draining)"
  if [[ "$(last_sample_age "$SD")" -ge 60 ]]; then ok "5q2. status-filter mutant samples the draining run as waiting (5i pin is live)"
  else bad "5q2. status-filter mutant" "5i would not notice: sample=$(last_sample_age "$SD") $out"; fi
fi
# M3: drop the server-side --created filter → 5m must stop reading the stale job.
if mutate _ci_queue_age.sh 's/ --created "<\$cutoff"//' "no --created mutant"; then
  SD="$WORK/state-burst-m"; mkdir -p "$SD"
  out="$(PUSH_UNDER_TEST="$MUT/push-queue-forecast.sh" run_forecast "$SD" GH_FIXTURE=burst QUEUE_AGE_RUN_LIMIT=50)"
  if ! grep -q "saturated now (75m" <<<"${out##*||LOG||}"; then ok "5q3. no---created mutant loses the buried stale job (5m pin is live)"
  else bad "5q3. no---created mutant" "5m would not notice: $out"; fi
fi
# M4: ignore the jobs-API failure count → 5k must stop failing closed.
if mutate _ci_queue_age.sh 's/CQA_JOBS_UNREADABLE=\$((CQA_JOBS_UNREADABLE + 1))/:/' "unreadable-count mutant"; then
  SD="$WORK/state-jobsfail-m"; mkdir -p "$SD"
  out="$(PUSH_UNDER_TEST="$MUT/push-queue-forecast.sh" run_forecast "$SD" GH_FIXTURE=jobs-fail)"
  if grep -q "rc=0" <<<"$out"; then ok "5q4. unreadable-count mutant reads a blind jobs API as green (5k pin is live)"
  else bad "5q4. unreadable-count mutant" "5k would not notice: $out"; fi
fi

# === 6. DOCS (horizon + staleness) ===============================================
docs_bad=""
grep -q "FORECAST_HORIZON_MIN" "$FRAG" || docs_bad="$docs_bad [fragment missing FORECAST_HORIZON_MIN]"
grep -q "FORECAST_STALE_AFTER_MIN" "$FRAG" || docs_bad="$docs_bad [fragment missing FORECAST_STALE_AFTER_MIN]"
grep -qi "horizon" "$FRAG" || docs_bad="$docs_bad [fragment missing horizon]"
grep -qi "stale" "$FRAG" || docs_bad="$docs_bad [fragment missing staleness]"
grep -q "FORECAST_HORIZON_MIN" "$PUSH" || docs_bad="$docs_bad [push missing FORECAST_HORIZON_MIN]"
grep -q "FORECAST_STALE_AFTER_MIN" "$PUSH" || docs_bad="$docs_bad [push missing FORECAST_STALE_AFTER_MIN]"
grep -qi "STALENESS HANDLING" "$PUSH" || docs_bad="$docs_bad [push missing staleness section]"
[[ -z "$docs_bad" ]] \
  && ok "6. horizon + staleness handling documented in fragment and push script" \
  || bad "6. horizon/staleness docs" "$docs_bad"

# === 7. NO PAID-RUNNER REFERENCES ================================================
paid_hits="$(grep -rniE 'blacksmith|paid.?runner|larger.?runner|runs-on:.*(large|paid)|self-?hosted.*(large|paid|gpu)' "$FRAG" "$PUSH" "$HELPER" || true)"
if [[ -n "$paid_hits" ]]; then
  bad "7. paid-runner reference in module" "$paid_hits"
else ok "7. no paid-runner or Blacksmith references in module"; fi
# positive control: the same pattern must fire on a planted reference.
echo "runs on blacksmith" > "$WORK/planted.txt"
if grep -qiE 'blacksmith|paid.?runner|larger.?runner' "$WORK/planted.txt"; then ok "7c. paid-runner scan fires on a planted reference"; else bad "7c. paid-runner scan" "pattern missed a planted reference"; fi

# === 8. REGISTRY ================================================================
reg_bad=""
grep -qF "platform_ci-queue-forecast" "$REG" || reg_bad="$reg_bad [missing key platform_ci-queue-forecast]"
for name in gatus-queue-forecast.yaml push-queue-forecast.sh _ci_queue_age.sh test_case_60330d6f_queue_forecast.sh; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [missing file $name]"
done
grep -qF "platform/ci-queue-forecast" "$REG" || reg_bad="$reg_bad [missing endpoint platform/ci-queue-forecast]"
[[ -z "$reg_bad" ]] \
  && ok "8. registry lists the endpoint, fragment, push script and suite" \
  || bad "8. registry coverage" "$reg_bad"

echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
