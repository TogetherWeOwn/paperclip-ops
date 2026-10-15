#!/usr/bin/env bash
# ===========================================================================
# test_case_d376bf4e_host_checks.sh — offline regression suite for the case_4ea7d92e
# host-cron audit wrappers (ops/case_4ea7d92e/).
#
# These three wrappers run unattended on the host under systemd --user with a
# long-lived agent API key. Nothing else in CI exercises them: before this
# suite they were covered only by the blanket "Syntax check every script"
# step, which is why a dead findings path (case_8891d115) survived review.
#
# Contracts, each with a positive control that proves the assertion can
# still fail:
#   1. FINDINGS-ONLY  — a clean detector makes ZERO API calls.
#   2. FAIL-CLOSED    — a detector that exits 1 or 2, or is missing entirely,
#                       posts a card. Never silence.
#   3. COALESCING     — an already-open card with the same tag gets a comment,
#                       not a second card.
#   4. NON-DISCLOSURE — neither the API key nor the request body ever reaches
#                       curl's argv (/proc/<pid>/cmdline is world-readable).
#   5. DELIVERY       — a non-2xx from the API is never reported as a posted
#                       finding; the wrapper fails loudly instead.
#
# Plus the host-specific contracts from the node7b1ff338 handback:
#   6. DETECTION ONLY — the git sweep never runs the revoking remediator, and
#                       its card does not tell anyone to.
#   7. READ-ONLY      — the watchdog only runs `systemctl --user show`; it never
#                       restarts the unit or drives sweep_daemon.sh.
#   8. REAL LAYOUT    — the checked-out git wrapper reaches the real detector
#                       at the repo root and scans the roots it is given.
#
# ALL THREE wrappers are executed. The first cut of this suite ran only
# git_remote_credential_sweep_check.sh, and that gap is exactly why two more
# fail-open sites and an inert guard survived it (case_8928d14e B1/B2/N1).
#
# Hermetic: no network beyond 127.0.0.1, no credentials, no real detectors.
# A stub API server records what was called; a curl shim records argv.
# ===========================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SRC="${REPO_ROOT}/ops/case_4ea7d92e"
PORT="${case_d376bf4e_TEST_PORT:-8791}"
SENTINEL_KEY="SENTINELKEY-must-never-reach-argv"

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -d "$SRC" ]] || { echo "REFUSE: ${SRC} not found — suite cannot attribute a result"; exit 3; }

