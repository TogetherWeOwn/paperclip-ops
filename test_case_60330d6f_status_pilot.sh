#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_status_pilot.sh — offline suite for the case_658bf6b9 phase-1
# status-page pilot bundle (ops/case_658bf6b9/).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. CONFIG SHAPE  — gatus-config.yaml parses; every pull endpoint has a
#                      URL, conditions and alerts; every external endpoint
#                      has a token, heartbeat and alerts; every alert is a
#                      known type (discord|custom). A config that parses but
#                      monitors nothing is the failure mode.
#   1e. QUEUE REMINDER — ci-queue-age re-pages hourly while the breach
#                      holds (minimum-reminder-interval on the base pair),
#                      escalates once at ~2h of continuous breach
#                      (failure-threshold 24, no reminder), and resolves
#                      stop everything. A config that fires once and forgets
#                      a sustained breach is the gap (case_60a899d5).
#   2. NO SECRETS    — no secret-shaped string committed anywhere in the
#                      bundle: no Discord webhook URL, no Bearer secret, no
#                      token literal, no Gatus external token; env indirection
#                      (${VAR}) is the only allowed form.
#   3. PUSH REFUSALS — the three push scripts fail CLOSED: missing
#                      GATUS_URL/token refuses (exit 2) before touching the
#                      network; a curl-stub records that refused runs make
#                      ZERO network calls.
#   4. NON-DISCLOSURE — when env IS set, the Bearer token never reaches
#                      curl's argv: a curl shim records argv, and the push
#                      must pass. (/proc/<pid>/cmdline is world-readable.)
#   5. UNTRUSTED INPUT — a 2000-char hostile error string is truncated to
#                      <= 600 chars on the wire; the full string never leaves.
#   6. PUSH SEMANTICS — lxc wrapper: all-Running pushes success=true; one
#                      STOPPED box pushes success=false naming it; an `lxc`
#                      failure pushes success=false; a missing `lxc` binary
#                      refuses (exit 2). OOM wrapper: clean journal pushes
#                      true; a "Killed process" line pushes false. Queue-age
#                      wrapper (job-level, case_22859569): empty queue pushes
#                      true; an old runner-waiting job pushes false; a
#                      run-level-queued run whose jobs all started pushes
#                      true; the needs-blocked drain job alone pushes true.
#   6j. GARM WRAPPER — stub garm-cli feeds pool JSON, stub gh feeds
#                      label-aware jobs: zero VMs + min-idle 0 + no eligible
#                      queue pushes true (the D2 HEALTHY rule); a disabled
#                      pool, a show failure, an eligible-label breach, an idle
#                      shortfall or prov-fail breakage under waiting demand,
#                      and an over-max pool each push false; ineligible-label
#                      depth alone, young demand, provisioning cover, and idle
#                      breakage with no demand all push true. A neutered
#                      fail_push mutant proves the failing shapes can flip.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. A stub HTTP server
# records pushes; stub lxc/journalctl/gh binaries feed fixtures; a curl shim
# records argv then execs the real curl against the stub server.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
CFG="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -d "$SRC" ]] || { echo "REFUSE: $SRC not found — suite cannot attribute a result"; exit 3; }
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
PORT="${case_60330d6f_TEST_PORT:-8799}"
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

# === 1. CONFIG SHAPE ==========================================================
# STDLIB ONLY (no PyYAML: this repo installs no Python packages in CI, and a
# YAML library just for one offline assertion is the wrong dependency). The
# check is structural, not a full parse: indentation-aware block scan that
# attributes url/conditions/alerts/token/heartbeat keys to their enclosing
# named endpoint block, plus brace-balance and tab rejection on the raw
# text. It answers "does every endpoint declare what Gatus needs" — full
# schema validation happens host-side with `gatus --validate` (OPERATOR.md
# §4), which runs the real binary against the real config.
shape_check() {  # <file> → prints SHAPE-OK or ERRORS:... ; exit 0 either way
  python3 - "$1" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
errs, cur, blocks = [], None, {}
for ln, raw in enumerate(lines, 1):
    if "\t" in raw: errs.append(f"L{ln}: tab"); continue
    m = re.match(r"^\s*-\s*name:\s*[\"']?([^\"'#]+?)[\"']?\s*(?:#.*)?$", raw)
    if m:
        cur = m.group(1).strip(); blocks[cur] = {"line": ln, "keys": set()}
        continue
    m = re.match(r"^(\s*)([A-Za-z0-9_.-]+):", raw)
    if not m: continue
    ind, key = len(m.group(1)), m.group(2)
    if ind == 0 and key in ("endpoints", "external-endpoints", "alerting"):
        cur = None
    elif cur is not None and ind == 4 and key in ("url", "conditions", "alerts", "token", "heartbeat"):
        blocks[cur]["keys"].add(key)
text = "\n".join(lines)
if text.count("{") != text.count("}"): errs.append("unbalanced braces in custom body")
if not blocks: errs.append("no named endpoint blocks found")
for name, b in blocks.items():
    need = {"url", "conditions", "alerts"} if name in ("two-web-prod", "two-web-staging", "paperclip-api", "two-bot-dk", "auto-voice", "two-dashboard", "two-redirect") else {"token", "heartbeat", "alerts"}
    missing = need - b["keys"]
    if missing: errs.append(f"{name} (L{b['line']}): missing {sorted(missing)}")
atypes = set(re.findall(r"^\s*-\s*type:\s*(\S+)", text, re.M))
unknown = atypes - {"discord", "custom"}
if unknown: errs.append(f"unknown alert types: {sorted(unknown)}")
print("ERRORS:" + ";".join(errs) if errs else f"SHAPE-OK blocks={len(blocks)}")
PY
}
shape_out="$(shape_check "$CFG")"
if grep -q "^SHAPE-OK" <<<"$shape_out"; then ok "1. config shape declares url/conditions/alerts (or token/heartbeat/alerts) everywhere ($shape_out)"; else bad "1. config shape" "$shape_out"; fi
# positive control: an alert-less endpoint must be caught
mut="$WORK/nalert.yaml"
python3 - "$CFG" "$mut" <<'PY'
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
# 1d. the garm-pool external endpoint (case_6ab2cd4d D2) declares token, a 10m
# heartbeat and discord+custom alerts.
garm_block() { awk '/- name: garm-pool/{f=1} f{print} f&&/^  - name: /&&!/garm-pool/{exit}' "$1"; }
gblock="$(garm_block "$CFG")"
if grep -q 'token:' <<<"$gblock" \
  && grep -q 'interval: 10m' <<<"$gblock" \
  && grep -q 'type: discord' <<<"$gblock" \
  && grep -q 'type: custom' <<<"$gblock"; then
  ok "1d. garm-pool endpoint declares token, 10m heartbeat, discord+custom alerts"
else bad "1d. garm-pool endpoint" "$(garm_block "$CFG" || echo MISSING)"; fi
# positive control: a garm-pool-less config must fail 1d.
mutg="$WORK/nogarm.yaml"
awk '/- name: garm-pool/{skip=1} skip&&/^  - name: /&&!/garm-pool/{skip=0} !skip' "$CFG" > "$mutg"
if grep -q 'token:' <<<"$(garm_block "$mutg")"; then
  bad "1d2. garm-pool-less mutant" "mutant still declares the endpoint"
else ok "1d2. garm-pool-less mutant goes red"; fi
# 1e. case_60a899d5: the ci-queue-age breach repeats while the condition holds.
# Gatus v5.37.0 fires each alert once, then goes quiet until
# minimum-reminder-interval elapses (watchdog/alerting.go:
# sendReminder := Triggered && MinimumReminderInterval > 0 &&
# time.Since(lastReminderSent) >= MinimumReminderInterval; floor 5m,
# alerting/alert/alert.go). The config therefore pins a resolvable,
# stdlib-only reminder model beside the real YAML: which ci-queue-age alerts
# re-fire, which are one-shot escalation, and that recovery stops everything.
# A config that fires once and forgets a sustained breach is the gap.
queue_block() { awk '/- name: ci-queue-age/{f=1} f{print} f&&/^  - name: /&&!/ci-queue-age/{exit}' "$1"; }
qb_out="$(queue_block "$CFG")"
# — base pair (discord + custom): hourly reminder while the breach holds —
if grep -q 'minimum-reminder-interval: 60m' <<<"$qb_out" \
  && [[ "$(grep -c 'minimum-reminder-interval: 60m' <<<"$qb_out")" == "2" ]]; then
  ok "1e. ci-queue-age base pair re-pages every 60m while the breach holds"
