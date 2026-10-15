#!/usr/bin/env bash
# case_5866fa6f — Paperclip host disk retention sweep.
#
# Reclaims the classes that Paperclip itself does NOT manage (verified against the
# deployed server source in case_5866fa6f): stale per-agent workspace clones, empty orphan
# workspace dirs, old run-logs, old Claude Code session transcripts, an oversized
# go-build cache, and regenerable dependency caches (node_modules/.venv/etc.) inside
# otherwise-idle clones. It never touches anything Paperclip owns (DB backups,
# execution workspace DB lifecycle, container images) or any agent memory.
# Class 7 (case_999adcc5) reaps per-run scratch dirs that Paperclip creates under the
# container's /tmp and removes only on a normal run end: after a server crash or
# restart they leak onto the container layer, which lives on the host root disk.
# Class 8 (case_0ddff3ec) reclaims the orphaned pnpm content store the same /tmp
# collects: `pnpm store path` is /tmp/.pnpm-store for every agent project, so each
# `pnpm install` writes there, hard-links node_modules out of it, and the store
# outlives the run scratch that held the links. Class 9 (case_0ddff3ec) is a
# report-only survey of /tmp by owner class and age; it never deletes.
#
# SAFETY MODEL
#   * DRY-RUN by default. It deletes nothing unless you pass --apply.
#   * Git clones are removed ONLY when provably nothing is lost: working tree clean
#     AND zero commits absent from a remote AND zero stashes AND untouched for
#     $CLONE_IDLE_DAYS. Such a clone is byte-identical to origin and re-cloneable.
#   * A git command that errors (e.g. a worktree whose common .git dir vanished) is
#     "unverifiable", never "clean" -- it fails closed and is kept, not reclaimed.
#   * The idle-activity scan ignores dependency-cache directory names (node_modules,
#     .venv, __pycache__, ...) so a package-manager reinstall can't reset the idle
#     clock on a clone nobody has actually edited.
#   * Dependency caches are reclaimed independently of a clone's dirty/unpushed/stash
#     state (they're always regenerable) but ONLY when git itself considers the path
#     ignored (git check-ignore) and the clone is idle -- never a tracked directory.
#   * Transcript sweep targets ONLY *.jsonl and explicitly skips any path containing
#     /memory/ — agent memory lives under .claude/projects/*/memory/ and is durable.
#   * Run scratch (class 7) is reaped ONLY when it is a direct child of $SCRATCH_ROOT
#     named paperclip-run-*, carries Paperclip's marker with a runId, is idle for
#     $SCRATCH_IDLE_HOURS, and no live process references it: no environ value, cwd or
#     open fd under it. If no process environ is readable, liveness is unverifiable and
#     the whole class is skipped. Run it inside the Paperclip container (that is where
#     the scratch lives), e.g. `docker exec -u node <paperclip> disk-retention-sweep.sh`.
#   * The pnpm store (class 8) loses only files whose link count is 1 (no node_modules
#     hard-links them: this is the set `pnpm store prune` removes), unchanged for
#     $PNPM_STORE_IDLE_HOURS, and only while no live process is running pnpm (an argv element named pnpm/pnpx). A file a
#     live install links has a link count of 2 or more and is never listed. If no
#     process cmdline is readable, liveness is unverifiable and the class is skipped.
#   * A cutover marker file makes the whole sweep a no-op, so it never runs during the
#     case_5e07b8c4 upgrade window.
#
# USAGE
#   disk-retention-sweep.sh                      # dry-run report, deletes nothing
#   disk-retention-sweep.sh --apply              # actually reclaim
#   disk-retention-sweep.sh --tmp-only [--apply] # classes 7-9 only: the /tmp pass.
#       Classes 1-6 walk every agent clone with git (hours under load); the /tmp
#       pass takes seconds, so it is the one a short timer runs.
#
set -uo pipefail

# ---- locate the Paperclip home (container view is /paperclip) --------------------
# SWEEP_* overrides exist for the offline suite (test_case_79b3d149_scratch_reaper.sh).
if [ -n "${SWEEP_PAPERCLIP_HOME:-}" ]; then
  PAPERCLIP_HOME="$SWEEP_PAPERCLIP_HOME"
