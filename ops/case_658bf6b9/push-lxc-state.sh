#!/usr/bin/env bash
# push-lxc-state.sh — cron: `lxc list` state → Gatus `platform_lxc-ci-vms`.
#
# Reports success=true when every CI VM the operator lists in LXC_VMS (space
# separated, default "ci-node7b1ff338 ci-node7b1ff338-iso" — both RUNNING, measured 2026-10-02)
# is Running; otherwise success=false with the failing names in the error
# text. Failure to even run `lxc list` is also a failure push (exit 1) — a
# blind monitor must never read green — except a missing `lxc` binary, which
# is an operator-install problem (exit 2).
#
# NOTE (LXD 5.21): there is no `-c v` column — keep `-c n,s` exactly; the
# richer `lxc list -c n,s,4,t` read path belongs to push-garm-pool.sh's
# follow-up, not this state check.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-lxc-state.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

LXC_BIN="${LXC_BIN:-lxc}"
VMS="${LXC_VMS:-ci-node7b1ff338 ci-node7b1ff338-iso}"

command -v "$LXC_BIN" >/dev/null 2>&1 \
  || { echo "push-lxc-state: refusing — no '$LXC_BIN' on PATH (operator install?)" >&2; exit 2; }

out="$("$LXC_BIN" list --format csv -c n,s 2>&1)" \
  || { gatus_push platform_lxc-ci-vms false "lxc list failed"; exit 1; }

bad=()
while read -r vm; do
  [[ -n "$vm" ]] || continue
  state="$(awk -F, -v n="$vm" '$1==n{print $2}' <<<"$out")"
  [[ "$state" == "RUNNING" ]] || bad+=("$vm:${state:-absent}")
done <<<"${VMS// /$'\n'}"

if (( ${#bad[@]} == 0 )); then
  gatus_push platform_lxc-ci-vms true
else
  gatus_push platform_lxc-ci-vms false "not running: ${bad[*]}"
fi