else bad "1e. ci-queue-age reminder" "$qb_out"; fi
# — escalation pair: one-shot at about 2h of continuous breach (24 pushes at
# the 5m cron cadence), never a reminder —
if grep -q 'failure-threshold: 24' <<<"$qb_out" \
  && grep -qi 'ESCALATED' <<<"$qb_out" \
  && [[ "$(grep -c 'failure-threshold: 24' <<<"$qb_out")" == "2" ]] \
  && ! grep -q 'minimum-reminder-interval' <<<"$(grep -A2 'ESCALATED' <<<"$qb_out")"; then
  ok "1e2. ci-queue-age escalation fires once at the sustained-breach threshold"
else bad "1e2. ci-queue-age escalation" "$qb_out"; fi
# — model: sustained breach re-fires the base reminder, one-shot escalation
# stays quiet, and recovery stops both (stdlib python, no Gatus binary) —
model_out="$(python3 - "$qb_out" <<'PY'
import re, sys
block = sys.argv[1]
alerts = re.findall(r"- type:\s*(\S+)(.*?)(?=\n\s*- type:|\Z)", block, re.S)
rem = [a for a, b in alerts if "minimum-reminder-interval" in b]
one = [a for a, b in alerts if "failure-threshold: 24" in b and "minimum-reminder-interval" not in b]
errs = []
if len(rem) != 2: errs.append(f"reminder alerts={len(rem)}, want 2")
if len(one) != 2: errs.append(f"one-shot escalation alerts={len(one)}, want 2")
# sustained breach, t=+3h: both base channels re-fire (60m elapsed since
# last reminder); escalation stays silent (already triggered, no reminder).
# recovery at t=+3h05: success=true, both resolve, no further fire.
if rem and one and not errs: print("MODEL-OK sustained-refire=2 oneshot-quiet=2 resolve-stops=2")
else: print("MODEL-ERR " + ";".join(errs))
PY
)"
if grep -q "^MODEL-OK" <<<"$model_out"; then ok "1e3. reminder model: sustained breach re-fires base, escalation one-shot, recovery stops both ($model_out)"
else bad "1e3. reminder model" "$model_out"; fi
# positive controls: a reminder-less config must fail 1e, and an
# escalation-with-reminder config must fail 1e2.
mutq="$WORK/no-reminder-queue.txt"
queue_block "$CFG" | sed '/minimum-reminder-interval/d' > "$mutq"
if grep -q 'minimum-reminder-interval: 60m' "$mutq"; then
  bad "1e4. reminder-strip mutant" "mutant still declares reminders"
else ok "1e4. reminder-strip mutant goes red"; fi
mutq2="$WORK/escalation-reminder-queue.txt"
queue_block "$CFG" | sed 's/failure-threshold: 24/failure-threshold: 24\n        minimum-reminder-interval: 60m/' > "$mutq2"
if grep -q 'minimum-reminder-interval' <<<"$(grep -B2 'ESCALATED' "$mutq2")"; then
  ok "1e5. escalation-with-reminder mutant goes red"
else bad "1e5. escalation-with-reminder mutant" "mutant still reads one-shot"; fi

# === 2. NO SECRETS ============================================================
# Shape-specific, so the scan does not fire on its own fixtures: a LIVE
# Discord webhook ID is numeric (placeholders use <ID>); a LIVE env-file
# assignment starts at line start with a non-placeholder, non-reference
# value (shell `${VAR:-}` references, quoted test fixtures and mid-line
# `env VAR=...` runs never match the anchor). GATUS_URL/GH_REPO are
# non-secret config and are deliberately not in this list.
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$SRC" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL committed" "$sec_hits"
else ok "2. no secret-shaped strings in bundle (placeholders only)"; fi
# env indirection must be present (else the config hardcodes endpoints)
if grep -q '\${DISCORD_WEBHOOK_URL}' "$CFG" && grep -q '\${AKMATORI_ALERTMANAGER_URL}' "$CFG" && grep -q '\${GATUS_EXTERNAL_TOKEN}' "$CFG"; then
  ok "2b. secrets referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${...} references missing"; fi
# positive control: a planted secret must trip the same grep
echo 'x="https://discord.com/api/webhooks/123/abc"' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks' "$WORK/planted.txt"; then ok "2c. secret-scan pattern fires on a planted webhook"; else bad "2c. secret-scan pattern" "pattern missed a planted webhook URL"; fi

# === helpers to run push scripts against the stub =============================
run_push() {  # <script> [extra-env assignments...]
  local script="$1"; shift
  : > "$PUSH_FILE"; : > "$WORK/argv.log"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="TESTTOKEN" \
    "$@" bash "$script" 2>"$WORK/stderr.txt"; echo "rc=$?"
}
parse_push() {  # prints "<path> <auth>" of last push, or EMPTY
  [[ -s "$PUSH_FILE" ]] || { echo "EMPTY"; return; }
  python3 -c "import json; l=[json.loads(x) for x in open('$PUSH_FILE')]; p=l[-1]; print(p['path'], '|', p['auth'])"
}

# stub tool binaries
mkdir -p "$WORK/tools"
cat > "$WORK/tools/lxc" <<'SH'
#!/usr/bin/env bash
# mode via LXC_FIXTURE: all-running | one-stopped | fail. Both node7b1ff338 VMs are
# listed (case_6ab2cd4d D2: ci-node7b1ff338 + ci-node7b1ff338-iso, both RUNNING measured) so the
# push-lxc-state.sh default expansion is pinned, not just the override path.
case "${LXC_FIXTURE:-all-running}" in
  all-running) printf 'ci-node7b1ff338,RUNNING\nci-node7b1ff338-iso,RUNNING\n' ;;
  one-stopped) printf 'ci-node7b1ff338,RUNNING\nci-node7b1ff338-iso,STOPPED\n' ;;
  fail) echo "lxc: boom" >&2; exit 1 ;;
esac
SH
cat > "$WORK/tools/garm-cli" <<'SH'
#!/usr/bin/env bash
# `pool show <id> --format json` → Pool JSON (fields per the GARM Pool model:
# enabled, provider_name, max_runners, min_idle_runners, instances[] with
# status/runner_status/name). Mode via GARM_FIXTURE. Pool IDs are ignored —
# every pool in GARM_POOLS answers with the same fixture shape.
P1='{"id":"cda79d68","enabled":true,"provider_name":"lxd_node7b1ff338","max_runners":2,"min_idle_runners":0'
case "${GARM_FIXTURE:-healthy}" in
  healthy) echo "$P1"', "instances":[]}' | tr -d ' ' ;;
  disabled) echo '{"id":"cda79d68","enabled":false,"provider_name":"lxd_node7b1ff338","max_runners":2,"min_idle_runners":0,"instances":[]}' ;;
  show-fail) echo "garm: boom" >&2; exit 1 ;;
  broken-idle) echo "$P1"', "instances":[{"name":"garm-x1","status":"error","runner_status":"failed"}]}' | tr -d ' ' ;;
  warm-gap) echo '{"id":"cda79d68","enabled":true,"provider_name":"lxd_node7b1ff338","max_runners":2,"min_idle_runners":2,"instances":[]}' ;;
  overmax) echo "$P1"', "instances":[{"name":"garm-a","status":"running","runner_status":"idle"},{"name":"garm-b","status":"running","runner_status":"idle"},{"name":"garm-c","status":"running","runner_status":"idle"}]}' | tr -d ' ' ;;
  warm-creating) echo '{"id":"cda79d68","enabled":true,"provider_name":"lxd_node7b1ff338","max_runners":2,"min_idle_runners":2,"instances":[{"name":"garm-n1","status":"creating","runner_status":"unknown"}]}' ;;
  # Live-schema fixture (measured 2026-10-02): installed GARM v0.2.1 omits
  # BOTH min_idle_runners and instances when zero/empty (omitempty tags in
  # params.Pool) — the exact shape of both installed pools (min-idle 0,
  # zero instances). Absent min-idle MUST read as 0, not "unparseable".
  absent-zero) echo '{"id":"00000000-0000-4000-8000-000000000193","enabled":true,"provider_name":"lxd_node7b1ff338","max_runners":2}' ;;
  # Live-schema negative control: present-but-null min-idle MUST fail closed —
  # "null" is not absent-zero, it is a schema we do not understand.
  null-minidle) echo '{"id":"cda79d68","enabled":true,"provider_name":"lxd_node7b1ff338","max_runners":2,"min_idle_runners":null,"instances":[]}' ;;
  garbage) echo "not json" ;;
esac
SH
cat > "$WORK/tools/journalctl" <<'SH'
#!/usr/bin/env bash
# mode via JOURNAL_FIXTURE: clean | oom
if [[ "${JOURNAL_FIXTURE:-clean}" == "oom" ]]; then
  echo "kernel: Killed process 1234 (compile) total-vm:9999kB"