elif [ -d /paperclip ]; then
  PAPERCLIP_HOME=/paperclip
else
  PAPERCLIP_HOME="${PAPERCLIP_HOME:-$HOME/.local/share/paperclip}"
fi

WS="$PAPERCLIP_HOME/instances/default/workspaces"
RUNLOGS="$PAPERCLIP_HOME/instances/default/data/run-logs"
TRANSCRIPTS="$PAPERCLIP_HOME/.claude/projects"
GO_CACHE="$PAPERCLIP_HOME/.cache/go-build"
CUTOVER_MARKER="$PAPERCLIP_HOME/instances/default/.case_e129e5c6-cutover-active"
# os.tmpdir() of the Paperclip server. Deliberately NOT $TMPDIR: inside a run that
# points at the run's own scratch dir, not at the root that holds every run's.
SCRATCH_ROOT="${SWEEP_SCRATCH_ROOT:-/tmp}"
PROC_ROOT="${SWEEP_PROC_ROOT:-/proc}"
SCRATCH_MARKER=".paperclip-run-scratch.json"

# ---- tunables (every number lives here on purpose) ------------------------------
EMPTY_DIR_AGE_DAYS=14      # empty, unreferenced workspace husks older than this
CLONE_IDLE_DAYS=30         # a safe clone must also be untouched this long
RUNLOG_AGE_DAYS=14
TRANSCRIPT_AGE_DAYS=14
GO_CACHE_CAP_MB=2048       # trim go-build cache down toward this size
DEP_CACHE_IDLE_DAYS=7      # dependency caches are reproducible; shorter idle bar than a full clone
DEP_CACHE_DIRS=(node_modules vendor .venv venv __pycache__ .next target .turbo .parcel-cache)
SCRATCH_IDLE_HOURS=6       # orphan run scratch: nothing under it modified for this long
# SWEEP_* overrides: the offline suite, and an operator who needs an emergency reclaim
# (SWEEP_PNPM_STORE_IDLE_HOURS=0 still keeps every linked file and any live pnpm).
PNPM_STORE_IDLE_HOURS="${SWEEP_PNPM_STORE_IDLE_HOURS:-6}"   # unlinked store content unchanged this long
SURVEY_CANDIDATE_HOURS="${SWEEP_SURVEY_CANDIDATE_HOURS:-48}" # survey lists unreferenced entries older than this
case "$PNPM_STORE_IDLE_HOURS$SURVEY_CANDIDATE_HOURS" in *[!0-9]*|"") echo "SWEEP_*_HOURS must be whole numbers" >&2; exit 2 ;; esac

APPLY=0
TMP_ONLY=0
for _a in "$@"; do
  case "$_a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --tmp-only) TMP_ONLY=1 ;;
    *) echo "usage: $0 [--apply|--dry-run] [--tmp-only]" >&2; exit 2 ;;
  esac
done
# --tmp-only: classes 1-6 each open with an existence test on one of these, so
# blanking them skips the git walks without touching any class body.
if [ "$TMP_ONLY" = 1 ]; then WS=""; RUNLOGS=""; TRANSCRIPTS=""; GO_CACHE=""; fi

