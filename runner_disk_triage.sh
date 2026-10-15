#!/usr/bin/env bash
# ===========================================================================
# runner_disk_triage.sh — read-only runner-disk triage (case_534bd668).
#
# Child of case_9dbf0796. Read-only enumeration step called by the disk-pressure
# runbook owned by case_d8c3515a (alert + authoritative runbook live there; this
# tool performs no alerting and no cleanup). Suspect list comes from the
# case_a97273bd on-call note: parked legacy worktree dependency caches
# (node_modules / vendor), the container writable layer, and the LXD pool.
#
# SAFETY MODEL — read-only by construction:
#   * The only host commands this script ever invokes are: df, du, find
#     (listing only), cat (fixture/info files only), sort, cut, sed (stream
#     filter, no in-place edit), and the read-only `lxc storage list` /
#     `zfs list` probes when those binaries exist. It creates no temp files
#     and modifies nothing.
#   * Any write-intent argument (--apply, --clean, --delete, --prune, --fix
#     and kin, see refuse_arg()) exits 2 without enumerating anything.
#     Cleanup execution stays operator-owned under case_d8c3515a.
#   * Output carries sizes, counts and paths only. It never prints
#     environment values, file contents (beyond an operator-supplied info
#     fixture), or credentials.
#
# OVERRIDES (for the offline suite; all optional):
#   TRIAGE_WORKTREE_ROOT  dir whose children are scanned for node_modules /
#                         vendor caches (default: auto-detected worktrees dir)
#   TRIAGE_DOCKER_ROOT    container storage root to size (default:
#                         /var/lib/docker when readable)
#   TRIAGE_PODMAN_ROOT    second container storage root to size (default:
#                         ~/.local/share/containers/storage when readable)
#   TRIAGE_DF_FILE        fixture file whose content replaces `df -h`
#   TRIAGE_DFI_FILE       fixture file whose content replaces `df -i`
#   TRIAGE_LXD_INFO_FILE  fixture file whose content replaces live
#                         `lxc storage` / `zfs` probing
#   TRIAGE_LXD_USAGE_FILE fixture file whose content replaces live
#                         `lxc storage info` per-pool probing (used/total
#                         for the case_a814f742 GiB tripwire)
#   LXC_BIN               lxc binary the LXD probes use (default lxc). The
#                         push script exports the same binary it uses for
#                         `lxc exec`, so the pool check can never go blind
#                         while the VM checks succeed (case_71e7a4e9).
#   TRIAGE_SKIP_SIZE_SCAN when set, skip the du size scan (suspect caches
#                         plus container roots report SKIPPED). The Gatus
#                         push sets it: the evaluator never reads sizes, so
#                         the 10-minute push must not scan the host it guards.
#
# Exit codes: 0 enumerated (missing suspects report as missing, not failure);
#             2 usage or write-intent refusal.
# ===========================================================================

set -uo pipefail

FORMAT="text"

usage() {
  cat <<'EOF'
Usage: runner_disk_triage.sh [--format text|json] [--json] [-h|--help]

Read-only runner-disk triage for the case_d8c3515a disk-pressure runbook.
Prints df + inode usage and per-suspect sizing. Never deletes anything:
any write-intent flag is refused with exit 2.
EOF
}

# ---- write-intent refusal (checked before anything else touches the host) --
refuse_arg() {
  case "$1" in
    --apply|--clean*|--delete*|--del*|--prune*|--fix|--rm*|--remove*|\
    --kill*|--reclaim*|--purge*|--wipe*|--exec*|--command*|--run*|\
    clean|delete|prune|fix|apply|purge|wipe|reclaim)
      printf 'REFUSE: %s would mutate the runner; this tool is read-only (cleanup is operator-owned under case_d8c3515a)\n' "$1" >&2
      return 0
      ;;
    *) return 1 ;;
  esac
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --json) FORMAT="json" ;;
    --format) : ;; # value consumed by the loop below
    --format=*) FORMAT="${arg#--format=}";;
    *) : ;;
  esac
