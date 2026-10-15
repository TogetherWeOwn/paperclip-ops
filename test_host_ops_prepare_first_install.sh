#!/usr/bin/env bash
# Offline gate for host-ops/prepare-first-install.sh.
#
# Hermetic by construction: getent/id/passwd/systemctl/useradd/groupadd are
# shell-function stubs backed by per-test state files, so no host account,
# group, key or service is ever touched. mkdir/chown/chmod/stat are REAL and
# run against temp roots, so directory modes, adoption metadata, the journal
# write and rollback text go through the shipped code. Invoked nested from
# test_host_ops_install.sh, like the other host-ops contract suites.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REAL_UID=$(command id -u)
REAL_GID=$(command id -g)
# shellcheck source=host-ops/prepare-first-install.sh
source "$ROOT/host-ops/prepare-first-install.sh"

ALL_TMP=""
track_tmp() { ALL_TMP="$ALL_TMP $1"; }
trap 'rm -rf $ALL_TMP' EXIT
# Failure counting is file-backed: every test body runs in a subshell, so a
# shell variable increment would be lost with the subshell.
FAIL_FILE=$(mktemp)
track_tmp "$FAIL_FILE"
: > "$FAIL_FILE"
ok() { printf '  ok: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; echo x >> "$FAIL_FILE"; }

CAPTURE_OUT=""
CAPTURE_RC=0
run_sub() {
  set +e
  CAPTURE_OUT=$("$@" 2>&1)
  CAPTURE_RC=$?
  set -e
}

# Runs with the production errexit mode. run_sub clears errexit, which hides a
# function that returns 1 on the normal fresh-host path and ends main silently.
run_strict() {
  CAPTURE_RC=0
  CAPTURE_OUT=$( (set -Eeuo pipefail; "$@") 2>&1 ) || CAPTURE_RC=$?
}

expect_refusal() {
  local label="$1" expected="$2"
  shift 2
  run_sub "$@"
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"$expected"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$CAPTURE_RC output=$CAPTURE_OUT)"
  fi
}

# ---- stub world -----------------------------------------------------------
NSS_DIR=""
CALL_LOG=""
STUB_SYSTEMCTL_SHOW="LoadState=not-found
ActiveState=inactive
UnitFileState="
NEXT_UID=0
NEXT_GID=0

setup_world() {
  NSS_DIR=$(mktemp -d)
  track_tmp "$NSS_DIR"
  CALL_LOG="$NSS_DIR/calls"
  : > "$NSS_DIR/groups"
  : > "$NSS_DIR/passwd"
  printf 'root:x:0:0:root:/root:/bin/bash\n' >> "$NSS_DIR/passwd"
  : > "$CALL_LOG"
  NSSWITCH_CONF="$NSS_DIR/nsswitch.conf"
  printf 'passwd: files\ngroup: files\ninitgroups: files\n' > "$NSSWITCH_CONF"
  PREPARE_JOURNAL_OWNER_UID=$REAL_UID
  PREPARE_JOURNAL_OWNER_GID=$REAL_GID
  STUB_SYSTEMCTL_SHOW="LoadState=not-found
ActiveState=inactive
UnitFileState="
}

seed_group() { printf '%s:x:%s:%s\n' "$1" "$2" "$3" >> "$NSS_DIR/groups"; }
seed_passwd() { printf '%s:x:%s:%s::%s:%s\n' "$1" "$2" "$3" "$4" "$5" >> "$NSS_DIR/passwd"; }

getent() {
  # `getent passwd` with no user lists the whole table (used by the
  # primary-group holder check). An empty table lists as empty success, never
  # "unreadable": there are simply no holders to find.
  if [[ "${1:-}" == passwd && -z "${2:-}" ]]; then
    cat "$NSS_DIR/passwd" 2>/dev/null || true
    return 0
  fi
  if [[ "${1:-}" == group && -z "${2:-}" ]]; then
    cat "$NSS_DIR/groups" 2>/dev/null || true
    return 0
  fi
  case "$1:$2" in
    group:*) grep -E "^$2:" "$NSS_DIR/groups" 2>/dev/null || return 2;;
    passwd:*) grep -E "^$2:" "$NSS_DIR/passwd" 2>/dev/null || return 2;;
    *) return 2;;
  esac
}
id() {
  if [[ "${1:-}" == -nG ]]; then
    local user="${2:-}" pgid gname gn gm
    pgid=$(grep -E "^$user:" "$NSS_DIR/passwd" | head -n 1 | cut -d: -f4) || return 1
    gname=$(awk -F: -v g="$pgid" '$3 == g {print $1; exit}' "$NSS_DIR/groups")
    printf '%s' "$gname"
    while IFS=: read -r gn _ _ gm; do
      case ",$gm," in *",$user,"*) printf ' %s' "$gn";; esac
    done < "$NSS_DIR/groups"
    printf '\n'
    return 0
  fi
  if [[ "${1:-}" == "$READER_USER" ]]; then
    grep -qE "^$READER_USER:" "$NSS_DIR/passwd" 2>/dev/null
  else
    return 1
  fi
}
passwd() {
  echo "passwd $*" >> "$CALL_LOG"
  if [[ "${1:-}" == -S ]]; then
    printf '%s L 2026-10-05 0 99999 7 -1\n' "${2:-}"
  fi
  return 0
}
systemctl() {
  if [[ "$1" == show ]]; then
    printf '%s\n' "$STUB_SYSTEMCTL_SHOW"
    return 0
  fi
  return 2
}
groupadd() {
  echo "groupadd $*" >> "$CALL_LOG"
  local name="${@: -1}" gid
  # Without uid 0 only the current gid is chown-able, so every fixture group
  # carries it unless a test pins the id; as root the stub allocates distinct
  # system ids instead.
  if [[ $REAL_UID -eq 0 ]]; then gid=$NEXT_GID; NEXT_GID=$((NEXT_GID + 1)); else gid=${NEW_GID_FIXTURE:-$REAL_GID}; fi
  printf '%s:x:%s:\n' "$name" "$gid" >> "$NSS_DIR/groups"
}
useradd() {
  echo "useradd $*" >> "$CALL_LOG"
  local pg="" sup="" home="" shell="" name=""
  local args=("$@") i=0
  while ((i < ${#args[@]})); do
    case "${args[$i]}" in
      -g) pg="${args[$((i + 1))]}"; i=$((i + 2));;
      -G) sup="${args[$((i + 1))]}"; i=$((i + 2));;
      -d) home="${args[$((i + 1))]}"; i=$((i + 2));;
      -s) shell="${args[$((i + 1))]}"; i=$((i + 2));;
      -*) i=$((i + 1));;
      *) name="${args[$i]}"; i=$((i + 1));;
    esac
  done
  local pgid supgid uid
  pgid=$(grep -E "^$pg:" "$NSS_DIR/groups" | cut -d: -f3)
  supgid=$(grep -E "^$sup:" "$NSS_DIR/groups" | cut -d: -f3)
  if [[ $REAL_UID -eq 0 ]]; then uid=$NEXT_UID; NEXT_UID=$((NEXT_UID + 1)); else uid=${NEW_UID_FIXTURE:-$REAL_UID}; fi
  printf '%s:x:%s:%s::%s:%s\n' "$name" "$uid" "$pgid" "$home" "$shell" >> "$NSS_DIR/passwd"
  # -G membership: append to the supplemental group's member list, never evict.
  if grep -qE "^$sup:[^:]*:[^:]*:$" "$NSS_DIR/groups"; then
    sed -i "s/^\($sup:[^:]*:[^:]*:\)$/\1$name/" "$NSS_DIR/groups"
  else
    sed -i "s/^\($sup:[^:]*:[^:]*:.*\)$/\1,$name/" "$NSS_DIR/groups"
  fi
}
validate_service_identity() {
  echo "validate_service_identity" >> "$CALL_LOG"
  return 0
}

