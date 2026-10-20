#!/usr/bin/env bash
# ===========================================================================
# op_queue_reject.sh — non-executing reject verb for operator-queue packets
#
# WHAT THIS IS. A receipt-only disposition for a single queue packet that must
# never execute: it binds one packet file to one terminal rejection receipt
# without moving, deleting, relabeling, replaying, or executing anything.
# The queue file is read-only input; its bytes are never modified, and the
# original payload stays exactly where it was. There is no execution path in
# this file — no exec of packet data, no allowlisted script, no eval, no
# `bash -c`, no `$($`, no backticks, no approval handling. Grep for `eval`,
# `bash -c`, `exec `, `timeout`, `chmod`, `chown`, `curl`, and `ssh` over the
# non-comment lines returns nothing.
#
# WHAT THIS IS NOT. Not an executor, not an installer, not a migration, not
# a credential minter, not a network client, not a queue sweeper. It takes
# exactly one packet path and one closed-enum reason. It manufactures no
# approval, reads no allowlist, and changes no staging posture. It never
# reads ~/secure-drop, never mints or grants anything, and never places a
# secret in env or argv (see CUSTODY).
#
# SHADOW. This verb is SHADOW-independent by design and never reads the
# shadow marker: recording a terminal non-execution receipt is not execution,
# so SHADOW neither blocks it nor is changed by it. The approval executor's
# SHADOW semantics are untouched — this file shares no code path with it.
#
# PRIVILEGE. Installed root-owned 755 by the authorized host operator, who also
# runs the single one-shot invocation as root inside the same bounded
# install window (see docs/runbooks/op-queue-reject.md). No new principal,
# no new sudoers grant, no agent write path: the receipt and log inherit
# root ownership from the root invoker. Re-verification is read-only
# (receipt + sha256sum over the preserved packet) through existing reads.
#
# LAYOUT (production paths; every one overridable by OP_REJECT_* env for
# tests. Private host maps stay private and are NOT in this repo):
#   OP_REJECT_QUEUE_DIR      /var/lib/paperclip-operator/queue      (drop dir)
#   OP_REJECT_PROCESSED_DIR  /var/lib/paperclip-operator/processed  (root-only)
#   OP_REJECT_LOG            /var/lib/paperclip-operator/rejection.log (JSONL)
#   OP_REJECT_REQUIRE_ROOT_OWNERSHIP=1  (tests set 0; production stays 1)
#
# PACKET (opaque bytes under QUEUE_DIR; never parsed, never executed):
# The packet is a byte string, not a schema: this verb binds its filename,
# size, mtime, and sha256 without interpreting a single field. A packet the
# executor cannot parse is still rejectable here — that is the point.
# The basename is gated to ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ BEFORE any
# receipt path is built, so no packet name can walk out of the processed
# dir. The receipt lands at processed/<basename>.rejected.json.
#
# REASON (closed enum; the only caller choice):
#   schema-probe | unknown-packet | stale-packet
# Anything else refuses. No free-text note field exists, so no caller
# text can reach the receipt or the log unscrubbed (see CUSTODY).
#
# CLAIM / REPLAY. processed/<base>.claim/ (mkdir, atomic) guards
# concurrency; processed/<base>.rejected.json marks done. A second reject
# with the same packet exits 6 having written nothing. Receipts are never
# overwritten and never retried: the first receipt is the terminal record.
#
# CUSTODY. This file takes no credential on argv, reads no credential file,
# and exports no credential variable. Receipt and log fields are all
# machine-derived (gated basename, enum reason, hashes, sizes, timestamps),
# and the serialized lines are still passed through the same secret-shape
# scrub as the approval executor, so a pasted token in a future free-text
# field could not leak silently.
#
# MACHINE OUTPUT. stdout carries exactly one JSON result object per run
# (outcome, reason_code, packet, packet_sha256, receipt, preserved, ...).
# Tests assert on exit codes and these fields, never on stderr prose.
# stderr is human text and may change.
#
# EXIT CODES (pinned by test_op_queue_reject.sh; executor subset, no 4/7):
#   0  rejected (or verify-only passed)
#   2  usage / malformed invocation (bad flags, missing reason)
#   3  evidence/policy refusal (missing, unreadable, changed, bad name/reason)
#   5  filesystem boundary / ownership refusal (escape, symlink, perms).
#      Symlinks refuse even to inside targets (packet_is_symlink): the
#      receipt name must equal the invoked name.
#   6  replay / concurrent claim (nothing written)
#   1  I/O failure (unwritable processed dir; could not write claim,
#      receipt, or log line)
# There is no 4: SHADOW does not apply to a non-executing verb. There is
# no 7: nothing executes, so nothing can fail post-execution.
#
# TRUST LINEAGE. The queue-dir jail (within_dir_strict) and the root-owned
# file check (check_root_owned_file) below are the same checks as the
# reviewed op_approval_executor.sh (`f91c47e1fbe92e80a6aec9a09209d143b2eedff
# 5e79ce15ebc03d6ef33320766`), adapted to a config-free verb: the ownership
# check applies to the packet file itself, since there is no allowlist,
# approvers file, or runbook script to check.
# ===========================================================================
set -uo pipefail

