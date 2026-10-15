#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=host-ops/install-lib.sh
source "$ROOT/host-ops/install-lib.sh"

failures=0
ok() { printf '  ok: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }
refuse() { printf 'REFUSED: %s\n' "$1" >&2; exit 2; }

FIXTURE=""
EXTRA_PASSWD=""
getent() {
  # Enumeration for the primary-group holder check: `getent passwd` with no
  # user lists the whole table. Includes the reader plus any injected extra
  # accounts (EXTRA_PASSWD) for the holder tests below.
  if [[ "${1:-}" == passwd && -z "${2:-}" ]]; then
    [[ "$FIXTURE" != absent ]] && printf '%s\n' "${READER_PASSWD:-paperclip-host-reader:x:991:991::/var/lib/paperclip-host-ops:/usr/sbin/nologin}"
    [[ -n "${EXTRA_PASSWD:-}" ]] && printf '%s\n' "$EXTRA_PASSWD"
    return 0
  fi
  case "$1:$2" in
    group:paperclip-host-reader) [[ "$FIXTURE" != absent ]] && printf '%s\n' "${READER_GROUP:-paperclip-host-reader:x:991:}" ;;
    group:paperclip-host-requests) [[ "$FIXTURE" != absent ]] && printf '%s\n' "${REQUESTS_GROUP:-paperclip-host-requests:x:992:paperclip-host-reader}" ;;
    passwd:paperclip-host-reader) [[ "$FIXTURE" != absent ]] && printf '%s\n' "${READER_PASSWD:-paperclip-host-reader:x:991:991::/var/lib/paperclip-host-ops:/usr/sbin/nologin}" ;;
    *) return 2 ;;
  esac
}
id() {
  if [[ "$1" == -nG ]]; then
    printf '%s\n' "${READER_GROUPS:-paperclip-host-reader paperclip-host-requests}"
  elif [[ "$1" == paperclip-host-reader ]]; then
    [[ "$FIXTURE" != absent ]]
  else
    return 2
  fi
}
passwd() {
  [[ "$1" == -S && "$2" == paperclip-host-reader ]] || return 2
  printf 'paperclip-host-reader %s 2026-08-28 0 99999 7 -1\n' "${PASSWORD_STATE:-L}"
}

reset_fixture() {
  FIXTURE=valid
  EXTRA_PASSWD=""
  NSSWITCH_CONF="$NSSWITCH_LOCAL"
  unset READER_GROUP REQUESTS_GROUP READER_PASSWD READER_GROUPS PASSWORD_STATE
}

# Membership-boundary journals (case_4a4d7340 follow-up): the validator binds to
# the preparation journal. The default root is an empty fixture dir so the
# strict-rule cases exercise the no-journal path hermetically, never the host.
JOURNAL_ROOT_FIXTURE=$(mktemp -d)
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
mkdir -p "$PREPARE_JOURNAL_ROOT"
chmod 0700 "$PREPARE_JOURNAL_ROOT"
PREPARE_JOURNAL_OWNER_UID=$(command id -u)
PREPARE_JOURNAL_OWNER_GID=$(command id -g)
NSSWITCH_LOCAL="$JOURNAL_ROOT_FIXTURE/nsswitch-local"
printf 'passwd: files\ngroup: files\ninitgroups: files\n' > "$NSSWITCH_LOCAL"
NSSWITCH_CONF="$NSSWITCH_LOCAL"

seed_journal() {
  # seed_journal <name> <adopted> <requestsGid> <readerUid> <readerGid> <members|_ABSENT_> [expected|_ABSENT_] [truncated]
  local dir="$JOURNAL_ROOT_FIXTURE/$1" expected="${7:-$6}"
  mkdir -p "$dir/prepare-abc123"
  chmod 0700 "$dir" "$dir/prepare-abc123"
  {
    printf '{\n  "version": 1,\n  "phase": "prepared",\n  "adoptedRequestsGroup": %s,\n' "$2"
    printf '  "readerUid": %s,\n  "readerGid": %s,\n  "requestsGid": %s,\n' "$4" "$5" "$3"
    if [[ "$6" != "_ABSENT_" ]]; then
      printf '  "preexistingRequestsMembers": "%s",\n' "$6"
    fi
    if [[ "$6" != "_ABSENT_" && "$expected" != "_ABSENT_" ]]; then
      printf '  "expectedRequestsMembers": "%s",\n' "$expected"
    fi
    printf '  "createdDirs": [],\n  "adoptedDirs": []\n}\n'
  } > "$dir/prepare-abc123/prepare.json"
  [[ "${8:-}" == truncated ]] && sed -i '$d' "$dir/prepare-abc123/prepare.json"
  PREPARE_JOURNAL_ROOT="$dir"
}