# Membership and primary-holder helpers come from the shipped install-lib, not
# local copies: sourcing here keeps the preparation contract bound to the exact
# implementation the installer enforces. The pipeline stubs
# validate_service_identity above (so the full runs below do not need system
# ids), but validate_created_identity and the classification under test call
# the real helpers.
# shellcheck source=host-ops/install-lib.sh
source "$ROOT/host-ops/install-lib.sh"
# Re-assert the pipeline stub after sourcing: install-lib ships the real
# validator, but full runs drive main with fixture gids that cannot satisfy
# its system-id checks without uid 0.
validate_service_identity() {
  echo "validate_service_identity" >> "$CALL_LOG"
  return 0
}

# Temp-root path overrides. Production always uses the real absolute paths;
# these reassignments happen only in test subshells, never via environment.
point_at_tmp() {
  TMP_ROOT=$(mktemp -d)
  track_tmp "$TMP_ROOT"
  mkdir -p "$TMP_ROOT/usr/local/libexec" "$TMP_ROOT/usr/local/share/doc" \
    "$TMP_ROOT/etc" "$TMP_ROOT/var/lib" "$TMP_ROOT/var/log" "$TMP_ROOT/var/spool"
  DIR_LIBEXEC="$TMP_ROOT/usr/local/libexec/paperclip-host-ops"
  DIR_DOC="$TMP_ROOT/usr/local/share/doc/paperclip-host-ops"
  DIR_ETC="$TMP_ROOT/etc/paperclip-host-ops"
  DIR_STATE="$TMP_ROOT/var/lib/paperclip-host-ops"
  DIR_KEYS="$TMP_ROOT/var/lib/paperclip-host-ops/keys"
  DIR_LOG="$TMP_ROOT/var/log/paperclip-host-ops"
  SPOOL_ROOT="$TMP_ROOT/var/spool/paperclip-host-ops"
  JOURNAL_ROOT="$TMP_ROOT/installer-journal"
  PRIVATE_KEY="$TMP_ROOT/var/lib/paperclip-host-ops/keys/response-signing.pem"
  PUBLIC_KEY="$TMP_ROOT/var/lib/paperclip-host-ops/keys/response-signing.pub.pem"
  # Ownership fixtures always use the current user: chown to anyone else needs
  # uid 0, which CI does not have. The exact production ids (system range) are
  # pinned by the classification tests above, which need no syscalls.
  OWNER_UID=$REAL_UID
  OWNER_GID=$REAL_GID
  NEXT_UID=$REAL_UID
  NEXT_GID=$REAL_GID
}

# Pipeline tests below drive main with a seeded classification outcome: the
# real decision table is covered by the classification tests above, and the
# fixture ids cannot satisfy its system-gid rule without uid 0. The stub
# records its position so the preflight-before-mutation order is still pinned,
# and mirrors the shipped expected-list gate so full runs cannot bypass it.
STUB_ADOPT=false
STUB_MEMBERS=""
stub_classify_for_pipeline() {
  classify_first_install_identity() {
    echo "classify_first_install_identity" >> "$CALL_LOG"
    if [[ "$STUB_ADOPT" == true ]]; then
      if [[ -n "$STUB_MEMBERS" ]]; then
        [[ "$EXPECT_SUPPLIED" == true ]] || {
          printf 'REFUSED: pre-existing requests group has members (%s); pass --expect-requests-members with the exact authorized list recorded in the install packet\n' "$STUB_MEMBERS" >&2
          exit 2
        }
        csv_set_equals "$STUB_MEMBERS" "$EXPECT_REQUESTS_MEMBERS" || {
          printf 'REFUSED: pre-existing requests group members (%s) do not match the expected list (%s); separate review required\n' "$STUB_MEMBERS" "$EXPECT_REQUESTS_MEMBERS" >&2
          exit 2
        }
      else
        if [[ "$EXPECT_SUPPLIED" == true && -n "$EXPECT_REQUESTS_MEMBERS" ]]; then
          printf 'REFUSED: pre-existing requests group is empty but --expect-requests-members names (%s); separate review required\n' "$EXPECT_REQUESTS_MEMBERS" >&2
          exit 2
        fi
      fi
      ADOPT_REQUESTS=true
      REQUESTS_GID=$REAL_GID
      REQUESTS_MEMBERS=$STUB_MEMBERS
    else
      if [[ "$EXPECT_SUPPLIED" == true && -n "$EXPECT_REQUESTS_MEMBERS" ]]; then
        printf 'REFUSED: --expect-requests-members names (%s) but no pre-existing requests group exists; separate review required\n' "$EXPECT_REQUESTS_MEMBERS" >&2
        exit 2
      fi
      ADOPT_REQUESTS=false
      REQUESTS_GID=""
      REQUESTS_MEMBERS=""
    fi
  }
  # Pipeline fixtures run as the current gid without uid 0, which can never
  # satisfy the real system-id checks: stub the preparation-time checkpoint
  # here (the direct unit tests above already drive the real function with
  # system-id fixtures). The member-append and journal-pinning assertions on
  # the full runs below still go through the shipped useradd/journal code.
  validate_created_identity() {
    echo "validate_created_identity" >> "$CALL_LOG"
    return 0
  }
}

