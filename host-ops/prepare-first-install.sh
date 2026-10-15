#!/usr/bin/env bash
# Bounded first-install preparation for the host-ops broker (fresh hosts only).
#
# The released installer (host-ops/install.sh) is upgrade-only: it refuses
# unless the broker identity, protected directories and broker-owned keys
# already exist. On a fresh host that refusal is correct but leaves the
# operator with no executable path. This script is that path's first step.
#
# Scope (deliberately narrow):
# - Creates the reader group and account the installer validates, and the
#   requests group unless an existing one is adopted (below), with the exact
#   properties install-lib.sh requires.
# - Creates the protected control directories and the broker runtime
#   directories with the exact modes/ownership install_transaction.py
#   requires (mirroring host-ops-broker.tmpfiles).
# - Adopts a pre-existing requests group ONLY when it is a system group, WITH
#   its members preserved exactly as observed AND asserted via
#   --expect-requests-members: membership is never edited (useradd -G only
#   appends the reader), the observed list must equal the operator-supplied
#   expected list before any mutation, both are pinned in the journal, and the
#   installer refuses any later addition or removal. Without the flag, a group
#   carrying members refuses.
#   Adopts pre-existing spool directories ONLY when every existing path is an
#   empty directory. Anything else refuses.
# - Never generates, rotates, repairs, reads or moves keys: both signing-key
#   paths must be absent, and the operator creates them afterwards in a
#   controlled host session (RUNBOOK step 2) with an independently recorded
#   hash. Never widens socket/group access, never changes carriers or images,
#   never enables writes, never restarts anything.
#
# Rollback and evidence: the script writes an exclusive JSON journal under
# /var/lib/paperclip-host-ops-installer/prepare-<token>/ before its first
# identity change, marks it "phase": "aborted" on any failure after that, and
# writes "phase": "prepared" just before the final installer-contract gate; a
# refusal there marks it aborted.
# The installer refuses every phase except "prepared". On failure the script
# prints the exact rollback commands (userdel/groupdel/rmdir/chown-restore).
# rmdir fails safe on non-empty directories; rollback is valid only before the
# operator key step that follows preparation. The journal root and the per-run
# journal directory are evidence: rollback never removes them.
#
# Sourcing contract: sourcing this file defines functions only. The EUID
# check and main() run only when executed. Tests source it with stubbed
# getent/id/passwd/systemctl/useradd/groupadd commands.

set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  cat <<'USAGE'
Usage:
  sudo ./host-ops/prepare-first-install.sh [--expect-requests-members <csv>]

Creates the broker identity and protected directories on a FRESH host only,
so the upgrade-only installer can run afterwards. Takes no secrets and only
the one adoption-assertion flag. Refuses when any identity, key, service,
control directory or non-empty spool state already exists outside the narrow
adoption rules. Prints a journal path and explicit rollback commands on success.

  --expect-requests-members <csv>  Exact pre-existing member list the operator
    recorded in the install packet (e.g. ubuntu for node7b1ff338 gid 986). Required
    when the pre-existing requests group has any members: preparation refuses
    unless the observed set equals this list exactly before any mutation, and
    pins both lists in the journal. With no flag, a group carrying members
    refuses. The reader itself must never appear in this list.
USAGE
}

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  if [[ "$HOST_MUTATED" == true ]]; then
    report_partial_rollback
  fi
  exit 2
}

