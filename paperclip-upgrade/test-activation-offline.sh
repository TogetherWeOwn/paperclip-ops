#!/usr/bin/env bash
# ===========================================================================
# paperclip-upgrade/test-activation-offline.sh — hermetic contract tests for
# the no-chown, exclusive-lock activation wrapper.
#
# Stubs docker and drain.sh; never touches a Docker daemon, a database, or a
# live host. Filesystem fixtures live under one mktemp dir (mode 0700).
# Exit: 0 all checks pass | 1 a check failed | 2 refused (bad environment)
# ===========================================================================
set -uo pipefail
umask 077

ME="${BASH_SOURCE[0]##*/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACT="$HERE/activation.sh"
REHARDEN="$HERE/post-restart-reharden.sh"
DRAIN="$HERE/drain.sh"
FAIL=0
CHECKS=0

for bin in bash jq sha256sum flock df awk stat; do
  command -v "$bin" >/dev/null 2>&1 || { printf 'REFUSED: %s: %s is missing\n' "$ME" "$bin" >&2; exit 2; }
done

ROOT="$(mktemp -d)"
trap '((FAIL == 0)) && rm -rf "$ROOT"' EXIT
mkdir -p -m 0700 "$ROOT/stub" "$ROOT/data/.local/bin" "$ROOT/data/plugin-packages-root" \
  "$ROOT/data/.cache/two-bot-next-bounded/slot-a" "$ROOT/lockdir" "$ROOT/bin"
: > "$ROOT/data/.gitconfig"
: > "$ROOT/data/.cache/two-bot-next-bounded/policy.json"
chmod 0755 "$ROOT/data/.local" "$ROOT/data/.local/bin" "$ROOT/data/plugin-packages-root" \
  "$ROOT/data/.cache/two-bot-next-bounded"
chmod 0644 "$ROOT/data/.gitconfig" "$ROOT/data/.cache/two-bot-next-bounded/policy.json"
chmod 0700 "$ROOT/lockdir"

say() { printf '%s\n' "$*"; }
pass() { CHECKS=$((CHECKS + 1)); say "ok: $*"; }
oops() { CHECKS=$((CHECKS + 1)); say "FAIL: $*"; FAIL=1; }
expect_ok() { # DESC -- CMD...
  local desc="$1"; shift; [[ "$1" == -- ]] && shift
  local out rc; out="$("$@" 2>&1)"; rc=$?
  if ((rc == 0)); then pass "$desc"; else oops "$desc (rc=$rc: $(head -c 300 <<<"$out"))"; fi
  LAST_OUT="$out"; LAST_RC=$rc
}
expect_refused() { # DESC NEEDLE -- CMD...
  local desc="$1" needle="$2"; shift 2; [[ "$1" == -- ]] && shift
  local out rc; out="$("$@" 2>&1)"; rc=$?
  if ((rc != 0)) && grep -qF -- "$needle" <<<"$out"; then
    pass "$desc"
  else
    oops "$desc (rc=$rc, want non-zero containing '$needle': $(head -c 300 <<<"$out"))"
  fi
  LAST_OUT="$out"; LAST_RC=$rc
}

# --- stub docker -------------------------------------------------------------
# Behaviour is driven by files in $STUB_DIR so one test can flip one fact.
STUB="$ROOT/stub"
export STUB_DIR="$STUB"
CAND_ID="sha256:$(printf 'a%.0s' $(seq 1 64))"
ROLL_ID="sha256:$(printf 'b%.0s' $(seq 1 64))"
COMMIT="$(printf 'c%.0s' $(seq 1 40))"
printf '%s\n' "$CAND_ID" > "$STUB/image_id"
printf '%s\n' "$ROLL_ID" > "$STUB/rollback_image_id"
printf '%s\n' "$ROLL_ID" > "$STUB/current_image_id"
printf '["/entrypoint.sh"]\n' > "$STUB/entrypoint_json"
printf '1000:1000\n' > "$STUB/image_user"
printf '%s\n' "$COMMIT" > "$STUB/image_revision"
: > "$STUB/writable_paths"
printf '#!/bin/sh\nexec node server.js\n' > "$STUB/entrypoint_file"
cat > "$STUB/compose.json" <<'JSON'
{"name":"example","services":{"server":{"image":"registry.example.invalid/app:candidate","container_name":"paperclip-example","user":"1000:1000","volumes":[{"type":"bind","source":"/host/data","target":"/paperclip"},{"type":"bind","source":"/host/app","target":"/app/data"}]}}}
JSON
cat > "$ROOT/bin/docker" <<'STUBDOCKER'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "docker $*" >> "$STUB_DIR/docker.log"
args=" $* "
if [[ "$args" == *" image inspect "* ]]; then
  last="${*: -1}"
  if [[ "$args" == *"{{.Id}}"* ]]; then
    if [[ "$last" == *rollback* ]]; then cat "$STUB_DIR/rollback_image_id"; else cat "$STUB_DIR/image_id"; fi
    exit 0
  fi
  printf '%s|%s|%s\n' "$(cat "$STUB_DIR/entrypoint_json")" "$(cat "$STUB_DIR/image_user")" "$(cat "$STUB_DIR/image_revision")"
  exit 0
