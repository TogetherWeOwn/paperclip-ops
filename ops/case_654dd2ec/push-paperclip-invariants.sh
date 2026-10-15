#!/usr/bin/env bash
# push-paperclip-invariants.sh — cron: Paperclip invariant views → Gatus.
#
# For each view in paperclip_invariants.sql, counts violating rows through a
# read-only session and pushes success=false when the view's threshold
# breaches. Follows the ops/case_658bf6b9 push-script contract:
#
# READ-ONLY: the session sets default_transaction_read_only=on via PGOPTIONS,
# and the only statement ever issued is "SELECT count(*) FROM <allowlisted
# view>". View names come from the table below, never from argv or the
# environment — there is no path that turns this script into an arbitrary
# probe runner. Remediation stays on the existing allowlisted verbs.
#
# FAIL CLOSED: a missing psql, a missing env var, a refused connection, or
# an unparseable count pushes success=false for every endpoint and exits 1.
# A probe that cannot read must page, never report healthy.
#
# NON-DISCLOSURE: PAPERCLIP_RO_DB_URL and GATUS_EXTERNAL_TOKEN travel in the
# environment (root-only 0600 env file; see OPERATOR.md). The Bearer token
# reaches curl through a 0600 config file, never argv — same pattern as
# ops/case_658bf6b9/_gatus_push.sh. This script never prints either value; on
# failure it prints the view name and the psql/Gatus error only.
#
# Env in:  PAPERCLIP_RO_DB_URL (read-only role URL, mandatory)
#          GATUS_URL, GATUS_EXTERNAL_TOKEN (mandatory)
#          GATUS_PUSH_TIMEOUT (seconds, default 10)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# view | gatus endpoint key | breach threshold (breach when count GREATER than this)
SPEC=(
  "paperclip_inv_agents_error_10m|paperclip_agents-error-10m|0"
  "paperclip_inv_failed_subtype_success|paperclip_failed-subtype-success|0"
  "paperclip_inv_cards_rewoken_6h|paperclip_cards-rewoken-6h|0"
  "paperclip_inv_missing_disposition_reparks|paperclip_missing-disposition-reparks|2"
  "paperclip_inv_unassigned_infocus_cards|paperclip_unassigned-infocus-cards|0"
  "paperclip_inv_db_pool_waiters|paperclip_db-pool-waiters|5"
)

fail_closed=0

need() {
  [[ -n "${!1:-}" ]] || { echo "push-paperclip-invariants: refusing — $1 is unset" >&2; return 1; }
}

gatus_push() {
  local key="$1" success="$2" err="${3:-}"
  [[ "$key" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
    || { echo "push-paperclip-invariants: refusing — bad endpoint key '$key'" >&2; return 2; }
  if (( ${#err} > 500 )); then err="${err:0:500}…(truncated)"; fi
  local err_qs=""
  if [[ -n "$err" ]]; then
    err_qs="&error=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$err")"
  fi
  local cfg_dir cfg http
  cfg_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/pcinv_push.XXXXXXXX")" \
    || { echo "push-paperclip-invariants: could not create a private temp directory" >&2; return 2; }
  # Expand now: the trap runs after this function returns, when locals are gone.
  # shellcheck disable=SC2064
  trap "rm -rf '$cfg_dir'" RETURN
  cfg="$(umask 077; mktemp "$cfg_dir/curlcfg.XXXXXXXX")" \
    || { echo "push-paperclip-invariants: could not create a curl config file" >&2; return 2; }
  {
    printf 'url = "%s/api/v1/endpoints/%s/external?success=%s%s"\n' \
      "$GATUS_URL" "$key" "$success" "$err_qs"
    printf 'request = "POST"\n'
    printf 'header = "Authorization: Bearer %s"\n' "$GATUS_EXTERNAL_TOKEN"
    printf 'max-time = "%s"\n' "${GATUS_PUSH_TIMEOUT:-10}"
    printf 'silent\nshow-error\n'
  } > "$cfg"
  chmod 0600 "$cfg"
  http="$(curl --config "$cfg" -o /dev/null -w '%{http_code}' 2>&1)" \
    || { echo "push-paperclip-invariants: curl failed pushing $key: $http" >&2; return 1; }
  case "$http" in
    2*) return 0 ;;
    *)  echo "push-paperclip-invariants: refused: HTTP $http pushing $key" >&2; return 1 ;;
  esac
}

# Preflight everything before the first probe so a half-pushed round is
# impossible: either all six endpoints get a fresh verdict or none do.
need PAPERCLIP_RO_DB_URL || exit 1
need GATUS_URL || exit 1
need GATUS_EXTERNAL_TOKEN || exit 1
command -v psql >/dev/null 2>&1 || { echo "push-paperclip-invariants: refusing — psql not found" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "push-paperclip-invariants: refusing — curl not found" >&2; exit 1; }

declare -a RESULTS=()
for row in "${SPEC[@]}"; do
  view="${row%%|*}"; rest="${row#*|}"; key="${rest%%|*}"; threshold="${rest##*|}"
  [[ "$view" =~ ^paperclip_inv_[a-z0-9_]+$ ]] \
    || { echo "push-paperclip-invariants: refusing — view '$view' outside the allowlist shape" >&2; exit 1; }
  grep -q "CREATE OR REPLACE VIEW $view AS" "$HERE/paperclip_invariants.sql" \
    || { echo "push-paperclip-invariants: refusing — view '$view' not defined in paperclip_invariants.sql" >&2; exit 1; }
  count="$(PGOPTIONS='-c default_transaction_read_only=on' \
    psql "$PAPERCLIP_RO_DB_URL" -tA -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM $view;" 2>&1)" || {
    echo "push-paperclip-invariants: probe failed for $view: $count" >&2
    RESULTS+=("$key|false|probe failed for $view")
    fail_closed=1
    continue
  }
  [[ "$count" =~ ^[0-9]+$ ]] || {
    echo "push-paperclip-invariants: unparseable count for $view: $count" >&2
    RESULTS+=("$key|false|unparseable count for $view")
    fail_closed=1
    continue
  }
  if (( count > threshold )); then
    RESULTS+=("$key|false|$count rows over threshold $threshold in $view")
  else
    RESULTS+=("$key|true|")
  fi
done

rc=0
for r in "${RESULTS[@]}"; do
  key="${r%%|*}"; rest="${r#*|}"; success="${rest%%|*}"; err="${rest#*|}"
  gatus_push "$key" "$success" "$err" || { rc=1; fail_closed=1; }
done

if (( fail_closed )); then
  echo "push-paperclip-invariants: round finished with a breach or a probe failure" >&2
  exit 1
fi
echo "push-paperclip-invariants: all six invariants within threshold"
