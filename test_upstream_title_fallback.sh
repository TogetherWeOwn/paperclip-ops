#!/usr/bin/env bash
# Title-fallback acceptance: upstream_gate.sh eval_our_review (PR #106 follow-up acceptance).
#
# Test-only handoff for PR #106 (fix/review-verdict-title-fallback). Pins the
# missing/empty-title -> first-summary-line fallback WITHOUT implementing it.
#
# Desired (PR #106): when output.title is missing/null/empty/whitespace-only,
# accept iff output.summary's FIRST line is exactly "5/5". A present non-5/5
# title refuses even when the summary mentions 5/5 (no fallback, no incidental
# substring pass). Any other shape FAILs our-review.
#
# NOTE on direction: upstream is currently OVER-permissive (it passes when the
# substring "5/5" appears anywhere in the output JSON), while the three other
# consumers are UNDER-permissive (they ignore summary entirely). So here the
# *refuse* variants are RED on main (current code passes them) and the *pass*
# cases are green controls. After the fix, all cases agree.
#
# RED expectation on main: the 9 refuse variants FAIL (current substring check
# passes them); the 5 pass cases + 2 fail-controls PASS. GREEN once the
# exact-title-or-exact-first-line check lands.
#
# Hermetic: extracts eval_our_review byte-identically from upstream_gate.sh at
# runtime (no copy-paste; a rename fails loudly), then feeds it minimal
# layout dirs (pr.json + check_runs.json only). No network, no gh, no token.
#
# Wiring pointer for the Land owner (do NOT wire here; test-only card):
#   ./test_upstream_title_fallback.sh
#   Add a ci.yml step running ./test_upstream_title_fallback.sh (next to the
#   ./test_upstream_gate.sh step) plus the offline-CI contract pin
#   (preview --propose, apply --write).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${UPSTREAM_GATE_SH:-$HERE/upstream_gate.sh}"

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ $# -gt 1 ] && printf '        %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -x "$TOOL" ] || { echo "no executable upstream_gate.sh at $TOOL" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

# Extract the shipped function verbatim; never a copy. If the function is
# renamed or gains dependencies, this fails loudly instead of testing a stale copy.
EVAL_SRC="$(sed -n '/^eval_our_review()/,/^}/p' "$TOOL")"
[ -n "$EVAL_SRC" ] || { echo "eval_our_review not found in $TOOL" >&2; exit 2; }
eval "$EVAL_SRC"
[ "$(type -t eval_our_review)" = "function" ] || { echo "eval_our_review did not load" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SHA="f5bca33a44fb20d90972a0d15db880e0e3129440"

# mk_layout <dir> <output_json>
# Minimal our-review layout: one completed/success Paperclip Review run.
mk_layout() {
  local dir="$1" output="$2"
  mkdir -p "$dir"
  jq -n --arg sha "$SHA" '{title:"t", head:{sha:$sha}}' > "$dir/pr.json"
  jq -n --argjson out "$output" \
    '{total_count:1, check_runs:[{name:"Paperclip Review", status:"completed",
      conclusion:"success", completed_at:"2026-10-06T03:00:00Z",
      started_at:"2026-10-06T03:00:00Z", id:1001, output:$out}]}' > "$dir/check_runs.json"
}

# expect_pass <name> <output_json>  — our-review must print PASS
expect_pass() {
  local name="$1" output="$2" dir="$WORK/p$PASS$FAIL"
  mk_layout "$dir" "$output"
  local out; out="$(eval_our_review "$dir" 2>&1)"
  if grep -q "^PASS.*our-review" <<<"$out"; then ok "$name";
  else bad "$name" "expected PASS, got: $out"; fi
}

# expect_fail <name> <output_json>  — our-review must print FAIL
expect_fail() {
  local name="$1" output="$2" dir="$WORK/f$PASS$FAIL"
  mk_layout "$dir" "$output"
  local out; out="$(eval_our_review "$dir" 2>&1)"
  if grep -q "^FAIL.*our-review" <<<"$out"; then ok "$name";
  else bad "$name" "expected FAIL, got: $out"; fi
}

hdr "pass: missing/empty title + exact first-line 5/5 (green controls here)"
expect_pass "missing title, summary 5/5" '{"summary":"5/5"}'
expect_pass "empty title, summary 5/5" '{"title":"","summary":"5/5"}'
expect_pass "null title, summary 5/5" '{"title":null,"summary":"5/5"}'
expect_pass "whitespace title, summary 5/5" '{"title":"   ","summary":"5/5"}'
expect_pass "missing title, multiline first-line 5/5" '{"summary":"5/5\n## details"}'
expect_pass "missing title, CRLF first-line 5/5" '{"summary":"5/5\r\n## details"}'
expect_pass "title 5/5 with heading summary" '{"title":"5/5","summary":"## Paperclip Review — 5/5"}'

hdr "refuse: whitespace/adverse/second-line/suffix + incidental (RED on main)"
expect_fail "leading space" '{"summary":" 5/5"}'
expect_fail "trailing space" '{"summary":"5/5 "}'
expect_fail "suffix" '{"summary":"5/5 approved"}'
expect_fail "prefix" '{"summary":"Score: 5/5"}'
expect_fail "second line" '{"summary":"Review complete\n5/5"}'
expect_fail "typical heading" '{"summary":"## Paperclip Review — 5/5"}'
expect_fail "present reviewed title with 5/5 summary" '{"title":"reviewed","summary":"5/5"}'
expect_fail "present 4/5 title with 5/5 summary" '{"title":"4/5","summary":"5/5"}'
expect_fail "incidental mention" '{"title":"reviewed","summary":"we saw 5/5 before, now 4/5"}'

hdr "controls: no 5/5 anywhere still fails"
expect_fail "no score" '{"title":"reviewed","summary":"looks fine"}'
expect_fail "empty output" '{"title":"","summary":""}'

printf '\n\033[1msummary\033[0m\n  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
