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
  if [[ -f "$STUB_DIR/exec_broken" ]]; then exit 125; fi
  if [[ "$args" == *"/proc/1/status"* ]]; then
    if [[ -f "$STUB_DIR/pid1_status" ]]; then cat "$STUB_DIR/pid1_status";
    else printf 'Uid:\t1000\t1000\t1000\t1000\nUmask:\t0077\n'; fi
    exit 0
  fi
  # reharden write probes are sentinel-shaped: answer DENIED unless a
  # listed writable path appears, then WRITABLE (test -w) / CREATED (create)
  if [[ "$args" == *"DENIED"* ]]; then
    while IFS= read -r p; do
      if [[ -n "$p" && "$args" == *"$p"* ]]; then
        if [[ "$args" == *"CREATED"* ]]; then echo CREATED; else echo WRITABLE; fi
        exit 0
      fi
    done < "$STUB_DIR/writable_paths"
    echo DENIED
    exit 0
  fi
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
printf 'helpers precheck=%s reharden=%s canary=%s\n' "${PAPERCLIP_UPGRADE_PRECHECK:-}" "${PAPERCLIP_UPGRADE_REHARDEN:-}" "${PAPERCLIP_UPGRADE_SANDBOX_CANARY:-}" >> "$STUB_DRAIN_LOG"
printf 'pending-migrations=%s\n' "${PAPERCLIP_ACTIVATION_PENDING_MIGRATIONS:-}" >> "$STUB_DRAIN_LOG"
[[ -n "${STUB_DRAIN_DRAIN_FAIL:-}" && "$cmd" == drain ]] && { echo "stub drain: drain forced fail (pre-state)" >&2; exit 1; }
[[ -n "${STUB_DRAIN_SLEEP_SECS:-}" && "$cmd" == drain ]] && sleep "$STUB_DRAIN_SLEEP_SECS"
[[ -n "${STUB_TAMPER_COMPOSE:-}" && "$cmd" == wait ]] && printf 'tampered\n' >> "$PAPERCLIP_UPGRADE_COMPOSE_FILE"
[[ -n "${STUB_TAMPER_COMPOSE_VERIFY:-}" && "$cmd" == verify ]] && printf 'tampered\n' >> "$PAPERCLIP_UPGRADE_COMPOSE_FILE"
case "$cmd" in
  drain) mkdir -m 0700 "$state"; printf 'x\n' > "$state/drain-started"; printf 'x\n' > "$state/drain_requested_at" ;;
  wait) printf 'x\n' > "$state/drained_at" ;;
  rewake) printf 'x\n' > "$state/rewake_ran" ;;
  backup) printf 'x\n' > "$state/backup_at" ;;
  restart)
    grep -q -- '--expect-file' "$STUB_DRAIN_LOG.args" || { echo "stub drain: restart lacks --expect-file" >&2; exit 43; }
    printf 'x\n' > "$state/restart_started_at"; printf 'x\n' > "$state/restarted_at"; printf 'img id\n' > "$state/restart-image" ;;
  verify)
    [[ -n "${STUB_DRAIN_VERIFY_FAIL:-}" ]] && { echo "stub drain: verify forced fail" >&2; exit 1; }
    printf 'x\n' > "$state/verified_at"; printf 'img id\n' > "$state/verified_image" ;;
  rollback) printf 'x\n' > "$state/verified_at"; printf 'x\n' > "$state/rolledback_at" ;;
  undrain|abort) printf 'x\n' > "$state/undrained_at"
    [[ -n "${STUB_DRAIN_UNDRAIN_RC3:-}" && "$cmd" == undrain ]] && exit 3 ;;
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
PRECHECK_SHA="$(sha256sum < "$HERE/pre-restart-check.sh" | cut -d' ' -f1)"
printf 'migration admission receipt (hermetic fixture)\n' > "$ROOT/mig-receipt.txt"
RECEIPT_SHA="$(sha256sum < "$ROOT/mig-receipt.txt" | cut -d' ' -f1)"
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
    printf 'PRE_RESTART_CHECK_SHA256=%s\n' "$PRECHECK_SHA"
    printf 'MAX_BACKUP_AGE_MINS=120\n'
    printf 'PENDING_MIGRATIONS=none\n'
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
reharden_body="$(grep -Ev '^[[:space:]]*#' "$REHARDEN" || true)"
if grep -Eq '(^|[^A-Za-z_])(chown|chmod|sudo)([[:space:]]|$)' <<<"$reharden_body"; then
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
# descendants are judged, not only the three roots (the recursive repair
# this replaces covered the whole trees)
mkdir -p "$ROOT/data/plugin-packages-root/pkg"
printf 'x\n' > "$ROOT/data/plugin-packages-root/pkg/index.js"; chmod 0666 "$ROOT/data/plugin-packages-root/pkg/index.js"
expect_refused "reharden fails on a group/other-writable descendant of a protected root" "descendant" -- reharden_env
chmod 0644 "$ROOT/data/plugin-packages-root/pkg/index.js"
printf 'x\n' > "$ROOT/outside-target"; chmod 0666 "$ROOT/outside-target"
ln -s "$ROOT/outside-target" "$ROOT/data/.local/bin/escape"
expect_refused "reharden fails on a protected symlink resolving outside the baseline" "symlink" -- reharden_env
rm -f "$ROOT/data/.local/bin/escape"
# each descendant dimension pinned separately: group-write alone (0664, no
# other-write bit) and group-owner alone. The test user belongs to one group
# and cannot chgrp a fixture, so the group-owner case shifts the EXPECTED
# gid: roots then fail in check_path, so the needle must be the
# failure-path-only "protected descendant violates" (a bare "descendant"
# would also match the success line "descendants within the baseline")
# to pin check_tree's own ! -group term.
chmod 0664 "$ROOT/data/plugin-packages-root/pkg/index.js"
expect_refused "reharden fails on a group-writable descendant (0664)" "protected descendant violates" -- reharden_env
chmod 0644 "$ROOT/data/plugin-packages-root/pkg/index.js"
expect_refused "reharden fails on a wrong-gid descendant" "protected descendant violates" -- \
  env PAPERCLIP_DATA="$ROOT/data" PAPERCLIP_UPGRADE_EXPECT_OWNER="$(id -u):$(( $(id -g) + 1 ))" \
    PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL=1 PAPERCLIP_UPGRADE_SERVER_CONTAINER=paperclip-example \
    DOCKER="$ROOT/bin/docker" bash "$REHARDEN"