# --- stub API ------------------------------------------------------------
# Records every request to $WORK/calls.jsonl. `seed_open_card` controls
# whether the coalescing lookup finds an existing open card.
cat > "$WORK/api.py" <<'PY'
import http.server, json, os, sys
CALLS = os.environ["CALLS_FILE"]
SEED  = os.environ.get("SEED_OPEN_CARD", "")
STATUS = int(os.environ.get("HTTP_STATUS", "200"))
class H(http.server.BaseHTTPRequestHandler):
    def _h(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        with open(CALLS, "a") as f:
            f.write(json.dumps({"m": self.command, "p": self.path,
                                "auth": self.headers.get("Authorization"),
                                "body": body}) + "\n")
        self.send_response(STATUS)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.command == "GET":
            items = ([{"id": "existing-card-id", "title": SEED}] if SEED else [])
            self.wfile.write(json.dumps(items).encode())
        else:
            self.wfile.write(b'{"id":"created-card-id"}')
    do_GET = _h
    do_POST = _h
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY

# --- curl shim -----------------------------------------------------------
# Records its own argv, then execs the real curl. This is how contract 4 is
# observed: whatever lands here is what a `ps` on the host would show.
REAL_CURL="$(command -v curl)"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<SHIM
#!/usr/bin/env bash
printf '%s\0' "\$@" >> "${WORK}/argv.bin"
exec "${REAL_CURL}" "\$@"
SHIM
chmod +x "$WORK/bin/curl"

# --- sandbox: a fake repo root holding the wrappers + stub detectors ------
# build_sandbox <name>; then write stub detectors into $SBX/
# Every sandbox also gets a scan root holding one clean config: run_sweep
# always passes explicit roots, so without it the unset-roots (N1) or
# empty-root (B2) guards would fire and each sweep test would assert the
# wrong scenario.
build_sandbox() {
  SBX="$WORK/$1"
  rm -rf "$SBX"; mkdir -p "$SBX"
  cp -r "$SRC" "$SBX/case_4ea7d92e-dir"
  mkdir -p "$SBX/ops"
  mv "$SBX/case_4ea7d92e-dir" "$SBX/ops/case_4ea7d92e"
  mkdir -p "$SBX/ws/repo/.git"
  printf '[remote "origin"]\n\turl = https://person-f431cd1b@example.invalid/o/r.git\n' > "$SBX/ws/repo/.git/config"
}

stub_detector() { # stub_detector <path> <exit-code> <stdout-line>
  cat > "$1" <<EOF
#!/usr/bin/env bash
echo "$3"
exit $2
EOF
  chmod +x "$1"
}

start_api() { # start_api [seed-open-card-title] [http-status]
  : > "$WORK/calls.jsonl"; : > "$WORK/argv.bin"
  CALLS_FILE="$WORK/calls.jsonl" SEED_OPEN_CARD="${1:-}" HTTP_STATUS="${2:-200}" \
    python3 "$WORK/api.py" "$PORT" & SRV_PID=$!
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
    sleep 0.1
  done
}
stop_api() { [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null; SRV_PID=""; wait 2>/dev/null || true; }

run_wrapper() { # run_wrapper <sandbox-dir> <wrapper.sh> [EXTRA=env ...] -> prints exit code
  local sbx="$1" wrapper="$2"; shift 2
  local rc=0
  # `env -u BASH_ENV` is load-bearing, not tidiness. Where BASH_ENV is set
  # (Paperclip agent sandboxes set it to a runtime .bashrc), every
  # non-interactive bash sources that file, and it REWRITES PATH -- so the
  # curl shim below silently never runs and contract 4 would assert nothing.
  # The `-s argv.bin` refusal further down is the backstop if this regresses.
  env -u BASH_ENV \
    PATH="$WORK/bin:$PATH" \
    PAPERCLIP_API_URL="http://127.0.0.1:$PORT" \
    PAPERCLIP_AGENT_API_KEY="$SENTINEL_KEY" \
    PAPERCLIP_COMPANY_ID="test-company" \
    PAPERCLIP_ASSIGNEE_AGENT_ID="test-agent" \
    case_d376bf4e_OPS_DIR="$sbx/ops" \
    case_d376bf4e_TRANSCRIPT_ROOT="$sbx/transcripts" \
    "$@" \
    bash "$sbx/ops/case_4ea7d92e/${wrapper}" >/dev/null 2>"$WORK/stderr.txt" || rc=$?
  echo "$rc"
}

run_sweep() { run_wrapper "$1" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$1/ws"; }

stderr_txt() { cat "$WORK/stderr.txt"; }

# Stub surroundings for the watchdog, which only ever asks systemd. The shim
# answers `systemctl --user show -p P ... -- UNIT` from STUB_TIMER_<P> /
# STUB_SVC_<P>, and logs every invocation so the read-only contract can be
# asserted: anything other than `--user show` is a hit.
SYSBIN="$WORK/sysbin"
mkdir -p "$SYSBIN"
cat > "$SYSBIN/systemctl" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "${WORK}/systemctl.log"
if [[ "\${STUB_SYSTEMCTL_RC:-0}" != 0 ]]; then
  echo "Failed to connect to bus: No medium found" >&2
  exit "\$STUB_SYSTEMCTL_RC"
fi
: "\${STUB_TIMER_LoadState=loaded}" "\${STUB_TIMER_ActiveState=active}"
: "\${STUB_SVC_LoadState=loaded}" "\${STUB_SVC_ActiveState=inactive}"
: "\${STUB_SVC_Result=success}" "\${STUB_SVC_ExecMainStatus=0}"
: "\${STUB_SVC_ExecMainStartTimestamp=\$(LC_ALL=C date -u -d "@\$(( \$(date +%s) - 60 ))" '+%a %Y-%m-%d %H:%M:%S UTC')}"
verb="" unit="" props=()
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --user) shift ;;
    -p) props+=("\$2"); shift 2 ;;
    --) unit="\$2"; shift 2 ;;
    *) if [[ -z "\$verb" ]]; then verb="\$1"; else unit="\$1"; fi; shift ;;
  esac
done
[[ "\$verb" == show ]] || exit 0
case "\$unit" in *.timer) kind=TIMER ;; *) kind=SVC ;; esac
for p in "\${props[@]}"; do
  var="STUB_\${kind}_\${p}"
  printf '%s=%s\n' "\$p" "\${!var-}"
done
SHIM
chmod +x "$SYSBIN/systemctl"

# A PATH with everything the watchdog and post_finding.sh use EXCEPT
# systemctl, so "systemctl missing" is testable on a runner that has one.
MINBIN="$WORK/minbin"
mkdir -p "$MINBIN"
for t in bash dirname date grep head mktemp rm python3 cat; do
  ln -sf "$(command -v "$t")" "$MINBIN/$t"
done
cp "$WORK/bin/curl" "$MINBIN/curl"

# sweep_daemon.sh is a tripwire now: the watchdog must never drive it.
stub_sweeper_deps() { # stub_sweeper_deps <sandbox>
  mkdir -p "$1/ops/case_bd594600"
  cat > "$1/ops/case_bd594600/sweep_daemon.sh" <<D
#!/usr/bin/env bash
echo "\$*" >> "${WORK}/sweep_daemon.called"
exit 0
D
  chmod +x "$1/ops/case_bd594600/sweep_daemon.sh"
  : > "$WORK/systemctl.log"; rm -f "$WORK/sweep_daemon.called"
}

run_watchdog() { # run_watchdog <sandbox> [EXTRA=env ...]
  local sbx="$1"; shift
  run_wrapper "$sbx" sweeper_watchdog_check.sh PATH="$SYSBIN:$WORK/bin:$PATH" "$@"
}

# systemctl invocations other than a read-only `--user show`.
systemctl_writes() { grep -vE '^--user show( |$)' "$WORK/systemctl.log" || true; }

