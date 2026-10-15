#!/usr/bin/env bash
# push-oom-kills.sh — cron: journal OOM kills → Gatus `platform_oom-kills`.
#
# success=true when no OOM-killer line appeared in the last window;
# success=false naming the victims otherwise (an OOM kill took down the
# ci-node7b1ff338 VM + 16 runners before — case_1fe7979a — so this is signal, not noise).
# The journal read failing is a failure push (exit 1), never a clean report.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-oom-kills.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

WINDOW="${OOM_WINDOW:-10min}"
JOURNALCTL_BIN="${JOURNALCTL_BIN:-journalctl}"

command -v "$JOURNALCTL_BIN" >/dev/null 2>&1 \
  || { echo "push-oom-kills: refusing — no '$JOURNALCTL_BIN' on PATH" >&2; exit 2; }

out="$("$JOURNALCTL_BIN" --since "-$WINDOW" -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short 2>&1)" \
  || { gatus_push platform_oom-kills false "journal read failed"; exit 1; }

if [[ -z "${out//[[:space:]]/}" ]]; then
  gatus_push platform_oom-kills true
else
  victims="$(grep -oE 'Killed process [0-9]+ \([^)]+\)' <<<"$out" | sort -u | head -5 | tr '\n' ';')"
  gatus_push platform_oom-kills false "OOM in last $WINDOW${victims:+: $victims}"
fi