fi
if [[ "$args" == *" compose "*" config "* ]]; then
  cf=""; prev=""
  for a in "$@"; do [[ "$prev" == -f ]] && cf="$a"; prev="$a"; done
  if [[ -n "$cf" && -f "$cf" ]] && grep -q rollback "$cf"; then
    sed 's|app:candidate|app:rollback|' "$STUB_DIR/compose.json"
  else
    cat "$STUB_DIR/compose.json"
  fi
  exit 0
fi
if [[ "$args" == *" run "* ]]; then cat "$STUB_DIR/entrypoint_file"; exit 0; fi
if [[ "$args" == *" inspect "* ]]; then
  if [[ "$args" == *"{{.Image}}"* ]]; then cat "$STUB_DIR/current_image_id"; exit 0; fi
  if [[ "$args" == *"{{.Config.User}}"* ]]; then cat "$STUB_DIR/image_user"; exit 0; fi
  printf '%s %s\n' "$(cat "$STUB_DIR/current_image_id")" "$(cat "$STUB_DIR/image_user")"; exit 0
fi
if [[ "$args" == *" exec "* ]]; then
  if [[ "$args" == *"/proc/1/status"* ]]; then
    if [[ -f "$STUB_DIR/pid1_status" ]]; then cat "$STUB_DIR/pid1_status";
    else printf 'Uid:\t1000\t1000\t1000\t1000\nUmask:\t0077\n'; fi
    exit 0
  fi
  # reharden write probes: writable only if the path is listed by the test
  while IFS= read -r p; do
    [[ -n "$p" && "$args" == *"$p"* ]] && exit 0
  done < "$STUB_DIR/writable_paths"
  exit 1
fi
printf 'stub docker: unhandled: %s\n' "$*" >&2; exit 99
STUBDOCKER
chmod +x "$ROOT/bin/docker"
cat > "$ROOT/bin/sudo" <<'STUBSUDO'
#!/usr/bin/env bash
printf '%s\n' "sudo $*" >> "$STUB_DIR/sudo.log"; exit 99
STUBSUDO
chmod +x "$ROOT/bin/sudo"
export PATH="$ROOT/bin:$PATH"
export DOCKER="$ROOT/bin/docker"

