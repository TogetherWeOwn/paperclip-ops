#!/usr/bin/env bash
# pre-review.sh — deterministic author self-check for a pull-request diff.
#
# Runs the mechanical half of a code review locally, on the diff only, and
# prints a pass/fail table. No network, no credentials, no model calls:
# bash and git, plus offline Cargo metadata when Cargo.toml changes. The
# judgment half stays with the author plus the rubric in RUBRIC.md.
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
SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pre-review.XXXXXX")" || { echo "cannot create scratch directory" >&2; exit 2; }
trap 'rm -rf -- "$SCRATCH_DIR"' EXIT

CHANGED=() ADDED_FILES=() UNTRACKED=()
if ! git diff --name-only -z "$BASE"...HEAD -- >"$SCRATCH_DIR/changed-paths" 2>/dev/null; then
  echo "cannot collect changed paths" >&2; exit 2
fi
while IFS= read -r -d '' path; do CHANGED+=("$path"); done <"$SCRATCH_DIR/changed-paths"
if ! git diff --diff-filter=A --name-only -z "$BASE"...HEAD -- >"$SCRATCH_DIR/added-paths" 2>/dev/null; then
  echo "cannot collect added paths" >&2; exit 2
fi
while IFS= read -r -d '' path; do ADDED_FILES+=("$path"); done <"$SCRATCH_DIR/added-paths"
if ! git diff -U0 "$BASE"...HEAD -- >"$SCRATCH_DIR/added.diff" 2>/dev/null; then
  echo "cannot collect added content" >&2; exit 2
fi
ADDED_LINES="$(grep -E '^\+' "$SCRATCH_DIR/added.diff" | grep -vE '^\+\+\+' || true)"
# Untracked files are invisible to git diff but fully part of the change:
# review their whole content as added lines (new files count as Added).
if ! git ls-files --others --exclude-standard -z >"$SCRATCH_DIR/untracked-paths" 2>/dev/null; then
  echo "cannot collect untracked paths" >&2; exit 2
fi
while IFS= read -r -d '' u; do
  [[ -n "$u" ]] || continue
  CHANGED+=("$u")
  ADDED_FILES+=("$u")
  UNTRACKED+=("$u")
  if [[ -f "$u" ]] && grep -Iq . -- "$u" 2>/dev/null; then
    if ! sed 's/^/+/' -- "$u" >"$SCRATCH_DIR/untracked-content" 2>/dev/null; then
      echo "cannot read untracked file: $u" >&2; exit 2
    fi
    ADDED_LINES+=$'\n'"$(<"$SCRATCH_DIR/untracked-content")"
  fi