# container-absolute symlink targets are judged through the /paperclip ->
# $DATA_DIR remap: a link created inside the container must not fail the
# baseline permanently just because the host mounts the data elsewhere,
# while a dangling or out-of-baseline mapped target must still fail
mkdir -p "$ROOT/data/.local/share/tool"
printf 'x\n' > "$ROOT/data/.local/share/tool/run"; chmod 0644 "$ROOT/data/.local/share/tool/run"
ln -s /paperclip/.local/share/tool/run "$ROOT/data/.local/bin/tool-abs"
expect_ok "reharden passes a container-absolute symlink whose mapped target is in the baseline" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-abs"
ln -s /paperclip/.local/share/tool/missing "$ROOT/data/.local/bin/tool-abs"
expect_refused "reharden fails a container-absolute symlink whose mapped target does not resolve" "does not resolve" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-abs"
printf 'x\n' > "$ROOT/data/outside-x"; chmod 0666 "$ROOT/data/outside-x"
ln -s /paperclip/outside-x "$ROOT/data/.local/bin/tool-abs"
expect_refused "reharden fails a container-absolute symlink whose mapped target is outside the baseline" "outside the baseline" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-abs" "$ROOT/data/outside-x"
# every container-absolute hop of a chain is remapped, not just the first:
# a second /paperclip hop must be judged by its data-dir target instead of
# resolving in the host namespace (where a host /paperclip path could pass
# owner/mode while the container-visible target stays agent-writable)
ln -s /paperclip/.local/share/tool/run "$ROOT/data/outside-intermediate"
ln -s /paperclip/outside-intermediate "$ROOT/data/.local/bin/tool-chain"
expect_ok "reharden remaps a second container-absolute hop onto its data-dir target" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-chain" "$ROOT/data/outside-intermediate"
printf 'x\n' > "$ROOT/data/outside-x"; chmod 0666 "$ROOT/data/outside-x"
ln -s /paperclip/outside-x "$ROOT/data/outside-intermediate"
ln -s /paperclip/outside-intermediate "$ROOT/data/.local/bin/tool-chain"
expect_refused "reharden judges a chained container-absolute target outside the baseline" "outside the baseline" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-chain" "$ROOT/data/outside-intermediate" "$ROOT/data/outside-x"
# directory-component hops are remapped too
ln -s /paperclip/.local/share "$ROOT/data/droot"
ln -s /paperclip/droot/tool/run "$ROOT/data/.local/bin/tool-dchain"
expect_ok "reharden remaps a container-absolute directory-component hop" -- reharden_env
rm -f "$ROOT/data/.local/bin/tool-dchain" "$ROOT/data/droot"
# a broken docker exec must FAIL the probes, never read as "denied"
: > "$STUB/exec_broken"
expect_refused "reharden fails when docker exec cannot answer the write-denial probes" "inconclusive" -- reharden_env
rm -f "$STUB/exec_broken"
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

