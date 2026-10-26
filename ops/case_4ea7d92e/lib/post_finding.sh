#!/usr/bin/env bash
# ===========================================================================
# post_finding.sh — shared "post a card only on a finding" helper (case_4ea7d92e).
#
# Sourced, not executed, by the three host-cron wrappers in this directory.
# Runs unattended under systemd --user with NO agent-run context: no
# $PAPERCLIP_API_KEY (that is a run-scoped JWT that dies with the heartbeat
# that minted it). Auth instead comes from a durable AGENT API key -- the
# credential `POST /api/agents/{id}/keys` returns -- supplied via
# EnvironmentFile as PAPERCLIP_AGENT_API_KEY. It is NOT a board API key and
# NOT an instance-admin credential; the OpenAPI security schemes name those
# separately (BoardApiKeyAuth / actor "board" + instanceAdmin), and none of
# the three routes below need either. See ../INSTALL.md §1.
#
# CONTRACT: exit 0 = clean, never call the API. Only call post_finding() when
# there is an actual finding (or an indeterminate/error result, which is NOT
# clean and must still be surfaced -- see each wrapper's exit-2 handling).
#
# DELIVERY IS NEVER ASSUMED: post_finding() returns non-zero when the board did
# not accept the finding (transport error, or any non-2xx). Under the wrappers'
# `set -Eeuo pipefail` that aborts the wrapper, so an undelivered finding shows
# up as a failed unit in `systemctl --user --failed` and in the journal rather
# than as a clean tick. A silent 403 is the one outcome this must never have.
#
# COALESCING: before creating a new card, this looks for an already-open card
# with the same dedupe tag in its title and comments onto that instead of
# spawning a duplicate on every timer tick while the finding persists.
#
# NON-DISCLOSURE: neither the key nor the card body is passed on curl's argv.
# Anything in argv is world-readable via /proc/<pid>/cmdline for the life of
# the process, and these run unattended on a shared host. The Authorization
# header goes through a `--config <(...)` pipe (bash `printf` is a builtin, so
# the key never becomes a forked process's argv either) and the request body
# goes through `--data-binary @-` on stdin. Neither ever touches argv, a file
# on disk, or the unit's journal. Pinned by ../../test_case_d376bf4e_host_checks.sh.
# ===========================================================================
set -Eeuo pipefail

# shellcheck source=lib/paperclip_api_allowlist.sh
_PF_ALLOWLIST_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../../lib/paperclip_api_allowlist.sh"
_PF_ALLOWLIST_LIB_OK=""
if [[ -r "$_PF_ALLOWLIST_LIB" ]]; then source "$_PF_ALLOWLIST_LIB"; _PF_ALLOWLIST_LIB_OK=1; fi

: "${PAPERCLIP_API_URL:?PAPERCLIP_API_URL must be set (EnvironmentFile)}"
: "${PAPERCLIP_AGENT_API_KEY:?PAPERCLIP_AGENT_API_KEY must be set (EnvironmentFile) -- see ops/case_4ea7d92e/INSTALL.md}"
: "${PAPERCLIP_COMPANY_ID:?PAPERCLIP_COMPANY_ID must be set (EnvironmentFile)}"
: "${PAPERCLIP_ASSIGNEE_AGENT_ID:?PAPERCLIP_ASSIGNEE_AGENT_ID must be set (EnvironmentFile) -- the reporting agent own id}"