# Wrapper so classification results survive the capture subshell as text.
report_identity() {
  classify_first_install_identity
  printf 'adopt=%s gid=%s members=%s' "$ADOPT_REQUESTS" "$REQUESTS_GID" "$REQUESTS_MEMBERS"
}

# ---- identity classification ----------------------------------------------
(
  setup_world
  run_sub report_identity
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == "adopt=false gid= members=" ]]; then
    ok "wholly absent identity classifies without adoption"
  else
    fail "wholly absent identity classifies without adoption (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_group paperclip-host-requests 986 ""
  run_sub report_identity
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == "adopt=true gid=986 members=" ]]; then
    ok "pre-existing empty system requests group (gid 986) is adoptable"
  else
    fail "pre-existing empty system requests group (gid 986) is adoptable (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_group paperclip-host-requests 986 "ubuntu"
  expect_refusal "requests group with members refuses without --expect-requests-members" "pass --expect-requests-members" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 986 "ubuntu"
  EXPECT_SUPPLIED=true
  EXPECT_REQUESTS_MEMBERS="ubuntu"
  run_sub report_identity
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == "adopt=true gid=986 members=ubuntu" ]]; then
    ok "pre-existing requests group with matching --expect-requests-members is adopted with members pinned"
  else
    fail "pre-existing requests group with matching --expect-requests-members is adopted with members pinned (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_group paperclip-host-requests 986 "ubuntu"
  EXPECT_SUPPLIED=true
  EXPECT_REQUESTS_MEMBERS="alice"
  expect_refusal "requests group with mismatched expected list refuses" "do not match the expected list" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 986 "ubuntu,www-data"
  EXPECT_SUPPLIED=true
  EXPECT_REQUESTS_MEMBERS="ubuntu"
  expect_refusal "requests group with extra observed member refuses even with partial expected list" "do not match the expected list" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 986 ""
  EXPECT_SUPPLIED=true
  EXPECT_REQUESTS_MEMBERS="ubuntu"
  expect_refusal "empty group with non-empty expected list refuses" "is empty but --expect-requests-members names" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 986 "ubuntu"
  seed_passwd mallory 1001 986 /home/mallory /bin/bash
  EXPECT_SUPPLIED=true
  EXPECT_REQUESTS_MEMBERS="ubuntu"
  expect_refusal "primary-group holder of the requests gid refuses at classification" "primary group" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 986 "evil;name"
  expect_refusal "requests group with unreadable members refuses" "unreadable member" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-requests 1001 ""
  expect_refusal "non-system requests group refuses" "not a system group" \
    classify_first_install_identity
)

(
  setup_world
  seed_group paperclip-host-reader 991 ""
  expect_refusal "pre-existing reader group refuses" "already exists" \
    classify_first_install_identity
)

(
  setup_world
  printf 'paperclip-host-reader:x:991:991::/var/lib/paperclip-host-ops:/usr/sbin/nologin\n' >> "$NSS_DIR/passwd"
  expect_refusal "pre-existing reader account refuses" "already exists" \
    classify_first_install_identity
)

# ---- preparation-time identity check --------------------------------------
# validate_created_identity runs in main BEFORE the journal exists, using the
# in-memory classification (not the journal). Seed system-id NSS fixtures and
# drive it directly: the pipeline full-runs below cannot use it (their fixture
# gids are the current gid without uid 0), so they stub it there.
seed_created_identity() {
  # seed_created_identity <requests-members-after> <requests-gid> <reader-uid> <reader-gid> <preexisting-members>
  seed_group paperclip-host-reader "$4" ""
  seed_group paperclip-host-requests "$2" "$1"
  printf 'paperclip-host-reader:x:%s:%s::/var/lib/paperclip-host-ops:/usr/sbin/nologin\n' "$3" "$4" >> "$NSS_DIR/passwd"
  REQUESTS_GID="$2"
  REQUESTS_MEMBERS="$5"
  READER_GID="$4"
}

(
  setup_world
  seed_created_identity "ubuntu,paperclip-host-reader" 986 991 991 "ubuntu"
  run_sub validate_created_identity
  if [[ $CAPTURE_RC -eq 0 ]]; then
    ok "created check passes with preserved ubuntu plus the reader"
  else
    fail "created check passes with preserved ubuntu plus the reader (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_created_identity "paperclip-host-reader" 986 991 991 ""
  run_sub validate_created_identity
  if [[ $CAPTURE_RC -eq 0 ]]; then
    ok "created check passes with only the reader and no pre-existing members"
  else
    fail "created check passes with only the reader and no pre-existing members (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_created_identity "ubuntu,mallory,paperclip-host-reader" 986 991 991 "ubuntu"
  expect_refusal "created check refuses a member gained during preparation" "gained member mallory" \
    validate_created_identity
)

(
  setup_world
  seed_created_identity "ubuntu,paperclip-host-reader" 986 991 991 "ubuntu,alice"
  expect_refusal "created check refuses a pre-existing member lost during preparation" "lost pre-existing member alice" \
    validate_created_identity
)

(
  setup_world
  seed_created_identity "ubuntu" 986 991 991 "ubuntu"
  expect_refusal "created check refuses a group missing the reader" "does not contain the broker reader" \
    validate_created_identity
)

(
  setup_world
  seed_created_identity "ubuntu,paperclip-host-reader" 986 991 991 "ubuntu"
  seed_passwd mallory 1001 986 /home/mallory /bin/bash
  expect_refusal "created check refuses a primary-group holder of the requests gid" "primary group" \
    validate_created_identity
)

(
  setup_world
  seed_created_identity "ubuntu,paperclip-host-reader" 986 991 991 "ubuntu"
  seed_passwd mallory 1001 991 /home/mallory /bin/bash
  expect_refusal "created check refuses a primary-group holder of the reader gid" "holds paperclip-host-reader as its primary group" \
    validate_created_identity
)

# ---- exact set equality and primary-holder enumeration ---------------------
expect_set() {
  local want="$1" left="$2" right="$3" label="$4" equal=0
  if csv_set_equals "$left" "$right"; then equal=1; fi
  if [[ "$equal" == "$want" ]]; then ok "$label"; else fail "$label (equal=$equal)"; fi
}

(
  expect_set 1 "" "" "empty expected list equals an empty group"
  expect_set 0 "" "ubuntu" "empty expected list differs from a populated group"
  expect_set 1 "ubuntu,alice" "alice,ubuntu" "expected list matches regardless of order"
  expect_set 0 "ubuntu,ubuntu" "ubuntu" "a duplicated member does not match a single member"
  expect_set 0 "a,a,b" "a,b,b" "duplicates with different counts never match"
  expect_set 0 "a,a" "a,b" "a duplicated name never matches a different name"
)