# bound pre-restart check: digest-pinned in the manifest, never a stub
manifest_with "$bad" PRE_RESTART_CHECK_SHA256 "$(printf 'e%.0s' $(seq 1 64))"
reset_logs
expect_refused "pre-restart-check digest mismatch is refused" "pre-restart" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
[[ ! -s "$STUB_DRAIN_LOG" ]] && pass "pre-restart mismatch: drain.sh untouched" || oops "pre-restart mismatch reached drain.sh"

write_manifest "$bad"; grep -v '^MAX_BACKUP_AGE_MINS=' "$bad" > "$bad.tmp" && mv "$bad.tmp" "$bad"; chmod 0600 "$bad"
expect_refused "missing backup-freshness bound is refused (no silent default)" "MAX_BACKUP_AGE_MINS" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"

# pending migrations need an admission receipt the wrapper consumes by pin,
# never a waiver computed here
manifest_with "$bad" PENDING_MIGRATIONS "0294"
expect_refused "pending migration without an admission receipt is refused" "migration admission" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
printf 'MIGRATION_ADMISSION_RECEIPT=%s\n' "$ROOT/mig-receipt.txt" >> "$bad"
printf 'MIGRATION_ADMISSION_RECEIPT_SHA256=%s\n' "$(printf 'e%.0s' $(seq 1 64))" >> "$bad"; chmod 0600 "$bad"
expect_refused "admission receipt digest mismatch is refused" "migration admission" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
sed -i "s|^MIGRATION_ADMISSION_RECEIPT_SHA256=.*|MIGRATION_ADMISSION_RECEIPT_SHA256=$RECEIPT_SHA|" "$bad"
expect_ok "pending migration with a pin-matching admission receipt passes preflight" -- \
  bash "$ACT" preflight --manifest "$bad" --state-dir "$ROOT/state-a"
ln -s "$ROOT/mig-receipt.txt" "$ROOT/mig-receipt-link.txt"
sed -i "s|^MIGRATION_ADMISSION_RECEIPT=.*|MIGRATION_ADMISSION_RECEIPT=$ROOT/mig-receipt-link.txt|" "$bad"
expect_refused "symlink admission receipt is refused" "migration admission" -- \
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
grep -qx -- '--max-backup-age-mins' "$STUB_DRAIN_LOG.args" && grep -qx '120' "$STUB_DRAIN_LOG.args" \
  && pass "restart argv carries the manifest backup-freshness bound" \
  || oops "restart argv lacks --max-backup-age-mins 120"
grep -q '^pending-migrations=none$' "$STUB_DRAIN_LOG" \
  && grep -q 'expect-pending-exact' "$DRAIN" \
  && pass "declared pending migrations reach drain.sh and are verified against image+ledger pre-recreate" \
  || oops "pending-migration declaration is not bound into drain.sh restart"

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

# resume after a pre-drain refusal re-judges window/pressure/baseline: the
# owner record exists but drain-started does not, so the next phase starts
# the drain and must not inherit stale admission
rm -rf "$ROOT/state-resume" "$ROOT/state-resume.activation-owner"
STUB_DRAIN_DRAIN_FAIL=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-resume" --live >/dev/null 2>&1 || true
reset_logs
expect_refused "resume that would start a fresh drain is refused inside the blocked window" "window" -- \
  env PAPERCLIP_ACTIVATION_NOW_UTC="2026-01-01T10:00:00Z" \
    bash "$ACT" resume --manifest "$MAN" --state-dir "$ROOT/state-resume" --live
