#!/usr/bin/env bash
# pre-review.sh — deterministic author self-check for a pull-request diff.
#
# Runs the mechanical half of a code review locally, on the diff only, and
# prints a pass/fail table. No network, no credentials, no model calls:
# bash and git only. The judgment half (does the change do the right thing?)
# stays with the author plus the rubric in RUBRIC.md.
#
# Usage:
#   pre-review.sh [--base <sha>] [--pr-title <title>] [--pr-body <file>] [--public]
#
#   --base      diff base (default: merge-base with origin/main, else HEAD~1)
#   --pr-title  check the PR title against the Conventional Commits header rule
#   --pr-body   check every markdown section of the PR body file is non-empty
#   --public    also fail on private-tracker patterns from
#               PRE_REVIEW_PRIVATE_PATTERNS (space-separated EREs, e.g. the
#               org's internal tracker prefixes); off when unset
#
# Exit status: 1 when any FAIL row is present, 0 otherwise. WARN rows are
# advisory: fix them or justify them in the PR, but they never block.
#
# Destination: org template repo, pre-review/pre-review.sh (staged source).
# Keep this file free of internal tracker IDs, private hosts and agent names.

# Strict mode: this script passes its own bucket-6 check. Non-final &&/||
# members, if-test conditions and while-read conditions are exempt from -e,
# so findings collection is unaffected; the few top-level statements whose
# nonzero status would abort the table carry explicit `|| true`.
set -euo pipefail

BASE="" PR_TITLE="" PR_BODY="" PUBLIC=0
flag_value() { # $1: flag name; $2: maybe-value. Missing values must fail as a
  # usage error (exit 2), never as a silent table-less abort that authors
  # misread as findings.
  if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == -* ]]; then
    echo "missing value for $1 (see --help)" >&2; exit 2
  fi
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) flag_value "$1" "${2:-}"; BASE="$2"; shift 2 ;;
    --pr-title) flag_value "$1" "${2:-}"; PR_TITLE="$2"; shift 2 ;;
    --pr-body) flag_value "$1" "${2:-}"; PR_BODY="$2"; shift 2 ;;
    --public) PUBLIC=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
done

FAILS=0; WARNS=0
row() { printf '%-4s  %-15s  %s\n' "$1" "$2" "$3"; }
pass() { row PASS "$1" "$2"; }
warn() { row WARN "$1" "$2"; WARNS=$((WARNS+1)); }
fail() { row FAIL "$1" "$2"; FAILS=$((FAILS+1)); }
skip() { row SKIP "$1" "$2"; }

REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO"

if [[ -z "$BASE" ]]; then
  if git rev-parse --verify -q origin/main >/dev/null 2>&1; then
    BASE="$(git merge-base origin/main HEAD 2>/dev/null || true)"
  fi
  [[ -z "$BASE" ]] && BASE="$(git rev-parse 'HEAD~1' 2>/dev/null || true)"
  [[ -z "$BASE" ]] && BASE="HEAD"
fi
BASE="$(git rev-parse "$BASE" 2>/dev/null || echo "")"
[[ -z "$BASE" ]] && { echo "cannot resolve diff base" >&2; exit 2; }

mapfile -t CHANGED < <(git diff --name-only -z "$BASE"...HEAD -- 2>/dev/null | tr '\0' '\n' | grep -v '^$' || true)
mapfile -t ADDED_FILES < <(git diff --name-status "$BASE"...HEAD -- 2>/dev/null | awk '$1=="A"{print $2}' || true)
ADDED_LINES="$(git diff -U0 "$BASE"...HEAD -- 2>/dev/null | grep -E '^\+' | grep -vE '^\+\+\+' || true)"
# Untracked files are invisible to git diff but fully part of the change:
# review their whole content as added lines (new files count as Added).
UNTRACKED=()
while IFS= read -r u; do
  [[ -n "$u" ]] || continue
  CHANGED+=("$u")
  ADDED_FILES+=("$u")
  UNTRACKED+=("$u")
  if [[ -f "$u" ]] && grep -Iq . "$u" 2>/dev/null; then
    ADDED_LINES+=$'\n'"$(sed 's/^/+/' "$u" || true)"
  fi