(
  setup_world
  : > "$NSS_DIR/passwd"
  expect_refusal "empty passwd enumeration refuses" "enumerated empty" \
    check_no_primary_holders 986 paperclip-host-requests
)

(
  setup_world
  getent() { return 2; }
  expect_refusal "unreadable passwd table refuses" "passwd table is unreadable" \
    check_no_primary_holders 986 paperclip-host-requests
)

(
  setup_world
  printf 'garbled entry\n' >> "$NSS_DIR/passwd"
  expect_refusal "malformed passwd entry refuses" "unreadable entry" \
    check_no_primary_holders 986 paperclip-host-requests
)

(
  setup_world
  seed_passwd paperclip-host-reader 991 991 /var/lib/paperclip-host-ops /usr/sbin/nologin
  run_sub check_no_primary_holders 991 paperclip-host-reader
  if [[ $CAPTURE_RC -eq 0 ]]; then
    ok "the reader's own primary group is not a foreign holder"
  else
    fail "the reader's own primary group is not a foreign holder (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  seed_passwd mallory 1001 991 /home/mallory /bin/bash
  expect_refusal "primary-group holder of the reader gid refuses in the shared check" "holds paperclip-host-reader as its primary group" \
    check_no_primary_holders 991 paperclip-host-reader
)

# ---- service / keys / paths ------------------------------------------------
(
  setup_world
  absent_root=$(mktemp -d)
  track_tmp "$absent_root"
  PRIVATE_KEY="$absent_root/keys/response-signing.pem"
  PUBLIC_KEY="$absent_root/keys/response-signing.pub.pem"
  run_strict check_keys_absent
  keys_rc=$CAPTURE_RC
  run_strict check_path_absent "$absent_root/etc"
  path_rc=$CAPTURE_RC
  if [[ $keys_rc -eq 0 && $path_rc -eq 0 ]]; then
    ok "absent keys and protected paths pass under errexit"
  else
    fail "absent keys and protected paths pass under errexit (keys rc=$keys_rc path rc=$path_rc out=$CAPTURE_OUT)"
  fi
  mkdir -p "$absent_root/keys"
  printf 'x' > "$PRIVATE_KEY"
  run_strict check_keys_absent
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"signing key already exists"* ]]; then
    ok "present signing key refuses under errexit"
  else
    fail "present signing key refuses under errexit (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)
(
  setup_world
  STUB_SYSTEMCTL_SHOW="LoadState=loaded
ActiveState=inactive
UnitFileState=disabled"
  expect_refusal "loaded service refuses preparation" "already exists" \
    check_service_absent
)

(
  setup_world
  STUB_SYSTEMCTL_SHOW="LoadState=not-found
ActiveState=active
UnitFileState="
  expect_refusal "active state without a unit file refuses" "already exists" \
    check_service_absent
)

(
  setup_world
  run_sub check_service_absent
  if [[ $CAPTURE_RC -eq 0 ]]; then ok "absent service passes preflight"; else fail "absent service passes preflight (rc=$CAPTURE_RC)"; fi
)

(
  setup_world
  point_at_tmp
  mkdir -p "$(dirname "$PRIVATE_KEY")"
  touch "$PRIVATE_KEY"
  expect_refusal "existing private key refuses" "already exists" check_keys_absent
)

(
  setup_world
  point_at_tmp
  mkdir -p "$(dirname "$PUBLIC_KEY")"
  touch "$PUBLIC_KEY"
  expect_refusal "existing public key refuses" "already exists" check_keys_absent
)

(
  setup_world
  point_at_tmp
  mkdir -p "$DIR_ETC"
  expect_refusal "existing control directory refuses" "already exists" \
    check_path_absent "$DIR_ETC"
)