export GIT_TERMINAL_PROMPT=0
mode=$([ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN)
echo "=== case_5866fa6f disk retention sweep ($mode) — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ---- cutover guard ---------------------------------------------------------------
if [ -f "$CUTOVER_MARKER" ]; then
  echo "cutover marker present ($CUTOVER_MARKER); skipping sweep (case_5e07b8c4 window)."
  exit 0
fi

reclaimed_bytes=0
mb() { awk -v b="${1:-0}" 'BEGIN{printf "%8.1f MB", b/1048576}'; }   # bytes -> MB (no bc dependency)
gb() { awk -v b="${1:-0}" 'BEGIN{printf "%.2f GB", b/1073741824}'; }

# shared "-prune" args so a stale dependency cache (node_modules, __pycache__, ...) never
# resets the idle clock a real clone is judged by -- npm/pnpm/pip touch every file's mtime
# on install even when nobody has edited actual source in months.
prune_args=(-name .git)
for _d in "${DEP_CACHE_DIRS[@]}"; do prune_args+=(-o -name "$_d"); done

# echoes a path (non-empty) if <repo> has a real, non-dependency-cache file modified
# within <days>; empty means "idle" by that definition.
repo_is_idle() {
  local repo="$1" days="$2"
  find "$repo" \( "${prune_args[@]}" \) -prune -o -type f -mtime -"$days" -print -quit 2>/dev/null
}
# remove <path> <human-label>; tallies bytes; honours dry-run
reclaim() {
  local p="$1" label="$2" b
  b=$(du -sb "$p" 2>/dev/null | cut -f1); b=${b:-0}
  if [ "$APPLY" = 1 ]; then
    rm -rf -- "$p" && { reclaimed_bytes=$((reclaimed_bytes + b)); echo "  DELETED  $(mb "$b")  $label"; }
  else
    reclaimed_bytes=$((reclaimed_bytes + b)); echo "  WOULD-RM $(mb "$b")  $label"
  fi
}

# ---- class 1: empty orphan workspace dirs ---------------------------------------
echo "--- [1] empty orphan workspace dirs (>${EMPTY_DIR_AGE_DAYS}d, no files beneath) ---"
if [ -d "$WS" ]; then
  for d in "$WS"/*/; do
    d=${d%/}; [ -d "$d" ] || continue
    # only top-level workspace roots that are completely empty of files
    [ -z "$(find "$d" -type f -print -quit 2>/dev/null)" ] || continue
    [ -n "$(find "$d" -maxdepth 0 -type d -mtime +"$EMPTY_DIR_AGE_DAYS" 2>/dev/null)" ] || continue
    reclaim "$d" "empty husk ${d#$WS/}"
  done
fi

# ---- class 2: git-safe stale clones inside agent workspace roots ----------------
echo "--- [2] stale clones (clean AND 0 unpushed AND 0 stashes AND idle >${CLONE_IDLE_DAYS}d) ---"
if [ -d "$WS" ]; then
  for gd in "$WS"/*/*/.git; do
    [ -e "$gd" ] || continue
    repo=$(dirname "$gd")
    ( cd "$repo" 2>/dev/null || exit 0
      # fail CLOSED: a git command erroring (e.g. a worktree whose common .git dir was
      # removed out from under it) must never read as "clean" -- an unreadable repo has
      # no verifiable safety state at all, which is the opposite of provably safe to rm.
      status_out=$(git status --porcelain 2>/dev/null); rc=$?
      [ "$rc" = 0 ] || { echo "  keep (git error, unverifiable) ${repo#$WS/}"; exit 0; }
      [ -z "$status_out" ] || { echo "  keep (dirty)          ${repo#$WS/}"; exit 0; }
      up_out=$(git log --branches --not --remotes --oneline 2>/dev/null); rc=$?
      [ "$rc" = 0 ] || { echo "  keep (git error, unverifiable) ${repo#$WS/}"; exit 0; }
      up=$(printf '%s\n' "$up_out" | grep -c .)
      [ "$up" = 0 ] || { echo "  keep ($up unpushed)      ${repo#$WS/}"; exit 0; }
      st_out=$(git stash list 2>/dev/null); rc=$?
      [ "$rc" = 0 ] || { echo "  keep (git error, unverifiable) ${repo#$WS/}"; exit 0; }
      st=$(printf '%s\n' "$st_out" | grep -c .)
      [ "$st" = 0 ] || { echo "  keep ($st stashes)       ${repo#$WS/}"; exit 0; }
      # idle: no non-.git, non-dependency-cache file modified within CLONE_IDLE_DAYS
      recent=$(repo_is_idle "$repo" "$CLONE_IDLE_DAYS")
      [ -z "$recent" ] || { echo "  keep (active <${CLONE_IDLE_DAYS}d)     ${repo#$WS/}"; exit 0; }
      exit 42   # safe to reclaim
    )
    [ "$?" = 42 ] && reclaim "$repo" "safe clone ${repo#$WS/}"
  done
fi

# ---- class 3: old run-logs -------------------------------------------------------
echo "--- [3] run-logs older than ${RUNLOG_AGE_DAYS}d ---"
if [ -d "$RUNLOGS" ]; then
  n=0; b=0
  while IFS= read -r -d '' f; do
    sz=$(stat -c %s "$f" 2>/dev/null); b=$((b + ${sz:-0})); n=$((n+1))
    [ "$APPLY" = 1 ] && rm -f -- "$f"
  done < <(find "$RUNLOGS" -type f -mtime +"$RUNLOG_AGE_DAYS" -print0 2>/dev/null)
  reclaimed_bytes=$((reclaimed_bytes + b))
  echo "  $([ "$APPLY" = 1 ] && echo DELETED || echo WOULD-RM) $(mb "$b")  ($n run-log files)"
fi

# ---- class 4: old session transcripts (NEVER memory) ----------------------------
echo "--- [4] Claude transcripts *.jsonl older than ${TRANSCRIPT_AGE_DAYS}d (memory/ excluded) ---"
if [ -d "$TRANSCRIPTS" ]; then
  n=0; b=0
  while IFS= read -r -d '' f; do
    case "$f" in */memory/*) continue ;; esac
    sz=$(stat -c %s "$f" 2>/dev/null); b=$((b + ${sz:-0})); n=$((n+1))
    [ "$APPLY" = 1 ] && rm -f -- "$f"
  done < <(find "$TRANSCRIPTS" -type f -name '*.jsonl' -mtime +"$TRANSCRIPT_AGE_DAYS" -print0 2>/dev/null)
  reclaimed_bytes=$((reclaimed_bytes + b))
  echo "  $([ "$APPLY" = 1 ] && echo DELETED || echo WOULD-RM) $(mb "$b")  ($n transcript files)"
fi

# ---- class 5: go-build cache size cap -------------------------------------------
echo "--- [5] go-build cache trim toward ${GO_CACHE_CAP_MB} MB ---"
if [ -d "$GO_CACHE" ]; then
  cur=$(du -sm "$GO_CACHE" 2>/dev/null | cut -f1); cur=${cur:-0}
  if [ "$cur" -gt "$GO_CACHE_CAP_MB" ]; then
    if [ "$APPLY" = 1 ]; then
      if command -v go >/dev/null 2>&1; then GOCACHE="$GO_CACHE" go clean -cache 2>/dev/null; else rm -rf -- "$GO_CACHE"/*; fi
      new=$(du -sm "$GO_CACHE" 2>/dev/null | cut -f1); new=${new:-0}
      reclaimed_bytes=$((reclaimed_bytes + (cur-new)*1048576)); echo "  TRIMMED  ${cur}MB -> ${new}MB"
    else
      echo "  WOULD-TRIM ${cur}MB (over ${GO_CACHE_CAP_MB}MB cap)"
      reclaimed_bytes=$((reclaimed_bytes + (cur-GO_CACHE_CAP_MB)*1048576))
    fi
  else
    echo "  ok (${cur}MB <= cap)"
  fi
fi

# ---- class 6: dependency caches inside idle-but-not-fully-reclaimable clones ----
# node_modules/.venv/__pycache__/etc. are always regenerable from the lockfile and are
# NOT gated on clean/pushed/no-stash like class 2 -- deleting them can never lose code.
# Scoped to agent workspace clones ($WS) only. Deliberately NOT extended to the shared
# per-project checkouts under instances/default/projects/*: those are reused across
# concurrent runs (this very run shares its project workspace with another live run),
# and a cache wipe mid-run there could break work in flight with no way for a cron
# sweep to detect "in use" -- left out of scope pending a real usage signal.
echo "--- [6] dependency caches in clones idle >${DEP_CACHE_IDLE_DAYS}d (git-ignored, regenerable) ---"
if [ -d "$WS" ]; then
  dep_only_args=(-name "${DEP_CACHE_DIRS[0]}")
  for _d in "${DEP_CACHE_DIRS[@]:1}"; do dep_only_args+=(-o -name "$_d"); done
  for gd in "$WS"/*/*/.git; do
    [ -e "$gd" ] || continue
    repo=$(dirname "$gd")
    [ -d "$repo" ] || continue   # class 2 may already have removed the whole clone
    ( cd "$repo" 2>/dev/null && git rev-parse --git-dir >/dev/null 2>&1 ) || {
      echo "  keep (git error, unverifiable) ${repo#$WS/}"; continue
    }
    idle=$(repo_is_idle "$repo" "$DEP_CACHE_IDLE_DAYS")
    [ -z "$idle" ] || continue   # still active: leave its caches warm
    while IFS= read -r -d '' cache_dir; do
      relpath=${cache_dir#"$repo"/}
      ( cd "$repo" 2>/dev/null && git check-ignore -q -- "$relpath" 2>/dev/null ) || continue
      reclaim "$cache_dir" "dep-cache ${cache_dir#$WS/}"
    done < <(find "$repo" -name .git -prune -o \( "${dep_only_args[@]}" \) -print0 -prune 2>/dev/null)
  done
fi

# ---- class 7: orphaned per-run scratch under the server's tmpdir (case_999adcc5) -----
# Paperclip makes $SCRATCH_ROOT/paperclip-run-<issue>-<run>-XXXXXX for every run, points
# the run's TMPDIR at it, and deletes it only when the run ends normally. A crash or
# restart orphans it. The marker proves Paperclip made it; the live-reference scan and
# the idle bar prove no run still uses it. Matching is by basename, so a sweep that sees
# the container's processes from another mount namespace still finds the references.
echo "--- [7] orphaned run scratch in ${SCRATCH_ROOT} (marker, unreferenced, idle >${SCRATCH_IDLE_HOURS}h) ---"
if [ -d "$SCRATCH_ROOT" ]; then
  live_refs=""; environ_read=0
  for p in "$PROC_ROOT"/[0-9]*; do
    [ -d "$p" ] || continue
    if env_txt=$(tr '\0' '\n' 2>/dev/null < "$p/environ") && [ -n "$env_txt" ]; then
      environ_read=$((environ_read + 1)); live_refs+="$env_txt"$'\n'
    fi
    live_refs+="$(find "$p" -maxdepth 1 -name cwd -printf '%l\n' 2>/dev/null)"$'\n'
    live_refs+="$(find "$p/fd" -mindepth 1 -maxdepth 1 -printf '%l\n' 2>/dev/null)"$'\n'
  done
  if [ "$environ_read" = 0 ]; then
    echo "  keep all (no process environ readable under $PROC_ROOT; liveness unverifiable)"
  else
    for d in "$SCRATCH_ROOT"/paperclip-run-*; do
      [ -d "$d" ] && [ ! -L "$d" ] || continue
      base=${d##*/}
      [[ "$base" =~ ^paperclip-run-[A-Za-z0-9_-]+$ ]] || { echo "  keep (unexpected name)  $base"; continue; }
      grep -Eqs '"runId"[[:space:]]*:[[:space:]]*"[^"]+"' "$d/$SCRATCH_MARKER" \
        || { echo "  keep (no run marker)    $base"; continue; }
      # here-string, not a pipe: under pipefail an early-exiting grep -q can SIGPIPE the
      # writer and turn a real reference into "unreferenced" -- that would fail open.
      if grep -Eq "/${base}([/:]|\$)" <<<"$live_refs"; then
        echo "  keep (live reference)   $base"; continue
      fi
      recent=$(find "$d" -mmin -"$((SCRATCH_IDLE_HOURS * 60))" -print -quit 2>/dev/null); rc=$?
      [ -z "$recent" ] || { echo "  keep (active <${SCRATCH_IDLE_HOURS}h)     $base"; continue; }
      [ "$rc" = 0 ] || { echo "  keep (unreadable, unverifiable) $base"; continue; }
      reclaim "$d" "run scratch $base"
    done
  fi
fi

# ---- class 8: orphaned pnpm content store under the server's tmpdir (case_0ddff3ec) --
# `pnpm store path` is $SCRATCH_ROOT/.pnpm-store for every agent project (checked from
# both a /tmp scratch dir and a /paperclip worktree), so the store is a shared,
# persistent, unbounded tree on the container layer. node_modules hard-links out of it;
# when a run's scratch dir is deleted the links go and the content stays at link count 1.
# On 2026-10-04 two installs (22:01Z and 22:05Z) left 93,015 such files, 8.8 GB, and the
# root disk lost the same amount in one hour. A file with link count 1 is reachable by
# no project on disk, so removing it only costs a re-fetch: this is `pnpm store prune`,
# made age-gated and liveness-guarded.
echo "--- [8] orphaned pnpm store content in ${SCRATCH_ROOT}/.pnpm-store (links==1, unchanged >${PNPM_STORE_IDLE_HOURS}h, no live pnpm) ---"
PNPM_STORE="$SCRATCH_ROOT/.pnpm-store"
if [ -d "$PNPM_STORE" ] && [ ! -L "$PNPM_STORE" ]; then
  cmdline_read=0; pnpm_live=""
  for p in "$PROC_ROOT"/[0-9]*; do
    [ -d "$p" ] || continue
    if cmd_txt=$(tr '\0' '\n' 2>/dev/null < "$p/cmdline") && [ -n "$cmd_txt" ]; then
      cmdline_read=$((cmdline_read + 1))
      # One argv element per line, matched whole: pnpm, pnpm.cjs, pnpx, or a path ending
      # in one. A substring match would see the server's own esbuild under
      # /app/node_modules/.pnpm/ and keep the store forever.
      # Here-string, not a pipe: an early-exiting grep -q can SIGPIPE the writer under
      # pipefail and turn a live pnpm into "none" -- that would fail open.
      if grep -Eq '(^|/)pnp[mx](\.[cm]?js)?$' <<<"$cmd_txt"; then pnpm_live="${p##*/}"; break; fi
    fi
  done
  if [ "$cmdline_read" = 0 ]; then
    echo "  keep all (no process cmdline readable under $PROC_ROOT; liveness unverifiable)"
  elif [ -n "$pnpm_live" ]; then
    echo "  keep all (live pnpm process: pid $pnpm_live)"
  else
    store_find=(find "$PNPM_STORE" -type f -links 1 -mmin +"$((PNPM_STORE_IDLE_HOURS * 60))")
    read -r n b < <("${store_find[@]}" -printf '%s\n' 2>/dev/null | awk '{n++; b+=$1} END{printf "%d %d\n", n, b}')
    n=${n:-0}; b=${b:-0}
    if [ "$APPLY" = 1 ]; then "${store_find[@]}" -delete 2>/dev/null; fi
    reclaimed_bytes=$((reclaimed_bytes + b))
    echo "  $([ "$APPLY" = 1 ] && echo DELETED || echo WOULD-RM) $(mb "$b")  ($n unlinked store files)"
  fi
fi

# ---- class 9: /tmp survey by owner class and age (case_0ddff3ec) ---------------------
# REPORT ONLY: nothing here is ever deleted, with or without --apply. Agents create
# the unprefixed entries (PR checkouts, scratch clones, test fixtures) and nothing
# records who owns one, so deletion is a human call. The table says where the bytes
# are; the candidate list names unreferenced entries older than ${SURVEY_CANDIDATE_HOURS}h.
echo "--- [9] ${SCRATCH_ROOT} survey: owner class x age (report-only, never deleted) ---"
if [ -d "$SCRATCH_ROOT" ]; then
  survey_out=$(python3 - "$SCRATCH_ROOT" "$SURVEY_CANDIDATE_HOURS" <<'PY'
import os, re, subprocess, sys, time
root, cand_h = sys.argv[1], float(sys.argv[2])
now = time.time()
# du -x stays on this filesystem; entries vanish mid-walk (a run ends), so its exit
# status is not an error here -- the sizes it did print are still the survey.
du = subprocess.run(["du", "-x", "-d1", "-k", root], capture_output=True, text=True).stdout
size = {}
for line in du.splitlines():
    k, _, path = line.partition("\t")
    if k.isdigit() and path != root:
        size[os.path.basename(path)] = int(k) * 1024
def klass(n):
    if n.startswith("paperclip-run-"): return "paperclip-run-* (run scratch)"
    if n == ".pnpm-store": return ".pnpm-store (pnpm content)"
    if n == "paperclip-github-runtime": return "paperclip-github-runtime"
    if re.match(r"^claude-\d+$", n): return "claude-<uid>"
    return "other (agent-made, unprefixed)"
rows, other = {}, []
for n in os.listdir(root):
    try: st = os.lstat(os.path.join(root, n))
    except OSError: continue
    age_h = (now - st.st_mtime) / 3600
    sz = size.get(n, st.st_size)
    r = rows.setdefault(klass(n), [0, 0, 0, 0, 0])
    r[0] += 1; r[1] += sz
    r[2 if age_h < 6 else 3 if age_h < cand_h else 4] += sz
    # names are agent-made and untrusted: printable ASCII only, bounded, before they reach a log
    if klass(n).startswith("other"): other.append((sz, age_h, re.sub(r"[^\x20-\x7e]", "?", n)[:80]))
mb = lambda b: "%.0f" % (b / 1048576)
print("  %-34s %6s %9s %8s %9s %8s" % ("class", "n", "MB", "<6h MB", "6-%dh MB" % cand_h, ">%dh MB" % cand_h))
for k, r in sorted(rows.items(), key=lambda kv: -kv[1][1]):
    print("  %-34s %6d %9s %8s %9s %8s" % (k, r[0], mb(r[1]), mb(r[2]), mb(r[3]), mb(r[4])))
print("  largest unprefixed entries:")
for sz, age_h, n in sorted(other, reverse=True)[:8]:
    print("    %8s MB  %5.1fh  %s" % (mb(sz), age_h, n))
print("CANDIDATES")
for sz, age_h, n in sorted(other, reverse=True):
    if age_h >= cand_h: print("%d\t%.1f\t%s" % (sz, age_h, n))
PY
  ) || survey_out="  survey failed (python3 or du unavailable)"
  printf '%s\n' "${survey_out%%CANDIDATES*}"
  echo "  candidates (>${SURVEY_CANDIDATE_HOURS}h, no live reference; REPORT ONLY, kept):"
  cand_n=0
  while IFS=$'\t' read -r csz cage cname; do
    [ -n "$cname" ] || continue
    # fixed-string, prefix-wide match: over-matching only keeps an entry, never adds one
    grep -Fq -- "/${cname}" <<<"${live_refs:-}" && continue
    printf '    CANDIDATE %8s MB  %5.1fh  %s\n' "$(awk -v b="$csz" 'BEGIN{printf "%.1f", b/1048576}')" "$cage" "$cname"
    cand_n=$((cand_n + 1))
  done < <(sed -n '/^CANDIDATES$/,$p' <<<"$survey_out" | sed 1d)
  [ "$cand_n" != 0 ] || echo "    (none)"
  [ "${environ_read:-0}" != 0 ] || echo "    note: no process environ readable; liveness unverifiable, a candidate may be in use"
fi

echo "=== $([ "$APPLY" = 1 ] && echo reclaimed || echo would reclaim): $(gb "$reclaimed_bytes") ==="
# the receipt names the filesystem the sweep worked on: --tmp-only reclaims from
# SCRATCH_ROOT (the root overlay), the full sweep from PAPERCLIP_HOME
if [ "$TMP_ONLY" = 1 ]; then df -h "$SCRATCH_ROOT" 2>/dev/null | tail -1
else df -h "$PAPERCLIP_HOME" 2>/dev/null | tail -1; fi