grep -q '^lock-proof-ok drain' "$STUB_DRAIN_LOG" \
  && oops "resume started a drain inside the blocked window" || pass "blocked resume never reached the drain phase"
expect_ok "resume outside the window completes the pre-drain-failed state" -- \
  bash "$ACT" resume --manifest "$MAN" --state-dir "$ROOT/state-resume" --live

# drain.sh exit 3 (undrain completed, rewake warned) is PARTIAL with a
# rewake hint and wrapper exit 3 — never a held-drain failure
rm -rf "$ROOT/state-partial" "$ROOT/state-partial.activation-owner"
partial_out="$(STUB_DRAIN_UNDRAIN_RC3=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-partial" --live 2>&1)"; partial_rc=$?
if ((partial_rc == 3)) && grep -q 'PARTIAL' <<<"$partial_out" && grep -q 'rewake' <<<"$partial_out" \
   && ! grep -q 'nothing undrained' <<<"$partial_out"; then
  pass "undrain exit 3 maps to wrapper PARTIAL/exit 3 with a rewake hint"
else
  oops "undrain exit 3 mishandled (rc=$partial_rc: $(head -c 300 <<<"$partial_out"))"
fi

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

# owned rewake after a completed activation (deferred-wake redelivery path)
reset_logs
expect_ok "owned rewake runs after undrain under the lock" -- \
  env PAPERCLIP_UPGRADE_PRECHECK=/tmp/evil-precheck PAPERCLIP_UPGRADE_SANDBOX_CANARY=/tmp/evil-canary \
    bash "$ACT" rewake --manifest "$MAN" --state-dir "$ROOT/state-run" --live --focus-gate-expired
grep -q '^lock-proof-ok rewake' "$STUB_DRAIN_LOG" \
  && pass "rewake carried lock proof" || oops "rewake lacked lock proof"
grep -qF "precheck=$HERE/pre-restart-check.sh" "$STUB_DRAIN_LOG" \
  && grep -qF "canary=$HERE/sandbox-canary.sh" "$STUB_DRAIN_LOG" \
  && pass "per-helper env overrides are pinned to the scanned helper set" \
  || oops "per-helper env overrides reached drain.sh unpinned"
grep -q -- '--focus-gate-expired' "$STUB_DRAIN_LOG.args" \
  && pass "rewake passes --focus-gate-expired through" || oops "rewake lost its passthrough flag"
manifest_with "$bad" EXPECT_COMMIT "$(printf 'f%.0s' $(seq 1 40))"
reset_logs
expect_refused "rewake of a state owned by a different manifest is refused" "REFUSED" -- \
  bash "$ACT" rewake --manifest "$bad" --state-dir "$ROOT/state-run" --live
[[ ! -s "$STUB_DRAIN_LOG" ]] && pass "foreign rewake never reached drain.sh" || oops "foreign rewake reached drain.sh"
expect_refused "rewake before any undrain is refused" "undrained_at" -- \
  bash "$ACT" rewake --manifest "$MAN" --state-dir "$ROOT/state-rb2" --live

# compose edited during the (potentially hours-long) wait is caught pre-restart
rm -rf "$ROOT/state-tamper" "$ROOT/state-tamper.activation-owner"
reset_logs
expect_refused "compose tampered during wait is refused before restart" "compose" -- \
  env STUB_TAMPER_COMPOSE=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-tamper" --live
grep -q '^lock-proof-ok restart' "$STUB_DRAIN_LOG" \
  && oops "restart ran against a tampered compose file" || pass "restart never ran against tampered compose"
printf 'services: candidate\n' > "$ROOT/compose.yaml"

# compose edited between verify and undrain is caught before the undrain recreate
rm -rf "$ROOT/state-tamper2" "$ROOT/state-tamper2.activation-owner"
reset_logs
expect_refused "compose tampered after verify is refused before undrain" "compose" -- \
  env STUB_TAMPER_COMPOSE_VERIFY=1 bash "$ACT" run --manifest "$MAN" --state-dir "$ROOT/state-tamper2" --live
grep -q '^lock-proof-ok undrain' "$STUB_DRAIN_LOG" \
  && oops "undrain ran against a tampered compose file" || pass "undrain never ran against tampered compose"