READER_USER=paperclip-host-reader
READER_GROUP=paperclip-host-reader
REQUESTS_GROUP=paperclip-host-requests
READER_HOME=/var/lib/paperclip-host-ops
READER_SHELL=/usr/sbin/nologin
PRIVATE_KEY=/var/lib/paperclip-host-ops/keys/response-signing.pem
PUBLIC_KEY=/var/lib/paperclip-host-ops/keys/response-signing.pub.pem
UNIT=paperclip-host-ops-broker.service
JOURNAL_ROOT=/var/lib/paperclip-host-ops-installer
PREPARE_LABEL="paperclip-host-ops-broker"
# Directory targets and the root owner as variables (not flags or
# environment): the operator CLI always uses these production values. They
# are variables so the offline test gate can redirect them at temp roots and
# exercise the real mkdir/chown/chmod/stat/journal code without touching the
# host or requiring uid 0.
DIR_LIBEXEC=/usr/local/libexec/paperclip-host-ops
DIR_DOC=/usr/local/share/doc/paperclip-host-ops
DIR_ETC=/etc/paperclip-host-ops
DIR_STATE=/var/lib/paperclip-host-ops
DIR_KEYS=/var/lib/paperclip-host-ops/keys
DIR_LOG=/var/log/paperclip-host-ops
SPOOL_ROOT=/var/spool/paperclip-host-ops
OWNER_UID=0
OWNER_GID=0

# Operator-asserted adoption list (case_cdaf2970 review). Empty with
# EXPECT_SUPPLIED=false means the operator asserted nothing; a pre-existing
# group carrying members then refuses. Set by argument parsing, consumed by
# classification and the journal.
EXPECT_REQUESTS_MEMBERS=""
EXPECT_SUPPLIED=false

# Set by classification; consumed by main and the journal.
ADOPT_REQUESTS=false
REQUESTS_GID=""
REQUESTS_MEMBERS=""

# Set once the first host change starts. From then on a refusal prints rollback
# and marks JOURNAL_FILE aborted; validators run in subshells with it cleared so
# their refusal is reported once, by the caller's partial_rollback.
HOST_MUTATED=false
JOURNAL_FILE=""

# Set by the spool planner; consumed by main and the journal.
CREATE_DIRS=()
ADOPT_DIRS=()

ensure_validator() {
  # The created identity is validated through the shipped installer contract,
  # not a re-implemented check. Tests pre-define this function to stub it;
  # production always loads it from the reviewed sibling file.
  if ! declare -F validate_service_identity >/dev/null; then
    [[ -f "$SCRIPT_DIR/install-lib.sh" ]] || refuse "install-lib.sh is missing; run from a clean checkout of the reviewed commit"
    # shellcheck source=host-ops/install-lib.sh
    source "$SCRIPT_DIR/install-lib.sh"
  fi
  declare -F validate_service_identity >/dev/null || refuse "service-identity validator is unavailable"
  # The installer binds its membership check to the preparation journal via
  # PREPARE_JOURNAL_ROOT; preparation writes to JOURNAL_ROOT. In production
  # both default to the same path, but bind them explicitly so the final
  # installer-contract gate below reads the journal this run just wrote
  # (tests redirect JOURNAL_ROOT at temp roots).
  PREPARE_JOURNAL_ROOT="$JOURNAL_ROOT"
}