# ---- spool planner ----------------------------------------------------------
(
  setup_world
  point_at_tmp
  collect_spool_plan "$SPOOL_ROOT"
  if [[ ${#CREATE_DIRS[@]} -eq 7 && ${#ADOPT_DIRS[@]} -eq 0 ]]; then
    ok "fresh spool plans seven creations"
  else
    fail "fresh spool plans seven creations (create=${#CREATE_DIRS[@]} adopt=${#ADOPT_DIRS[@]})"
  fi
)

(
  setup_world
  point_at_tmp
  mkdir -p "$SPOOL_ROOT" "$SPOOL_ROOT/requests"
  collect_spool_plan "$SPOOL_ROOT"
  if [[ ${#CREATE_DIRS[@]} -eq 5 && ${#ADOPT_DIRS[@]} -eq 2 ]]; then
    ok "empty existing spool dirs are adopted"
  else
    fail "empty existing spool dirs are adopted (create=${#CREATE_DIRS[@]} adopt=${#ADOPT_DIRS[@]})"
  fi
)

(
  setup_world
  point_at_tmp
  mkdir -p "$SPOOL_ROOT"
  touch "$SPOOL_ROOT/request.bundle"
  expect_refusal "spool tree holding files refuses" "existing data" \
    collect_spool_plan "$SPOOL_ROOT"
)

(
  setup_world
  point_at_tmp
  mkdir -p "$SPOOL_ROOT"
  ln -s /tmp "$SPOOL_ROOT/evil"
  expect_refusal "spool tree holding a symlink refuses" "symlink" \
    collect_spool_plan "$SPOOL_ROOT"
)

(
  setup_world
  point_at_tmp
  mkdir -p "$SPOOL_ROOT/surprise"
  expect_refusal "unexpected spool subdirectory refuses" "unexpected directory" \
    collect_spool_plan "$SPOOL_ROOT"
)

# ---- real directory primitives ----------------------------------------------
(
  setup_world
  point_at_tmp
  make_dir "$TMP_ROOT/probe" 750 "$REAL_UID" "$REAL_GID"
  if [[ $(stat -c '%u %g %a' "$TMP_ROOT/probe") == "$REAL_UID $REAL_GID 750" ]]; then
    ok "make_dir creates the exact mode and ownership"
  else
    fail "make_dir creates the exact mode and ownership"
  fi
)

(
  setup_world
  point_at_tmp
  mkdir -p "$TMP_ROOT/adopted"
  ADOPT_PRIOR=()
  adopt_dir "$TMP_ROOT/adopted" 700 "$REAL_UID" "$REAL_GID"
  if [[ "${ADOPT_PRIOR[0]}" == "$TMP_ROOT/adopted:"* ]]; then
    ok "adopt_dir records prior metadata for rollback"
  else
    fail "adopt_dir records prior metadata for rollback (${ADOPT_PRIOR[*]})"
  fi
)

# The script must validate through the shipped install-lib implementation,
# not a re-implemented copy: compare against a clean load of install-lib.sh.
(
  unset -f validate_service_identity classify_service_identity
  source "$ROOT/host-ops/prepare-first-install.sh"
  ensure_validator
  shipped=$(declare -f validate_service_identity)
  fresh=$(bash -c 'source "$1"; declare -f validate_service_identity' _ "$ROOT/host-ops/install-lib.sh")
  if [[ "$shipped" == "$fresh" ]]; then
    ok "preparation validates through the shipped install-lib implementation"
  else
    fail "preparation validates through the shipped install-lib implementation"
  fi
)

# ---- full runs ----------------------------------------------------------------
(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  run_strict main
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == *"PREPARED first-install identity"* ]]; then
    ok "fresh full run completes under errexit"
  else
    fail "fresh full run completes under errexit (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  mkdir "$JOURNAL_ROOT"
  chmod 0755 "$JOURNAL_ROOT"
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"unexpected owner or mode"* ]] && ! grep -q groupadd "$CALL_LOG"; then
    ok "existing journal root with the wrong mode refuses before any mutation"
  else
    fail "existing journal root with the wrong mode refuses before any mutation (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  chmod 0700 "$JOURNAL_ROOT"
  OWNER_UID=$((REAL_UID + 1))
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"unexpected owner or mode"* ]] && ! grep -q groupadd "$CALL_LOG"; then
    ok "existing journal root owned by another user refuses before any mutation"
  else
    fail "existing journal root owned by another user refuses before any mutation (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  OWNER_UID=$REAL_UID
  OWNER_GID=$((REAL_GID + 1))
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"unexpected owner or mode"* ]] && ! grep -q groupadd "$CALL_LOG"; then
    ok "existing journal root in another group refuses before any mutation"
  else
    fail "existing journal root in another group refuses before any mutation (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  run_sub main
  if [[ $CAPTURE_RC -ne 0 ]]; then
    fail "fresh full run succeeds (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  else
    ok "fresh full run succeeds"
  fi
  classify_line=$(grep -n "classify_first_install_identity" "$CALL_LOG" | head -1 | cut -d: -f1 || true)
  first_group=$(grep -n "groupadd" "$CALL_LOG" | head -1 | cut -d: -f1 || true)
  if [[ -n "$classify_line" && -n "$first_group" && $classify_line -lt $first_group ]]; then
    ok "full run classifies before mutating"
  else
    fail "full run classifies before mutating"
  fi
  for needle in "groupadd -r paperclip-host-reader" "groupadd -r paperclip-host-requests" \
    "validate_service_identity" "PREPARE JOURNAL:" "NEXT: create the signing key"; do
    if grep -qF "$needle" "$CALL_LOG" 2>/dev/null || grep -qF "$needle" <<<"$CAPTURE_OUT"; then
      ok "full run emits: $needle"
    else
      fail "full run emits: $needle"
    fi
  done
  if grep -qF "useradd -r -g paperclip-host-reader -G paperclip-host-requests -d /var/lib/paperclip-host-ops -s /usr/sbin/nologin -M paperclip-host-reader" "$CALL_LOG"; then
    ok "full run creates the exact documented account"
  else
    fail "full run creates the exact documented account ($(cat "$CALL_LOG"))"
  fi
  if grep -qF "passwd -l paperclip-host-reader" "$CALL_LOG"; then
    ok "full run locks the account credentials"
  else
    fail "full run locks the account credentials"
  fi
  first_group=$(grep -n "groupadd" "$CALL_LOG" | head -1 | cut -d: -f1 || true)
  validator_line=$(grep -n "validate_service_identity" "$CALL_LOG" | head -1 | cut -d: -f1 || true)
  if [[ -n "$first_group" && -n "$validator_line" && $first_group -lt $validator_line ]]; then
    ok "full run creates accounts before validating them"
  else
    fail "full run creates accounts before validating them"
  fi
  [[ $(stat -c '%a' "$DIR_KEYS") == 700 ]] && ok "keys directory is 0700" || fail "keys directory is 0700"
  [[ $(stat -c '%a' "$SPOOL_ROOT/requests") == 3730 ]] && ok "requests queue is 3730" || fail "requests queue is 3730"
  [[ $(stat -c '%a' "$SPOOL_ROOT/previews") == 2750 ]] && ok "previews directory is 2750" || fail "previews directory is 2750"
  [[ $(stat -c '%a' "$DIR_ETC") == 750 ]] && ok "config directory is 0750" || fail "config directory is 0750"
  journal=$(grep -F "PREPARE JOURNAL: " <<<"$CAPTURE_OUT" | cut -d' ' -f3 || true)
  if [[ -n "$journal" && -f "$journal/prepare.json" && $(stat -c '%a' "$journal/prepare.json") == 600 ]]; then
    ok "journal is a mode-0600 file"
  else
    fail "journal is a mode-0600 file (journal=$journal)"
  fi
  if grep -qF '"adoptedRequestsGroup": false' "$journal/prepare.json"; then
    ok "journal records no adoption on a fresh host"
  else
    fail "journal records no adoption on a fresh host"
  fi
  if grep -qF '"preexistingRequestsMembers": ""' "$journal/prepare.json"; then
    ok "journal records an empty pre-existing member list on a fresh host"
  else
    fail "journal records an empty pre-existing member list on a fresh host"
  fi
  userdel_line=$(grep -n "userdel paperclip-host-reader" <<<"$CAPTURE_OUT" | head -1 | cut -d: -f1 || true)
  rmdir_first=$(grep -n "^  rmdir " <<<"$CAPTURE_OUT" | head -1 | cut -d: -f1 || true)
  rmdir_first_path=$(grep "^  rmdir " <<<"$CAPTURE_OUT" | head -1 || true)
  rmdir_last_path=$(grep "^  rmdir " <<<"$CAPTURE_OUT" | tail -1 || true)
  if [[ -n "$userdel_line" && -n "$rmdir_first" && $userdel_line -lt $rmdir_first ]]; then
    ok "rollback removes the account before directories"
  else
    fail "rollback removes the account before directories"
  fi
  if [[ "$rmdir_first_path" == *"/quarantine" && "$rmdir_last_path" == *"/usr/local/libexec/paperclip-host-ops" ]]; then
    ok "rollback removes directories leaf-first"
  else
    fail "rollback removes directories leaf-first (first=$rmdir_first_path last=$rmdir_last_path)"
  fi
  # A second run in the same world refuses: the identity now exists.
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"already exists"* ]]; then
    ok "second run refuses on the identity it created"
  else
    fail "second run refuses on the identity it created (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=true
  STUB_MEMBERS=""
  stub_classify_for_pipeline
  seed_group paperclip-host-requests "$REAL_GID" ""
  mkdir -p "$SPOOL_ROOT" "$SPOOL_ROOT/previews"
  run_sub main
  if [[ $CAPTURE_RC -ne 0 ]]; then
    fail "adopting full run succeeds (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  else
    ok "adopting full run succeeds"
  fi
  if grep -q "groupadd -r paperclip-host-requests" "$CALL_LOG"; then
    fail "adopting run does not recreate the requests group"
  else
    ok "adopting run does not recreate the requests group"
  fi
  journal=$(grep -F "PREPARE JOURNAL: " <<<"$CAPTURE_OUT" | cut -d' ' -f3 || true)
  if grep -qF '"adoptedRequestsGroup": true' "$journal/prepare.json"; then
    ok "journal records the adopted requests group"
  else
    fail "journal records the adopted requests group"
  fi
  if grep -qF '"preexistingRequestsMembers": ""' "$journal/prepare.json"; then
    ok "journal records an empty pre-existing member list for an empty group"
  else
    fail "journal records an empty pre-existing member list for an empty group"
  fi
  prior_ids=$(stat -c '%u %g' "$SPOOL_ROOT/previews")
  # shellcheck disable=SC2086
  if grep -qF "chown ${prior_ids% *}:${prior_ids#* } $SPOOL_ROOT/previews" <<<"$CAPTURE_OUT"; then
    ok "rollback restores adopted spool ownership"
  else
    fail "rollback restores adopted spool ownership"
  fi
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=true
  STUB_MEMBERS="ubuntu"
  stub_classify_for_pipeline
  seed_group paperclip-host-requests "$REAL_GID" "ubuntu"
  run_sub main --expect-requests-members ubuntu
  if [[ $CAPTURE_RC -ne 0 ]]; then
    fail "member-preserving full run succeeds (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  else
    ok "member-preserving full run succeeds"
  fi
  if grep -qE "^paperclip-host-requests:[^:]*:[^:]*:ubuntu,$READER_USER$" "$NSS_DIR/groups"; then
    ok "member-preserving run appends the reader without evicting ubuntu"
  else
    fail "member-preserving run appends the reader without evicting ubuntu ($(cat "$NSS_DIR/groups"))"
  fi
  journal=$(grep -F "PREPARE JOURNAL: " <<<"$CAPTURE_OUT" | cut -d' ' -f3 || true)
  if grep -qF '"preexistingRequestsMembers": "ubuntu"' "$journal/prepare.json"; then
    ok "journal pins the pre-existing member list"
  else
    fail "journal pins the pre-existing member list"
  fi
  if grep -qF '"expectedRequestsMembers": "ubuntu"' "$journal/prepare.json"; then
    ok "journal pins the operator-supplied expected list"
  else
    fail "journal pins the operator-supplied expected list"
  fi
  if grep -qF "ADOPTED requests group members preserved: ubuntu" <<<"$CAPTURE_OUT"; then
    ok "run reports the preserved member list"
  else
    fail "run reports the preserved member list"
  fi
  if grep -q "groupadd -r paperclip-host-requests" "$CALL_LOG"; then
    fail "member-preserving run does not recreate the requests group"
  else
    ok "member-preserving run does not recreate the requests group"
  fi
  # Round trip through the shipped installer contract: the journal this run
  # wrote must satisfy the real membership parser, which must then refuse
  # drift. Sources the real install-lib (not the pipeline stub) with the
  # journal root pointed at this run's journal.
  (
    unset -f validate_service_identity
    # shellcheck source=host-ops/install-lib.sh
    source "$ROOT/host-ops/install-lib.sh"
    PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT"
    current_line=$(grep -E "^paperclip-host-requests:" "$NSS_DIR/groups")
    current_members=$(cut -d: -f4 <<<"$current_line")
    current_gid=$(cut -d: -f3 <<<"$current_line")
    reader_line=$(grep -E "^paperclip-host-reader:" "$NSS_DIR/passwd")
    reader_uid=$(cut -d: -f3 <<<"$reader_line")
    reader_gid=$(cut -d: -f4 <<<"$reader_line")
    if ( validate_requests_group_membership "$current_members" "$current_gid" "$reader_uid" "$reader_gid" ) 2>/dev/null; then
      ok "journal from the full preparation run passes the real installer membership check"
    else
      fail "journal from the full preparation run passes the real installer membership check"
    fi
    if ( validate_requests_group_membership "$current_members,mallory" "$current_gid" "$reader_uid" "$reader_gid" ) 2>/dev/null; then
      fail "real installer check refuses a member gained after preparation"
    else
      gained_out=$( ( validate_requests_group_membership "$current_members,mallory" "$current_gid" "$reader_uid" "$reader_gid" ) 2>&1 || true)
      if grep -qF "gained member mallory" <<<"$gained_out"; then
        ok "real installer check refuses a member gained after preparation"
      else
        fail "real installer check refuses a member gained after preparation (out=$gained_out)"
      fi
    fi
  )
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=true
  STUB_MEMBERS="ubuntu"
  stub_classify_for_pipeline
  seed_group paperclip-host-requests "$REAL_GID" "ubuntu"
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"pass --expect-requests-members"* ]]; then
    ok "member-preserving run without the expected flag refuses"
  else
    fail "member-preserving run without the expected flag refuses (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=true
  STUB_MEMBERS="ubuntu"
  stub_classify_for_pipeline
  seed_group paperclip-host-requests "$REAL_GID" "ubuntu"
  run_sub main --expect-requests-members alice
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"do not match the expected list"* ]]; then
    ok "member-preserving run with a mismatched expected list refuses"
  else
    fail "member-preserving run with a mismatched expected list refuses (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