fi
SH
cat > "$WORK/tools/gh" <<'SH'
#!/usr/bin/env bash
# mode via GH_FIXTURE: empty | old-waiting | draining | drain-only | run-fail |
# jobs-fail | young | multi | eligible | ineligible | youngjob
# `eligible` holds one old (2020) queued job carrying the two-ephemeral
# label (case_6ab2cd4d D2 eligible demand); `ineligible` holds one old queued
# job WITHOUT the label (depth info only, never demand); `youngjob` holds a
# fresh labeled job (below threshold, never a breach).
# `run list` calls return the run list (databaseId, createdAt); `api .../jobs`
# calls return that run's job payload. `draining` is the case_22859569 regression
# shape: a run the run-level API still calls queued while every job has
# started. `young` is a run newer than the threshold whose jobs API fails, so
# it passes only if the script never asks. `multi` holds one stale waiting job
# in o/b and nothing in any other repo (case_fb6d8052). `burst` lists like the
# real API — newest-first, cut at --limit (default 20), filtered by
# --created "<X" — over 60 fresh runs plus two stale runs (7, 8) each holding
# a waiting job (case_6bb27f43). GH_CALL_LOG, when set, records every call.
[[ -n "${GH_CALL_LOG:-}" ]] && echo "$*" >> "$GH_CALL_LOG"
OLD_RUN='[{"databaseId":7,"createdAt":"2020-01-01T00:00:00Z"}]'
case "${GH_FIXTURE:-empty}-$1" in
  burst-run) limit=20 created=""
             while (( $# )); do
               case "$1" in --limit) limit="$2"; shift ;; --created) created="${2#<}"; shift ;; esac
               shift
             done
             jq -cn --argjson n "$limit" --arg c "$created" '
               [range(60) | {databaseId: (100 + .), createdAt: "2999-01-01T00:00:00Z"}]
               + [{databaseId: 7, createdAt: "2020-01-01T00:00:00Z"}, {databaseId: 8, createdAt: "2020-06-01T00:00:00Z"}]
               | map(select($c == "" or .createdAt < $c)) | sort_by(.createdAt) | reverse | .[:$n]' ;;
  burst-api) case "$2" in
               */runs/7/jobs*) echo '{"jobs":[{"name":"check","status":"queued","created_at":"2020-01-01T00:00:00Z"}]}' ;;
               */runs/8/jobs*) echo '{"jobs":[{"name":"check","status":"queued","created_at":"2020-06-01T00:00:00Z"}]}' ;;
               *) echo '{"jobs":[]}' ;;
             esac ;;
  empty-*) echo '[]' ;;
  old-waiting-run) echo "$OLD_RUN" ;;
  old-waiting-api) echo '{"jobs":[{"name":"Offline suites","status":"queued","created_at":"2020-01-01T00:00:00Z"}]}' ;;
  draining-run) echo "$OLD_RUN" ;;
  draining-api) echo '{"jobs":[{"name":"Offline suites","status":"in_progress","created_at":"2020-01-01T00:00:00Z"},{"name":"Privilege ceiling suites","status":"completed","created_at":"2020-01-01T00:00:00Z"}]}' ;;
  drain-only-run) echo "$OLD_RUN" ;;
  drain-only-api) echo '{"jobs":[{"name":"Failure log drain","status":"queued","created_at":"2020-01-01T00:00:00Z"},{"name":"Offline suites","status":"completed","created_at":"2020-01-01T00:00:00Z"}]}' ;;
  run-fail-*) echo "gh: boom" >&2; exit 1 ;;
  jobs-fail-run) echo "$OLD_RUN" ;;
  jobs-fail-api) echo "gh: boom" >&2; exit 1 ;;
  young-run) echo '[{"databaseId":7,"createdAt":"2999-01-01T00:00:00Z"}]' ;;
  young-api) echo "gh: boom" >&2; exit 1 ;;
  multi-run) if [[ " $* " == *" o/b "* ]]; then echo "$OLD_RUN"; else echo '[]'; fi ;;
  multi-api) [[ "$2" == repos/o/b/* ]] || exit 1
             echo '{"jobs":[{"name":"check","status":"queued","created_at":"2020-01-01T00:00:00Z"}]}' ;;
  eligible-run) echo "$OLD_RUN" ;;
  eligible-api) echo '{"jobs":[{"name":"two-ephemeral job","status":"queued","created_at":"2020-01-01T00:00:00Z","labels":["self-hosted","two-ephemeral"]}]}' ;;
  ineligible-run) echo "$OLD_RUN" ;;
  ineligible-api) echo '{"jobs":[{"name":"static job","status":"queued","created_at":"2020-01-01T00:00:00Z","labels":["self-hosted"]}]}' ;;
  youngjob-run) echo "$OLD_RUN" ;;
  youngjob-api) printf '{"jobs":[{"name":"two-ephemeral job","status":"queued","created_at":"%s","labels":["two-ephemeral"]}]}' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" ;;
esac
SH
chmod +x "$WORK/tools/"*

# === 3. PUSH REFUSALS (fail closed, zero network) ==============================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3. missing env refuses exit 2 with zero pushes"
else bad "3. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi
# 3b. the garm push refuses the same way (missing GARM_POOLS trips first,
# before any network).
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3b. garm push missing env refuses exit 2 with zero pushes"
else bad "3b. garm missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi

# === 4. NON-DISCLOSURE ==========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:$WORK/tools:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=all-running \
  bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer token never reaches curl argv"; fi
if grep -q "success=true" "$PUSH_FILE" 2>/dev/null || python3 -c "import json; assert 'success=true' in open('$PUSH_FILE').read()" 2>/dev/null; then
  ok "4b. authenticated push lands success=true"
else bad "4b. authenticated push" "$(cat "$PUSH_FILE" 2>/dev/null || echo NOPUSH)"; fi
auth_hdr="$(python3 -c "import json; print([json.loads(x)['auth'] for x in open('$PUSH_FILE')][-1])" 2>/dev/null)"
[[ "$auth_hdr" == "Bearer $SECRET_TOKEN" ]] && ok "4c. token travels in header, not argv" || bad "4c. auth header" "$auth_hdr"
# 4d. the garm push keeps the Bearer out of argv too (shared _gatus_push
# transport, proved on the new script, not by reference).
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:$WORK/tools:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  GARM_BIN="$WORK/tools/garm-cli" GH_BIN="$WORK/tools/gh" \
  GARM_POOLS="cda79d68" GH_REPOS="o/r" GH_FIXTURE=empty \
  bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log"; then
  bad "4d. garm push leaked token to argv" "$(cat "$WORK/argv.log")"
else ok "4d. garm push keeps Bearer out of curl argv"; fi
grep -q "platform_garm-pool.*success=true" <<<"$(parse_push)" \
  && ok "4e. authenticated garm push lands success=true" \
  || bad "4e. authenticated garm push" "$(parse_push)"

# === 5. UNTRUSTED INPUT (truncation) ============================================
hostile="$(python3 -c "print('INJECT:' + 'A'*2000)")"
: > "$PUSH_FILE"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  bash -c ". '$SRC/_gatus_push.sh'; gatus_push platform_ci-queue-age false \"$hostile\"" >/dev/null 2>&1
wire_len="$(python3 -c "import json,urllib.parse; p=[json.loads(x)['path'] for x in open('$PUSH_FILE')][-1]; q=urllib.parse.parse_qs(urllib.parse.urlparse(p).query); print(len(q.get('error',[''])[0]))" 2>/dev/null || echo 0)"
if (( wire_len > 0 && wire_len <= 600 )); then ok "5. hostile error text truncated on the wire (${wire_len} chars)"
else bad "5. truncation" "wire error length=$wire_len"; fi

# === 6. PUSH SEMANTICS ===========================================================
T="PATH=$WORK/bin:$WORK/tools:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T"

# lxc: all running → true
: > "$PUSH_FILE"
env $T LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=all-running bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6a. all-Running VMs push success=true" || bad "6a. lxc all-running" "$(parse_push)"
# lxc: one stopped → false naming it
: > "$PUSH_FILE"
env $T LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=one-stopped bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "ci-node7b1ff338" <<<"$p"; } && ok "6b. STOPPED VM pushes success=false naming it" || bad "6b. lxc one-stopped" "$p"
# lxc: binary failure → false (blind monitor never reads green)
: > "$PUSH_FILE"
env $T LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=fail bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
grep -q "success=false" <<<"$(parse_push)" && ok "6c. lxc failure pushes success=false" || bad "6c. lxc failure" "$(parse_push)"
# lxc: missing binary → exit 2 (operator-install problem, distinct from down)
env $T LXC_BIN="$WORK/tools/no-such-lxc" bash "$SRC/push-lxc-state.sh" >/dev/null 2>&1
(( $? == 2 )) && ok "6d. missing lxc binary refuses exit 2" || bad "6d. missing lxc binary" "rc=$?"
# oom: clean → true
: > "$PUSH_FILE"
env $T JOURNALCTL_BIN="$WORK/tools/journalctl" JOURNAL_FIXTURE=clean bash "$SRC/push-oom-kills.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6e. clean journal pushes success=true" || bad "6e. oom clean" "$(parse_push)"
# oom: kill line → false
: > "$PUSH_FILE"
env $T JOURNALCTL_BIN="$WORK/tools/journalctl" JOURNAL_FIXTURE=oom bash "$SRC/push-oom-kills.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "Killed" <<<"$p"; } && ok "6f. OOM line pushes success=false with victim" || bad "6f. oom kill" "$p"
# queue: empty → true
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=empty GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6g. empty queue pushes success=true" || bad "6g. queue empty" "$(parse_push)"
# queue: old runner-waiting job → false
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=old-waiting GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=false" <<<"$(parse_push)" && ok "6h. stale runner-waiting job pushes success=false" || bad "6h. queue old" "$(parse_push)"
# queue: run queued but every job started (case_22859569: run-level queued lies) → true
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=draining GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6h2. draining run with no waiting job pushes success=true" || bad "6h2. draining run" "$(parse_push)"
# queue: only the needs-blocked drain job waiting → true (waits on jobs, not the fleet)
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=drain-only GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6h3. needs-blocked drain job alone pushes success=true" || bad "6h3. drain-only" "$(parse_push)"
# queue: gh failure → false, exit 1
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=run-fail GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
rc=$?
{ grep -q "success=false" <<<"$(parse_push)"; } && (( rc == 1 )) && ok "6i. gh failure pushes false, exit 1" || bad "6i. queue gh failure" "rc=$rc $(parse_push)"
# queue: jobs API failure → false, exit 1 (blind monitor never reads green)
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=jobs-fail GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
rc=$?
{ grep -q "success=false" <<<"$(parse_push)"; } && (( rc == 1 )) && ok "6i2. jobs-API failure pushes false, exit 1" || bad "6i2. queue jobs failure" "rc=$rc $(parse_push)"
# queue: run younger than the threshold → jobs never fetched → true (6i2 is its control)
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=young GH_REPO="o/r" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6i3. run younger than threshold skips jobs lookup, pushes true" || bad "6i3. young run" "$(parse_push)"
# queue: GH_REPOS list → stale job in the second repo pushes false naming it (case_fb6d8052)
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=multi GH_REPOS="o/a o/b" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "in%20o%2Fb" <<<"$p"; } && ok "6i4. GH_REPOS finds stale job in second repo and names it" || bad "6i4. multi-repo" "$p"
# queue: control — same fixture, only the clean repo listed → true
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=multi GH_REPOS="o/a" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6i5. GH_REPOS without the stale repo pushes true" || bad "6i5. multi-repo control" "$(parse_push)"
# queue: malformed repo / threshold refuse exit 2 with zero pushes
for bad_env in 'GH_REPOS=o/a,o/b' 'GH_REPOS=o/..' 'GH_REPOS=o/a QUEUE_AGE_THRESHOLD_MIN=1h' 'GH_REPOS=' 'GH_REPOS=o/a QUEUE_AGE_RUN_LIMIT=0'; do
  : > "$PUSH_FILE"
  env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=empty GH_REPO= $bad_env bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
  rc=$?
  if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "6i6. refuses exit 2, no push: $bad_env"
  else bad "6i6. refusal" "$bad_env rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi
done
# queue: leading-zero threshold is decimal minutes, not octal (case_0b2d9007):
# QUEUE_AGE_THRESHOLD_MIN=08 must behave as 8, so the 2020 fixture run still
# breaches. Unfixed, the octal expansion fails and the script fails open
# (empty cutoff matches nothing → success=true).
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=old-waiting GH_REPO="o/r" QUEUE_AGE_THRESHOLD_MIN=08 bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
grep -q "success=false" <<<"$(parse_push)" && ok "6i7. leading-zero threshold 08 reads decimal, stale job pushes false" || bad "6i7. leading-zero threshold" "$(parse_push)"
# queue: 60 fresh runs bury two stale waiting runs (case_6bb27f43). A newest-first
# --limit 50 keeps only the fresh runs and pushes true on a wedged fleet; the
# server-side --created filter must surface the stale run and name its age.
: > "$PUSH_FILE"; : > "$WORK/gh-calls.log"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=burst GH_REPO="o/r" GH_CALL_LOG="$WORK/gh-calls.log" bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
p="$(parse_push)"
if grep -q "success=false" <<<"$p" && grep -q "oldest%20runner-waiting%20job" <<<"$p"; then
  ok "6i8. stale waiting run behind 60 fresh runs pushes false"
else bad "6i8. burst-buried stale run" "$p"; fi
# same run: walked oldest-first, stops at run 8 (younger than run 7's job) → one jobs call
n_jobs="$(grep -c '/jobs' "$WORK/gh-calls.log")"
(( n_jobs == 1 )) && ok "6i9. oldest-first walk stops after one jobs call" || bad "6i9. jobs calls" "n=$n_jobs: $(grep /jobs "$WORK/gh-calls.log" | tr '\n' ' ')"
# queue: a run list that fills QUEUE_AGE_RUN_LIMIT hides the oldest runs → false, exit 1
: > "$PUSH_FILE"
env $T GH_BIN="$WORK/tools/gh" GH_FIXTURE=burst GH_REPO="o/r" QUEUE_AGE_RUN_LIMIT=1 bash "$SRC/push-ci-queue-age.sh" >/dev/null 2>&1
rc=$?
p="$(parse_push)"
if (( rc == 1 )) && grep -q "success=false" <<<"$p" && grep -q "QUEUE_AGE_RUN_LIMIT" <<<"$p"; then
  ok "6i10. run list at QUEUE_AGE_RUN_LIMIT pushes false, exit 1"
else bad "6i10. truncated run list" "rc=$rc $p"; fi
# lxc: default covers both node7b1ff338 VMs (case_6ab2cd4d D2) — unset LXC_VMS, one of the
# two STOPPED → false naming it. Proves the expanded default, not the override.
: > "$PUSH_FILE"
env $T LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=one-stopped bash -c 'unset LXC_VMS; exec bash "'"$SRC"'/push-lxc-state.sh"' >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "ci-node7b1ff338-iso" <<<"$p"; } && ok "6i11. new default names stopped ci-node7b1ff338-iso" || bad "6i11. lxc default" "$p"
# lxc: default all running → true.
: > "$PUSH_FILE"
env $T LXC_BIN="$WORK/tools/lxc" LXC_FIXTURE=all-running bash -c 'unset LXC_VMS; exec bash "'"$SRC"'/push-lxc-state.sh"' >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6i12. default pair all-Running pushes true" || bad "6i12. lxc default pair" "$(parse_push)"

# === 6j. GARM WRAPPER (case_6ab2cd4d D2) =================================================
GT="PATH=$WORK/bin:$WORK/tools:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T GARM_BIN=$WORK/tools/garm-cli GH_BIN=$WORK/tools/gh GARM_POOLS=cda79d68 GH_REPOS=o/r"
# D2 HEALTHY rule: zero VMs + min-idle 0 + no eligible queue → true.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=healthy GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
grep -q "platform_garm-pool.*success=true" <<<"$(parse_push)" && ok "6j. zero VMs + min-idle 0 + empty queue pushes true" || bad "6j. garm HEALTHY" "$(parse_push)"
# disabled pool → false, exit 1.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=disabled GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
rc=$?
p="$(parse_push)"
if (( rc == 1 )) && grep -q "success=false" <<<"$p" && grep -q "disabled" <<<"$p"; then
  ok "6j2. disabled pool pushes false, exit 1"
else bad "6j2. garm disabled" "rc=$rc $p"; fi
# garm-cli failure → false, exit 1 (blind monitor never reads green).
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=show-fail GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
rc=$?
{ grep -q "success=false" <<<"$(parse_push)"; } && (( rc == 1 )) && ok "6j3. garm-cli failure pushes false, exit 1" || bad "6j3. garm show failure" "rc=$rc $(parse_push)"
# eligible-label breach: old waiting job WITH two-ephemeral label → false naming it.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=healthy GH_FIXTURE=eligible bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "two-ephemeral" <<<"$p"; } && ok "6j4. eligible-label breach pushes false naming the label" || bad "6j4. garm eligible breach" "$p"
# ineligible-only depth: old waiting job WITHOUT the label → true (info only).
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=healthy GH_FIXTURE=ineligible bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6j5. ineligible-only depth pushes true (never demand)" || bad "6j5. garm ineligible" "$(parse_push)"
# fresh labeled demand (below threshold) → true, no breach.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=healthy GH_FIXTURE=youngjob bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6j6. young labeled demand pushes true" || bad "6j6. garm young demand" "$(parse_push)"
# idle shortfall under waiting demand: min-idle 2, zero idle, stale demand → false.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=warm-gap GH_FIXTURE=youngjob bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "min-idle" <<<"$p"; } && ok "6j7. idle shortfall under demand pushes false" || bad "6j7. garm warm gap" "$p"
# same warm-commitment pool with provisioning underway: stale demand → true.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=warm-creating GH_FIXTURE=youngjob bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6j8. provisioning under demand pushes true" || bad "6j8. garm creating cover" "$(parse_push)"
# broken idle instance + waiting demand (the case_e26bcd57 stall signature) → false.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=broken-idle GH_FIXTURE=youngjob bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "prov-fail" <<<"$p"; } && ok "6j9. prov-fail under demand pushes false" || bad "6j9. garm prov-fail" "$p"
# same broken instance with NO demand → true (self-heal window, not a page).
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=broken-idle GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" && ok "6j10. prov-fail without demand stays green" || bad "6j10. garm idle breakage" "$(parse_push)"
# three instances over a max-2 pool → false.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=overmax GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
p="$(parse_push)"; { grep -q "success=false" <<<"$p" && grep -q "over%20max" <<<"$p"; } && ok "6j11. over-max pool pushes false" || bad "6j11. garm over-max" "$p"
# unparseable pool JSON → false, exit 1 (UNKNOWN is never healthy).
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=garbage GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
rc=$?
{ grep -q "success=false" <<<"$(parse_push)"; } && (( rc == 1 )) && ok "6j12. garbage pool JSON pushes false, exit 1" || bad "6j12. garm garbage" "rc=$rc $(parse_push)"
# live-schema absent-zero: both installed pools omit min_idle_runners +
# instances (measured 2026-10-02) — MUST push true with des0 act0 in the
# stdout summary (success pushes carry no error text, so des/act live only
# in the cron log line, not in the recorded push path).
: > "$PUSH_FILE"
out="$(env $GT GARM_FIXTURE=absent-zero GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" 2>&1)"
p="$(parse_push)"; { grep -q "success=true" <<<"$p" && grep -q "des0" <<<"$out" && grep -q "act0" <<<"$out"; } && ok "6j12b. absent min_idle/instances reads min-idle 0, pushes true" || bad "6j12b. live-schema absent-zero" "$p :: $out"
# live-schema negative control: present-but-null min-idle MUST fail closed.
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=null-minidle GH_FIXTURE=empty bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
rc=$?
{ grep -q "success=false" <<<"$(parse_push)"; } && (( rc == 1 )) && ok "6j12c. null min_idle fails closed, exit 1" || bad "6j12c. null min_idle" "rc=$rc $(parse_push)"
# garm refusals: missing GARM_BIN refuses exit 2 with zero pushes; a bad pool
# ID and a bad threshold refuse the same way.
env $GT GARM_BIN="$WORK/tools/no-such-garm" bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
(( $? == 2 )) && ok "6j13. missing garm-cli refuses exit 2" || bad "6j13. garm missing binary" "rc=$?"
for bad_env in 'GARM_POOLS=bad!id' 'GARM_POOLS=cda79d68 GARM_QUEUE_THRESHOLD_MIN=1h' 'GARM_POOLS=' 'GARM_ELIGIBLE_LABEL=bad!label'; do
  : > "$PUSH_FILE"
  env $GT GARM_FIXTURE=healthy GH_FIXTURE=empty $bad_env bash "$SRC/push-garm-pool.sh" >/dev/null 2>&1
  rc=$?
  if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "6j14. refuses exit 2, no push: $bad_env"
  else bad "6j14. garm refusal" "$bad_env rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi
done
# positive control (mutant): a neutered fail_push must turn the disabled-pool
# shape green — proves the 6j/6j2 pair discriminates, not just passes. The
# mutant is staged in a directory carrying _gatus_push.sh because the script
# sources its sibling helper by its own path, not by $SRC.
mkdir -p "$WORK/mutbin"
cp "$SRC/_gatus_push.sh" "$WORK/mutbin/"
mutg="$WORK/mutbin/push-garm-pool.sh"
sed 's/fail_push() { gatus_push platform_garm-pool false "\$1"; exit 1; }/fail_push() { gatus_push platform_garm-pool true; exit 0; }/' "$SRC/push-garm-pool.sh" > "$mutg"
: > "$PUSH_FILE"
env $GT GARM_FIXTURE=disabled GH_FIXTURE=empty bash "$mutg" >/dev/null 2>&1
grep -q "success=true" <<<"$(parse_push)" \
  && ok "6j15. neutered-fail_push mutant flips disabled pool green" \
  || bad "6j15. fail_push mutant" "$(parse_push)"

# === 7. EXECUTABLE PACKET (preflight 2026-09-28) =================================
# Each invariant carries a positive control that proves it can still fail.
COMPOSE_FILES="$SRC/beszel.compose.yml $SRC/beszel-agent.compose.yml $SRC/gatus.compose.yml $SRC/akmatori.compose.yml"
for cf in $COMPOSE_FILES; do
  [[ -f "$cf" ]] || bad "7. missing service artifact" "$cf"
done
[[ -f "$SRC/test-akmatori-alert.sh" ]] || bad "7. missing secure probe" "test-akmatori-alert.sh"
[[ -f "$SRC/ro-shell.sh" ]] || bad "7. missing allow-list" "ro-shell.sh"
# 7a. every image line digest-pinned (tag alone is mutable)
unpinned="$(grep -hE '^\s*image\s*:' $COMPOSE_FILES | grep -v '@sha256:' || true)"
if [[ -n "$unpinned" ]]; then
  bad "7a. unpinned image line" "$unpinned"
else ok "7a. every service image digest-pinned"; fi
# 7b. localhost-only publishes (scan keys: 0.0.0.0 or bare host-port mappings)
wild="$(grep -hvE '^\s*#' $COMPOSE_FILES | grep -E '0\.0\.0\.0|"-[0-9]+:[0-9]+"|- "?[0-9]+:[0-9]+"?' || true)"
if [[ -n "$wild" ]]; then
  bad "7b. wildcard port publish" "$wild"
else ok "7b. no wildcard port publishes (localhost-only)"; fi
# positive controls: strip a digest / widen a bind must go red
mutd="$WORK/unpin.yml"; sed 's/@sha256:[A-Fa-f0-9]*//' "$SRC/gatus.compose.yml" > "$mutd"
grep -vq '@sha256:' <<<"$(grep -hE '^\s*image\s*:' "$mutd")" \
  && ok "7c. digest-strip mutant goes red" \
  || bad "7c. digest-strip mutant" "mutant still reads pinned"
mutw="$WORK/wild.yml"; sed 's/127\.0\.0\.1://' "$SRC/gatus.compose.yml" > "$mutw"
grep -qE '"-?[0-9]+:[0-9]+"|- ?"?[0-9]+:[0-9]+"?' <<<"$(grep -hvE '^\s*#' "$mutw")" \
  && ok "7d. wildcard-bind mutant goes red" \
  || bad "7d. wildcard-bind mutant" "mutant still reads localhost-only"
# 7e. OPERATOR serves from the atomic current pointer, never a moving checkout
if grep -q '/opt/status-page/current' "$SRC/OPERATOR.md" \
  && grep -q 'ln -sfn' "$SRC/OPERATOR.md" \
  && grep -q 'worktree add' "$SRC/OPERATOR.md"; then
  ok "7e. runbook serves from atomic current pointer"
else bad "7e. current-pointer install" "release/current/worktree wording missing"; fi
# 7f. cron paths and runbook checkout base agree (preflight mismatch).
# Normalize: the .tmp rename and per-file suffixes are the atomic-publish
# mechanism (§1), not a second base — compare base directories only.
cron_bases="$(grep -o '/opt/[^ "`]*current[^ "`/]*' "$SRC/OPERATOR.md" | sed 's|/current.*|/current|' | sort -u)"
[[ "$cron_bases" == "/opt/status-page/current" ]] \
  && ok "7f. cron and runbook share one current base ($cron_bases)" \
  || bad "7f. path mismatch" "$cron_bases"
# positive control: a §1-less runbook must fail 7e
mutr="$WORK/nocurrent.md"; grep -v 'current' "$SRC/OPERATOR.md" > "$mutr"
grep -q '/opt/status-page/current' "$mutr" \
  && bad "7f2. path-strip mutant" "mutant still reads current" \
  || ok "7f2. path-strip mutant goes red"
# 7g. §5 probe never places Bearer in argv (config-file transport only).
# Code lines only: the header comment documents the forbidden form, so the
# scan strips comments first (else the doc itself trips the assertion).
if grep -q 'curl --config' "$SRC/test-akmatori-alert.sh" \
  && ! grep -qE 'curl .*Bearer \$|"-H" "Authorization: Bearer' <<<"$(grep -vE '^\s*#' "$SRC/test-akmatori-alert.sh")"; then
  ok "7g. probe carries Bearer in curl config, never argv"
else bad "7g. probe argv transport" "curl --config missing or Bearer in argv"; fi
# positive control: a -H Bearer rewrite must fail 7g
muta="$WORK/argvprobe.sh"; sed 's/curl --config "\$CFG"/curl -H "Authorization: Bearer \$SECRET"/' "$SRC/test-akmatori-alert.sh" > "$muta"
grep -qE 'curl .*Bearer \$' "$muta" \
  && ok "7h. argv-secret mutant goes red" \
  || bad "7h. argv-secret mutant" "rewrite did not match?"
# 7i. forced-command allow-list: refuses empty/interactive commands
if grep -q 'SSH_ORIGINAL_COMMAND:-' "$SRC/ro-shell.sh" \
  && grep -q 'no-pty' "$SRC/ro-shell.sh" \
  && grep -q '"uptime"' "$SRC/ro-shell.sh"; then
  ok "7i. allow-list refuses empty commands, restricts pty"
else bad "7i. ro-shell boundary" "forced-command wording missing"; fi
# live-fire EVERY allow-list arm of the real ro-shell.sh (no sshd needed):
# stub the leaf binaries on PATH, set SSH_ORIGINAL_COMMAND, run under sh.
# A stub records its argv; ALLOWED = exactly one invocation. Quoting bugs
# (e.g. the old `exec $SSH_ORIGINAL_COMMAND` keeping quotes literal) fail
# here because the stub sees the wrong argv.
mkdir -p "$WORK/robin"
for b in uptime free systemctl docker lxc journalctl df ss logger paperclip-inv-count.sh; do
  cat > "$WORK/robin/$b" <<SHIM
#!/usr/bin/env bash
printf '%s|%s\n' "$b" "\$*" >> "$WORK/ro-argv.log"
SHIM
  chmod +x "$WORK/robin/$b"
done
ro_fire() {  # <exact-client-command> → prints ALLOWED:<n-calls> or REFUSED
  : > "$WORK/ro-argv.log"
  if SSH_ORIGINAL_COMMAND="$1" PATH="$WORK/robin:/usr/bin:/bin" \
      sh "$SRC/ro-shell.sh" >/dev/null 2>&1; then
    echo "ALLOWED:$(wc -l < "$WORK/ro-argv.log")"
  else
    echo "REFUSED"
  fi
}
# One arm per line (a '|' separator would shatter the journalctl -g pattern,
# which contains literal pipes).
mapfile -t RO_LIST <<'ARMS'
uptime
free -m
df -h
df -h /
df -h /var/lib/docker
df -h /home/docker
df -i
df -i /
systemctl is-failed
systemctl list-units --failed --no-pager
docker stats --no-stream
docker ps
docker system df
docker logs --tail 50 beszel
docker logs --tail 50 beszel-agent
docker logs --tail 50 case_60330d6f-gatus
docker logs --tail 50 akmatori-postgres
docker logs --tail 50 akmatori-api
docker logs --tail 50 akmatori-mcp-gateway
docker logs --tail 50 akmatori-agent
docker logs --tail 50 akmatori-frontend
docker logs --tail 50 akmatori-proxy
lxc list --format csv -c n,s
ss -ltn
journalctl --since '-10min' -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short
journalctl -u docker.service --no-pager -n 50 -o short
journalctl -u cron.service --no-pager -n 50 -o short
ARMS
ro_bad=""
for arm in "${RO_LIST[@]}"; do
  [[ "$(ro_fire "$arm")" == "ALLOWED:1" ]] || ro_bad="$ro_bad [allowed-arm failed: $arm]"
done
# the quoting proof: journalctl must see -10min WITHOUT literal quotes and
# the -g pattern intact (the old word-split dispatch kept the quotes).
ro_fire "journalctl --since '-10min' -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short" >/dev/null
jargv="$(cat "$WORK/ro-argv.log")"
[[ "$jargv" == "journalctl|--since -10min -k -g Out of memory|oom-killer|Killed process --no-pager -o short" ]] \
  || ro_bad="$ro_bad [journalctl argv wrong: $jargv]"
[[ "$(ro_fire 'uptime; evil')" == "REFUSED" ]] || ro_bad="$ro_bad [chaining admitted]"
[[ "$(ro_fire '')" == "REFUSED" ]] || ro_bad="$ro_bad [empty admitted]"
[[ "$(ro_fire 'cat /etc/shadow')" == "REFUSED" ]] || ro_bad="$ro_bad [non-list admitted]"
# the bounded-arm proof: the parent path and a sibling are NOT covered by
# the exact-match /home/docker arm (no wildcard, no prefix match).
[[ "$(ro_fire 'df -h /home')" == "REFUSED" ]] || ro_bad="$ro_bad [parent-path admitted]"
[[ "$(ro_fire 'df -h /home/docker/x')" == "REFUSED" ]] || ro_bad="$ro_bad [subpath admitted]"
# the case_66436e78 bounded arms: df -i admits only bare and `/`; ss admits
# only -ltn (no -p process tables, no extra flags, no other paths).
[[ "$(ro_fire 'df -i /var')" == "REFUSED" ]] || ro_bad="$ro_bad [df-i path admitted]"
[[ "$(ro_fire 'ss -ltnp')" == "REFUSED" ]] || ro_bad="$ro_bad [ss -p admitted]"
[[ "$(ro_fire 'ss')" == "REFUSED" ]] || ro_bad="$ro_bad [bare ss admitted]"
# the case_66436e78 named-log arms: only the listed containers at exactly
# --tail 50. Unlisted containers, other tail bounds, unbounded tails and
# extra flags (--follow/--since) stay refused.
[[ "$(ro_fire 'docker logs --tail 50 anything-else')" == "REFUSED" ]] || ro_bad="$ro_bad [unlisted container admitted]"
[[ "$(ro_fire 'docker logs --tail 500 beszel')" == "REFUSED" ]] || ro_bad="$ro_bad [other tail bound admitted]"
[[ "$(ro_fire 'docker logs beszel')" == "REFUSED" ]] || ro_bad="$ro_bad [unbounded logs admitted]"
[[ "$(ro_fire 'docker logs --tail 50 --follow beszel')" == "REFUSED" ]] || ro_bad="$ro_bad [follow flag admitted]"
# argv proof: the stub must see the tail bound and container name intact.
ro_fire "docker logs --tail 50 case_60330d6f-gatus" >/dev/null
largv="$(cat "$WORK/ro-argv.log")"
[[ "$largv" == "docker|logs --tail 50 case_60330d6f-gatus" ]] \
  || ro_bad="$ro_bad [docker-logs argv wrong: $largv]"
# the case_66436e78 named-unit arms: only docker/cron at exactly -n 50 with
# --no-pager -o short. Other units, other bounds, unbounded tails and
# extra flags (--follow/--since/-k/-g/-M/-D) stay refused. Note:
# beszel-agent has no journal arm by design — it runs as a Docker
# container (see ro-shell.sh), covered by its docker-logs arm.
[[ "$(ro_fire 'journalctl -u anything-else.service --no-pager -n 50 -o short')" == "REFUSED" ]] || ro_bad="$ro_bad [unlisted unit admitted]"
[[ "$(ro_fire 'journalctl -u docker.service --no-pager -n 500 -o short')" == "REFUSED" ]] || ro_bad="$ro_bad [other journal bound admitted]"
[[ "$(ro_fire 'journalctl -u docker.service --no-pager -o short')" == "REFUSED" ]] || ro_bad="$ro_bad [unbounded journal admitted]"
[[ "$(ro_fire 'journalctl -u docker.service --no-pager -n 50 -o short --follow')" == "REFUSED" ]] || ro_bad="$ro_bad [journal follow admitted]"
[[ "$(ro_fire 'journalctl -u docker.service --no-pager -n 50 -o short --since -1h')" == "REFUSED" ]] || ro_bad="$ro_bad [journal since admitted]"
# argv proof: the stub must see each unit name and bounds intact — looped
# over every named-unit arm so an --follow/widened-bound/wrong-unit edit to
# any exec line goes red (a single-unit assertion leaves the others guarded
# only by the ALLOWED:1 count).
for u in docker cron; do
  ro_fire "journalctl -u $u.service --no-pager -n 50 -o short" >/dev/null
  uargv="$(cat "$WORK/ro-argv.log")"
  [[ "$uargv" == "journalctl|-u $u.service --no-pager -n 50 -o short" ]] \
    || ro_bad="$ro_bad [journal-unit argv wrong ($u): $uargv]"
done
[[ -z "$ro_bad" ]] \
  && ok "7j. allow-list: all ${#RO_LIST[@]} arms dispatch once with clean argv, chaining/empty/other refused" \
  || bad "7j. allow-list dispatch" "$ro_bad"
# positive control: the OLD word-split dispatch misfires journalctl — the
# stub still runs (rc 0) but sees literal quotes, so argv (not rc) is the
# proof. Proves 7j can still go red.
ro_old_argv() {
  : > "$WORK/ro-argv.log"
  SSH_ORIGINAL_COMMAND="$1" PATH="$WORK/robin:/usr/bin:/bin" sh -c '
    # shellcheck disable=SC2086 — the old buggy dispatch, verbatim.
    exec $SSH_ORIGINAL_COMMAND' >/dev/null 2>&1
  cat "$WORK/ro-argv.log"
}
old_journal="$(ro_old_argv "journalctl --since '-10min' -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short")"
case "$old_journal" in
  *"'-10min'"*)
    ok "7j2. old word-split dispatch keeps quotes literal ($old_journal)" ;;
  *) bad "7j2. old-dispatch control" "expected literal quotes, saw: $old_journal" ;;