printf 'services: candidate\n' > "$ROOT/compose.yaml"

# --- 5b. pre-restart-check: pending-decision gate + backup checksum format ------
# Pending-decision SQL must cover BOTH paused and terminated origin agents
# with no expiry narrowing (an expired-only filter would hide live rows).
PDSQL="$HERE/pending-decisions.sql"
if [[ -f "$PDSQL" ]] && grep -q "continuationPending" "$PDSQL" && grep -q "wake_origin_agent" "$PDSQL" \
   && grep -q "'paused'" "$PDSQL" && grep -q "'terminated'" "$PDSQL" && ! grep -qi "expires" "$PDSQL"; then
  pass "pending-decisions.sql covers paused+terminated origins, no expiry filter"
else
  oops "pending-decisions.sql missing or narrowed (want paused+terminated, no expires filter)"
fi
grep -q 'pending-decisions.sql' "$HERE/pre-restart-check.sh" \
  && pass "pre-restart-check wires the pending-decision gate" \
  || oops "pre-restart-check does not wire pending-decisions.sql"

# Execute the real pre-restart-check against a SQL-answering docker stub.
cat > "$ROOT/bin/docker-precheck" <<'STUBPRE'
#!/usr/bin/env bash
set -uo pipefail
sql="$(cat)"
case "$sql" in
  *continuationPending*) printf 'pending_wake_origin_decisions|%s\n' "$(cat "$STUB_DIR/pending_count")" ;;
  *__drizzle_migrations*) printf '1 0280aaaa\n2 0281bbbb\n' ;;
  *native_lease_expired*) printf 'native_lease_expired|0\n' ;;
  *) printf 'stub docker-precheck: unhandled sql\n' >&2; exit 99 ;;
esac
STUBPRE
chmod +x "$ROOT/bin/docker-precheck"
mkdir -p -m 0700 "$ROOT/pre-state" "$ROOT/pre-backup"
printf '2026-01-01T00:00:00Z\n' > "$ROOT/pre-state/drained_at"
printf 'dump-bytes' > "$ROOT/pre-backup/paperclip.dump"
# exactly the format drain.sh backup writes (sha256sum paperclip.dump)
(cd "$ROOT/pre-backup" && sha256sum paperclip.dump > SHA256SUMS)
precheck_run() {
  env DOCKER="$ROOT/bin/docker-precheck" PAPERCLIP_DATA="$ROOT/data" \
    bash "$HERE/pre-restart-check.sh" --state-dir "$ROOT/pre-state" \
      --backup-dir "$ROOT/pre-backup" --max-backup-age-mins 120 --ack-orphans 0 \
      --require 0280 --require 0281
}
printf '0\n' > "$STUB/pending_count"
expect_ok "pre-restart-check passes: fresh checksum-verifying backup (drain format), zero pending decisions" -- precheck_run
printf '2\n' > "$STUB/pending_count"
expect_refused "pre-restart-check fails on pending wake_origin_agent decisions" "pending" -- precheck_run
printf '0\n' > "$STUB/pending_count"
printf 'tampered' >> "$ROOT/pre-backup/paperclip.dump"
expect_refused "pre-restart-check fails when the backup checksum does not verify" "backup" -- precheck_run
printf 'dump-bytes' > "$ROOT/pre-backup/paperclip.dump"

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

# a same-uid attacker who opens the lock file themselves (a distinct open
# file description) while the holder holds it passes the token, identity
# and not-held checks but must fail the inheritance proof
LOCKF="$ROOT/lockdir/service.deploy.lock"; rm -f "$LOCKF"; : > "$LOCKF"; chmod 0600 "$LOCKF"
forged_out="$(
  exec 8>>"$LOCKF"
  flock -n 8 || exit 97
  printf 'token=testtoken789\n' > "$LOCKF"
  (
    exec 9>>"$LOCKF"
    PAPERCLIP_ACTIVATION_LOCK_PATH="$LOCKF" PAPERCLIP_ACTIVATION_LOCK_TOKEN="testtoken789" \
    PAPERCLIP_ACTIVATION_LOCK_FD=9 PAPERCLIP_UPGRADE_API_AUTH_FILE="$ROOT/board.curl" \
    DOCKER="$ROOT/bin/docker" bash "$DRAIN" wait --state-dir "$ROOT/state-forged" --live 2>&1
  )
)"; forged_rc=$?
if ((forged_rc == 2)) && grep -qF "not the holder's inherited fd" <<<"$forged_out"; then
  pass "drain.sh refuses a self-opened lock fd while the holder holds the lock"