# Distinct ids (requests 986 seeded with ubuntu, reader 990:991) drive the real
# classifier, created-identity check and installer gate. Only ownership is
# stubbed: chown is recorded and stat serves the recorded owner with the real
# mode, so each directory's uid, gid and mode are asserted, not just that main
# finishes.
(
  setup_world
  point_at_tmp
  NEW_GID_FIXTURE=991
  NEW_UID_FIXTURE=990
  seed_group paperclip-host-requests 986 ubuntu
  touch "$NSS_DIR/owners"
  chown() {
    echo "chown $*" >> "$CALL_LOG"
    local path="$2"
    awk -v p="$path" '$1 != p' "$NSS_DIR/owners" > "$NSS_DIR/owners.next"
    printf '%s %s %s\n' "$path" "${1%%:*}" "${1#*:}" >> "$NSS_DIR/owners.next"
    mv "$NSS_DIR/owners.next" "$NSS_DIR/owners"
  }
  stat_identity() {
    local recorded
    recorded=$(awk -v p="$1" '$1 == p { id = $2 " " $3 } END { print id }' "$NSS_DIR/owners")
    if [[ -n "$recorded" ]]; then
      printf '%s %s\n' "$recorded" "$(stat -c '%a' "$1")"
    else
      stat -c '%u %g %a' "$1"
    fi
  }
  unset -f validate_service_identity
  # shellcheck source=host-ops/install-lib.sh
  source "$ROOT/host-ops/install-lib.sh"
  eval "real_$(declare -f validate_created_identity)"
  validate_created_identity() { echo "validate_created_identity" >> "$CALL_LOG"; real_validate_created_identity; }
  eval "real_$(declare -f validate_service_identity)"
  validate_service_identity() { echo "validate_service_identity" >> "$CALL_LOG"; real_validate_service_identity; }
  run_strict main --expect-requests-members ubuntu
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == *"PREPARED first-install identity"* ]]; then
    ok "distinct-id run completes under the real gates"
  else
    fail "distinct-id run completes under the real gates (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  journal=$(grep -F "PREPARE JOURNAL: " <<<"$CAPTURE_OUT" | cut -d' ' -f3 || true)
  for needle in '"readerUid": 990,' '"readerGid": 991,' '"requestsGid": 986,' \
    '"preexistingRequestsMembers": "ubuntu",' '"expectedRequestsMembers": "ubuntu",'; do
    if grep -qF "$needle" "$journal/prepare.json" 2>/dev/null; then
      ok "journal pins $needle"
    else
      fail "journal pins $needle"
    fi
  done
  expect_owner() {
    local label="$1" path="$2" want="$3" got
    got=$(stat_identity "$path" 2>/dev/null || true)
    if [[ "$got" == "$want" ]]; then ok "$label"; else fail "$label (want $want got $got)"; fi
  }
  expect_owner "config directory carries the reader group" "$DIR_ETC" "$REAL_UID 991 750"
  expect_owner "runtime directory belongs to the reader" "$DIR_STATE" "990 991 750"
  expect_owner "key directory belongs to the reader" "$DIR_KEYS" "990 991 700"
  expect_owner "log directory belongs to the reader" "$DIR_LOG" "990 991 750"
  expect_owner "spool root carries the requests group" "$SPOOL_ROOT" "990 986 750"
  expect_owner "spool requests carries the requests group" "$SPOOL_ROOT/requests" "990 986 3730"
  expect_owner "spool staging carries the reader group" "$SPOOL_ROOT/.staging" "990 991 700"
  expect_owner "spool previews carry the requests group" "$SPOOL_ROOT/previews" "990 986 2750"
  for needle in validate_created_identity validate_service_identity; do
    if grep -qx "$needle" "$CALL_LOG"; then ok "full run calls $needle"; else fail "full run calls $needle"; fi
  done
)