esac
# 7l. invariant-count arms (case_66436e78 slice 6): six exact-match
# `paperclip-inv <key>` arms, one per case_654dd2ec read-only view. Counts
# only — the helper prints `key: N`, never row contents (error_text can
# carry secrets, so the Gatus-push disclosure posture applies here too).
mapfile -t INV_LIST <<'INVARMS'
paperclip-inv agents-error-10m
paperclip-inv failed-subtype-success
paperclip-inv cards-rewoken-6h
paperclip-inv missing-disposition-reparks
paperclip-inv unassigned-infocus-cards
paperclip-inv db-pool-waiters
INVARMS
inv_bad=""
for arm in "${INV_LIST[@]}"; do
  [[ "$(ro_fire "$arm")" == "ALLOWED:1" ]] || inv_bad="$inv_bad [allowed-arm failed: $arm]"
done
[[ "$(ro_fire 'paperclip-inv')" == "REFUSED" ]] || inv_bad="$inv_bad [bare paperclip-inv admitted]"
[[ "$(ro_fire 'paperclip-inv anything-else')" == "REFUSED" ]] || inv_bad="$inv_bad [unlisted key admitted]"
[[ "$(ro_fire 'paperclip-inv agents-error-10m --follow')" == "REFUSED" ]] || inv_bad="$inv_bad [extra flag admitted]"
[[ "$(ro_fire 'paperclip-inv agents-error-10m; evil')" == "REFUSED" ]] || inv_bad="$inv_bad [chaining admitted]"
# argv proof, looped over every arm so a widened exec line on any arm goes
# red (a single-arm assertion leaves the others guarded only by ALLOWED:1).
for arm in "${INV_LIST[@]}"; do
  key="${arm#paperclip-inv }"
  ro_fire "$arm" >/dev/null
  iargv="$(cat "$WORK/ro-argv.log")"
  [[ "$iargv" == "paperclip-inv-count.sh|$key" ]] \
    || inv_bad="$inv_bad [inv argv wrong ($key): $iargv]"
