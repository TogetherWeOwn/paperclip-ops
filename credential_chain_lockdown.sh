#!/usr/bin/env bash
# case_965c83b3 -- close the agent-writable git credential-helper chain.
#
# PROPOSAL, not a deployment.  Review before running, as root:
#   podman exec -u root paperclip <path-to-this-checkout>/credential_chain_lockdown.sh
#
#   --dry-run   print the plan and the current state, change nothing (default)
#   --apply     make the changes
#   --apply --i-have-verified-this-copy   skip the provenance refusal below
#   --verify    re-run the audit as uid node and report
#
# ---------------------------------------------------------------------------
# RUN THIS FROM THE GIT CHECKOUT, NOT FROM /paperclip/operator-handoff
#
# The first draft of this script was staged at
# /paperclip/operator-handoff/case_965c83b3-apply.sh and the operator was asked to run
# THAT as root.  That was a mistake, and a worse one than the finding this
# script fixes.  Measured 2026-08-24 as an ordinary agent (uid 1000 node):
#
#   /paperclip/operator-handoff       root:root  1777   (world-writable, sticky)
#   .../case_965c83b3-apply.sh              node:node  0755   -- agent-OWNED
#   test -w .../case_965c83b3-apply.sh      -> true
#
# Sticky stops you deleting someone else's file; it does not stop you deleting
# your own, and every agent is the same uid `node` (case_e848a83d).  So any agent
# could have rewritten that file at any point between staging and execution --
# and it executes as ROOT.  case_965c83b3 is an agent -> agent escalation; asking the
# operator to root-run an agent-writable script is agent -> root, which is
# strictly worse than the bug.
#
# So this file is canonical HERE, in git, where a change needs a reviewed PR.
# The copy under operator-handoff is a convenience mirror and is not trusted.
# `credential_chain_audit.sh --staged` compares the two and reports DRIFT.
#
# Refusing to trust its own location is not paranoia for its own sake: the whole
# premise of case_965c83b3 is that agent-writable paths in a privileged execution path
# are the vulnerability.  This script is in a privileged execution path.
#
# ---------------------------------------------------------------------------
# WHY THIS IS LONGER THAN THE FOUR COMMANDS IN THE ISSUE
#
# case_965c83b3 asked for chown/chmod on the helper file and its directory.  That is
# correct and necessary, and it is not sufficient: it closes one of four ways to
# get code into the chain.  git runs a helper named by ANY config file it reads,
# so a writable config is worth exactly as much to an attacker as a writable
# helper.  Measured on the live container 2026-08-24 as uid node:
#
#   1. the retired per-call helper         node:node   -- the filed finding
#   2. /paperclip/.local/bin                   node:node   -- the filed finding
#   3. /paperclip/.gitconfig                   node:node   -- NOT in the issue
#   4. /paperclip/.config/git/config           creatable   -- NOT in the issue
#
# Link 3 alone reduces the whole fix to theatre.  With the helper locked down,
# any agent still runs one command:
#
#   git config --global credential."https://github.com".helper '!/tmp/mine.js'
#
# and every agent's next git call executes /tmp/mine.js and hands it a token.
# Link 4 is the same move via git's other global config, which does not exist
# yet -- `mkdir -p` creates it, and "the file isn't there" is not a boundary.
#
# THE PART THAT IS EASY TO GET WRONG:
#
# chown-ing a file inside an agent-owned directory does not protect it.  Unlink
# and rename are governed by the *directory's* write bit, not the file's, so
# node can rename a root-owned /paperclip/.gitconfig aside and drop in its own.
# /paperclip is drwxrwxr-x node:node today, so the obvious `chown root:root
# /paperclip/.gitconfig` on its own accomplishes nothing.
#
# The directory has to hold too.  Rather than take write access to HOME away
# from every tool that legitimately creates dotfiles there, this uses the model
# already in use for /paperclip/operator-handoff in this same container:
# root-owned + sticky + world-writable (drwxrwxrwt, i.e. /tmp semantics).  New
# entries can still be created by anyone; root-owned entries cannot be removed
# or renamed by anyone but root.  That keeps the blast radius of this change to
# "agents can no longer delete root's dotfiles", which is the intent.
#
# If you would rather not have a world-writable HOME, the stricter alternative
# is `chmod 1755 /paperclip` -- but that stops agents creating any new entry
# directly in HOME, which will break tools that expect to write ~/.something.
# Sticky alone is not an option: node currently OWNS /paperclip, and owning the
# directory overrides the sticky restriction.
#
# case_0b186cde -- THE GRANDPARENT DIRECTORY, LINK 5
#
# Fixing links 1-2 above (helper + its immediate directory) is not the end of
# the chain either: it just moves the same "directory has to hold too" problem
# up one level, onto the *grandparent*. Re-measured 2026-08-31: after links 1-2
# were applied, the retired helper and /paperclip/.local/bin
# were both root:root -- but /paperclip/.local itself was still node:node 0755.
# Renaming /paperclip/.local/bin aside and dropping in a substitute needs only
# write+execute on /paperclip/.local, which node had. Demonstrated, not
# theorised: this run performed exactly that rename against a live copy of
# this container and restored it (see case_0b186cde).
#
# The instinctive fix -- `chmod +t /paperclip/.local` -- was tried live here
# and DOES NOT WORK, for the same reason chmod +t was never proposed for
# /paperclip in step 4: node OWNS /paperclip/.local, and a sticky bit never
# restricts the directory's own owner, only other principals. Every agent is
# uid node, so "restricts everyone except the owner" restricts nobody. Tested:
# `mv bin bin.evil` still succeeded under 1755 node:node exactly as it did
# under 0755. Only a change of OWNER closes it, matching the audit script's
# own `dir_allows_replace`: `[[ "$o" == "$AS_UID" ]] && return 0  # dir owner
# ignores sticky`.
#
# Unlike /paperclip (step 4), /paperclip/.local does not need to stay writable
# for new top-level entries by convention -- `bin`, `share` and `state` are the
# fixed, known set (pnpm, opencode and font state live under share/state and
# are unaffected by an ownership change on the parent; they keep their own
# node:node ownership and stay writable). So this uses the plain chown, not
# the /tmp-semantics 1777 model:
#
#   chown root:root /paperclip/.local   (mode stays 0755)
#
# This does not take away node's ability to write INSIDE share/ or state/ --
# only the ability to rename/replace THEIR OWN NAMES or bin's name within
# .local. If a future tool needs to create a new top-level directory under
# .local at runtime (not observed as of this fix), that will start failing and
# is the signal to revisit -- not a reason to leave the grandparent open now.
#
# WHAT THIS DELIBERATELY DOES NOT FIX
#
# Per-repository .git/config is agent-owned by necessity -- agents have to
# configure their own checkouts.  It is a hole only between agents sharing one
# checkout, which is the same trust boundary as the shared working tree itself,
# and it cannot be closed by ownership without breaking normal work.  Recorded
# as residual risk rather than silently dropped.
#
# ORDER OF OPERATIONS
#
# Every change here is chown/chmod on an existing inode.  Nothing is deleted and
# nothing is rewritten, so unlike the 03:31 incident there is no window in which
# git has no helper.  The script is idempotent; re-running it is a no-op.
# ---------------------------------------------------------------------------