PROG="${0##*/}"

# ---- configuration (env-overridable; production defaults) -------------------
QUEUE_DIR="${OP_REJECT_QUEUE_DIR:-/var/lib/paperclip-operator/queue}"
PROCESSED_DIR="${OP_REJECT_PROCESSED_DIR:-/var/lib/paperclip-operator/processed}"
REJECT_LOG="${OP_REJECT_LOG:-/var/lib/paperclip-operator/rejection.log}"
REQUIRE_ROOT_OWNERSHIP="${OP_REJECT_REQUIRE_ROOT_OWNERSHIP:-1}"
NOW_OVERRIDE="${OP_REJECT_NOW:-}"
# A trailing separator must not make a symlink look like its target directory.
while [[ "$PROCESSED_DIR" != "/" && "$PROCESSED_DIR" == */ ]]; do
  PROCESSED_DIR="${PROCESSED_DIR%/}"
done

command -v jq >/dev/null || { echo "$PROG: jq required" >&2; exit 2; }

log_human() { printf '%s\n' "$*" >&2; }

emit_result() { # $1 = json object (already built)
  printf '%s\n' "$1"
}

iso_now() {
  if [[ -n "$NOW_OVERRIDE" ]]; then date -u -d "$NOW_OVERRIDE" +%Y-%m-%dT%H:%M:%SZ;
  else date -u +%Y-%m-%dT%H:%M:%SZ; fi
}