done <"$SCRATCH_DIR/untracked-paths"

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
cargo_lock_current() { # $1: changed Cargo.toml path; inspect committed inputs only.
  command -v cargo >/dev/null 2>&1 || return 1
  local snapshot manifest dir lock_path lock_tracked result
  snapshot="$(mktemp -d 2>/dev/null)" || return 1
  if ! git archive HEAD 2>/dev/null | tar -x -C "$snapshot" 2>/dev/null; then
    rm -rf "$snapshot"
    return 1
  fi
  # Remove every submitted .cargo entry, including symlinks. Cargo command-line
  # overrides also neutralize compiler wrappers if another config source exists.
  find "$snapshot" -name .cargo -prune -exec rm -rf {} + 2>/dev/null || true
  manifest="$snapshot/$1"
  lock_tracked=0
  dir="$(dirname "$1")"
  while true; do
    lock_path="${dir#./}"
    [[ "$dir" == "." ]] && lock_path=""
    if git cat-file -e "HEAD:${lock_path:+$lock_path/}Cargo.lock" 2>/dev/null; then
      lock_tracked=1
      break
    fi
    [[ "$dir" == "." ]] && break
    dir="$(dirname "$dir")"
  done
  local -a lock_args=()
  [[ "$lock_tracked" -eq 1 ]] && lock_args+=(--locked)
  (cd "$snapshot" && cargo --config 'build.rustc="rustc"' \
    --config 'build.rustc-wrapper=""' \
    --config 'build.rustc-workspace-wrapper=""' \
    metadata "${lock_args[@]}" --offline --format-version 1 --manifest-path "$manifest" >/dev/null 2>&1)
  result=$?
  rm -rf "$snapshot"
  return "$result"
}
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
    if [[ "$hit" -eq 0 ]]; then
      if [[ "$m" == Cargo.toml ]] && cargo_lock_current "$f"; then
        continue
      fi
      if [[ "$m" == Cargo.toml ]]; then
        fail "generated-drift" "$f changed without a lockfile update; offline Cargo metadata could not verify dependency state"
      else
        fail "generated-drift" "$f changed without its lockfile in $d or above"
      fi
      GEN_FAIL=1
    fi
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
# Keep the canonical literal checks, while recognizing values immediately
# before delimiters the bare-value branch intentionally excludes. A narrowly
# recognized Rust type span (line-leading fields, or inline fn-parameter and
# struct-field positions) may suppress only a match wholly inside that span;
# strong credential families are checked first against original added lines.
# Generic-shape detection is per-occurrence: only `<` inside the colon's own
# type position with a non-type generic argument flags. Paren/call values
# must be quoted literals or literal-shaped runs; typed parameters, bare
# identifiers, dotted paths and method-chain receivers are not credentials.
SECRET_RE='(BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[bap]-[A-Za-z0-9-]{8,}|(token|secret|password)["'"'"' ]*[:=](["'"'"' ]*["'"'"'][^"'"'"' ]{12,}|["'"'"' ]+[^"'"'"' ().<>]{12,}(["'"'"' ,;#]|$)))'
# Strong credential families are always checked against the original added
# bytes, before any language-specific match-span classification. The api-key
# family is checked separately (api_key_literal_hit) because its unquoted value
# keeps dots: a SendGrid-style SG.<segment>.<segment> key is a credential, and
# only values that start with a known environment/config lookup are dropped.
STRONG_SECRET_RE='(BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[bap]-[A-Za-z0-9-]{8,})'
RUST_TYPE_NAME='(Option|String|str|Arc|Result|Vec|HashMap|HashSet|Box|Cow|SecretString|SensitiveString|PasswordHash|SecretWrapper|bool|char|f32|f64|[iu](8|16|32|64|128|size)|[A-Z][A-Za-z0-9_]*(Type|Envelope[0-9]*|Response|Error))'
API_KEY_SECRET_RE="api[_-]?key[\"'[:space:]]*[:=][\"'[:space:]]*([\"'][A-Za-z0-9_.-]{12,}[\"']|[A-Za-z0-9_.-]{12,})"
# An unquoted api-key value that starts with one of these is a lookup, not a
# credential (process.env.X, os.environ[...], import.meta.env.X, ENV.X,
# settings.X, config.X). Quoted values never qualify, since a quoted lookup
# is a string literal, and the check is case-sensitive on the lookup prefix.
API_KEY_LOOKUP_RE="^[Aa][Pp][Ii][_-]?[Kk][Ee][Yy][\"'[:space:]]*[:=][[:space:]]*(process\\.env\\.|os\\.environ(\\.|$)|import\\.meta\\.env\\.|ENV\\.|settings\\.|config\\.)"
# A lookup exempts only its own value: a quoted 12+ literal behind any lookup
# via `||`, `??` or `or` is a credential. A second call argument is a default
# only for getters that take one (os.environ.get, os.getenv, ENV.fetch);
# config.get(section, option) and settings.get(...) take a key name, not a
# default, so they never trigger on the second argument.
API_KEY_FALLBACK_OP_RE="${API_KEY_LOOKUP_RE#^}[^[:space:]]*(([[:space:]]*(\\|\\||\\?\\?)|[[:space:]]+or)[[:space:]]*[\"'][^\"']{12,}[\"'])"
API_KEY_FALLBACK_ARG_RE="[Aa][Pp][Ii][_-]?[Kk][Ee][Yy][\"'[:space:]]*[:=][[:space:]]*(os\\.environ\\.get|os\\.getenv|ENV\\.fetch)\\([^(),]*,[[:space:]]*[\"'][^\"']{12,}[\"']"
API_KEY_FALLBACK_RE="(${API_KEY_FALLBACK_OP_RE}|${API_KEY_FALLBACK_ARG_RE})"
RUST_TYPE_PATH="(([A-Za-z_][A-Za-z0-9_]*::)*${RUST_TYPE_NAME})"
RUST_TYPE_GENERIC_REF="(&[[:space:]]*(mut[[:space:]]+)?('[[:alpha:]_][[:alnum:]_]*[[:space:]]*)?)?"
RUST_TYPE_GENERIC_ARG1="${RUST_TYPE_GENERIC_REF}${RUST_TYPE_PATH}([[:space:]]*<${RUST_TYPE_GENERIC_REF}${RUST_TYPE_PATH}(,[[:space:]]*${RUST_TYPE_GENERIC_REF}${RUST_TYPE_PATH})*[[:space:]]*>)?"
RUST_TYPE_GENERIC_ARG2="${RUST_TYPE_GENERIC_REF}${RUST_TYPE_PATH}([[:space:]]*<${RUST_TYPE_GENERIC_ARG1}(,[[:space:]]*${RUST_TYPE_GENERIC_ARG1})*[[:space:]]*>)?"
RUST_TYPE_GENERIC_ARG3="${RUST_TYPE_GENERIC_REF}${RUST_TYPE_PATH}([[:space:]]*<${RUST_TYPE_GENERIC_ARG2}(,[[:space:]]*${RUST_TYPE_GENERIC_ARG2})*[[:space:]]*>)?"
RUST_TYPE_GENERIC="([[:space:]]*<${RUST_TYPE_GENERIC_ARG3}(,[[:space:]]*${RUST_TYPE_GENERIC_ARG3})*[[:space:]]*>)?"
RUST_TYPE_SPAN_RE="^[+][[:space:]]*(pub([[:space:]]*[(][^)]*[)])?[[:space:]]+)?(let[[:space:]]+(mut[[:space:]]+)?)?[[:alnum:]_]*([Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd])[[:space:]]*:[[:space:]]*(&[[:space:]]*(mut[[:space:]]+)?('[[:alpha:]_][[:alnum:]_]*[[:space:]]*)?|[*](const|mut)[[:space:]]+|dyn[[:space:]]+|impl[[:space:]]+)?([[][^]]+;[[:space:]]*[0-9]+[]]|${RUST_TYPE_PATH}${RUST_TYPE_GENERIC})[[:space:]]*([,;=]|$)"
# A trailing dot ends a value only at end of line or before whitespace
# (sentence terminators); a dot before an identifier is a method call or
# attribute path, so receiver.method() chains are not credentials.
EDGE_VALUE_RE="(token|secret|password)[\"'[:space:]]*[:=][\"'[:space:]]*[A-Za-z0-9_-]{12,}([\"'[:space:],;#=]|\.[[:space:]]|\.$|$)"
EDGE_ANGLE_RE="(token|secret|password)[\"'[:space:]]*[:=][\"'[:space:]]*<?[A-Za-z0-9_-]{12,}(<[A-Za-z0-9_][A-Za-z0-9_]*>|>)"
# Paren values are quoted literals or dotless 12+ runs; whether an unquoted
# run is a literal is decided by edge_paren_value_is_literal. Typed
# parameters (fn new(token: String)), bare identifiers (secret_name_value)
# and CamelCase types (AccessTokenValue) before ')' are not credentials,
# while credential-shaped runs (Q7m2V9z4L6p8R1s3, abcdefghijklmnop) still fail.
EDGE_PAREN_RE="(token|secret|password)[\"'[:space:]]*[:=][\"'[:space:]]*[(]?[\"'[:space:]]*(\"[^\"]{12,}\"|'[^']{12,}'|[A-Za-z0-9_-]{12,})[)]"
EDGE_CALL_RE="(token|secret|password)[\"'[:space:]]*[(][\"'[:space:]]*(\"[^\"]{12,}\"|'[^']{12,}'|[A-Za-z0-9_-]{12,})={0,}[\"'[:space:]]*[)]"
RUST_LITERAL_RE="(token|secret|password)[^;\"/]*[:=][^;\"/]*\"[^\"[:space:]]{12,}\""
# A credential word with optional block comments up to its colon. Case
# variants are explicit so byte offsets from case-sensitive grep stay valid.
RUST_TOKEN_COLON_RE='([Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd])([[:space:]]|/\*([^*]|\*+[^*/])*\*/)*:'
# Inline recognized type spans: fn parameters and inline struct fields shaped
# like the line-leading RUST_TYPE_SPAN_RE, anchored after (, comma or {.
# A match wholly inside one of these spans is a declaration, not a secret.
RUST_INLINE_TYPE_SPAN_RE="[(,{][[:space:]]*[[:alnum:]_]*([Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd])[[:space:]]*:[[:space:]]*(&[[:space:]]*(mut[[:space:]]+)?('[[:alpha:]_][[:alnum:]_]*[[:space:]]*)?|[*](const|mut)[[:space:]]+|dyn[[:space:]]+|impl[[:space:]]+)?([[][^]]+;[[:space:]]*[0-9]+[]]|${RUST_TYPE_PATH}${RUST_TYPE_GENERIC})[[:space:]]*([,;=)}]|$)"
# True when a paren/call candidate holds a literal: a quoted 12+ value, or an
# unquoted dotless 12+ run shaped like a credential. Base64 padding (`=`)
# after the run is part of the value. Runs containing an underscore are
# bare identifiers (secret_name_value) and runs with uppercase but no digit
# or dash are type references (AccessTokenValue); neither counts.
# Digit/dash runs and solid lowercase runs are literals.
edge_paren_value_is_literal() {
  local cand="$1" val
  if [[ "$cand" =~ [\"\']([^\"\']{12,})[\"\'][[:space:]]*\)?$ ]]; then
    return 0
  fi
  if [[ "$cand" =~ [^A-Za-z0-9_.-]([A-Za-z0-9_-]{12,})=*[\"\'[:space:]]*\)?$ ]]; then
    val="${BASH_REMATCH[1]}"
    [[ "$val" == *[_]* ]] && return 1
    [[ "$val" =~ [0-9-] ]] && return 0
    [[ "$val" =~ ^[a-z]+$ ]] && return 0
  fi
  return 1
}
# True when the text holds an api-key assignment whose value is not an
# environment/config lookup, or a lookup followed by a quoted 12+ literal
# fallback (`|| "..."`, `?? "..."`, `or "..."`, or a second argument to
# os.environ.get / os.getenv / ENV.fetch): the lookup exempts its own value,
# never a literal that rides behind it.
# Matches are collected first and filtered after: a pipeline into `grep -q`
# could be cut short by SIGPIPE and read as a miss.
api_key_literal_hit() {
  local matches
  matches="$(grep -Eoi "$API_KEY_SECRET_RE" <<<"$1" || true)"
  if [[ -n "$matches" && -n "$(grep -Ev "$API_KEY_LOOKUP_RE" <<<"$matches" || true)" ]]; then
    return 0
  fi
  grep -Eq "$API_KEY_FALLBACK_RE" <<<"$1"
}
# True when the byte range [off, off+len) lies wholly inside one region of a
# space-separated "start:end ..." region list.
offset_in_regions() {
  local off="$1" len="$2" regions="$3" r rs re
  for r in $regions; do
    rs="${r%%:*}"; re="${r##*:}"
    if [[ "$off" -ge "$rs" && $((off + len)) -le "$re" ]]; then return 0; fi
  done
  return 1
}
# True when a generic argument list (the text inside `<...>`) holds something
# that looks like a literal rather than a type. Type syntax is open-ended
# (Option<Token>, impl AsRef<[u8]>, Arc<RwLock<String>>, Cow<'a, str>), so
# the test is inverted: only literal-shaped arguments flag. An argument is
# literal-shaped when it holds a character outside type syntax (quote, slash,
# dot, backslash, ...) or an identifier run of 12+ characters that is
# lowercase-initial or carries a digit or dash. Module path prefixes
# (a::b::) are dropped first; CamelCase runs without digits are type names.
# Prints the text from the start of $1 to the `>` that closes the generic
# list opened just before it (nested lists included; `->` is not a closer).
# Returns 1 when the list does not close on this line.
rust_generic_args_text() {
  local s="$1" i ch prev="" depth=1
  for ((i = 0; i < ${#s}; i++)); do
    ch="${s:i:1}"
    if [[ "$ch" == "<" ]]; then
      depth=$((depth + 1))
    elif [[ "$ch" == ">" && "$prev" != "-" ]]; then
      depth=$((depth - 1))
      if [[ "$depth" -eq 0 ]]; then
        printf '%s' "${s:0:i}"
        return 0
      fi
    fi
    prev="$ch"
  done
  return 1
}
# `!` is macro invocation (Token![..]) and the never type; `#`, `$`, `{`, `}`
# are macro and const-generic syntax. All are type syntax, not literals.
RUST_NON_TYPE_CHAR_RE='[^][A-Za-z0-9_[:space:]:<>&'"'"';,()+?*={}$#!-]'
rust_generic_arg_is_literal() {
  local LC_ALL=C
  local rest="$1" tok
  [[ "$rest" =~ $RUST_NON_TYPE_CHAR_RE ]] && return 0
  while [[ "$rest" =~ [A-Za-z_][A-Za-z0-9_]*:: ]]; do
    rest="${rest/"${BASH_REMATCH[0]}"/}"
  done
  while [[ "$rest" =~ ([A-Za-z0-9_-]{12,}) ]]; do
    tok="${BASH_REMATCH[1]}"
    [[ "$tok" =~ ^[a-z] || "$tok" =~ [0-9-] ]] && return 0
    rest="${rest#*"$tok"}"
  done
  return 1
}
# Per-occurrence generic check for lines without a line-leading type span.
# Flags only occurrences whose colon is followed, within one type position
# (no = ; , ( ) { } - between), by `<` opening a literal-shaped generic
# argument (rust_generic_arg_is_literal). Occurrences inside a recognized
# span are declarations and never flag.
rust_generic_occurrence_is_suspicious() {
  local LC_ALL=C
  local line="$1" lexical="$2" tks="$3" te="$4" regions="$5"
  local off omatch rest arg
  while IFS=: read -r off omatch; do
    [[ -n "$omatch" ]] || continue
    if [[ "$lexical" -eq 1 ]] && [[ "$tks" -ge 0 ]] \
      && [[ "$off" -ge "$tks" ]] && [[ $((off + ${#omatch})) -le "$te" ]]; then
      continue
    fi
    if [[ "$lexical" -eq 1 ]] && offset_in_regions "$off" "${#omatch}" "$regions"; then
      continue
    fi
    rest="${line:off}"
    if [[ "$rest" =~ ^${RUST_TOKEN_COLON_RE}[^=\;,(){}-]*\< ]]; then
      # The whole generic list when it closes on this line; otherwise only
      # the first argument, so trailing line text is not read as a type.
      if ! arg="$(rust_generic_args_text "${rest#*<}")"; then
        arg="${rest#*<}"
        arg="${arg%%[,>]*}"
      fi
      rust_generic_arg_is_literal "$arg" && return 0
      continue
    fi
  done < <(LC_ALL=C grep -Ebo "$RUST_TOKEN_COLON_RE" <<<"$line" || true)
  return 1
}
authz_file_lines() {
  local file="$1" untracked=0 path
  for path in "${UNTRACKED[@]}"; do [[ "$path" == "$file" ]] && { untracked=1; break; }; done
  if [[ "$untracked" -eq 1 ]]; then
    [[ -f "$file" ]] || return 1
    if [[ -s "$file" ]] && ! grep -Iq . -- "$file" 2>/dev/null; then
      return 1
    fi
    awk '{ print NR "|+" $0 }' <"$file"
  else
    # Gitlinks carry no collectible text in the superproject: a submodule
    # pointer bump adds no lines, so it passes. Read the tracked mode first;
    # any listing failure fails closed.
    ls_out=""
    if ! ls_out="$(git --literal-pathspecs ls-tree HEAD -- "$file" 2>/dev/null)"; then
      return 1
    fi
    if [[ "$ls_out" =~ ^160000[[:space:]] ]]; then
      return 0 # gitlink: no added text to scan
    fi
    if git cat-file -e "HEAD:$file" 2>/dev/null; then
      : # present in HEAD: continue to diff collection below
    elif [[ -n "$ls_out" ]]; then
      return 1 # present in HEAD but cat-file failed: query error, fail closed
    else
      return 0 # verified absent in HEAD: deleted, no added content
    fi
    if ! git --literal-pathspecs diff -U0 "$BASE"...HEAD -- "$file" >"$SCRATCH_DIR/file.diff" 2>/dev/null; then
      return 1
    fi
    if grep -q '^Binary files ' "$SCRATCH_DIR/file.diff"; then
      return 1
    fi
    awk '
      /^@@ / {
        if (!match($0, /\+[0-9]+(,[0-9]+)?/)) exit 1
        range = substr($0, RSTART + 1, RLENGTH - 1)
        split(range, parts, ",")
        line = parts[1]
        next
      }
      /^\+\+\+/ { next }
      /^\+/ { print line "|+" substr($0, 2); line++; next }
      /^ / { line++; next }
    ' "$SCRATCH_DIR/file.diff"
  fi
}
rust_plain_code_lines() {
  awk '
    function raw_open(rest,    prefix, hashes) {
      if (!match(rest, /^(br|r)#*"/)) return 0
      prefix = substr(rest, 1, RLENGTH)
      hashes = prefix
      sub(/^(br|r)/, "", hashes)
      sub(/"$/, "", hashes)
      raw_end = "\"" hashes
      return RLENGTH
    }
    {
      text = $0
      safe = 1
      i = 1
      while (i <= length(text)) {
        rest = substr(text, i)
        ch = substr(text, i, 1)
        if (raw_end != "") {
          safe = 0
          if (substr(text, i, length(raw_end)) == raw_end) {
            i += length(raw_end)
            raw_end = ""
          } else i++
          continue
        }
        if (string_state) {
          safe = 0
          if (escaped) { escaped = 0; i++; continue }
          if (ch == "\\") { escaped = 1; i++; continue }
          if (ch == "\"") string_state = 0
          i++
          continue
        }
        if (block_depth > 0) {
          safe = 0
          if (substr(text, i, 2) == "/*") { block_depth++; i += 2; continue }
          if (substr(text, i, 2) == "*/") { block_depth--; i += 2; continue }
          i++
          continue
        }
        if (substr(text, i, 2) == "//") { safe = 0; break }
        if (substr(text, i, 2) == "/*") { safe = 0; block_depth = 1; i += 2; continue }
        consumed = raw_open(rest)
        if (consumed > 0) { safe = 0; i += consumed; continue }
        if (ch == "\"") { safe = 0; string_state = 1; i++; continue }
        if (ch == sprintf("%c", 39)) {
          safe = 0
          if (substr(text, i + 1, 1) == "\\") i += 4
          else if (substr(text, i + 2, 1) == sprintf("%c", 39)) i += 3
          # A lifetime is code, but it must never restore safe=1 after
          # string, raw-string or comment content was seen on the same
          # line. A line with no literal content stays safe (safe is still
          # 1 from line start); a line with any is not plain code.
          else { i++ }
          continue
        }
        i++
      }
      if (safe && raw_end == "" && block_depth == 0 && !string_state) print NR
      if (string_state && escaped) escaped = 0
    }
  ' "$1"
}
rust_line_has_secret_match() {
  local LC_ALL=C
  local line="$1" line_number="$2" safe_lines="$3" pattern offset candidate lexical=0
  local type_end=-1 type_key_start=-1 type_key_match type_shape=0
  local inline_regions="" ioff imatch
  grep -Fxq "$line_number" <<<"$safe_lines" && lexical=1
  if [[ "+$line" =~ $RUST_TYPE_SPAN_RE ]]; then
    type_shape=1
    if [[ "$lexical" -eq 1 ]]; then
      type_end=$((${#BASH_REMATCH[0]} - 1))
      while IFS=: read -r offset type_key_match; do
        [[ -n "$type_key_match" ]] || continue
        type_key_start="$offset"
        break
      done < <(grep -Eboi '(token|secret|password)[[:space:]]*:' <<<"$line" || true)
    fi
  fi
  # Inline recognized type spans (fn parameters, inline struct fields) exempt
  # matches the way the line-leading span does. Strings and comments are
  # never exempt: regions apply only to lexically plain-code lines.
  if [[ "$lexical" -eq 1 ]]; then
    while IFS=: read -r ioff imatch; do
      [[ -n "$imatch" ]] || continue
      inline_regions+="${ioff}:$((ioff + ${#imatch})) "
    done < <(LC_ALL=C grep -Ebo "$RUST_INLINE_TYPE_SPAN_RE" <<<"$line" || true)
  fi
  if [[ "$type_shape" -eq 0 ]] \
    && rust_generic_occurrence_is_suspicious "$line" "$lexical" "$type_key_start" "$type_end" "$inline_regions"; then
    return 0
  fi
  for pattern in "$SECRET_RE" "$EDGE_VALUE_RE" "$EDGE_ANGLE_RE" "$EDGE_PAREN_RE" "$EDGE_CALL_RE" "$RUST_LITERAL_RE"; do
    while IFS=: read -r offset candidate; do
      [[ -n "$candidate" ]] || continue
      # Paren/call candidates must hold a literal, not a bare identifier.
      if [[ "$pattern" == "$EDGE_PAREN_RE" || "$pattern" == "$EDGE_CALL_RE" ]] \
        && ! edge_paren_value_is_literal "$candidate"; then
        continue
      fi
      if [[ "$lexical" -eq 1 && "$type_key_start" -ge 0 \
        && "$offset" -ge "$type_key_start" \
        && $((offset + ${#candidate})) -le "$type_end" ]]; then
        continue
      fi
      if [[ "$lexical" -eq 1 ]] \
        && offset_in_regions "$offset" "${#candidate}" "$inline_regions"; then
        continue
      fi
      return 0
    done < <(LC_ALL=C grep -Eboi "$pattern" <<<"$line" || true)
  done
  return 1
}
RUST_TYPED_VALUE_RE='(token|secret|password)[[:space:]]*:[^=;]*=([A-Za-z0-9_-]{12,}[=),;[:space:].:/+]|[^;]*[[:space:](,.:/+=][A-Za-z0-9_-]{12,}[=),;[:space:].:/+])'
# Generic-shape detection is per-occurrence now (rust_generic_occurrence_is_suspicious):
# only a `<` inside the colon's own type position with a non-type generic
# argument flags. The old whole-line `[^=;]*<` reached return types and other
# fields, failing ordinary fn signatures.
AUTHZ_NON_RUST_LINES=""
AUTHZ_RUST_PATTERN_HIT=0
AUTHZ_COLLECTION_FAILED=0
STRONG_SECRET_HIT=0
if grep -Eiq "$STRONG_SECRET_RE" <<<"$ADDED_LINES" \
  || api_key_literal_hit "$ADDED_LINES"; then
  STRONG_SECRET_HIT=1
fi
for file in "${CHANGED[@]}"; do
  if ! file_lines="$(authz_file_lines "$file")"; then
    fail "authz" "cannot collect added lines for changed file: $file"
    AUTHZ_COLLECTION_FAILED=1
    continue
  fi
  if [[ "$file" == *.rs ]]; then
    if [[ -n "$file_lines" ]]; then
      is_untracked=0
      for path in "${UNTRACKED[@]}"; do [[ "$path" == "$file" ]] && { is_untracked=1; break; }; done
      if [[ "$is_untracked" -eq 1 ]]; then
        if ! cp -- "$file" "$SCRATCH_DIR/rust-source" 2>/dev/null; then
          fail "authz" "cannot read Rust lexical context: $file"
          AUTHZ_COLLECTION_FAILED=1
          continue
        fi
      elif ! git cat-file blob "HEAD:$file" >"$SCRATCH_DIR/rust-source" 2>/dev/null; then
        fail "authz" "cannot collect committed Rust lexical context: $file"
        AUTHZ_COLLECTION_FAILED=1
        continue
      fi
      if ! safe_lines="$(rust_plain_code_lines "$SCRATCH_DIR/rust-source")"; then
        fail "authz" "cannot classify Rust lexical context: $file"
        AUTHZ_COLLECTION_FAILED=1
        continue
      fi
    else
      safe_lines=""
    fi
    while IFS= read -r numbered; do
      [[ -n "$numbered" ]] || continue
      line_number="${numbered%%|*}"
      added_line="${numbered#*|}"
      if rust_line_has_secret_match "${added_line#+}" "$line_number" "$safe_lines" \
        || grep -Eiq "$RUST_TYPED_VALUE_RE" <<<"$added_line"; then
        AUTHZ_RUST_PATTERN_HIT=1
      fi
    done <<<"$file_lines"
  else
    non_rust_lines=""
    while IFS= read -r numbered; do
      [[ -n "$numbered" ]] || continue
      non_rust_lines+=$'\n'"${numbered#*|}"
    done <<<"$file_lines"
    AUTHZ_NON_RUST_LINES+=$'\n'"$non_rust_lines"
  fi
done
# Paren/call candidates on non-Rust lines use the same literal-shape gate as
# the Rust path: bare identifiers and dotted paths before ')' are not secrets.
non_rust_edge_call_hit() {
  local numbered linematch pat
  while IFS= read -r numbered; do
    [[ -n "$numbered" ]] || continue
    for pat in "$EDGE_PAREN_RE" "$EDGE_CALL_RE"; do
      while IFS= read -r linematch; do
        [[ -n "$linematch" ]] || continue
        edge_paren_value_is_literal "$linematch" && return 0
      done < <(grep -Eoi "$pat" <<<"$numbered" || true)
    done
  done <<<"$AUTHZ_NON_RUST_LINES"
  return 1
}
EDGE_ANGLE_MATCHES="$(grep -Eio "$EDGE_ANGLE_RE" <<<"$AUTHZ_NON_RUST_LINES" || true)"
if [[ "$AUTHZ_COLLECTION_FAILED" -eq 1 ]] \
  || [[ "$STRONG_SECRET_HIT" -eq 1 ]] \
  || grep -Eiq "$SECRET_RE" <<<"$AUTHZ_NON_RUST_LINES" \
  || grep -Eiq "$EDGE_VALUE_RE" <<<"$AUTHZ_NON_RUST_LINES" \
  || [[ -n "$EDGE_ANGLE_MATCHES" ]] \
  || non_rust_edge_call_hit \
  || [[ "$AUTHZ_RUST_PATTERN_HIT" -eq 1 ]];
then
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