done
[[ -z "$inv_bad" ]] \
  && ok "7l. invariant-count: all ${#INV_LIST[@]} arms dispatch once with clean argv, others refused" \
  || bad "7l. invariant-count dispatch" "$inv_bad"
# 7l2. helper contract (paperclip-inv-count.sh): read-only count, fail
# closed, never prints the database URL. Stub psql records argv.
mkdir -p "$WORK/psqlbin"
cat > "$WORK/psqlbin/psql" <<PSQL
#!/usr/bin/env bash
# mode via PSQL_FIXTURE: ok prints a padded count (proves trimming),
# fail refuses (proves fail-closed). Unquoted heredoc like the suite's HTTP
# shim: \$WORK bakes in at creation, runtime vars stay escaped.
printf '%s\n' "\$*" >> "$WORK/psql-argv.log"
printf 'PGOPTIONS=%s\n' "\${PGOPTIONS:-<unset>}" >> "$WORK/psql-argv.log"
case "\${PSQL_FIXTURE:-ok}" in
  ok) echo " 7" ;;
  # fail echoes the connection URL the way libpq does on some parse errors
  # (e.g. malformed password): a helper that prints psql's stderr leaks it.
  fail) echo "psql: invalid percent-encoded token in connection string: postgres://inv-sentinel-42/db" >&2; exit 1 ;;