posted_bodies() { python3 -c '
import json,sys
for l in open(sys.argv[1]):
    d=json.loads(l)
    if d["m"]=="POST": print(d["body"])
' "$WORK/calls.jsonl"; }

stub_transcript_deps() { # stub_transcript_deps <sandbox> <exit-code>
  mkdir -p "$1/ops/case_d9917646" "$1/transcripts"
  stub_detector "$1/ops/case_d9917646/transcript-mode-audit.sh" "$2" "sidecars=12 drifted=3"
}

calls() { wc -l < "$WORK/calls.jsonl" | tr -d ' '; }
methods_paths() { python3 -c '
import json,sys
for l in open(sys.argv[1]):
    d=json.loads(l); print(d["m"], d["p"].split("?")[0])
' "$WORK/calls.jsonl"; }

echo "case_4ea7d92e host-check contracts"

# === 1. FINDINGS-ONLY ====================================================
build_sandbox clean
stub_detector "$SBX/git_remote_credential_scan.sh" 0 "CLEAN"
start_api
rc="$(run_sweep "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "clean detector: wrapper exits 0 and makes 0 API calls" \
  || bad "clean detector should be silent (exit=$rc calls=$n)"

# === 2. FAIL-CLOSED ======================================================
for spec in "1:findings" "2:indeterminate"; do
  code="${spec%%:*}"; label="${spec##*:}"
  build_sandbox "exit$code"
  stub_detector "$SBX/git_remote_credential_scan.sh" "$code" \
    "FINDING repo=/fake config_line=7 kind=gh-app-installation-token host=github.com perms=x"
  start_api
  rc="$(run_sweep "$SBX")"
  n="$(calls)"
  stop_api
  [[ "$rc" == "0" && "$n" -ge 2 ]] \
    && ok "detector exit $code ($label): a card is posted (exit=$rc calls=$n)" \
    || bad "detector exit $code ($label) must post, not die (exit=$rc calls=$n)"
done

# A detector that is missing entirely is indeterminate, not clean.
build_sandbox missing
rm -f "$SBX/git_remote_credential_scan.sh"
start_api
rc="$(run_sweep "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] \
  && ok "missing detector: reported as a finding, not silently clean" \
  || bad "missing detector must post (exit=$rc calls=$n)"

# Positive control: the fail-closed assertion above must be able to fail.
# Re-introduce exactly the case_8891d115 bug (rc read after an unguarded command
# substitution under set -e) in a COPY, and require the suite to notice.
build_sandbox mutant
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
python3 - "$SBX/ops/case_4ea7d92e/git_remote_credential_sweep_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
fixed = 'scan_rc=0\nscan_out="$("$SCAN_SCRIPT" ${roots[@]+"${roots[@]}"} 2>&1)" || scan_rc=$?\n'
buggy = 'scan_out="$("$SCAN_SCRIPT" ${roots[@]+"${roots[@]}"} 2>&1)"\nscan_rc=$?\n'
if fixed not in s:
    sys.stderr.write("REFUSE: guarded form not found — control cannot be built\n")
    sys.exit(3)
open(p, "w").write(s.replace(fixed, buggy, 1))
PY
start_api
rc="$(run_sweep "$SBX")"
n="$(calls)"
stop_api
[[ "$n" == "0" ]] \
  && ok "positive control: the case_8891d115 bug still produces 0 calls (suite is not vacuous)" \
  || bad "positive control did not reproduce the bug (calls=$n) — the fail-closed check may be vacuous"

# === 3. COALESCING =======================================================
build_sandbox coalesce
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
start_api "[case_d376bf4e-git-remote-credential-sweep] already open"
rc="$(run_sweep "$SBX")"
paths="$(methods_paths)"
stop_api
if grep -q "POST /api/issues/existing-card-id/comments" <<<"$paths" \
   && ! grep -q "POST /api/companies/test-company/issues" <<<"$paths"; then
  ok "an open card with the same tag gets a comment, not a duplicate card"
else
  bad "coalescing failed; calls were: $(tr '\n' '|' <<<"$paths")"
fi

# Positive control: with no open card, it MUST create one — otherwise the
# assertion above would pass for a helper that never posts at all.
build_sandbox nocoalesce
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
start_api ""
rc="$(run_sweep "$SBX")"
paths="$(methods_paths)"
stop_api
grep -q "POST /api/companies/test-company/issues" <<<"$paths" \
  && ok "positive control: with no open card, a new card is created" \
  || bad "no-open-card path did not create; calls were: $(tr '\n' '|' <<<"$paths")"

# === 4. NON-DISCLOSURE ===================================================
# $WORK/argv.bin holds every argument every curl was invoked with, from the
# coalescing run above. Neither the key nor the finding body may appear.
build_sandbox argv
stub_detector "$SBX/git_remote_credential_scan.sh" 1 \
  "FINDING repo=/fake config_line=7 kind=gh-app-installation-token host=github.com perms=x"
start_api ""
rc="$(run_sweep "$SBX")"
stop_api
argv_txt="$(tr '\0' '\n' < "$WORK/argv.bin")"
[[ -s "$WORK/argv.bin" ]] || bad "REFUSE: curl shim recorded nothing — contract 4 unproven"
if [[ -s "$WORK/argv.bin" ]]; then
  grep -qF "$SENTINEL_KEY" <<<"$argv_txt" \
    && bad "the API key reached curl argv (/proc/<pid>/cmdline)" \
    || ok "the API key never reaches curl argv"
  grep -qF "gh-app-installation-token" <<<"$argv_txt" \
    && bad "the finding body reached curl argv" \
    || ok "the finding body never reaches curl argv"
  grep -q -- '--max-time' <<<"$argv_txt" \
    && ok "every board API call carries an explicit --max-time bound" \
    || bad "post_finding issued an unbounded HTTP call — a hung board endpoint would stall the wrapper"
  grep -q -- '--connect-timeout' <<<"$argv_txt" \
    && ok "every board API call carries an explicit --connect-timeout bound" \
    || bad "post_finding issued an HTTP call with no connect bound"
fi

# === 4c. TIMEOUT — a hung board endpoint fails loudly, never as posted (case_e107fefc)
# Swap the recording shim for a curl that dies with exit 28 (operation timed
# out — exactly what real curl returns once --max-time fires), then run a
# wrapper with a real finding. The open-card lookup cannot be read, so
# post_finding must refuse to post blind and the wrapper must exit non-zero.
build_sandbox timeout
stub_detector "$SBX/git_remote_credential_scan.sh" 1 \
  "FINDING repo=/fake config_line=7 kind=gh-app-installation-token host=github.com perms=x"
cp "$WORK/bin/curl" "$WORK/curl.bound"
cat > "$WORK/bin/curl" <<SHIM
#!/usr/bin/env bash
printf '%s\0' "\$@" >> "${WORK}/argv.bin"
echo "curl: (28) Operation timed out after 30001 milliseconds" >&2
exit 28
SHIM
chmod +x "$WORK/bin/curl"
: > "$WORK/argv.bin"
rc="$(run_sweep "$SBX")"
cp "$WORK/curl.bound" "$WORK/bin/curl"
[[ "$rc" != "0" ]] \
  && ok "a hung board endpoint fails the wrapper loudly (exit $rc)" \
  || bad "a hung board endpoint exited 0 — the finding reads as handled when nothing was posted"
grep -q 'NOT posting blind' "$WORK/stderr.txt" \
  && ok "the timeout refusal says the finding was NOT recorded" \
  || bad "no NOT-posting-blind refusal on stderr: $(cat "$WORK/stderr.txt")"
grep -q -- '--max-time' <(tr '\0' '\n' < "$WORK/argv.bin") \
  && ok "even the timed-out call carried --max-time" \
  || bad "the bound was not applied to the hung call"

# Positive control: the argv check must be able to see a leak. Invoke the
# real curl the old way and confirm the same grep fires.
: > "$WORK/argv.bin"
start_api ""
env -u BASH_ENV PATH="$WORK/bin:$PATH" curl -sS -o /dev/null \
  -H "Authorization: Bearer $SENTINEL_KEY" "http://127.0.0.1:$PORT/unused" 2>/dev/null || true
stop_api
grep -qF "$SENTINEL_KEY" <(tr '\0' '\n' < "$WORK/argv.bin") \
  && ok "positive control: an -H bearer on argv IS detected by this check" \
  || bad "positive control failed — the argv check cannot see a real leak"

# === 4b. GIT SWEEP: detection only, explicit roots, real layout ==========
# Revoking a credential is owner-reserved, so a finding must not run the
# revoke-then-scrub remediator. A stub at the path the old revision used
# leaves a marker if anything executes it.
stub_remediator() { # stub_remediator <sandbox>
  cat > "$1/gh_embedded_token_remediate.py" <<R
import sys
open("${WORK}/remediator.called", "a").write(" ".join(sys.argv[1:]) + "\n")
R
  chmod +x "$1/gh_embedded_token_remediate.py"
  rm -f "$WORK/remediator.called"
}

build_sandbox detect_only; stub_remediator "$SBX"
stub_detector "$SBX/git_remote_credential_scan.sh" 1 \
  "FINDING repo=/fake config_line=7 kind=gh-app-installation-token host=github.com perms=x"
start_api ""
rc="$(run_sweep "$SBX")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 && ! -e "$WORK/remediator.called" ]] \
  && ok "a finding is posted and the revoking remediator is NOT run" \
  || bad "detection-only broken (exit=$rc calls=$n remediator=$(cat "$WORK/remediator.called" 2>/dev/null))"
grep -q 'gh_embedded_token_remediate' <<<"$body" \
  && bad "the posted card still points at the revoking remediator" \
  || ok "the posted card does not tell anyone to run the revoking remediator"

# Positive control: the marker must fire if the wrapper calls the remediator.
build_sandbox detect_only_mutant; stub_remediator "$SBX"
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
python3 - "$SBX/ops/case_4ea7d92e/git_remote_credential_sweep_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
anchor = 'post_finding "$TAG" "embedded git remote credential" "$body_file"\n'
if anchor not in s:
    sys.stderr.write("REFUSE: findings post not found — control cannot be built\n")
    sys.exit(3)
open(p, "w").write(s.replace(anchor, 'python3 "${REPO_ROOT}/gh_embedded_token_remediate.py" --repo /fake || true\n' + anchor, 1))
PY
start_api ""
rc="$(run_sweep "$SBX")"
stop_api
[[ -e "$WORK/remediator.called" ]] \
  && ok "positive control: a remediator call from the wrapper IS caught" \
  || bad "positive control failed — the remediator tripwire cannot see a call"

# Roots: case_d376bf4e_GIT_SCAN_ROOTS reaches the detector as separate arguments.
build_sandbox roots
mkdir -p "$SBX/r1" "$SBX/r 2"
# Non-empty on purpose: an existing root holding no .git/config now posts
# (B2 regression below), which would drown this propagation assertion.
mkdir -p "$SBX/r1/.git" "$SBX/r 2/.git"
printf '[remote "origin"]\n' > "$SBX/r1/.git/config"
printf '[remote "origin"]\n' > "$SBX/r 2/.git/config"
cat > "$SBX/git_remote_credential_scan.sh" <<EOF2
#!/usr/bin/env bash
printf '%s\n' "\$@" > "${WORK}/scan.args"
echo "CLEAN"
exit 0
EOF2
chmod +x "$SBX/git_remote_credential_scan.sh"
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SBX/r1:$SBX/r 2")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" && "$(cat "$WORK/scan.args")" == "$SBX/r1"$'\n'"$SBX/r 2" ]] \
  && ok "case_d376bf4e_GIT_SCAN_ROOTS is passed to the detector root by root" \
  || bad "roots not propagated (exit=$rc calls=$n args=$(tr '\n' '|' < "$WORK/scan.args"))"