(
  setup_world
  point_at_tmp
  printf 'passwd: files sss\ngroup: files\ninitgroups: files\n' > "$NSSWITCH_CONF"
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"passwd must use local files only"* ]] && ! grep -q groupadd "$CALL_LOG"; then
    ok "directory passwd source refuses before any host change"
  else
    fail "directory passwd source refuses before any host change (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  printf 'passwd: files\ngroup: files sss\ninitgroups: files\n' > "$NSSWITCH_CONF"
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"group must use local files only"* ]] && ! grep -q groupadd "$CALL_LOG"; then
    ok "directory group source refuses before any host change"
  else
    fail "directory group source refuses before any host change (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

# Post-mutation refusals must still print ROLLBACK lines. refuse() exits the
# shell, so a bare `validate || partial_rollback` would never reach the
# rollback printer; the shipped code runs validators in a subshell. Fail the
# final installer gate on purpose (exit, like the real refuse) and assert the
# rollback text survives.
(
  setup_world
  point_at_tmp
  STUB_ADOPT=false
  STUB_MEMBERS=""
  stub_classify_for_pipeline
  validate_service_identity() {
    printf 'REFUSED: created identity fails the installer validation contract\n' >&2
    exit 2
  }
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"REFUSED:"* && "$CAPTURE_OUT" == *"ROLLBACK"* && "$CAPTURE_OUT" == *"userdel paperclip-host-reader"* ]]; then
    ok "post-mutation installer-gate failure still prints rollback"
  else
    fail "post-mutation installer-gate failure still prints rollback (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  aborted=$(grep -F "JOURNAL MARKED ABORTED: " <<<"$CAPTURE_OUT" | cut -d' ' -f4 || true)
  if [[ -n "$aborted" ]] && grep -qF '"phase": "aborted"' "$aborted"; then
    ok "post-mutation installer-gate failure marks the journal aborted"
  else
    fail "post-mutation installer-gate failure marks the journal aborted (out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=true
  STUB_MEMBERS="ubuntu"
  stub_classify_for_pipeline
  seed_group paperclip-host-requests "$REAL_GID" "ubuntu"
  validate_service_identity() {
    if [[ "$IFS" == $' \t\n' ]]; then
      echo "validator-ifs-default" >> "$CALL_LOG"
    else
      echo "validator-ifs-leaked" >> "$CALL_LOG"
    fi
  }
  run_sub main --expect-requests-members ubuntu
  if grep -qF "validator-ifs-default" "$CALL_LOG" && ! grep -qF "validator-ifs-leaked" "$CALL_LOG"; then
    ok "installer gate runs with the default IFS after a populated-group run"
  else
    fail "installer gate runs with the default IFS after a populated-group run (out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  useradd() {
    if ls "$JOURNAL_ROOT"/prepare-*/prepare.json >/dev/null 2>&1; then
      echo "journal-before-identity" >> "$CALL_LOG"
    fi
    return 1
  }
  run_sub main
  aborted=$(grep -F "JOURNAL MARKED ABORTED: " <<<"$CAPTURE_OUT" | cut -d' ' -f4 || true)
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"ROLLBACK"* && "$CAPTURE_OUT" == *"groupdel paperclip-host-reader"* ]]; then
    ok "failure after the reader group is created prints rollback"
  else
    fail "failure after the reader group is created prints rollback (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  if [[ -n "$aborted" ]] && grep -qF '"phase": "aborted"' "$aborted"; then
    ok "failure after the reader group is created marks the journal aborted"
  else
    fail "failure after the reader group is created marks the journal aborted (out=$CAPTURE_OUT)"
  fi
  if grep -qF "journal-before-identity" "$CALL_LOG"; then
    ok "journal exists before the first identity change"
  else
    fail "journal exists before the first identity change"
  fi
)