# Preparation-time identity check (case_4a4d7340 follow-up). The shared installer
# validator accepts only a journal in the "prepared" phase, and at the
# post-useradd checkpoint the journal is still "started": calling it there
# would refuse a correct member-preserving run. This check
# mirrors the installer contract but takes the expected membership from the
# in-memory classification (REQUESTS_MEMBERS set by
# classify_first_install_identity just before the first mutation): the current
# group must contain the reader plus exactly the pre-existing members, no
# additions and no evictions. useradd -G only appends, so any other result
# means state changed under us: refuse. The full journal-bound installer
# contract runs again after the journal is written.
validate_created_identity() {
  local reader_group requests_group reader_passwd
  local reader_group_name reader_gid reader_group_members
  local requests_group_name requests_gid requests_group_members
  local reader_name reader_uid reader_primary_gid reader_home reader_shell
  local item
  reader_group=$(getent group "$READER_GROUP") || refuse "paperclip-host-reader group is unreadable after preparation"
  IFS=: read -r reader_group_name _ reader_gid reader_group_members <<< "$reader_group"
  [[ "$reader_group_name" == "$READER_GROUP" && "$reader_gid" =~ ^[0-9]+$ && $reader_gid -gt 0 && $reader_gid -lt 1000 ]] || \
    refuse "paperclip-host-reader must be a system group"
  [[ -z "$reader_group_members" ]] || refuse "paperclip-host-reader has unintended explicit members"
  reader_passwd=$(getent passwd "$READER_USER") || refuse "paperclip-host-reader account is unreadable after preparation"
  IFS=: read -r reader_name _ reader_uid reader_primary_gid _ reader_home reader_shell <<< "$reader_passwd"
  [[ "$reader_name" == "$READER_USER" && "$reader_uid" =~ ^[0-9]+$ && $reader_uid -gt 0 && $reader_uid -lt 1000 ]] || \
    refuse "paperclip-host-reader must be a system account"
  requests_group=$(getent group "$REQUESTS_GROUP") || refuse "paperclip-host-requests group is unreadable after preparation"
  IFS=: read -r requests_group_name _ requests_gid requests_group_members <<< "$requests_group"
  [[ "$requests_group_name" == "$REQUESTS_GROUP" && "$requests_gid" =~ ^[0-9]+$ && $requests_gid -gt 0 && $requests_gid -lt 1000 ]] || \
    refuse "paperclip-host-requests must be a system group"
  [[ "$requests_gid" == "$REQUESTS_GID" ]] || \
    refuse "paperclip-host-requests gid changed during preparation; separate review required"
  # Primary-group holders bypass the member list yet gain the same spool and
  # config access. The shipped installer refuses them; preparation must too.
  # The requests gid is checked before mutation (classification) and again
  # here; the reader gid can only be checked after groupadd, since a new gid
  # may be claimed by an account left behind with no group entry.
  check_no_primary_holders "$requests_gid" paperclip-host-requests
  check_no_primary_holders "$reader_gid" "$READER_GROUP"
  member_in_csv "$READER_USER" "$requests_group_members" || \
    refuse "paperclip-host-requests does not contain the broker reader after preparation; separate review required"
  local -a current=() prior=()
  IFS=',' read -r -a current <<< "$requests_group_members"
  for item in "${current[@]}"; do
    valid_member_name "$item" || \
      refuse "paperclip-host-requests holds an unreadable member entry; separate review required"
    [[ "$item" == "$READER_USER" ]] || member_in_csv "$item" "$REQUESTS_MEMBERS" || \
      refuse "paperclip-host-requests gained member $item during preparation; separate review required"
  done
  if [[ -n "$REQUESTS_MEMBERS" ]]; then
    IFS=',' read -r -a prior <<< "$REQUESTS_MEMBERS"
    for item in "${prior[@]}"; do
      member_in_csv "$item" "$requests_group_members" || \
        refuse "paperclip-host-requests lost pre-existing member $item during preparation; separate review required"
    done
  else
    [[ "$requests_group_members" == "$READER_USER" ]] || \
      refuse "paperclip-host-requests holds unexpected members after preparation; separate review required"
  fi
  [[ "$reader_primary_gid" == "$reader_gid" ]] || refuse "paperclip-host-reader primary group is incompatible"
  [[ "$reader_home" == "$READER_HOME" ]] || refuse "paperclip-host-reader home is incompatible"
  [[ "$reader_shell" == "$READER_SHELL" ]] || refuse "paperclip-host-reader shell is incompatible"
}

check_service_absent() {
  local show load active files
  show=$(systemctl show "$UNIT" --all --no-pager --property=LoadState,ActiveState,UnitFileState 2>/dev/null) || \
    refuse "service state is unreadable; inspect $UNIT before preparing"
  load=$(grep '^LoadState=' <<<"$show" | cut -d= -f2)
  active=$(grep '^ActiveState=' <<<"$show" | cut -d= -f2)
  files=$(grep '^UnitFileState=' <<<"$show" | cut -d= -f2)
  [[ "$load" == not-found && "$active" == inactive && -z "$files" ]] || \
    refuse "broker service already exists ($load/$active); fresh-host preparation does not apply: use the upgrade installer or a separate review"
}