# A configured root that does not exist: the detector skips it quietly, so
# the wrapper must say so even when the rest of the scan is clean.
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SBX/r1:$SBX/no-such-root")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q 'no-such-root' <<<"$body" \
  && ok "a missing configured root is posted, not swallowed by a clean scan" \
  || bad "a missing root went unreported (exit=$rc calls=$n)"

# case_3cd9412c B2: an existing root holding no `.git/config` at all makes the
# detector print `CLEAN: scanned 0` — looked nowhere, not clean. The wrapper
# must post it like a missing root.
build_sandbox emptyroot
stub_detector "$SBX/git_remote_credential_scan.sh" 0 "CLEAN: scanned 0 git config(s)"
mkdir -p "$SBX/empty-ws"
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SBX/empty-ws")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q 'empty-ws' <<<"$body" \
  && ok "an existing root with no .git/config is posted, not read as clean" \
  || bad "an empty root went unreported (exit=$rc calls=$n)"

# Positive control: the same root holding one clean config stays silent, so
# the assertion above cannot be satisfied by a wrapper that always posts.
mkdir -p "$SBX/empty-ws/repo/.git"
printf '[remote "origin"]\n\turl = https://person-f431cd1b@example.invalid/o/r.git\n' > "$SBX/empty-ws/repo/.git/config"
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SBX/empty-ws")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "positive control: a root holding a clean config stays silent" \
  || bad "a non-empty clean root posted (exit=$rc calls=$n)"