(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  chmod() {
    if [[ "${2:-}" == "$DIR_KEYS" ]]; then return 1; fi
    command chmod "$@"
  }
  run_sub main
  aborted=$(grep -F "JOURNAL MARKED ABORTED: " <<<"$CAPTURE_OUT" | cut -d' ' -f4 || true)
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"PARTIAL: runtime directory failed"* && "$CAPTURE_OUT" == *"rmdir $DIR_KEYS"* ]]; then
    ok "directory that failed after mkdir is rolled back with the rest"
  else
    fail "directory that failed after mkdir is rolled back with the rest (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  if [[ -n "$aborted" ]] && grep -qF '"phase": "aborted"' "$aborted"; then
    ok "directory failure marks the journal aborted"
  else
    fail "directory failure marks the journal aborted (out=$CAPTURE_OUT)"
  fi
)

(
  setup_world
  point_at_tmp
  stub_classify_for_pipeline
  mkdir() {
    if [[ "${1:-}" == *"/prepare-"* ]]; then return 1; fi
    command mkdir "$@"
  }
  run_sub main
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"REFUSED: journal directory already exists"* && "$CAPTURE_OUT" == *"ROLLBACK"* ]]; then
    ok "refusal after the journal root is created prints rollback"
  else
    fail "refusal after the journal root is created prints rollback (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

# Installer round trip with the shipped validator, unstubbed. Fixture ids are
# distinct and system-range (requests gid 986, reader uid 990, reader gid 991),
# so a swapped or mis-bound id shows as a mismatch. Only getent, id and passwd
# are stubbed.
real_installer_check() {
  unset -f validate_service_identity
  # shellcheck source=host-ops/install-lib.sh
  source "$ROOT/host-ops/install-lib.sh"
  PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT"
  validate_service_identity
}

write_round_trip_journal() {
  # write_round_trip_journal <phase> <requests-gid> <reader-gid> <pinned-members> [reader-uid]
  ADOPT_REQUESTS=true
  READER_UID="${5:-990}"
  READER_GID="$3"
  REQUESTS_GID="$2"
  REQUESTS_MEMBERS="$4"
  EXPECT_REQUESTS_MEMBERS="$4"
  CREATED_DIRS=()
  ADOPT_PRIOR=()
  mkdir -p "$JOURNAL_ROOT/prepare-roundtrip"
  write_journal "$1" "$JOURNAL_ROOT/prepare-roundtrip/prepare.json"
}

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 986 991 ubuntu
  run_sub real_installer_check
  if [[ $CAPTURE_RC -eq 0 ]]; then
    ok "real installer check accepts the journal the real writer produced"
  else
    fail "real installer check accepts the journal the real writer produced (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
  (
    IFS=','
    run_sub real_installer_check
    if [[ $CAPTURE_RC -eq 0 ]]; then
      ok "real installer check is unaffected by a caller's comma IFS"
    else
      fail "real installer check is unaffected by a caller's comma IFS (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
    fi
  )
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 991 986 ubuntu
  expect_refusal "real installer check refuses a journal with swapped requests and reader gids" \
    "does not match the current identity" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 990 991 ubuntu
  expect_refusal "real installer check refuses a journal bound to another requests gid alone" \
    "does not match the current identity" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 986 990 ubuntu
  expect_refusal "real installer check refuses a journal bound to another reader gid alone" \
    "does not match the current identity" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 986 991 ubuntu 991
  expect_refusal "real installer check refuses a journal bound to another reader uid alone" \
    "does not match the current identity" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader,mallory" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 986 991 ubuntu
  expect_refusal "real installer check refuses a member gained after preparation" \
    "gained member mallory" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal prepared 986 991 "ubuntu,alice"
  expect_refusal "real installer check refuses a pinned member removed after preparation" \
    "lost pre-existing member alice" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  seed_passwd mallory 1001 991 /home/mallory /bin/bash
  write_round_trip_journal prepared 986 991 ubuntu
  expect_refusal "real installer check refuses a primary-group account on the reader gid" \
    "holds paperclip-host-reader as its primary group" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  seed_passwd mallory 1001 986 /home/mallory /bin/bash
  write_round_trip_journal prepared 986 991 ubuntu
  expect_refusal "real installer check refuses a primary-group account on the requests gid" \
    "holds paperclip-host-requests as its primary group" real_installer_check
)

(
  setup_world
  point_at_tmp
  seed_created_identity "ubuntu,paperclip-host-reader" 986 990 991 "ubuntu"
  write_round_trip_journal aborted 986 991 ubuntu
  expect_refusal "real installer check refuses an aborted journal" \
    "not a completed preparation" real_installer_check
)

(
  setup_world
  point_at_tmp
  run_sub main extra-arg
  if [[ $CAPTURE_RC -eq 2 ]]; then ok "unknown arguments are refused"; else fail "unknown arguments are refused (rc=$CAPTURE_RC)"; fi
)

(
  setup_world
  point_at_tmp
  STUB_ADOPT=false
  STUB_MEMBERS=""
  stub_classify_for_pipeline
  run_sub main --expect-requests-members "paperclip-host-reader"
  if [[ $CAPTURE_RC -eq 2 && "$CAPTURE_OUT" == *"must not include"* ]]; then
    ok "expected list naming the reader is refused"
  else
    fail "expected list naming the reader is refused (rc=$CAPTURE_RC out=$CAPTURE_OUT)"
  fi
)

(
  run_sub main --help
  if [[ $CAPTURE_RC -eq 0 && "$CAPTURE_OUT" == *"Usage:"* ]]; then
    ok "--help prints usage"
  else
    fail "--help prints usage (rc=$CAPTURE_RC)"
  fi
)

failures=$(wc -l < "$FAIL_FILE")
if ((failures != 0)); then
  printf '%s first-install preparation contract tests failed\n' "$failures" >&2
  exit 1
fi
printf 'first-install preparation contract tests passed\n'
