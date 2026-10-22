#!/usr/bin/env bash
# Shared Paperclip API host allowlist (warn-first shadow) for shell senders.
#
# Source this file, then call at the same point as the structural gate:
#   paperclip_allowlist_check "$BASE" "sender_name" || exit 65
# Returns 0 when the send may proceed (allowlisted, bridge, hatch, or shadow
# warning in warn mode), 1 when it must not (fail closed, zero requests).
# Also runs standalone as a dry-run: ./paperclip_api_allowlist.sh --check BASE.
#
# Semantics mirror lib/paperclip_api_allowlist.py exactly: operator-owned
# host file (default /etc/paperclip/allowed-api-hosts, one https origin per
# line, `#` comments), exact-origin match after normalization, marker-gated
# run bridge, test-only hatch, warn-by-default shadow. See the Python module
# for the full contract; this header repeats the check order only:
# bridge marker + http://127.0.0.1:<port>, then hatch + test-only host, then
# the host file, then warn-and-send vs enforce.
#
# MODE=warn is an availability rollback, not a security boundary: it shares
# the environment namespace with the base it validates.

# Double-source guard (readonly constants below would error on re-source).
if [[ -n "${__PAPERCLIP_API_ALLOWLIST_SOURCED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
__PAPERCLIP_API_ALLOWLIST_SOURCED=1

PAPERCLIP_ALLOWLIST_DEFAULT_FILE="/etc/paperclip/allowed-api-hosts"
PAPERCLIP_ALLOWLIST_HATCH_VAR="PAPERCLIP_API_ALLOWLIST_TEST"
PAPERCLIP_ALLOWLIST_MODE_VAR="PAPERCLIP_API_ALLOWLIST_MODE"
PAPERCLIP_ALLOWLIST_BRIDGE_VAR="PAPERCLIP_API_BRIDGE_MODE"
PAPERCLIP_ALLOWLIST_BRIDGE_VALUE="queue_v1"
PAPERCLIP_ALLOWLIST_COUNTER_VAR="PAPERCLIP_API_ALLOWLIST_COUNTER_FILE"
: "${PAPERCLIP_ALLOWLIST_SHADOW_COUNT:=0}"

_pca_log() { printf '%s\n' "$1" >&2; }

# Keep printable ASCII, truncate: raw bases/values never hit the log whole.
_pca_sanitize() { printf '%s' "${1:-}" | tr -d '[:cntrl:]' | head -c "${2:-200}"; }

# _pca_parse <base>: sets _PCA_SCHEME/_PCA_HOST/_PCA_PORT/_PCA_PATH/
# _PCA_ORIGIN, or _PCA_REASON and return 1.
_pca_parse() {
  local base="${1:-}" rest authority
  _PCA_SCHEME=""; _PCA_HOST=""; _PCA_PORT=""; _PCA_PATH=""; _PCA_ORIGIN=""
  _PCA_REASON=""
  [[ "$base" == */ ]] && base="${base%/}"
  case "$base" in http://*|https://*) ;; *) _PCA_REASON="not an http(s) URL"; return 1;; esac
  case "$base" in *@*|*[[:space:]]*|*[[:cntrl:]]*|*\?*|*#*|*\{*|*\}*)
    _PCA_REASON="carries userinfo, whitespace/control characters, a query/fragment, or braces"; return 1;;
  esac
  _PCA_SCHEME="${base%%://*}"
  rest="${base#*://}"
  if [[ "$rest" == */* ]]; then
    authority="${rest%%/*}"; _PCA_PATH="/${rest#*/}"
  else
    authority="$rest"; _PCA_PATH=""
  fi
  case "$authority" in *%*) _PCA_REASON="carries encoded controls in the authority"; return 1;; esac
  if [[ -z "$authority" ]]; then _PCA_REASON="has an empty host"; return 1; fi
  case "$authority" in :*|*,*) _PCA_REASON="authority is not one bare host"; return 1;; esac
  if [[ "$authority" == \[* ]]; then
    [[ "$authority" =~ ^\[([0-9a-fA-F:.]+)\](:[0-9]+)?$ ]] \
      || { _PCA_REASON="authority is not one bare host"; return 1; }
    _PCA_HOST="${BASH_REMATCH[1]}"
    _PCA_PORT="${BASH_REMATCH[2]#:}"
  elif [[ "$authority" == *:* ]]; then
    _PCA_HOST="${authority%:*}"; _PCA_PORT="${authority##*:}"
    [[ "$_PCA_HOST" == *:* || ! "$_PCA_PORT" =~ ^[0-9]+$ ]] \
      && { _PCA_REASON="authority is not one bare host"; return 1; }
  else
    _PCA_HOST="$authority"; _PCA_PORT=""
  fi
  [[ -n "$_PCA_HOST" ]] || { _PCA_REASON="has an empty host"; return 1; }
  case "$_PCA_HOST" in *\[*|*\]*|*\{*|*\}*|*,*) _PCA_REASON="authority is not one bare host"; return 1;; esac
  _PCA_HOST="${_PCA_HOST,,}"
  if [[ "$_PCA_SCHEME" == "https" && "$_PCA_PORT" == "443" ]]; then _PCA_PORT=""; fi
  if [[ "$_PCA_SCHEME" == "http" && "$_PCA_PORT" == "80" ]]; then _PCA_PORT=""; fi
  _PCA_ORIGIN="${_PCA_SCHEME}://"
  if [[ "$_PCA_HOST" == *:* ]]; then _PCA_ORIGIN+="[${_PCA_HOST}]"; else _PCA_ORIGIN+="${_PCA_HOST}"; fi
  [[ -n "$_PCA_PORT" ]] && _PCA_ORIGIN+=":${_PCA_PORT}"
  return 0
}

# _pca_is_loopback <host>: localhost, ::1, or 127.0.0.0/8 (octets validated).
_pca_is_loopback() {
  local host="${1:-}" o1 o2 o3 o4
  [[ "$host" == "localhost" || "$host" == "::1" ]] && return 0
  [[ "$host" =~ ^127\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  o1="${BASH_REMATCH[1]}"; o2="${BASH_REMATCH[2]}"; o3="${BASH_REMATCH[3]}"
  # 10# forces decimal: an octet like 08 is invalid, never octal.
  (( 10#$o1 <= 255 && 10#$o2 <= 255 && 10#$o3 <= 255 )) || return 1
  return 0
}

_pca_is_test_only() {
  _pca_is_loopback "${1:-}" && return 0
  [[ "${1:-}" == *.invalid ]] && return 0
  return 1
}

# _pca_load <file>: fills _PCA_ALLOWED (one canonical origin per line) or
# sets _PCA_FILE_ERROR and returns 1.
_pca_load() {
  local file="${1:-}" line lineno=0 trimmed origin
  _PCA_ALLOWED=""; _PCA_FILE_ERROR=""
  [[ -f "$file" && -r "$file" ]] || { _PCA_FILE_ERROR="allowlist file missing or unreadable: $file"; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    # Trim leading/trailing whitespace (space, tab, CR).
    trimmed="${line#"${line%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    if ! _pca_parse "$trimmed"; then
      _PCA_FILE_ERROR="allowlist file $file:$lineno: malformed origin $(_pca_sanitize "$trimmed" 120) ($_PCA_REASON)"
      return 1
    fi
    if [[ "$_PCA_SCHEME" != "https" ]]; then
      _PCA_FILE_ERROR="allowlist file $file:$lineno: malformed origin $(_pca_sanitize "$trimmed" 120) (entries are https-only)"
      return 1
    fi
    if [[ "$_PCA_PATH" != "" && "$_PCA_PATH" != "/" ]]; then
      _PCA_FILE_ERROR="allowlist file $file:$lineno: malformed origin $(_pca_sanitize "$trimmed" 120) (entries are bare origins, no path)"
      return 1
    fi
    if [[ "$_PCA_HOST" == *. ]]; then
      _PCA_FILE_ERROR="allowlist file $file:$lineno: malformed origin $(_pca_sanitize "$trimmed" 120) (trailing-dot host)"
      return 1
    fi
    origin="$_PCA_ORIGIN"
    _PCA_ALLOWED+="${origin}"$'\n'
  done < "$file"
  return 0
}

_pca_bump_counter() {
  PAPERCLIP_ALLOWLIST_SHADOW_COUNT=$((PAPERCLIP_ALLOWLIST_SHADOW_COUNT + 1))
  local counter_file="${PAPERCLIP_API_ALLOWLIST_COUNTER_FILE:-}"
  [[ -z "$counter_file" ]] && return 0
  local current=0 raw
  if raw="$(cat "$counter_file" 2>/dev/null)" && [[ "$raw" =~ ^[0-9]+$ ]]; then
    current="$raw"
  fi
  printf '%d\n' $((current + 1)) > "$counter_file" 2>/dev/null || true
}

# paperclip_allowlist_check <base> <sender> [file]: 0 = may send, 1 = refuse.
paperclip_allowlist_check() {
  local base="${1:-}" sender="${2:-unknown}" file="${3:-$PAPERCLIP_ALLOWLIST_DEFAULT_FILE}"
  local reason mode origin host scheme cpath cport
  if ! _pca_parse "$base"; then
    _pca_log "refusing: allowlist base is not a clean http(s) base ($_PCA_REASON) sender=$sender"
    return 1
  fi
  # Save the candidate: _pca_load reuses _pca_parse and overwrites _PCA_*.
  origin="$_PCA_ORIGIN"; host="$_PCA_HOST"; scheme="$_PCA_SCHEME"; cpath="$_PCA_PATH"; cport="$_PCA_PORT"
  # 1. Run-scoped bridge: explicit marker plus the observed dynamic-port
  # loopback shape. The marker alone widens nothing; anything else falls
  # through.
  if [[ "${PAPERCLIP_API_BRIDGE_MODE:-}" == "$PAPERCLIP_ALLOWLIST_BRIDGE_VALUE" \
      && "$scheme" == "http" && "$host" == "127.0.0.1" && -n "$cport" ]]; then
    return 0
  fi
  # 2. Test hatch: env-only, honored only with a test-only base.
  if [[ "${PAPERCLIP_API_ALLOWLIST_TEST:-}" == "1" ]]; then
    if _pca_is_test_only "$host"; then
      return 0
    fi
    _pca_log "refusing: allowlist test hatch with non-test base base=$(_pca_sanitize "$origin") sender=$sender"
    return 1
  fi
  # 3. Host file: missing/unreadable/malformed refuses in every mode.
  if ! _pca_load "$file"; then
    _pca_log "refusing: $_PCA_FILE_ERROR sender=$sender"
    return 1
  fi
  if [[ "$cpath" != "" && "$cpath" != "/" ]]; then
    reason="path-not-matched"
  elif grep -qxF "$origin" <<<"$_PCA_ALLOWED"; then
    return 0
  else
    reason="no-match"
  fi
  # 4. No match: warn-and-send in shadow, refuse in enforce.
  mode="${PAPERCLIP_API_ALLOWLIST_MODE:-}"
  [[ -z "$mode" ]] && mode="warn"
  if [[ "$mode" == "warn" ]]; then
    _pca_log "WARNING allowlist_shadow base_host=$host sender=$sender base=$origin mode=warn reason=$reason"
    _pca_bump_counter
    return 0
  fi
  if [[ "$mode" == "enforce" ]]; then
    _pca_log "refusing: allowlist base not allowlisted base=$(_pca_sanitize "$origin") mode=enforce sender=$sender"
    return 1
  fi
  _pca_log "refusing: allowlist unknown PAPERCLIP_API_ALLOWLIST_MODE=$(_pca_sanitize "$mode" 40) sender=$sender"
  return 1
}

# Executed directly (not sourced): --check dry-run, never sends.
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  _pca_main() {
    local base="" sender="check" file="$PAPERCLIP_ALLOWLIST_DEFAULT_FILE" entries=0
    [[ "${1:-}" == "--check" ]] || { echo "usage: $0 --check BASE [--sender NAME] [--file PATH] [--mode MODE]" >&2; return 2; }
    shift
    [[ $# -ge 1 ]] || { echo "usage: $0 --check BASE [--sender NAME] [--file PATH] [--mode MODE]" >&2; return 2; }
    base="$1"; shift
    while [[ $# -gt 0 ]]; do
      case "$1" in
        # A dangling flag (no value) must refuse, not spin: an unconditional
        # `shift 2` on one word fails without consuming, looping forever.
        --sender) [[ $# -ge 2 ]] || { echo "missing value: $1" >&2; return 2; }; sender="$2"; shift 2;;
        --file) [[ $# -ge 2 ]] || { echo "missing value: $1" >&2; return 2; }; file="$2"; shift 2;;
        --mode) [[ $# -ge 2 ]] || { echo "missing value: $1" >&2; return 2; }; PAPERCLIP_API_ALLOWLIST_MODE="$2"; shift 2;;
        *) echo "unknown argument: $1" >&2; return 2;;
      esac
    done
    if ! _pca_load "$file"; then
      _pca_log "refusing: $_PCA_FILE_ERROR sender=$sender"
      printf 'REFUSE base=%s file=%s entries=? reason=file\n' "$(_pca_sanitize "$base")" "$file"
      return 1
    fi
    entries="$(grep -c . <<<"$_PCA_ALLOWED")"
    if paperclip_allowlist_check "$base" "$sender" "$file"; then
      printf 'ALLOW base=%s file=%s entries=%d\n' "$(_pca_sanitize "$base")" "$file" "$entries"
      return 0
    fi
    printf 'REFUSE base=%s file=%s entries=%d\n' "$(_pca_sanitize "$base")" "$file" "$entries"
    return 1
  }
  _pca_main "$@"
  exit $?
fi