# Positive control for the fail-open itself: neuter the empty-root check in a
# COPY (the pre-B2 shape) and the empty root must go back to 0 calls. If it
# still posts, this regression check is vacuous.
build_sandbox emptyroot_mutant
stub_detector "$SBX/git_remote_credential_scan.sh" 0 "CLEAN: scanned 0 git config(s)"
mkdir -p "$SBX/empty-ws"
python3 - "$SBX/ops/case_4ea7d92e/git_remote_credential_sweep_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
fixed = "  elif ! find \"$r\" -type f -path '*/.git/config' -print -quit 2>/dev/null | grep -q .; then\n"
buggy = "  elif false; then\n"
if fixed not in s:
    sys.stderr.write("REFUSE: empty-root check not found — control cannot be built\n")
    sys.exit(3)
open(p, "w").write(s.replace(fixed, buggy, 1))
PY
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SBX/empty-ws")"
n="$(calls)"
stop_api
[[ "$n" == "0" ]] \
  && ok "positive control: without the empty-root check the empty root is silent (suite is not vacuous)" \
  || bad "positive control did not reproduce the fail-open (calls=$n)"

# case_3cd9412c N1: case_d376bf4e_GIT_SCAN_ROOTS unset entirely. Without configured
# roots the real detector falls back to container paths that do not exist on
# the host — another "looked nowhere" clean. Must post.
build_sandbox noroots
stub_detector "$SBX/git_remote_credential_scan.sh" 0 "CLEAN"
start_api
rc="$(run_wrapper "$SBX" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS=)"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q 'case_d376bf4e_GIT_SCAN_ROOTS is unset or empty' <<<"$body" \
  && ok "unset case_d376bf4e_GIT_SCAN_ROOTS is posted, not left to container defaults" \
  || bad "unset roots went unreported (exit=$rc calls=$n)"

# INDET (an unreadable config) on an otherwise clean exit is not clean.
build_sandbox indet
stub_detector "$SBX/git_remote_credential_scan.sh" 0 "INDET repo=/fake reason=config-unreadable"
start_api
rc="$(run_sweep "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] \
  && ok "an INDET line on exit 0 is posted, not read as clean" \
  || bad "INDET on exit 0 was treated as clean (exit=$rc calls=$n)"

# Real layout: the wrapper as checked out, with the real detector at the repo
# root, against a seeded scratch root. This is the path the sandbox stubs
# above cannot see — an earlier revision looked for the detector under ops/
# and every stub sat there too. The decoy is assembled at run time so no
# token-shaped literal is committed.
SEED="$WORK/seed"
decoy="gh""s_$(printf 'Q%.0s' {1..36})"
mkdir -p "$SEED/ws/leaky/.git" "$SEED/ws/clean/.git"
printf '[remote "origin"]\n\turl = https://x-access-token:%s@github.com/o/r.git\n' "$decoy" > "$SEED/ws/leaky/.git/config"
printf '[remote "origin"]\n\turl = https://github.com/o/r.git\n' > "$SEED/ws/clean/.git/config"
start_api ""
rc="$(run_wrapper "$REPO_ROOT" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SEED/ws")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q "FINDING repo=$SEED/ws/leaky " <<<"$body" \
  && ok "real layout: the checked-out wrapper finds the seeded credential via the real detector" \
  || bad "real layout: no finding posted (exit=$rc calls=$n stderr: $(tr '\n' '|' < "$WORK/stderr.txt"))"
