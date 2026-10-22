#!/usr/bin/env bash
# =====================================================================================
# case_e77a1e99-red-main-mutation-gate.sh -- proof that test_red_main_poll.sh is not vacuous.
#
# The poller's whole value is that it refuses to guess: a SHA-keyed signature would
# open one card per commit; an order- or case-sensitive hash would rotate the key on
# noise; an unreadable board read as "no incident" would manufacture a duplicate per
# tick; an unobservable repo read as green would file a partial sweep as a clean bill
# of health; a never-ran verdict read as red would send a human chasing a billing
# block as a code breakage. Every one of those refusals is a limb a reasonable person
# would simplify away, and each simplification leaves a suite that still passes unless
# a case is specifically pinned to it.
#
# The board read has the same shape of risk: a query that lets a quiet incident card
# age out of the default 500-row window, a full page read as "no incident", an HTTP
# error whose body happens to parse, or an empty body, each manufactures a duplicate
# card per tick. The host wrapper adds its own: a record per tick instead of a bounded
# cadence, a gap that rides on a draft proposal, an outstanding finding that exits
# clean and reads as healthy.
#
# Each mutation below removes exactly ONE limb from a staged copy of the poller (or of
# the host wrapper, TARGET=ops/case_e77a1e99/red_main_poll_check.sh) and
# asserts that the NAMED cases go red. The unmutated copy is asserted green FIRST, in
# the same staging directory: "the mutated suite failed" is unattributable without it.
#
# Mutations are single-LINE edits (mutate_line): the target is a distinctive
# substring of one source line, the replacement is the whole new line.
# Multi-line literals are shell-quoting landmines; one line cannot hide one.
#
#   ./verification/case_e77a1e99-red-main-mutation-gate.sh
#
# Exit 0 = every mutation was detected by the case that claims to cover it.
# =====================================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
SRC="$ROOT/red_main_poll.sh"
SUITE="$ROOT/test_red_main_poll.sh"
[ -f "$SRC" ]   || { echo "FATAL: cannot find red_main_poll.sh above $HERE" >&2; exit 4; }
[ -f "$SUITE" ] || { echo "FATAL: cannot find test_red_main_poll.sh above $HERE" >&2; exit 4; }
[ -f "$ROOT/lib/paperclip_api_allowlist.sh" ] || { echo "FATAL: cannot find lib/paperclip_api_allowlist.sh above $HERE" >&2; exit 4; }
[ -x "$ROOT/gh_ci_status.sh" ] || { echo "FATAL: gh_ci_status.sh missing above $HERE -- run bash scripts/sync-toolkit.sh first (INSTALL.md §3)" >&2; exit 4; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
WRAP_REL="ops/case_e77a1e99/red_main_poll_check.sh"
PF_REL="ops/case_4ea7d92e/lib/post_finding.sh"
DOC_REL="ops/case_e77a1e99/INSTALL.md"
for f in "$WRAP_REL" "$PF_REL" "$DOC_REL"; do
  [ -f "$ROOT/$f" ] || { echo "FATAL: cannot find $f above $HERE" >&2; exit 4; }
done
cp "$SRC" "$STAGE/red_main_poll.sh"
cp "$SUITE" "$STAGE/test_red_main_poll.sh"
cp "$ROOT/gh_ci_status.sh" "$STAGE/gh_ci_status.sh"
mkdir -p "$STAGE/ops/case_e77a1e99" "$STAGE/ops/case_4ea7d92e/lib" "$STAGE/lib"
cp "$ROOT/$WRAP_REL" "$STAGE/$WRAP_REL"
cp "$ROOT/$PF_REL" "$STAGE/$PF_REL"
cp "$ROOT/$DOC_REL" "$STAGE/$DOC_REL"
# The poller fail-closes without its allowlist dep; stage it unmodified so
# the baseline is green and kills stay attributable to the mutated copy.
cp "$ROOT/lib/paperclip_api_allowlist.sh" "$STAGE/lib/"
chmod +x "$STAGE"/*.sh "$STAGE/$WRAP_REL"

pass=0; fail=0
red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }

echo "=== baseline: the UNMUTATED copy passes in this same staging directory ==="
base_out="$("$STAGE/test_red_main_poll.sh" 2>&1)" && base_rc=0 || base_rc=$?
if [ "$base_rc" = "0" ]; then
  grn "  PASS  unmutated copy: suite green in $STAGE"; pass=$((pass+1))
else
  red "  FAIL  unmutated copy is ALREADY RED (rc=$base_rc) -- staging is broken."
  printf '%s\n' "$base_out" | grep -E '^\s+FAIL' || true
  exit 1
fi
if grep -qE '^0 passed' <<<"$base_out"; then
  red "  FAIL  baseline ran ZERO assertions"; exit 1
else
  grn "  PASS  baseline ran $(printf '%s' "$base_out" | grep -oE '[0-9]+ passed' | head -1)"; pass=$((pass+1))
fi
echo

mutate_line() {
  local name="$1" old="$2" new="$3"; shift 3
  local WHICH="${WHICH:-}" KEEP="${KEEP:-}" TARGET="${TARGET:-red_main_poll.sh}"
  if [ "${KEEP:-}" != "1" ]; then
    cp "$SRC" "$STAGE/red_main_poll.sh"; cp "$ROOT/$WRAP_REL" "$STAGE/$WRAP_REL"
  fi
  chmod +x "$STAGE/red_main_poll.sh" "$STAGE/$WRAP_REL"
  OLD="$old" NEW="$new" F="$STAGE/$TARGET" WHICH="$WHICH" python3 - <<'PY' || { red "  FAIL  $name: mutation did not apply"; fail=$((fail+1)); return; }
import os, sys
f = os.environ["F"]; old = os.environ["OLD"]; new = os.environ["NEW"]
lines = open(f).read().split("\n")
hits = [i for i, l in enumerate(lines) if old in l]
which = os.environ.get("WHICH", "")
if which != "":
    hits = [hits[int(which)]] if int(which) < len(hits) else []
if len(hits) != 1:
    print("target on %d lines, expected exactly 1" % len(hits), file=sys.stderr)
    sys.exit(1)
lines[hits[0]] = lines[hits[0]].replace(old, new, 1)
open(f, "w").write("\n".join(lines))
PY
  local out rc=0
  out="$("$STAGE/test_red_main_poll.sh" 2>&1)" || rc=$?
  printf '%s\n' "$out" | grep -E '^  (ok|FAIL)' | sed 's/^/    [suite] /' >&2
  if [ "$rc" = "0" ]; then
    red "  FAIL  $name: suite stayed GREEN with this limb removed"
    fail=$((fail+1)); return
  fi
  local case_name ok=1
  for case_name in "$@"; do
    if grep -qF "FAIL $case_name" <<<"$out"; then :; else
      red "  FAIL  $name: red, but NOT on the covering case:"
      red "        expected: $case_name"
      ok=0
    fi
  done
  if [ "$ok" = "1" ]; then grn "  PASS  $name"; pass=$((pass+1)); else fail=$((fail+1)); fi
}

echo "=== each limb of the poller, removed one at a time ==="

mutate_line "signature keyed on head SHA instead of failing set" \
  'sig="$(sig_for "$key")"' \
  'sig="$(sig_for "$head")"' \
  "same failing set at different heads hashes to one signature"

mutate_line "failing checks hashed in API order, not sorted" \
  'LC_ALL=C sort "$tmp"' \
  'cat "$tmp"' \
  "reversed run order keeps the signature"

mutate_line "failing checks hashed case-sensitively" \
  'ascii_downcase' \
  'tostring' \
  "capitalised job name keeps the signature"

mutate_line "check runs paged at the moving branch ref, not the pinned commit" \
  'commits/$resolved/check-runs' \
  'commits/main/check-runs' \
  "moving-branch red records sigSource:checks" \
  "moving-branch red names the page-2 failure at the pinned commit" \
  "moving-branch red reports the pinned commit, not the moving ref"

mutate_line "CI reader re-resolves main instead of the poll's pinned commit" \
  '"$CI_STATUS_BIN" --quiet "$1" "${2:-main}"' \
  '"$CI_STATUS_BIN" --quiet "$1" main' \
  "racing-branch red is reported (exit 1)" \
  "racing-branch red keys on the check set read at the verdict's commit" \
  "racing-branch red reports the commit the verdict was read at"

mutate_line "failed resolve of main falls back to the moving ref" \
  'if ! pin="$(resolve_main "$slug")"; then' \
  'pin="$(resolve_main "$slug")" || pin="main"; if false; then' \
  "unresolvable main exits 3" \
  "unresolvable main emits no red entry" \
  "unresolvable main names the blind spot on stderr"

# Occurrence 0 of this anchor is the allowlist-lib guard (fail-closed on a
# missing dep); occurrence 1 is the snapshot board-read refusal under test.
WHICH=1 mutate_line "unreadable board asserts incidentExists:false" \
  'exit 3; }' \
  ': > "$titles_file"; }' \
  "snapshot exits 3 when the board is unreadable" \
  "snapshot prints no fragment when the board is unreadable"

mutate_line "never-ran verdict read as a red build" \
  "printf 'UNKNOWN %s @ main: not started" \
  "printf 'ENTRY {\"repo\":\"MUTANT\"}' #" \
  "never-ran exits 3, not 1" \
  "never-ran drafts no repo-lead incident (billing block is not a code red)" \
  "never-ran says not-started on stderr"

mutate_line "red dominates unknown in the exit code" \
  'then printf '"'"'3'"'"'' \
  'then printf '"'"'9'"'"'' \
  "green plus one blind repo exits 3 (incompleteness dominates)" \
  "red plus one blind repo exits 3 (incompleteness dominates the red)"

mutate_line "status-sourced red keys on the empty set, not the reason" \
  'if [[ -n "$key" ]]; then' \
  'if true; then' \
  "status-sourced red records sigSource:reason"

mutate_line "ENTRY JSON pretty-printed instead of compact" \
  'jq -cn --arg repo "$slug"' \
  'jq -n --arg repo "$slug"' \
  "propose drafts nothing for a tracked incident (exit 0, empty)" \
  "draft carries the contract tag for coalescing"

KEEP=1; WHICH=1 mutate_line "repo validation only inside the poll subshell (snapshot loop)" \
  'valid_repo "$slug" || die' \
  'valid_repo "$slug" || :' \
  "path-traversal repo is refused"
KEEP=1; WHICH=0 mutate_line "repo validation only inside the poll subshell (subshell backstop)" \
  'valid_repo "$slug" || die' \
  'valid_repo "$slug" || :' \
  "path-traversal repo is refused"
KEEP=;

echo "=== the board read, one limb at a time ==="

mutate_line "board read drops the server-side q filter" \
  '&q=red-main:v1' \
  '' \
  "board read filters server-side with q=red-main:v1"

mutate_line "board read drops the limit (default 500-row window)" \
  '&limit=${BOARD_LIMIT}' \
  '' \
  "board read asks for limit=1000 (the hard cap)" \
  "a FULL page (1000 rows) is treated as truncated: exit 3, no fragment"

mutate_line "a full page is searched instead of refused" \
  'if [[ "$n" -ge "$BOARD_LIMIT" ]]; then' \
  'if false; then' \
  "a FULL page (1000 rows) is treated as truncated: exit 3, no fragment"

mutate_line "an HTTP error status is trusted when the body parses" \
  '[[ "$code" == "200" ]] || { echo "red_main_poll: board' \
  'true || { echo "red_main_poll: board' \
  "HTTP 500 with a parseable [] body is unreadable, not empty"

mutate_line "a non-list body reads as an empty board" \
  'error("unrecognised board shape")' \
  '[]' \
  "a 200 whose body is not a list of issues is unreadable"

mutate_line "an empty body reads as an empty board" \
  '[[ -n "$rows" ]] || return 1' \
  'true' \
  "an empty board body is unreadable, not 'no incident'"

mutate_line "backlog incident cards are invisible to the dedupe" \
  'BOARD_STATUSES="backlog,todo' \
  'BOARD_STATUSES="todo' \
  "board read covers every non-terminal status, backlog included"

mutate_line "failing checks ordered by a punctuation-blind collation" \
  'LC_ALL=C sort "$tmp"' \
  'LC_ALL=C sort -d "$tmp"' \
  "signature is sha1 over byte-ordered names (independent oracle)"

echo
echo "=== the host wrapper, one limb at a time ==="

export TARGET="$WRAP_REL"

mutate_line "an unchanged finding is recorded on every tick" \
  'if [[ "$last_digest" == "$digest" && "$age" -lt "$REPOST_SECONDS" ]]; then' \
  'if false; then' \
  "two more ticks on the same red record nothing new and still write nothing (cadence bound)"

mutate_line "a changed finding is recorded with no floor" \
  'if [[ "$last_digest" != "$digest" && "$age" -lt "$CHANGE_FLOOR_SECONDS" ]]; then' \
  'if false; then' \
  "a changed digest 60s after the last record is held by the floor"

mutate_line "a clean tick keeps the cadence state" \
  'rm -f "${STATE_DIR}"/*.last "${PROPOSAL_DIR}"/*.md' \
  ':' \
  "a clean tick clears the cadence state and the outstanding proposals" \
  "red after green is recorded afresh, not swallowed by the old bound"

mutate_line "an outstanding finding exits clean" \
  'exit 1  # outstanding-findings: never exit clean with proposals on disk' \
  'exit 0  # outstanding-findings: never exit clean with proposals on disk' \
  "first red tick records one proposal, fails the unit, writes nothing to the board" \
  "an unobservable repo records a [red-main-poll-gap] proposal and fails the unit"

mutate_line "a blind repo beside a red is dropped behind the drafts" \
  "if grep -q '^UNKNOWN ' <<<\"\$err\"; then record_gap; fi" \
  ':' \
  "red plus a blind repo records BOTH a draft proposal and a gap proposal"

mutate_line "a tick keeps proposals of kinds it did not see" \
  'rm -f "${STATE_DIR}/${_sweep_kind}.last" "${PROPOSAL_DIR}/${_sweep_kind}-"*.md' \
  ':' \
  "a draft-only tick sweeps the stale error proposal and stamp"

# Proposal records are unconditional (no tag lookup collapses them), so the
# shared tag changes nothing countable -- only the distinct-tags case, which
# pins the two proposals' identities, can see it.
mutate_line "the gap finding shares the draft tag" \
  'TAG_GAP="[red-main-poll-gap]"' \
  'TAG_GAP="[red-main-poll]"' \
  "the draft proposal and the gap proposal carry distinct tags"

mutate_line "the draft digest hashes the text, not the key set" \
  "if [[ -n \"\$keys\" ]]; then printf '%s\\n' \"\$keys\" | sha1sum | cut -c1-40; else body_digest; fi" \
  'body_digest' \
  "the same keys in a different repo order are one finding"

mutate_line "the handoff drops the snapshot entries" \
  "rows=\"\$(jq -c '.redMains' \"\$snap_out\")\"" \
  "rows='[]'" \
  "red envelope: snapshot exit 1, one entry" \
  "the entry carries the contract dedupe tag"

mutate_line "the handoff carries drafts text beside the snapshot fields" \
  "redMains: \$rows}' > \"\$tmp\"; then" \
  "redMains: \$rows, drafts: \"x\"}' > \"\$tmp\"; then" \
  "the envelope holds the snapshot fields and no drafts text"

mutate_line "a snapshot with no array is handed off clean" \
  "    snap_rc=3" \
  "    :" \
  "a snapshot with no array is an unknown handoff, never a clean tick"

mutate_line "a repo listed twice is polled twice" \
  "awk '!seen[\$0]++'" \
  "cat" \
  "a repo listed twice yields one handoff row"

mutate_line "the handoff reports a blind snapshot as clean" \
  "|| snap_rc=\$?" \
  "|| snap_rc=0" \
  "an unobservable repo is exit 3 in the envelope"

mutate_line "the handoff is owner-only (0600), so the agent mount cannot read it" \
  "chmod 0644" \
  "chmod 0600" \
  "envelope and directory are readable by the agent mount (0644 and 0755)"

mutate_line "the handoff measuredAt is a constant, not this tick" \
  "measured_at=\"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" \
  "measured_at=\"2026-10-07T00:00:00Z\"" \
  "measuredAt is this tick's own clock (within two minutes)"

unset TARGET

echo
echo "$pass passed, $fail failed"
[ "$fail" = "0" ] || exit 1
exit 0
