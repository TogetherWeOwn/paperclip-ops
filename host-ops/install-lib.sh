#!/usr/bin/env bash

# Render the image reference into the exact-argv sudoers grant.
#
# Every colon becomes "\:". In sudoers a bare colon separates the run-as spec
# from the command, so an unescaped one is a syntax error that visudo rejects --
# and BOTH accepted reference forms carry one (name:tag and name@sha256:<64hex>),
# so without the escape the only reference that ever rendered was a bare
# repository name. sudo strips the escape before matching argv, and broker.py
# unescapes this file before comparing it to config.json, so the grant and the
# config still name the same image.
#
# This lives in the shared lib so the installer and the test suite render
# through one implementation: a test that re-implemented the substitution could
# pass while the shipped installer aborted, which is how this defect reached an
# operator's host window.
render_sudoers_image_ref() {
  local template="$1" destination="$2" image_ref="$3"
  grep -Fq '@@PAPERCLIP_IMAGE_REF@@' "$template" || refuse "reviewed sudoers template is missing the image-reference placeholder"
  python3 - "$template" "$destination" "$image_ref" <<'PY'
import sys
from pathlib import Path
template, destination, image_ref = sys.argv[1], sys.argv[2], sys.argv[3]
Path(destination).write_text(
    Path(template).read_text().replace("@@PAPERCLIP_IMAGE_REF@@", image_ref.replace(":", "\\:"))
)
PY
}

