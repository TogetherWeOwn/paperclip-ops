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
#      assignment-prefixed, leading-redirect (fd-first, stdin-first,
#      assignment-then-redirect, var-fd `{fd}>`), pathed, backslash-escaped
#      and line-continued spellings, plus interiors of `${...}`/`$((...))`
#      and `case` bodies (bare, extglob and quoted-paren patterns). A `#`
#      inside double quotes or a `${#..}` length expansion never hides a
#      substitution; neither does a `case` word in argument position, a
#      paren inside double quotes, or a `$'...'` escape. ANSI-C hex, octal
#      (mod-256 wrapped) and unicode escapes all decode before the `yes`
#      comparison. Quotes/`#` in unquoted heredoc bodies, arithmetic before a
#      command, and newline-separated loop lists hide nothing; neither does
#      in-span quoting inside a heredoc body (glued, quoted-paren,
#      multi-line, case-pattern and brace-paren forms), a `for ((...))`
#      prelude, or a heredoc operator mentioned in a comment. `((` without
#      a matching arithmetic `))` reparses as subshells, exposing commands;
#      a matching arithmetic close still keeps yes-named variables inert.
#   3. Fail-closed pins: `$(yes | head -n 1)`, `$(yes --help)` and
#      `$(command -v yes)` are refused even though bounded/descriptive.
#      The gate cannot verify the bound or model per-flag semantics, so it
#      refuses and directs the author to `printf`/`type`. Never silently
#      weakened: these tests fail if anyone "helpfully" allows them.
#   4. Safe fixed-output checks stay allowed: the `printf 'yes'`/`printf
#      'no'` boolean idiom, `yes` as an argument/literal, comments (full-line,
#      trailing and inside substitutions), arithmetic, parameter expansion,
#      `[[ ]]` operands, `case` patterns, the descriptive-only `type yes`,
#      quoted-heredoc bodies (full, partial-quote and backslash delimiters)
#      and heredoc content lines, redirects into a file named `yes`, loop
#      lists (including `do`/`yes` as list words), and a literal `yes`
#      argument after arithmetic. A guard that fires on these gets muted,
#      which is the same as having no guard.
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

run_cmd 'x=$(n=$((1+1)); yes)'
want_finding "arithmetic before yes refuses"

# ---------------------------------------------------------------------------
hdr "arithmetic openers without a matching double close reparse as subshells"
run_cmd 'x=$(((1)); yes)'
want_finding "arithmetic-looking subshell then yes refuses"

run_cmd 'x=$((yes);yes)'
want_finding "single subshell close then yes refuses"

run_cmd 'x=$(( (1) ); yes)'
want_finding "spaced subshell then yes refuses"

run_cmd 'y=$(echo $(((1)); yes))'
want_finding "nested arithmetic-looking subshell refuses"

run_cmd 'echo "value=$((echo hi); yes)"'
want_finding "double-quoted arithmetic-looking subshell refuses"

run_cmd 'x=$((echo hi);(yes))'
want_finding "adjacent final closes are not sufficient for arithmetic"

run_cmd 'x=$((echo hi); yes); z=$((1+2))'
want_finding "later arithmetic cannot supply the missing double close"

run_cmd 'x=`((echo hi); yes)`'
want_finding "backtick arithmetic-looking subshell refuses"

run_cmd 'x=$( ((echo hi); yes) )'
want_finding "bare double-open subshell inside dollar-paren refuses"

run_cmd 'x=$((echo hi); y\
es)'
want_finding "continued yes after arithmetic-looking subshell refuses"

cat > "$WORK/heredoc-arith-subshell.sh" <<'FIX'
cat <<EOF
don't $((echo hi); y'es')
EOF
FIX
run_file "$WORK/heredoc-arith-subshell.sh"
want_finding "heredoc arithmetic-looking subshell preserves quoting"

run_cmd 'x=$(( (1) + 2 ))'
want_clean "nested parentheses in genuine arithmetic"

run_cmd 'x=$((yes+1))'
want_clean "yes variable in compact genuine arithmetic"

run_cmd 'x=$(echo $(( (1) + 2 )) yes)'
want_clean "argument after genuine nested arithmetic"

run_cmd 'x=$((echo hi); printf yes)'
want_clean "arithmetic-looking subshell with printf"

run_cmd 'x=`((echo hi); printf yes)`'
want_clean "backtick subshell with printf"

run_cmd 'x=$( ((yes+1)) )'
want_clean "yes variable in bare arithmetic command"

run_cmd 'x=$(( (1) + 2 )); yes | head -n 1'
want_clean "genuine arithmetic then top-level bounded yes"

