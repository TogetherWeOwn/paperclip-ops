#!/usr/bin/env bash
# ===========================================================================
# Offline regression suite for shell_subst_guard.py.
#
# WHY THIS RUNS ANYWHERE. The guard scans one command STRING and never
# executes it (the suite asserts the source contains no process-spawn
# primitive). Every fixture here is inert text: even the refused shapes are
# passed as argv/heredoc bytes to a lexer, never to a shell. No network, no
# credential, no database, and nothing leaves the temp directory. An
# unbounded `yes` is NEVER executed by this suite -- that is the point: the
# exact shape that consumed ~30 GiB through command-substitution collection
# must be refused BEFORE execution, so no test may depend on running it.
#
# THE ASSERTIONS THAT CARRY THIS SUITE:
#   1. The observed shape is refused. `echo "RUNSET=$([ -n ... ] && yes
#      || no)"` -- spacing and newline variants included -- exits 3 with a
#      FINDING naming unbounded-yes-substitution. If this goes, the guard
#      has become the thing that watched the memcg OOM happen.
#   2. Ordinary `$()`/backtick variants are refused, including nested,
#      glued quoted-word (`ye"s"`, `$'yes'`), prefixed
#      (`command`/`builtin`/`env`/`sudo`/`time`/`exec`/`eval`/`nice`),
#      assignment-prefixed, leading-redirect, pathed and backslash-escaped
#      spellings, plus interiors of `${...}`/`$((...))` and `case` bodies.
#      A `#` inside double quotes or a `${#..}` length expansion never
#      hides a substitution.
#   3. Fail-closed pins: `$(yes | head -n 1)`, `$(yes --help)` and
#      `$(command -v yes)` are refused even though bounded/descriptive.
#      The gate cannot verify the bound or model per-flag semantics, so it
#      refuses and directs the author to `printf`/`type`. Never silently
#      weakened: these tests fail if anyone "helpfully" allows them.
#   4. Safe fixed-output checks stay allowed: the `printf 'yes'`/`printf
#      'no'` boolean idiom, `yes` as an argument/literal, comments (full-line,
#      trailing and inside substitutions), arithmetic, parameter expansion,
#      `[[ ]]` operands, `case` patterns, the descriptive-only `type yes`,
#      quoted-heredoc bodies and heredoc content lines, redirects into a
#      file named `yes`, and loop lists. A guard that fires on these gets
#      muted, which is the same as having no guard.
#   5. Scope pin: a bare `yes` with NO substitution (`yes | head -n 1`,
#      `if yes; ...`) stays allowed. Nothing is collected, so the memcg
#      mechanism here does not apply; policing bare commands is general
#      command censorship, explicitly out of scope.
#   6. Same-label PASS/FAIL printing (see test_channel_drift.sh header).
#      Every assertion prints its label either way, so a failure names the
#      exact shape that regressed.
#
# Exit status only beyond the labels. No assertion matches prose that drifts.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="${SHELL_SUBST_GUARD_PY:-$HERE/shell_subst_guard.py}"
PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() {
  printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  if [ $# -gt 1 ]; then printf '        %s\n' "$2"; fi
  FAIL=$((FAIL+1))
}
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -f "$GUARD" ] || { echo "no shell_subst_guard.py at $GUARD" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

run_cmd()  { OUT="$("$GUARD" check --command "$1" 2>&1)"; RC=$?; }
run_file() { OUT="$("$GUARD" check --file "$1" 2>&1)"; RC=$?; }
# want_rc <expected> <label>
want_rc() {
  if [ "$RC" -eq "$1" ]; then ok "$2"; else bad "$2" "exit $RC, want $1 :: $OUT"; fi
}
# want_finding <label> — refused with the named finding.
want_finding() {
  want_rc 3 "$1 refuses (exit 3)"
  if grep -q "FINDING unbounded-yes-substitution" <<<"$OUT"; then
    ok "$1 names unbounded-yes-substitution"
  else
    bad "$1 names unbounded-yes-substitution" "$OUT"
  fi
}
# want_clean <label> — allowed.
want_clean() {
  want_rc 0 "$1 stays allowed (exit 0)"
  if grep -q "shell_subst_guard: clean" <<<"$OUT"; then
    ok "$1 prints clean"
  else
    bad "$1 prints clean" "$OUT"
  fi
}

# ---------------------------------------------------------------------------
hdr "the observed shape is refused, spacing and newline variants included"
run_cmd 'echo "RUNSET=$([ -n "$PAPERCLIP_RUN_ID" ] && yes || no)"'
want_finding "observed shape refuses"

run_cmd 'RUNSET=$( [ -n "$PAPERCLIP_RUN_ID" ] && yes || no )'
want_finding "spacing variant refuses"

cat > "$WORK/newline.sh" <<'EOF'
RUNSET=$(
  [ -n "$PAPERCLIP_RUN_ID" ] && yes || no
)
EOF
run_file "$WORK/newline.sh"
want_finding "newline variant refuses"

# ---------------------------------------------------------------------------
hdr "ordinary substitution variants are refused"
run_cmd 'x=$(yes)'
want_finding "bare dollar-paren refuses"

run_cmd 'echo `yes`'
want_finding "bare backtick refuses"

run_cmd 'RUNSET=`[ -n "$PAPERCLIP_RUN_ID" ] && yes || no`'
want_finding "backtick long form refuses"

run_cmd 'echo $(echo $(yes))'
want_finding "nested substitution refuses"

run_cmd 'echo $(echo $(echo $(yes)))'
want_finding "triply nested substitution refuses"

run_cmd 'x=$(yes y)'
want_finding "yes with args refuses"

run_cmd 'x=$(/usr/bin/yes)'
want_finding "pathed yes refuses"

run_cmd 'x=$(command yes)'
want_finding "command-prefixed yes refuses"

run_cmd 'x=$(env yes)'
want_finding "env-prefixed yes refuses"

run_cmd 'x=$(sudo yes)'
want_finding "sudo-prefixed yes refuses"

run_cmd 'x=$(time yes)'
want_finding "time-prefixed yes refuses"

run_cmd 'x=$( "yes" )'
want_finding "double-quoted command word refuses"

run_cmd 'x=$( '"'"'yes'"'"' )'
want_finding "single-quoted command word refuses"

run_cmd 'x=$(FOO=1 yes)'
want_finding "assignment-prefixed yes refuses"

run_cmd 'x=$(FOO=1 BAR=2 yes --foo=bar)'
want_finding "multi-assignment yes refuses"

run_cmd 'x=$( \y\e\s )'
want_finding "backslash-escaped yes refuses"

run_cmd 'x=$(! yes)'
want_finding "negated yes refuses"

run_cmd 'echo $(yes'
want_finding "unclosed substitution refuses (fail-closed)"

# ---------------------------------------------------------------------------
hdr "nested expansions still expose inner substitutions"
run_cmd 'x=${A:-$(yes)}'
want_finding "default-expansion interior refuses"

run_cmd 'x=$(( $(yes) + 1 ))'
want_finding "arithmetic interior refuses"

# ---------------------------------------------------------------------------
hdr "case bodies refuse while patterns stay allowed"
run_cmd 'x=$(case $A in *) yes;; esac)'
want_finding "case body refuses"

run_cmd 'x=$(case $A in *) echo hi; yes;; esac)'
want_finding "second case body refuses"

# ---------------------------------------------------------------------------
hdr "leading redirects never clear command position"
run_cmd 'x=$(2>/dev/null yes)'
want_finding "fd-first redirect refuses"

run_cmd 'x=$(</dev/null yes)'
want_finding "stdin-first redirect refuses"

run_cmd 'x=$(FOO=1 2>/dev/null yes)'
want_finding "assignment-then-redirect refuses"

run_cmd 'x=$(2>&1 yes)'
want_finding "fd-duplication redirect refuses"

# ---------------------------------------------------------------------------
hdr "hash inside quotes and length expansion are not comments"
run_cmd 'echo "a #b $(yes)"'
want_finding "hash inside double quotes refuses"

run_cmd 'x=$(echo ${#A}; yes)'
want_finding "length-expansion hash refuses"

# ---------------------------------------------------------------------------
hdr "glued quoted spellings refuse"
run_cmd 'x=$(ye"s")'
want_finding "glued double-quote refuses"

run_cmd 'x=$(y'"'"'es'"'"')'
want_finding "glued single-quote refuses"

run_cmd 'x=$("y"es)'
want_finding "leading quoted segment refuses"

run_cmd 'x=$(ye'"'"''"'"'s)'
want_finding "empty-quote glue refuses"

run_cmd 'x=$($'"'"'yes'"'"')'
want_finding "dollar-single-quote refuses"

# ---------------------------------------------------------------------------
hdr "direct exec/eval/nice prefixes refuse"
run_cmd 'x=$(exec yes)'
want_finding "exec-prefixed yes refuses"

run_cmd 'x=$(eval yes)'
want_finding "eval-prefixed yes refuses"

run_cmd 'x=$(nice yes)'
want_finding "nice-prefixed yes refuses"

run_cmd 'x=$(exec 2>/dev/null)'
want_clean "redirect-only exec stays allowed"

# ---------------------------------------------------------------------------
hdr "fail-closed pins are refused, never silently weakened"
run_cmd 'x=$(yes | head -n 1)'
want_finding "already-bounded yes pipeline refuses"

run_cmd 'x=$(yes y | head -c 10)'
want_finding "bounded pipeline with args refuses"

run_cmd 'x=$(yes --help)'
want_finding "yes --help refuses"

run_cmd 'x=$(command -v yes)'
want_finding "command -v yes refuses (no per-flag modelling)"

cat > "$WORK/unquoted-heredoc.sh" <<'FIX'
cat <<EOF
$(yes)
EOF
FIX
run_file "$WORK/unquoted-heredoc.sh"
want_finding "unquoted-heredoc substitution refuses"

# ---------------------------------------------------------------------------
hdr "safe fixed-output checks stay allowed"
run_cmd 'RUNSET=$([ -n "$PAPERCLIP_RUN_ID" ] && printf '"'"'yes'"'"' || printf '"'"'no'"'"')'
want_clean "printf boolean idiom"

run_cmd 'echo '"'"'yes'"'"''
want_clean "single-quoted literal"

run_cmd 'echo "yes"'
want_clean "double-quoted literal without substitution"

run_cmd 'echo $(echo yes)'
want_clean "yes as argument"

run_cmd 'echo $(printf yes)'
want_clean "yes as printf argument"

run_cmd 'x=$(FOO=yes)'
want_clean "yes as assignment value"

run_cmd 'x=$(FOO=bar printf yes)'
want_clean "assignment then printf"

run_cmd 'x=$(a=yes; echo "$a")'
want_clean "assignment then use"

run_cmd '# $(yes)'
want_clean "full-line comment"

run_cmd 'echo hi # $(yes)'
want_clean "trailing comment"

cat > "$WORK/inner-comment.sh" <<'EOF'
x=$(# pick the default
printf yes)
EOF
run_file "$WORK/inner-comment.sh"
want_clean "comment inside substitution"

run_cmd 'x=$((1 + 2))'
want_clean "arithmetic expansion"

run_cmd 'x=$((yes + 1))'
want_clean "yes-named variable in arithmetic"

run_cmd 'x=${FLAG:-yes}'
want_clean "parameter expansion default"

run_cmd 'x=$( [[ $A == yes ]] && printf ok )'
want_clean "test operand"

run_cmd 'x=$(case $A in yes) printf ok;; *) printf no;; esac)'
want_clean "case pattern"

run_cmd 'x=$(case $A in (yes|y)) printf ok;; *) printf no;; esac)'
want_clean "paren case patterns"

run_cmd 'x=$(type yes)'
want_clean "descriptive-only type probe"

cat > "$WORK/quoted-heredoc.sh" <<'FIX'
cat <<'EOF'
$(yes)
EOF
FIX
run_file "$WORK/quoted-heredoc.sh"
want_clean "quoted-heredoc body is literal"

cat > "$WORK/heredoc-content.sh" <<'FIX'
echo $(cat <<EOF
yes
EOF
)
FIX
run_file "$WORK/heredoc-content.sh"
want_clean "heredoc content line is not a command"

run_cmd 'x=$(for f in a b; do printf "%s" "$f"; done)'
want_clean "for loop"

run_cmd 'x=$(select s in a b; do echo "$s"; break; done)'
want_clean "select loop"

run_cmd 'echo hi >yes'
want_clean "redirect into file named yes"

run_cmd 'x=$(echo a#b)'
want_clean "hash inside word"

run_cmd 'echo foo#bar'
want_clean "hash glued to word"

run_cmd 'x=$(echo "a # not comment")'
want_clean "hash inside double quotes"

run_cmd 'echo \$(yes)'
want_clean "escaped dollar-paren is literal"

run_cmd 'x=$(true)'
want_clean "trivial substitution"

# ---------------------------------------------------------------------------
hdr "scope pin: bare yes with no substitution stays allowed"
run_cmd 'yes | head -n 1'
want_clean "top-level bounded pipe (nothing collected)"

run_cmd 'if yes; then echo hi; fi'
want_clean "bare yes command (no collection)"

# ---------------------------------------------------------------------------
hdr "input paths and usage errors"
run_cmd 'x=$(yes)'
if [ "$RC" -eq 3 ]; then ok "argv input path works"; else bad "argv input path works" "exit $RC"; fi

printf '%s' 'x=$(yes)' > "$WORK/argv-eq.sh"
run_file "$WORK/argv-eq.sh"
if [ "$RC" -eq 3 ]; then ok "file input path works"; else bad "file input path works" "exit $RC"; fi

OUT="$(printf '%s' 'x=$(yes)' | "$GUARD" check --stdin 2>&1)"; RC=$?
if [ "$RC" -eq 3 ]; then ok "stdin input path works"; else bad "stdin input path works" "exit $RC"; fi

OUT="$("$GUARD" check --file "$WORK/does-not-exist" 2>&1)"; RC=$?
want_rc 2 "missing file is a usage error"

printf 'x=$(yes)\xff\xfe' > "$WORK/bad-utf8.sh"
OUT="$("$GUARD" check --file "$WORK/bad-utf8.sh" 2>&1)"; RC=$?
want_rc 2 "undecodable file is a usage error (never exit 1)"

OUT="$("$GUARD" 2>&1)"; RC=$?
want_rc 2 "missing subcommand is a usage error"

# CI must actually run this suite: verify-fast.sh --parse-only (all CI
# calls on this path) never reaches it, so it needs its own ci.yml step
# (same pattern as test_review_route.sh).
grep -q 'test_shell_subst_guard.sh' .github/workflows/ci.yml \
  && ok "ci.yml runs the shell-subst guard suite" || bad "ci.yml runs the shell-subst guard suite"

# ---------------------------------------------------------------------------
hdr "the guard never executes what it scans"
if grep -nE 'subprocess|os\.system|os\.exec|os\.popen|os\.spawn|eval\(|exec\(|__import__|import os' "$GUARD" \
    | grep -v 'no process-spawn' >/dev/null; then
  bad "no process-spawn primitive in guard" "$(grep -nE 'subprocess|os\.system|os\.exec|os\.popen|os\.spawn|eval\(|exec\(|__import__|import os' "$GUARD" | head -3)"
else
  ok "no process-spawn primitive in guard"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