else
  oops "drain.sh lock-proof inheritance wrong (rc=$forged_rc: $(head -c 200 <<<"$forged_out"))"
fi
[[ ! -e "$ROOT/state-forged" ]] && pass "forged-fd drain.sh created no state" || oops "forged-fd drain.sh created state"
rm -f "$LOCKF"

# an unheld lock with a self-opened fd must fail the not-held check, not a
# later gate: this pins the check order in require_activation_lock (the
# flock inheritance proof must run after the not-held check, since on an
# unheld lock it would take the lock itself and mask the refusal)
LOCKF="$ROOT/lockdir/service.deploy.lock"; rm -f "$LOCKF"; : > "$LOCKF"; chmod 0600 "$LOCKF"
unheld_out="$(
  printf 'token=testtoken321\n' > "$LOCKF"
  exec 9>>"$LOCKF"
  PAPERCLIP_ACTIVATION_LOCK_PATH="$LOCKF" PAPERCLIP_ACTIVATION_LOCK_TOKEN="testtoken321" \
  PAPERCLIP_ACTIVATION_LOCK_FD=9 PAPERCLIP_UPGRADE_API_AUTH_FILE="$ROOT/board.curl" \
  DOCKER="$ROOT/bin/docker" bash "$DRAIN" wait --state-dir "$ROOT/state-unheld" --live 2>&1
)"; unheld_rc=$?
if ((unheld_rc == 2)) && grep -qF "activation lock is not held" <<<"$unheld_out"; then
  pass "drain.sh refuses an unheld lock before the fd inheritance proof"
else
  oops "drain.sh lock-check order wrong (rc=$unheld_rc: $(head -c 200 <<<"$unheld_out"))"
fi
[[ ! -e "$ROOT/state-unheld" ]] && pass "unheld-lock drain.sh created no state" || oops "unheld-lock drain.sh created state"
rm -f "$LOCKF"

# the same proof through a symlinked ancestor must also be accepted:
# identity (-ef), not the canonical path string, is what is compared
mkdir -p -m 0700 "$ROOT/real-locks"
ln -s "$ROOT/real-locks" "$ROOT/link-locks"
LOCKF2="$ROOT/link-locks/service.deploy.lock"; : > "$ROOT/real-locks/service.deploy.lock"; chmod 0600 "$ROOT/real-locks/service.deploy.lock"
held2_out="$(
  exec 8>>"$LOCKF2"
  flock -n 8 || exit 97
  printf 'token=testtoken456\n' > "$LOCKF2"
  PAPERCLIP_ACTIVATION_LOCK_PATH="$LOCKF2" PAPERCLIP_ACTIVATION_LOCK_TOKEN="testtoken456" \
  PAPERCLIP_ACTIVATION_LOCK_FD=8 PAPERCLIP_UPGRADE_API_AUTH_FILE="$ROOT/board.curl" \
  DOCKER="$ROOT/bin/docker" bash "$DRAIN" wait --state-dir "$ROOT/state-missing" --live 2>&1
)"; held2_rc=$?
if ((held2_rc == 2)) && grep -qF "state dir missing" <<<"$held2_out" && ! grep -qi "points at" <<<"$held2_out"; then
  pass "drain.sh lock proof accepts a symlinked ancestor (device+inode identity)"
else
  oops "drain.sh lock proof refused a symlinked ancestor (rc=$held2_rc: $(head -c 200 <<<"$held2_out"))"
fi

# --- 7. migration_gate --expect-pending-exact: declaration proved, not trusted -
MGH1="$(printf 'a%.0s' $(seq 1 64))"; MGH2="$(printf 'b%.0s' $(seq 1 64))"
printf '%s  0293_applied.sql\n%s  0294_pending.sql\n' "$MGH1" "$MGH2" > "$ROOT/mig-hashes"
printf '%s\n' "$MGH1" > "$ROOT/mig-ledger"
expect_refused "declared 'none' fails when the image carries a pending migration" "declared" -- \
  python3 "$HERE/migration_gate.py" --sql-hashes "$ROOT/mig-hashes" --ledger "$ROOT/mig-ledger" \
    --max-unknown 0 --expect-pending-exact