run_cmd '((echo hi); yes)'
want_clean "arithmetic-looking subshell outside substitution is out of scope"

cat > "$WORK/arith-continuation.sh" <<'FIX'
x=$(( (yes) )\
)
FIX
run_file "$WORK/arith-continuation.sh"
want_clean "line continuation between arithmetic closers"

cat > "$WORK/arith-continuation-subshell.sh" <<'FIX'
x=$((echo hi); (yes))
FIX
run_file "$WORK/arith-continuation-subshell.sh"
want_finding "subshell after arithmetic-looking opener still refuses"

cat > "$WORK/quoted-heredoc-arith-subshell.sh" <<'FIX'
cat <<'EOF'
$((yes);yes)
EOF
FIX
run_file "$WORK/quoted-heredoc-arith-subshell.sh"
want_clean "quoted heredoc arithmetic-looking substitution is literal"

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
hdr "quoted parens never desync span boundaries"
run_cmd 'x=$(echo "a)b"; yes)'
want_finding "quoted close-paren before yes refuses"

run_cmd 'x=$(echo "step (1) done"; yes)'
want_finding "quoted paren pair before yes refuses"

run_cmd 'x=$(echo "("); yes | head -n 1'
want_clean "quoted open-paren plus top-level bounded yes"

# ---------------------------------------------------------------------------
hdr "case as an argument hides nothing"
run_cmd 'x=$(echo case; yes)'
want_finding "bare case argument refuses"

run_cmd 'x=$(printf '"'"'%s'"'"' case; yes)'
want_finding "case after printf refuses"

run_cmd 'x=$(echo case in a; yes)'
want_finding "case/in as arguments refuses"

run_cmd 'x=$(echo a=b case in a; yes)'
want_finding "case after argument assignment refuses"

run_cmd 'x=$(echo case)'
want_clean "case as sole argument"

run_cmd 'x=$(echo esac yes)'
want_clean "esac as argument hides its neighbor"

run_cmd 'x=$(echo if yes)'
want_clean "if as argument hides its neighbor"

run_cmd 'x=$(case $A in case) echo;; *) yes;; esac)'
want_finding "case as pattern word refuses"

run_cmd 'x=$(FOO=1 case $A in *) yes;; esac)'
want_finding "assignment-prefixed case refuses (fail-closed)"

# ---------------------------------------------------------------------------
hdr "line continuations are deleted before scanning"
# NOTE: the fixtures below contain literal backslash-newline pairs inside
# single quotes; bash passes them through to the guard verbatim.
run_cmd 'x=$(ye\
s)'
want_finding "continued command word refuses"

run_cmd 'x=$(FOO=1 ye\
s)'
want_finding "continued word after assignment refuses"

run_cmd 'x=$\
(yes)'
want_finding "continued dollar-paren refuses"

run_cmd 'x=$("ye\
s")'
want_finding "continued word inside double quotes refuses"

# ---------------------------------------------------------------------------
hdr "dollar-single-quote escapes hold quote state"
cat > "$WORK/dollar-single-top.sh" <<'EOF'
a=$'\''; x=$(yes)
EOF
run_file "$WORK/dollar-single-top.sh"
want_finding "escaped quote before substitution refuses"

cat > "$WORK/dollar-single-body.sh" <<'EOF'
x=$(echo $'a\'b'; yes)
EOF
run_file "$WORK/dollar-single-body.sh"
want_finding "escaped quote inside body refuses"

cat > "$WORK/dollar-single-expansion.sh" <<'EOF'
x=${A:-$'a\'b'}; y=$(yes)
EOF
run_file "$WORK/dollar-single-expansion.sh"
want_finding "escaped quote inside expansion refuses"

cat > "$WORK/dollar-single-comment.sh" <<'EOF'
x=$(echo $'a\' b #x'; yes)
EOF
run_file "$WORK/dollar-single-comment.sh"
want_finding "hash after escaped quote refuses"

# ---------------------------------------------------------------------------
hdr "var-fd redirects never clear command position"
run_cmd 'x=$({fd}>/dev/null yes)'
want_finding "var-fd stdout redirect refuses"

run_cmd 'x=$({fd}>&1 yes)'
want_finding "var-fd duplication refuses"

run_cmd 'x=$({9}>/dev/null yes)'
want_finding "digits var-fd refuses"

run_cmd 'x=$(echo {fd})'
want_clean "brace word without redirect"

run_cmd 'x=$(echo {fd} >/dev/null)'
want_clean "spaced brace word is a literal argument"