# --- stub drain.sh -------------------------------------------------------------
# Records argv, proves it inherited the wrapper's lock (env token + open fd),
# and simulates the marker state machine.
STUB_DRAIN="$ROOT/bin/stub-drain.sh"
export STUB_DRAIN_LOG="$ROOT/drain.log"
cat > "$STUB_DRAIN" <<'STUBDRAIN'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "drain $*" >> "$STUB_DRAIN_LOG"
cmd="${1:-}"; state=""
printf '%s\n' "$@" >> "$STUB_DRAIN_LOG.args"
while (($#)); do case "$1" in --state-dir) state="${2:-}"; shift 2;; *) shift;; esac; done
[[ -n "${PAPERCLIP_ACTIVATION_LOCK_PATH:-}" && -n "${PAPERCLIP_ACTIVATION_LOCK_TOKEN:-}" && -n "${PAPERCLIP_ACTIVATION_LOCK_FD:-}" ]] \
  || { echo "stub drain: lock env missing" >&2; exit 42; }
[[ -e "/proc/self/fd/$PAPERCLIP_ACTIVATION_LOCK_FD" ]] || { echo "stub drain: lock fd not inherited" >&2; exit 42; }
grep -qx "token=$PAPERCLIP_ACTIVATION_LOCK_TOKEN" "$PAPERCLIP_ACTIVATION_LOCK_PATH" \
  || { echo "stub drain: lock token mismatch" >&2; exit 42; }
printf '%s\n' "lock-proof-ok $cmd" >> "$STUB_DRAIN_LOG"
[[ -n "${STUB_DRAIN_SLEEP_SECS:-}" && "$cmd" == drain ]] && sleep "$STUB_DRAIN_SLEEP_SECS"
case "$cmd" in
  drain) mkdir -m 0700 "$state"; printf 'x\n' > "$state/drain-started"; printf 'x\n' > "$state/drain_requested_at" ;;
  wait) printf 'x\n' > "$state/drained_at" ;;
  backup) printf 'x\n' > "$state/backup_at" ;;
  restart)
    grep -q -- '--expect-file' "$STUB_DRAIN_LOG.args" || { echo "stub drain: restart lacks --expect-file" >&2; exit 43; }
    printf 'x\n' > "$state/restart_started_at"; printf 'x\n' > "$state/restarted_at"; printf 'img id\n' > "$state/restart-image" ;;
  verify)
    [[ -n "${STUB_DRAIN_VERIFY_FAIL:-}" ]] && { echo "stub drain: verify forced fail" >&2; exit 1; }
    printf 'x\n' > "$state/verified_at"; printf 'img id\n' > "$state/verified_image" ;;
  rollback) printf 'x\n' > "$state/verified_at"; printf 'x\n' > "$state/rolledback_at" ;;
  undrain|abort) printf 'x\n' > "$state/undrained_at" ;;
  *) echo "stub drain: unknown $cmd" >&2; exit 44 ;;
esac
exit 0
STUBDRAIN
chmod +x "$STUB_DRAIN"
export PAPERCLIP_ACTIVATION_DRAIN="$STUB_DRAIN"

# --- manifest fixtures ---------------------------------------------------------
printf 'services: candidate\n' > "$ROOT/compose.yaml"
printf 'services: rollback\n' > "$ROOT/compose.rollback.yaml"
COMPOSE_SHA="$(sha256sum < "$ROOT/compose.yaml" | cut -d' ' -f1)"
ROLLBACK_COMPOSE_SHA="$(sha256sum < "$ROOT/compose.rollback.yaml" | cut -d' ' -f1)"
ENTRY_SHA="$(sha256sum < "$STUB/entrypoint_file" | cut -d' ' -f1)"
MOUNTS_SHA="$(printf '/app/data\n/paperclip\n' | sha256sum | cut -d' ' -f1)"
FILE_SHA="$(printf 'd%.0s' $(seq 1 64))"
OWNER="$(id -u):$(id -g)"
printf '0.10 0.20 0.30 1/100 123\n' > "$ROOT/loadavg"
printf 'MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n' > "$ROOT/meminfo"
export PAPERCLIP_ACTIVATION_TEST_SEAMS=1
export PAPERCLIP_ACTIVATION_NOW_UTC="2026-01-01T03:00:00Z"
export PAPERCLIP_ACTIVATION_LOADAVG_FILE="$ROOT/loadavg"
export PAPERCLIP_ACTIVATION_MEMINFO_FILE="$ROOT/meminfo"

