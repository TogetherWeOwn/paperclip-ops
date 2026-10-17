#!/usr/bin/env bash
# ===========================================================================
# paperclip-upgrade/activation.sh — single-process, exclusive-lock
# activation wrapper around drain.sh.
#
# One invocation holds ONE host/service-scoped deployment lock (flock on
# the manifest's DEPLOY_LOCK_PATH) continuously across baseline
# validation, natural drain, backup, recreate, verification and owned
# undrain. drain.sh only ever ran a per-state-dir lock, so two state dirs
# could interleave against the same service; drain.sh now refuses every
# mutating subcommand that does not prove it descends from this lock
# holder (see require_activation_lock in drain.sh), which also closes
# the legacy direct-invocation bypass once these sources are installed.
#
# Fail-closed contract (every value comes from the operator manifest;
# there are no silent defaults for identity, pressure or window):
#   - candidate image id, source commit, entrypoint (argv + file hash +
#     no ownership-change command reachable in it), service user
#     (non-root), compose file hash, compose mount destinations,
#     effective-build file pins and the ACTUAL pre-change rollback
#     image are all pinned and re-read from the runtime, never trusted
#     from a mutable tag or a stamped label alone;
#   - host pressure (load / MemAvailable / disk) and the blocked UTC
#     window are judged from live evidence against manifest thresholds;
#     missing or malformed evidence refuses;
#   - the hardening baseline is VERIFIED (post-restart-reharden.sh),
#     never repaired: this path changes no ownership or modes anywhere;
#   - helpers reachable from drain.sh are scanned for ownership-change
#     commands before anything runs;
#   - recreate stays drain.sh's --no-build --pull never compose_up;
#   - any phase failure leaves the drain/hold exactly as drain.sh left
#     it (nothing is cancelled or undrained) and prints the owned
#     abort/rollback argv for this same state dir.
#
# Subcommands:
#   preflight   lock + validate bindings/baseline only (no drain)
#   run         fresh activation, all phases (--live)
#   resume      continue an owned, unfinished state dir (--live)
#   rollback    owned rollback to the pinned pre-change image, after the
#               operator points the compose file at it (--live)
#   abort       owned pre-restart abort (--live)
#
# Manifest: 0600 regular file, KEY=VALUE lines, parsed never sourced.
# See activation.example.conf and ACTIVATION.md for the key contract,
# the parity table and the remaining host-only attestations. This
# source proves the contract; it does not attest installation.
#
# Test seams (refused unless PAPERCLIP_ACTIVATION_TEST_SEAMS=1):
#   PAPERCLIP_ACTIVATION_NOW_UTC, PAPERCLIP_ACTIVATION_LOADAVG_FILE,
#   PAPERCLIP_ACTIVATION_MEMINFO_FILE
# Overrides used by the hermetic suite and by operators staging a new
# helper set: PAPERCLIP_ACTIVATION_DRAIN, PAPERCLIP_UPGRADE_HELPERS_DIR,
# DOCKER.
#
# Exit: 0 ok | 1 a gate/phase failed (hold preserved) | 2 refused
# ===========================================================================
set -uo pipefail
umask 077

ME="${BASH_SOURCE[0]##*/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="${PAPERCLIP_UPGRADE_HELPERS_DIR:-$HERE}"
DRAIN="${PAPERCLIP_ACTIVATION_DRAIN:-$HELPERS_DIR/drain.sh}"
REHARDEN="$HELPERS_DIR/post-restart-reharden.sh"
DOCKER="${DOCKER:-docker}"

CMD=""; MANIFEST=""; STATE_DIR=""; LIVE=0
usage() {
  cat <<'USAGE'
usage: activation.sh preflight|run|resume|rollback|abort \
         --manifest FILE --state-dir DIR [--live]
USAGE
}
while (($#)); do
  case "$1" in
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:?}"; shift 2 ;;
    --live) LIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    preflight|run|resume|rollback|abort)
      [[ -z "$CMD" ]] || { printf 'REFUSED: %s: two subcommands given\n' "$ME" >&2; exit 2; }
      CMD="$1"; shift ;;
    *) printf 'REFUSED: %s: unknown flag or subcommand: %s\n' "$ME" "$1" >&2; exit 2 ;;
  esac
done