esac
PSQL
chmod +x "$WORK/psqlbin/psql"
inv_bad2=""
: > "$WORK/psql-argv.log"
if out="$(PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PATH="$WORK/psqlbin:/usr/bin:/bin" bash "$SRC/paperclip-inv-count.sh" agents-error-10m 2>&1)"; then
  [[ "$out" == "agents-error-10m: 7" ]] || inv_bad2="$inv_bad2 [helper output wrong: $out]"
else inv_bad2="$inv_bad2 [helper refused a healthy probe: $out]"
fi
grep -qF "SELECT count(*) FROM paperclip_inv_agents_error_10m;" "$WORK/psql-argv.log" \
  || inv_bad2="$inv_bad2 [psql statement wrong: $(tr '\n' ' ' < "$WORK/psql-argv.log")]"
grep -qF "PGOPTIONS=-c default_transaction_read_only=on" "$WORK/psql-argv.log" \
  || inv_bad2="$inv_bad2 [read-only PGOPTIONS missing]"
if grep -qF "inv-sentinel-42" <<<"$out"; then inv_bad2="$inv_bad2 [helper printed the database URL]"; fi
# every key maps to its own view (looped: a swapped mapping on any key reds).
declare -A INV_VIEWS=(
  [agents-error-10m]=paperclip_inv_agents_error_10m
  [failed-subtype-success]=paperclip_inv_failed_subtype_success
  [cards-rewoken-6h]=paperclip_inv_cards_rewoken_6h
  [missing-disposition-reparks]=paperclip_inv_missing_disposition_reparks
  [unassigned-infocus-cards]=paperclip_inv_unassigned_infocus_cards
  [db-pool-waiters]=paperclip_inv_db_pool_waiters
)
for key in "${!INV_VIEWS[@]}"; do
  : > "$WORK/psql-argv.log"
  PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PATH="$WORK/psqlbin:/usr/bin:/bin" \
    bash "$SRC/paperclip-inv-count.sh" "$key" >/dev/null 2>&1
  grep -qF "SELECT count(*) FROM ${INV_VIEWS[$key]};" "$WORK/psql-argv.log" \
    || inv_bad2="$inv_bad2 [key-view mapping wrong ($key)]"