write_manifest() { # OUT [KEY=VALUE overrides...]
  local out="$1"; shift
  {
    printf 'SERVICE=server\n'
    printf 'SERVER_CONTAINER=paperclip-example\n'
    printf 'COMPOSE_FILE=%s\n' "$ROOT/compose.yaml"
    printf 'COMPOSE_SHA256=%s\n' "$COMPOSE_SHA"
    printf 'ROLLBACK_COMPOSE_SHA256=%s\n' "$ROLLBACK_COMPOSE_SHA"
    printf 'IMAGE=registry.example.invalid/app:candidate\n'
    printf 'EXPECT_IMAGE_ID=%s\n' "$CAND_ID"
    printf 'EXPECT_COMMIT=%s\n' "$COMMIT"
    printf 'ROLLBACK_IMAGE=registry.example.invalid/app:rollback\n'
    printf 'ROLLBACK_IMAGE_ID=%s\n' "$ROLL_ID"
    printf 'ENTRYPOINT_PATH=/entrypoint.sh\n'
    printf 'ENTRYPOINT_SHA256=%s\n' "$ENTRY_SHA"
    printf 'ROLLBACK_ENTRYPOINT_SHA256=%s\n' "$ENTRY_SHA"
    printf 'EXPECT_ENTRYPOINT_JSON=["/entrypoint.sh"]\n'
    printf 'EXPECT_USER=1000:1000\n'
    printf 'EXPECT_FILES=/app/server.js:%s\n' "$FILE_SHA"
    printf 'REQUIRE_MIGRATIONS=0280,0281\n'
    printf 'MOUNT_DESTINATIONS_SHA256=%s\n' "$MOUNTS_SHA"
    printf 'DEPLOY_LOCK_PATH=%s\n' "$ROOT/lockdir/service.deploy.lock"
    printf 'DATA_DIR=%s\n' "$ROOT/data"
    printf 'MAX_LOAD1_X100=400\n'
    printf 'MIN_MEM_AVAILABLE_KB=1048576\n'
    printf 'MIN_DISK_AVAIL_BYTES=1048576\n'
    printf 'BLOCKED_START_HOUR_UTC=8\n'
    printf 'BLOCKED_END_HOUR_UTC=18\n'
    printf 'REQUIRE_CARGO_POOL=1\n'
    printf 'EXPECT_PROTECTED_OWNER=%s\n' "$OWNER"
    printf 'STABLE_POLLS=4\n'
    printf 'POLL_SECS=1\n'
    printf 'MAX_WAIT_SECS=60\n'
    printf 'HEALTH_TIMEOUT_SECS=60\n'
    printf 'PRIVATE_OVERLAY_REQUIRED=0\n'
    printf 'BUILD_MARKER=EPIPE\n'
    for kv in "$@"; do printf '%s\n' "$kv"; done
  } > "$out"
  chmod 0600 "$out"
}
# Overrides must REPLACE a key; rebuild with sed for the negative cases.
manifest_with() { # OUT KEY VALUE
  write_manifest "$1"
  if grep -q "^$2=" "$1"; then
    sed -i "s|^$2=.*|$2=$3|" "$1"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
  chmod 0600 "$1"
}
MAN="$ROOT/activation.conf"
write_manifest "$MAN"

reset_logs() { : > "$STUB_DRAIN_LOG"; : > "$STUB_DRAIN_LOG.args"; : > "$STUB/docker.log"; }

# --- 1. parse + static no-chown contract --------------------------------------
for f in "$ACT" "$REHARDEN" "$DRAIN"; do
  if bash -n "$f" 2>/dev/null; then pass "parses: ${f##*/}"; else oops "does not parse: ${f##*/}"; fi
done
if grep -Ev '^[[:space:]]*#' "$REHARDEN" | grep -Eq '(^|[^A-Za-z_])(chown|chmod|sudo)([[:space:]]|$)'; then
  oops "reharden still reaches chown/chmod/sudo"
else
  pass "reharden reaches no chown/chmod/sudo (verify-only)"
fi
if grep -q 'require_activation_lock' "$DRAIN" && grep -q 'activation.sh' "$DRAIN"; then
  pass "drain.sh wires the shared-lock proof"
else
  oops "drain.sh does not wire the shared-lock proof"
fi

# --- 2. reharden verify-only behaviour -----------------------------------------
reharden_env() {
  env PAPERCLIP_DATA="$ROOT/data" PAPERCLIP_UPGRADE_EXPECT_OWNER="$OWNER" \
    PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL=1 PAPERCLIP_UPGRADE_SERVER_CONTAINER=paperclip-example \
    DOCKER="$ROOT/bin/docker" bash "$REHARDEN"
}
rm -f "$STUB/sudo.log"
expect_ok "reharden passes on a matching baseline" -- reharden_env
[[ ! -e "$STUB/sudo.log" ]] && pass "reharden never invoked sudo" || oops "reharden invoked sudo"
slot_mode_before="$(stat -c %a "$ROOT/data/.cache/two-bot-next-bounded/slot-a")"
chmod 0664 "$ROOT/data/.gitconfig"
expect_refused "reharden fails (not repairs) a group-writable protected path" "FAIL" -- reharden_env
[[ "$(stat -c %a "$ROOT/data/.gitconfig")" == 664 ]] \
  && pass "reharden left the bad mode untouched (no metadata repair)" \
  || oops "reharden changed the bad mode"
