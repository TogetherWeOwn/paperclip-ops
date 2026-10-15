# _gatus_push.sh — shared POST helper for the case_658bf6b9 cron push scripts.
#
# Sourced, never executed. Provides one function:
#
#   gatus_push <endpoint-key> <true|false> [error-text]
#
# Requires GATUS_URL and GATUS_EXTERNAL_TOKEN in the environment (both live
# in the root-only 0600 env file the operator installs; see OPERATOR.md).
# Refuses (exit 2) when either is missing — a push that cannot authenticate
# must fail loudly so cron reports it, never silently skip.
#
# NON-DISCLOSURE: the Bearer token travels in a 0600 curl config file, never
# in argv — /proc/<pid>/cmdline is world-readable on the shared host. Same
# pattern as notify_paperclip_issue.sh and lib/post_finding.sh.
#
# UNTRUSTED INPUT: the error text names host state (VM names, journal lines)
# and is truncated to 500 chars before it touches the network. Gatus renders
# it into the status page and forwards it to Akmatori, so a hostile or merely
# enormous string must not become a prompt-injection carrier or an E2BIG
# brick downstream.
MAX_PUSH_ERROR_CHARS=500

gatus_push() {
  local key="$1" success="$2" err="${3:-}"
  [[ -n "${GATUS_URL:-}" ]] \
    || { echo "gatus_push: refusing — GATUS_URL is unset" >&2; return 2; }
  [[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
    || { echo "gatus_push: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; return 2; }
  [[ "$key" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
    || { echo "gatus_push: refusing — bad endpoint key '$key'" >&2; return 2; }
  [[ "$success" == "true" || "$success" == "false" ]] \
    || { echo "gatus_push: refusing — success must be true|false" >&2; return 2; }

  # Truncate by CHARACTERS deliberately: the downstream render is a page, not
  # a wake env var, so the E2BIG byte budget does not apply here — but an
  # unbounded journal dump still must not ride along.
  if (( ${#err} > MAX_PUSH_ERROR_CHARS )); then
    err="${err:0:MAX_PUSH_ERROR_CHARS}…(truncated)"
  fi
  local err_qs=""
  if [[ -n "$err" ]]; then
    err_qs="&error=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$err")"
  fi

  local cfg_dir cfg
  cfg_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/gatus_push.XXXXXXXX")" \
    || { echo "gatus_push: could not create a private temp directory" >&2; return 2; }
  # Expand now: the trap runs after this function returns, when locals are gone.
  # shellcheck disable=SC2064
  trap "rm -rf '$cfg_dir'" RETURN
  cfg="$(umask 077; mktemp "$cfg_dir/curlcfg.XXXXXXXX")" \
    || { echo "gatus_push: could not create a curl config file" >&2; return 2; }
  {
    printf 'url = "%s/api/v1/endpoints/%s/external?success=%s%s"\n' \
      "$GATUS_URL" "$key" "$success" "$err_qs"
    printf 'request = "POST"\n'
    printf 'header = "Authorization: Bearer %s"\n' "$GATUS_EXTERNAL_TOKEN"
    printf 'max-time = "%s"\n' "${GATUS_PUSH_TIMEOUT:-10}"
    printf 'silent\nshow-error\n'
  } > "$cfg"
  chmod 0600 "$cfg"

  local http
  http="$(curl --config "$cfg" -o /dev/null -w '%{http_code}' 2>&1)" \
    || { echo "gatus_push: curl failed: $http" >&2; return 1; }
  case "$http" in
    2*) return 0 ;;
    *)  echo "gatus_push: refused: HTTP $http pushing $key" >&2; return 1 ;;
  esac
}