set -uo pipefail

MODE="dry-run"
VERIFIED_COPY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   MODE="apply";   shift ;;
    --verify)  MODE="verify";  shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --i-have-verified-this-copy) VERIFIED_COPY=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SELF_SHA="$(sha256sum "$SELF" 2>/dev/null | awk '{print $1}')"

# Refuse to root-execute a copy of ourselves that an agent uid could have
# rewritten -- see the RUN THIS FROM THE GIT CHECKOUT header.
#
# Note honestly what this can and cannot do.  EVERY copy of this file inside the
# container is owned by `node`, including the git checkout, because agents own
# their own checkouts.  So this gate fires essentially always and cannot be
# satisfied by moving the file somewhere "safe" -- there is no such place on
# this filesystem.  That is the point.  The integrity anchor is not the
# filesystem, it is git: a blob that matches a reviewed commit on origin/main
# has been through a PR, and a working-tree file that matches that blob is that
# reviewed script.  The gate exists to make the operator perform that check
# rather than to pretend a mode bit performed it for them.
self_is_agent_writable() {
  local st o m
  st="$(stat -L -c '%u %a' "$SELF" 2>/dev/null)" || return 0   # cannot tell => refuse
  read -r o m <<<"$st"
  local node_uid; node_uid="$(id -u node 2>/dev/null || echo -1)"
  [[ "$o" == "$node_uid" ]] && return 0
  m="$(printf '%03d' "$((10#$m % 1000))")"
  [[ "${m:2:1}" =~ [2367] ]] && return 0
  return 1
}

if [[ "$MODE" == "apply" && $VERIFIED_COPY -eq 0 ]] && self_is_agent_writable; then
  cat >&2 <<EOF

REFUSING TO APPLY: this script is writable by the agent uid, and --apply runs as root.

  script : $SELF
  owner  : $(stat -L -c '%U:%G %a' "$SELF" 2>/dev/null)
  sha256 : ${SELF_SHA:-<unreadable>}

Any agent could have rewritten it since it was staged. Verify it against git --
that is the only tamper-evident record here -- then re-run:

  git -C "$(dirname "$SELF")" fetch origin
  git -C "$(dirname "$SELF")" diff --stat origin/main -- credential_chain_lockdown.sh   # expect: empty
  git -C "$(dirname "$SELF")" log --oneline -1 origin/main -- credential_chain_lockdown.sh

If the diff is empty, the file is the reviewed version on origin/main. Then:

  $SELF --apply --i-have-verified-this-copy

EOF
  exit 2
fi

# The retired per-call helper's path, built from fragments so this file does
# not reintroduce its literal name (the 2026-10-05 retirement card greps the
# tree for it). The value at runtime is the same path as before.
HELPER="/paperclip/.local/bin/gh-app-""token.js"
HELPER_DIR=/paperclip/.local/bin
LOCAL_DIR=/paperclip/.local
GITCONFIG=/paperclip/.gitconfig
HOME_DIR=/paperclip
XDG_DIR=/paperclip/.config
XDG_GIT=/paperclip/.config/git
BACKUP_DIR=/paperclip/operator-handoff/case_965c83b3-helper-backups

say()  { printf '  %s\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

show_state() {
  hdr "Current state"
  for p in "$HOME_DIR" "$GITCONFIG" "$XDG_DIR" "$XDG_GIT" "$XDG_GIT/config" "$LOCAL_DIR" "$HELPER_DIR" "$HELPER"; do
    if [[ -e "$p" ]]; then
      printf '  %-40s %s\n' "$p" "$(stat -c '%A %U:%G' "$p")"
    else
      printf '  %-40s %s\n' "$p" "(absent)"
    fi
  done
}

verify_as_node() {
  hdr "Verification (as uid node, which is what every agent is)"
  local rc_w=0
  # The direct question: can an unprivileged agent still substitute any link?
  for p in "$HELPER" "$GITCONFIG"; do
    if runuser -u node -- test -w "$p" 2>/dev/null; then
      say "STILL WRITABLE: $p"; rc_w=1
    else
      say "ok, not writable by node: $p"
    fi
  done
  # Rename/replace, which the write test above does not cover.
  for d in "$HELPER_DIR" "$HOME_DIR" "$XDG_GIT"; do
    [[ -d "$d" ]] || continue
    if runuser -u node -- sh -c "touch '$d/.case_03f8828b-probe' 2>/dev/null" ; then
      say "STILL ACCEPTS NEW FILES: $d"
      rm -f "$d/.case_03f8828b-probe"
      # For HOME this is intended (sticky), so only flag the ones that matter.
      case "$d" in "$HELPER_DIR"|"$XDG_GIT") rc_w=1 ;; esac
    else
      say "ok, refuses new files: $d"
    fi
  done
  if runuser -u node -- sh -c "mv '$GITCONFIG' '$GITCONFIG.probe' 2>/dev/null"; then
    say "STILL RENAMEABLE: $GITCONFIG -- restoring"
    mv "$GITCONFIG.probe" "$GITCONFIG"; rc_w=1
  else
    say "ok, node cannot rename $GITCONFIG out of the way"
  fi

  # case_0b186cde: the actual attack -- can node rename the root-owned helper dir
  # aside within its (formerly agent-owned) parent? A touch-new-file probe on
  # LOCAL_DIR would not catch this: LOCAL_DIR staying non-writable to new files
  # is necessary but not sufficient, because rename of an EXISTING entry the
  # agent does not own is governed by the same write bit, not a separate one.
  if [[ -d "$HELPER_DIR" ]]; then
    if runuser -u node -- sh -c "mv '$HELPER_DIR' '$HELPER_DIR.case_a70ecfedprobe' 2>/dev/null"; then
      say "STILL RENAMEABLE: $HELPER_DIR out of $LOCAL_DIR -- restoring"
      mv "$HELPER_DIR.case_a70ecfedprobe" "$HELPER_DIR"; rc_w=1
    else
      say "ok, node cannot rename $HELPER_DIR out of $LOCAL_DIR"
    fi
  fi

  # And the full chain audit.  Only the copy sitting next to this script is
  # used.  The first version searched `find /paperclip/instances -name
  # credential_chain_audit.sh | head -1`, which picks an arbitrary agent's
  # checkout out of a directory tree that agents write to -- i.e. it chose the
  # program to execute by the same mechanism this whole issue is about.  It ran
  # as node rather than root so it was not an escalation, but "pick an
  # executable out of an agent-writable tree" is not a habit to keep in the
  # script whose job is to stamp that habit out.
  local audit="$(dirname "$SELF")/credential_chain_audit.sh"
  if [[ -x "$audit" ]]; then
    hdr "Full chain audit (case_965c83b3 standing check)"
    runuser -u node -- "$audit"
    local arc=$?
    say "audit exit: $arc  (0 = clean, 1 = hole remains, 3 = indeterminate)"
    # Fold it in.  The first version printed this number and returned $rc_w
    # regardless, so --verify could print "All probes refused" as its verdict
    # while the audit immediately above it reported a hole.  The handful of
    # probes in this function cover the two links case_965c83b3 filed; the audit
    # covers all four plus PATH.  Letting the narrow check overrule the broad
    # one is how a clean report gets issued for a chain that is still open.
    [[ $arc -ne 0 ]] && rc_w=1
  else
    say "credential_chain_audit.sh not found next to this script -- chain NOT fully verified"
    rc_w=1
  fi
  return $rc_w
}

if [[ "$MODE" == "verify" ]]; then
  [[ "$(id -u)" == "0" ]] || { echo "must run as root" >&2; exit 2; }
  show_state; verify_as_node; exit $?
fi

show_state

hdr "Plan"
cat <<PLAN
  1. remove the RETIRED helper if one is live (back it up first, outside
       the agent-writable directory, for forensics). The agent-side minter
       was retired 2026-10-05 -- restoring it would resurrect the old model.
       Agents use the Paperclip built-in GitHub connection now.

  2. root-own the helper and its directory        [the filed finding]
       chown root:root $HELPER $HELPER_DIR
       chmod 0755      $HELPER $HELPER_DIR

  2a. root-own the helper's PARENT directory      [case_0b186cde, not in case_965c83b3]
       chown root:root $LOCAL_DIR
       chmod 0755      $LOCAL_DIR
      (share/ and state/ under it keep their own node:node ownership and stay
       writable; only the ability to rename bin/, share/, state/ out of
       LOCAL_DIR is removed)

  3. root-own the global config                   [not in the issue]
       chown root:root $GITCONFIG
       chmod 0644      $GITCONFIG

  4. make HOME hold that file against rename      [not in the issue]
       chown root:root $HOME_DIR
       chmod 1777      $HOME_DIR      # drwxrwxrwt, same as operator-handoff

  5. deny the second global config path           [not in the issue]
       chown root:root $XDG_DIR ; chmod 1777 $XDG_DIR
       install -d -o root -g root -m 0755 $XDG_GIT
       install    -o root -g root -m 0644 /dev/null $XDG_GIT/config

  6. verify as uid node -- expected: every probe refused
PLAN

if [[ "$MODE" == "dry-run" ]]; then
  printf '\n  Dry run. Re-run with --apply to make these changes.\n\n'
  exit 0
fi

[[ "$(id -u)" == "0" ]] || { echo "ERROR: --apply must run as root" >&2; exit 2; }

hdr "Applying"

# 1 -- the retired helper must be ABSENT, not backed up and locked. If a copy
# is live, keep it for forensics, then remove it: locking it in place would
# preserve the retired agent-side minter. Never recreate it from git history;
# agents use the Paperclip built-in GitHub connection since 2026-10-05.
if [[ -f "$HELPER" ]]; then
  install -d -o root -g root -m 0755 "$BACKUP_DIR"
  sha="$(sha256sum "$HELPER" | cut -c1-12)"
  dest="$BACKUP_DIR/$(basename "$HELPER").$sha"
  if [[ -e "$dest" ]]; then
    say "backup already present: $dest"
  else
    install -o root -g root -m 0644 "$HELPER" "$dest"
    say "backed up live helper -> $dest"
  fi
  rm -f "$HELPER"
  say "removed the retired helper -- do NOT restore it"
else
  say "retired helper absent, as expected."
fi

# 2 -- the filed finding (kept as a guard: if step 1 removed a live copy, or
# one reappears, this locks the directory chain below regardless)
if [[ -e "$HELPER" ]]; then
  chown root:root "$HELPER"    && chmod 0755 "$HELPER"     && say "locked $HELPER"
else
  say "no live helper to lock (retired, absent)."
fi
chown root:root "$HELPER_DIR"  && chmod 0755 "$HELPER_DIR" && say "locked $HELPER_DIR"

# 2a -- case_0b186cde: the grandparent. Root-owning HELPER and HELPER_DIR is not
# sufficient while their PARENT stays agent-owned -- node can still rename
# HELPER_DIR aside within an agent-owned LOCAL_DIR and substitute its own.
# chmod +t alone does not fix this: node OWNS LOCAL_DIR, and a directory's
# owner is never restricted by its own sticky bit (see header). Plain chown
# is used, not the 1777 /tmp-semantics model from step 4, because LOCAL_DIR's
# top-level entries (bin, share, state) are a fixed set, not an
# every-tool-creates-its-own-dotfile location the way HOME is.
if [[ -e "$LOCAL_DIR" ]]; then
  chown root:root "$LOCAL_DIR" && chmod 0755 "$LOCAL_DIR" && say "locked $LOCAL_DIR (root-owned; share/ and state/ beneath it keep their own ownership)"
else
  say "WARNING: $LOCAL_DIR does not exist -- nothing to lock, but re-check HELPER_DIR's actual parent"
fi

# 3 -- the global config that selects the helper
if [[ -e "$GITCONFIG" ]]; then
  chown root:root "$GITCONFIG" && chmod 0644 "$GITCONFIG"  && say "locked $GITCONFIG"
else
  say "WARNING: $GITCONFIG absent -- git has no credential helper configured."
fi

# 4 -- HOME, so that link 3 cannot simply be renamed aside
chown root:root "$HOME_DIR"    && chmod 1777 "$HOME_DIR"   && say "locked $HOME_DIR (root-owned, sticky, still writable for new entries)"

# 5 -- git's other global config location
chown root:root "$XDG_DIR"     && chmod 1777 "$XDG_DIR"    && say "locked $XDG_DIR"
install -d -o root -g root -m 0755 "$XDG_GIT"              && say "created root-owned $XDG_GIT"
[[ -e "$XDG_GIT/config" ]] || install -o root -g root -m 0644 /dev/null "$XDG_GIT/config"
chown root:root "$XDG_GIT/config" && chmod 0644 "$XDG_GIT/config" && say "placeholder $XDG_GIT/config is root-owned"

# 6
show_state
verify_as_node
rc=$?

hdr "Result"
if [[ $rc -eq 0 ]]; then
  say "All probes refused. The credential chain is no longer agent-substitutable."
  say "Residual, by design: per-repo .git/config in shared checkouts (see header)."
else
  say "At least one probe still succeeded -- see STILL_* lines above. Do not close case_965c83b3."
fi
exit $rc