# Fresh-host identity classification. The reader group and account must be
# wholly absent. The requests group is the one documented pre-existing spool
# artifact: adopt it when it is a system group, WITH its members preserved
# exactly as observed AND asserted by the operator. Membership is never edited
# here (useradd -G only appends the reader: no eviction, no widening); the
# exact member list is pinned in the journal and the installer refuses any
# later addition or removal. The operator records the authorized list in the
# install packet and passes it as --expect-requests-members; without that
# flag a group carrying members refuses, so an unexpected member can never be
# blessed by running preparation.
classify_first_install_identity() {
  local reader_group requests_group reader_passwd reader_id
  require_local_account_sources
  ADOPT_REQUESTS=false
  REQUESTS_GID=""
  REQUESTS_MEMBERS=""
  if getent group "$READER_GROUP" >/dev/null 2>&1; then
    refuse "reader group already exists; fresh-host preparation does not apply: use the upgrade installer or a separate review"
  fi
  if getent passwd "$READER_USER" >/dev/null 2>&1 || id "$READER_USER" >/dev/null 2>&1; then
    refuse "reader account already exists without its group; partial identity requires separate review, never repair here"
  fi
  if requests_group=$(getent group "$REQUESTS_GROUP" 2>/dev/null); then
    local name _ gid members
    IFS=: read -r name _ gid members <<< "$requests_group"
    [[ "$name" == "$REQUESTS_GROUP" && "$gid" =~ ^[0-9]+$ && $gid -gt 0 && $gid -lt 1000 ]] || \
      refuse "pre-existing requests group is not a system group; separate review required"
    [[ "$members" =~ ^[A-Za-z0-9_.-]*(,[A-Za-z0-9_.-]+)*$ ]] || \
      refuse "pre-existing requests group holds an unreadable member entry; separate review required"
    if [[ -n "$members" ]]; then
      [[ "$EXPECT_SUPPLIED" == true ]] || \
        refuse "pre-existing requests group has members ($members); pass --expect-requests-members with the exact authorized list recorded in the install packet"
      csv_set_equals "$members" "$EXPECT_REQUESTS_MEMBERS" || \
        refuse "pre-existing requests group members ($members) do not match the expected list ($EXPECT_REQUESTS_MEMBERS); separate review required"
    else
      if [[ "$EXPECT_SUPPLIED" == true && -n "$EXPECT_REQUESTS_MEMBERS" ]]; then
        refuse "pre-existing requests group is empty but --expect-requests-members names ($EXPECT_REQUESTS_MEMBERS); separate review required"
      fi
    fi
    # Primary-group holders are invisible to the member list yet gain the same
    # spool access. Refuse before any mutation when one already exists.
    check_no_primary_holders "$gid" paperclip-host-requests
    REQUESTS_MEMBERS="$members"
    ADOPT_REQUESTS=true
    REQUESTS_GID="$gid"
  else
    if [[ "$EXPECT_SUPPLIED" == true && -n "$EXPECT_REQUESTS_MEMBERS" ]]; then
      refuse "--expect-requests-members names ($EXPECT_REQUESTS_MEMBERS) but no pre-existing requests group exists; separate review required"
    fi
  fi
  # A reader id without a passwd entry is the same partial state by another name.
  if id "$READER_USER" >/dev/null 2>&1; then
    refuse "reader identity is partially configured; separate review required"
  fi
}

check_keys_absent() {
  local path
  for path in "$PRIVATE_KEY" "$PUBLIC_KEY"; do
    if [[ -e "$path" || -L "$path" ]]; then
      refuse "signing key already exists at $path; preparation never repairs key metadata: use the upgrade installer or a separate review"
    fi
  done
}

check_path_absent() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    refuse "protected path already exists: $path; fresh-host preparation does not apply: use the upgrade installer or a separate review"
  fi
}