done
# Two-arg --format <value> (kept separate so unknown flags still enumerate).
_prev=""
for arg in "$@"; do
  if [[ "$_prev" == "--format" ]]; then FORMAT="$arg"; fi
  _prev="$arg"
done
case "$FORMAT" in
  text|json) : ;;
  *) printf 'usage: unknown --format %s (want text|json)\n' "$FORMAT" >&2; exit 2 ;;
esac
# MUTATION-ANCHOR-START: refusal gate (the offline suite deletes this block to
# prove the gate is load-bearing: the mutant must accept --apply).
for arg in "$@"; do
  case "$arg" in
    -h|--help|--json|--format|--format=*) : ;;
    --*) refuse_arg "$arg" && exit 2 ;;
  esac
  # Bare-word write intents (no dashes) are also refused.
  case "$arg" in
    clean|delete|prune|fix|apply|purge|wipe|reclaim) refuse_arg "$arg" && exit 2 ;;
  esac
done
# MUTATION-ANCHOR-END

# ---- roots -----------------------------------------------------------------
if [[ -n "${TRIAGE_WORKTREE_ROOT:-}" ]]; then
  WORKTREE_ROOT="$TRIAGE_WORKTREE_ROOT"
elif [[ -d /paperclip/instances/default/worktrees ]]; then
  WORKTREE_ROOT="/paperclip/instances/default/worktrees"
else
  WORKTREE_ROOT="${HOME:-/root}/worktrees"
fi
DOCKER_ROOT="${TRIAGE_DOCKER_ROOT:-/var/lib/docker}"
PODMAN_ROOT="${TRIAGE_PODMAN_ROOT:-${HOME:-/root}/.local/share/containers/storage}"

# ---- df / inode overview ----------------------------------------------------
if [[ -n "${TRIAGE_DF_FILE:-}" ]]; then
  DF_OUT="$(cat -- "${TRIAGE_DF_FILE}")"
else
  DF_OUT="$(df -h 2>/dev/null || printf 'df unavailable\n')"
fi
if [[ -n "${TRIAGE_DFI_FILE:-}" ]]; then
  DFI_OUT="$(cat -- "${TRIAGE_DFI_FILE}")"
else
  DFI_OUT="$(df -i 2>/dev/null || printf 'df -i unavailable\n')"
fi

# ---- per-suspect sizing (KB, deterministic) --------------------------------
# Prints lines: <kb>\t<path>. Never follows into unreadable trees loudly.
kb_of() {
  if [[ -d "$1" ]]; then
    du -sk -- "$1" 2>/dev/null | cut -f1
  else
    printf 'MISSING'
  fi
}

SUSPECT_LINES=""
if [[ -n "${TRIAGE_SKIP_SIZE_SCAN:-}" ]]; then
  # The Gatus push reads df/inodes/pools only; scanning every cache on the
  # guarded host each run wastes the very disk pressure it monitors.
  DOCKER_KB="SKIPPED"
  PODMAN_KB="SKIPPED"