chmod 0644 "$ROOT/data/.gitconfig"
printf '/paperclip/.gitconfig\n' > "$STUB/writable_paths"
expect_refused "reharden fails when the agent uid can write a protected path" "FAIL" -- reharden_env
: > "$STUB/writable_paths"
mv "$ROOT/data/plugin-packages-root" "$ROOT/data/plugin-packages-root.gone"
expect_refused "reharden fails closed on a missing protected path" "FAIL" -- reharden_env
mv "$ROOT/data/plugin-packages-root.gone" "$ROOT/data/plugin-packages-root"
expect_refused "reharden fails closed on missing owner baseline" "REFUSED" -- \
  env PAPERCLIP_DATA="$ROOT/data" PAPERCLIP_UPGRADE_EXPECT_OWNER="" DOCKER="$ROOT/bin/docker" bash "$REHARDEN"
[[ "$(stat -c %a "$ROOT/data/.cache/two-bot-next-bounded/slot-a")" == "$slot_mode_before" ]] \
  && pass "cargo slot dir mode untouched by reharden" || oops "cargo slot dir mode changed"

# --- 3. preflight happy path + binding refusals --------------------------------
reset_logs
expect_ok "preflight passes on the pinned manifest" -- \
  bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
[[ ! -s "$STUB_DRAIN_LOG" ]] && pass "preflight never invoked drain.sh" || oops "preflight invoked drain.sh"

bad="$ROOT/bad.conf"
manifest_with "$bad" EXPECT_IMAGE_ID "sha256:$(printf 'e%.0s' $(seq 1 64))"
reset_logs
expect_refused "wrong candidate image id is refused" "image" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
[[ ! -s "$STUB_DRAIN_LOG" ]] && pass "image mismatch: drain.sh untouched" || oops "image mismatch reached drain.sh"

manifest_with "$bad" EXPECT_COMMIT "$(printf 'f%.0s' $(seq 1 40))"
expect_refused "wrong source commit (image revision label) is refused" "commit" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

manifest_with "$bad" COMPOSE_SHA256 "$(printf 'e%.0s' $(seq 1 64))"
expect_refused "wrong compose sha256 is refused" "compose" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

manifest_with "$bad" MOUNT_DESTINATIONS_SHA256 "$(printf 'e%.0s' $(seq 1 64))"
expect_refused "wrong mount-destination binding is refused" "mount" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

manifest_with "$bad" EXPECT_USER "0:0"
expect_refused "root service user is refused" "user" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

manifest_with "$bad" EXPECT_ENTRYPOINT_JSON '["/other.sh"]'
expect_refused "wrong entrypoint binding is refused" "entrypoint" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

printf '#!/bin/sh\nchown -R node:node /paperclip\nexec node server.js\n' > "$STUB/entrypoint_file"
chown_entry_sha="$(sha256sum < "$STUB/entrypoint_file" | cut -d' ' -f1)"
manifest_with "$bad" ENTRYPOINT_SHA256 "$chown_entry_sha"
expect_refused "entrypoint containing chown is refused even when its hash is pinned" "chown" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
printf '#!/bin/sh\nexec node server.js\n' > "$STUB/entrypoint_file"

manifest_with "$bad" ROLLBACK_IMAGE_ID "sha256:$(printf 'e%.0s' $(seq 1 64))"
expect_refused "rollback binding that is not the running image is refused" "rollback" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

manifest_with "$bad" UNKNOWN_KEY "x"
expect_refused "unknown manifest key is refused" "unknown" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

write_manifest "$bad"; grep -v '^MAX_LOAD1_X100=' "$bad" > "$bad.tmp" && mv "$bad.tmp" "$bad"; chmod 0600 "$bad"
expect_refused "missing pressure threshold is refused (no silent default)" "MAX_LOAD1_X100" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

# pressure + window evidence
printf 'garbage\n' > "$ROOT/loadavg"
expect_refused "malformed load evidence is refused" "load" -- \
  bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
printf '99.00 0.20 0.30 1/100 123\n' > "$ROOT/loadavg"
expect_refused "load over the manifest ceiling is refused" "load" -- \
  bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
printf '0.10 0.20 0.30 1/100 123\n' > "$ROOT/loadavg"
printf 'MemTotal:       16384000 kB\n' > "$ROOT/meminfo"
expect_refused "missing MemAvailable evidence is refused" "MemAvailable" -- \
  bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