# Render the operator-captured CLIProxy unit name into the exact-argv sudoers
# grant (E3 of the CTO topology-evidence decision, case_477bab93 rev 8a515f6a).
#
# Like the image reference, the unit name is host state, not source state: the
# operator captures it during the consolidated host window and passes it as
# install.sh's required --cliproxy-unit argument. There is no default, no
# wildcard and no invented name here. The accepted shape is a systemd unit name
# (basename plus a known unit suffix, no path, no specifier, no whitespace and
# no colon -- a bare colon is the sudoers run-as separator); anything else
# refuses before mutation. No lifecycle verb may reach this grant through the
# unit name: the suffix allow-list admits only `.service`, and a name carrying
# whitespace, a shell metacharacter or a second argv token never renders.
#
# This lives in the shared lib next to the image-ref renderer so the installer
# and the test suite render through one implementation.
render_sudoers_cliproxy_unit() {
  local template="$1" destination="$2" unit="$3"
  # Refuse the colon rather than escaping it: sudoers reads a bare colon as the
  # run-as separator, so a colon-bearing unit renders a file visudo rejects
  # (probed 2026-09-28 on visudo 1.9.16p2: `show evil:reader.service` fails at
  # the colon). Unlike the image reference -- whose tag and digest forms
  # legitimately carry a colon -- there is nothing to escape to here: a unit
  # that cannot be named in this grammar cannot be granted by it.
  [[ "$unit" != *":"* ]] || refuse "reviewed CLIProxy unit name must not carry a colon"
  [[ "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || refuse "reviewed CLIProxy unit name is not a plain .service unit"
  [[ "$unit" != *".."* && "$unit" != *"/"* && "$unit" != *" "* ]] || refuse "reviewed CLIProxy unit name must not carry a path"
  grep -Fq '@@PAPERCLIP_CLIPROXY_UNIT@@' "$template" || refuse "reviewed sudoers template is missing the CLIProxy-unit placeholder"
  python3 - "$template" "$destination" "$unit" <<'PY'
import sys
from pathlib import Path
template, destination, unit = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(template).read_text()
if "@@PAPERCLIP_CLIPROXY_UNIT@@" not in text:
    raise SystemExit("REFUSED: reviewed sudoers template is missing the CLIProxy-unit placeholder")
rendered = text.replace("@@PAPERCLIP_CLIPROXY_UNIT@@", unit)
if "@@PAPERCLIP_CLIPROXY_UNIT@@" in rendered:
    raise SystemExit("REFUSED: CLIProxy-unit placeholder survived rendering")
Path(destination).write_text(rendered)
PY
}

# Upgrade preparation must not inherit an enabled/test configuration. Per-verb
# gates are source constants in broker.py and remain false; there is no runtime
# per-verb enablement setting in the deployed config schema.
render_disabled_config() {
  local source="$1" destination="$2" image_ref="$3"
  python3 - "$source" "$destination" "$image_ref" <<'PY'
import json, sys
from pathlib import Path
source, destination, image_ref = sys.argv[1:]
try:
    config = json.loads(Path(source).read_text())
except (OSError, ValueError):
    raise SystemExit("REFUSED: install config is unreadable or invalid") from None
if not isinstance(config, dict):
    raise SystemExit("REFUSED: install config must be an object")
config["paperclipImageRef"] = image_ref
config["writesEnabled"] = False
config["testMode"] = False
Path(destination).write_text(json.dumps(config, indent=2) + "\n")
PY
}

classify_service_identity() {
  local present=0
  getent group paperclip-host-reader >/dev/null && ((present += 1))
  getent group paperclip-host-requests >/dev/null && ((present += 1))
  id paperclip-host-reader >/dev/null 2>&1 && ((present += 1))
  if (( present == 0 )); then
    printf 'absent\n'
  elif (( present == 3 )); then
    validate_service_identity
    printf 'valid\n'
  else
    refuse "paperclip-host-reader identity is partially configured"
  fi
}

# Preparation journal root (production path). The installer never reads this
# from the environment: tests reassign the variable in subshells, exactly like
# the prepare script's own JOURNAL_ROOT.
PREPARE_JOURNAL_ROOT=/var/lib/paperclip-host-ops-installer

# Membership-boundary exception (case_4a4d7340 follow-up). The requests group may
# carry pre-existing members observed and journaled by bounded preparation
# (node7b1ff338: the host operator account). Preparation never edits membership
# (useradd -G only appends the reader), so the installer enforces zero drift:
# current members must equal the journaled pre-existing set plus
# paperclip-host-reader. Any addition or removal between preparation and
# install refuses, as does a journal that fails to bind to the current group
# and reader ids. With no preparation journal on the host, the original
# strict rule applies (reader only). Membership is never evicted and never
# widened by either script: the only membership mutation in the whole flow is
# the reader joining at preparation time.
member_in_csv() {
  local needle="$1" csv="$2" item
  [[ -n "$csv" ]] || return 1
  local IFS=','
  local -a entries=()
  read -r -a entries <<< "$csv"
  for item in "${entries[@]}"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

valid_member_name() {
  [[ "$1" =~ ^[A-Za-z0-9_.-]+$ ]]
}

# Order-insensitive exact multiset equality for member CSVs: empty equals empty,
# otherwise each name must occur the same number of times on both sides, so a
# duplicate on one side never matches a missing entry on the other. Used to
# bind the operator-supplied expected list to the observed group at preparation
# time. Returns 0 on exact match.
csv_set_equals() {
  local left="$1" right="$2"
  if [[ -z "$left" && -z "$right" ]]; then
    return 0
  fi
  [[ -n "$left" && -n "$right" ]] || return 1
  local -a larr=() rarr=()
  IFS=',' read -r -a larr <<< "$left"
  IFS=',' read -r -a rarr <<< "$right"
  [[ "$(printf '%s\n' "${larr[@]}" | sort)" == "$(printf '%s\n' "${rarr[@]}" | sort)" ]]
}

# Primary-group holders are invisible to `getent group` member lists yet gain
# the same DAC access as the group's members (the mode-3730 requests spool, the
# reader's 0750 trees). Refuse when any account other than the broker reader
# holds the group as its primary group. An enumeration that fails, comes back
# empty or carries an unreadable entry refuses too: no evidence is not proof.
check_no_primary_holders() {
  local gid="$1" group="$2" table entry name _ egid
  table=$(getent passwd 2>/dev/null) || refuse "passwd table is unreadable; cannot prove no primary-group holder of $group"
  [[ -n "$table" ]] || refuse "passwd table enumerated empty; cannot prove no primary-group holder of $group"
  while IFS= read -r entry; do
    IFS=: read -r name _ _ egid _ <<< "$entry"
    [[ -n "$name" && "$egid" =~ ^[0-9]+$ ]] || \
      refuse "passwd table holds an unreadable entry; cannot prove no primary-group holder of $group"
    if [[ "$egid" == "$gid" && "$name" != paperclip-host-reader ]]; then
      refuse "account $name holds $group as its primary group; separate review required"
    fi
  done <<< "$table"
  return 0
}

# Membership and primary-holder enumeration are complete only when the account
# and group databases are local: a directory source can add members that
# `getent group` does not show, or primary holders that `getent passwd` does
# not list. NSSWITCH_CONF is overridable for tests only.
require_local_account_sources() {
  # Byte-wise matching: under a UTF-8 locale a line with an invalid byte makes
  # every `=~` fail, which would skip the line while glibc still reads it.
  local LC_ALL=C
  local conf="${NSSWITCH_CONF:-/etc/nsswitch.conf}" line stripped db rest sources
  local seen_passwd= seen_group=
  [[ -r "$conf" ]] || refuse "name service switch is unreadable; account sources cannot be proven local"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(.*)$ ]] || refuse "name service switch line cannot be parsed; account sources cannot be proven local"
    stripped="${BASH_REMATCH[1]}"
    [[ -z "$stripped" || "$stripped" == \#* ]] && continue
    [[ "$stripped" =~ ^(passwd|group|initgroups)([[:space:]:].*|$) ]] || continue
    db="${BASH_REMATCH[1]}"
    [[ "$db" == passwd ]] && seen_passwd=1
    [[ "$db" == group ]] && seen_group=1
    rest="${stripped#"$db"}"
    [[ "$rest" =~ ^[[:space:]]*:?[[:space:]]*(.*)$ ]] || refuse "$db sources cannot be parsed; separate review required"
    sources="${BASH_REMATCH[1]}"
    # No trailing-`#` stripping: glibc 2.33 and later does not treat `#` as a
    # comment inside the source list, so any `#` below is a non-local source.
    [[ "$sources" =~ ^[[:space:]]*files[[:space:]]*$ ]] || \
      refuse "$db must use local files only (nsswitch: $line); separate review required"
  done < "$conf"
  # A missing database falls back to glibc's default (`nis [NOTFOUND=return]
  # files`), which is not local-only, so both lines must be present.
  [[ -n "$seen_passwd" ]] || refuse "passwd database is absent from $conf; account sources cannot be proven local"
  [[ -n "$seen_group" ]] || refuse "group database is absent from $conf; account sources cannot be proven local"
}

# The journal root and its entries are root-owned and 0700 from creation; the
# installer checks them rather than assuming they stayed that way.
check_journal_trust() {
  local root="$1" entry want="${PREPARE_JOURNAL_OWNER_UID:-0}:${PREPARE_JOURNAL_OWNER_GID:-0}"
  [[ -d "$root" && ! -L "$root" ]] || \
    refuse "preparation journal root is not a directory: $root; separate review required"
  [[ "$(stat -c '%u:%g %a' "$root")" == "$want 700" ]] || \
    refuse "preparation journal root has unexpected owner or mode: $root; separate review required"
  while IFS= read -r entry; do
    [[ -d "$entry" && ! -L "$entry" ]] || \
      refuse "preparation journal entry is not a directory: $entry; separate review required"
    [[ "$(stat -c '%u:%g %a' "$entry")" == "$want 700" ]] || \
      refuse "preparation journal entry has unexpected owner or mode: $entry; separate review required"
    if [[ -e "$entry/prepare.json" || -L "$entry/prepare.json" ]]; then
      [[ -f "$entry/prepare.json" && ! -L "$entry/prepare.json" ]] || \
        refuse "preparation journal is not a regular file: $entry/prepare.json; separate review required"
      [[ "$(stat -c '%u:%g' "$entry/prepare.json")" == "$want" ]] || \
        refuse "preparation journal has unexpected owner: $entry/prepare.json; separate review required"
    fi
  done < <(find "$root" -mindepth 1 -maxdepth 1 -name 'prepare-*')
}

newest_prepare_journal() {
  local root="${PREPARE_JOURNAL_ROOT:-/var/lib/paperclip-host-ops-installer}"
  local candidate
  candidate=$(ls -td "$root"/prepare-*/prepare.json 2>/dev/null | head -n 1) || return 1
  [[ -n "$candidate" && -f "$candidate" && ! -L "$candidate" ]] || return 1
  printf '%s\n' "$candidate"
}

# Prints the value of one journal key (bare or quoted scalar). Empty-string
# values print empty with success; a missing key fails. Callers validate the
# shape for their key: this stays lexical on purpose, never a JSON parser.
prepare_journal_value() {
  local file="$1" key="$2" raw
  raw=$(grep -F "\"$key\":" "$file" | head -n 1) || return 1
  raw="${raw#*:}"
  raw="${raw//[[:space:]]/}"
  raw="${raw#\"}"
  raw="${raw%\",}"
  raw="${raw%\"}"
  raw="${raw%,}"
  printf '%s' "$raw"
  return 0
}

validate_requests_group_membership() {
  local members="$1" gid="$2" reader_uid="$3" reader_gid="$4"
  local journal phase adopted jgid jruid jrgid jmembers jexpected item
  local journal_root="${PREPARE_JOURNAL_ROOT:-/var/lib/paperclip-host-ops-installer}"
  if [[ -e "$journal_root" || -L "$journal_root" ]]; then
    check_journal_trust "$journal_root"
  fi
  if ! journal=$(newest_prepare_journal); then
    [[ "$members" == paperclip-host-reader ]] || refuse "paperclip-host-requests must contain only paperclip-host-reader"
    return 0
  fi
  # A journal exists: it must fully bind, or refuse explicitly. Falling back
  # to the strict rule here would misreport a binding failure as a membership
  # failure, and could wave through a group the journal never covered.
  phase=$(prepare_journal_value "$journal" phase) || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  [[ "$phase" == prepared ]] || \
    refuse "preparation journal is not a completed preparation ($journal); separate review required"
  [[ "$(tail -n 1 "$journal")" == "}" ]] || \
    refuse "preparation journal is incomplete ($journal); separate review required"
  adopted=$(prepare_journal_value "$journal" adoptedRequestsGroup) || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  if [[ "$adopted" == false ]]; then
    [[ "$members" == paperclip-host-reader ]] || refuse "paperclip-host-requests must contain only paperclip-host-reader"
    return 0
  fi
  [[ "$adopted" == true ]] || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  jgid=$(prepare_journal_value "$journal" requestsGid) || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  jruid=$(prepare_journal_value "$journal" readerUid) || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  jrgid=$(prepare_journal_value "$journal" readerGid) || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  # The members field postdates the first preparation journals: absent means
  # none were adopted, which is exactly the strict case.
  if grep -qF '"preexistingRequestsMembers":' "$journal"; then
    jmembers=$(prepare_journal_value "$journal" preexistingRequestsMembers) || \
      refuse "preparation journal is unreadable ($journal); separate review required"
  else
    jmembers=""
  fi
  [[ "$jgid" =~ ^[0-9]+$ && "$jruid" =~ ^[0-9]+$ && "$jrgid" =~ ^[0-9]+$ ]] || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  [[ "$jmembers" =~ ^[A-Za-z0-9_.-]*(,[A-Za-z0-9_.-]+)*$ ]] || \
    refuse "preparation journal is unreadable ($journal); separate review required"
  if [[ -n "$jmembers" ]]; then
    grep -qF '"expectedRequestsMembers":' "$journal" || \
      refuse "preparation journal lacks the operator-asserted member list ($journal); separate review required"
    jexpected=$(prepare_journal_value "$journal" expectedRequestsMembers) || \
      refuse "preparation journal is unreadable ($journal); separate review required"
    [[ "$jexpected" =~ ^[A-Za-z0-9_.-]*(,[A-Za-z0-9_.-]+)*$ ]] || \
      refuse "preparation journal is unreadable ($journal); separate review required"
    csv_set_equals "$jmembers" "$jexpected" || \
      refuse "preparation journal pins members ($jmembers) that do not match the operator-asserted list ($jexpected); separate review required"
  fi
  [[ "$jgid" == "$gid" && "$jruid" == "$reader_uid" && "$jrgid" == "$reader_gid" ]] || \
    refuse "preparation journal does not match the current identity (journal $journal); separate review required"
  member_in_csv paperclip-host-reader "$members" || \
    refuse "paperclip-host-requests does not contain the broker reader; separate review required"
  local -a current=() prior=()
  IFS=',' read -r -a current <<< "$members"
  for item in "${current[@]}"; do
    valid_member_name "$item" || \
      refuse "paperclip-host-requests holds an unreadable member entry; separate review required"
    [[ "$item" == paperclip-host-reader ]] || member_in_csv "$item" "$jmembers" || \
      refuse "paperclip-host-requests gained member $item since preparation; separate review required"
  done
  if [[ -n "$jmembers" ]]; then
    IFS=',' read -r -a prior <<< "$jmembers"
    for item in "${prior[@]}"; do
      member_in_csv "$item" "$members" || \
        refuse "paperclip-host-requests lost pre-existing member $item since preparation; separate review required"
    done
  fi
  return 0
}

validate_service_identity() {
  local IFS=$' \t\n'
  local reader_group requests_group reader_passwd password_state
  local reader_group_name reader_gid reader_group_members
  local requests_group_name requests_gid requests_group_members
  local reader_name reader_uid reader_primary_gid reader_home reader_shell
  local -a reader_groups

  require_local_account_sources
  reader_group=$(getent group paperclip-host-reader) || refuse "paperclip-host-reader group is unreadable"
  IFS=: read -r reader_group_name _ reader_gid reader_group_members <<< "$reader_group"
  [[ "$reader_group_name" == paperclip-host-reader && "$reader_gid" =~ ^[0-9]+$ && $reader_gid -gt 0 && $reader_gid -lt 1000 ]] || \
    refuse "paperclip-host-reader must be a system group"
  [[ -z "$reader_group_members" ]] || refuse "paperclip-host-reader has unintended explicit members"
  check_no_primary_holders "$reader_gid" paperclip-host-reader

  # The reader account parses before the requests group because the
  # journal-pinned membership check binds to the reader ids.
  reader_passwd=$(getent passwd paperclip-host-reader) || refuse "paperclip-host-reader account is unreadable"
  IFS=: read -r reader_name _ reader_uid reader_primary_gid _ reader_home reader_shell <<< "$reader_passwd"
  [[ "$reader_name" == paperclip-host-reader && "$reader_uid" =~ ^[0-9]+$ && $reader_uid -gt 0 && $reader_uid -lt 1000 ]] || \
    refuse "paperclip-host-reader must be a system account"

  requests_group=$(getent group paperclip-host-requests) || refuse "paperclip-host-requests group is unreadable"
  IFS=: read -r requests_group_name _ requests_gid requests_group_members <<< "$requests_group"
  [[ "$requests_group_name" == paperclip-host-requests && "$requests_gid" =~ ^[0-9]+$ && $requests_gid -gt 0 && $requests_gid -lt 1000 ]] || \
    refuse "paperclip-host-requests must be a system group"
  # Primary-group holders bypass the member-list pin: getent group never shows
  # them, yet the kernel grants them the same spool and config access. Refuse
  # closed here in both the strict and journal-pinned paths.
  check_no_primary_holders "$requests_gid" paperclip-host-requests
  validate_requests_group_membership "$requests_group_members" "$requests_gid" "$reader_uid" "$reader_gid"

  [[ "$reader_primary_gid" == "$reader_gid" ]] || refuse "paperclip-host-reader primary group is incompatible"
  [[ "$reader_home" == /var/lib/paperclip-host-ops ]] || refuse "paperclip-host-reader home is incompatible"
  [[ "$reader_shell" == /usr/sbin/nologin ]] || refuse "paperclip-host-reader shell is incompatible"
  read -r _ password_state _ <<< "$(passwd -S paperclip-host-reader 2>/dev/null)"
  [[ "$password_state" == L || "$password_state" == LK ]] || refuse "paperclip-host-reader credentials must be locked"
  read -r -a reader_groups <<< "$(id -nG paperclip-host-reader)"
  [[ ${#reader_groups[@]} -eq 2 ]] || refuse "paperclip-host-reader has unintended supplemental groups"
  [[ " ${reader_groups[*]} " == *" paperclip-host-reader "* && " ${reader_groups[*]} " == *" paperclip-host-requests "* ]] || \
    refuse "paperclip-host-reader group membership is incompatible"
}

validate_response_signing_key() {
  local private_key="$1" public_key="$2" expected_sha256="$3" work_dir="$4"
  local key_description key_bits derived_public derived_fingerprint provided_fingerprint
  local signing_probe signing_signature

  [[ -f "$private_key" && -f "$public_key" ]] || {
    printf '%s\n' "REFUSED: pre-create the response signing key and public key in /var/lib/paperclip-host-ops/keys, record the public-key SHA-256 independently, then rerun the installer" >&2
    printf '%s\n' "The installer never generates this key: generating and approving its pin in one operation would make the pin self-authenticating." >&2
    exit 2
  }
  [[ ! -L "$private_key" && ! -L "$public_key" ]] || refuse "signing key paths must not be symlinks"
  openssl pkey -in "$private_key" -check -noout >/dev/null 2>&1 || refuse "response signing private key is invalid"
  openssl rsa -in "$private_key" -check -noout >/dev/null 2>&1 || refuse "response signing key must be a valid RSA private key"
  key_description=$(openssl rsa -in "$private_key" -text -noout 2>/dev/null | { IFS= read -r line; printf '%s\n' "$line"; }) || \
    refuse "response signing key size could not be read"
  [[ "$key_description" =~ ^Private-Key:\ \(([0-9]+)\ bit, ]] || refuse "response signing key size could not be read"
  key_bits=${BASH_REMATCH[1]}
  (( key_bits >= 2048 )) || refuse "response signing RSA key must be at least 2048 bits"

  derived_public="$work_dir/derived-public.pem"
  openssl pkey -in "$private_key" -pubout -out "$derived_public" >/dev/null 2>&1 || refuse "could not derive public key from response signing private key"
  derived_fingerprint=$(openssl pkey -pubin -in "$derived_public" -outform DER 2>/dev/null | sha256sum)
  provided_fingerprint=$(openssl pkey -pubin -in "$public_key" -outform DER 2>/dev/null | sha256sum) || refuse "response signing public key is invalid"
  [[ "${derived_fingerprint%% *}" == "${provided_fingerprint%% *}" ]] || refuse "response signing private and public keys do not match"

  signing_probe="$work_dir/signing-probe"
  signing_signature="$work_dir/signing-probe.sig"
  printf 'paperclip-host-ops-signing-check\n' > "$signing_probe"
  openssl dgst -sha256 -sign "$private_key" -out "$signing_signature" "$signing_probe" >/dev/null 2>&1 || \
    refuse "response signing private key does not support RSA/SHA-256 signing"
  openssl dgst -sha256 -verify "$public_key" -signature "$signing_signature" "$signing_probe" >/dev/null 2>&1 || \
    refuse "response signing public key cannot verify the configured signature contract"

  ACTUAL_PUBLIC_KEY_SHA256=$(sha256sum "$public_key")
  ACTUAL_PUBLIC_KEY_SHA256=${ACTUAL_PUBLIC_KEY_SHA256%% *}
  [[ "$ACTUAL_PUBLIC_KEY_SHA256" == "$expected_sha256" ]] || {
    printf '%s\n' "REFUSED: existing public key does not match independently expected hash" >&2
    printf 'actual public-key sha256: %s\n' "$ACTUAL_PUBLIC_KEY_SHA256" >&2
    exit 2
  }
}