elif [[ -d "$WORKTREE_ROOT" ]]; then
  while IFS= read -r child; do
    [[ -n "$child" ]] || continue
    for cache in node_modules vendor; do
      cdir="$child/$cache"
      if [[ -d "$cdir" ]]; then
        kb="$(kb_of "$cdir")"
        SUSPECT_LINES+="${kb}	${cdir}"$'\n'
      fi
    done
  done < <(find -- "$WORKTREE_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
else
  SUSPECT_LINES="MISSING	${WORKTREE_ROOT} (worktree root absent)"$'\n'
fi

if [[ -z "${TRIAGE_SKIP_SIZE_SCAN:-}" ]]; then
  DOCKER_KB="$(kb_of "$DOCKER_ROOT")"
  PODMAN_KB="$(kb_of "$PODMAN_ROOT")"
fi

# ---- LXD pool (read-only info only) -----------------------------------------
# The lxc binary resolves through LXC_BIN (default lxc): a snap install
# outside cron's PATH must not read as "no LXD" while the push script's own
# `lxc exec` checks succeed against the same binary.
LXC="${LXC_BIN:-lxc}"
if [[ -n "${TRIAGE_LXD_INFO_FILE:-}" ]]; then
  LXD_OUT="$(cat -- "${TRIAGE_LXD_INFO_FILE}")"
elif command -v "$LXC" >/dev/null 2>&1; then
  LXD_OUT="$("$LXC" storage list 2>/dev/null || printf 'lxc storage list unavailable\n')"
elif command -v zfs >/dev/null 2>&1; then
  LXD_OUT="$(zfs list 2>/dev/null || printf 'zfs list unavailable\n')"
else
  LXD_OUT="UNAVAILABLE: neither lxc nor zfs present on this host"
fi

# ---- LXD pool usage totals (read-only tripwire input for case_a814f742) -----------
# `lxc storage list` names pools but carries no used/total, so the disk
# module reads per-pool `lxc storage info` (read-only) here. A host with no
# lxc reports the exact "UNAVAILABLE: neither lxc nor zfs present" sentinel and
# the module skips the pool check. A failed `lxc storage info` is NOT that
# sentinel: it prints "info unavailable" under its pool header and the module
# fails closed (UNKNOWN naming the pool) downstream, never here.
if [[ -n "${TRIAGE_LXD_USAGE_FILE:-}" ]]; then
  LXD_USAGE_OUT="$(cat -- "${TRIAGE_LXD_USAGE_FILE}")"
elif command -v "$LXC" >/dev/null 2>&1; then
  LXD_USAGE_OUT=""
  while IFS= read -r pool; do
    [[ -n "$pool" ]] || continue
    LXD_USAGE_OUT+="== pool ${pool} =="$'\n'
    LXD_USAGE_OUT+="$("$LXC" storage info "$pool" 2>/dev/null || printf 'info unavailable')"$'\n'
  done < <("$LXC" storage list --format csv -c n 2>/dev/null)
  [[ -n "$LXD_USAGE_OUT" ]] || LXD_USAGE_OUT="lxc storage info unavailable"
else
  LXD_USAGE_OUT="UNAVAILABLE: neither lxc nor zfs present on this host"
fi

# ---- render -----------------------------------------------------------------
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g' -e 's/\t/\\t/g'
}

if [[ "$FORMAT" == "json" ]]; then
  suspects_json=""
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    kb="${line%%	*}"; p="${line#*	}"
    [[ -n "$suspects_json" ]] && suspects_json+=","
    suspects_json+="{\"kb\":\"$(json_escape "$kb")\",\"path\":\"$(json_escape "$p")\"}"
  done <<<"$SUSPECT_LINES"
  printf '{"tool":"runner_disk_triage","runbook_owner":"case_d8c3515a","read_only":true,"df":"%s","df_inode":"%s","suspects":[%s],"docker_root_kb":"%s","podman_root_kb":"%s","lxd":"%s","lxd_usage":"%s"}\n' \
    "$(json_escape "$DF_OUT")" "$(json_escape "$DFI_OUT")" "$suspects_json" \
    "$(json_escape "$DOCKER_KB")" "$(json_escape "$PODMAN_KB")" "$(json_escape "$LXD_OUT")" \
    "$(json_escape "$LXD_USAGE_OUT")"
  exit 0
fi

printf '== runner-disk triage (read-only; runbook owner case_d8c3515a) ==\n'
printf '\n-- disk overview (df -h) --\n%s\n' "$DF_OUT"
printf '\n-- inode overview (df -i) --\n%s\n' "$DFI_OUT"
printf '\n-- worktree dependency caches under %s --\n' "$WORKTREE_ROOT"
printf '%s' "$SUSPECT_LINES"
printf '\n-- container writable layer --\n'
printf 'docker root %s : %s KB\n' "$DOCKER_ROOT" "$DOCKER_KB"
printf 'podman root %s : %s KB\n' "$PODMAN_ROOT" "$PODMAN_KB"
printf '\n-- LXD pool --\n%s\n' "$LXD_OUT"
printf '\n-- LXD pool usage totals (GiB tripwire input) --\n%s\n' "$LXD_USAGE_OUT"
printf '\nNOTE: read-only triage only. Cleanup is operator-executed under case_d8c3515a.\n'