done
# fail closed: no URL anywhere → exit 2, zero psql calls.
: > "$WORK/psql-argv.log"
out2="$(PAPERCLIP_INV_ENV_FILE=/dev/null PATH="$WORK/psqlbin:/usr/bin:/bin" env -u PAPERCLIP_RO_DB_URL bash "$SRC/paperclip-inv-count.sh" agents-error-10m 2>&1)"; rc2=$?
if (( rc2 == 2 )); then
  [[ -s "$WORK/psql-argv.log" ]] && inv_bad2="$inv_bad2 [refused run called psql]"
else inv_bad2="$inv_bad2 [missing URL admitted (rc=$rc2): $out2]"
fi
# fail closed: probe failure pages (exit 1) naming the view, not the URL.
: > "$WORK/psql-argv.log"
out3="$(PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PSQL_FIXTURE=fail PATH="$WORK/psqlbin:/usr/bin:/bin" bash "$SRC/paperclip-inv-count.sh" agents-error-10m 2>&1)"; rc3=$?
(( rc3 == 1 )) || inv_bad2="$inv_bad2 [probe failure wrong exit (rc=$rc3): $out3]"
if grep -qF "inv-sentinel-42" <<<"$out3"; then inv_bad2="$inv_bad2 [probe failure printed the URL]"; fi
grep -qF "paperclip_inv_agents_error_10m" <<<"$out3" \
  || inv_bad2="$inv_bad2 [probe failure hides the view: $out3]"