# ---- path boundary (same jail as the approval executor) ----------------------
# Resolve symlinks too (no -m): the file must REALLY live inside.
within_dir_strict() { # $1=base $2=candidate
  local base="$1" cand="$2" real br
  [[ -e "$cand" ]] || return 1
  real="$(realpath -- "$cand" 2>/dev/null)" || return 1
  br="$(realpath -- "$base" 2>/dev/null)" || return 1
  [[ "$real" == "$br" || "$real" == "$br"/* ]] || return 1
  printf '%s' "$real"
}

# ---- ownership (same check as the approval executor) --------------------------
# Root-owned, regular file, not writable by group/other.
check_root_owned_file() { # $1=file $2=label -> rc 0 ok
  local f="$1" label="$2" uid mode
  [[ -f "$f" && ! -L "$f" ]] || { log_human "$label: not a regular file: $f"; return 1; }
  if [[ "$REQUIRE_ROOT_OWNERSHIP" == "1" ]]; then
    uid="$(stat -c %u -- "$f")" || return 1
    [[ "$uid" == "0" ]] || { log_human "$label: must be root-owned (uid=$uid): $f"; return 1; }
  fi
  mode="$(stat -c %a -- "$f")" || return 1
  # group/other write bits: mode is octal string like 644; check numerically.
  if (( (8#$mode & 8#022) != 0 )); then
    log_human "$label: must not be group/other-writable (mode=$mode): $f"
    return 1
  fi
  return 0
}

# Replay and claim names are trusted only inside a protected real directory.
check_root_owned_dir() { # $1=directory -> rc 0 ok
  local d="$1" uid mode
  [[ -d "$d" && ! -L "$d" ]] || return 1
  if [[ "$REQUIRE_ROOT_OWNERSHIP" == "1" ]]; then
    uid="$(stat -c %u -- "$d")" || return 1
    [[ "$uid" == "0" ]] || return 1
  fi
  mode="$(stat -c %a -- "$d")" || return 1
  (( (8#$mode & 8#022) == 0 ))
}

# The append target and its direct parent must both be protected. In particular,
# never open a symlink or FIFO as root, even when a receipt could be published.
check_reject_log() {
  local parent
  parent="$(dirname -- "$REJECT_LOG")" || return 1
  check_root_owned_dir "$parent" || return 1
  if [[ -e "$REJECT_LOG" || -L "$REJECT_LOG" ]]; then
    check_root_owned_file "$REJECT_LOG" "rejection log" || return 1
  fi
  return 0
}

# ---- log scrub (same scrub as the approval executor) --------------------------
# No free-text field exists today, but the serialized receipt and log line
# still pass through the scrub so a future one could not leak silently.
scrub_value() { # $1=string -> scrubbed string
  printf '%s' "$1" | sed -E 's/((token|secret|password|api[_-]?key|private[_-]?key)[^A-Za-z0-9]{0,5}[=:][[:space:]]*)[^[:space:],}"]+/\1[redacted]/gI'
}

# ===========================================================================
usage() {
  cat >&2 <<'EOF'
usage:
  op_queue_reject.sh reject --packet <file> --reason <schema-probe|unknown-packet|stale-packet>
  op_queue_reject.sh verify-only --packet <file> --reason <schema-probe|unknown-packet|stale-packet>
env overrides: OP_REJECT_QUEUE_DIR OP_REJECT_PROCESSED_DIR OP_REJECT_LOG
  OP_REJECT_REQUIRE_ROOT_OWNERSHIP OP_REJECT_NOW
EOF
}

MODE=""; PACKET_FILE=""; REASON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    reject|verify-only) MODE="$1"; shift ;;
    --packet) [[ $# -ge 2 ]] || { usage; exit 2; }; PACKET_FILE="$2"; shift 2 ;;
    --reason) [[ $# -ge 2 ]] || { usage; exit 2; }; REASON="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log_human "unknown argument: $1"; usage; exit 2 ;;
  esac
done
[[ -n "$MODE" && -n "$PACKET_FILE" && -n "$REASON" ]] || { usage; exit 2; }

# ---- step 0: packet exists and stays inside the queue dir ----------------------
# Missing evidence is an evidence refusal (3); escaping the dir is a
# boundary refusal (5). The two must not share a code: one means "there is
# nothing to dispose", the other means "someone aimed us outside the jail".
if [[ ! -e "$PACKET_FILE" ]]; then
  emit_result "$(jq -n --arg p "unknown" '{outcome:"refused",reason_code:"packet_unavailable",packet:$p,packet_sha256:"",receipt:"",preserved:false}')"
  log_human "packet file missing"
  exit 3
fi
# A symlink is refused even when its target sits inside the queue: the
# receipt name must equal the invoked name, and a link would force the verb
# to guess which one to record. Name the real file instead.
if [[ -L "$PACKET_FILE" ]]; then
  emit_result "$(jq -n --arg p "none" '{outcome:"refused",reason_code:"packet_is_symlink",packet:$p,packet_sha256:"",receipt:"",preserved:false}')"
  log_human "packet file is a symlink; refusing"
  exit 5
fi
PACKET_REAL="$(within_dir_strict "$QUEUE_DIR" "$PACKET_FILE")" \
  || { emit_result "$(jq -n --arg p "none" '{outcome:"refused",reason_code:"packet_outside_queue",packet:$p,packet_sha256:"",receipt:"",preserved:false}')"; log_human "packet file escapes queue dir or is missing"; exit 5; }

PACKET_BASE="$(basename -- "$PACKET_REAL")"

result() { # $1=outcome $2=reason $3=sha $4=receipt $5=preserved
  jq -n --arg o "$1" --arg r "$2" --arg p "$PACKET_BASE" --arg s "$3" \
        --arg rcpt "$4" --argjson k "$5" \
    '{outcome:$o,reason_code:$r,packet:$p,packet_sha256:$s,receipt:$rcpt,preserved:$k}'
}

# ---- step 1: packet name gates the receipt path ---------------------------------
# The basename becomes a FILENAME under the processed dir (receipt, claim).
# Without a charset gate, a hostile name walks out of the jail — the same
# traversal class the executor's request_id gate closes. Gate it here,
# before any path is built.
if ! [[ "$PACKET_BASE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  emit_result "$(result refused packet_name_rejected "" "" false)"
  log_human "packet basename charset rejected"
  exit 3
fi

# ---- step 2: reason resolves ONLY through the closed enum -----------------------
# No free text reaches the receipt: reasons outside the enum refuse, and the
# enum yields a fixed token, never a caller string.
case "$REASON" in
  schema-probe|unknown-packet|stale-packet) ;;
  *) emit_result "$(result refused reason_not_allowlisted "" "" false)"
     log_human "reason not in the closed enum"
     exit 3 ;;
esac

# ---- step 3: packet is a root-owned, non-writable regular file -------------------
# Same ownership check as the executor's allowlist/approvers/script checks,
# applied to the packet itself: this verb has no config files of its own.
check_root_owned_file "$PACKET_REAL" "packet" \
  || { emit_result "$(result refused packet_ownership "" "" false)"; exit 5; }

# ---- step 4: bind the payload WITHOUT interpreting it ------------------------------
# The packet is opaque bytes: hash, size, and mtime bind it to the receipt.
# No field is parsed, so an executor-unparsable packet is still rejectable.
# Keep full-resolution metadata for change detection even though the receipt's
# human timestamp is second-resolution. Include inode identity to catch swaps.
PACKET_STAMP="$(stat -c '%d:%i:%s:%y' -- "$PACKET_REAL")" \
  || { emit_result "$(result refused packet_unreadable "" "" false)"; exit 3; }
PACKET_SHA="$(sha256sum -- "$PACKET_REAL" | cut -d' ' -f1)" || PACKET_SHA=""
[[ "$PACKET_SHA" =~ ^[a-f0-9]{64}$ ]] \
  || { emit_result "$(result refused packet_unreadable "" "" false)"; log_human "packet unreadable"; exit 3; }
PACKET_SHA="sha256:$PACKET_SHA"
PACKET_BYTES="$(stat -c %s -- "$PACKET_REAL")" \
  || { emit_result "$(result refused packet_unreadable "" "" false)"; exit 3; }
PACKET_EPOCH="$(stat -c %Y -- "$PACKET_REAL")" \
  || { emit_result "$(result refused packet_unreadable "" "" false)"; exit 3; }
PACKET_MTIME="$(date -u -d "@$PACKET_EPOCH" +%Y-%m-%dT%H:%M:%SZ)" \
  || { emit_result "$(result refused packet_unreadable "" "" false)"; exit 3; }

# Revalidate trust before reading again; a replacement symlink or FIFO must not
# be hashed. The claim serializes rejectors, not packet producers. Provisioning
# must still prevent writers changing a packet during the final publish window.
packet_unchanged() {
  local real stamp sha
  real="$(within_dir_strict "$QUEUE_DIR" "$PACKET_REAL")" || return 1
  [[ "$real" == "$PACKET_REAL" ]] || return 1
  check_root_owned_file "$PACKET_REAL" "packet" || return 1
  stamp="$(stat -c '%d:%i:%s:%y' -- "$PACKET_REAL")" || return 1
  [[ "$stamp" == "$PACKET_STAMP" ]] || return 1
  sha="$(sha256sum -- "$PACKET_REAL" | cut -d' ' -f1)" || return 1
  [[ "sha256:$sha" == "$PACKET_SHA" ]] || return 1
  stamp="$(stat -c '%d:%i:%s:%y' -- "$PACKET_REAL")" || return 1
  [[ "$stamp" == "$PACKET_STAMP" ]]
}

# ---- step 5: replay + claim (atomic; nothing writes before this) ----------------------
RECEIPT="$PROCESSED_DIR/$PACKET_BASE.rejected.json"
CLAIM="$PROCESSED_DIR/$PACKET_BASE.claim"
# Reject unsafe existing state before it can masquerade as replay evidence.
if [[ -e "$PROCESSED_DIR" || -L "$PROCESSED_DIR" ]] &&
   ! check_root_owned_dir "$PROCESSED_DIR"; then
  emit_result "$(result refused processed_dir_ownership "$PACKET_SHA" "" true)"
  log_human "processed dir must be a root-owned real directory without group/other write"
  exit 5
fi
if [[ -e "$RECEIPT" || -L "$RECEIPT" ]]; then
  emit_result "$(result refused replay_detected "$PACKET_SHA" "$RECEIPT" true)"
  log_human "replay: receipt already exists; refusing"
  exit 6
fi

check_reject_log \
  || { emit_result "$(result refused log_ownership "$PACKET_SHA" "" true)"; log_human "unsafe rejection log or parent directory"; exit 5; }

if [[ "$MODE" == "verify-only" ]]; then
  emit_result "$(result verified would_reject "$PACKET_SHA" "" true)"
  log_human "verify-only: packet binds cleanly; reason=$REASON; nothing written"
  exit 0
fi

mkdir -- "$PROCESSED_DIR" 2>/dev/null || true
# Creation may have failed or another writer may have created the path. Never
# claim or publish through it until its type, ownership and mode are checked.
if ! check_root_owned_dir "$PROCESSED_DIR"; then
  if [[ ! -e "$PROCESSED_DIR" && ! -L "$PROCESSED_DIR" ]]; then
    emit_result "$(result failed processed_dir_unwritable "$PACKET_SHA" "" true)"
    log_human "processed dir unavailable"
    exit 1
  fi
  emit_result "$(result refused processed_dir_ownership "$PACKET_SHA" "" true)"
  log_human "processed dir must be a root-owned real directory without group/other write"
  exit 5
fi
if ! mkdir -- "$CLAIM" 2>/dev/null; then
  # A failed claim mkdir is a busy claim ONLY if the claim exists; an
  # unwritable processed dir must read as I/O failure, not "retry later".
  if [[ -e "$CLAIM" ]]; then
    emit_result "$(result refused concurrent_claim "$PACKET_SHA" "" true)"
    log_human "concurrent claim in progress; refusing"
    exit 6
  fi
  emit_result "$(result failed processed_dir_unwritable "$PACKET_SHA" "" true)"
  log_human "processed dir unwritable; refusing"
  exit 1
fi

RECEIPT_TMP="$CLAIM/receipt.tmp"
# Clean only our owned temporary, never a packet or a terminal receipt.
# ShellCheck does not trace EXIT trap callbacks.
# shellcheck disable=SC2329
cleanup_claim() {
  rm -f -- "$RECEIPT_TMP" 2>/dev/null || true
  rmdir -- "$CLAIM" 2>/dev/null || true
}
trap cleanup_claim EXIT
# A contender may have completed after our early check but before this claim.
if [[ -e "$RECEIPT" || -L "$RECEIPT" ]]; then
  emit_result "$(result refused replay_detected "$PACKET_SHA" "$RECEIPT" true)"
  log_human "replay: receipt appeared while claiming; refusing"
  exit 6
fi

# Do not turn the pre-claim snapshot of a changed packet into terminal state.
packet_unchanged \
  || { emit_result "$(result refused packet_changed "" "" false)"; log_human "packet changed while claiming"; exit 3; }

# ---- step 6: one receipt + one log line, secrets scrubbed -------------------------------
TS="$(iso_now)" \
  || { emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"; log_human "could not timestamp receipt"; exit 1; }
RAW_RECEIPT="$(jq -cn --arg ts "$TS" --arg pkt "$PACKET_BASE" --arg sha "$PACKET_SHA" \
      --argjson bytes "$PACKET_BYTES" --arg mtime "$PACKET_MTIME" \
      --arg reason "$REASON" \
  '{v:1, verb:"op-queue-reject", ts:$ts, packet:$pkt, packet_sha256:$sha,
    packet_bytes:$bytes, packet_mtime:$mtime, outcome:"rejected",
    reason_code:$reason, payload_preserved:true, executed:false}')" \
  || { emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"; log_human "could not build receipt"; exit 1; }
# Check each transformation; never turn a failed scrub into a terminal record.
SCRUBBED="$(scrub_value "$RAW_RECEIPT")" \
  || { emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"; log_human "could not scrub receipt"; exit 1; }
# Slurp rejects empty/multiple values as well as invalid JSON. Re-serialize to
# exactly one compact JSON object for the line-oriented audit consumer.
SCRUBBED="$(jq -cse --arg pkt "$PACKET_BASE" --arg sha "$PACKET_SHA" --arg reason "$REASON" '
  if length == 1 and (.[0] | type == "object" and .v == 1
    and .verb == "op-queue-reject" and .packet == $pkt and .packet_sha256 == $sha
    and .reason_code == $reason and .outcome == "rejected"
    and .payload_preserved == true and .executed == false)
  then .[0] else error("invalid receipt") end' <<< "$SCRUBBED")" \
  || { emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"; log_human "invalid scrubbed receipt"; exit 1; }
printf '%s\n' "$SCRUBBED" > "$RECEIPT_TMP" \
  || { emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"; log_human "could not write receipt"; exit 1; }
# Recheck the sibling output and the packet after receipt construction, as
# close as possible to publication. Refusals leave no terminal record or log.
check_reject_log \
  || { emit_result "$(result refused log_ownership "$PACKET_SHA" "" true)"; log_human "unsafe rejection log or parent directory"; exit 5; }
packet_unchanged \
  || { emit_result "$(result refused packet_changed "" "" false)"; log_human "packet changed before publication"; exit 3; }
# Hard-link publication is atomic and cannot replace an existing terminal file,
# including one created by a writer outside this claim protocol.
if ! ln -T -- "$RECEIPT_TMP" "$RECEIPT"; then
  if [[ -e "$RECEIPT" || -L "$RECEIPT" ]]; then
    emit_result "$(result refused replay_detected "$PACKET_SHA" "$RECEIPT" true)"
    log_human "replay: receipt already published; refusing"
    exit 6
  fi
  emit_result "$(result failed receipt_unwritten "$PACKET_SHA" "" true)"
  log_human "could not publish receipt"
  exit 1
fi
# The receipt remains terminal even if the audit append fails. Report I/O
# failure with its path, so operators verify the receipt rather than retrying.
if ! { printf '%s\n' "$SCRUBBED" >> "$REJECT_LOG"; } 2>/dev/null; then
  emit_result "$(result failed log_unwritten "$PACKET_SHA" "$RECEIPT" true)"
  log_human "rejection log append failed; receipt remains terminal"
  exit 1
fi

emit_result "$(result rejected ok "$PACKET_SHA" "$RECEIPT" true)"
log_human "rejected $PACKET_BASE ($REASON); receipt $RECEIPT; payload preserved untouched"
exit 0