# ---------------------------------------------------------------------------
hdr "extglob and quoted parens in case patterns"
run_cmd 'x=$(case $A in @(yes|no)) printf ok;; *) yes;; esac)'
want_finding "extglob pattern refuses"

run_cmd 'x=$(case $A in ")") printf ok;; *) yes;; esac)'
want_finding "quoted-paren pattern refuses"

run_cmd 'x=$(case $A in *(a|b)) printf ok;; *) yes;; esac)'
want_finding "star-extglob pattern refuses"

run_cmd 'x=$(case $A in !(a)) printf ok;; *) yes;; esac)'
want_finding "bang-extglob pattern refuses"

run_cmd 'x=$(case $A in @(a|b)|yes) printf ok;; *) printf no;; esac)'
want_clean "yes as pattern alternative after extglob"

# ---------------------------------------------------------------------------
hdr "ANSI-C escapes decode to yes"
run_cmd 'x=$($'"'"'\x79es'"'"')'
want_finding "hex escape refuses"

run_cmd 'x=$($'"'"'\171es'"'"')'
want_finding "octal escape refuses"

run_cmd 'x=$($'"'"'\u0079es'"'"')'
want_finding "short unicode escape refuses"

run_cmd 'x=$($'"'"'\U00000079es'"'"')'
want_finding "long unicode escape refuses"

run_cmd 'x=$($'"'"'\571es'"'"')'
want_finding "wrapped octal escape refuses"

# ---------------------------------------------------------------------------
hdr "top-level case tracking never swallows following commands"
run_cmd 'case $x in a) v=$(date);; esac; yes | head -1'
want_clean "top-level case plus bare bounded yes"

run_cmd 'echo case; for f in *; do v=$(date); done; yes | head -1'
want_clean "case word plus loop plus bare bounded yes"

run_cmd 'x=$(if case $A in *) yes;; esac; then echo; fi)'
want_finding "if-case nesting refuses"

run_cmd 'x=$(case $A in a) case $B in *) yes;; esac;; *) echo;; esac)'
want_finding "nested case in body refuses"

run_cmd 'x=$(if (echo) then yes; fi)'
want_finding "then after paren-close refuses"

# ---------------------------------------------------------------------------
hdr "do, loop lists and for-arith preludes"
cat > "$WORK/for-newline-do.sh" <<'EOF'
x=$(for i in a b
do yes
done)
EOF
run_file "$WORK/for-newline-do.sh"
want_finding "newline-separated for list refuses"

cat > "$WORK/for-noin-newline-do.sh" <<'EOF'
x=$(for i
do yes
done)
EOF
run_file "$WORK/for-noin-newline-do.sh"
want_finding "for without in refuses at newline do"

cat > "$WORK/for-newline-allow.sh" <<'EOF'
x=$(for i in a b
do printf "%s" "$i"
done)
EOF
run_file "$WORK/for-newline-allow.sh"
want_clean "newline-separated for list with printf"

run_cmd 'x=$(for i do yes; done)'
want_finding "immediate for-i-do refuses"

cat > "$WORK/for-arith-newline-do.sh" <<'EOF'
x=$(for ((i=0;i<1;i++))
do yes
done)
EOF
run_file "$WORK/for-arith-newline-do.sh"
want_finding "for-arith newline do refuses"

run_cmd 'x=$(for ((i=0;i<1;i++)) do yes; done)'
want_finding "for-arith space do refuses"

run_cmd 'x=$(for ((i=0;i<1;i++)); do yes; done)'
want_finding "for-arith semicolon do refuses"

run_cmd 'x=$(for ((i=0;i<1;i++)) do printf "%s" "$i"; done)'
want_clean "for-arith printf body"

run_cmd 'x=$(for i in do yes; do echo $i; done)'
want_clean "do and yes as loop-list words"

run_cmd 'x=$(for i in a do yes; do echo $i; done)'
want_clean "mid-list do word"

run_cmd 'x=$(select i in do yes; do echo $i; break; done)'
want_clean "do and yes as select-list words"

run_cmd 'x=$(select yes in a; do echo; done)'
want_clean "select loop variable named yes"

cat > "$WORK/select-newline-do.sh" <<'EOF'
x=$(select i in a
do yes; break; done)
EOF
run_file "$WORK/select-newline-do.sh"
want_finding "newline-separated select list refuses"

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

cat > "$WORK/heredoc-apostrophe.sh" <<'FIX'
cat <<EOF
don't $(yes)
EOF
FIX
run_file "$WORK/heredoc-apostrophe.sh"
want_finding "heredoc apostrophe hides nothing"