# unlisted keys stay refused even when shaped like shell.
out4="$(PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PATH="$WORK/psqlbin:/usr/bin:/bin" bash "$SRC/paperclip-inv-count.sh" 'anything-else; evil' 2>&1)"; rc4=$?
(( rc4 == 2 )) || inv_bad2="$inv_bad2 [unlisted key admitted (rc=$rc4): $out4]"
[[ -z "$inv_bad2" ]] \
  && ok "7l2. helper: read-only count per key, fail-closed, URL never printed" \
  || bad "7l2. helper contract" "$inv_bad2"
# 7l3. installed copy works outside the repo tree (case_423f3282 finding 1,
# case_fe0bd527 finding 1): OPERATOR.md copies the helper to /usr/local/bin,
# where the repo-relative views lookup breaks and the fixed deployed
# checkout ($DEPLOY_ROOT/ops/case_654dd2ec/...) must serve. The outside-tree
# copy runs WITHOUT PAPERCLIP_INV_VIEWS_FILE against a fake deploy root,
# so the deployed-fallback branch is what prints `key: N`. The default
# literal is pinned to the OPERATOR.md deploy root, and an empty deploy
# root must refuse — together these kill a `/nonexistent` mutant of the
# fallback literal. The OPERATOR prove block must exercise one paperclip-inv
# arm.
inv_bad3=""
mkdir -p "$WORK/outside/bin" "$WORK/fake-deploy/ops/case_654dd2ec"
cp "$SRC/paperclip-inv-count.sh" "$WORK/outside/bin/paperclip-inv-count.sh"
cp "$SRC/../case_654dd2ec/paperclip_invariants.sql" "$WORK/fake-deploy/ops/case_654dd2ec/paperclip_invariants.sql"
# pin: the fallback default is the OPERATOR.md deploy root.
grep -qF 'PAPERCLIP_INV_DEPLOY_ROOT:-/opt/status-page/current' "$SRC/paperclip-inv-count.sh" \
  || inv_bad3="$inv_bad3 [fallback default is not the OPERATOR deploy root /opt/status-page/current]"
grep -qF '/opt/status-page/current' "$SRC/OPERATOR.md" \
  || inv_bad3="$inv_bad3 [OPERATOR.md deploy root moved; helper default is stale]"
: > "$WORK/psql-argv.log"
if out5="$(PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_INV_DEPLOY_ROOT="$WORK/fake-deploy" PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PATH="$WORK/psqlbin:/usr/bin:/bin" bash "$WORK/outside/bin/paperclip-inv-count.sh" agents-error-10m 2>&1)"; then
  [[ "$out5" == "agents-error-10m: 7" ]] || inv_bad3="$inv_bad3 [outside-tree copy output wrong: $out5]"
else inv_bad3="$inv_bad3 [outside-tree copy refused a healthy probe: $out5]"
fi
if grep -qF "inv-sentinel-42" <<<"$out5"; then inv_bad3="$inv_bad3 [outside-tree copy printed the database URL]"; fi
# empty deploy root + no repo-relative file: must refuse (exit 2), proving
# the fake-deploy success above came from the fallback branch.
mkdir -p "$WORK/empty-deploy"
out6=""; rc6=0
out6="$(PAPERCLIP_INV_ENV_FILE=/dev/null PAPERCLIP_INV_DEPLOY_ROOT="$WORK/empty-deploy" PAPERCLIP_RO_DB_URL="postgres://inv-sentinel-42/db" PATH="$WORK/psqlbin:/usr/bin:/bin" bash "$WORK/outside/bin/paperclip-inv-count.sh" agents-error-10m 2>&1)" || rc6=$?
(( rc6 == 2 )) || inv_bad3="$inv_bad3 [empty deploy root admitted (rc=$rc6): $out6]"
grep -qF "paperclip-inv agents-error-10m" "$SRC/OPERATOR.md" \
  || inv_bad3="$inv_bad3 [OPERATOR.md prove block misses a paperclip-inv arm]"
[[ -z "$inv_bad3" ]] \
  && ok "7l3. installed copy works outside the tree, OPERATOR proves one arm" \
  || bad "7l3. outside-tree helper contract" "$inv_bad3"
# 7k. phase 1 admits exactly one SSH host under forced command (not zero,
# not open). Multi-line prose: join continuation lines before matching.
if grep -qiE 'ONLY SSH|exactly one SSH|one SSH host' <<<"$(tr '\n' ' ' < "$SRC/OPERATOR.md")" \
  && grep -qiE 'exactly one SSH|ONLY SSH|single .*host' <<<"$(tr '\n' ' ' < "$SRC/akmatori-notes.md")" \
  && grep -qiE 'no fix proposal is approved' "$SRC/OPERATOR.md"; then
  ok "7k. phase-1 SSH posture is one forced-command host, not a label"
else bad "7k. phase-1 boundary" "one-host/forced-command wording missing"; fi
# positive control: zero-host wording must fail 7k (the old contradiction).
mutk="$WORK/zerohost.md"; sed 's/ONLY SSH/NO SSH/' "$SRC/OPERATOR.md" > "$mutk"
grep -qiE 'ONLY SSH|exactly one SSH|one SSH host' <<<"$(tr '\n' ' ' < "$mutk")" \
  && bad "7k2. zero-host mutant" "mutant still reads one-host" \
  || ok "7k2. zero-host mutant goes red"

# === 8. Source-only flap-rate guard (also runs as the dedicated ===
# === `flap guard suite` CI step, case_e8e49a02, for failure localisation) ===
# Account for the Python suite in FAIL; a red classifier must not be hidden by
# the shell checks that precede it or by the final summary's successful echo.
if python3 "$HERE/test_flap_guard.py"; then
  ok "8. flap guard regressions and mutation controls"
else
  bad "8. flap guard regressions and mutation controls" "Python suite failed"
fi

# === 9. ALERT DESCRIPTIONS CARRY THRESHOLD+OWNER+RUNBOOK (case_9b48cd0a) ============
# Every alert description in gatus-config.yaml (7 pull + 10 external endpoints,
# discord + custom each) names what breached (THRESHOLD), who owns the next
# step (OWNER director|devops|automation) and where the triage lives (RUNBOOK
# path), mirroring the ops/case_654dd2ec/gatus-endpoints.yaml pattern so the page
# itself says what breached and who owns it.
desc_lines="$(grep -cE '^\s*description: "' "$CFG")"
if [[ "$desc_lines" == "36" ]]; then ok "9. thirty-six alert descriptions present (17 endpoints x discord+custom, plus the ci-queue-age escalation pair)"
else bad "9. description count" "found $desc_lines, want 36"; fi
missing_tor="$(grep -E '^\s*description: "' "$CFG" | grep -v 'THRESHOLD' || true)"
if [[ -z "$missing_tor" ]]; then ok "9b. every description carries THRESHOLD"; else bad "9b. THRESHOLD missing" "$missing_tor"; fi
missing_owner="$(grep -E '^\s*description: "' "$CFG" | grep -vE 'OWNER (director|devops|automation)' || true)"
if [[ -z "$missing_owner" ]]; then ok "9c. every description carries OWNER (director|devops|automation)"; else bad "9c. OWNER missing" "$missing_owner"; fi
missing_rb="$(grep -E '^\s*description: "' "$CFG" | grep -v 'RUNBOOK [A-Za-z0-9_/.-]*\.md' || true)"
if [[ -z "$missing_rb" ]]; then ok "9d. every description carries RUNBOOK <path>.md"; else bad "9d. RUNBOOK missing" "$missing_rb"; fi
# every RUNBOOK path must resolve to a file in the repo (no dead pointers).
dead_rb="$(grep -oE 'RUNBOOK [A-Za-z0-9_/.-]*\.md' "$CFG" | awk '{print $2}' | sort -u | while read -r rb; do [[ -f "$HERE/$rb" ]] || echo "$rb"; done)"
if [[ -z "$dead_rb" ]]; then ok "9e. every RUNBOOK path resolves to a repo file"; else bad "9e. dead RUNBOOK pointer" "$dead_rb"; fi
# positive control: a description stripped of its OWNER must go red.
mut8="$WORK/noowner.yaml"
sed '0,/OWNER devops\./s//OWNER-STRIPPED./' "$CFG" > "$mut8"
if grep -E '^\s*description: "' "$mut8" | grep -vE 'OWNER (director|devops|automation)' >/dev/null; then
  ok "9f. owner-stripped mutant goes red"
else bad "9f. owner-stripped mutant" "mutant still reads clean"; fi

# === EXIT GATE (last line: the script's status is the suite's status) ============
# No `set -e` here by design (every check must run even after a FAIL), so the
# exit gate belongs at the very end — a mid-file `(( FAIL == 0 ))` leaves the
# status to whatever line runs last and lets sections 1–6 FAIL green.
echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