expect_ok "declared 0294 passes when exactly 0294 is pending" -- \
  python3 "$HERE/migration_gate.py" --sql-hashes "$ROOT/mig-hashes" --ledger "$ROOT/mig-ledger" \
    --max-unknown 0 --expect-pending-exact --expect-pending 0294
printf '%s\n%s\n' "$MGH1" "$MGH2" > "$ROOT/mig-ledger"
expect_ok "declared 'none' passes when every image migration is applied" -- \
  python3 "$HERE/migration_gate.py" --sql-hashes "$ROOT/mig-hashes" --ledger "$ROOT/mig-ledger" \
    --max-unknown 0 --expect-pending-exact

# --- 8. drain.sh restart wiring of that gate, executed verbatim ----------------
# pending_migrations_gate is extracted from drain.sh and run against fixture
# state (hashes-ahead + ledger-backup), so the restart wiring itself — env
# binding, 4-digit prefixes, --expect-pending appends, fail-closed on a gate
# miss — is behaviourally pinned, not only grepped. fail() is stubbed with
# its real exit semantics; in cmd_restart that exit is what stops the
# restart, and the call-site order check below pins it before compose_up.
mkdir -p "$ROOT/migstate"; : > "$ROOT/migstate/gate.log"
cp "$ROOT/mig-hashes" "$ROOT/migstate/hashes-ahead"
printf '%s\n' "$MGH1" > "$ROOT/migstate/ledger-backup"
pmig_gate() { # DECLARED | __unset__
  (
    eval "$(awk '/^pending_migrations_gate\(\) \{/,/^}$/' "$DRAIN")"
    STATE_DIR="$ROOT/migstate"; HELPERS_DIR="$HERE"; LOG="$ROOT/migstate/gate.log"
    st() { printf '%s/%s' "$STATE_DIR" "$1"; }
    fail() { printf 'ERROR: %s\n' "$*"; exit 1; }
    if [[ "$1" != __unset__ ]]; then
      PAPERCLIP_ACTIVATION_PENDING_MIGRATIONS="$1"
    else
      unset PAPERCLIP_ACTIVATION_PENDING_MIGRATIONS
    fi
    pending_migrations_gate
  )
}
expect_refused "restart gate: missing pending-migrations declaration is refused" "declared pending migrations missing" -- pmig_gate __unset__
expect_refused "restart gate: declared 'none' refuses an image carrying a pending migration" "do not match" -- pmig_gate none
expect_ok "restart gate: declared 0294 passes when exactly 0294 is pending" -- pmig_gate 0294
expect_refused "restart gate: a wrong declared prefix is refused" "do not match" -- pmig_gate 0299
expect_refused "restart gate: a malformed declared prefix is refused" "4-digit" -- pmig_gate 29x
printf '%s\n%s\n' "$MGH1" "$MGH2" > "$ROOT/migstate/ledger-backup"
expect_ok "restart gate: declared 'none' passes when every image migration is applied" -- pmig_gate none
expect_refused "restart gate: declared 0294 refuses when nothing is pending" "do not match" -- pmig_gate 0294
gate_line="$(awk '/^cmd_restart\(\) \{/{on=1} on{line=$0; sub(/#.*/,"",line)} on && line ~ /pending_migrations_gate/{print NR; exit}' "$DRAIN")"
up_line="$(awk '/^cmd_restart\(\) \{/{on=1} on{line=$0; sub(/#.*/,"",line)} on && line ~ /compose_up/{print NR; exit}' "$DRAIN")"
if [[ -n "$gate_line" && -n "$up_line" ]] && ((gate_line < up_line)); then
  pass "restart gate: pending_migrations_gate runs in cmd_restart before any compose_up"
else
  oops "restart gate: pending_migrations_gate call site missing or after compose_up (gate=$gate_line up=$up_line)"
fi

say "test-activation-offline: $CHECKS checks, FAIL=$FAIL"
((FAIL == 0)) && say "ACTIVATION CONTRACT PASSED" || say "ACTIVATION CONTRACT FAILED"
exit "$FAIL"