refuse() { printf 'REFUSED: %s: %s\n' "$ME" "$*" >&2; exit 2; }
fail() { printf 'FAIL: %s: %s\n' "$ME" "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }

[[ -n "$CMD" ]] || { usage >&2; refuse "a subcommand is required"; }
[[ -n "$MANIFEST" ]] || refuse "--manifest is required"
[[ -n "$STATE_DIR" && "$STATE_DIR" == /* ]] || refuse "--state-dir must be an absolute path"
STATE_DIR="${STATE_DIR%/}"
[[ "$CMD" == preflight || "$LIVE" == 1 ]] || refuse "$CMD changes the live host; pass --live to confirm"
[[ -z "${FORCE:-}" ]] || refuse "FORCE is set in the environment; there is no force escape on this path"
for bin in "$DOCKER" jq sha256sum flock df awk stat readlink od; do
  command -v "$bin" >/dev/null 2>&1 || refuse "required tool is missing: $bin"
done
if [[ "${PAPERCLIP_ACTIVATION_TEST_SEAMS:-0}" != 1 ]]; then
  for v in PAPERCLIP_ACTIVATION_NOW_UTC PAPERCLIP_ACTIVATION_LOADAVG_FILE PAPERCLIP_ACTIVATION_MEMINFO_FILE; do
    [[ -z "${!v:-}" ]] || refuse "$v is a test seam; refusing without PAPERCLIP_ACTIVATION_TEST_SEAMS=1"
  done
fi

WORKD="$(mktemp -d)" || refuse "cannot create a work dir"
trap 'rm -rf "$WORKD"' EXIT

# --- manifest ------------------------------------------------------------------
[[ -f "$MANIFEST" && ! -L "$MANIFEST" ]] || refuse "manifest is not a regular file: $MANIFEST"
[[ -O "$MANIFEST" ]] || refuse "manifest is not owned by $(id -un): $MANIFEST"
m_mode="$(stat -c %a -- "$MANIFEST")"
(( (8#$m_mode & 8#077) == 0 )) || refuse "manifest must not be group/other readable or writable (is $m_mode): $MANIFEST"

declare -A M=()
ALLOWED_KEYS=" SERVICE SERVER_CONTAINER COMPOSE_FILE COMPOSE_SHA256 ROLLBACK_COMPOSE_SHA256 \
IMAGE EXPECT_IMAGE_ID EXPECT_COMMIT ROLLBACK_IMAGE ROLLBACK_IMAGE_ID \
ENTRYPOINT_PATH ENTRYPOINT_SHA256 ROLLBACK_ENTRYPOINT_SHA256 EXPECT_ENTRYPOINT_JSON \
EXPECT_USER EXPECT_FILES REQUIRE_MIGRATIONS MOUNT_DESTINATIONS_SHA256 DEPLOY_LOCK_PATH \
DATA_DIR MAX_LOAD1_X100 MIN_MEM_AVAILABLE_KB MIN_DISK_AVAIL_BYTES \
BLOCKED_START_HOUR_UTC BLOCKED_END_HOUR_UTC REQUIRE_CARGO_POOL EXPECT_PROTECTED_OWNER \
STABLE_POLLS POLL_SECS MAX_WAIT_SECS HEALTH_TIMEOUT_SECS PRIVATE_OVERLAY_REQUIRED \
BUILD_MARKER EXPECT_HOST_FILES PLUGIN_INVENTORY "
REQUIRED_KEYS="SERVICE SERVER_CONTAINER COMPOSE_FILE COMPOSE_SHA256 ROLLBACK_COMPOSE_SHA256 \
IMAGE EXPECT_IMAGE_ID EXPECT_COMMIT ROLLBACK_IMAGE ROLLBACK_IMAGE_ID \
ENTRYPOINT_PATH ENTRYPOINT_SHA256 ROLLBACK_ENTRYPOINT_SHA256 EXPECT_ENTRYPOINT_JSON \
EXPECT_USER EXPECT_FILES REQUIRE_MIGRATIONS MOUNT_DESTINATIONS_SHA256 DEPLOY_LOCK_PATH \
DATA_DIR MAX_LOAD1_X100 MIN_MEM_AVAILABLE_KB MIN_DISK_AVAIL_BYTES \
BLOCKED_START_HOUR_UTC BLOCKED_END_HOUR_UTC REQUIRE_CARGO_POOL EXPECT_PROTECTED_OWNER \
STABLE_POLLS POLL_SECS MAX_WAIT_SECS HEALTH_TIMEOUT_SECS PRIVATE_OVERLAY_REQUIRED BUILD_MARKER"
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || refuse "malformed manifest line (want KEY=VALUE): $line"
  key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
  [[ "$ALLOWED_KEYS" == *" $key "* ]] || refuse "unknown manifest key: $key"
  [[ -z "${M[$key]+set}" ]] || refuse "duplicate manifest key: $key"
  M["$key"]="$val"
done < "$MANIFEST"
for key in $REQUIRED_KEYS; do
  [[ -n "${M[$key]:-}" ]] || refuse "manifest key $key is required (no silent default)"
done
mv_() { printf '%s' "${M[$1]:-}"; }

# shape checks: every identity value is pinned, nothing is a bare tag
sha64_re='^[0-9a-f]{64}$'
[[ "$(mv_ COMPOSE_SHA256)" =~ $sha64_re && "$(mv_ ROLLBACK_COMPOSE_SHA256)" =~ $sha64_re ]] \
  || refuse "COMPOSE_SHA256 / ROLLBACK_COMPOSE_SHA256 must be 64 lowercase hex"
[[ "$(mv_ MOUNT_DESTINATIONS_SHA256)" =~ $sha64_re ]] || refuse "MOUNT_DESTINATIONS_SHA256 must be 64 lowercase hex"
[[ "$(mv_ ENTRYPOINT_SHA256)" =~ $sha64_re && "$(mv_ ROLLBACK_ENTRYPOINT_SHA256)" =~ $sha64_re ]] \
  || refuse "ENTRYPOINT_SHA256 / ROLLBACK_ENTRYPOINT_SHA256 must be 64 lowercase hex"
[[ "$(mv_ EXPECT_IMAGE_ID)" =~ ^sha256:[0-9a-f]{64}$ && "$(mv_ ROLLBACK_IMAGE_ID)" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || refuse "EXPECT_IMAGE_ID / ROLLBACK_IMAGE_ID must be sha256:<64 hex>"
[[ "$(mv_ EXPECT_COMMIT)" =~ ^[0-9a-f]{40}$ ]] || refuse "EXPECT_COMMIT must be a full 40-hex source commit"
for img_key in IMAGE ROLLBACK_IMAGE; do
  img="$(mv_ "$img_key")"
  [[ "$img" != *:latest && "$img" =~ ^[A-Za-z0-9._/:@-]+$ && ( "$img" == *@sha256:* || "${img##*/}" == *:* ) ]] \
    || refuse "$img_key must carry a non-latest tag or digest: $img"
done
if [[ "$(mv_ IMAGE)" == *@sha256:* && "${M[IMAGE]#*@}" != "$(mv_ EXPECT_IMAGE_ID)" ]]; then
  refuse "IMAGE digest does not match EXPECT_IMAGE_ID"
fi
[[ "$(mv_ ENTRYPOINT_PATH)" =~ ^/[A-Za-z0-9_.@/-]+$ ]] || refuse "ENTRYPOINT_PATH must be a clean absolute path"
expect_user="$(mv_ EXPECT_USER)"
[[ "$expect_user" =~ ^[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)?$ ]] || refuse "EXPECT_USER (service user) is malformed"
user_uid_part="${expect_user%%:*}"
[[ "$user_uid_part" != 0 && "$user_uid_part" != root ]] \
  || refuse "EXPECT_USER service user must not be root (a root entrypoint can take forbidden ownership paths)"
jq -ce . >/dev/null 2>&1 <<<"$(mv_ EXPECT_ENTRYPOINT_JSON)" || refuse "EXPECT_ENTRYPOINT_JSON is not valid JSON"
IFS=',' read -r -a EXPECT_FILE_LIST <<<"$(mv_ EXPECT_FILES)"
((${#EXPECT_FILE_LIST[@]} >= 1)) || refuse "EXPECT_FILES must pin at least one effective-build file"
for xf in "${EXPECT_FILE_LIST[@]}"; do
  [[ "$xf" =~ ^/[A-Za-z0-9_.@/-]+:[0-9a-f]{64}$ && "$xf" != *..* ]] \
    || refuse "EXPECT_FILES entry must be ABS:64-hex-sha256: $xf"
done
IFS=',' read -r -a MIGRATION_LIST <<<"$(mv_ REQUIRE_MIGRATIONS)"
for t in "${MIGRATION_LIST[@]}"; do [[ "$t" =~ ^[0-9]{4}$ ]] || refuse "REQUIRE_MIGRATIONS entry must be a 4-digit prefix: $t"; done
for n_key in MAX_LOAD1_X100 MIN_MEM_AVAILABLE_KB MIN_DISK_AVAIL_BYTES POLL_SECS MAX_WAIT_SECS HEALTH_TIMEOUT_SECS; do
  [[ "$(mv_ "$n_key")" =~ ^[0-9]+$ && "$(mv_ "$n_key")" -ge 1 ]] || refuse "$n_key must be a positive integer"
done
[[ "$(mv_ STABLE_POLLS)" =~ ^[0-9]+$ && "$(mv_ STABLE_POLLS)" -ge 2 ]] \
  || refuse "STABLE_POLLS must be an integer >= 2 (stable-zero readback is never a single poll)"
for h_key in BLOCKED_START_HOUR_UTC BLOCKED_END_HOUR_UTC; do
  [[ "$(mv_ "$h_key")" =~ ^[0-9]+$ && "$(mv_ "$h_key")" -le 23 ]] || refuse "$h_key must be an hour 0-23"
done
[[ "$(mv_ BLOCKED_START_HOUR_UTC)" != "$(mv_ BLOCKED_END_HOUR_UTC)" ]] || refuse "blocked window start and end must differ"
[[ "$(mv_ REQUIRE_CARGO_POOL)" =~ ^[01]$ && "$(mv_ PRIVATE_OVERLAY_REQUIRED)" =~ ^[01]$ ]] \
  || refuse "REQUIRE_CARGO_POOL / PRIVATE_OVERLAY_REQUIRED must be 0 or 1"
[[ "$(mv_ EXPECT_PROTECTED_OWNER)" =~ ^[0-9]+:[0-9]+$ ]] || refuse "EXPECT_PROTECTED_OWNER must be uid:gid"
[[ "$(mv_ BUILD_MARKER)" =~ ^[A-Za-z0-9_.~-]+$ ]] || refuse "BUILD_MARKER must be a plain token"
[[ "$(mv_ COMPOSE_FILE)" == /* && "$(mv_ DATA_DIR)" == /* && "$(mv_ DEPLOY_LOCK_PATH)" == /* ]] \
  || refuse "COMPOSE_FILE, DATA_DIR and DEPLOY_LOCK_PATH must be absolute paths"
# SERVICE is interpolated into drain.sh's hold-override YAML; keep both it
# and the container name to plain identifier characters.
[[ "$(mv_ SERVICE)" =~ ^[A-Za-z0-9_.-]+$ && "$(mv_ SERVER_CONTAINER)" =~ ^[A-Za-z0-9_.-]+$ ]] \
  || refuse "SERVICE / SERVER_CONTAINER must be plain identifiers"
host_files=()
if [[ -n "$(mv_ EXPECT_HOST_FILES)" ]]; then
  IFS=',' read -r -a host_files <<<"$(mv_ EXPECT_HOST_FILES)"
  for hf in "${host_files[@]}"; do
    [[ "$hf" =~ ^/[A-Za-z0-9_.@/-]+:[0-9a-f]{64}$ && "$hf" != *..* ]] \
      || refuse "EXPECT_HOST_FILES entry must be ABS:64-hex-sha256: $hf"
  done
fi
if [[ "$(mv_ PRIVATE_OVERLAY_REQUIRED)" == 1 && ${#host_files[@]} -eq 0 ]]; then
  refuse "PRIVATE_OVERLAY_REQUIRED=1 but EXPECT_HOST_FILES pins no overlay file"
fi
if [[ -n "$(mv_ PLUGIN_INVENTORY)" ]]; then
  [[ "$(mv_ PLUGIN_INVENTORY)" == /* && -f "$(mv_ PLUGIN_INVENTORY)" && ! -L "$(mv_ PLUGIN_INVENTORY)" ]] \
    || refuse "PLUGIN_INVENTORY is not an absolute regular file"
fi
MANIFEST_SHA256="$(sha256sum < "$MANIFEST" | cut -d' ' -f1)"

# --- the one deployment lock -----------------------------------------------------
LOCK_PATH="$(mv_ DEPLOY_LOCK_PATH)"
lock_parent="$(dirname -- "$LOCK_PATH")"
[[ -d "$lock_parent" && ! -L "$lock_parent" ]] || refuse "lock parent dir missing or a symlink: $lock_parent"
[[ -O "$lock_parent" ]] || refuse "lock parent dir is not owned by $(id -un): $lock_parent"
lp_mode="$(stat -c %a -- "$lock_parent")"
(( (8#$lp_mode & 8#022) == 0 )) || refuse "lock parent dir must not be group/other writable (is $lp_mode): $lock_parent"
if [[ -L "$LOCK_PATH" ]]; then
  refuse "lock path is a symlink: $LOCK_PATH"
elif [[ -e "$LOCK_PATH" ]]; then
  [[ -f "$LOCK_PATH" && -O "$LOCK_PATH" ]] || refuse "lock path is not an owned regular file: $LOCK_PATH"
  lf_mode="$(stat -c %a -- "$LOCK_PATH")"
  (( (8#$lf_mode & 8#077) == 0 )) || refuse "lock file must be owner-only (is $lf_mode): $LOCK_PATH"
else
  ( set -C; : > "$LOCK_PATH" ) 2>/dev/null || refuse "cannot create lock file: $LOCK_PATH"
fi
exec 8>>"$LOCK_PATH" || refuse "cannot open deployment lock: $LOCK_PATH"
flock -n 8 \
  || refuse "deployment lock already held (another activation owns this service): $LOCK_PATH"
LOCK_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
[[ "$LOCK_TOKEN" =~ ^[0-9a-f]{32}$ ]] || refuse "cannot mint a lock token"
printf 'token=%s\npid=%s\nstarted=%s\n' "$LOCK_TOKEN" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK_PATH" \
  || refuse "cannot record the lock owner token"
export PAPERCLIP_ACTIVATION_LOCK_PATH="$LOCK_PATH"
export PAPERCLIP_ACTIVATION_LOCK_TOKEN="$LOCK_TOKEN"
export PAPERCLIP_ACTIVATION_LOCK_FD=8
log "ok: deployment lock held ($LOCK_PATH)"

# environment for drain.sh and its helpers: one helper set, one service
export PAPERCLIP_UPGRADE_HELPERS_DIR="$HELPERS_DIR"
export PAPERCLIP_UPGRADE_COMPOSE_FILE="$(mv_ COMPOSE_FILE)"
export PAPERCLIP_UPGRADE_SERVER_SERVICE="$(mv_ SERVICE)"
export PAPERCLIP_UPGRADE_SERVER_CONTAINER="$(mv_ SERVER_CONTAINER)"
export PAPERCLIP_DATA="$(mv_ DATA_DIR)"
export PAPERCLIP_UPGRADE_EXPECT_OWNER="$(mv_ EXPECT_PROTECTED_OWNER)"
export PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL="$(mv_ REQUIRE_CARGO_POOL)"
export DOCKER

OWNER_FILE="$STATE_DIR.activation-owner"
write_owner() {
  ( set -C; printf 'manifest_sha256=%s\nlock_path=%s\n' "$MANIFEST_SHA256" "$LOCK_PATH" > "$OWNER_FILE" ) 2>/dev/null \
    || refuse "activation owner record already exists for $STATE_DIR (use resume/abort/rollback, never a second run)"
}
check_owner() {
  [[ -f "$OWNER_FILE" && ! -L "$OWNER_FILE" ]] \
    || refuse "no activation owner record beside $STATE_DIR; this wrapper never started that state"
  grep -Fqx "manifest_sha256=$MANIFEST_SHA256" "$OWNER_FILE" \
    || refuse "state $STATE_DIR is owned by a different manifest; refusing to touch another activation's drain"
  grep -Fqx "lock_path=$LOCK_PATH" "$OWNER_FILE" \
    || refuse "state $STATE_DIR belongs to a different deployment lock"
}
have() { [[ -s "$STATE_DIR/$1" ]]; }

# --- gates ------------------------------------------------------------------------
scan_no_ownership_change() { # FILE...: no ownership-change command reachable
  local f hits
  for f in "$@"; do
    [[ -f "$f" && ! -L "$f" ]] || refuse "helper missing or a symlink: $f"
    hits="$(grep -Ev '^[[:space:]]*#' "$f" | grep -En '\bchown\b|\bfchown\b|\blchown\b|chownSync|os\.chown' || true)"
    [[ -z "$hits" ]] || refuse "forbidden chown reachable in helper $f: $hits"
  done
}

window_gate() {
  local hour
  if [[ -n "${PAPERCLIP_ACTIVATION_NOW_UTC:-}" ]]; then
    [[ "$PAPERCLIP_ACTIVATION_NOW_UTC" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}):[0-9]{2}:[0-9]{2}Z$ ]] \
      || refuse "PAPERCLIP_ACTIVATION_NOW_UTC is malformed"
    hour=$((10#${BASH_REMATCH[1]}))
  else
    hour=$((10#$(date -u +%H)))
  fi
  local s e; s="$(mv_ BLOCKED_START_HOUR_UTC)"; e="$(mv_ BLOCKED_END_HOUR_UTC)"
  if (( s < e ? (hour >= s && hour < e) : (hour >= s || hour < e) )); then
    refuse "inside the blocked maintenance window (${s}:00-${e}:00 UTC, now ${hour}:xx UTC); no window exception exists on this path"
  fi
  log "ok: outside the blocked window (${s}:00-${e}:00 UTC, now ${hour}:xx UTC)"
}

pressure_gate() {
  local load_file="${PAPERCLIP_ACTIVATION_LOADAVG_FILE:-/proc/loadavg}"
  local mem_file="${PAPERCLIP_ACTIVATION_MEMINFO_FILE:-/proc/meminfo}"
  local load1 int frac load_x100 mem_avail avail
  load1="$(awk 'NR == 1 { print $1 }' "$load_file" 2>/dev/null)" \
    || refuse "load evidence unreadable: $load_file"
  [[ "$load1" =~ ^([0-9]+)\.([0-9]{2})$ ]] || refuse "load evidence malformed (want N.NN load average): '$load1'"
  int=$((10#${BASH_REMATCH[1]})); frac=$((10#${BASH_REMATCH[2]}))
  load_x100=$((int * 100 + frac))
  (( load_x100 <= $(mv_ MAX_LOAD1_X100) )) \
    || refuse "load1 $load1 exceeds the manifest load ceiling (MAX_LOAD1_X100=$(mv_ MAX_LOAD1_X100))"
  mem_avail="$(awk '$1 == "MemAvailable:" { print $2 }' "$mem_file" 2>/dev/null)"
  [[ "$mem_avail" =~ ^[0-9]+$ ]] || refuse "MemAvailable evidence missing or malformed: $mem_file"
  (( mem_avail >= $(mv_ MIN_MEM_AVAILABLE_KB) )) \
    || refuse "MemAvailable ${mem_avail}kB is below the manifest floor (MIN_MEM_AVAILABLE_KB=$(mv_ MIN_MEM_AVAILABLE_KB))"
  # -P and --output are mutually exclusive in current coreutils: a combined
  # call errors out and would judge real headroom "unreadable" forever.
  avail="$(df -B1 --output=avail -- "$(mv_ DATA_DIR)" 2>/dev/null | tail -n1 | tr -dc 0-9)"
  [[ "$avail" =~ ^[0-9]+$ ]] || refuse "disk evidence unreadable for $(mv_ DATA_DIR)"
  (( avail >= $(mv_ MIN_DISK_AVAIL_BYTES) )) \
    || refuse "disk available $avail bytes is below the manifest floor (MIN_DISK_AVAIL_BYTES=$(mv_ MIN_DISK_AVAIL_BYTES))"
  log "ok: host pressure (load1 $load1, MemAvailable ${mem_avail}kB, disk avail $avail)"
}

entrypoint_gate() { # IMAGE_ID EXPECTED_SHA256 — content, hash, no chown token
  local iid="$1" want="$2" out="$WORKD/entrypoint" got hits
  "$DOCKER" run --rm --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint cat "$iid" -- "$(mv_ ENTRYPOINT_PATH)" \
    > "$out" 2>"$WORKD/entrypoint.err" \
    || refuse "candidate entrypoint unreadable in image $iid (cannot prove its content)"
  got="$(sha256sum < "$out" | cut -d' ' -f1)"
  [[ "$got" == "$want" ]] \
    || refuse "entrypoint content hash $got does not match the pinned entrypoint sha256"
  hits="$(grep -Ev '^[[:space:]]*#' "$out" | grep -En '\bchown\b|\bfchown\b|\blchown\b' || true)"
  [[ -z "$hits" ]] \
    || refuse "forbidden chown reachable in the pinned entrypoint (image entrypoints must never change ownership under the mounts): $hits"
  log "ok: entrypoint content pinned ($want) and chown-free"
}

bindings_gate() { # MODE(candidate|rollback) EXPECTED_RUNNING_ID(may be "")
  local mode="$1" expected_running="${2:-}"
  local image image_id sha_key entry_sha compose_sha_key
  if [[ "$mode" == candidate ]]; then
    image="$(mv_ IMAGE)"; image_id="$(mv_ EXPECT_IMAGE_ID)"
    compose_sha_key=COMPOSE_SHA256; entry_sha="$(mv_ ENTRYPOINT_SHA256)"
  else
    image="$(mv_ ROLLBACK_IMAGE)"; image_id="$(mv_ ROLLBACK_IMAGE_ID)"
    compose_sha_key=ROLLBACK_COMPOSE_SHA256; entry_sha="$(mv_ ROLLBACK_ENTRYPOINT_SHA256)"
  fi

  # helper set first: nothing runs before this scan passes
  scan_no_ownership_change "$DRAIN" "$REHARDEN" \
    "$HELPERS_DIR/pre-restart-check.sh" "$HELPERS_DIR/effective-build-check.sh" \
    "$HELPERS_DIR/sandbox-canary.sh" "$HELPERS_DIR/migration_gate.py" \
    "$HELPERS_DIR/check-plugin-inventory.py"
  log "ok: helper set carries no reachable ownership-change command"

  # compose file identity, then its resolved service
  [[ -f "$(mv_ COMPOSE_FILE)" && ! -L "$(mv_ COMPOSE_FILE)" ]] \
    || refuse "compose file missing or a symlink: $(mv_ COMPOSE_FILE)"
  local compose_sha; compose_sha="$(sha256sum < "$(mv_ COMPOSE_FILE)" | cut -d' ' -f1)"
  [[ "$compose_sha" == "$(mv_ "$compose_sha_key")" ]] \
    || refuse "compose file sha256 $compose_sha does not match the pinned $compose_sha_key"
  local cfg svc_image svc_user dests dest_hash
  cfg="$("$DOCKER" compose -f "$(mv_ COMPOSE_FILE)" config --format json 2>/dev/null)" \
    || refuse "compose config unreadable for $(mv_ COMPOSE_FILE)"
  svc_image="$(jq -r --arg s "$(mv_ SERVICE)" '.services[$s].image // empty' <<<"$cfg" 2>/dev/null)" \
    || refuse "compose service $(mv_ SERVICE) unreadable"
  [[ "$svc_image" == "$image" ]] \
    || refuse "compose service $(mv_ SERVICE) resolves to image '$svc_image', want pinned '$image'"
  svc_user="$(jq -r --arg s "$(mv_ SERVICE)" '.services[$s].user // ""' <<<"$cfg" 2>/dev/null)"
  [[ "$svc_user" == "$expect_user" ]] \
    || refuse "compose service user '$svc_user' does not match the pinned non-root service user '$expect_user'"
  dests="$(jq -r --arg s "$(mv_ SERVICE)" '(.services[$s].volumes // [])[] | (.target // error("mount without target"))' <<<"$cfg" 2>/dev/null | sort)" \
    || refuse "compose mount table unreadable for service $(mv_ SERVICE)"
  if [[ -n "$dests" ]]; then dest_hash="$(printf '%s\n' "$dests" | sha256sum | cut -d' ' -f1)"
  else dest_hash="$(printf '' | sha256sum | cut -d' ' -f1)"; fi
  [[ "$dest_hash" == "$(mv_ MOUNT_DESTINATIONS_SHA256)" ]] \
    || refuse "compose mount destinations do not match the pinned mount binding"
  log "ok: compose file, service image, service user and mount destinations pinned"

  # image identity: id, entrypoint argv, user, revision label (never alone)
  local got_id cfgline entry_json image_user revision want_json
  got_id="$("$DOCKER" image inspect -f '{{.Id}}' "$image" 2>/dev/null)" || refuse "candidate image unreadable: $image"
  [[ "$got_id" == "$image_id" ]] \
    || refuse "image $image resolves to $got_id, not the pinned image id $image_id"
  cfgline="$("$DOCKER" image inspect -f '{{json .Config.Entrypoint}}|{{.Config.User}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null)" \
    || refuse "image config unreadable: $image"
  IFS='|' read -r entry_json image_user revision <<<"$cfgline"
  want_json="$(jq -ce . <<<"$(mv_ EXPECT_ENTRYPOINT_JSON)" 2>/dev/null)"
  [[ "$(jq -ce . <<<"$entry_json" 2>/dev/null)" == "$want_json" ]] \
    || refuse "image entrypoint $entry_json does not match the pinned entrypoint binding"
  [[ "$image_user" == "$expect_user" ]] \
    || refuse "image user '$image_user' does not match the pinned non-root service user '$expect_user'"
  if [[ "$mode" == candidate ]]; then
    if [[ -n "$revision" && "$revision" != "$(mv_ EXPECT_COMMIT)" ]]; then
      refuse "image revision label $revision does not match the pinned source commit $(mv_ EXPECT_COMMIT)"
    fi
    [[ -n "$revision" ]] || log "note: image carries no revision label; source commit is proven by health + effective-build pins"
  fi
  entrypoint_gate "$image_id" "$entry_sha"
  log "ok: image identity pinned ($image_id)"

  # private-overlay host files, when pinned
  local hf hp hh
  for hf in ${host_files[@]+"${host_files[@]}"}; do
    hp="${hf%:*}"; hh="${hf##*:}"
    [[ -f "$hp" && ! -L "$hp" ]] || refuse "pinned host overlay file missing or a symlink: $hp"
    [[ "$(sha256sum < "$hp" | cut -d' ' -f1)" == "$hh" ]] || refuse "pinned host overlay file hash mismatch: $hp"
  done
  ((${#host_files[@]} == 0)) || log "ok: ${#host_files[@]} pinned host overlay file(s) verify"

  # the running container is the image this phase expects (rollback binding
  # for a fresh activation is the ACTUAL pre-change image, read live)
  if [[ -n "$expected_running" ]]; then
    local running
    running="$("$DOCKER" inspect -f '{{.Image}}' "$(mv_ SERVER_CONTAINER)" 2>/dev/null)" \
      || refuse "running server container unreadable: $(mv_ SERVER_CONTAINER)"
    [[ "$running" == "$expected_running" ]] \
      || refuse "running server image $running is not the expected $expected_running (rollback/pre-change binding is read live, never assumed)"
    log "ok: running server is the expected pre-change/rollback image"
  fi
  # the rollback ref must resolve locally to its pinned id before anything
  # is drained, so rollback never depends on a pull
  local rb_id
  rb_id="$("$DOCKER" image inspect -f '{{.Id}}' "$(mv_ ROLLBACK_IMAGE)" 2>/dev/null)" \
    || refuse "rollback image unreadable: $(mv_ ROLLBACK_IMAGE)"
  [[ "$rb_id" == "$(mv_ ROLLBACK_IMAGE_ID)" ]] \
    || refuse "rollback image $(mv_ ROLLBACK_IMAGE) resolves to $rb_id, not its pinned rollback id"
}

baseline_gate() {
  bash "$REHARDEN" || fail "hardening baseline FAILED (verify-only; nothing was repaired). Resolve out of band, then rerun"
  log "ok: hardening baseline verified"
}

runtime_probe() { # after a recreate: PID 1 is the pinned non-root uid, umask 0077
  local status uid1 umask1
  status="$("$DOCKER" exec "$(mv_ SERVER_CONTAINER)" cat /proc/1/status 2>/dev/null)" \
    || return 1
  uid1="$(awk '$1 == "Uid:" { print $2 }' <<<"$status")"
  umask1="$(awk '$1 == "Umask:" { print $2 }' <<<"$status")"
  [[ "$uid1" =~ ^[0-9]+$ && "$uid1" != 0 ]] || { log "FAIL: PID 1 runs as uid '$uid1' (root entrypoint behaviour is forbidden)"; return 1; }
  if [[ "$user_uid_part" =~ ^[0-9]+$ && "$uid1" != "$user_uid_part" ]]; then
    log "FAIL: PID 1 uid $uid1 does not match the pinned service user $expect_user"; return 1
  fi
  [[ "$umask1" == 0077 ]] || { log "FAIL: PID 1 umask is '$umask1', want 0077"; return 1; }
  log "ok: PID 1 uid $uid1, umask 0077"
}

rollback_hint() {
  printf 'next (owned rollback): point the compose file at %s, then: activation.sh rollback --manifest %s --state-dir %s --live\n' \
    "$(mv_ ROLLBACK_IMAGE)" "$MANIFEST" "$STATE_DIR" >&2
  printf 'next (owned abort, pre-restart only): activation.sh abort --manifest %s --state-dir %s --live\n' \
    "$MANIFEST" "$STATE_DIR" >&2
}
run_drain() { # SUBCOMMAND [args...] — child of the lock holder (fd inherited)
  bash "$DRAIN" "$@" --state-dir "$STATE_DIR" --live
}
phase() { # DESC SUBCOMMAND [args...]
  local desc="$1"; shift
  log "phase: $desc"
  if ! run_drain "$@"; then
    printf 'FAIL: %s: phase %s failed; the drain/hold is preserved exactly as drain.sh left it (nothing cancelled, nothing undrained)\n' "$ME" "$desc" >&2
    rollback_hint
    exit 1
  fi
}

run_phases() {
  have undrained_at && refuse "state $STATE_DIR already undrained; an activation never reruns over a completed state"
  have drain-started || phase drain drain
  have drained_at || phase wait wait --poll-secs "$(mv_ POLL_SECS)" \
    --stable-polls "$(mv_ STABLE_POLLS)" --max-wait-secs "$(mv_ MAX_WAIT_SECS)"
  have backup_at || phase backup backup
  if have restart_started_at && ! have restarted_at && ! have rolledback_at; then
    printf 'FAIL: %s: restart was interrupted mid-recreate; not guessing. Inspect, then use the owned rollback\n' "$ME" >&2
    rollback_hint
    exit 1
  fi
  if ! have restarted_at; then
    window_gate; pressure_gate
    local -a rargs=(--image "$(mv_ IMAGE)" --expect-image-id "$(mv_ EXPECT_IMAGE_ID)"
      --expect-commit "$(mv_ EXPECT_COMMIT)" --expect-build-marker "$(mv_ BUILD_MARKER)"
      --health-timeout-secs "$(mv_ HEALTH_TIMEOUT_SECS)"
      --expect-file "$(mv_ ENTRYPOINT_PATH):$(mv_ ENTRYPOINT_SHA256)")
    local xf t
    for xf in "${EXPECT_FILE_LIST[@]}"; do rargs+=(--expect-file "$xf"); done
    for t in "${MIGRATION_LIST[@]}"; do rargs+=(--require "$t"); done
    [[ -n "$(mv_ PLUGIN_INVENTORY)" ]] && rargs+=(--plugin-inventory "$(mv_ PLUGIN_INVENTORY)")
    phase restart restart "${rargs[@]}"
  fi
  runtime_probe || { printf 'FAIL: %s: runtime probe failed under the hold\n' "$ME" >&2; rollback_hint; exit 1; }
  have verified_at || phase verify verify --expect-commit "$(mv_ EXPECT_COMMIT)"
  # undrain recreates the server too; a long drain can cross into the
  # blocked window, so it is re-judged, never inherited from the start.
  window_gate
  phase undrain undrain
  log "activation: done (undrained on $(mv_ EXPECT_IMAGE_ID))"
}

case "$CMD" in
  preflight)
    bindings_gate candidate "$(mv_ ROLLBACK_IMAGE_ID)"
    window_gate; pressure_gate; baseline_gate
    log "preflight: PASS (no drain started, lock released on exit)"
    ;;
  run)
    [[ ! -e "$STATE_DIR" && ! -L "$STATE_DIR" ]] \
      || refuse "state dir already exists: $STATE_DIR (use resume; an activation never reuses state)"
    bindings_gate candidate "$(mv_ ROLLBACK_IMAGE_ID)"
    window_gate; pressure_gate; baseline_gate
    write_owner
    run_phases
    ;;
  resume)
    check_owner
    expected_running="$(mv_ ROLLBACK_IMAGE_ID)"
    have restarted_at && expected_running="$(mv_ EXPECT_IMAGE_ID)"
    have rolledback_at && expected_running="$(mv_ ROLLBACK_IMAGE_ID)"
    bindings_gate candidate "$expected_running"
    run_phases
    ;;
  rollback)
    check_owner
    have undrained_at && refuse "state $STATE_DIR already undrained; rollback of a completed activation needs a fresh drain"
    have restart_started_at \
      || refuse "no restart was started in $STATE_DIR; use abort (nothing to roll back)"
    bindings_gate rollback ""
    # A previous rollback invocation may have completed the rollback and
    # died before undrain: drain.sh refuses a second rollback over
    # rolledback_at, so resume at the probe + undrain instead.
    have rolledback_at \
      || phase rollback rollback --image "$(mv_ ROLLBACK_IMAGE)" --expect-image-id "$(mv_ ROLLBACK_IMAGE_ID)"
    runtime_probe || { printf 'FAIL: %s: rollback runtime probe failed under the hold\n' "$ME" >&2; exit 1; }
    window_gate
    phase undrain undrain
    log "rollback: done (undrained on the pinned pre-change image $(mv_ ROLLBACK_IMAGE_ID))"
    ;;
  abort)
    check_owner
    if have restart_started_at; then
      printf 'REFUSED: %s: abort is pre-restart only; the restart already began in %s\n' "$ME" "$STATE_DIR" >&2
      rollback_hint
      exit 2
    fi
    phase abort abort
    log "abort: done (drain lifted by its owner)"
    ;;
esac
exit 0
