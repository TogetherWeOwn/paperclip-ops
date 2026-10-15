#!/usr/bin/env bash
# paperclip-inv-count.sh — on-call read-only invariant count (case_66436e78 slice 6).
#
# Invoked ONLY through the six exact-match `paperclip-inv <key>` arms of
# ro-shell.sh (forced command, deny-by-default). <key> is one of the six
# breach keys below; it maps to a hardcoded case_654dd2ec view — the key never
# reaches SQL except through this allow-list.
#
# READ-ONLY: the session sets default_transaction_read_only=on via
# PGOPTIONS, and the only statement ever issued is
# "SELECT count(*) FROM <allowlisted view>". Prints `key: N` (counts only
# — row contents, including error text that can carry secrets, never leave
# the database through this path).
#
# FAIL CLOSED: a missing env file/URL, a missing psql, a refused
# connection, or an unparseable count exits non-zero (2 = refused usage,
# 1 = probe failed: page, never report healthy).
#
# NON-DISCLOSURE: PAPERCLIP_RO_DB_URL travels in the environment (root-only
# 0600 env file, same file the case_658bf6b9 pushes use) and reaches psql as a
# single word that is never printed; on failure only the key/view and a
# fixed rc are printed — psql's stderr (which can echo parts of the URL on
# some parse errors) is discarded to /dev/null, never sent to syslog or back
# over the SSH forced command. A fixed `probe failed for <view> rc=N` line
# still goes to the host syslog via logger so operators can correlate pages
# without ever seeing connection text. PAPERCLIP_INV_ENV_FILE overrides the
# env-file path in tests only; production always uses the root-only default
# (client input cannot reach it — ro-shell matches the whole command
# literally and passes a hardcoded key). PAPERCLIP_INV_VIEWS_FILE likewise
# overrides the views-file lookup in tests only, and PAPERCLIP_INV_DEPLOY_ROOT
# overrides the deployed-checkout prefix in tests only; production resolves
# repo-relative first, then the fixed deployed checkout.
set -euo pipefail

ENV_FILE="${PAPERCLIP_INV_ENV_FILE:-/root/.config/paperclip/case_60330d6f.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090 — host-provided env file, path fixed above.
  . "$ENV_FILE"
  set +a
fi

# Resolve through symlinks (an installed symlink still finds the repo), then
# locate the case_654dd2ec views file: repo-relative first (dev/test), fixed
# deployed checkout second (OPERATOR.md copies this helper to /usr/local/bin,
# where the relative lookup breaks). Test-only PAPERCLIP_INV_VIEWS_FILE wins.
SRC_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SRC_PATH" ]]; do
  LINK="$(readlink "$SRC_PATH")"
  if [[ "$LINK" = /* ]]; then SRC_PATH="$LINK"; else SRC_PATH="$(dirname "$SRC_PATH")/$LINK"; fi
done
HERE="$(cd "$(dirname "$SRC_PATH")" && pwd)"
VIEWS_FILE="${PAPERCLIP_INV_VIEWS_FILE:-}"
if [[ -z "$VIEWS_FILE" ]]; then
  DEPLOY_ROOT="${PAPERCLIP_INV_DEPLOY_ROOT:-/opt/status-page/current}"
  if [[ -f "$HERE/../case_654dd2ec/paperclip_invariants.sql" ]]; then
    VIEWS_FILE="$HERE/../case_654dd2ec/paperclip_invariants.sql"
  else
    VIEWS_FILE="$DEPLOY_ROOT/ops/case_654dd2ec/paperclip_invariants.sql"
  fi
fi

case "${1:-}" in
  agents-error-10m) VIEW="paperclip_inv_agents_error_10m" ;;
  failed-subtype-success) VIEW="paperclip_inv_failed_subtype_success" ;;
  cards-rewoken-6h) VIEW="paperclip_inv_cards_rewoken_6h" ;;
  missing-disposition-reparks) VIEW="paperclip_inv_missing_disposition_reparks" ;;
  unassigned-infocus-cards) VIEW="paperclip_inv_unassigned_infocus_cards" ;;
  db-pool-waiters) VIEW="paperclip_inv_db_pool_waiters" ;;
  *) echo "paperclip-inv-count: refusing — unknown key '${1:-<empty>}'" >&2; exit 2 ;;
esac
KEY="$1"

[[ "$VIEW" =~ ^paperclip_inv_[a-z0-9_]+$ ]] \
  || { echo "paperclip-inv-count: refusing — view '$VIEW' outside the allowlist shape" >&2; exit 2; }
[[ -f "$VIEWS_FILE" ]] && grep -qF "CREATE OR REPLACE VIEW $VIEW AS" "$VIEWS_FILE" \
  || { echo "paperclip-inv-count: refusing — view '$VIEW' not defined in paperclip_invariants.sql" >&2; exit 2; }

[[ -n "${PAPERCLIP_RO_DB_URL:-}" ]] \
  || { echo "paperclip-inv-count: refusing — PAPERCLIP_RO_DB_URL is unset" >&2; exit 2; }
command -v psql >/dev/null 2>&1 \
  || { echo "paperclip-inv-count: refusing — psql not found" >&2; exit 2; }

# psql's stderr can echo parts of the connection string on some parse errors,
# so it is discarded — never logged, never sent back over the forced command.
# The client and the host syslog see only the fixed view + rc message.
psql_rc=0
count="$(PGOPTIONS='-c default_transaction_read_only=on' \
  psql "$PAPERCLIP_RO_DB_URL" -tA -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM $VIEW;" 2>/dev/null)" || psql_rc=$?
if (( psql_rc != 0 )); then
  if command -v logger >/dev/null 2>&1; then
    logger -t paperclip-inv-count "probe failed for $VIEW rc=$psql_rc" 2>/dev/null || true
  fi
  echo "paperclip-inv-count: probe failed for $VIEW (rc=$psql_rc)" >&2
  exit 1
fi
[[ "$count" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] \
  || { echo "paperclip-inv-count: probe failed for $VIEW: unparseable count" >&2; exit 1; }
count="$(printf '%s' "$count" | tr -d '[:space:]')"
echo "$KEY: $count"