# Spool planner (no mutation). A pre-existing spool tree is adopted only when
# every existing path is a directory containing no files, symlinks or special
# nodes: adopting (chown/chmod) a tree holding another principal's files
# would silently transfer ownership of data this broker must never touch.
collect_spool_plan() {
  local root="$1"
  CREATE_DIRS=()
  ADOPT_DIRS=()
  local -a expected=(
    "$root"
    "$root/requests"
    "$root/previews"
    "$root/responses"
    "$root/.staging"
    "$root/processing"
    "$root/quarantine"
  )
  local dir
  for dir in "${expected[@]}"; do
    if [[ -L "$dir" ]]; then
      refuse "spool path is a symlink: $dir; separate review required"
    elif [[ -e "$dir" ]]; then
      [[ -d "$dir" ]] || refuse "spool path is not a directory: $dir; separate review required"
    else
      CREATE_DIRS+=("$dir")
    fi
  done
  if [[ -e "$root" || -L "$root" ]]; then
    local -a stack=("$root")
    local current entry
    shopt -s dotglob nullglob
    while ((${#stack[@]})); do
      current="${stack[-1]}"
      unset 'stack[-1]'
      for entry in "$current"/*; do
        if [[ -L "$entry" ]]; then
          refuse "spool tree holds a symlink: $entry; adopting it could redirect ownership: separate review required"
        elif [[ -d "$entry" ]]; then
          stack+=("$entry")
        else
          refuse "spool tree holds existing data: $entry; adopting it would transfer ownership of another principal's files: preserve and escalate on this chain"
        fi
      done
    done
    shopt -u dotglob nullglob
    # Every existing expected dir is an empty directory: adopt (fix modes).
    for dir in "${expected[@]}"; do
      [[ -e "$dir" ]] && ADOPT_DIRS+=("$dir")
    done
    # An unexpected pre-existing directory inside the spool root is data.
    local -a known=()
    local known_entry
    for dir in "${expected[@]}"; do known+=("$dir"); done
    shopt -s dotglob nullglob
    for entry in "$root"/*; do
      local seen=false
      for known_entry in "${known[@]}"; do [[ "$entry" == "$known_entry" ]] && seen=true; done
      [[ "$seen" == true ]] || refuse "spool tree holds an unexpected directory: $entry; separate review required"
    done
    shopt -u dotglob nullglob
  fi
}

stat_identity() {
  stat -c '%u %g %a' "$1"
}

# Post-mutation directory helpers must RETURN (not exit) on failure so the
# caller's `|| partial_rollback` runs and prints ROLLBACK lines. A bare
# `refuse` (exit 2) would terminate the script before rollback. Validation
# helpers (validate_created_identity, validate_service_identity) are read-only
# and are instead run in a subshell at their call sites.
refuse_return() {
  printf 'REFUSED: %s\n' "$1" >&2
  return 2
}

make_dir() {
  local path="$1" mode="$2" uid="$3" gid="$4" observed
  mkdir "$path" || { refuse_return "could not create directory $path"; return 2; }
  CREATED_DIRS+=("$path")
  chown "$uid:$gid" "$path" || { refuse_return "could not set ownership on $path"; return 2; }
  chmod "$mode" "$path" || { refuse_return "could not set mode on $path"; return 2; }
  observed=$(stat_identity "$path") || { refuse_return "could not verify $path"; return 2; }
  [[ "$observed" == "$uid $gid $mode" ]] || { refuse_return "directory verification failed for $path (got $observed)"; return 2; }
}

adopt_dir() {
  local path="$1" mode="$2" uid="$3" gid="$4" observed prior
  prior=$(stat_identity "$path") || { refuse_return "could not read prior metadata for $path"; return 2; }
  ADOPT_PRIOR+=("$path:$prior")
  chown "$uid:$gid" "$path" || { refuse_return "could not set ownership on $path"; return 2; }
  chmod "$mode" "$path" || { refuse_return "could not set mode on $path"; return 2; }
  observed=$(stat_identity "$path") || { refuse_return "could not verify $path"; return 2; }
  [[ "$observed" == "$uid $gid $mode" ]] || { refuse_return "directory verification failed for $path (got $observed)"; return 2; }
}

ADOPT_PRIOR=()
CREATED_GROUPS=()
CREATED_USER=false
CREATED_DIRS=()

partial_rollback() {
  printf 'PARTIAL: %s\n' "$1" >&2
  report_partial_rollback
  exit 2
}

report_partial_rollback() {
  print_rollback
  [[ -n "$JOURNAL_FILE" && -f "$JOURNAL_FILE" ]] || return 0
  if write_journal aborted "$JOURNAL_FILE"; then
    printf 'JOURNAL MARKED ABORTED: %s (the installer refuses it; keep it as evidence)\n' "$JOURNAL_FILE"
  else
    printf 'JOURNAL NOT MARKED ABORTED: %s; move it aside before any retry\n' "$JOURNAL_FILE" >&2
  fi
}

# Writes the journal atomically (temp file then rename) so a reader never sees a
# half-written phase. Unknown ids print as null: only a "prepared" journal is
# parsed by the installer, and every other phase is refused on its phase alone.
write_journal() {
  local phase="$1" target="$2" tmp="$2.tmp" d entry first=true
  {
    printf '{\n  "version": 1,\n  "phase": "%s",\n  "adoptedRequestsGroup": %s,\n' "$phase" "$ADOPT_REQUESTS"
    printf '  "readerUid": %s,\n  "readerGid": %s,\n  "requestsGid": %s,\n' \
      "${READER_UID:-null}" "${READER_GID:-null}" "${REQUESTS_GID:-null}"
    printf '  "preexistingRequestsMembers": "%s",\n  "expectedRequestsMembers": "%s",\n' \
      "$REQUESTS_MEMBERS" "$EXPECT_REQUESTS_MEMBERS"
    printf '  "createdDirs": ['
    for d in "${CREATED_DIRS[@]}"; do
      [[ "$first" == true ]] && first=false || printf ', '
      printf '"%s"' "$d"
    done
    printf '],\n  "adoptedDirs": ['
    first=true
    for entry in "${ADOPT_PRIOR[@]}"; do
      [[ "$first" == true ]] && first=false || printf ', '
      printf '{"path": "%s", "prior": "%s"}' "${entry%%:*}" "${entry#*:}"
    done
    printf ']\n}\n'
  } > "$tmp" || return 1
  chmod 0600 "$tmp" && mv -f "$tmp" "$target"
}

print_rollback() {
  printf 'ROLLBACK (valid only before the operator key step that follows preparation):\n'
  if [[ "$CREATED_USER" == true ]]; then
    printf '  userdel %s\n' "$READER_USER"
  fi
  local group
  for ((i = ${#CREATED_GROUPS[@]} - 1; i >= 0; i--)); do
    printf '  groupdel %s\n' "${CREATED_GROUPS[$i]}"
  done
  local i
  for ((i = ${#CREATED_DIRS[@]} - 1; i >= 0; i--)); do
    printf '  rmdir %s\n' "${CREATED_DIRS[$i]}"
  done
  local entry path prior prior_uid prior_gid prior_mode
  for ((i = ${#ADOPT_PRIOR[@]} - 1; i >= 0; i--)); do
    entry="${ADOPT_PRIOR[$i]}"
    path="${entry%%:*}"
    prior="${entry#*:}"
    read -r prior_uid prior_gid prior_mode <<< "$prior"
    printf '  chown %s:%s %s && chmod %s %s\n' "$prior_uid" "$prior_gid" "$path" "$prior_mode" "$path"
  done
  printf 'rmdir fails safe on non-empty directories; a failure there means state changed after preparation: stop and escalate.\n'
}

main() {
  EXPECT_REQUESTS_MEMBERS=""
  EXPECT_SUPPLIED=false
  HOST_MUTATED=false
  JOURNAL_FILE=""
  CREATED_GROUPS=()
  CREATED_USER=false
  CREATED_DIRS=()
  ADOPT_PRIOR=()
  while [[ $# -gt 0 ]]; do
    case "${1:-}" in
      --help|-h) usage; return 0;;
      --expect-requests-members)
        [[ $# -ge 2 ]] || { usage >&2; refuse "--expect-requests-members requires a value"; }
        EXPECT_REQUESTS_MEMBERS="${2:-}"
        EXPECT_SUPPLIED=true
        shift 2;;
      --expect-requests-members=*)
        EXPECT_REQUESTS_MEMBERS="${1#--expect-requests-members=}"
        EXPECT_SUPPLIED=true
        shift;;
      *) usage >&2; refuse "unknown argument: $1";;
    esac
  done
  if [[ "$EXPECT_SUPPLIED" == true ]]; then
    [[ "$EXPECT_REQUESTS_MEMBERS" =~ ^[A-Za-z0-9_.-]*(,[A-Za-z0-9_.-]+)*$ ]] || \
      refuse "--expect-requests-members holds an unreadable member entry"
    if [[ -n "$EXPECT_REQUESTS_MEMBERS" ]]; then
      local exp_item
      local -a exp_arr=()
      IFS=',' read -r -a exp_arr <<< "$EXPECT_REQUESTS_MEMBERS"
      for exp_item in "${exp_arr[@]}"; do
        [[ -n "$exp_item" ]] || refuse "--expect-requests-members holds an empty entry"
        [[ "$exp_item" != "$READER_USER" ]] || \
          refuse "--expect-requests-members must not include $READER_USER (it joins during preparation)"
      done
    fi
  fi

  for command in getent id passwd systemctl groupadd useradd mkdir chown chmod stat; do
    command -v "$command" >/dev/null || refuse "required command missing: $command"
  done
  ensure_validator

  # Full preflight before the first mutation.
  check_service_absent
  classify_first_install_identity
  check_keys_absent
  check_path_absent "$DIR_ETC"
  check_path_absent "$DIR_LIBEXEC"
  check_path_absent "$DIR_DOC"
  check_path_absent "$DIR_STATE"
  check_path_absent "$DIR_LOG"
  collect_spool_plan "$SPOOL_ROOT"

  # Mutation phase, in dependency order. Every step verifies before the next.
  # From the first host change on, failures go through partial_rollback, which
  # prints rollback and marks the journal aborted.
  if [[ -e "$JOURNAL_ROOT" || -L "$JOURNAL_ROOT" ]]; then
    [[ -d "$JOURNAL_ROOT" && ! -L "$JOURNAL_ROOT" ]] || refuse "journal root is not a directory: $JOURNAL_ROOT"
    [[ "$(stat_identity "$JOURNAL_ROOT")" == "$OWNER_UID $OWNER_GID 700" ]] || \
      refuse "journal root has unexpected owner or mode: $JOURNAL_ROOT; separate review required"
  else
    HOST_MUTATED=true
    mkdir "$JOURNAL_ROOT" || partial_rollback "could not create journal root"
    chown "$OWNER_UID:$OWNER_GID" "$JOURNAL_ROOT" || partial_rollback "journal root ownership failed"
    chmod 0700 "$JOURNAL_ROOT" || partial_rollback "journal root mode failed"
  fi

  # The journal opens before the first identity change, so an interrupted run
  # always leaves a non-prepared journal behind for the installer to refuse.
  local token journal_dir
  token=$(od -An -tx1 -N12 /dev/urandom | tr -d ' \n') || refuse "journal token failed"
  [[ "$token" =~ ^[0-9a-f]{24}$ ]] || refuse "journal token is malformed"
  journal_dir="$JOURNAL_ROOT/prepare-$token"
  mkdir "$journal_dir" || refuse "journal directory already exists; inspect before retry"
  JOURNAL_FILE="$journal_dir/prepare.json"
  HOST_MUTATED=true
  chmod 0700 "$journal_dir" || partial_rollback "journal directory mode failed"
  write_journal started "$JOURNAL_FILE" || partial_rollback "journal write failed"

  groupadd -r "$READER_GROUP" || partial_rollback "could not create system group $READER_GROUP"
  CREATED_GROUPS+=("$READER_GROUP")
  if [[ "$ADOPT_REQUESTS" == true ]]; then
    [[ -n "$REQUESTS_GID" ]] || partial_rollback "adopted requests gid missing"
  else
    groupadd -r "$REQUESTS_GROUP" || partial_rollback "could not create system group $REQUESTS_GROUP"
    CREATED_GROUPS+=("$REQUESTS_GROUP")
    REQUESTS_GID=$(getent group "$REQUESTS_GROUP" | cut -d: -f3) || partial_rollback "new requests group is unreadable"
  fi
  READER_GID=$(getent group "$READER_GROUP" | cut -d: -f3) || partial_rollback "new reader group is unreadable"
  READER_UID=""
  useradd -r -g "$READER_GROUP" -G "$REQUESTS_GROUP" -d "$READER_HOME" -s "$READER_SHELL" -M "$READER_USER" || \
    partial_rollback "could not create system account $READER_USER"
  CREATED_USER=true
  passwd -l "$READER_USER" >/dev/null || partial_rollback "could not lock $READER_USER credentials"
  READER_UID=$(getent passwd "$READER_USER" | cut -d: -f3) || partial_rollback "new reader account is unreadable"
  # Preparation-time check against the in-memory classification: the installer's
  # journal-bound validator would refuse a correct member-preserving run here,
  # because the journal is not final yet. The subshell clears HOST_MUTATED so
  # its refusal is reported once, by this partial_rollback.
  ( HOST_MUTATED=false; validate_created_identity ) || partial_rollback "created identity fails the preparation membership contract"

  make_dir "$DIR_LIBEXEC" 755 "$OWNER_UID" "$OWNER_GID" || partial_rollback "control directory failed"
  make_dir "$DIR_DOC" 755 "$OWNER_UID" "$OWNER_GID" || partial_rollback "control directory failed"
  make_dir "$DIR_ETC" 750 "$OWNER_UID" "$READER_GID" || partial_rollback "control directory failed"
  make_dir "$DIR_STATE" 750 "$READER_UID" "$READER_GID" || partial_rollback "runtime directory failed"
  make_dir "$DIR_KEYS" 700 "$READER_UID" "$READER_GID" || partial_rollback "runtime directory failed"
  make_dir "$DIR_LOG" 750 "$READER_UID" "$READER_GID" || partial_rollback "runtime directory failed"

  local spool="$SPOOL_ROOT" dir mode group
  for dir in "${CREATE_DIRS[@]}"; do
    case "$dir" in
      "$spool") mode=750; group="$REQUESTS_GID";;
      "$spool/requests") mode=3730; group="$REQUESTS_GID";;
      "$spool"/previews|"$spool"/responses) mode=2750; group="$REQUESTS_GID";;
      *) mode=700; group="$READER_GID";;
    esac
    make_dir "$dir" "$mode" "$READER_UID" "$group" || partial_rollback "spool directory failed: $dir"
  done
  for dir in "${ADOPT_DIRS[@]}"; do
    case "$dir" in
      "$spool") mode=750; group="$REQUESTS_GID";;
      "$spool/requests") mode=3730; group="$REQUESTS_GID";;
      "$spool"/previews|"$spool"/responses) mode=2750; group="$REQUESTS_GID";;
      *) mode=700; group="$READER_GID";;
    esac
    adopt_dir "$dir" "$mode" "$READER_UID" "$group" || partial_rollback "spool adoption failed: $dir"
  done

  write_journal prepared "$JOURNAL_FILE" || partial_rollback "journal write failed"
  # Final gate through the exact installer contract, run against the journal
  # just marked prepared. The subshell clears HOST_MUTATED so the refusal is
  # reported once; partial_rollback then marks that journal aborted, so the
  # installer never binds to a preparation that failed its own gate.
  ( HOST_MUTATED=false; validate_service_identity ) || partial_rollback "created identity fails the installer validation contract"

  printf 'PREPARED first-install identity and directories for %s\n' "$PREPARE_LABEL"
  printf 'PREPARE JOURNAL: %s\n' "$journal_dir"
  if [[ "$ADOPT_REQUESTS" == true ]]; then
    printf 'ADOPTED requests group members preserved: %s\n' "${REQUESTS_MEMBERS:-(none)}"
  fi
  print_rollback
  printf 'NEXT: create the signing key in a controlled host session, record its SHA-256 independently, then run install.sh.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    usage
    exit 0
  fi
  [[ $EUID -eq 0 ]] || refuse "preparation must run as root"
  main "$@"
fi