_pf_api() {
  # method path [json-body]
  # Prints the response body on stdout. Returns 0 ONLY on a 2xx; a transport
  # failure or any >=400 status returns non-zero with a one-line reason on
  # stderr. `curl -sS` alone exits 0 on a 403 or a 500, so without this the
  # helper would announce "created new card" against an API that recorded
  # nothing (case_8928d14e B3) -- which would also make INSTALL.md §1's
  # least-privilege experiment and §2's key rotation unfalsifiable.
  # The status is read from -w, never from the body: only argv-free inputs.
  # Bounded transport (case_e107fefc): a hung board endpoint must fail the post
  # (non-zero, "transport failure"), never stall the unattended wrapper nor
  # read as a posted finding.
  local method="$1" path="$2" body="${3:-}"
  # BASE host validation: the agent key is sent ONLY to this base. Refuse a
  # base that is not an http(s) URL with a non-empty bare host: no userinfo
  # (`user@host` misdirects the credential), no whitespace or control
  # characters, no query/fragment, no braces (curl expands `{a,b}` globs,
  # so one base is one host), and an authority that is a bare host[:port]
  # or a bracketed [IPv6] literal with no percent sign (parity with the
  # Python gate, which refuses %-encoded controls urllib would decode).
  # A well-formed hostile host still passes; blocking that
  # needs an operator-owned host allowlist, not a per-script guess.
  local _base="${PAPERCLIP_API_URL%/}" _base_host
  case "$_base" in http://*|https://*) ;; *) echo "post_finding: refusing: PAPERCLIP_API_URL is not an http(s) URL" >&2; return 1;; esac
  case "$_base" in *@*|*[[:space:]]*|*[[:cntrl:]]*|*\?*|*#*|*\{*|*\}*) echo "post_finding: refusing: PAPERCLIP_API_URL carries userinfo, whitespace/control characters, a query/fragment, or braces" >&2; return 1;; esac
  _base_host="${_base#http://}"; _base_host="${_base_host#https://}"; _base_host="${_base_host%%/*}"
  [[ -n "$_base_host" ]] || { echo "post_finding: refusing: PAPERCLIP_API_URL has an empty host" >&2; return 1; }
  case "$_base_host" in :*|*,*|*%*) echo "post_finding: refusing: PAPERCLIP_API_URL authority is not one bare host" >&2; return 1;; esac
  if [[ "$_base_host" == *\[* || "$_base_host" == *\]* ]]; then
    [[ "$_base_host" =~ ^\[[0-9a-fA-F:.]+\](:[0-9]+)?$ ]] || { echo "post_finding: refusing: PAPERCLIP_API_URL authority is not one bare host" >&2; return 1; }
  elif [[ "$_base_host" == *:* ]]; then
    if [[ "${_base_host%:*}" == *:* || ! "${_base_host##*:}" =~ ^[0-9]+$ ]]; then
      echo "post_finding: refusing: PAPERCLIP_API_URL authority is not one bare host" >&2; return 1
    fi
  fi
  if [[ -z "${_PF_ALLOWLIST_LIB_OK:-}" ]]; then
    echo "post_finding: refusing: API allowlist lib missing" >&2; return 1
  fi
  paperclip_allowlist_check "$_base" "post_finding" || return 1
  local args=(-sS --connect-timeout 10 --max-time 30 -X "$method" -H "Content-Type: application/json" -w $'\n%{http_code}')
  [[ -n "$body" ]] && args+=(--data-binary @-)
  local raw rc=0
  raw="$(printf '%s' "$body" | curl "${args[@]}" \
    --config <(printf 'header = "Authorization: Bearer %s"\n' "$PAPERCLIP_AGENT_API_KEY") \
    "${PAPERCLIP_API_URL%/}${path}")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "post_finding: transport failure on ${method} ${path%%\?*} (curl exit ${rc})" >&2
    return 1
  fi
  local code="${raw##*$'\n'}"
  printf '%s' "${raw%$'\n'*}"
  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    # Path only, never the query string or the response body: the first can
    # carry ids and the second can echo request content back into the journal.
    echo "post_finding: API rejected ${method} ${path%%\?*} with HTTP ${code}" >&2
    return 1
  fi
}

# find_open_card_by_tag TAG -> prints issue id on stdout, or nothing.
# Returns non-zero if the lookup itself failed -- an unreadable board is NOT
# "no open card found"; treating it as such would spawn a duplicate card per
# tick, or (with the create equally blind) report success having posted nothing.
find_open_card_by_tag() {
  local tag="$1" resp rc=0
  resp="$(_pf_api GET "/api/companies/${PAPERCLIP_COMPANY_ID}/issues?assigneeAgentId=${PAPERCLIP_ASSIGNEE_AGENT_ID}&status=todo,in_progress,in_review,blocked")" || rc=$?
  [[ $rc -eq 0 ]] || return 1
  printf '%s' "$resp" | python3 -c '
import json, sys
tag = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data if isinstance(data, list) else (data.get("issues") or data.get("data") or [])
for it in items:
    if tag in (it.get("title") or ""):
        print(it.get("id"))
        break
' "$tag" || true
}

# post_finding TAG TITLE BODY_FILE
# TAG: short stable dedupe token embedded in the card title, e.g. "[case_d376bf4e-sweeper]"
post_finding() {
  local tag="$1" title="$2" body_file="$3"
  local body_json
  body_json="$(python3 -c "import json,sys; print(json.dumps(open(sys.argv[1]).read()))" "$body_file")"

  local existing rc=0
  existing="$(find_open_card_by_tag "$tag")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "post_finding: could not read open cards (tag ${tag}); NOT posting blind -- finding NOT recorded" >&2
    return 1
  fi

  if [[ -n "$existing" ]]; then
    if ! _pf_api POST "/api/issues/${existing}/comments" "{\"body\": ${body_json}}" >/dev/null; then
      echo "post_finding: FAILED to comment on ${existing} (tag ${tag}) -- finding NOT recorded" >&2
      return 1
    fi
    echo "post_finding: commented on existing open card ${existing} (tag ${tag})" >&2
  else
    local full_title
    full_title="$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "${tag} ${title}")"
    if ! _pf_api POST "/api/companies/${PAPERCLIP_COMPANY_ID}/issues" \
      "{\"title\": ${full_title}, \"description\": ${body_json}, \"priority\": \"high\", \"assigneeAgentId\": \"${PAPERCLIP_ASSIGNEE_AGENT_ID}\"}" >/dev/null; then
      echo "post_finding: FAILED to create card (tag ${tag}) -- finding NOT recorded" >&2
      return 1
    fi
    echo "post_finding: created new card (tag ${tag})" >&2
  fi
}