grep -qF "$decoy" "$WORK/calls.jsonl" "$WORK/argv.bin" \
  && bad "real layout: the credential value reached the API or curl argv" \
  || ok "real layout: the credential value never leaves the host"
grep -q "repo=$SEED/ws/clean " <<<"$body" \
  && bad "real layout: a username-only remote was reported" \
  || ok "real layout: the username-only remote is not reported"

rm -f "$SEED/ws/leaky/.git/config"
start_api
rc="$(run_wrapper "$REPO_ROOT" git_remote_credential_sweep_check.sh case_d376bf4e_GIT_SCAN_ROOTS="$SEED/ws")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "positive control: the same real layout without the credential is silent" \
  || bad "real layout posts with nothing to find (exit=$rc calls=$n)"

# === 5. SWEEPER WATCHDOG (second wrapper; read-only unit inspection) ======
# A healthy host: timer loaded+active, last service run succeeded a minute
# ago, no ALERT or GAP file. Nothing to say, so nothing may be said -- and
# nothing may be touched: only `systemctl --user show`, never sweep_daemon.sh.
build_sandbox wd_clean; stub_sweeper_deps "$SBX"
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "watchdog, healthy unit: exits 0 and makes 0 API calls" \
  || bad "watchdog must be silent on a healthy unit (exit=$rc calls=$n stderr: $(tr '\n' '|' < "$WORK/stderr.txt"))"
shows="$(grep -c '^--user show ' "$WORK/systemctl.log" || true)"
[[ "$shows" -ge 2 && -z "$(systemctl_writes)" && ! -e "$WORK/sweep_daemon.called" ]] \
  && ok "watchdog is read-only: ${shows} x 'systemctl --user show', no other verb, sweep_daemon.sh never run" \
  || bad "watchdog touched the host (shows=$shows writes=$(systemctl_writes | tr '\n' '|') daemon=$(cat "$WORK/sweep_daemon.called" 2>/dev/null | tr '\n' '|'))"

# Positive control: the read-only tripwire must see a restart if one is added.
build_sandbox wd_restart; stub_sweeper_deps "$SBX"
python3 - "$SBX/ops/case_4ea7d92e/sweeper_watchdog_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
anchor = 'if [[ ${#findings[@]} -eq 0 ]]; then\n'
if anchor not in s:
    sys.stderr.write("REFUSE: findings gate not found — control cannot be built\n")
    sys.exit(3)
open(p, "w").write(s.replace(anchor, 'systemctl --user restart "${UNIT}.service" || true\n"${SWEEP_DIR}/sweep_daemon.sh" start || true\n' + anchor, 1))
PY
start_api
rc="$(run_watchdog "$SBX")"
stop_api
[[ -n "$(systemctl_writes)" && -e "$WORK/sweep_daemon.called" ]] \
  && ok "positive control: an added restart IS caught by the read-only tripwire" \
  || bad "positive control failed — the read-only tripwire cannot see a restart"

# Every unhealthy state posts a card. One sandbox per scenario.
wd_case() { # wd_case <label> [EXTRA=env ...] -> asserts a card is posted
  local label="$1"; shift
  build_sandbox "wd_$(tr -c 'a-z0-9' '_' <<<"$label")"; stub_sweeper_deps "$SBX"
  start_api
  local rc n
  rc="$(run_watchdog "$SBX" "$@")"
  n="$(calls)"
  WD_BODY="$(posted_bodies)"
  stop_api
  [[ "$rc" == "0" && "$n" -ge 2 ]] \
    && ok "watchdog: ${label} posts a card" \
    || bad "watchdog: ${label} must post (exit=$rc calls=$n stderr: $(tr '\n' '|' < "$WORK/stderr.txt"))"
}
old_ts="$(LC_ALL=C date -u -d "@$(( $(date +%s) - 7200 ))" '+%a %Y-%m-%d %H:%M:%S UTC')"
wd_case "timer not loaded"          STUB_TIMER_LoadState=not-found
wd_case "timer inactive"            STUB_TIMER_ActiveState=inactive
wd_case "service not loaded"        STUB_SVC_LoadState=not-found
wd_case "last run failed"           STUB_SVC_Result=exit-code STUB_SVC_ExecMainStatus=1 STUB_SVC_ActiveState=failed
grep -q 'Result=exit-code ExecMainStatus=1' <<<"$WD_BODY" \
  && ok "the failed-run card names Result and ExecMainStatus" \
  || bad "the failed-run card does not name the unit result (body: $(head -c 300 <<<"$WD_BODY"))"
wd_case "nonzero status"            STUB_SVC_ExecMainStatus=2
wd_case "never run"                 STUB_SVC_ExecMainStartTimestamp=
wd_case "stale last run"            STUB_SVC_ExecMainStartTimestamp="$old_ts" case_d376bf4e_SWEEPER_MAX_AGE_SEC=3600
grep -q 'STALE' <<<"$WD_BODY" \
  && ok "the stale-run card says STALE" \
  || bad "the stale-run card does not say STALE (body: $(head -c 300 <<<"$WD_BODY"))"
wd_case "unparseable timestamp"     STUB_SVC_ExecMainStartTimestamp="not a date"
wd_case "non-numeric max age"       case_d376bf4e_SWEEPER_MAX_AGE_SEC=1h
wd_case "user manager unreachable"  STUB_SYSTEMCTL_RC=1