cat > "$WORK/heredoc-apostrophe-bt.sh" <<'FIX'
cat <<EOF
don't `yes`
EOF
FIX
run_file "$WORK/heredoc-apostrophe-bt.sh"
want_finding "heredoc apostrophe hides no backtick"

cat > "$WORK/heredoc-hash.sh" <<'FIX'
cat <<EOF
# not a comment $(yes)
EOF
FIX
run_file "$WORK/heredoc-hash.sh"
want_finding "heredoc hash hides nothing"

cat > "$WORK/heredoc-glued-squote.sh" <<'FIX'
cat <<EOF
$(y'es')
EOF
FIX
run_file "$WORK/heredoc-glued-squote.sh"
want_finding "heredoc glued single-quote refuses"

cat > "$WORK/heredoc-glued-dquote.sh" <<'FIX'
cat <<EOF
$(y"e"s)
EOF
FIX
run_file "$WORK/heredoc-glued-dquote.sh"
want_finding "heredoc glued double-quote refuses"

cat > "$WORK/heredoc-quoted-paren.sh" <<'FIX'
cat <<EOF
$(echo ")"; yes)
EOF
FIX
run_file "$WORK/heredoc-quoted-paren.sh"
want_finding "heredoc quoted paren refuses"

cat > "$WORK/heredoc-ansic-hex.sh" <<'FIX'
cat <<EOF
$($'ye\x73')
EOF
FIX
run_file "$WORK/heredoc-ansic-hex.sh"
want_finding "heredoc ANSI-C hex refuses"

cat > "$WORK/heredoc-wrapped-octal.sh" <<'FIX'
cat <<EOF
$($'\571es')
EOF
FIX
run_file "$WORK/heredoc-wrapped-octal.sh"
want_finding "heredoc wrapped octal refuses"

cat > "$WORK/heredoc-exec-glued.sh" <<'FIX'
cat <<EOF
$(exec y'es')
EOF
FIX
run_file "$WORK/heredoc-exec-glued.sh"
want_finding "heredoc exec-glued refuses"

cat > "$WORK/heredoc-backtick-glued.sh" <<'FIX'
cat <<EOF
`y'es'`
EOF
FIX
run_file "$WORK/heredoc-backtick-glued.sh"
want_finding "heredoc backtick glued refuses"

cat > "$WORK/heredoc-multiline-span.sh" <<'FIX'
cat <<EOF
$(echo begin;
y'es')
EOF
FIX
run_file "$WORK/heredoc-multiline-span.sh"
want_finding "heredoc multi-line span refuses"

cat > "$WORK/heredoc-case-glued.sh" <<'FIX'
cat <<EOF
$(case $A in x) y'es';; esac)
EOF
FIX
run_file "$WORK/heredoc-case-glued.sh"
want_finding "heredoc case-pattern glued refuses"

cat > "$WORK/heredoc-brace-paren-glued.sh" <<'FIX'
cat <<EOF
$(echo ${A:-a)b}; y'es')
EOF
FIX
run_file "$WORK/heredoc-brace-paren-glued.sh"
want_finding "heredoc brace-paren glued refuses"

cat > "$WORK/comment-heredoc.sh" <<'EOF'
# note <<'ZZ'
x=$(yes)
ZZ
EOF
run_file "$WORK/comment-heredoc.sh"
want_finding "comment mentioning quoted heredoc refuses"

cat > "$WORK/trailing-comment-heredoc.sh" <<'EOF'
echo hi # <<'EOT'
x=$(yes)
EOT
EOF
run_file "$WORK/trailing-comment-heredoc.sh"
want_finding "trailing comment mentioning heredoc refuses"

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

run_cmd 'x=$(echo $((1+2)))'
want_clean "echo with arithmetic argument"

run_cmd 'x=$(echo $((1+2)) yes)'
want_clean "arithmetic then literal yes argument"

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

cat > "$WORK/quoted-heredoc-apos.sh" <<'FIX'
cat <<'EOF'
don't $(yes)
EOF
FIX
run_file "$WORK/quoted-heredoc-apos.sh"
want_clean "quoted-heredoc apostrophe body is literal"

cat > "$WORK/partial-quote-heredoc.sh" <<'FIX'
cat <<E'OF'
$(yes)
EOF
FIX
run_file "$WORK/partial-quote-heredoc.sh"
want_clean "partial-quote delimiter body is literal"

cat > "$WORK/backslash-heredoc.sh" <<'FIX'
cat <<\EOF
$(yes)
EOF
FIX
run_file "$WORK/backslash-heredoc.sh"
want_clean "backslash delimiter body is literal"

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
