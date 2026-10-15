#!/usr/bin/env bash
# ===========================================================================
# docker_hygiene.sh — prune DANGLING Docker images + build cache older than
# N hours (case_b0e822f6 item 4: Docker hygiene timer).
#
# WHY THIS SCRIPT EXISTS. On 2026-10-09 node7b1ff338 /home measured 92%+ with docker
# at 59G. Untagged ("dangling") images and stale build cache accumulate on
# every image build and nothing reclaims them. This tool is the recurring,
# bounded form of `docker image prune` / `docker builder prune`.
#
# SAFETY MODEL — rollback images are safe BY CONSTRUCTION, not by list:
#   * The image prune NEVER passes -a/--all: only DANGLING (untagged,
#     unreferenced) images are candidates. A tagged rollback image is not
#     dangling and cannot be selected, whatever its age. `test_docker_hygiene.sh`
#     pins the exact argv and fails on any -a/--all/rmi/volume/container/
#     network/system-prune token.
#   * The builder prune touches only the build cache (regenerable by rebuild).
#     Volumes, containers, networks and the daemon config are never addressed:
#     no `volume`, `container`, `network`, `system prune` or `rmi` argv exists
#     anywhere in this file — grep is the audit.
#   * DRY-RUN is the default. Nothing is pruned without --apply passed
#     literally on argv (the mode bit lives in reviewed bytes, never in the
#     environment: the sweep-auto drift precedent). The env file can only turn
#     the VOLUME knob (age hours) and the kill switch, never flip the mode.
#   * A kill switch forces dry-run: env DOCKER_HYGIENE_DISABLED=1.
#   * Fail closed: a missing docker binary, a non-integer age, or a failed
#     prune step exits non-zero LOUDLY (exit 2) before/after anything runs. A
#     run that measured nothing must never read as a clean bill of health.
#   * Bounded: the age filter (default 168h = 7d) scopes every prune; the
#     systemd unit wraps the run in `timeout` + TimeoutStartSec.
#
# USAGE
#   docker_hygiene.sh                 # dry-run report, prunes nothing
#   docker_hygiene.sh --apply         # actually prune dangling > age
#
# OVERRIDES (for the offline suite; all optional):
#   DOCKER_BIN                  docker binary (default: docker)
#   DOCKER_HYGIENE_AGE_HOURS    prune objects older than this many hours
#                               (default: 168; must be a positive integer)
#   DOCKER_HYGIENE_DISABLED=1   kill switch: force dry-run even with --apply
#
# Exit codes: 0 report/apply complete, 2 usage or fatal error (docker
# missing/unusable, bad age, a prune step failed).
#
# No secrets in output: stdout carries only sizes, counts and the docker
# data-root path. No credential is read, printed or passed.
#
# Rollback: `systemctl disable --now paperclip-docker-hygiene.timer`. Pruned
# bytes are dangling images (unreferenced by definition) and build cache
# (rebuilt on the next build); tagged images are never candidates.
# ===========================================================================
set -uo pipefail

APPLY=0
for _a in "$@"; do
  case "$_a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    *) echo "usage: $0 [--apply|--dry-run]" >&2; exit 2 ;;
  esac
done

DOCKER_BIN="${DOCKER_BIN:-docker}"
AGE_HOURS="${DOCKER_HYGIENE_AGE_HOURS:-168}"
case "$AGE_HOURS" in ''|*[!0-9]*)
  echo "REFUSED: DOCKER_HYGIENE_AGE_HOURS='$AGE_HOURS' is not a positive integer" >&2
  exit 2 ;;
esac
[ "$AGE_HOURS" -ge 1 ] 2>/dev/null \
  || { echo "REFUSED: DOCKER_HYGIENE_AGE_HOURS='$AGE_HOURS' is not a positive integer" >&2; exit 2; }

if [ -n "${DOCKER_HYGIENE_DISABLED:-}" ]; then
  case "${DOCKER_HYGIENE_DISABLED,,}" in 1|true|yes|on)
    echo "KILL-SWITCH (DOCKER_HYGIENE_DISABLED): apply disabled, dry-run only" >&2
    APPLY=0 ;;
  esac
fi

command -v "$DOCKER_BIN" >/dev/null 2>&1 \
  || { echo "REFUSED: docker binary not found: $DOCKER_BIN" >&2; exit 2; }

mode=$([ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN)
echo "=== docker hygiene ($mode, older than ${AGE_HOURS}h) — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Receipts: where docker keeps its bytes, and how full that filesystem is,
# before and after. Best-effort reads; a failed read is reported, never fatal
# to the verdict — the prune steps below are the verdict.
docker_root="$("$DOCKER_BIN" info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
[ -n "$docker_root" ] || docker_root="(unknown docker root)"
echo "--- docker root: $docker_root ---"
if [ -d "$docker_root" ]; then df -h "$docker_root" 2>/dev/null | tail -1; fi
echo "--- docker system df (before) ---"
"$DOCKER_BIN" system df 2>/dev/null || echo "(docker system df unavailable)"

if [ "$APPLY" = 0 ]; then
  echo "--- dry-run: no prune executed (pass --apply to prune) ---"
  echo "--- would run: $DOCKER_BIN image prune -f --filter until=${AGE_HOURS}h (dangling only, never -a) ---"
  echo "--- would run: $DOCKER_BIN builder prune -f --filter until=${AGE_HOURS}h (build cache only) ---"
  echo "=== docker hygiene: DRY-RUN COMPLETE, nothing pruned ==="
  exit 0
fi

rc=0
echo "--- prune dangling images (tagged rollback images are never candidates) ---"
# PINNED ARGV: `image prune` without -a/--all removes DANGLING images only.
# A tagged image — including every rollback image — is not dangling and is
# therefore unselectable here regardless of age. The offline suite pins this.
if ! "$DOCKER_BIN" image prune -f --filter "until=${AGE_HOURS}h"; then
  echo "::error::docker image prune failed" >&2
  rc=2
fi
echo "--- prune build cache ---"
# Build cache is regenerable by the next build; scoped by the same age filter.
if ! "$DOCKER_BIN" builder prune -f --filter "until=${AGE_HOURS}h"; then
  echo "::error::docker builder prune failed" >&2
  rc=2
fi

echo "--- docker system df (after) ---"
"$DOCKER_BIN" system df 2>/dev/null || echo "(docker system df unavailable)"
if [ -d "$docker_root" ]; then df -h "$docker_root" 2>/dev/null | tail -1; fi
if [ "$rc" = 0 ]; then
  echo "=== docker hygiene: APPLY COMPLETE ==="
else
  echo "=== docker hygiene: APPLY FINISHED WITH FAILURES ===" >&2
fi
exit "$rc"