expect_refusal() {
  local label="$1" expected="$2"
  shift 2
  local output rc
  set +e
  output=$("$@" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 2 && "$output" == *"$expected"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

generate_signing_fixture() {
  local bits="$1" fixture_dir="$2"
  mkdir -p "$fixture_dir/work"
  openssl genpkey -algorithm RSA -pkeyopt "rsa_keygen_bits:$bits" -out "$fixture_dir/private.pem" >/dev/null 2>&1
  openssl pkey -in "$fixture_dir/private.pem" -pubout -out "$fixture_dir/public.pem" >/dev/null 2>&1
  sha256sum "$fixture_dir/public.pem" | cut -d' ' -f1
}

KEY_FIXTURES=$(mktemp -d)
trap 'rm -rf "$KEY_FIXTURES" "$JOURNAL_ROOT_FIXTURE"' EXIT
for weak_bits in 512 1024; do
  weak_dir="$KEY_FIXTURES/rsa-$weak_bits"
  weak_pin=$(generate_signing_fixture "$weak_bits" "$weak_dir")
  expect_refusal "${weak_bits}-bit signing key is refused" "must be at least 2048 bits" \
    validate_response_signing_key "$weak_dir/private.pem" "$weak_dir/public.pem" "$weak_pin" "$weak_dir/work"
done
valid_dir="$KEY_FIXTURES/rsa-2048"
valid_pin=$(generate_signing_fixture 2048 "$valid_dir")
if validate_response_signing_key "$valid_dir/private.pem" "$valid_dir/public.pem" "$valid_pin" "$valid_dir/work"; then
  ok "2048-bit matching signing key with independent pin passes"
else
  fail "2048-bit matching signing key with independent pin passes"
fi

reset_fixture
[[ $(classify_service_identity) == valid ]] && ok "valid identity passes" || fail "valid identity passes"

FIXTURE=absent
[[ $(classify_service_identity) == absent ]] && ok "fully absent identity is classifiable" || fail "fully absent identity is classifiable"

reset_fixture; READER_GROUP='paperclip-host-reader:x:1000:'
expect_refusal "reader group must be system" "must be a system group" classify_service_identity
reset_fixture; READER_GROUP='paperclip-host-reader:x:991:someone'
expect_refusal "reader group has no explicit members" "unintended explicit members" classify_service_identity
reset_fixture; REQUESTS_GROUP='paperclip-host-requests:x:992:'
expect_refusal "requests group membership is exact" "must contain only paperclip-host-reader" classify_service_identity

# case_4a4d7340 follow-up: journal-pinned membership boundary. Default fixtures
# are reader 991:991 with requests gid 992; journals below bind to those ids.
reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "members without a journal refuse strict" "must contain only paperclip-host-reader" classify_service_identity

reset_fixture
seed_journal adopted-ubuntu true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
[[ $(classify_service_identity) == valid ]] && ok "journaled pre-existing member passes" || fail "journaled pre-existing member passes"

reset_fixture
seed_journal adopted-gained true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,mallory,paperclip-host-reader'
expect_refusal "member added after preparation refuses" "gained member mallory" classify_service_identity

reset_fixture
seed_journal adopted-lost true 992 991 991 "ubuntu,alice"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "member removed after preparation refuses" "lost pre-existing member alice" classify_service_identity

reset_fixture
seed_journal adopted-gid true 993 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "journal bound to another gid refuses" "does not match the current identity" classify_service_identity

reset_fixture
seed_journal adopted-noreader true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu'
expect_refusal "journaled group missing the reader refuses" "does not contain the broker reader" classify_service_identity

reset_fixture
seed_journal strict-journal false 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:paperclip-host-reader'
[[ $(classify_service_identity) == valid ]] && ok "non-adopting journal keeps the strict rule" || fail "non-adopting journal keeps the strict rule"

reset_fixture
seed_journal old-format true 992 991 991 "_ABSENT_"
REQUESTS_GROUP='paperclip-host-requests:x:992:paperclip-host-reader'
[[ $(classify_service_identity) == valid ]] && ok "journal without a member list keeps the strict rule" || fail "journal without a member list keeps the strict rule"

reset_fixture
seed_journal adopted-unasserted true 992 991 991 "ubuntu" _ABSENT_
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "pinned members without the operator-asserted list refuse" "lacks the operator-asserted member list" classify_service_identity

reset_fixture
seed_journal adopted-contradicted true 992 991 991 "ubuntu" alice
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "operator-asserted list contradicting the pin refuses" "do not match the operator-asserted list" classify_service_identity

reset_fixture
seed_journal adopted-truncated true 992 991 991 "ubuntu" ubuntu truncated
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "truncated prepared journal refuses" "preparation journal is incomplete" classify_service_identity

reset_fixture
seed_journal newest-aborted true 992 991 991 "ubuntu"
mkdir -p "$JOURNAL_ROOT_FIXTURE/newest-aborted/prepare-newer"
chmod 0700 "$JOURNAL_ROOT_FIXTURE/newest-aborted/prepare-newer"
sed 's/"phase": "prepared"/"phase": "aborted"/' "$JOURNAL_ROOT_FIXTURE/newest-aborted/prepare-abc123/prepare.json" > "$JOURNAL_ROOT_FIXTURE/newest-aborted/prepare-newer/prepare.json"
touch -d '2 hours ago' "$JOURNAL_ROOT_FIXTURE/newest-aborted/prepare-abc123/prepare.json"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "newest aborted journal refuses beside an older prepared one" "not a completed preparation" classify_service_identity

reset_fixture
seed_journal older-aborted true 992 991 991 "ubuntu"
mkdir -p "$JOURNAL_ROOT_FIXTURE/older-aborted/prepare-old"
chmod 0700 "$JOURNAL_ROOT_FIXTURE/older-aborted/prepare-old"
sed 's/"phase": "prepared"/"phase": "aborted"/' "$JOURNAL_ROOT_FIXTURE/older-aborted/prepare-abc123/prepare.json" > "$JOURNAL_ROOT_FIXTURE/older-aborted/prepare-old/prepare.json"
touch -d '2 hours ago' "$JOURNAL_ROOT_FIXTURE/older-aborted/prepare-old/prepare.json"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
[[ $(classify_service_identity) == valid ]] && ok "newest prepared journal passes beside an older aborted one" || fail "newest prepared journal passes beside an older aborted one"

reset_fixture
seed_journal newest-symlink true 992 991 991 "ubuntu"
mkdir -p "$JOURNAL_ROOT_FIXTURE/newest-symlink/prepare-linked"
chmod 0700 "$JOURNAL_ROOT_FIXTURE/newest-symlink/prepare-linked"
ln -s "$JOURNAL_ROOT_FIXTURE/newest-symlink/prepare-abc123/prepare.json" "$JOURNAL_ROOT_FIXTURE/newest-symlink/prepare-linked/prepare.json"
touch -d '2 hours ago' "$JOURNAL_ROOT_FIXTURE/newest-symlink/prepare-abc123/prepare.json"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
expect_refusal "symlinked journal entry refuses" "preparation journal is not a regular file" classify_service_identity

reset_fixture
seed_journal drift-root true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
chmod 0775 "$JOURNAL_ROOT_FIXTURE/drift-root"
expect_refusal "journal root writable by others refuses" "journal root has unexpected owner or mode" classify_service_identity

reset_fixture
seed_journal drift-entry true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
chmod 0755 "$JOURNAL_ROOT_FIXTURE/drift-entry/prepare-abc123"
expect_refusal "journal entry with drifted mode refuses" "journal entry has unexpected owner or mode" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files sss\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-sss"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-sss"
expect_refusal "directory passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files\ngroup: files sss\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-group-sss"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-group-sss"
expect_refusal "directory group source refuses identity validation" "group must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files\ngroup: files\ninitgroups: files sss\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-initgroups-sss"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-initgroups-sss"
expect_refusal "directory initgroups source refuses identity validation" "initgroups must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf '  passwd: files sss\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-leading-space"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-leading-space"
expect_refusal "leading-space passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd : files sss\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-space-colon"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-space-colon"
expect_refusal "space-before-colon passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd files sss\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-no-colon"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-no-colon"
expect_refusal "no-colon passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: sss files #\377\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-invalid-utf8"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-invalid-utf8"
LC_ALL=C.UTF-8 expect_refusal "invalid-UTF-8 passwd source refuses under a UTF-8 locale" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd:   files   # local only\ngroup: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-comment"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-comment"
expect_refusal "trailing-hash passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files # sss\ngroup: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-hash-sss"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-passwd-hash-sss"
expect_refusal "hash passwd source refuses identity validation" "passwd must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files\ngroup: files # systemd\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-group-hash-systemd"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-group-hash-systemd"
expect_refusal "hash group source refuses identity validation" "group must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files\ngroup: files\ninitgroups: files # sss\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-initgroups-hash-sss"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-initgroups-hash-sss"
expect_refusal "hash initgroups source refuses identity validation" "initgroups must use local files only" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'group: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-no-passwd"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-no-passwd"
expect_refusal "conf without a passwd line refuses identity validation" "passwd database is absent" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
printf 'passwd: files\ninitgroups: files\n' > "$JOURNAL_ROOT_FIXTURE/nsswitch-no-group"
NSSWITCH_CONF="$JOURNAL_ROOT_FIXTURE/nsswitch-no-group"
expect_refusal "conf without a group line refuses identity validation" "group database is absent" classify_service_identity

# Primary-group holders bypass the member list yet gain the same spool access.
# They must refuse in both the strict and journal-pinned paths.
reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
EXTRA_PASSWD='mallory:x:1001:992::/home/mallory:/bin/bash'
expect_refusal "primary-group holder refuses under the strict rule" "primary group" classify_service_identity

reset_fixture
seed_journal adopted-primary true 992 991 991 "ubuntu"
REQUESTS_GROUP='paperclip-host-requests:x:992:ubuntu,paperclip-host-reader'
EXTRA_PASSWD='mallory:x:1001:992::/home/mallory:/bin/bash'
expect_refusal "primary-group holder refuses under a journal-pinned group" "primary group" classify_service_identity

reset_fixture
PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT_FIXTURE/empty"
reset_fixture; READER_PASSWD='paperclip-host-reader:x:1000:991::/var/lib/paperclip-host-ops:/usr/sbin/nologin'
expect_refusal "reader uid must be system" "must be a system account" classify_service_identity
reset_fixture; READER_PASSWD='paperclip-host-reader:x:991:992::/var/lib/paperclip-host-ops:/usr/sbin/nologin'
expect_refusal "primary gid is exact" "primary group is incompatible" classify_service_identity
reset_fixture; READER_PASSWD='paperclip-host-reader:x:991:991::/wrong:/usr/sbin/nologin'
expect_refusal "home is exact" "home is incompatible" classify_service_identity
reset_fixture; READER_PASSWD='paperclip-host-reader:x:991:991::/var/lib/paperclip-host-ops:/bin/bash'
expect_refusal "shell is exact" "shell is incompatible" classify_service_identity
reset_fixture; PASSWORD_STATE=P
expect_refusal "password is locked" "credentials must be locked" classify_service_identity
reset_fixture; READER_GROUPS='paperclip-host-reader'
expect_refusal "supplemental group count is exact" "unintended supplemental groups" classify_service_identity
reset_fixture; READER_GROUPS='paperclip-host-reader other'
expect_refusal "supplemental group names are exact" "group membership is incompatible" classify_service_identity
reset_fixture; FIXTURE=partial
getent() {
  case "$1:$2" in
    group:paperclip-host-reader) printf 'paperclip-host-reader:x:991:\n' ;;
    *) return 2 ;;
  esac
}
id() { return 1; }
expect_refusal "partial identity refuses before creation" "partially configured" classify_service_identity