# Positive control for the stale case: the same 2h-old run under a 24h budget
# is healthy, so the card above came from the age check and nothing else.
build_sandbox wd_fresh_budget; stub_sweeper_deps "$SBX"
start_api
rc="$(run_watchdog "$SBX" STUB_SVC_ExecMainStartTimestamp="$old_ts" case_d376bf4e_SWEEPER_MAX_AGE_SEC=86400)"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "positive control: a 2h-old run within a 24h budget stays silent" \
  || bad "the freshness check fires regardless of budget (exit=$rc calls=$n)"

# Positive control for "user manager unreachable": unguard the first
# read_props — the case_8891d115 shape — and that case must go to 0 calls.
build_sandbox wd_mutant; stub_sweeper_deps "$SBX"
python3 - "$SBX/ops/case_4ea7d92e/sweeper_watchdog_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
fixed = 'read_props "${UNIT}.timer" LoadState ActiveState || rc=$?\n'
buggy = 'read_props "${UNIT}.timer" LoadState ActiveState\n  rc=$?\n'
if fixed not in s:
    sys.stderr.write("REFUSE: guarded read_props not found — control cannot be built\n")
    sys.exit(3)
open(p, "w").write(s.replace(fixed, buggy, 1))
PY
start_api
rc="$(run_watchdog "$SBX" STUB_SYSTEMCTL_RC=1)"
n="$(calls)"
stop_api
[[ "$n" == "0" ]] \
  && ok "positive control: an unguarded systemctl call reproduces the 0-call fail-open" \
  || bad "positive control did not reproduce the fail-open (calls=$n) — this check may be vacuous"

# systemctl absent from PATH entirely: indeterminate, never clean.
build_sandbox wd_nosystemctl; stub_sweeper_deps "$SBX"
start_api
rc="$(run_wrapper "$SBX" sweeper_watchdog_check.sh PATH="$MINBIN")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] \
  && ok "watchdog: systemctl missing from PATH posts a card" \
  || bad "watchdog: systemctl missing must post (exit=$rc calls=$n stderr: $(tr '\n' '|' < "$WORK/stderr.txt"))"

# ALERT / GAP files: named with their headline; the sweep output inside the
# alert file stays on the host.
build_sandbox wd_alert; stub_sweeper_deps "$SBX"
printf '# alert\n\n**3 of 9 session records COULD NOT BE READ**\n\nLast sweep output:\nALERTBODY-must-stay-on-host\n' \
  > "$SBX/ops/case_bd594600/SWEEP-ALERT.md"
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q 'COULD NOT BE READ' <<<"$body" \
  && ok "watchdog: SWEEP-ALERT.md posts a card naming the alert reason" \
  || bad "watchdog: SWEEP-ALERT.md not reported (exit=$rc calls=$n)"
grep -q 'ALERTBODY-must-stay-on-host' <<<"$body" \
  && bad "the sweep output inside SWEEP-ALERT.md reached the board" \
  || ok "the sweep output inside SWEEP-ALERT.md stays on the host"

build_sandbox wd_gap; stub_sweeper_deps "$SBX"
printf '# WATCHDOG WAS BLIND for 9h\n' > "$SBX/ops/case_bd594600/WATCHDOG-GAP.md"
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] \
  && ok "watchdog: WATCHDOG-GAP.md posts a card" \
  || bad "watchdog: WATCHDOG-GAP.md not reported (exit=$rc calls=$n)"

# case_3cd9412c B1: a missing or wrong case_d376bf4e_OPS_DIR leaves the alert dir
# absent. The unit can still be healthy, and without the dir check the run
# exits 0 with 0 calls — a SWEEP-ALERT the sweeper wrote would never be seen.
build_sandbox wd_nodir; stub_sweeper_deps "$SBX"
rm -rf "$SBX/ops/case_bd594600"
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] && grep -q 'case_bd594600' <<<"$body" \
  && ok "watchdog: a missing sweeper alert dir posts, not silent 0-call clean" \
  || bad "watchdog: missing alert dir went unreported (exit=$rc calls=$n)"

# Positive control: the same sandbox with the dir present stays silent, so
# the assertion above cannot be satisfied by a wrapper that always posts.
mkdir -p "$SBX/ops/case_bd594600"
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "positive control: a healthy unit with the alert dir present stays silent" \
  || bad "healthy unit with alert dir posted (exit=$rc calls=$n)"

# Positive control for the fail-open itself: restore the exact pre-B1 shape in
# a COPY (bare `[[ -f ... ]]` checks, no dir guard) and the missing dir must
# go back to 0 calls. If it still posts, this regression check is vacuous.
# (Neutralizing only the `if ! -d` branch is NOT a valid mutant here: the
# sibling `elif ! -r/! -x` branch also fires on a missing dir.)
build_sandbox wd_nodir_mutant; stub_sweeper_deps "$SBX"
rm -rf "$SBX/ops/case_bd594600"
python3 - "$SBX/ops/case_4ea7d92e/sweeper_watchdog_check.sh" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
start = 'if [[ ! -d "$SWEEP_DIR" ]]; then\n'
mid = 'else\n  if [[ -f "$GAP_FILE" ]]; then\n'
end = 'fi\n\nif [[ ${#findings[@]} -eq 0 ]]; then\n'
i = s.find(start)
j = s.find(mid, i) if i >= 0 else -1
k = s.find(end, j) if j >= 0 else -1
if i < 0 or j < 0 or k < 0:
    sys.stderr.write("REFUSE: alert-dir guard not found — control cannot be built\n")
    sys.exit(3)