done < <(git ls-files --others --exclude-standard -z 2>/dev/null | tr '\0' '\n' || true)

printf '%-4s  %-15s  %s\n' "STAT" "BUCKET" "DETAIL"
echo "---  ---------------  ------------------------------------------------"

# --- helpers ---------------------------------------------------------------
is_code() { [[ "$1" =~ \.(py|js|mjs|cjs|ts|tsx|jsx|go|rs|sh|bash|java|rb|php|pl|c|cc|cpp|h|hpp|cs|swift|kt|sql)$ ]]; }
is_test() { # basename- or directory-anchored: contest.py and latest/ are NOT tests.
  local b="${1##*/}"
  [[ "$b" == test_* || "$b" == *_test.* || "$b" == *.test.* || "$b" == *_spec.* || "$b" == *.spec.* ]] && return 0
  [[ "$1" == */tests/* || "$1" == */test/* || "$1" == */__tests__/* || "$1" == */spec/* ]] && return 0
  return 1
}
is_doc()  { [[ "$1" == *.md ]] || [[ "$1" == docs/* ]] || [[ "$1" == ./*.md ]]; }

# --- 1. untested-branch ------------------------------------------------------
CODE=(); TESTS=()
for f in "${CHANGED[@]}"; do
  if is_code "$f" && ! is_test "$f" && [[ "$f" != .github/* ]]; then CODE+=("$f"); fi
  if is_test "$f"; then TESTS+=("$f"); fi
done
if [[ "${#CODE[@]}" -eq 0 ]]; then
  skip "untested-branch" "no source files changed"
elif [[ "${#TESTS[@]}" -eq 0 ]]; then
  fail "untested-branch" "${#CODE[@]} source file(s) changed, no test file changed (e.g. ${CODE[0]})"
else
  pass "untested-branch" "${#CODE[@]} source, ${#TESTS[@]} test file(s) changed"
fi

# --- 2. docs-drift -----------------------------------------------------------
code_changed=$([[ "${#CODE[@]}" -gt 0 ]] && echo 1 || echo 0)
DOC_CHANGED=0
for f in "${CHANGED[@]}"; do if is_doc "$f"; then DOC_CHANGED=1; break; fi; done
if [[ "$code_changed" -eq 1 && "$DOC_CHANGED" -eq 0 ]]; then
  warn "docs-drift" "source changed, no doc changed — update docs or justify why not"
else
  pass "docs-drift" "docs updated or no source change"
fi
BROKEN_LINKS=0
for f in "${CHANGED[@]}"; do
  [[ "$f" == *.md ]] || continue
  [[ -f "$f" ]] || continue
  dir="$(dirname "$f")"
  while IFS= read -r link; do
    bare="${link%%[#?]*}"
    [[ "$bare" == "" ]] && continue
    [[ "$bare" =~ ^/ ]] && continue # root-relative URL, not a file
    [[ "$bare" =~ ^[a-zA-Z][a-zA-Z0-9+.-]*: ]] && continue # URI scheme, not a file
    if [[ ! -e "$dir/$bare" && ! -e "$bare" ]]; then
      BROKEN_LINKS=$((BROKEN_LINKS+1))
      [[ "$BROKEN_LINKS" -le 3 ]] && fail "docs-drift" "broken relative link in $f: $link" || true
    fi
  # Balanced to one paren level (plus angle-bracket destinations): a naive
  # [^)]+ cut truncates valid ./api_(v2).md links at the first ')', failing
  # a correct doc. Unbalanced targets match nothing and stay unchecked.
  done < <(grep -oE '\]\(<[^>]+>|\]\(([^()]|\([^()]*\))*\)' "$f" 2>/dev/null | sed -E -e 's/^]\(<//' -e 's/^]\(//' -e 's/>\)$//' -e 's/\)$//' | cut -d' ' -f1 || true)
done
[[ "$BROKEN_LINKS" -gt 3 ]] && fail "docs-drift" "... and $((BROKEN_LINKS-3)) more broken link(s)"

# --- 3. ci-logic -------------------------------------------------------------
WF=()
for f in "${CHANGED[@]}"; do
  [[ "$f" == .github/workflows/*.yml || "$f" == .github/workflows/*.yaml ]] && WF+=("$f")
done
if [[ "${#WF[@]}" -eq 0 ]]; then
  skip "ci-logic" "no workflow files changed"
else
  WF_FAIL=0
  for f in "${WF[@]}"; do
    [[ -f "$f" ]] || continue
    WF_NOCOMMENT="$(grep -vE '^\s*#' "$f" 2>/dev/null || true)"
    grep -qE 'pull_request_target' <<<"$WF_NOCOMMENT" \
      && { fail "ci-logic" "$f uses pull_request_target — justify or use pull_request"; WF_FAIL=1; }
    # Every job block needs timeout-minutes unless the job is a bare
    # reusable-workflow call (a caller-side timeout is rejected there).
    # Scope to the jobs: section so triggers like on.pull_request are skipped.
    # Job indent is derived from the file (two spaces is convention, not law).
    JOBS_SECT="$(awk '/^jobs:/{on=1;next} on && /^[^[:space:]]/{on=0} on{print}' "$f")"
    INDENT="$(grep -oE '^[ ]+[A-Za-z0-9_.-]+:' <<<"$JOBS_SECT" | sed -E 's/^( *)[^ ].*/\1/' | awk '{ print length }' | sort -n | head -n 1 || true)"
    [[ -z "$INDENT" ]] && INDENT=2
    while IFS= read -r job; do
      block="$(awk -v ind="$INDENT" -v j="$job" '
        BEGIN { pad = sprintf("%*s", ind, "") }
        index($0, pad j ":") == 1 { on = 1; next }
        on && $0 ~ ("^" pad "[A-Za-z0-9_.-]+:") { on = 0 }
        on { print }
      ' <<<"$JOBS_SECT")"
      if ! grep -q 'timeout-minutes:' <<<"$block"; then
        if grep -qE '^[[:space:]]+uses:' <<<"$block" && ! grep -qE '^[[:space:]]+(run:|steps:)' <<<"$block"; then
          : # bare reusable call: caller-side timeout is rejected, skip
        else
          warn "ci-logic" "$f job '$job' has no timeout-minutes"; WF_FAIL=2
        fi
      fi
    done < <(grep -oE "^ {$INDENT}[A-Za-z0-9_.-]+:" <<<"$JOBS_SECT" | sed -E 's/^ +//;s/:$//' | sort -u || true)
  done
  [[ "$WF_FAIL" -eq 0 ]] && pass "ci-logic" "${#WF[@]} workflow file(s) parsed, no target/timeout findings"
fi

# --- 4. generated-drift ------------------------------------------------------
GEN_FAIL=0
# Workspace-aware pairs: a manifest change needs its lockfile regenerated
# beside the manifest or at any ancestor up to the repo root (npm/pnpm
# workspace roots own the single lockfile for nested manifests). A lockfile
# whose manifest is live but untouched is a lock-only update (audit-fix,
# version bump), not drift: only a lockfile with no manifest of its kind at
# its side, above, or anywhere else in the tree fails, as dangling.
gen_pair() { # $1: manifest basename; $2...: lockfile basenames.
  local m="$1"; shift
  local f d h g key hit up
  for f in "${CHANGED[@]}"; do
    [[ "$(basename "$f")" == "$m" ]] || continue
    d="$(dirname "$f")"; hit=0; up="$d"
    while true; do
      for g in "$@"; do
        key="$up/$g"; key="${key#./}"
        for h in "${CHANGED[@]}"; do [[ "$h" == "$key" ]] && { hit=1; break 3; }; done
      done
      [[ "$up" == "." ]] && break
      up="$(dirname "$up")"
    done
    [[ "$hit" -eq 1 ]] || { fail "generated-drift" "$f changed without its lockfile in $d or above"; GEN_FAIL=1; }
  done
  for h in "${CHANGED[@]}"; do
    for g in "$@"; do
      [[ "$(basename "$h")" == "$g" ]] || continue
      d="$(dirname "$h")"; key="$d/$m"; key="${key#./}"; hit=0
      for f in "${CHANGED[@]}"; do [[ "$f" == "$key" ]] && { hit=1; break; }; done
      [[ "$hit" -eq 1 ]] && continue
      up="$d"; hit=0
      while true; do
        [[ -e "$up/$m" ]] && { hit=1; break; }
        [[ "$up" == "." ]] && break
        up="$(dirname "$up")"
      done
      [[ "$hit" -eq 1 ]] && continue
      # Workspace-owned: the owning manifest may live in a nested package
      # while the single lockfile sits at the root (or vice versa). Only a
      # lockfile with no manifest of its kind anywhere in the tree fails.
      TRACKED="$(git ls-files --cached --others --exclude-standard 2>/dev/null || true)"
      if grep -qE "(^|/)$m$" <<<"$TRACKED"; then
        continue
      fi
      fail "generated-drift" "$h changed without its manifest in $d or above"
    done
  done
}
gen_pair 'package.json' 'package-lock.json' 'npm-shrinkwrap.json' 'pnpm-lock.yaml' 'yarn.lock'
gen_pair 'go.mod' 'go.sum'
gen_pair 'Cargo.toml' 'Cargo.lock'
gen_pair 'Gemfile' 'Gemfile.lock'
gen_pair 'composer.json' 'composer.lock'
PROTO=0; PBGO=0
for f in "${CHANGED[@]}"; do
  [[ "$f" == *.proto ]] && PROTO=1
  [[ "$f" == *.pb.go ]] && PBGO=1
done
[[ "$PROTO" -eq 1 && "$PBGO" -eq 0 ]] && { fail "generated-drift" ".proto changed, no .pb.go regenerated"; GEN_FAIL=1; }
[[ "$PROTO" -eq 0 && "$PBGO" -eq 1 ]] && { warn "generated-drift" ".pb.go changed with no .proto change — hand-edit?"; }
for f in "${CHANGED[@]}"; do
  if [[ "$f" == dist/* || "$f" == build/* || "$f" == *.min.js || "$f" == *.bundle.js ]]; then
    if [[ "${#CODE[@]}" -eq 0 ]]; then
      warn "generated-drift" "$f changed with no source change — hand-edited output?"
    fi
    break
  fi
done
[[ "$GEN_FAIL" -eq 0 ]] && pass "generated-drift" "lockfile/generated pairs consistent"

# --- 5. conflicts-stale ------------------------------------------------------
STRIPPED="$(sed 's/^+//' <<<"$ADDED_LINES")"
if grep -qE '^(<{7}|={7}|\|{7}|>{7})' <<<"$STRIPPED"; then
  fail "conflicts-stale" "conflict markers in added lines"
else
  pass "conflicts-stale" "no conflict markers"
fi
if git rev-parse --verify -q origin/main >/dev/null 2>&1; then
  BEHIND="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
  [[ "$BEHIND" -gt 0 ]] && warn "conflicts-stale" "$BEHIND commit(s) on main since this branch forked — merge/rebase first"
else
  skip "conflicts-stale" "origin/main unavailable, staleness unchecked"
fi
[[ -n "$(git status --porcelain 2>/dev/null || true)" ]] && warn "conflicts-stale" "uncommitted changes present — not part of the reviewed diff"

# --- 6. unhandled-errors ------------------------------------------------------
ERR_FAIL=0
has_set_e() { # -e must sit inside a dash cluster: `set -uo pipefail` has
  # none even though the word pipefail contains one. Long form accepted too.
  grep -qE '^\s*set\b[^#]*\s-[a-zA-Z]*e[a-zA-Z]*(\s|$|;)' "$1" \
    || grep -qE '^\s*set\b[^#]*\s-o\s+errexit\b' "$1"
}
for f in "${CHANGED[@]}"; do
  [[ "$f" == *.sh || "$f" == *.bash ]] || continue
  [[ -f "$f" ]] || continue
  has_set_e "$f" \
    || { fail "unhandled-errors" "$f has no 'set -e' guard"; ERR_FAIL=1; }
done
[[ "$ERR_FAIL" -eq 0 ]] && pass "unhandled-errors" "shell guards present"
echo "$ADDED_LINES" | grep -E '^\+\s*except\s*:' >/dev/null \
  && warn "unhandled-errors" "bare 'except:' in added Python — catch a specific error"
echo "$ADDED_LINES" | grep -E 'catch\s*(\([^)]*\))?\s*\{\s*\}' >/dev/null \
  && warn "unhandled-errors" "empty catch block in added JS/TS — handle or explain"

# --- 7. authz ----------------------------------------------------------------
# Quoted values need a 12+ non-space, non-quote run; bare values must be
# a full 12+ run ending at a value boundary (space, comma, semicolon,
# quote, comment hash, or end of line) — parens and dots after the run
# mark calls or attribute access (e.g. match.group(1)), not a credential.
# Angle brackets also end the run, so generic type shapes such as
# Option<String> read as declarations, not literals.
SECRET_RE='(BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[bap]-[A-Za-z0-9-]{8,}|api[_-]?key["'"'"' ]*[:=]["'"'"' ]+[A-Za-z0-9_.-]{12,}|(token|secret|password)["'"'"' ]*[:=](["'"'"' ]*["'"'"'][^"'"'"' ]{12,}|["'"'"' ]+[^"'"'"' ().<>]{12,}(["'"'"' ,;#<>]|$)))'

if grep -Eq "$SECRET_RE" <<<"$ADDED_LINES"; then
  fail "authz" "possible secret in added lines — move to a credential store"
else
  pass "authz" "no secret patterns in added lines"
fi
PATTERNS="${PRE_REVIEW_PRIVATE_PATTERNS:-}"
if [[ -n "$PATTERNS" ]]; then
  for pat in $PATTERNS; do
    if grep -Eq "$pat" <<<"$ADDED_LINES"; then
      fail "authz" "private-tracker pattern '$pat' in added lines"
    fi
  done
elif [[ "$PUBLIC" -eq 1 ]]; then
  skip "authz" "no patterns configured (set PRE_REVIEW_PRIVATE_PATTERNS)"
else
  skip "authz" "private-pattern scan off (use --public)"
fi
# Workflow-only checks run on workflow added lines: the warn texts themselves
# contain these patterns, so a global scan would flag this script too.
WF_ADDED="$(git diff -U0 "$BASE"...HEAD -- '.github/workflows/*.yml' '.github/workflows/*.yaml' 2>/dev/null | grep -E '^\+' | grep -vE '^\+\+\+' || true)"
for f in "${UNTRACKED[@]}"; do
  [[ "$f" == .github/workflows/* ]] || continue
  if [[ -f "$f" ]] && grep -Iq . "$f" 2>/dev/null; then
    WF_ADDED+=$'\n'"$(sed 's/^/+/' "$f")"
  fi
done
grep -q 'persist-credentials:[ ]*true' <<<"$WF_ADDED" \
  && warn "authz" "persist-credentials: true added — default is false, justify"
echo "$WF_ADDED" | grep -E 'uses:[ ]*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@' | grep -vE '@[0-9a-f]{40}' >/dev/null \
  && warn "authz" "unpinned action ref added — pin to a full commit SHA"

# --- 8. perf -----------------------------------------------------------------
PERF_HIT=0
# WF_ADDED covers tracked and untracked workflow files alike.
echo "$WF_ADDED" | grep -E 'sleep ([0-9]|infinity)' >/dev/null \
  && { warn "perf" "sleep in added workflow steps — poll an endpoint or bound the wait"; PERF_HIT=1; }
while IFS= read -r line; do
  body="${line#+}"
  if [[ "$body" =~ curl|wget ]] && [[ ! "$body" =~ --retry|max-time|connect-time|timeout ]]; then
    warn "perf" "network fetch without retry/timeout: ${body:0:80}"; PERF_HIT=1; break
  fi
done <<<"$ADDED_LINES"
CLONES="$(grep -E 'git clone' <<<"$ADDED_LINES" || true)"
# Guard -n: a herestring always feeds one line, so an empty capture would
# false-match the inverted grep; the pipeline form fed it EOF instead.
if [[ -n "$CLONES" ]] && grep -qvE -- '--depth|--filter' <<<"$CLONES"; then
  warn "perf" "full git clone added — use --depth/--filter for CI"; PERF_HIT=1
fi
[[ "$PERF_HIT" -eq 0 ]] && pass "perf" "no sleep/unbounded-fetch findings"

# --- 9. dead-state ------------------------------------------------------------
DEAD_HIT=0
grep -Eq '^\+\s*.*(TODO|FIXME|XXX|HACK|WIP):' <<<"$ADDED_LINES" \
  && { warn "dead-state" "TODO/FIXME marker added — file an issue or finish it"; DEAD_HIT=1; }
# debugg[ae]r is spelled to not match this very line (self-scan safety).
grep -Eq '^\+\s*.*(console\.(log|debug)|binding\.pry|pdb\.set_trace|breakpoint\(\)|(^|[^_])debugg[ae]r)' <<<"$ADDED_LINES" \
  && { warn "dead-state" "debug leftover added (stray log/pdb/breakpoint call)"; DEAD_HIT=1; }
grep -Eq '^\+\s*(#|//)\s*(if|for|while|return|const|let|var|function|import|from|def |class |public |private )' <<<"$ADDED_LINES" \
  && { warn "dead-state" "commented-out code added — delete it, history keeps it"; DEAD_HIT=1; }
for f in "${ADDED_FILES[@]}"; do
  is_code "$f" || continue
  key="$(basename "$f" | sed -E 's/\.(test|spec)\.[^.]+$//;s/\.[^.]+$//;s/[_-]/ /g' | awk '{print $1}')"
  [[ -z "$key" || ${#key} -lt 3 ]] && continue
  # Worktree grep (not HEAD): co-added files that wire this one up are
  # visible even before anything is committed.
  if ! grep -rlI --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=target --exclude="$(basename "$f")" -- "$key" . >/dev/null 2>&1; then
    warn "dead-state" "new file $f looks unreferenced — wire it up or drop it"; DEAD_HIT=1
  fi
done
[[ "$DEAD_HIT" -eq 0 ]] && pass "dead-state" "no TODO/debug/commented-code findings"

# --- 10. spec-mismatch ---------------------------------------------------------
CC_RE='^(feat|fix|perf|refactor|test|docs|build|ci|chore|revert|style|security)(\([A-Za-z0-9_., /-]+\))?(!)?: ([^ .]|[^ .].*[^.])$'
cc_ok() { [[ "${#1}" -le 100 ]] && [[ "$1" =~ $CC_RE ]]; }
SPEC_FAIL=0
# Merge subjects are generated by git and pushed merges cannot be
# rewritten, so only non-merge subjects are held to the header rule.
while IFS= read -r subj; do
  cc_ok "$subj" || { fail "spec-mismatch" "commit subject breaks Conventional Commits: $subj"; SPEC_FAIL=1; }
done < <(git log --no-merges --format='%s' "$BASE"..HEAD 2>/dev/null || true)
if [[ -n "$PR_TITLE" ]]; then
  cc_ok "$PR_TITLE" \
    || { fail "spec-mismatch" "PR title breaks Conventional Commits (max 100 chars, no trailing period)"; SPEC_FAIL=1; }
fi
if [[ -n "$PR_BODY" ]]; then
  if [[ -f "$PR_BODY" ]]; then
    EMPTY_SECS="$(awk '/^## /{if(sec!=""){if(body==0)print sec} sec=$0; body=0; next} NF{body=1} END{if(sec!=""&&body==0)print sec}' "$PR_BODY")"
    if [[ -n "$EMPTY_SECS" ]]; then
      while IFS= read -r s; do fail "spec-mismatch" "PR body section empty: $s"; done <<<"$EMPTY_SECS"
      SPEC_FAIL=1
    elif [[ "$SPEC_FAIL" -eq 0 ]]; then
      pass "spec-mismatch" "commits and PR body sections complete"
    fi
  else
    fail "spec-mismatch" "PR body file not found: $PR_BODY"; SPEC_FAIL=1
  fi
elif [[ "$SPEC_FAIL" -eq 0 ]]; then
  skip "spec-mismatch" "no --pr-body/--pr-title given (commits checked)"
fi

echo "---  ---------------  ------------------------------------------------"
echo "FAIL=$FAILS WARN=$WARNS (exit $([[ $FAILS -gt 0 ]] && echo 1 || echo 0))"
[[ "$FAILS" -gt 0 ]] && exit 1 || exit 0