# case_954f2dc1: --paperclip-image-ref argument contract and rendering.
# The installer refuses a reference that could terminate or widen the exact-argv
# sudoers grant, and renders the accepted one identically into the sudoers file
# and config.json. Argument validation is exercised through the real script so
# the regex under test is the shipped one.
# Run the shipped script with the EUID guard stubbed out, so the refusal under
# test is the image-reference check and not "must run as root". Invoking the
# real install.sh unmodified would refuse at the EUID line first and pass no
# matter what the image-reference contract said.
image_ref_arg_refusal() {
  local label="$1" candidate="$2" output rc harness
  harness=$(mktemp)
  # Stop before any host mutation: the required-command loop is the first step
  # after argument validation, so failing it bounds the run to arg parsing.
  sed -e 's/^\[\[ \$EUID -eq 0 \]\].*$/:/' \
      -e 's|^for command in git |for command in __case_76f433fe_absent__ git |' \
      "$ROOT/host-ops/install.sh" > "$harness"
  set +e
  output=$(bash "$harness" --source-ref "$(printf 'a%.0s' {1..40})" \
    --public-key-out /etc/paperclip-host-ops/response-signing.pub.pem --expected-public-key-sha256 "$(printf 'b%.0s' {1..64})" \
    --cliproxy-unit cliproxy.service \
    --paperclip-image-ref "$candidate" 2>&1)
  rc=$?
  set -e
  rm -f "$harness"
  if [[ $rc -eq 2 && "$output" == *"must be a plain image reference"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

# Positive control: the same harness must get PAST argument validation on a good
# reference, proving the refusals above are caused by the reference and not by
# the harness itself.
image_ref_arg_accepted() {
  local label="$1" candidate="$2" output rc harness
  harness=$(mktemp)
  sed -e 's/^\[\[ \$EUID -eq 0 \]\].*$/:/' \
      -e 's|^for command in git |for command in __case_76f433fe_absent__ git |' \
      "$ROOT/host-ops/install.sh" > "$harness"
  set +e
  output=$(bash "$harness" --source-ref "$(printf 'a%.0s' {1..40})" \
    --public-key-out /etc/paperclip-host-ops/response-signing.pub.pem --expected-public-key-sha256 "$(printf 'b%.0s' {1..64})" \
    --cliproxy-unit cliproxy.service \
    --paperclip-image-ref "$candidate" 2>&1)
  rc=$?
  set -e
  rm -f "$harness"
  if [[ "$output" == *"required command missing: __case_76f433fe_absent__"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

image_ref_regex_accepts() {
  local candidate="$1"
  [[ "$candidate" =~ ^[a-z0-9][a-z0-9._/-]{0,159}(:[A-Za-z0-9._-]{1,127}|@sha256:[0-9a-f]{64})?$ ]]
}

for good in \
  "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b" \
  "ghcr.io/paperclipai/paperclip@sha256:$(printf 'f%.0s' {1..64})" \
  "paperclip-local"; do
  if image_ref_regex_accepts "$good"; then ok "image ref accepted: $good"; else fail "image ref should be accepted: $good"; fi
done

for bad in \
  "paperclip-local --format {{json .}}, /bin/sh" \
  "paperclip local" \
  "paperclip-local, ALL=(ALL) NOPASSWD: ALL" \
  "-leading-dash" \
  ""; do
  if image_ref_regex_accepts "$bad"; then fail "image ref should be refused: $bad"; else ok "image ref refused: ${bad:-<empty>}"; fi
done

image_ref_arg_refusal "installer refuses an injecting image ref" "paperclip-local, ALL=(ALL) NOPASSWD: ALL"
image_ref_arg_refusal "installer refuses an image ref with whitespace" "paperclip local"
image_ref_arg_refusal "installer refuses an empty image ref" ""
image_ref_arg_accepted "installer accepts the running host's image ref" "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b"

# The rendered sudoers grant and the rendered config must name the same image.
render_dir=$(mktemp -d)
trap 'rm -rf "$render_dir"' EXIT
render_ref="localhost/paperclip-local:case_2e2581b7v2-51ee6c01b"
# Render through the SHIPPED function, not a local sed. The previous version of
# this test re-implemented the substitution, so it passed on a grant the
# installer could never actually produce.
render_sudoers_image_ref "$ROOT/host-ops/host-ops-broker.sudoers" "$render_dir/sudoers" "$render_ref"
python3 - "$ROOT/host-ops/config.example.json" "$render_dir/config.json" "$render_ref" <<'PY'
import json, sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text().replace("@@PAPERCLIP_IMAGE_REF@@", sys.argv[3]))
Path(sys.argv[2]).write_text(json.dumps(config))
PY
# The grant's bytes carry the sudoers colon escape; unescape before comparing,
# exactly as broker.py's sudoers_granted_image_ref does.
granted=$(grep -oP 'docker image inspect \K\S+' "$render_dir/sudoers" | sed 's/\\:/:/g')
configured=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["paperclipImageRef"])' "$render_dir/config.json")
if [[ "$granted" == "$render_ref" && "$configured" == "$render_ref" ]]; then
  ok "sudoers grant and config render the same image reference"
else
  fail "render mismatch (sudoers=$granted config=$configured)"
fi
if grep -Fq '@@PAPERCLIP_IMAGE_REF@@' "$render_dir/sudoers" || grep -Fq '@@PAPERCLIP_IMAGE_REF@@' "$render_dir/config.json"; then
  fail "placeholder survived rendering"
else
  ok "no placeholder survives rendering"
fi

# case_4f3123eb: the protected grant must stay at sudo's required root-only mode,
# while an exact-byte mirror is readable by the service group. The root-prefixed
# cmp binds the two before the unprivileged acceptance-check and daemon run.
service_source=$(<"$ROOT/host-ops/host-ops-broker.service")
install_source=$(<"$ROOT/host-ops/install.sh")
protected_sudoers=/etc/sudoers.d/paperclip-host-ops-broker
sudoers_mirror=/etc/paperclip-host-ops/sudoers.rendered
if grep -Fqx "ExecStartPre=+/usr/bin/cmp --silent $protected_sudoers $sudoers_mirror" <<<"$service_source"; then
  ok "root-only preflight binds readable sudoers mirror to protected grant"
else
  fail "unit does not bind the sudoers mirror to the protected grant"
fi
if grep -Fqx "ExecStartPre=/usr/local/libexec/paperclip-host-ops/broker.py acceptance-check --config /etc/paperclip-host-ops/config.json" <<<"$service_source" &&
   grep -Fqx "ExecStart=/usr/local/libexec/paperclip-host-ops/broker.py run --config /etc/paperclip-host-ops/config.json" <<<"$service_source"; then
  ok "acceptance-check and daemon remain unprivileged"
else
  fail "acceptance-check or daemon gained root execution"
fi
# case_81816394 node7b1ff338 Docker port: rootful Docker needs no per-user runtime or
# container-storage path. The only writable set is the broker's own
# host-owned state/spool/audit directories; /run/user/1000 and
# .local/share/containers must be gone (broker suite asserts the same).
docker_writable_paths=$(grep -F "ReadWritePaths=" <<<"$service_source" || true)
expected_hardening=(
  "NoNewPrivileges=no"
  "PrivateTmp=yes"
  "PrivateDevices=yes"
  "ProtectSystem=strict"
  "ProtectHome=read-only"
  "ProtectKernelTunables=yes"
  "ProtectKernelModules=yes"
  "ProtectKernelLogs=yes"
  "ProtectControlGroups=yes"
  "LockPersonality=yes"
  "MemoryDenyWriteExecute=yes"
  "MemoryMax=256M"
  "TasksMax=32"
  "RestrictSUIDSGID=yes"
  "RestrictRealtime=yes"
  "SystemCallArchitectures=native"
  "UMask=0027"
)
hardening_complete=true
for directive in "${expected_hardening[@]}"; do
  grep -Fqx "$directive" <<<"$service_source" || hardening_complete=false
done
if [[ "$hardening_complete" == true ]] &&
   ! grep -Eq '^[[:space:]]*RestrictNamespaces[[:space:]]*=' <<<"$service_source" &&
   ! grep -Eq '^[[:space:]]*(ReadWriteDirectories|BindPaths)[[:space:]]*=' <<<"$service_source" &&
   grep -Fq "RestrictNamespaces is intentionally omitted" <<<"$service_source" &&
   ! grep -Fq "/run/user/1000" <<<"$service_source" &&
   ! grep -Fq ".local/share/containers" <<<"$service_source" &&
   grep -Fq "ReadWritePaths=/var/lib/paperclip-host-ops /var/log/paperclip-host-ops /var/spool/paperclip-host-ops" <<<"$docker_writable_paths"; then
  ok "rootful docker reads need no per-user runtime path inside the strict sandbox"
else
  fail "unit cannot run rootful docker reads inside its strict sandbox"
fi
# The transaction fixtures assert actual published modes, ownership and identical
# sudoers/mirror bytes, rather than pinning an obsolete direct-copy command.
if python3 "$ROOT/test_host_ops_install_transaction.py"; then
  ok "transaction publication and rollback contracts pass offline"
else
  fail "transaction publication or rollback contract drifted"
fi

# A partial setup must never appear as a mutation-capable install journal.
if python3 "$ROOT/test_host_ops_install_preparation.py"; then
  ok "journal preparation and atomic publication failure boundaries pass"
else
  fail "journal preparation or atomic publication contract drifted"
fi

# A fresh host has no broker identity for the upgrade-only installer. The
# bounded preparation script creates exactly that state and nothing else;
# its refusal table is the upgrade guarantee's front door.
if bash "$ROOT/test_host_ops_prepare_first_install.sh"; then
  ok "first-install preparation and refusal contracts pass offline"
else
  fail "first-install preparation or refusal contract drifted"
fi

# The pinned bundle manifest is the reviewable operator handoff: every
# reviewed tree member accounted for (no silent ride-along), every install
# destination resolved to reviewed bytes or the host-held key, export loop
# parsed from the reviewed installer itself, replacement objects refused.
if python3 "$ROOT/test_host_ops_install_manifest.py"; then
  ok "pinned bundle manifest and operator-command refusals pass"
else
  fail "bundle manifest or operator-command contract drifted"
fi

# ID-map arithmetic is a preparation check, never a live permission attestation.
if python3 "$ROOT/test_host_ops_spool_mapping.py"; then
  ok "offline spool ID-map and group-access refusals pass"
else
  fail "offline spool ID-map or group-access contract drifted"
fi

# Offline candidate generation must not imply host render/permission acceptance.
if python3 "$ROOT/test_host_ops_spool_carrier.py"; then
  ok "company-scoped carrier candidates and local DAC counterexamples pass"
else
  fail "company-scoped carrier or local DAC contract drifted"
fi

# Offline provisioning must mirror the reviewed broker allow-list exactly and
# hold the companies/ traverse guard the broker cannot see; a widened guard
# or loosened validation is a refusal, never a silent pass.
if python3 "$ROOT/test_host_ops_spool_provisioning.py"; then
  ok "company-spool provisioning mirror and traverse-guard refusals pass"
else
  fail "company-spool provisioning contract drifted"
fi

# case_954f2dc1/case_4f3123eb: the rendered grant must PARSE. Requested by the operator
# after PR #247 aborted on this host at `sudoers:10:114: syntax error` on the
# `@sha256:` token: sudoers reads a bare colon as the run-as separator. Both
# accepted forms carry a colon, so before the escape landed the ONLY reference
# that rendered a parseable grant was a bare repository name -- the hardcoded
# value this argument was added to replace. Rendering is not enough; only visudo
# on the RENDERED file catches this, which is why CI stayed green while the
# installer could not install.
render_parses() {
  local label="$1" candidate="$2" out rc target
  target="$render_dir/parse-check.sudoers"
  render_sudoers_image_ref "$ROOT/host-ops/host-ops-broker.sudoers" "$target" "$candidate"
  chmod 0440 "$target"
  set +e
  out=$("$VISUDO" -cf "$target" 2>&1)
  rc=$?
  set -e
  chmod 0640 "$target"
  if [[ $rc -eq 0 ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc $out)"
  fi
  # The grant sudo authorizes must still be the operator's exact reference:
  # a parse fixed by mangling the argument would attest the wrong image.
  local authorized
  authorized=$(grep -oP 'docker image inspect \K\S+' "$target" | sed 's/\\:/:/g')
  if [[ "$authorized" == "$candidate" ]]; then
    ok "$label grants the exact reference"
  else
    fail "$label granted $authorized, not $candidate"
  fi
}

VISUDO=$(command -v visudo || true)
if [[ -z "$VISUDO" && -x /usr/sbin/visudo ]]; then VISUDO=/usr/sbin/visudo; fi
if [[ -n "$VISUDO" ]]; then
  render_parses "rendered grant parses for a registry digest" \
    "ghcr.io/paperclipai/paperclip@sha256:$(printf 'f%.0s' {1..64})"
  render_parses "rendered grant parses for a tag" "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b"
  render_parses "rendered grant parses for a bare repository name" "paperclip-local"

  # Negative control: the unescaped render is what actually failed on the host.
  # Without this, a future change that dropped the escape would leave the three
  # assertions above passing only because visudo was absent.
  sed "s|@@PAPERCLIP_IMAGE_REF@@|ghcr.io/paperclipai/paperclip@sha256:$(printf 'f%.0s' {1..64})|" \
    "$ROOT/host-ops/host-ops-broker.sudoers" > "$render_dir/unescaped.sudoers"
  chmod 0440 "$render_dir/unescaped.sudoers"
  set +e
  "$VISUDO" -cf "$render_dir/unescaped.sudoers" >/dev/null 2>&1
  unescaped_rc=$?
  set -e
  chmod 0640 "$render_dir/unescaped.sudoers"
  if [[ $unescaped_rc -ne 0 ]]; then
    ok "control: an unescaped digest render is rejected by visudo"
  else
    fail "control: an unescaped digest render parsed, so the escape assertions prove nothing"
  fi
else
  fail "visudo is unavailable, so the rendered sudoers grant was never parse-checked"
fi

# E3 (CTO topology-evidence decision, case_477bab93 rev 8a515f6a): exactly one
# further exact-argv read-only grant, `systemctl --user show <pinned-unit>
# --no-pager --property=ActiveState,SubState,MainPID`. The unit name is host
# state captured by the operator during the consolidated host window -- pinned,
# never wildcarded, invented or defaulted -- and passed as the required
# --cliproxy-unit argument. The proposed OmniRoute unit read was dropped by
# that decision; no lifecycle verb (restart/start/stop/enable/reload) against
# any unit may ever appear in the allow-list (E2).
#
# Argument validation runs through the real install.sh with the EUID guard
# stubbed out, exactly as the case_954f2dc1 image-ref arms do, so the regex under
# test is the shipped one. The run still stops at the required-command loop
# before any host mutation.
cliproxy_unit_arg_arm() {
  local label="$1" expect="$2"
  shift 2
  local harness output rc
  harness=$(mktemp)
  sed -e 's/^\[\[ \$EUID -eq 0 \]\].*$/:/' \
      -e 's|^for command in git |for command in __toge3_absent__ git |' \
      "$ROOT/host-ops/install.sh" > "$harness"
  set +e
  output=$(bash "$harness" --source-ref "$(printf 'a%.0s' {1..40})" \
    --public-key-out /etc/paperclip-host-ops/response-signing.pub.pem --expected-public-key-sha256 "$(printf 'b%.0s' {1..64})" \
    "$@" \
    --paperclip-image-ref paperclip-local 2>&1)
  rc=$?
  set -e
  rm -f "$harness"
  if [[ $rc -eq 2 && "$output" == *"$expect"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

# A missing unit refuses: there is no default, no wildcard and no invented
# name, and without this arm the harness could pass while install.sh silently
# shipped an empty grant.
cliproxy_unit_arg_arm "a missing --cliproxy-unit is refused" \
  "--cliproxy-unit must be a plain .service unit name"
# A colon is the sudoers run-as separator: a colon-bearing unit renders a file
# visudo rejects (probed on 1.9.16p2: `show evil:reader.service` fails at the
# colon), so it must refuse in argument validation, never at the host.
cliproxy_unit_arg_arm "a colon-bearing --cliproxy-unit is refused" \
  "--cliproxy-unit must not carry a colon" \
  --cliproxy-unit "evil:reader.service"
cliproxy_unit_arg_arm "a whitespace-carrying --cliproxy-unit is refused" \
  "--cliproxy-unit must be a plain .service unit name" \
  --cliproxy-unit "cliproxy service"
cliproxy_unit_arg_arm "a path-carrying --cliproxy-unit is refused" \
  "--cliproxy-unit must be a plain .service unit name" \
  --cliproxy-unit "../cliproxy.service"
cliproxy_unit_arg_arm "a shell-metacharacter --cliproxy-unit is refused" \
  "--cliproxy-unit must be a plain .service unit name" \
  --cliproxy-unit "cliproxy;id.service"
cliproxy_unit_arg_arm "a grant-widening --cliproxy-unit is refused" \
  "--cliproxy-unit must not carry a colon" \
  --cliproxy-unit "cliproxy.service, ALL=(ALL) NOPASSWD: ALL"
cliproxy_unit_arg_arm "a non-service suffix is refused" \
  "--cliproxy-unit must be a plain .service unit name" \
  --cliproxy-unit "cliproxy.timer"
# Positive control: a plain captured unit name gets PAST argument validation,
# proving the refusals above are caused by the unit and not by the harness.
cliproxy_unit_arg_arm "a plain captured unit name passes the E3 guard" \
  "required command missing: __toge3_absent__" \
  --cliproxy-unit "cliproxy.service"

# Both layers refuse the colon, not just install.sh's argv check: the shared
# lib renderer is the last function to touch the grant before visudo, so a
# change that dropped its refusal would render a file the host rejects. These
# call the SHIPPED lib function directly against the shipped template.
lib_template="$render_dir/lib-template.sudoers"
cp "$ROOT/host-ops/host-ops-broker.sudoers" "$lib_template"
render_sudoers_image_ref "$lib_template" "$lib_template" "$render_ref"
expect_refusal "the lib renderer refuses a colon-bearing unit" \
  "must not carry a colon" \
  render_sudoers_cliproxy_unit "$lib_template" "$render_dir/lib-out.sudoers" "evil:reader.service"
render_sudoers_cliproxy_unit "$lib_template" "$render_dir/lib-out.sudoers" "cliproxy.service" \
  && ok "the lib renderer accepts a plain unit" \
  || fail "the lib renderer accepts a plain unit"

# The installer must actually call the cliproxy render before visudo: a grant
# that parsed with an UNRENDERED placeholder (probed rc=0) means dropping the
# call would ship a three-grant sudoers while every arg arm above still passed.
# Read install.sh live, never retype the call.
if grep -Fqx 'render_sudoers_cliproxy_unit "$SUDOERS_TMP" "$SUDOERS_TMP" "$CLIPROXY_UNIT"' <<<"$install_source" &&
   grep -Fqx 'render_sudoers_image_ref "$SOURCE_DIR/host-ops-broker.sudoers" "$SUDOERS_TMP" "$PAPERCLIP_IMAGE_REF"' <<<"$install_source"; then
  ok "install.sh renders both image and unit before visudo"
else
  fail "install.sh does not render both image and unit before visudo"
fi

# The rendered grant must name exactly the captured unit with the exact E3 argv
# and nothing else, rendered through the SHIPPED functions -- never a local
# sed -- and parse-checked by the same real visudo as the case_954f2dc1 arms above.
# The fully rendered file (image AND unit) is what parses: an unrendered
# @@PAPERCLIP_CLIPROXY_UNIT@@ placeholder parses too (probed rc=0), so a gate
# that rendered only the image reference would attest nothing about this grant.
render_e3_parses() {
  local label="$1" unit="$2" out rc target
  target="$render_dir/e3-check.sudoers"
  render_sudoers_image_ref "$ROOT/host-ops/host-ops-broker.sudoers" "$target" "$render_ref"
  render_sudoers_cliproxy_unit "$target" "$target" "$unit"
  chmod 0440 "$target"
  set +e
  out=$("$VISUDO" -cf "$target" 2>&1)
  rc=$?
  set -e
  chmod 0640 "$target"
  if [[ $rc -eq 0 ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc $out)"
  fi
  if grep -Fq '@@PAPERCLIP_CLIPROXY_UNIT@@' "$target"; then
    fail "$label left the unit placeholder unrendered"
  else
    ok "$label renders the unit placeholder"
  fi
  # Exact fourth-grant shape: the pinned unit with the E3 argv and nothing
  # else. No ExecMainStartTimestampMonotonic (the paperclip.service grant's),
  # no extra property, no second grant.
  if grep -Fqx "  /usr/bin/env XDG_RUNTIME_DIR=/run/user/1000 /usr/bin/systemctl --user show $unit --no-pager --property=ActiveState\\,SubState\\,MainPID" "$target"; then
    ok "$label grants exactly the captured unit with the E3 argv"
  else
    fail "$label does not carry the exact E3 grant for $unit"
  fi
  # E2 pin: the only systemctl verb anywhere in the rendered allow-list is
  # `show`. A lifecycle verb (restart/start/stop/enable/reload, including
  # daemon-reload and is-active) against any unit refuses the render here, so
  # it can never reach the protected grant through this change.
  # case_81816394 node7b1ff338 Docker port: exactly ONE `systemctl --user show` grant
  # remains (the E3 CLIProxy read). The retired paperclip.service user-unit
  # grant is gone -- host operator verified 2026-10-05 that node7b1ff338 runs no such
  # unit, and `docker inspect` is the container-state source instead.
  if grep -Eq 'systemctl --user (restart|start|stop|enable|reload|daemon-reload|is-active)( |$)' "$target"; then
    fail "$label carries a lifecycle verb against a unit (E2)"
  elif [[ $(grep -co 'systemctl --user show ' "$target") -eq 1 ]]; then
    ok "$label carries no lifecycle verb (E2)"
  else
    fail "$label changed the systemctl grant count"
  fi
}

render_e3_parses "rendered E3 grant parses for the captured unit" "cliproxy.service"
render_e3_parses "rendered E3 grant parses for an instanced unit name" "cliproxy@fixture.service"

# case_b981db13: --public-key-out must not collide with the installer's own PUBLIC_KEY.
#
# `install "$PUBLIC_KEY" "$PUBLIC_KEY_OUT"` is the last step before daemon-reload.
# GNU install exits 1 on "are the same file", and under `set -Eeuo pipefail` that
# aborted the script with sudoers, the unit, tmpfiles and config.json already on
# the host but the unit never reloaded -- a DIRTY failure. case_4f3123eb step 2 carried
# exactly that path and nearly spent a host window on it.
#
# The keys path is READ OUT OF THE SHIPPED SCRIPT, never re-typed here. A test
# that hardcoded /var/lib/paperclip-host-ops/keys/... would keep passing against
# its own stale literal after the constant moved, which is precisely how the
# original gap survived: both existing arms pass /tmp/none.pem, so nothing ever
# joined the flag to the constant.
INSTALLER_PUBLIC_KEY=$(sed -n 's/^PUBLIC_KEY=\(.*\)$/\1/p' "$ROOT/host-ops/install.sh")
if [[ -n "$INSTALLER_PUBLIC_KEY" ]]; then
  ok "installer's PUBLIC_KEY constant is readable ($INSTALLER_PUBLIC_KEY)"
else
  fail "could not read PUBLIC_KEY from install.sh, so the collision arms below prove nothing"
fi

# Bound the run before any host mutation, exactly as the case_954f2dc1 arms do: stub the
# EUID guard so the refusal under test is the collision and not "must run as
# root", and break the required-command loop so a run that gets PAST the guard
# stops at the first step after it. The guard is deliberately placed above that
# loop -- it uses only bash builtins -- so both arms are reachable here.
public_key_out_arm() {
  local label="$1" candidate="$2" expect="$3" harness output rc
  harness=$(mktemp)
  sed -e 's/^\[\[ \$EUID -eq 0 \]\].*$/:/' \
      -e 's|^for command in git |for command in __case_c02d1c6a_absent__ git |' \
      "$ROOT/host-ops/install.sh" > "$harness"
  set +e
  output=$(bash "$harness" --source-ref "$(printf 'a%.0s' {1..40})" \
    --public-key-out "$candidate" --expected-public-key-sha256 "$(printf 'b%.0s' {1..64})" \
    --cliproxy-unit cliproxy.service \
    --paperclip-image-ref paperclip-local 2>&1)
  rc=$?
  set -e
  rm -f "$harness"
  if [[ $rc -eq 2 && "$output" == *"$expect"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

# Arm A: the exact constant is refused.
public_key_out_arm "--public-key-out equal to the installer's own public key is refused" \
  "$INSTALLER_PUBLIC_KEY" "must not name the installer's own public key"

# Arm A': the same file spelled differently is refused too. A string comparison
# would pass this one, so these arms are what force normalised-path comparison.
# The `..` alias bounces off the key's OWN parent directory name rather than a
# literal "keys", so the arm keeps testing the alias -- not a stale path -- when
# the constant moves.
INSTALLER_KEY_DIR=$(dirname "$INSTALLER_PUBLIC_KEY")
public_key_out_arm "--public-key-out aliased via .. is refused" \
  "$INSTALLER_KEY_DIR/../$(basename "$INSTALLER_KEY_DIR")/$(basename "$INSTALLER_PUBLIC_KEY")" \
  "must not name the installer's own public key"
public_key_out_arm "--public-key-out aliased via a trailing /. is refused" \
  "$(dirname "$INSTALLER_PUBLIC_KEY")/./$(basename "$INSTALLER_PUBLIC_KEY")" \
  "must not name the installer's own public key"

# Arm B / positive control: a DISTINCT destination must get PAST the collision
# guard. Without this the refusals above would pass even if the guard refused
# unconditionally -- the failure mode that would break every real install.
public_key_out_arm "a distinct --public-key-out passes the collision guard" \
  /etc/paperclip-host-ops/response-signing.pub.pem \
  "required command missing: __case_c02d1c6a_absent__"

if (( failures != 0 )); then
  printf '%s host-ops installer contract tests failed\n' "$failures" >&2
  exit 1
fi
python3 "$ROOT/test_host_ops_image_binding.py"
python3 "$ROOT/test_host_ops_install_state.py"
printf 'host-ops installer contract tests passed\n'