printf 'MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n' > "$ROOT/meminfo"
manifest_with "$bad" MIN_DISK_AVAIL_BYTES "999999999999999999"
expect_refused "disk below the manifest floor is refused" "disk" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
expect_refused "inside the blocked UTC window is refused" "window" -- \
  env PAPERCLIP_ACTIVATION_NOW_UTC="2026-01-01T10:00:00Z" \
    bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
expect_refused "clock override without the test-seams flag is refused" "REFUSED" -- \
  env -u PAPERCLIP_ACTIVATION_TEST_SEAMS PAPERCLIP_ACTIVATION_NOW_UTC="2026-01-01T03:00:00Z" \
    bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"

# helper chown scan: a helper dir whose reharden reaches chown must refuse
mkdir -p "$ROOT/badhelpers"
cp "$HERE"/*.sh "$HERE"/*.py "$ROOT/badhelpers/" 2>/dev/null
printf '\nchown -R 0:0 /tmp/x\n' >> "$ROOT/badhelpers/post-restart-reharden.sh"
expect_refused "helper reaching chown is refused before any drain" "chown" -- \
  env PAPERCLIP_UPGRADE_HELPERS_DIR="$ROOT/badhelpers" PAPERCLIP_ACTIVATION_DRAIN="$ROOT/badhelpers/drain.sh" \
    bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"

# --- 4. lock safety ------------------------------------------------------------
ln -s "$ROOT/lockdir/real.lock" "$ROOT/lockdir/link.lock"
manifest_with "$bad" DEPLOY_LOCK_PATH "$ROOT/lockdir/link.lock"
expect_refused "symlink lock path is refused" "lock" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
: > "$ROOT/lockdir/service.deploy.lock"; chmod 0664 "$ROOT/lockdir/service.deploy.lock"
expect_refused "group/other-writable lock file is refused" "lock" -- \
  bash "$ACT" preflight --manifest "$MAN" --state-dir "$ROOT/state-a"
rm -f "$ROOT/lockdir/service.deploy.lock"

# concurrent activation with a DIFFERENT state dir is denied by the one lock
reset_logs
STUB_DRAIN_SLEEP_SECS=3 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-conc-a" --live \
  > "$ROOT/conc-a.out" 2>&1 &
conc_pid=$!
sleep 1
expect_refused "concurrent activation (different state dir) is denied" "lock" -- \
  bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-conc-b" --live
wait "$conc_pid"; conc_rc=$?
((conc_rc == 0)) && pass "first concurrent activation completed" \
  || oops "first concurrent activation rc=$conc_rc: $(head -c 300 "$ROOT/conc-a.out")"

# --- 5. full run sequence under the lock ----------------------------------------
reset_logs
rm -rf "$ROOT/state-run"
expect_ok "full run completes drain->wait->backup->restart->verify->undrain" -- \
  bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-run" --live
got_seq="$(grep '^lock-proof-ok' "$STUB_DRAIN_LOG" | awk '{print $2}' | tr '\n' ',')"
[[ "$got_seq" == "drain,wait,backup,restart,verify,undrain," ]] \
  && pass "phase order and lock proof on every phase ($got_seq)" \
  || oops "phase order wrong: $got_seq"
if grep -q -- '--no-build' "$STUB_DRAIN_LOG" || grep -Eq 'compose .* pull' "$STUB/docker.log"; then
  oops "wrapper pulled or built an image"
else
  pass "wrapper issued no build/pull (drain.sh compose_up keeps --no-build --pull never)"
fi
grep -q 'compose_up() .*--no-build --pull never\|--no-build --pull never --force-recreate' "$DRAIN" \
  && pass "drain.sh recreate stays --no-build --pull never" \
  || oops "drain.sh recreate lost its no-build/no-pull flags"

# a second run over a finished state refuses instead of re-draining
expect_refused "run over a completed state dir is refused" "REFUSED" -- \
  bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-run" --live

# verify failure: hold preserved, undrain never called, rollback argv printed
reset_logs
rm -rf "$ROOT/state-fail"
expect_refused "verify failure fails the run and keeps the hold" "rollback" -- \
  env STUB_DRAIN_VERIFY_FAIL=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-fail" --live
grep -q '^lock-proof-ok undrain' "$STUB_DRAIN_LOG" \
  && oops "undrain ran after a failed verify" || pass "undrain never ran after a failed verify"

# foreign state (different manifest) cannot be aborted/rolled back by us
manifest_with "$bad" EXPECT_COMMIT "$(printf 'f%.0s' $(seq 1 40))"
reset_logs
expect_refused "abort of a state owned by a different manifest is refused" "REFUSED" -- \
  bash "$ACT" abort --manifest "$bad" --state-dir "$ROOT/state-fail" --live
[[ ! -s "$STUB_DRAIN_LOG" ]] && pass "foreign abort never reached drain.sh" || oops "foreign abort reached drain.sh"

# owned rollback: operator first points compose at the rollback image
cp "$ROOT/compose.yaml" "$ROOT/compose.saved"
cp "$ROOT/compose.rollback.yaml" "$ROOT/compose.yaml"
reset_logs
expect_ok "owned rollback runs rollback+undrain on the pinned pre-change image" -- \
  bash "$ACT" rollback --manifest "$MAN" --state-dir "$ROOT/state-fail" --live
got_seq="$(grep '^lock-proof-ok' "$STUB_DRAIN_LOG" | awk '{print $2}' | tr '\n' ',')"
[[ "$got_seq" == "rollback,undrain," ]] && pass "rollback phase order ($got_seq)" || oops "rollback order wrong: $got_seq"
grep -q -- "registry.example.invalid/app:rollback" "$STUB_DRAIN_LOG.args" \
  && grep -q -- "$ROLL_ID" "$STUB_DRAIN_LOG.args" \
  && pass "rollback argv pins the actual pre-change image ref and id" \
  || oops "rollback argv does not pin the pre-change image"
cp "$ROOT/compose.saved" "$ROOT/compose.yaml"

# rollback refuses while compose still resolves as the candidate config
rm -rf "$ROOT/state-rb2" "$ROOT/state-rb2.activation-owner"
reset_logs
STUB_DRAIN_VERIFY_FAIL=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-rb2" --live >/dev/null 2>&1 || true
expect_refused "rollback refused until compose matches the rollback pin" "compose" -- \
  bash "$ACT" rollback --manifest "$MAN" --state-dir "$ROOT/state-rb2" --live

# --- 6. real drain.sh refuses mutating commands without the wrapper lock --------
printf 'header = "X-Test: 1"\n' > "$ROOT/board.curl"; chmod 0600 "$ROOT/board.curl"
expect_refused "drain.sh mutating command without wrapper lock proof is refused" "activation" -- \
  env -u PAPERCLIP_ACTIVATION_LOCK_PATH -u PAPERCLIP_ACTIVATION_LOCK_TOKEN -u PAPERCLIP_ACTIVATION_LOCK_FD \
    PAPERCLIP_UPGRADE_API_AUTH_FILE="$ROOT/board.curl" DOCKER="$ROOT/bin/docker" \
    bash "$DRAIN" wait --state-dir "$ROOT/state-direct" --live
[[ ! -e "$ROOT/state-direct" ]] && pass "direct drain.sh created no state" || oops "direct drain.sh created state"

# with a genuinely held lock + inherited fd, drain.sh proceeds to its own gates
LOCKF="$ROOT/lockdir/service.deploy.lock"; rm -f "$LOCKF"; : > "$LOCKF"; chmod 0600 "$LOCKF"
held_out="$(
  exec 8>>"$LOCKF"
  flock -n 8 || exit 97
  printf 'token=testtoken123\n' > "$LOCKF"
  PAPERCLIP_ACTIVATION_LOCK_PATH="$LOCKF" PAPERCLIP_ACTIVATION_LOCK_TOKEN="testtoken123" \
  PAPERCLIP_ACTIVATION_LOCK_FD=8 PAPERCLIP_UPGRADE_API_AUTH_FILE="$ROOT/board.curl" \
  DOCKER="$ROOT/bin/docker" bash "$DRAIN" wait --state-dir "$ROOT/state-missing" --live 2>&1
)"; held_rc=$?
if ((held_rc == 2)) && grep -qF "state dir missing" <<<"$held_out" && ! grep -qi "activation" <<<"$held_out"; then
  pass "drain.sh accepts genuine lock proof and reaches its own state gate"
else
  oops "drain.sh lock-proof acceptance wrong (rc=$held_rc: $(head -c 200 <<<"$held_out"))"
fi
rm -f "$LOCKF"

say "test-activation-offline: $CHECKS checks, FAIL=$FAIL"
((FAIL == 0)) && say "ACTIVATION CONTRACT PASSED" || say "ACTIVATION CONTRACT FAILED"
exit "$FAIL"