s = s[:i] + s[j + len('else\n'):k] + s[k + len('fi\n\n'):]
open(p, "w").write(s)
PY
start_api
rc="$(run_watchdog "$SBX")"
n="$(calls)"
stop_api
[[ "$n" == "0" ]] \
  && ok "positive control: without the dir guard the missing dir is silent (suite is not vacuous)" \
  || bad "positive control did not reproduce the fail-open (calls=$n)"

# === 6. TRANSCRIPT MODE AUDIT (third wrapper) ============================
build_sandbox tr_clean; stub_transcript_deps "$SBX" 0
start_api
rc="$(run_wrapper "$SBX" transcript_mode_audit_check.sh)"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" == "0" ]] \
  && ok "transcript audit, clean: exits 0 and makes 0 API calls" \
  || bad "transcript audit must be silent when clean (exit=$rc calls=$n)"

for code in 1 2; do
  build_sandbox "tr_exit$code"; stub_transcript_deps "$SBX" "$code"
  start_api
  rc="$(run_wrapper "$SBX" transcript_mode_audit_check.sh)"
  n="$(calls)"
  stop_api
  [[ "$rc" == "0" && "$n" -ge 2 ]] \
    && ok "transcript audit exit $code: posts a card" \
    || bad "transcript audit exit $code must post (exit=$rc calls=$n)"
done

build_sandbox tr_missing; stub_transcript_deps "$SBX" 0
rm -f "$SBX/ops/case_d9917646/transcript-mode-audit.sh"
start_api
rc="$(run_wrapper "$SBX" transcript_mode_audit_check.sh)"
n="$(calls)"
stop_api
[[ "$rc" == "0" && "$n" -ge 2 ]] \
  && ok "transcript audit: a missing detector is a finding, not clean" \
  || bad "missing transcript detector must post (exit=$rc calls=$n)"

# A --fix that fails must be visible on the card, not swallowed.
build_sandbox tr_fixfail; stub_transcript_deps "$SBX" 1
cat > "$SBX/ops/case_d9917646/transcript-mode-audit.sh" <<'A'
#!/usr/bin/env bash
for a in "$@"; do [[ "$a" == --fix ]] && { echo "chmod refused"; exit 3; }; done
echo "sidecars=12 drifted=3"; exit 1
A
start_api
rc="$(run_wrapper "$SBX" transcript_mode_audit_check.sh)"
body="$(posted_bodies)"
stop_api
[[ "$rc" == "0" ]] && grep -q 'exit 3' <<<"$body" \
  && ok "transcript audit: a failing --fix is reported with its exit code" \
  || bad "a failing --fix was not reported (exit=$rc)"

# === 7. DELIVERY: a non-2xx is never reported as a posted finding ========
# `curl -sS` exits 0 on a 403, so without an explicit status check the helper
# announces success against an API that stored nothing (case_8928d14e B3) — and
# INSTALL.md §1's least-privilege check and §4's seeded cards become unfalsifiable.
build_sandbox deny
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
start_api "" 403
rc="$(run_sweep "$SBX")"
err="$(stderr_txt)"
stop_api
if [[ "$rc" != "0" ]] && ! grep -q "created new card" <<<"$err"; then
  ok "a 403 API fails the wrapper loudly instead of claiming a card was created"
else
  bad "a 403 was reported as a posted finding (exit=$rc stderr: $(tr '\n' '|' <<<"$err"))"
fi
grep -q "403" <<<"$err" \
  && ok "the failure names the HTTP status the API actually returned" \
  || bad "the delivery failure does not name the status (stderr: $(tr '\n' '|' <<<"$err"))"

# Positive control: the identical scenario over a 200 must succeed, so the
# two assertions above cannot be satisfied by a helper that always fails.
build_sandbox allow
stub_detector "$SBX/git_remote_credential_scan.sh" 1 "FINDING repo=/fake"
start_api "" 200
rc="$(run_sweep "$SBX")"
err="$(stderr_txt)"
stop_api
[[ "$rc" == "0" ]] && grep -q "created new card" <<<"$err" \
  && ok "positive control: over a 200 the same path posts and exits 0" \
  || bad "positive control failed — the delivery check may reject everything (exit=$rc)"

# === secret hygiene of the package itself ================================
# The shape the Offline job's Secret scan greps for. A decoy in a runbook is
# still a failure; keep the shape assembled at run time (INSTALL.md §4).
if git -C "$REPO_ROOT" grep -qIE '(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})' -- ops/case_4ea7d92e 2>/dev/null; then
  bad "a token-shaped literal is committed under ops/case_4ea7d92e"
else
  ok "no token-shaped literal under ops/case_4ea7d92e"
fi

# The credential must be named for the principal it actually is.
if grep -rq "PAPERCLIP_BOARD_API_KEY" "$SRC"; then
  bad "PAPERCLIP_BOARD_API_KEY survives — POST /agents/{id}/keys mints an AGENT key"
else
  ok "the credential is named PAPERCLIP_AGENT_API_KEY, matching its principal"
fi

echo
echo "passed=$pass failed=$fail"
[[ $fail -eq 0 ]]
