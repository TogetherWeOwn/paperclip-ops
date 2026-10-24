#!/usr/bin/env bash
# ===========================================================================
# Offline regression suite for upstream_gate.sh (case_3cd3dc9f).
#
# WHY FIXTURES, NOT LIVE API. Every evaluator reads the same standard layout
# (pr.json check_runs.json commits.json comments.json threads.json) that live
# mode fetches. --fixtures feeds that layout from files, so the SAME eval
# code is asserted with no network, no token and no flake.
#
# THE BASELINE IS THE ALL-PASS WORLD. The control fixture passes Gate A and
# `ready` end to end; every case below is one named mutation toward today's
# reality. A tool that always exits 1 would "pass" every red case, so the
# baseline going green is what makes them prove anything.
#
# THE MUTATIONS THIS SUITE EXISTS FOR (each pinned to a measured failure):
#   - latest run red after an old green (fork f5bca33: 09-28 green, 10-06
#     red on ci/verify + vitest 1/2) -> fork-ci FAIL, not "mostly green"
#   - pending run alongside greens -> FAIL, not pass
#   - zero check runs -> REFUSED (exit 2), never pass (case_b730ec29 vacuous green)
#   - Paperclip Review success without "5/5" in output -> FAIL
#   - 2 commits on the mirror branch (measured: mirrors #44/#45/#49/#51)
#     -> single-commit FAIL
#   - "Refs: case_4e5328d7" in the mirror body -> no-internal-ids FAIL
#   - Greptile 5/5 scored on an OLD head -> FAIL (stale)
#   - Greptile 4/5 -> FAIL
#   - unresolved review thread with 5/5 -> FAIL
#
# case_214e61d3 added the pre-push linter, the second-model reputation receipt and
# the parallel-gate `status`/`publish`. Their fixtures live here too: the
# all-pass world now also has a diff (files.json), a maintainer-memory file and
# a clean reputation receipt for the exact upstream text; each new case below
# removes ONE of them. The python engines have their own suites
# (test_upstream_lint.py, test_upstream_reputation.py).
#
# Assertions are on exit status and PASS/FAIL/REFUSED item lines only.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${UPSTREAM_GATE_SH:-$HERE/upstream_gate.sh}"

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ $# -gt 1 ] && printf '        %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -x "$TOOL" ] || { echo "no executable upstream_gate.sh at $TOOL" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Offline maintainer-memory fixtures. paperclipai/paperclip is the small
# world the fixture bodies satisfy; FAKE/upstream uses the retained public
# template/rule fixture, without private historical feedback. The publish dry
# run must satisfy the same source checklist and rules, not a live policy file.
export UPSTREAM_MEMORY_DIR="$WORK/memory"; mkdir -p "$UPSTREAM_MEMORY_DIR"
# Receipts are comments on the mirror PR, trusted only from our own login.
export UPSTREAM_RECEIPT_AUTHOR=rick
python3 - "$HERE" "$UPSTREAM_MEMORY_DIR" <<'PYMEM'
import json, sys
here, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)
import upstream_lint as ul
boxes = ["I have included a thinking path that traces from project context to this change",
         "I have specified the model used (with version and capability details)",
         "I have run tests locally and they pass",
         "All Paperclip CI gates are green",
         "Greptile is 5/5 with no open P2s, recommendations, or follow-ups"]
mem = ul.default_memory("paperclipai/paperclip")
mem["default_base"] = "master"
mem["template"].update(sections=["Thinking Path", "Linked Issues or Issue Description", "What Changed",
                                 "Verification", "Risks", "Model Used", "Checklist"],
                       checkboxes=[ul.norm(b) for b in boxes],
                       pending_ok_at_first_push=["greptile", "ci gates are green"])
ul.save_memory("paperclipai/paperclip", mem, out)
real = json.load(open(here + "/tests/fixtures/upstream-public-template.json"))
real["repo"] = "FAKE/upstream"
real["feedback"] = []
real["template"]["blob_sha"] = "fixtureblob"
for r in real["template"]["section_rules"]:
    r.pop("require_code", None)
ul.save_memory("FAKE/upstream", real, out)
PYMEM

SHA="f5bca33a44fb20d90972a0d15db880e0e3129440"

BODY_FULL='## Thinking Path

> - Paperclip is the open source app people use to manage AI agents for work
> - the heartbeat subsystem keeps task sessions across runs
> - project row touches rotate sessions for no reason
> - This pull request drops the timestamp from the fingerprint
> - The benefit is stable sessions

## Linked Issues or Issue Description

Fixes #15142. A project-row touch alone must not rotate a reusable session.

## What Changed

- drop projectConfigRevisionAt from the session fingerprint

## Verification

- ran the heartbeat workspace session suite: 12 passed
- fork CI green on this head

## Risks

- Low risk: stored sessions reset once after deploy

## Model Used

Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking
'

# The diff of the fixture change: one source file plus its test in the same
# package (the baseline passes the linter's tests/budget/internal checks).
mk_files() { # dir [mutation...]
  local dir="$1"; shift
  local src_patch='@@ -10,2 +10,3 @@\n const a = 1;\n+const fingerprint = hash(a);\n'
  local test_patch='@@ -0,0 +1,3 @@\n+it("keeps the session", () => {\n+  expect(rotate()).toBe(false);\n+});\n'
  local files
  files="$(jq -n --arg sp "$(printf "$src_patch")" --arg tp "$(printf "$test_patch")" '[
    {filename:"server/src/services/heartbeat.ts",status:"modified",additions:1,deletions:0,changes:1,patch:$sp},
    {filename:"server/src/__tests__/heartbeat.test.ts",status:"added",additions:3,deletions:0,changes:3,patch:$tp}]')"
  local m
  for m in "$@"; do case "$m" in
    no-test) files="$(jq '.[0:1]' <<<"$files")" ;;
    leak-in-diff) files="$(jq '.[0].patch += ("+// see TOG-" + "999001\n")' <<<"$files")" ;;
    huge-diff) files="$(jq '.[0].additions = 5000 | .[0].changes = 5000' <<<"$files")" ;;
  esac; done
  printf '%s' "$files" > "$dir/files.json"
}

mk_check_run() { # name status conclusion completed_at [output_json]
  local out="${5:-}"; [ -n "$out" ] || out='{"title":"","summary":""}'
  jq -n --arg n "$1" --arg s "$2" --arg c "$3" --arg t "$4" --argjson o "$out" \
    '{name:$n,status:$s,conclusion:$c,completed_at:$t,started_at:$t,id:(($t|tostring)|length*7+($n|length)),output:$o}'
}

# Build a mirror fixture dir. Args: dir [mutation...]
mk_mirror() {
  local dir="$1"; shift
  local body="$BODY_FULL" ncommits=1 extra_runs="" paperclip_out='{"title":"5/5","summary":"## Paperclip Review — 5/5"}'
  local m
  for m in "$@"; do case "$m" in
    placeholder-body) body='## Thinking Path

> - TBD
' ;;
    tog-in-body) body="$BODY_FULL
Refs: TOG-""999002" ;;
    two-commits) ncommits=2 ;;
    no-review-run) paperclip_out='SKIP' ;;
    review-no-score) paperclip_out='{"title":"reviewed","summary":"looks fine"}' ;;
    review-fallback) paperclip_out='{"title":"","summary":"5/5\nadditional details"}' ;;
    review-fallback-missing) paperclip_out='{"summary":"5/5\nadditional details"}' ;;
    review-title-authoritative) paperclip_out='{"title":"reviewed","summary":"5/5\nadditional details"}' ;;
    review-incidental-score) paperclip_out='{"title":"reviewed","summary":"reviewer notes mention 5/5 inline"}' ;;
    ci-red-latest) extra_runs='RED_LATEST' ;;
    ci-pending) extra_runs='PENDING' ;;
    ci-pending-no-time) extra_runs='PENDING_NO_TIMESTAMP' ;;
    review-pending) extra_runs='REVIEW_PENDING' ;;
    review-pending-no-time) extra_runs='REVIEW_PENDING_NO_TIMESTAMP' ;;
    ci-empty) extra_runs='EMPTY' ;;
  esac; done
  mkdir -p "$dir"
  jq -n --arg sha "$SHA" --arg body "$body" \
    '{title:"fix(heartbeat): keep task sessions across project touches",
      body:$body, draft:true,
      head:{sha:$sha,ref:"fix/session-fingerprint-project-revision-churn"},
      base:{ref:"mirror-base/15142-1c07b590"}}' > "$dir/pr.json"
  { mk_check_run "ci / verify" completed success "2026-09-28T17:35:00Z"
    mk_check_run "ci / e2e" completed success "2026-09-28T17:30:00Z"
    if [ "$paperclip_out" != "SKIP" ]; then
      mk_check_run "Paperclip Review" completed success "2026-09-28T17:40:00Z" "$paperclip_out"
    fi
    case "$extra_runs" in
      RED_LATEST)
        mk_check_run "ci / verify" completed failure "2026-10-06T02:49:57Z"
        mk_check_run "ci / Verify Paperclip Runner (vitest 1/2)" completed failure "2026-10-06T02:46:36Z" ;;
      PENDING)
        mk_check_run "ci / verify" in_progress null "2026-10-06T02:49:57Z" ;;
      PENDING_NO_TIMESTAMP)
        printf '%s\n' '{"name":"ci / verify","status":"queued","conclusion":null,"completed_at":null,"started_at":null,"id":999,"output":{"title":"","summary":""}}' ;;
      REVIEW_PENDING)
        mk_check_run "Paperclip Review" in_progress null "2026-10-06T04:00:00Z" ;;
      REVIEW_PENDING_NO_TIMESTAMP)
        printf '%s\n' '{"name":"Paperclip Review","status":"queued","conclusion":null,"completed_at":null,"started_at":null,"id":999,"output":{"title":"","summary":""}}' ;;
      EMPTY) : ;;
    esac
  } | jq -s '{total_count: length, check_runs: .}' > "$dir/check_runs.json"
  if [ "$extra_runs" = "EMPTY" ]; then echo '{"total_count":0,"check_runs":[]}' > "$dir/check_runs.json"; fi
  if [ "$ncommits" -eq 1 ]; then
    jq -n --arg sha "$SHA" '[{sha:$sha,commit:{message:"fix(heartbeat): keep task sessions across project touches"}}]' > "$dir/commits.json"
  else
    jq -n --arg sha "$SHA" '[{sha:$sha,commit:{message:"fix(heartbeat): keep task sessions"}},{sha:"aaaa",commit:{message:"fixup"}}]' > "$dir/commits.json"
  fi
  echo '[]' > "$dir/comments.json"; echo '[]' > "$dir/threads.json"
  mk_files "$dir"
}

UPSTREAM_BODY_FULL='## Thinking Path

> - Paperclip is the open source app people use to manage AI agents for work
> - the heartbeat subsystem keeps task sessions across runs
> - project row touches rotate sessions for no reason
> - This pull request drops the timestamp from the fingerprint
> - The benefit is stable sessions

## Linked Issues or Issue Description

Fixes #15142.

## What Changed

- drop projectConfigRevisionAt from the session fingerprint

## Verification

- ran the heartbeat workspace session suite: 12 passed
- upstream CI green on this head, Greptile 5/5

## Risks

- Low risk: stored sessions reset once after deploy

## Model Used

Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking

## Checklist

- [x] I have included a thinking path that traces from project context to this change
- [x] I have specified the model used (with version and capability details)
- [x] I have run tests locally and they pass
- [x] All Paperclip CI gates are green
- [x] Greptile is 5/5 with no open P2s, recommendations, or follow-ups
'

# Build an upstream fixture dir. Args: dir [mutation...]
mk_upstream() {
  local dir="$1"; shift
  local score=5 reviewed="$SHA" threads='[]' greptile_ci=1 nocomment=0 draft=true nolink=0
  local ubody="$UPSTREAM_BODY_FULL" repmut=ok filemuts="" v2=0 editafter=0 pending_no_timestamp=0
  local mergeable_json='true' mstate='clean'
  local m
  for m in "$@"; do case "$m" in
    mergeable-dirty) mergeable_json='false'; mstate='dirty' ;;
    mergeable-dirty-state) mergeable_json='true'; mstate='dirty' ;;
    mergeable-null) mergeable_json='null'; mstate='unknown' ;;
    no-reputation) repmut=none ;;
    rep-stranger) repmut=stranger ;;
    rep-other-head) repmut=otherhead ;;
    rep-failed) repmut=failed ;;
    rep-samefamily) repmut=samefamily ;;
    edit-after-pass) editafter=1 ;;
    ci-pending-no-time) pending_no_timestamp=1 ;;
    no-test|leak-in-diff|huge-diff) filemuts="$filemuts $m" ;;
    v2) v2=1 ;;
    score-4) score=4 ;;
    stale-score) reviewed="aaaaaaaabbbbccccddddeeeeffff111122223333" ;;
    no-comment) nocomment=1 ;;
    no-link) nolink=1 ;;
    open-thread) threads='[{"isResolved":false}]' ;;
    null-threads) threads='null' ;;
    published) draft=false ;;
    stub-body) ubody='Draft mirror of paperclipai/paperclip#15142.' ;;
    unchecked-boxes) ubody="$(printf '%s' "$UPSTREAM_BODY_FULL" | sed 's/- \[x\] I have run tests locally/- [ ] I have run tests locally/')" ;;
    gate-boxes-open) ubody="$(printf '%s' "$UPSTREAM_BODY_FULL" | sed -e 's/- \[x\] Greptile/- [ ] Greptile/' -e 's/- \[x\] All Paperclip CI/- [ ] All Paperclip CI/')" ;;
    near-gate-box) ubody="$ubody
- [ ] Greptile is 5/5 with no open P2s, recommendations, or follow-ups if maintainers agree" ;;
  esac; done
  [ "$v2" -eq 1 ] && ubody="$ubody
## Readiness (upstream-gate)

- pipeline: upstream-gate v2
"
  mkdir -p "$dir"
  jq -n --arg sha "$SHA" --argjson draft "$draft" --arg body "$ubody" \
    --argjson mergeable "$mergeable_json" --arg mstate "$mstate" \
    '{title:"fix(heartbeat): keep task sessions across project row updates",
      body:$body, draft:$draft, head:{sha:$sha,ref:"fix/session-fingerprint-project-revision-churn"},
      base:{ref:"master"}, mergeable:$mergeable, mergeable_state:$mstate}' > "$dir/pr.json"
  { mk_check_run "ci / verify" completed success "2026-10-06T03:00:00Z"
    mk_check_run "ci / e2e" completed success "2026-10-06T03:00:00Z"
    if [ "$greptile_ci" -eq 1 ]; then
      mk_check_run "Greptile Review" completed success "2026-10-06T03:01:00Z" \
        '{"title":"Base review","summary":"Greptile has reviewed the Pull Request. 2 files reviewed, 0 comments added."}'
    fi
  } | jq -s '{total_count: length, check_runs: .}' > "$dir/check_runs.json"
  if [ "$pending_no_timestamp" -eq 1 ]; then
    jq '.check_runs += [{name:"ci / verify",status:"queued",conclusion:null,started_at:null,completed_at:null,id:999,output:{title:"",summary:""}}] | .total_count = (.check_runs|length)' \
      "$dir/check_runs.json" > "$dir/check_runs.json.tmp" && mv "$dir/check_runs.json.tmp" "$dir/check_runs.json"
  fi
  if [ "$nocomment" -eq 1 ]; then echo '[]' > "$dir/comments.json";
  elif [ "$nolink" -eq 1 ]; then
    jq -n --argjson score "$score" \
      '[{user:{login:"greptile-apps[bot]"},created_at:"2026-10-06T03:02:00Z",
         body:("<!-- greptile_summary -->\n<h2>Confidence Score: \($score)/5</h2>\n<!-- greptile_confidence_score:\($score) -->")}]' \
      > "$dir/comments.json"
  else
    jq -n --argjson score "$score" --arg rev "$reviewed" \
      '[{user:{login:"greptile-apps[bot]"},created_at:"2026-10-06T03:02:00Z",
         body:("<!-- greptile_summary -->\n<h2>Confidence Score: \($score)/5</h2>\n<!-- greptile_confidence_score:\($score) -->\nLast reviewed commit: [x](https://github.com/paperclipai/paperclip/commit/\($rev))")}]' \
      > "$dir/comments.json"
  fi
  echo "$threads" > "$dir/threads.json"
  echo '[{"sha":"'"$SHA"'","commit":{"message":"fix(heartbeat): keep task sessions"}}]' > "$dir/commits.json"
  # shellcheck disable=SC2086
  mk_files "$dir" $filemuts
  # The reputation receipt lives on the fork head, i.e. in the SIBLING mirror
  # dir, bound to the hash of the upstream text as it is NOW.
  local mdir; mdir="$(dirname "$dir")/mirror"
  if [ -d "$mdir" ]; then
    local h; h="$(python3 "$HERE/upstream_reputation.py" hash --upstream-layout "$dir")"
    python3 - "$mdir/comments.json" "$h" "$repmut" "$HERE" "$SHA" <<'PYREP'
import json, sys
out, h, mode, here, sha = sys.argv[1:6]
sys.path.insert(0, here)
import upstream_lint as ul
desc = {"ok": "clean text=%s author=anthropic reviewer=openai/gpt-6.1-sol" % h,
        "failed": "2 finding(s) text=%s author=anthropic reviewer=openai/gpt-6.1-sol" % h,
        "samefamily": "clean text=%s author=anthropic reviewer=anthropic/claude-sonnet-5-5" % h}
comments = []
if mode in ("ok", "failed", "samefamily", "stranger", "otherhead"):
    d = desc["failed" if mode == "failed" else "samefamily" if mode == "samefamily" else "ok"]
    state = "failure" if mode == "failed" else "success"
    head = "b" * 40 if mode == "otherhead" else sha
    comments.append({"id": 9, "user": {"login": "stranger" if mode == "stranger" else "rick"},
                     "created_at": "2026-10-06T04:00:00Z", "body": ul.receipt_body("upstream-gate/reputation", state, head, d)})
json.dump(comments, open(out, "w"))
PYREP
  fi
  if [ "$editafter" -eq 1 ]; then
    jq '.body += "\nOne more sentence added after the pass.\n"' "$dir/pr.json" > "$dir/pr.json.tmp" && mv "$dir/pr.json.tmp" "$dir/pr.json"
  fi
}

run_check() { OUT="$("$TOOL" check --mirror-pr 52 --fixtures "$1" 2>&1)"; RC=$?; }
run_ready() { OUT="$("$TOOL" ready --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$1" --dry-run 2>&1)"; RC=$?; }
run_audit() { OUT="$("$TOOL" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$1" 2>&1)"; RC=$?; }
add_lint_receipt() { # mirror-comments.json author head
  python3 - "$HERE" "$1" "$2" "$3" <<'PY'
import json, sys
here, path, author, head = sys.argv[1:]
sys.path.insert(0, here)
import upstream_lint as ul
comments = json.load(open(path))
comments.append({"id": len(comments) + 1, "user": {"login": author},
                 "created_at": "2026-10-07T04:00:00Z",
                 "body": ul.receipt_body("upstream-gate/lint", "success", head,
                                          "pass first-push head=%s pipeline=v2" % head[:12])})
json.dump(comments, open(path, "w"))
PY
}

# ===========================================================================
hdr "baseline: Gate A passes on the all-green world"
M="$WORK/m_pass"; mk_mirror "$M"
run_check "$M"
if [ "$RC" -eq 0 ] && grep -q "GATE A: PASS" <<<"$OUT"; then ok "all-green mirror passes Gate A";
else bad "all-green mirror passes Gate A" "rc=$RC out=$OUT"; fi
for item in fork-ci our-review checklist no-internal-ids single-commit; do
  if grep -q "PASS.*$item\|$item.*PASS\|PASS.*$item" <<<"$OUT"; then ok "baseline item visible: $item";
  else bad "baseline item visible: $item" "$OUT"; fi
done

# ===========================================================================
hdr "mutations: each Gate A item fails independently"

M="$WORK/m_red"; mk_mirror "$M" ci-red-latest
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*fork-ci" <<<"$OUT"; then ok "latest red after old green fails fork-ci";
else bad "latest red after old green fails fork-ci" "rc=$RC out=$OUT"; fi

M="$WORK/m_pend"; mk_mirror "$M" ci-pending
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*fork-ci" <<<"$OUT"; then ok "pending run fails fork-ci (not a pass)";
else bad "pending run fails fork-ci (not a pass)" "rc=$RC out=$OUT"; fi

M="$WORK/m_pend_no_time"; mk_mirror "$M" ci-pending-no-time
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*fork-ci not concluded.*ci / verify:queued" <<<"$OUT"; then
  ok "new queued fork check supersedes old green without timestamps";
else bad "new queued fork check supersedes old green without timestamps" "rc=$RC out=$OUT"; fi

M="$WORK/m_review_pending"; mk_mirror "$M" review-pending
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review.*in_progress" <<<"$OUT"; then ok "new pending Paperclip Review supersedes an older 5/5";
else bad "new pending Paperclip Review supersedes an older 5/5" "rc=$RC out=$OUT"; fi

M="$WORK/m_review_pending_no_time"; mk_mirror "$M" review-pending-no-time
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review: latest Paperclip Review is queued" <<<"$OUT"; then
  ok "new queued Paperclip Review supersedes older 5/5 without timestamps";
else bad "new queued Paperclip Review supersedes older 5/5 without timestamps" "rc=$RC out=$OUT"; fi

M="$WORK/m_empty"; mk_mirror "$M" ci-empty
run_check "$M"
if [ "$RC" -eq 2 ] && grep -qi "REFUSED" <<<"$OUT"; then ok "zero signals refuses (exit 2)";
else bad "zero signals refuses (exit 2)" "rc=$RC out=$OUT"; fi

M="$WORK/m_norev"; mk_mirror "$M" no-review-run
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review" <<<"$OUT"; then ok "missing Paperclip Review fails our-review";
else bad "missing Paperclip Review fails our-review" "rc=$RC out=$OUT"; fi

M="$WORK/m_noscore"; mk_mirror "$M" review-no-score
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review" <<<"$OUT"; then ok "success without 5/5 fails our-review";
else bad "success without 5/5 fails our-review" "rc=$RC out=$OUT"; fi

M="$WORK/m_fallback"; mk_mirror "$M" review-fallback
run_check "$M"
if [ "$RC" -eq 0 ] && grep -q "PASS.*our-review 5/5" <<<"$OUT"; then
  ok "empty review title falls back to the first summary line";
else bad "empty review title falls back to the first summary line" "rc=$RC out=$OUT"; fi

M="$WORK/m_fallback_missing"; mk_mirror "$M" review-fallback-missing
run_check "$M"
if [ "$RC" -eq 0 ] && grep -q "PASS.*our-review 5/5" <<<"$OUT"; then
  ok "missing review title falls back to the first summary line";
else bad "missing review title falls back to the first summary line" "rc=$RC out=$OUT"; fi

M="$WORK/m_title_authoritative"; mk_mirror "$M" review-title-authoritative
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review" <<<"$OUT"; then
  ok "nonempty review title remains authoritative";
else bad "nonempty review title remains authoritative" "rc=$RC out=$OUT"; fi

M="$WORK/m_incidental"; mk_mirror "$M" review-incidental-score
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*our-review" <<<"$OUT"; then
  ok "incidental 5/5 elsewhere in the summary does not satisfy our-review";
else bad "incidental 5/5 elsewhere in the summary does not satisfy our-review" "rc=$RC out=$OUT"; fi

M="$WORK/m_2c"; mk_mirror "$M" two-commits
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*single-commit" <<<"$OUT"; then ok "two commits fail single-commit";
else bad "two commits fail single-commit" "rc=$RC out=$OUT"; fi

M="$WORK/m_tog"; mk_mirror "$M" tog-in-body
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*no-internal-ids" <<<"$OUT"; then ok "Refs: TOG- in body fails no-internal-ids";
else bad "Refs: TOG- in body fails no-internal-ids" "rc=$RC out=$OUT"; fi

M="$WORK/m_place"; mk_mirror "$M" placeholder-body
run_check "$M"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*checklist" <<<"$OUT"; then ok "placeholder body fails checklist";
else bad "placeholder body fails checklist" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "ready: upstream side"

F="$WORK/f_pass"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream"
run_ready "$F"
if [ "$RC" -eq 0 ]; then ok "green upstream + 5/5 on head + mirror pass is ready";
else bad "green upstream + 5/5 on head + mirror pass is ready" "rc=$RC out=$OUT"; fi

F="$WORK/f_s4"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" score-4
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*greptile" <<<"$OUT"; then ok "Greptile 4/5 blocks ready";
else bad "Greptile 4/5 blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/f_stale"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" stale-score
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -qi "FAIL.*greptile.*stale" <<<"$OUT"; then ok "5/5 on an old head blocks ready (stale)";
else bad "5/5 on an old head blocks ready (stale)" "rc=$RC out=$OUT"; fi

F="$WORK/f_nc"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-comment
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*greptile" <<<"$OUT"; then ok "no Greptile comment blocks ready";
else bad "no Greptile comment blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/f_ot"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" open-thread
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*greptile" <<<"$OUT"; then ok "unresolved thread blocks ready despite 5/5";
else bad "unresolved thread blocks ready despite 5/5" "rc=$RC out=$OUT"; fi

F="$WORK/f_null_threads"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" null-threads
run_ready "$F"
if [ "$RC" -eq 2 ] && grep -q "REFUSED.*invalid shape" <<<"$OUT"; then ok "malformed review threads refuse ready, never pass";
else bad "malformed review threads refuse ready, never pass" "rc=$RC out=$OUT"; fi

F="$WORK/f_mred"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror" ci-red-latest; mk_upstream "$F/upstream"
run_ready "$F"
if [ "$RC" -eq 1 ]; then ok "mirror Gate A regression blocks ready";
else bad "mirror Gate A regression blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/f_pend_no_time"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" ci-pending-no-time
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*upstream-ci not concluded.*ci / verify" <<<"$OUT"; then
  ok "new queued upstream check supersedes old green without timestamps";
else bad "new queued upstream check supersedes old green without timestamps" "rc=$RC out=$OUT"; fi

F="$WORK/f_stub"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" stub-body
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*up-checklist" <<<"$OUT"; then ok "stub upstream body blocks ready";
else bad "stub upstream body blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/f_unchecked"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" unchecked-boxes
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*up-checklist" <<<"$OUT"; then ok "ready refuses with unchecked boxes (tick what the gate verified)";
else bad "ready refuses with unchecked boxes (tick what the gate verified)" "rc=$RC out=$OUT"; fi

F="$WORK/f_near_gate"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" near-gate-box
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*up-checklist" <<<"$OUT"; then ok "ready does not tick a near-match gate phrase";
else bad "ready does not tick a near-match gate phrase" "rc=$RC out=$OUT"; fi

F="$WORK/f_nolink"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-link
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*greptile.*no reviewed commit" <<<"$OUT"; then ok "score without a reviewed-commit link blocks ready";
else bad "score without a reviewed-commit link blocks ready" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "audit backstop"

F="$WORK/f_pass"; run_audit "$F"
if [ "$RC" -eq 0 ] && grep -q "AUDIT: PASS" <<<"$OUT"; then ok "audit passes a ready-state head";
else bad "audit passes a ready-state head" "rc=$RC out=$OUT"; fi

F="$WORK/f_pub"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror" ci-red-latest; mk_upstream "$F/upstream" published
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "convert to draft now" <<<"$OUT"; then ok "audit flags non-draft failing head with convert-to-draft";
else bad "audit flags non-draft failing head with convert-to-draft" "rc=$RC out=$OUT"; fi

F="$WORK/f_pubunchecked"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published unchecked-boxes
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*up-checklist" <<<"$OUT"; then ok "non-draft with unchecked boxes fails audit";
else bad "non-draft with unchecked boxes fails audit" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" --json 2>&1)"; RC=$?
if printf '%s' "$OUT" | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["pr"]==15142 and v["pass"] is False and len(v["items"])>0' 2>/dev/null; then
  ok "audit --json emits a watcher-readable record"
else bad "audit --json emits a watcher-readable record" "rc=$RC out=$OUT"; fi

# case_165ac98c: merge conflicts are measured. A dirty non-draft fails on the
# mergeable item with convert-to-draft; a dirty draft fails as already-draft.
F="$WORK/f_dirty"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published mergeable-dirty
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "mergeable: FAIL" <<<"$OUT" && grep -q "convert to draft now" <<<"$OUT"; then ok "dirty non-draft fails audit on mergeable with convert-to-draft";
else bad "dirty non-draft fails audit on mergeable with convert-to-draft" "rc=$RC out=$OUT"; fi

F="$WORK/f_dirty_draft"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" mergeable-dirty
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "mergeable: FAIL" <<<"$OUT" && grep -q "already draft" <<<"$OUT"; then ok "dirty draft fails audit without convert-to-draft";
else bad "dirty draft fails audit without convert-to-draft" "rc=$RC out=$OUT"; fi

# case_165ac98c: mergeable=null is unmeasurable readiness state: refuse, never pass.
F="$WORK/f_mnull"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published mergeable-null
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "mergeable: REFUSED" <<<"$OUT"; then ok "null mergeable refuses the audit (never a pass)";
else bad "null mergeable refuses the audit (never a pass)" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" --json --enforce 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["refused"] is True and v["converted"] is False' 2>/dev/null; then
  ok "audit --json --enforce refuses null mergeable with converted:false"
else bad "audit --json --enforce refuses null mergeable with converted:false" "rc=$RC out=$OUT"; fi

# case_165ac98c follow-up (review 4/5): mergeable_state=dirty fails even when the
# mergeable flag itself is true. This pins the `state == "dirty"` arm that the
# mutation gate found undetected.
F="$WORK/f_dirty_state"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published mergeable-dirty-state
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "mergeable: FAIL" <<<"$OUT" && grep -q "convert to draft now" <<<"$OUT"; then ok "dirty state with mergeable true fails audit on mergeable";
else bad "dirty state with mergeable true fails audit on mergeable" "rc=$RC out=$OUT"; fi

# case_165ac98c follow-up (review 4/5): a measured FAIL plus a null mergeable is
# still a FAIL, not a refusal. GitHub returns null on the first GET after any
# head or base push; that transient must not shield a dirty PR from conversion.
# Fixtures never mutate, so this asserts the exit and both items; the live-path
# case below asserts the --enforce mutation runs.
F="$WORK/f_fail_mnull"; mkdir -p "$F/mirror" "$F/upstream"
mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published score-4 mergeable-null
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "mergeable: REFUSED" <<<"$OUT" && grep -q "FAIL" <<<"$OUT"; then ok "measured FAIL plus null mergeable stays FAIL (no refusal veto)";
else bad "measured FAIL plus null mergeable stays FAIL (no refusal veto)" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" --json --enforce 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["pass"] is False and v["refused"] is True and v["converted"] is False' 2>/dev/null; then
  ok "audit --json records FAIL plus refused with converted:false under fixtures"
else bad "audit --json records FAIL plus refused with converted:false under fixtures" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "fetch: paging is load-bearing (stub API server)"

# Page 1 holds 100 old-green runs; the red ci/verify re-run sits alone on
# page 2. A tool that reads only page 1 reports PASS on bytes that are red.
STUB="$WORK/stub"; mkdir -p "$STUB"
MKPAGE="$WORK/mkpages.py"
cat > "$MKPAGE" <<'PYEOF'
import json
sha = "f5bca33a44fb20d90972a0d15db880e0e3129440"
def run(name, concl, ts, i):
    return {"name": name, "status": "completed", "conclusion": concl,
            "completed_at": ts, "started_at": ts, "id": i,
            "output": {"title": "", "summary": ""}}
import sys
d = sys.argv[1]
p1 = [run("filler-%d" % i, "success", "2026-09-28T17:00:00Z", i) for i in range(98)]
p1.append(run("ci / verify", "success", "2026-09-28T17:35:00Z", 101))
p1.append(run("Paperclip Review", "success", "2026-09-28T17:40:00Z", 102))
p1[-1]["output"] = {"title": "5/5", "summary": "## Paperclip Review — 5/5"}
json.dump({"total_count": 100, "check_runs": p1}, open(d + "/cr1.json", "w"))
p2 = [run("ci / verify", "failure", "2026-10-06T02:49:57Z", 201)]
json.dump({"total_count": 1, "check_runs": p2}, open(d + "/cr2.json", "w"))
pr = {"title": "fix(heartbeat): keep task sessions across project touches",
      "body": "x", "draft": True,
      "head": {"sha": sha, "ref": "fix/session-fingerprint-project-revision-churn"},
      "base": {"ref": "mirror-base/15142-1c07b590"}}
json.dump(pr, open(d + "/pr.json", "w"))
json.dump([{"sha": sha, "commit": {"message": "fix(heartbeat): keep task sessions"}}],
          open(d + "/commits.json", "w"))
json.dump([], open(d + "/comments.json", "w"))
json.dump([], open(d + "/files.json", "w"))
PYEOF
python3 "$MKPAGE" "$STUB"
cat > "$STUB/server.py" <<'PYEOF'
import http.server, json, sys, urllib.parse
d = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/pulls/52"):
            return self._send(json.load(open(d + "/pr.json")))
        if u.path.endswith("/check-runs"):
            page = q.get("page", ["1"])[0]
            return self._send(json.load(open(d + ("/cr2.json" if page == "2" else "/cr1.json"))))
        if u.path.endswith("/commits"):
            return self._send(json.load(open(d + "/commits.json")))
        if u.path.endswith("/comments"):
            return self._send([])
        if u.path.endswith("/files"):
            return self._send([])
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path == "/graphql":
            ln = int(self.headers.get("Content-Length", 0)); self.rfile.read(ln)
            return self._send({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        self.send_response(404); self.end_headers()
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
python3 "$STUB/server.py" "$STUB" > "$STUB/port" 2>"$STUB/err" &
SRV=$!
for _ in $(seq 1 50); do [ -s "$STUB/port" ] && break; sleep 0.1; done
PORT="$(cat "$STUB/port")"
# GH_BIN points at nothing: this pins the gh-less CI container (measured
# 2026-10-06, where need_gh refused a GH_TOKEN run). The curl transport
# must carry the whole read path on its own.
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" GH_BIN="$STUB/no-such-gh" \
  "$TOOL" check --mirror-pr 52 --fork-repo ACME/fork 2>&1)"; RC=$?
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
if [ "$RC" -eq 1 ] && grep -q "FAIL.*fork-ci failing on f5bca33a44fb: ci / verify" <<<"$OUT"; then
  ok "page-2 red run is read (no silent page-1 green)"
else
  bad "page-2 red run is read (no silent page-1 green)" "rc=$RC out=$OUT"
fi

# ===========================================================================
hdr "live path: mirror lookup + fetch + eval (stub server, fake gh)"

# Twice this run the live path silently measured nothing: gh --paginate
# concatenating objects, then `pr list --jq --arg` rejected by gh. This
# drives the WHOLE audit live path offline: stub REST + fake gh.
LIVE="$WORK/live"; mkdir -p "$LIVE/bin"
cat > "$LIVE/mkdata.py" <<'PYEOF'
import json
sha = "f5bca33a44fb20d90972a0d15db880e0e3129440"
def run(name, concl, ts, i, out=None):
    return {"name": name, "status": "completed", "conclusion": concl,
            "completed_at": ts, "started_at": ts, "id": i,
            "output": out or {"title": "", "summary": ""}}
import sys
d = sys.argv[1]
json.dump({"object": {"sha": sha}}, open(d + "/fork_ref.json", "w"))
sys.path.insert(0, d + "/../..")
up = [run("ci / verify", "success", "2026-10-06T03:00:00Z", 1),
      run("ci / e2e", "success", "2026-10-06T03:00:00Z", 2),
      run("Greptile Review", "success", "2026-10-06T03:01:00Z", 3,
          {"title": "Base review", "summary": "2 files reviewed, 0 comments added."})]
json.dump({"total_count": 3, "check_runs": up}, open(d + "/up_cr.json", "w"))
mi = [run("ci / verify", "success", "2026-10-06T03:00:00Z", 11),
      run("ci / e2e", "success", "2026-10-06T03:00:00Z", 12),
      run("Paperclip Review", "success", "2026-10-06T03:01:00Z", 13,
          {"title": "5/5", "summary": "## Paperclip Review — 5/5"})]
json.dump({"total_count": 3, "check_runs": mi}, open(d + "/mi_cr.json", "w"))
mi_red = [run("ci / verify", "failure", "2026-10-06T04:00:00Z", 21),
          run("ci / e2e", "success", "2026-10-06T04:00:00Z", 22),
          run("Paperclip Review", "success", "2026-10-06T04:00:00Z", 23,
              {"title": "5/5", "summary": "## Paperclip Review — 5/5"})]
json.dump({"total_count": 3, "check_runs": mi_red}, open(d + "/mi_cr_red.json", "w"))
body = open(d + "/up_body.md").read()
json.dump({"title": "fix(heartbeat): keep task sessions across project row updates",
           "body": body, "draft": False,
           "head": {"sha": sha, "ref": "fix/session-fingerprint-project-revision-churn"},
           "base": {"ref": "master"},
           "mergeable": True, "mergeable_state": "clean"}, open(d + "/up_pr.json", "w"))
mbody = open(d + "/mi_body.md").read()
json.dump({"title": "Mirror review: fix(heartbeat) session fingerprint churn (upstream #15142)",
           "body": mbody, "draft": True,
           "head": {"sha": sha, "ref": "fix/session-fingerprint-project-revision-churn"},
           "base": {"ref": "mirror-base/15142-1c07b590"}}, open(d + "/mi_pr.json", "w"))
json.dump([{"sha": sha, "commit": {"message": "fix(heartbeat): keep task sessions"}}],
          open(d + "/commits.json", "w"))
json.dump([{"user": {"login": "greptile-apps[bot]"}, "created_at": "2026-10-06T03:02:00Z",
            "body": "<!-- greptile_summary -->\n<h2>Confidence Score: 5/5</h2>\n<!-- greptile_confidence_score:5 -->\nLast reviewed commit: [x](https://github.com/paperclipai/paperclip/commit/" + sha + ")"}],
          open(d + "/comments.json", "w"))
json.dump([{"number": 52, "headRefOid": sha, "baseRefName": "mirror-base/15142-1c07b590"},
           {"number": 13, "headRefOid": sha, "baseRefName": "master"}],
          open(d + "/prlist.json", "w"))
json.dump([{"filename": "server/src/services/heartbeat.ts", "status": "modified", "additions": 1,
            "deletions": 0, "changes": 1, "patch": "@@ -10,2 +10,3 @@\n const a = 1;\n+const fingerprint = hash(a);\n"},
           {"filename": "server/src/__tests__/heartbeat.test.ts", "status": "added", "additions": 3,
            "deletions": 0, "changes": 3,
            "patch": "@@ -0,0 +1,3 @@\n+it(\"keeps the session\", () => {\n+  expect(rotate()).toBe(false);\n+});\n"}],
          open(d + "/files.json", "w"))
json.dump([], open(d + "/open_prs.json", "w"))
# Mirror check runs where fork CI is still running (parallel-publish case).
mi_pend = [run("ci / verify", "success", "2026-10-06T04:00:00Z", 31)]
mi_pend.append({"name": "ci / e2e", "status": "in_progress", "conclusion": None,
                "completed_at": None, "started_at": "2026-10-06T04:00:00Z", "id": 32,
                "output": {"title": "", "summary": ""}})
mi_pend.append(run("Paperclip Review", "success", "2026-10-06T04:00:00Z", 33,
                   {"title": "5/5", "summary": "## Paperclip Review — 5/5"}))
json.dump({"total_count": 3, "check_runs": mi_pend}, open(d + "/mi_cr_pending.json", "w"))
# A mirror diff that leaks an internal id in an added line.
leak = json.load(open(d + "/files.json"))
leak[0]["patch"] += "+// see TOG-" "999001\n"
json.dump(leak, open(d + "/files_leak.json", "w"))
# `ready` live case: a DRAFT upstream PR whose gate-verified boxes are still open,
# with a full 14-box checklist (the real template) and a matching reputation receipt.
sys.path.insert(0, d + "/..")
mem = json.load(open(sys.argv[2] + "/FAKE__upstream.json"))
boxes = "\n".join("- [%s] %s" % ("x" if not any(k in b for k in ("ci gates", "greptile")) else " ", b)
                   for b in mem["template"]["checkboxes"])
body14 = open(d + "/up_body.md").read().split("## Checklist")[0] + "## Checklist\n\n" + boxes + "\n"
dr = json.load(open(d + "/up_pr.json")); dr["draft"] = True; dr["body"] = body14
json.dump(dr, open(d + "/up_pr_draft.json", "w"))
import upstream_reputation as rp
h = rp.text_hash(dr["title"], dr["body"], json.load(open(d + "/commits.json")))
import upstream_lint as ul
json.dump([{"id": 5, "user": {"login": "rick"}, "created_at": "2026-10-06T05:00:00Z",
            "body": ul.receipt_body("upstream-gate/reputation", "success", sha,
                                    "clean text=%s author=anthropic reviewer=openai/gpt-6.1-sol" % h)}],
          open(d + "/comments_rep.json", "w"))
# Our other open PR in the upstream repo, touching the same file: the batch trap.
json.dump([{"number": 77, "user": {"login": "FAKE"}, "head": {"ref": "fix/other-change"}}],
          open(d + "/open_prs_overlap.json", "w"))
json.dump([{"filename": "server/src/services/heartbeat.ts"}], open(d + "/open_files_77.json", "w"))
# #40 is the trap: lower-numbered, mirror-base, ASSOCIATED with the
# commit (contains it as an ancestor) but headed elsewhere. The lookup
# must skip it for #52. Without the .head.sha pin it returns 40.
json.dump([{"number": 40, "head": {"ref": "fix/other-change", "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            "base": {"ref": "mirror-base/15142-1c07b590"}, "state": "open"},
           {"number": 13, "head": {"ref": "fix/session-fingerprint-project-revision-churn", "sha": sha},
            "base": {"ref": "master"}, "state": "open"},
           {"number": 52, "head": {"ref": "fix/session-fingerprint-project-revision-churn", "sha": sha},
            "base": {"ref": "mirror-base/15142-1c07b590"}, "state": "open"}],
          open(d + "/cpulls.json", "w"))
PYEOF
printf '%s' "$UPSTREAM_BODY_FULL" > "$LIVE/up_body.md"
printf '%s' "$BODY_FULL" > "$LIVE/mi_body.md"
PYTHONPATH="$HERE" python3 "$LIVE/mkdata.py" "$LIVE" "$UPSTREAM_MEMORY_DIR"
cat > "$LIVE/server.py" <<'PYEOF'
import http.server, json, sys, urllib.parse
d = sys.argv[1]
FILES = {}
def load():
    return {
        ("FAKE/upstream", "pulls"): "up_pr.json",
        ("FAKE/upstream", "check-runs"): "up_cr.json",
        ("FAKE/upstream", "commits"): "commits.json",
        ("FAKE/upstream", "comments"): "comments.json",
        ("FAKE/fork", "pulls"): "mi_pr.json",
        ("FAKE/fork", "check-runs"): "mi_cr.json",
        ("FAKE/fork", "commits"): "commits.json",
        ("FAKE/fork", "comments"): "comments.json",
        ("FAKE/fork", "cpulls"): "cpulls.json",
        ("FAKE/fork", "ref"): "fork_ref.json",
        ("FAKE/upstream", "files"): "files.json",
        ("FAKE/fork", "files"): "files.json",
    }
def failing(*names):
    try:
        want = open(d + "/fail").read().split()
    except OSError:
        return False
    return any(n in want for n in names)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _fail(self):
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def _route(self):
        u = urllib.parse.urlparse(self.path)
        parts = u.path.strip("/").split("/")
        repo = "/".join(parts[1:3])
        # case_214e61d3: files, the viewer, our open PRs, the template blob.
        if u.path == "/user":
            return "USER"
        if u.path.endswith("/reviews"):
            return "open_prs.json"
        if u.path.endswith("/pulls") and "/commits/" not in u.path:
            return "open_prs_overlap.json" if failing("overlap") else "open_prs.json"
        if "/pulls/" in u.path and u.path.endswith("/files"):
            return ("open_files_77.json" if parts[-2] == "77" else
                    "files.json" if parts[-2] in ("15142", "52") else None)
        if "/contents/" in u.path:
            return "TEMPLATE"
        # commits-pulls endpoint (…/commits/<sha>/pulls): serves cpulls.json.
        if u.path.endswith("/pulls") and "/commits/" in u.path:
            kind = "cpulls"
        elif "/pulls/" in u.path and "/commits" not in u.path and "/comments" not in u.path:
            # Number-aware single-PR fetch: only the PRs the tests name
            # exist. A lookup that picks the #40 decoy (associated, other
            # head) 404s here, failing loudly instead of scoring it.
            kind = "pulls"
            if parts[-1] not in ("15142", "52"):
                return None
        elif "/git/ref/heads/" in u.path: kind = "ref"
        elif "check-runs" in u.path: kind = "check-runs"
        elif u.path.endswith("/commits"): kind = "commits"
        elif u.path.endswith("/comments"): kind = "comments"
        else: return None
        return load().get((repo, kind))
    def do_GET(self):
        # NOTE: self.path carries the query string; match on the parsed
        # path, or endswith checks silently never fire (measured 2026-10-06).
        upath = urllib.parse.urlparse(self.path).path
        if upath == "/repos/FAKE/upstream/issues/comments/1":
            if failing("split-unreadable"):
                return self._fail()
            api_root = "http://%s:%d" % self.server.server_address
            comment = {"id": 1, "issue_url": api_root + "/repos/FAKE/upstream/issues/77",
                       "user": {"login": "maintainer"}, "author_association": "MEMBER",
                       "body": "Please split this pull request into smaller changes."}
            if failing("split-untrusted"):
                comment["author_association"] = "CONTRIBUTOR"
            if failing("split-no-request"):
                comment["body"] = "Looks good to me."
            return self._send(comment)
        if failing("no-mirror") and upath.endswith("/pulls") and "/commits/" in upath:
            return self._send([])
        if failing("cpulls") and upath.endswith("/pulls"):
            return self._fail()
        if failing("mirror_red") and upath.endswith("/check-runs") and "/FAKE/fork/" in upath:
            return self._send(json.load(open(d + "/mi_cr_red.json")))
        if failing("mirror_pending") and upath.endswith("/check-runs") and "/FAKE/fork/" in upath:
            return self._send(json.load(open(d + "/mi_cr_pending.json")))
        if failing("leakdiff") and upath.endswith("/files") and "/FAKE/fork/" in upath:
            return self._send(json.load(open(d + "/files_leak.json")))
        if "/git/ref/heads/" in upath and failing("branch-moved"):
            return self._send({"object": {"sha": "b" * 40}})
        if "/git/ref/heads/" in upath and failing("branch-moves-between-checks"):
            counter = d + "/branch_ref_reads"
            try: count = int(open(counter).read()) + 1
            except OSError: count = 1
            open(counter, "w").write(str(count))
            sha = json.load(open(d + "/fork_ref.json"))["object"]["sha"]
            return self._send({"object": {"sha": sha if count == 1 else "b" * 40}})
        if failing("snapshot-head-moved") and upath.endswith("/pulls/15142") and "/FAKE/upstream/" in upath:
            counter = d + "/snapshot_head_reads"
            try: count = int(open(counter).read()) + 1
            except OSError: count = 1
            open(counter, "w").write(str(count))
            pr = json.load(open(d + "/up_pr.json"))
            if count > 1: pr["head"]["sha"] = "b" * 40
            return self._send(pr)
        if failing("snapshot-mirror-title-moved") and upath.endswith("/pulls/52") and "/FAKE/fork/" in upath:
            counter = d + "/snapshot_mirror_reads"
            try: count = int(open(counter).read()) + 1
            except OSError: count = 1
            open(counter, "w").write(str(count))
            pr = json.load(open(d + "/mi_pr.json"))
            if count > 1: pr["title"] = pr.get("title", "") + " (edited)"
            return self._send(pr)
        if failing("no-mirror") and upath.endswith("/pulls/15142") and "/FAKE/upstream/" in upath:
            pr = json.load(open(d + "/up_pr.json"))
            pr["body"] = pr.get("body", "").replace("pipeline: upstream-gate v2", "pipeline: upstream-gate legacy")
            return self._send(pr)
        if failing("ready-head-moved") and upath.endswith("/pulls/15142") and "/FAKE/upstream/" in upath:
            counter = d + "/ready_head_reads"
            try: count = int(open(counter).read()) + 1
            except OSError: count = 1
            open(counter, "w").write(str(count))
            pr = json.load(open(d + "/up_pr_draft.json"))
            if count > 2: pr["head"]["sha"] = "b" * 40
            return self._send(pr)
        if (failing("mergeable-dirty") or failing("mergeable-null")) and upath.endswith("/pulls/15142") and "/FAKE/upstream/" in upath:
            pr = json.load(open(d + ("/up_pr_draft.json" if failing("upstream_draft") else "/up_pr.json")))
            if failing("mergeable-null"):
                pr["mergeable"] = None; pr["mergeable_state"] = "unknown"
            else:
                pr["mergeable"] = False; pr["mergeable_state"] = "dirty"
            return self._send(pr)
        if failing("upstream_draft") and upath.endswith("/pulls/15142") and "/FAKE/upstream/" in upath:
            return self._send(json.load(open(d + "/up_pr_draft.json")))
        if failing("repstatus") and upath.endswith("/comments") and "/FAKE/fork/" in upath:
            return self._send(json.load(open(d + "/comments_rep.json")))
        if failing("check-runs") and "check-runs" in upath:
            return self._fail()
        f = self._route()
        if f == "USER": return self._send({"login": "FAKE"})
        if f == "TEMPLATE":
            import base64
            return self._send({"content": base64.b64encode(b"## A\n").decode(), "sha": "fixtureblob"})
        if f: return self._send(json.load(open(d + "/" + f)))
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path.endswith("/comments") and "/issues/" in self.path:
            if failing("receipt-post-fail"):
                return self._fail()
            ln = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(ln).decode()
            open(d + "/posted.log", "a").write(self.path + " " + raw + "\n")
            open(d + "/sequence.log", "a").write("receipt\n")
            return self._send({"id": 1})
        if self.path == "/graphql":
            ln = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(ln).decode())
            if failing("graphql"):
                return self._fail()
            variables = payload.get("variables") or {}
            with open(d + "/graphql.log", "a") as log:
                log.write(json.dumps(variables) + "\\n")
            if failing("threads-unresolved") and variables.get("after") is None:
                threads = {"nodes": [{"isResolved": True}] * 100,
                           "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}}
            elif failing("threads-unresolved"):
                threads = {"nodes": [{"isResolved": False}],
                           "pageInfo": {"hasNextPage": False, "endCursor": None}}
            else:
                threads = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return self._send({"data": {"repository": {"pullRequest": {"reviewThreads": threads}}}})
        self.send_response(404); self.end_headers()
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
printf '{"id":"PR_fake123"}' > "$LIVE/prview.json"
printf '{"data":{"convertPullRequestToDraft":{"pullRequest":{"isDraft":true}}}}' > "$LIVE/mutate.json"
cat > "$LIVE/bin/gh" <<'SHEOF'
#!/usr/bin/env bash
# Fake gh: canned JSON per subcommand with the caller's own --jq expression
# applied (last argument), exactly like real gh. Anything else fails loudly
# so a live-path regression cannot hide behind network.
last="${@: -1}"
case "${1:-} ${2:-}" in
  "pr list")
    # Assert the caller's lookup shape: bare --head (owner:branch is
    # unsupported), --state all (closed-then-republished must match), and
    # headRepositoryOwner in --json (owner filtering). A caller that
    # regresses any of these fails HERE, not silently downstream.
    case "$*" in *"--state all"*) ;; *)
      echo "fake-gh: pr list without --state all" >&2; exit 3;; esac
    headval="$(printf '%s' "$*" | sed -n 's/.*--head \([^ ]*\).*/\1/p')"
    case "$headval" in *:* )
      echo "fake-gh: pr list with owner:branch head syntax" >&2; exit 3;; esac
    case "$*" in *"headRepositoryOwner"*) ;; *)
      echo "fake-gh: pr list without headRepositoryOwner" >&2; exit 3;; esac
    jq "$last" "$CANNED_PR_LIST";;
  "pr view") jq "$last" "$CANNED_PR_VIEW";;
  "pr create") echo "$*" >> "$CANNED_GH_CALLS"; printf 'create\n' >> "$CANNED_EVENT_LOG"; exit 0;;
  "pr ready") echo "$*" >> "$CANNED_GH_CALLS"; printf 'ready\n' >> "$CANNED_EVENT_LOG"; exit 0;;
  "api -X")
    echo "$*" >> "$CANNED_GH_CALLS"
    bf="$(printf '%s' "$*" | sed -n 's/.*body=@\([^ ]*\).*/\1/p')"
    [ -n "$bf" ] && cp "$bf" "$CANNED_GH_BODY"
    echo '{}';;
  "api graphql")
    echo "$*" >> "$CANNED_GH_CALLS"
    if grep -q "pullRequestId" <<<"$*"; then jq "$last" "$CANNED_MUTATE";
    else echo "fake-gh: unexpected graphql without pullRequestId" >&2; exit 1; fi;;
  *) echo "fake-gh: unexpected call: $*" >&2; exit 1;;
esac
SHEOF
chmod +x "$LIVE/bin/gh"
python3 "$LIVE/server.py" "$LIVE" > "$LIVE/port" 2>"$LIVE/err" &
SRV=$!
for _ in $(seq 1 50); do [ -s "$LIVE/port" ] && break; sleep 0.1; done
PORT="$(cat "$LIVE/port")"
# NOTE: GH_BIN, not PATH: this host resets PATH via BASH_ENV for every
# non-interactive shell, which silently un-injects a PATH-prepended fake
# (measured 2026-10-06). GH_BIN survives because only PATH is rewritten.
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork 2>&1)"; RC=$?
# Server stays up: the --enforce case below drives the same stub.
if [ "$RC" -eq 0 ] && grep -q "AUDIT: PASS" <<<"$OUT"; then
  ok "live-path audit passes green/green (lookup + fetch + eval)"
else
  bad "live-path audit passes green/green (lookup + fetch + eval)" "rc=$RC out=$OUT"
fi

# --enforce on a failing non-draft must reach the convert mutation. Serve
# the mirror check runs red (valid data, genuine FAIL, not a refusal).
printf 'mirror_red' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
# Server stays up: the fail-closed and publish cases below drive the same stub.
if [ "$RC" -eq 1 ] && grep -q "converted #15142 to draft" <<<"$OUT" \
   && grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --enforce converts a failing non-draft via the draft mutation"
else
  bad "audit --enforce converts a failing non-draft via the draft mutation" "rc=$RC out=$OUT"
fi

# case_165ac98c: a dirty (conflicting) non-draft converts through the same mutation.
printf 'mergeable-dirty' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "mergeable: FAIL" <<<"$OUT" \
   && grep -q "converted #15142 to draft" <<<"$OUT" \
   && grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --enforce converts a dirty non-draft via the draft mutation"
else
  bad "audit --enforce converts a dirty non-draft via the draft mutation" "rc=$RC out=$OUT"
fi

# A dirty draft fails but is never converted: there is nothing to convert.
printf 'upstream_draft mergeable-dirty' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "already draft" <<<"$OUT" \
   && ! grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --enforce never converts an already-draft dirty PR"
else
  bad "audit --enforce never converts an already-draft dirty PR" "rc=$RC out=$OUT"
fi

# case_165ac98c: --json --enforce runs the same conversion and reports it in the
# JSON record instead of prose (stdout stays one parseable JSON line).
printf 'mirror_red' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --json --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["pass"] is False and v["converted"] is True' 2>/dev/null \
   && grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --json --enforce converts and reports converted:true in the record"
else
  bad "audit --json --enforce converts and reports converted:true in the record" "rc=$RC out=$OUT"
fi

# case_165ac98c: null mergeable refuses even with --enforce, and never mutates.
printf 'mergeable-null' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 2 ] && grep -q "mergeable: REFUSED" <<<"$OUT" \
   && ! grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --enforce refuses null mergeable with no mutation"
else
  bad "audit --enforce refuses null mergeable with no mutation" "rc=$RC out=$OUT"
fi

# case_165ac98c follow-up (review 4/5): a measured FAIL plus a null mergeable still
# converts under --enforce. The stub serves mergeable=null for the upstream PR
# while mirror_red fails the mirror side; the transient null must not veto the
# conversion of a known-bad non-draft.
printf 'mirror_red mergeable-null' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
OUT="$(GH_TOKEN=dummy GH_API_URL="http://127.0.0.1:$PORT" CANNED_PR_LIST="$LIVE/prlist.json" \
  CANNED_PR_VIEW="$LIVE/prview.json" CANNED_MUTATE="$LIVE/mutate.json" CANNED_GH_CALLS="$LIVE/gh_calls.log" \
  GH_BIN="$LIVE/bin/gh" \
  "$TOOL" audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "mergeable: REFUSED" <<<"$OUT" \
   && grep -q "converted #15142 to draft" <<<"$OUT" \
   && grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "audit --enforce converts a measured FAIL even with null mergeable"
else
  bad "audit --enforce converts a measured FAIL even with null mergeable" "rc=$RC out=$OUT"
fi

# Fail-closed: a 502 anywhere in the fetch path must exit 2 with NO mutation,
# even with --enforce on a non-draft. (Finding 1: this converted a ready PR
# to draft during an outage.)
LIVE_ENV="GH_TOKEN=dummy GH_API_URL=http://127.0.0.1:$PORT CANNED_PR_VIEW=$LIVE/prview.json CANNED_MUTATE=$LIVE/mutate.json CANNED_GH_CALLS=$LIVE/gh_calls.log CANNED_EVENT_LOG=$LIVE/sequence.log GH_BIN=$LIVE/bin/gh"
# Guard against vacuous refusal: prove the stub is reachable first, or a
# dead server would satisfy every fail-closed assertion for the wrong reason
# (measured 2026-10-06: a misplaced kill made two tests pass spuriously).
stub_up() {
  [ "$(curl -sS -m 10 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$PORT/repos/FAKE/upstream/pulls/15142" 2>/dev/null)" = "200" ]
}
# A markerless PR with no current mirror has unknown provenance, not legacy.
# --enforce must refuse rather than convert an unmeasurable PR to draft.
if ! stub_up; then
  bad "no mirror + markerless provenance refuses enforce without mutation" "stub server unreachable; refusal would be vacuous"
else
  printf 'no-mirror' > "$LIVE/fail"
  : > "$LIVE/gh_calls.log"
  OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist.json \\
    $TOOL audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce" 2>&1)"; RC=$?
  rm -f "$LIVE/fail"
  if [ "$RC" -eq 2 ] && grep -q "pipeline-provenance: REFUSED" <<<"$OUT" \
     && ! grep -q "legacy PR" <<<"$OUT" && ! grep -q "pullRequestId" "$LIVE/gh_calls.log"; then
    ok "no mirror + markerless provenance refuses enforce without mutation"
  else
    bad "no mirror + markerless provenance refuses enforce without mutation" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"
  fi
fi

# A PR head change during the by-number layout fetch must not mix its files with
# check runs pinned to the earlier SHA or let audit report a pass.
if ! stub_up; then
  bad "audit refuses a PR head change during fetch without mutation" "stub server unreachable; refusal would be vacuous"
else
  printf 'snapshot-head-moved' > "$LIVE/fail"
  rm -f "$LIVE/snapshot_head_reads"
  : > "$LIVE/gh_calls.log"
  OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist.json \\
    $TOOL audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce" 2>&1)"; RC=$?
  rm -f "$LIVE/fail"
  if [ "$RC" -eq 2 ] && grep -q "changed during fetch" <<<"$OUT" \
     && ! grep -q "pullRequestId" "$LIVE/gh_calls.log"; then
    ok "audit refuses a PR head change during fetch without mutation"
  else
    bad "audit refuses a PR head change during fetch without mutation" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"
  fi
fi

# The same refusal must survive command substitution on EVERY caller: `lint` and
# `publish --dry-run` read the layout inside $(...), where a bare `exit` is lost.
if ! stub_up; then
  bad "lint refuses a PR head change during fetch" "stub server unreachable; refusal would be vacuous"
else
  printf 'snapshot-head-moved' > "$LIVE/fail"
  rm -f "$LIVE/snapshot_head_reads"
  OUT="$(eval "$LIVE_ENV $TOOL lint --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
  rm -f "$LIVE/fail"
  if [ "$RC" -eq 2 ] && grep -q "changed during fetch" <<<"$OUT" && ! grep -q "LINT: PASS" <<<"$OUT"; then
    ok "lint refuses a PR head change during fetch"
  else
    bad "lint refuses a PR head change during fetch" "rc=$RC out=$OUT"
  fi
  printf 'snapshot-mirror-title-moved' > "$LIVE/fail"
  rm -f "$LIVE/snapshot_mirror_reads"
  OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist.json $TOOL publish --mirror-pr 52 --title 'fix(heartbeat): keep task sessions across project touches' --upstream-repo FAKE/upstream --fork-repo FAKE/fork --dry-run" 2>&1)"; RC=$?
  rm -f "$LIVE/fail"
  if [ "$RC" -eq 2 ] && grep -q "changed during fetch" <<<"$OUT" && ! grep -q "GATE A: PASS" <<<"$OUT"; then
    ok "publish --dry-run refuses a mirror PR change during fetch"
  else
    bad "publish --dry-run refuses a mirror PR change during fetch" "rc=$RC out=$OUT"
  fi
fi

printf 'graphql' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
if ! stub_up; then
  bad "502 on threads + enforce refuses (exit 2) with no mutation" "stub server unreachable; refusal would be vacuous"
else
OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist.json \
  $TOOL audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce" 2>&1)"; RC=$?
fi
rm -f "$LIVE/fail"
if [ "${RC:-9}" -eq 2 ] && grep -qi "REFUSED" <<<"${OUT:-}" \
   && ! grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "502 on threads + enforce refuses (exit 2) with no mutation"
else
  bad "502 on threads + enforce refuses (exit 2) with no mutation" "rc=${RC:-9} out=${OUT:-}"
fi

# Fail-closed, mirror side: a broken lookup is not "no mirror".
printf 'cpulls' > "$LIVE/fail"
: > "$LIVE/gh_calls.log"
if ! stub_up; then
  bad "broken mirror lookup + enforce refuses (exit 2) with no mutation" "stub unreachable; refusal would be vacuous"
else
OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist.json \
  $TOOL audit --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork --enforce" 2>&1)"; RC=$?
fi
rm -f "$LIVE/fail"
if [ "${RC:-9}" -eq 2 ] && ! grep -q "pullRequestId" "$LIVE/gh_calls.log" 2>/dev/null; then
  ok "broken mirror lookup + enforce refuses (exit 2) with no mutation"
else
  bad "broken mirror lookup + enforce refuses (exit 2) with no mutation" "rc=${RC:-9} out=${OUT:-}"
fi

# Publish duplicate guard: an existing upstream PR for the branch refuses.
# (Finding 3: the guard never fired because `pr list --jq` lacked --json,
# then because --head owner:branch is unsupported.)
# Owner must equal the fork owner (FAKE/fork here), or the filter skips it.
printf '[{"number":7,"headRefName":"fix/session-fingerprint-project-revision-churn","headRepositoryOwner":{"login":"FAKE"}}]' > "$LIVE/prlist_dup.json"
OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist_dup.json \
  $TOOL publish --mirror-pr 52 --title 'fix(heartbeat): keep task sessions across project touches' \
    --upstream-repo FAKE/upstream --fork-repo FAKE/fork --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && grep -q "upstream #7 already exists" <<<"$OUT"; then
  ok "publish refuses when an upstream PR already exists for the branch"
else
  bad "publish refuses when an upstream PR already exists for the branch" "rc=$RC out=$OUT"
fi

# Owner filter: the same branch name under another owner is NOT a duplicate.
printf '[{"number":9,"headRefName":"fix/session-fingerprint-project-revision-churn","headRepositoryOwner":{"login":"SomeoneElse"}}]' > "$LIVE/prlist_foreign.json"
OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist_foreign.json \
  $TOOL publish --mirror-pr 52 --title 'fix(heartbeat): keep task sessions across project touches' \
    --upstream-repo FAKE/upstream --fork-repo FAKE/fork --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "Readiness (upstream-gate)" <<<"$OUT"; then
  ok "same branch under another owner is not a duplicate"
else
  bad "same branch under another owner is not a duplicate" "rc=$RC out=$OUT"
fi

# Positive control: no duplicate -> dry run prints the code-filled body.
printf '[]' > "$LIVE/prlist_empty2.json"
OUT="$(eval "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist_empty2.json \
  $TOOL publish --mirror-pr 52 --title 'fix(heartbeat): keep task sessions across project touches' \
    --upstream-repo FAKE/upstream --fork-repo FAKE/fork --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "Readiness (upstream-gate)" <<<"$OUT"; then
  ok "publish dry run prints the generated body when the gate passes"
else
  bad "publish dry run prints the generated body when the gate passes" "rc=$RC out=$OUT"
fi

# ---------------------------------------------------------------------------
# case_214e61d3: parallel publish, batch policy, lint at publish, live `ready`.
PUB_ARGS="publish --mirror-pr 52 --title 'fix(heartbeat): keep task sessions across project touches' --upstream-repo FAKE/upstream --fork-repo FAKE/fork"
mk_live_env() { # extra env words
  printf '%s' "$LIVE_ENV CANNED_PR_LIST=$LIVE/prlist_empty2.json CANNED_GH_BODY=$LIVE/gh_body.md $*"
}
: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"

# Parallel gates: fork CI still running, nothing red -> the DRAFT opens now.
printf 'mirror_pending' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 0 ] && grep -q "PASS so far" <<<"$OUT" && grep -q "^PEND" <<<"$OUT" && grep -q "Readiness (upstream-gate)" <<<"$OUT"; then
  ok "parallel publish opens the draft while fork CI is still running";
else bad "parallel publish opens the draft while fork CI is still running" "rc=$RC out=$OUT"; fi

# The generated body is honest about parallel mode and carries the v2 marker.
if grep -q "pipeline: upstream-gate v2" <<<"$OUT" && grep -q "^- \[ \] All Paperclip CI gates are green" <<<"$OUT" \
   && ! grep -q "Gate A: PASS at publish" <<<"$OUT"; then
  ok "generated body leaves the CI box open and claims nothing unmeasured";
else bad "generated body leaves the CI box open and claims nothing unmeasured" "$OUT"; fi

# --serial restores the old rule: pending is a refusal.
printf 'mirror_pending' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --serial --dry-run" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "publish refused" <<<"$OUT"; then ok "--serial refuses while a gate is still running";
else bad "--serial refuses while a gate is still running" "rc=$RC out=$OUT"; fi

# A completed red is never allowed, parallel or not.
printf 'mirror_red' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "publish refused" <<<"$OUT"; then ok "parallel publish still refuses a failed gate";
else bad "parallel publish still refuses a failed gate" "rc=$RC out=$OUT"; fi

# The linter blocks the publish on the diff that would go public.
printf 'leakdiff' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "PRE-PUSH LINT: FAIL" <<<"$OUT" && grep -q "internal" <<<"$OUT"; then
  ok "an internal id in an added diff line blocks publish";
else bad "an internal id in an added diff line blocks publish" "rc=$RC out=$OUT"; fi

# Fail closed: no maintainer memory for the upstream repo is a refusal.
mkdir -p "$WORK/emptymem"
OUT="$(eval "$(mk_live_env) UPSTREAM_MEMORY_DIR=$WORK/emptymem $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && grep -q "PRE-PUSH LINT: REFUSED" <<<"$OUT"; then ok "no maintainer memory refuses publish (exit 2)";
else bad "no maintainer memory refuses publish (exit 2)" "rc=$RC out=$OUT"; fi

# A changed upstream template is a refusal until memory is refreshed.
python3 - "$UPSTREAM_MEMORY_DIR/FAKE__upstream.json" "$WORK/stale_mem.json" <<'PYSTALE'
import json, sys
m = json.load(open(sys.argv[1])); m["template"]["blob_sha"] = "oldblob"; json.dump(m, open(sys.argv[2], "w"))
PYSTALE
mkdir -p "$WORK/stalemem"; cp "$WORK/stale_mem.json" "$WORK/stalemem/FAKE__upstream.json"
OUT="$(eval "$(mk_live_env) UPSTREAM_MEMORY_DIR=$WORK/stalemem $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && grep -qi "template changed" <<<"$OUT"; then ok "a changed upstream PR template refuses publish";
else bad "a changed upstream PR template refuses publish" "rc=$RC out=$OUT"; fi

# Batch policy: another of OUR open PRs touches the same file -> fold into it.
printf 'overlap' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && grep -q "BATCH POLICY: FAIL" <<<"$OUT"; then ok "overlap with our open PR refuses publish (fewer, complete PRs)";
else bad "overlap with our open PR refuses publish (fewer, complete PRs)" "rc=$RC out=$OUT"; fi
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run --maintainer-split-url https://github.com/FAKE/upstream/pull/77#issuecomment-1" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ]; then ok "a verified maintainer split request allows the overlap";
else bad "a verified maintainer split request allows the overlap" "rc=$RC out=$OUT"; fi
printf 'overlap split-untrusted' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run --maintainer-split-url https://github.com/FAKE/upstream/pull/77#issuecomment-1" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && grep -q "BATCH POLICY: FAIL" <<<"$OUT"; then ok "an untrusted commenter cannot waive overlap";
else bad "an untrusted commenter cannot waive overlap" "rc=$RC out=$OUT"; fi
printf 'overlap split-no-request' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run --maintainer-split-url https://github.com/FAKE/upstream/pull/77#issuecomment-1" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && grep -q "BATCH POLICY: FAIL" <<<"$OUT"; then ok "a comment without a split request cannot waive overlap";
else bad "a comment without a split request cannot waive overlap" "rc=$RC out=$OUT"; fi
printf 'overlap split-unreadable' > "$LIVE/fail"; : > "$LIVE/gh_calls.log"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run --maintainer-split-url https://github.com/FAKE/upstream/pull/77#issuecomment-1" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && ! grep -q '^pr create' "$LIVE/gh_calls.log"; then ok "an unreadable split-request comment refuses publish";
else bad "an unreadable split-request comment refuses publish" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"; fi
printf 'overlap' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS --dry-run --maintainer-split-url 'trust me'" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ]; then ok "a non-link split justification is refused";
else bad "a non-link split justification is refused" "rc=$RC out=$OUT"; fi

# Recheck the mirror ref before any publish-side write and again before PR creation.
: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"; printf 'branch-moved' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 2 ] && grep -q "branch .* moved during publish" <<<"$OUT" && [ ! -s "$LIVE/posted.log" ] && ! grep -q '^pr create' "$LIVE/gh_calls.log"; then
  ok "publish refuses a moved mirror branch before writing receipts or opening a PR";
else bad "publish refuses a moved mirror branch before writing receipts or opening a PR" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log") posts=$(cat "$LIVE/posted.log")"; fi

: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"; rm -f "$LIVE/branch_ref_reads"; printf 'branch-moves-between-checks' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 2 ] && grep -q "branch .* moved during publish" <<<"$OUT" && [ "$(grep -c '/issues/52/comments' "$LIVE/posted.log")" = 1 ] && ! grep -q '^pr create' "$LIVE/gh_calls.log"; then
  ok "publish rechecks the branch after receipt posting and before PR creation";
else bad "publish rechecks the branch after receipt posting and before PR creation" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log") posts=$(cat "$LIVE/posted.log")"; fi

# Real publish: gh pr create runs ONCE, as a draft, and the lint receipt is posted.
: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"; : > "$LIVE/sequence.log"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && [ "$(grep -c '^pr create' "$LIVE/gh_calls.log")" = 1 ] && grep -q -- '--draft' "$LIVE/gh_calls.log" \
   && grep -q 'upstream-gate/lint' "$LIVE/posted.log" && grep -q '/repos/FAKE/fork/issues/52/comments' "$LIVE/posted.log" \
   && [ "$(tr '\n' ',' < "$LIVE/sequence.log")" = "receipt,create," ]; then
  ok "publish records v2 provenance before opening one draft";
else bad "publish records v2 provenance before opening one draft" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log") posted=$(cat "$LIVE/posted.log") sequence=$(cat "$LIVE/sequence.log")"; fi

: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"; : > "$LIVE/sequence.log"; printf 'receipt-post-fail' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 2 ] && ! grep -q '^pr create' "$LIVE/gh_calls.log" && [ ! -s "$LIVE/posted.log" ]; then
  ok "a failed v2 provenance receipt refuses publish before gh pr create";
else bad "a failed v2 provenance receipt refuses publish before gh pr create" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"; fi

# A refused publish never reaches gh pr create.
: > "$LIVE/gh_calls.log"; printf 'mirror_red' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL $PUB_ARGS" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -ne 0 ] && ! grep -q '^pr create' "$LIVE/gh_calls.log"; then ok "a refused publish never calls gh pr create";
else bad "a refused publish never calls gh pr create" "rc=$RC calls=$(cat "$LIVE/gh_calls.log")"; fi

# G21/T1: the publish draft-plan path under a stub GH_BIN that hard-fails every
# mutation. The guard must bite on a direct mutation, yet the offline draft
# plan completes with zero mutation invocations (never a bare live dry-run).
cat > "$LIVE/bin/gh-guard" <<'GHGUARD'
#!/usr/bin/env bash
case " $* " in
  *" pr create "*|*" pr ready "*|*" pr merge "*|*" pr edit "*|*" pr close "*|*" pr comment "*|*" -X POST "*|*" -X PATCH "*|*" -X PUT "*|*" -X DELETE "*|*" --method POST "*|*" --method PATCH "*|*" --method PUT "*|*" --method DELETE "*)
    printf 'MUTATION: %s\n' "$*" >> "${GH_GUARD_LOG:-/dev/null}"
    echo "gh-guard: mutation refused: $*" >&2
    exit 3 ;;
esac
exec "$(dirname "$0")/gh" "$@"
GHGUARD
chmod +x "$LIVE/bin/gh-guard"
export GH_GUARD_LOG="$LIVE/gh_guard.log"; : > "$GH_GUARD_LOG"
"$LIVE/bin/gh-guard" pr create --draft >/dev/null 2>&1; GUARD_RC=$?
if [ "$GUARD_RC" -eq 3 ] && grep -q 'MUTATION: pr create' "$GH_GUARD_LOG"; then
  ok "T1 stub GH_BIN hard-fails a direct pr create mutation"
else bad "T1 stub GH_BIN hard-fails a direct pr create mutation" "rc=$GUARD_RC log=$(cat "$GH_GUARD_LOG")"; fi

: > "$GH_GUARD_LOG"; : > "$LIVE/gh_calls.log"
printf 'mirror_pending' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) GH_BIN=$LIVE/bin/gh-guard $TOOL $PUB_ARGS --dry-run" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 0 ] && grep -q "draft opens now" <<<"$OUT" && grep -q "PASS so far" <<<"$OUT"; then
  ok "G21 parallel publish still emits a draft plan, never ready, while fork CI is not green"
else bad "G21 parallel publish still emits a draft plan, never ready, while fork CI is not green" "rc=$RC out=$OUT"; fi
if [ "$RC" -eq 0 ] && [ ! -s "$GH_GUARD_LOG" ] && ! grep -E -q '^(pr create|pr ready|pr merge|pr edit)' "$LIVE/gh_calls.log"; then
  ok "T1 publish draft-plan completes offline with zero mutation invocations"
else bad "T1 publish draft-plan completes offline with zero mutation invocations" "rc=$RC guard=$(cat "$GH_GUARD_LOG") calls=$(cat "$LIVE/gh_calls.log") out=$OUT"; fi
unset GH_GUARD_LOG

# An unresolved review thread on page 2 must block ready; page 1 has 100 resolved threads.
: > "$LIVE/gh_calls.log"; : > "$LIVE/graphql.log"; printf 'threads-unresolved' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL ready --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*greptile.*unresolved review thread" <<<"$OUT" && grep -q 'cursor-1' "$LIVE/graphql.log" && ! grep -E -q '^(api -X PATCH|pr ready)' "$LIVE/gh_calls.log"; then
  ok "unresolved review thread after the first 100 blocks ready";
else bad "unresolved review thread after the first 100 blocks ready" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log") pages=$(cat "$LIVE/graphql.log")"; fi

# If the PR head changes during evaluation, ready must stop before ticking boxes.
: > "$LIVE/gh_calls.log"; rm -f "$LIVE/ready_head_reads"; printf 'upstream_draft repstatus ready-head-moved' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL ready --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 2 ] && grep -q "PR #15142 head moved during ready" <<<"$OUT" && ! grep -E -q '^(api -X PATCH|pr ready)' "$LIVE/gh_calls.log"; then
  ok "ready refuses a changed PR head before mutating its body";
else bad "ready refuses a changed PR head before mutating its body" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"; fi

# Live `ready`: all gates and receipts pass on a DRAFT whose gate boxes are open ->
# the tool ticks exactly those boxes, THEN marks ready, and records the receipt.
: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"; rm -f "$LIVE/gh_body.md"
printf 'upstream_draft repstatus' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL ready --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 0 ] && grep -q "ticked 3 gate-verified" <<<"$OUT" && grep -q "marked ready" <<<"$OUT" \
   && grep -q '^api -X PATCH' "$LIVE/gh_calls.log" && grep -q '^pr ready' "$LIVE/gh_calls.log" \
   && grep -qi '\[x\] all paperclip ci gates are green' "$LIVE/gh_body.md" \
   && grep -qi '\[x\] greptile is 5/5' "$LIVE/gh_body.md" \
   && grep -q 'upstream-gate/ready' "$LIVE/posted.log"; then
  ok "live ready ticks the measured boxes, marks ready and records the receipt on the mirror PR";
else bad "live ready ticks the measured boxes, marks ready and records the receipt" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"; fi
ORDER="$(grep -n -E '^(api -X PATCH|pr ready)' "$LIVE/gh_calls.log" | cut -d: -f2 | cut -d' ' -f1-2 | tr '\n' ',')"
if [ "$ORDER" = "api -X,pr ready," ]; then ok "boxes are ticked before the PR is marked ready";
else bad "boxes are ticked before the PR is marked ready" "order=$ORDER"; fi

# Live `ready` without the reputation receipt must not mutate anything.
: > "$LIVE/gh_calls.log"
printf 'upstream_draft' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL ready --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT" && [ ! -s "$LIVE/gh_calls.log" ]; then
  ok "live ready without a reputation receipt refuses with no mutation";
else bad "live ready without a reputation receipt refuses with no mutation" "rc=$RC out=$OUT calls=$(cat "$LIVE/gh_calls.log")"; fi

# The watcher tick: ONE JSON line with the audit record, the three gates and the
# unseen-feedback count, and it never mutates (no gh call, no status post).
: > "$LIVE/gh_calls.log"; : > "$LIVE/posted.log"
OUT="$(eval "$(mk_live_env) UPSTREAM_AUTHOR_LOGIN=FAKE $HERE/upstream_watch.sh --upstream-repo FAKE/upstream --upstream-pr 15142 --fork-repo FAKE/fork" 2>"$WORK/watch.err")"; RC=$?
if [ "$RC" -eq 0 ] && [ "$(printf '%s\n' "$OUT" | wc -l)" -eq 1 ] && printf '%s' "$OUT" | python3 -c '
import json, sys
v = json.load(sys.stdin)
assert v["repo"] == "FAKE/upstream" and v["pr"] == 15142 and len(v["head"]) == 40
assert v["audit"]["pass"] is True and v["gates"]["all_green"] is True
assert isinstance(v["unseen_feedback"], int) and v["exit"] == 0
' 2>/dev/null && [ ! -s "$LIVE/gh_calls.log" ] && [ ! -s "$LIVE/posted.log" ]; then
  ok "watcher tick emits one record (audit + gates + unseen feedback) and mutates nothing";
else bad "watcher tick emits one record (audit + gates + unseen feedback) and mutates nothing" "rc=$RC out=$OUT err=$(cat "$WORK/watch.err")"; fi
printf 'mirror_red' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) UPSTREAM_AUTHOR_LOGIN=FAKE $HERE/upstream_watch.sh --upstream-repo FAKE/upstream --upstream-pr 15142 --fork-repo FAKE/fork" 2>/dev/null)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | python3 -c '
import json, sys
v = json.load(sys.stdin)
assert v["audit"]["pass"] is False and v["gates"]["gates"]["fork-ci"]["state"] == "red"
' 2>/dev/null; then ok "watcher tick reports a red gate and exits non-zero";
else bad "watcher tick reports a red gate and exits non-zero" "rc=$RC out=$OUT"; fi

# status, live: three parallel gates from the real fetch path.
OUT="$(eval "$(mk_live_env) $TOOL status --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "ALL THREE GREEN" <<<"$OUT"; then ok "live status reads three green gates on one head";
else bad "live status reads three green gates on one head" "rc=$RC out=$OUT"; fi
printf 'mirror_pending' > "$LIVE/fail"
OUT="$(eval "$(mk_live_env) $TOOL status --upstream-pr 15142 --upstream-repo FAKE/upstream --fork-repo FAKE/fork" 2>&1)"; RC=$?
rm -f "$LIVE/fail"
if [ "$RC" -eq 3 ] && grep -q "PENDING" <<<"$OUT"; then ok "live status reports a running gate as pending (exit 3)";
else bad "live status reports a running gate as pending (exit 3)" "rc=$RC out=$OUT"; fi

kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null


# ===========================================================================
hdr "case_214e61d3: ready needs the linter and a second-model reputation receipt"

run_ready_mem() { # fixturesdir memorydir
  OUT="$(UPSTREAM_MEMORY_DIR="$2" "$TOOL" ready --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$1" --dry-run 2>&1)"; RC=$?
}

F="$WORK/n_ok"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream"
run_ready "$F"
if [ "$RC" -eq 0 ] && grep -q "PASS.*lint passed" <<<"$OUT" && grep -q "PASS.*reputation clean" <<<"$OUT"; then
  ok "baseline: lint and reputation items are present and green at ready";
else bad "baseline: lint and reputation items are present and green at ready" "rc=$RC out=$OUT"; fi

F="$WORK/n_norep"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-reputation
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation: no second-model receipt" <<<"$OUT"; then ok "no reputation receipt blocks ready";
else bad "no reputation receipt blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/n_repfail"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" rep-failed
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT"; then ok "a reputation pass with findings blocks ready";
else bad "a reputation pass with findings blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/n_repsame"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" rep-samefamily
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation.*not different" <<<"$OUT"; then ok "a same-family reviewer receipt blocks ready";
else bad "a same-family reviewer receipt blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/n_edit"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" edit-after-pass
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation.*different PR text" <<<"$OUT"; then ok "editing the PR text after the pass invalidates it";
else bad "editing the PR text after the pass invalidates it" "rc=$RC out=$OUT"; fi

F="$WORK/n_notest"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-test
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*lint failed.*tests" <<<"$OUT"; then ok "a behavior change without a test blocks ready (linter)";
else bad "a behavior change without a test blocks ready (linter)" "rc=$RC out=$OUT"; fi

F="$WORK/n_leak"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" leak-in-diff
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*lint failed.*internal" <<<"$OUT"; then ok "an internal id in the diff blocks ready (linter)";
else bad "an internal id in the diff blocks ready (linter)" "rc=$RC out=$OUT"; fi

F="$WORK/n_huge"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" huge-diff
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*lint failed.*budget" <<<"$OUT"; then ok "an over-budget diff blocks ready (linter)";
else bad "an over-budget diff blocks ready (linter)" "rc=$RC out=$OUT"; fi

F="$WORK/n_nomem"; mkdir -p "$F/mirror" "$F/upstream" "$WORK/nomem"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream"
run_ready_mem "$F" "$WORK/nomem"
if [ "$RC" -eq 2 ] && grep -qi "REFUSED" <<<"$OUT"; then ok "no maintainer memory refuses ready (exit 2)";
else bad "no maintainer memory refuses ready (exit 2)" "rc=$RC out=$OUT"; fi

F="$WORK/n_nocomments"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream"; rm "$F/mirror/comments.json"
run_ready "$F"
if [ "$RC" -eq 2 ]; then ok "unreadable receipts (no mirror comments) refuse ready, never pass";
else bad "unreadable receipts (no mirror comments) refuse ready, never pass" "rc=$RC out=$OUT"; fi

F="$WORK/n_stranger"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" rep-stranger
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation: no second-model receipt" <<<"$OUT"; then ok "a receipt comment written by someone else is not trusted";
else bad "a receipt comment written by someone else is not trusted" "rc=$RC out=$OUT"; fi

F="$WORK/n_otherhead"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" rep-other-head
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation: no second-model receipt" <<<"$OUT"; then ok "a receipt for another head does not count";
else bad "a receipt for another head does not count" "rc=$RC out=$OUT"; fi

F="$WORK/n_nofiles"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream"; rm "$F/upstream/files.json"
run_ready "$F"
if [ "$RC" -eq 2 ]; then ok "an unreadable diff refuses ready, never passes";
else bad "an unreadable diff refuses ready, never passes" "rc=$RC out=$OUT"; fi

F="$WORK/n_gatebox"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" gate-boxes-open
run_ready "$F"
if [ "$RC" -eq 0 ] && grep -q "would tick 2 gate-verified" <<<"$OUT"; then ok "boxes the code measures (CI, Greptile) are ticked by ready, not by hand";
else bad "boxes the code measures (CI, Greptile) are ticked by ready, not by hand" "rc=$RC out=$OUT"; fi

F="$WORK/n_unchecked"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" unchecked-boxes
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*up-checklist" <<<"$OUT"; then ok "an author-declared box left unticked still blocks ready";
else bad "an author-declared box left unticked still blocks ready" "rc=$RC out=$OUT"; fi

F="$WORK/n_cipend"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror" ci-pending; mk_upstream "$F/upstream"
run_ready "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*fork-ci" <<<"$OUT"; then ok "ready refuses while fork CI is still running";
else bad "ready refuses while fork CI is still running" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "case_214e61d3: status reports the three parallel gates on one head"

run_status() { OUT="$("$TOOL" status --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$1" "${@:2}" 2>&1)"; RC=$?; }

F="$WORK/s_green"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream"
run_status "$F"
if [ "$RC" -eq 0 ] && grep -q "ALL THREE GREEN" <<<"$OUT"; then ok "all three gates green is exit 0";
else bad "all three gates green is exit 0" "rc=$RC out=$OUT"; fi

F="$WORK/s_gpend"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-comment
run_status "$F"
if [ "$RC" -eq 3 ] && grep -q "PENDING" <<<"$OUT" && grep -q "greptile *pending" <<<"$OUT"; then ok "Greptile not yet scored is pending (exit 3), not red";
else bad "Greptile not yet scored is pending (exit 3), not red" "rc=$RC out=$OUT"; fi

F="$WORK/s_stale"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" stale-score
run_status "$F"
if [ "$RC" -eq 3 ] && grep -q "greptile *pending" <<<"$OUT"; then ok "a Greptile score on an older head is pending until re-review";
else bad "a Greptile score on an older head is pending until re-review" "rc=$RC out=$OUT"; fi

F="$WORK/s_cipend"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror" ci-pending; mk_upstream "$F/upstream"
run_status "$F"
if [ "$RC" -eq 3 ] && grep -q "fork-ci *pending" <<<"$OUT"; then ok "fork CI still running is pending";
else bad "fork CI still running is pending" "rc=$RC out=$OUT"; fi

F="$WORK/s_red"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror" ci-red-latest; mk_upstream "$F/upstream"
run_status "$F"
if [ "$RC" -eq 1 ] && grep -q "fork-ci *red" <<<"$OUT" && grep -q "RED" <<<"$OUT"; then ok "a failed gate is red (exit 1)";
else bad "a failed gate is red (exit 1)" "rc=$RC out=$OUT"; fi

F="$WORK/s_redpend"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror" ci-red-latest; mk_upstream "$F/upstream" no-comment
run_status "$F"
if [ "$RC" -eq 1 ]; then ok "red wins over pending";
else bad "red wins over pending" "rc=$RC out=$OUT"; fi

F="$WORK/s_score4"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" score-4
run_status "$F"
if [ "$RC" -eq 1 ] && grep -q "greptile *red" <<<"$OUT"; then ok "Greptile 4/5 is red";
else bad "Greptile 4/5 is red" "rc=$RC out=$OUT"; fi

F="$WORK/s_empty"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror" ci-empty; mk_upstream "$F/upstream"
run_status "$F"
if [ "$RC" -eq 2 ] && grep -q "REFUSED" <<<"$OUT"; then ok "an unmeasurable gate refuses (exit 2), never green";
else bad "an unmeasurable gate refuses (exit 2), never green" "rc=$RC out=$OUT"; fi

F="$WORK/s_green"
run_status "$F" --json
if printf '%s' "$OUT" | python3 -c '
import json, sys
v = json.load(sys.stdin)
assert v["all_green"] is True and v["exit"] == 0 and v["pr"] == 15142
for g in ("fork-ci", "our-review", "greptile", "upstream-ci", "lint", "reputation"):
    assert v["gates"][g]["state"] == "green", g
' 2>/dev/null; then ok "status --json is a machine-readable record of every gate";
else bad "status --json is a machine-readable record of every gate" "rc=$RC out=$OUT"; fi

F="$WORK/s_norep"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" no-reputation
run_status "$F"
if [ "$RC" -eq 0 ] && grep -q "reputation *red" <<<"$OUT"; then ok "the three gates decide the verdict; a missing reputation receipt is shown but only blocks ready";
else bad "the three gates decide the verdict; a missing reputation receipt is shown but only blocks ready" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "case_214e61d3: audit enforces the pipeline only on v2 PRs"

F="$WORK/a_legacy"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
run_audit "$F"
if [ "$RC" -eq 0 ] && grep -q "legacy PR" <<<"$OUT"; then ok "a legacy ready PR without a receipt is NOT converted to draft";
else bad "a legacy ready PR without a receipt is NOT converted to draft" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2bad"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published v2 no-reputation
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT" && grep -q "convert to draft now" <<<"$OUT"; then ok "a v2 ready PR without the reputation receipt is flagged for conversion";
else bad "a v2 ready PR without the reputation receipt is flagged for conversion" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2receipt"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
add_lint_receipt "$F/mirror/comments.json" rick "$SHA"
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then
  ok "a trusted lint receipt preserves v2 enforcement after the body marker is deleted";
else bad "a trusted lint receipt preserves v2 enforcement after the body marker is deleted" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2untrusted"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
add_lint_receipt "$F/mirror/comments.json" stranger "$SHA"
run_audit "$F"
if [ "$RC" -eq 0 ] && grep -q "legacy PR" <<<"$OUT"; then ok "an untrusted v2-looking comment is not pipeline provenance";
else bad "an untrusted v2-looking comment is not pipeline provenance" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2stale"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
add_lint_receipt "$F/mirror/comments.json" rick "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then ok "a trusted stale-head v2 lint receipt preserves provenance";
else bad "a trusted stale-head v2 lint receipt preserves provenance" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2unreadable"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
printf '{malformed' > "$F/mirror/comments.json"
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "pipeline-provenance: REFUSED" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then ok "unreadable markerless provenance refuses instead of downgrading to legacy";
else bad "unreadable markerless provenance refuses instead of downgrading to legacy" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2wrongshape"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
printf '["not-a-comment-object"]' > "$F/mirror/comments.json"
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "pipeline-provenance: REFUSED" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then ok "a provenance probe that crashes refuses instead of downgrading to legacy";
else bad "a provenance probe that crashes refuses instead of downgrading to legacy" "rc=$RC out=$OUT"; fi

# An engine that dies before it prints a verdict is a refusal, never an empty pass.
STUB="$WORK/crashing-lint"; mkdir -p "$STUB"
cp "$HERE/upstream_gate.sh" "$HERE/upstream_reputation.py" "$STUB/"
printf 'import sys\nsys.exit(1)\n' > "$STUB/upstream_lint.py"
F="$WORK/a_v2crash"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" v2
OUT="$("$STUB/upstream_gate.sh" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && grep -q "lint: REFUSED" <<<"$OUT" && ! grep -q "AUDIT: PASS" <<<"$OUT"; then ok "a linter that crashes before a verdict refuses the audit";
else bad "a linter that crashes before a verdict refuses the audit" "rc=$RC out=$OUT"; fi

STUB="$WORK/crashing-reputation"; mkdir -p "$STUB"
cp "$HERE/upstream_gate.sh" "$HERE/upstream_lint.py" "$STUB/"
printf 'import sys\nsys.exit(1)\n' > "$STUB/upstream_reputation.py"
F="$WORK/a_v2repcrash"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published v2
OUT="$("$STUB/upstream_gate.sh" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && grep -q "reputation: REFUSED" <<<"$OUT" && ! grep -q "AUDIT: PASS" <<<"$OUT"; then ok "a reputation engine that crashes before a verdict refuses the audit";
else bad "a reputation engine that crashes before a verdict refuses the audit" "rc=$RC out=$OUT"; fi

# An evaluator that crashes (a null review thread) must not leave a blank passing item.
F="$WORK/a_evalcrash"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published v2
printf '[null]' > "$F/upstream/threads.json"
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "greptile: REFUSED" <<<"$OUT" && ! grep -q "AUDIT: PASS" <<<"$OUT"; then ok "an audit evaluator that crashes refuses instead of passing a blank item";
else bad "an audit evaluator that crashes refuses instead of passing a blank item" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2missing-comments"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
rm -f "$F/mirror/comments.json"
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "pipeline-provenance: REFUSED" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then ok "missing markerless provenance data refuses instead of downgrading to legacy";
else bad "missing markerless provenance data refuses instead of downgrading to legacy" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2missing-mirror"; mkdir -p "$F/upstream"; mk_upstream "$F/upstream" published no-reputation
run_audit "$F"
if [ "$RC" -eq 2 ] && grep -q "pipeline-provenance: REFUSED" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then
  ok "markerless provenance with no mirror refuses instead of downgrading to legacy"
else
  bad "markerless provenance with no mirror refuses instead of downgrading to legacy" "rc=$RC out=$OUT"
fi

# Put the marker before text larger than a pipe buffer: early-exiting grep
# must not strand jq and silently classify this v2 PR as legacy.
F="$WORK/a_v2large"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published v2 no-reputation
python3 - "$F/upstream/pr.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    pr = json.load(f)
pr["body"] += "\n" + "Additional verification detail.\n" * 20000
with open(path, "w") as f:
    json.dump(pr, f)
PY
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*reputation" <<<"$OUT" && ! grep -q "legacy PR" <<<"$OUT"; then ok "a large v2 body still requires its reputation receipt";
else bad "a large v2 body still requires its reputation receipt" "rc=$RC out=$OUT"; fi

F="$WORK/a_legacylarge"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published no-reputation
python3 - "$F/upstream/pr.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    pr = json.load(f)
pr["body"] += "\n" + "Additional verification detail.\n" * 20000
with open(path, "w") as f:
    json.dump(pr, f)
PY
run_audit "$F"
if [ "$RC" -eq 0 ] && grep -q "legacy PR" <<<"$OUT"; then ok "a large legacy body without the marker stays legacy";
else bad "a large legacy body without the marker stays legacy" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2draft"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" v2 no-reputation
run_audit "$F"
if [ "$RC" -eq 0 ]; then ok "a v2 DRAFT does not need the receipt yet";
else bad "a v2 DRAFT does not need the receipt yet" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2good"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" published v2
run_audit "$F"
if [ "$RC" -eq 0 ] && grep -q "AUDIT: PASS" <<<"$OUT"; then ok "a v2 ready PR with lint and receipt passes audit";
else bad "a v2 ready PR with lint and receipt passes audit" "rc=$RC out=$OUT"; fi

F="$WORK/a_v2lint"; mkdir -p "$F/mirror" "$F/upstream"; mk_mirror "$F/mirror"; mk_upstream "$F/upstream" v2 no-test
run_audit "$F"
if [ "$RC" -eq 1 ] && grep -q "FAIL.*lint" <<<"$OUT"; then ok "a v2 PR that fails the linter fails audit even as a draft";
else bad "a v2 PR that fails the linter fails audit even as a draft" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" audit --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$WORK/a_v2good" --json 2>&1)"; RC=$?
if printf '%s' "$OUT" | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["v2"] is True and v["pass"] is True' 2>/dev/null; then
  ok "audit --json carries the v2 flag for the watcher";
else bad "audit --json carries the v2 flag for the watcher" "rc=$RC out=$OUT"; fi

# ===========================================================================
hdr "case_214e61d3: receipts and the lint command"

F="$WORK/s_green"
OUT="$("$TOOL" receipts --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "| fork-ci | green" <<<"$OUT" && grep -q "| reputation | green" <<<"$OUT" \
   && grep -q 'receipt `upstream-gate/reputation`: success' <<<"$OUT"; then ok "receipts prints the markdown block read back from the layout";
else bad "receipts prints the markdown block read back from the layout" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" lint --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "LINT: PASS" <<<"$OUT"; then ok "lint subcommand passes the baseline PR";
else bad "lint subcommand passes the baseline PR" "rc=$RC out=$OUT"; fi
F="$WORK/n_notest"
OUT="$("$TOOL" lint --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$F" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && grep -q "FAIL .*tests" <<<"$OUT"; then ok "lint subcommand fails a behavior change without a test";
else bad "lint subcommand fails a behavior change without a test" "rc=$RC out=$OUT"; fi

OUT="$("$TOOL" reputation --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$WORK/s_green" --response-file /dev/null 2>&1)"; RC=$?
if [ "$RC" -eq 2 ]; then ok "an empty reviewer reply refuses, never passes";
else bad "an empty reviewer reply refuses, never passes" "rc=$RC out=$OUT"; fi
printf '{"verdict":"clean","findings":[]}' > "$WORK/clean_reply.json"
OUT="$(UPSTREAM_RECEIPT_DIR="$WORK/rcpt" "$TOOL" reputation --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$WORK/s_green" --response-file "$WORK/clean_reply.json" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "REPUTATION: PASS" <<<"$OUT" && [ -n "$(ls "$WORK/rcpt" 2>/dev/null)" ]; then ok "reputation subcommand records a receipt on a clean reply (offline seam)";
else bad "reputation subcommand records a receipt on a clean reply (offline seam)" "rc=$RC out=$OUT"; fi
OUT="$(UPSTREAM_RECEIPT_DIR="$WORK/rcpt" "$TOOL" reputation --upstream-pr 15142 --upstream-repo paperclipai/paperclip --fixtures "$WORK/s_green" --response-file "$WORK/clean_reply.json" --post-receipt 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && grep -qi "refused.*response-file" <<<"$OUT"; then ok "reputation offline seam with --post-receipt refuses, never posts";
else bad "reputation offline seam with --post-receipt refuses, never posts" "rc=$RC out=$OUT"; fi
# `ready` must write the ticked body byte-for-byte: jq -r appends a newline,
# which changes the reputation text hash and fails the next audit (3/5 at f57ac32).
ready_body_lines="$(grep "ready_body" "$HERE/upstream_gate.sh")"
if grep -Fq "jq -j" <<<"$ready_body_lines" && ! grep -Fq "jq -r" <<<"$ready_body_lines"; then ok "ready writes the ticked body byte-for-byte (jq -j)";
else bad "ready writes the ticked body byte-for-byte (jq -j)" "ready_body line still uses jq -r"; fi
printf '%s' '{"title":"t","body":"line\n"}' > "$WORK/nl_pr.json"
if [ "$(jq -j '.body // ""' "$WORK/nl_pr.json" | od -An -tx1 | tr -d ' \n')" = "$(printf 'line\n' | od -An -tx1 | tr -d ' \n')" ]; then ok "jq -j preserves a trailing newline without adding one";
else bad "jq -j preserves a trailing newline without adding one" "jq -j added or dropped a byte"; fi

# ===========================================================================
hdr "compat profiles: reviewed repo-specific CI profiles fail closed (G0-G31)"

# The profile registry is co-located with the gate, so these cases run a copy
# of the tool from a temp dir whose upstream_ci_profiles.json the suite owns.
COMPAT="$WORK/compat-tool"; mkdir -p "$COMPAT"
cp "$HERE/upstream_gate.sh" "$HERE/upstream_lint.py" "$HERE/upstream_reputation.py" "$COMPAT/"
chmod +x "$COMPAT/upstream_gate.sh"
CTOOL="$COMPAT/upstream_gate.sh"

write_compat_profiles() { # variant: good|no-cli|upstream-pending|badjson|bad-schema|empty-required|nonstring|smuggled|drift
  python3 - "$COMPAT/upstream_ci_profiles.json" "${1:-good}" <<'PYPROF'
import json, sys
path, variant = sys.argv[1], sys.argv[2]
fork_names = ["build", "ensure-no-translator-changes", "close-when-agents-md-changed"]
up_names = fork_names + ["Greptile Review"]
def ev(policy):
    return {"policy_ref": policy,
            "workflows_manifest": [{"file": f, "blob_sha": "blob-" + f} for f in
                                   ("pr-test-build.yml", "pr-path-guard.yml", "agents-md-guard.yml")],
            "check_run_names": list(fork_names), "checked_at": "2026-10-18"}
data = {"schema_version": 1, "profiles": {
    "TogetherWeOwn/paperclip": {
        "fork_required": ["ci / verify", "ci / e2e", "Paperclip Review"],
        "upstream_required": ["ci / verify", "ci / e2e", "Greptile Review"],
        "status": "reviewed", "evidence": ev("master@fixture")},
    "paperclipai/paperclip": {
        "fork_required": ["ci / verify", "ci / e2e", "Paperclip Review"],
        "upstream_required": ["ci / verify", "ci / e2e", "Greptile Review"],
        "status": "reviewed", "evidence": ev("alias-preserved@fixture")},
    "TogetherWeOwn/CLIProxyAPI": {
        "fork_required": fork_names, "upstream_required": up_names,
        "status": "reviewed", "evidence": ev("dev@e9af1ba")},
    "router-for-me/CLIProxyAPI": {
        "fork_required": fork_names, "upstream_required": up_names,
        "status": "reviewed", "evidence": ev("main@fixture")}}}
cli = data["profiles"]["TogetherWeOwn/CLIProxyAPI"]
if variant == "no-cli":
    data["profiles"].pop("TogetherWeOwn/CLIProxyAPI")
elif variant == "upstream-pending":
    data["profiles"]["router-for-me/CLIProxyAPI"]["status"] = "pending-live-confirmation"
elif variant == "bad-schema":
    data["schema_version"] = 2
elif variant == "empty-required":
    cli["fork_required"] = []
elif variant == "nonstring":
    cli["fork_required"] = ["build", 42]
elif variant == "smuggled":
    cli["fork_required"] = fork_names + ["Paperclip Review"]
elif variant == "drift":
    cli["evidence"]["workflows_manifest"][0]["blob_sha"] = "stale-blob"
elif variant == "badjson":
    open(path, "w").write("{not json")
    raise SystemExit
json.dump(data, open(path, "w"))
PYPROF
}

# Maintainer memory for the compat repos (same world the fixture bodies satisfy).
python3 - "$HERE" "$UPSTREAM_MEMORY_DIR" <<'PYMEM2'
import sys
here, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)
import upstream_lint as ul
for repo in ("router-for-me/CLIProxyAPI", "TogetherWeOwn/CLIProxyAPI",
               "TogetherWeOwn/paperclip"):
    mem = ul.load_memory("paperclipai/paperclip", out)
    mem["repo"] = repo
    ul.save_memory(repo, mem, out)
PYMEM2

# Mirror fixture whose check runs carry the profiled rendered names, exact-head
# sha and app provenance. Args: dir [mutation]
mk_cli_mirror() {
  local dir="$1" mut="${2:-good}"
  mk_mirror "$dir"
  python3 - "$dir/check_runs.json" "$SHA" "$mut" <<'PYRUNS'
import json, sys
path, sha, mut = sys.argv[1], sys.argv[2], sys.argv[3]
def r(name, status, concl, i, hs="HEAD", app="github-actions", out=None, ts="2026-10-06T03:00:00Z"):
    d = {"name": name, "status": status, "conclusion": concl,
         "completed_at": ts, "started_at": ts, "id": i,
         "app": {"slug": app}, "output": out or {"title": "", "summary": ""}}
    if hs == "HEAD":
        d["head_sha"] = sha
    elif hs is not None:
        d["head_sha"] = hs
    return d
runs = [r("build", "completed", "success", 11),
        r("ensure-no-translator-changes", "completed", "success", 12),
        r("close-when-agents-md-changed", "completed", "success", 13),
        r("Paperclip Review", "completed", "success", 14,
          out={"title": "5/5", "summary": "## Paperclip Review — 5/5"})]
if mut == "missing-one":
    runs = [x for x in runs if x["name"] != "ensure-no-translator-changes"]
elif mut == "red-latest":
    runs.append(r("build", "completed", "failure", 21, ts="2026-10-06T04:00:00Z"))
elif mut == "queued":
    runs[0] = r("build", "queued", None, 21, ts="2026-10-06T04:00:00Z")
elif mut == "queued-no-time":
    q = r("build", "queued", None, 21)
    q["completed_at"] = None; q["started_at"] = None
    runs[0] = q
elif mut == "in-progress":
    runs[0] = r("build", "in_progress", None, 21, ts="2026-10-06T04:00:00Z")
elif mut == "zero":
    runs = []
elif mut == "skipped":
    runs[0] = r("build", "completed", "skipped", 11)
elif mut == "neutral":
    runs[0] = r("build", "completed", "neutral", 11)
elif mut == "empty-conclusion":
    runs[0] = r("build", "completed", "", 11)
elif mut == "unknown-conclusion":
    runs[0] = r("build", "completed", "mystery", 11)
elif mut == "stale-head":
    runs[0] = r("build", "completed", "success", 11, hs="a" * 40)
elif mut == "optional-skipped":
    runs.append(r("[code]smith", "completed", "skipped", 31, app="blacksmith-sh"))
elif mut == "run-no-headsha":
    runs[0] = r("build", "completed", "success", 11, hs=None)
elif mut == "review-no-score":
    runs[3] = r("Paperclip Review", "completed", "success", 14,
                out={"title": "reviewed", "summary": "looks fine"})
elif mut == "bad-extra":
    runs.append(r("docs-link-check", "completed", "failure", 31, ts="2026-10-06T04:00:00Z"))
json.dump({"total_count": len(runs), "check_runs": runs}, open(path, "w"))
PYRUNS
  if [ "$mut" = "pr-no-headsha" ]; then
    jq 'del(.head.sha)' "$dir/pr.json" > "$dir/pr.json.tmp" && mv "$dir/pr.json.tmp" "$dir/pr.json"
  fi
  # The pinned workflow manifest the live side resolves against (fixture form).
  cat > "$dir/workflows.json" <<'JSONWF'
[{"file": "pr-test-build.yml", "blob_sha": "blob-pr-test-build.yml"},
 {"file": "pr-path-guard.yml", "blob_sha": "blob-pr-path-guard.yml"},
 {"file": "agents-md-guard.yml", "blob_sha": "blob-agents-md-guard.yml"}]
JSONWF
}

# Overwrite an upstream fixture's check runs with the profiled upstream names.
mk_cli_upstream_runs() { # dir mutation
  python3 - "$1/check_runs.json" "$SHA" "${2:-good}" <<'PYRUNS'
import json, sys
path, sha, mut = sys.argv[1], sys.argv[2], sys.argv[3]
def r(name, status, concl, i, hs="HEAD", ts="2026-10-06T03:00:00Z"):
    d = {"name": name, "status": status, "conclusion": concl,
         "completed_at": ts, "started_at": ts, "id": i,
         "app": {"slug": "github-actions"}, "output": {"title": "", "summary": ""}}
    d["head_sha"] = sha if hs == "HEAD" else hs
    return d
runs = [r("build", "completed", "success", 41),
        r("ensure-no-translator-changes", "completed", "success", 42),
        r("close-when-agents-md-changed", "completed", "success", 43),
        r("Greptile Review", "completed", "success", 44)]
if mut == "missing-one":
    runs = [x for x in runs if x["name"] != "close-when-agents-md-changed"]
elif mut == "red-latest":
    runs.append(r("build", "completed", "failure", 51, ts="2026-10-06T04:00:00Z"))
elif mut == "skipped":
    runs[1] = r("ensure-no-translator-changes", "completed", "skipped", 42)
elif mut == "stale-head":
    runs[0] = r("build", "completed", "success", 41, hs="b" * 40)
json.dump({"total_count": len(runs), "check_runs": runs}, open(path, "w"))
PYRUNS
}

run_compat_check() { OUT="$("$CTOOL" check --mirror-pr 52 --fork-repo TogetherWeOwn/CLIProxyAPI --fixtures "$1" 2>&1)"; RC=$?; }
run_compat_ready() { OUT="$("$CTOOL" ready --upstream-pr 15142 --upstream-repo router-for-me/CLIProxyAPI --mirror-pr 52 --fork-repo TogetherWeOwn/CLIProxyAPI --fixtures "$1" --dry-run 2>&1)"; RC=$?; }
run_compat_audit() { OUT="$("$CTOOL" audit --upstream-pr 15142 --upstream-repo router-for-me/CLIProxyAPI --fixtures "$1" 2>&1)"; RC=$?; }
expect() { # label want_rc patterns...
  local label="$1" want="$2"; shift 2
  local p
  if [ "$RC" -ne "$want" ]; then bad "$label" "rc=$RC want=$want out=$OUT"; return; fi
  for p in "$@"; do
    if ! grep -q -- "$p" <<<"$OUT"; then bad "$label" "missing pattern [$p] rc=$RC out=$OUT"; return; fi
  done
  ok "$label"
}

write_compat_profiles good

# G0 controls: the profiled world passes on its own rendered names.
M="$WORK/c_good"; mk_cli_mirror "$M" good
run_compat_check "$M"
expect "G0 profiled mirror passes Gate A on CLIProxyAPI check names" 0 "GATE A: PASS"

F="$WORK/c_ready_good"; mkdir -p "$F/mirror" "$F/upstream"
mk_cli_mirror "$F/mirror" good; mk_upstream "$F/upstream"; mk_cli_upstream_runs "$F/upstream" good
run_compat_ready "$F"
expect "G0 profiled upstream world is ready" 0 "READY"

# G1-G10 fork-side predicate.
M="$WORK/c_g1"; mk_cli_mirror "$M" missing-one; run_compat_check "$M"
expect "G1 missing profiled fork check fails fork-ci [pending]" 1 "FAIL.*fork-ci" "\[pending\]"

M="$WORK/c_g2"; mk_cli_mirror "$M" red-latest; run_compat_check "$M"
expect "G2 red latest run fails fork-ci (latest wins)" 1 "FAIL.*fork-ci"

M="$WORK/c_g3"; mk_cli_mirror "$M" queued; run_compat_check "$M"
expect "G3 queued profiled check fails fork-ci [pending]" 1 "FAIL.*fork-ci" "\[pending\]"

M="$WORK/c_g3b"; mk_cli_mirror "$M" queued-no-time; run_compat_check "$M"
expect "G3 queued profiled check with null timestamps fails fork-ci [pending]" 1 "FAIL.*fork-ci" "\[pending\]"

M="$WORK/c_g3c"; mk_cli_mirror "$M" in-progress; run_compat_check "$M"
expect "G3 in-progress profiled check fails fork-ci [pending]" 1 "FAIL.*fork-ci" "\[pending\]"

M="$WORK/c_g4"; mk_cli_mirror "$M" zero; run_compat_check "$M"
expect "G4 zero check runs refuses (exit 2)" 2 "REFUSED"

M="$WORK/c_g5"; mk_cli_mirror "$M" skipped; run_compat_check "$M"
expect "G5 skipped required check fails fork-ci, never passes" 1 "FAIL.*fork-ci"

M="$WORK/c_g6"; mk_cli_mirror "$M" neutral; run_compat_check "$M"
expect "G6 neutral required check fails fork-ci, never passes" 1 "FAIL.*fork-ci"

M="$WORK/c_g7"; mk_cli_mirror "$M" empty-conclusion; run_compat_check "$M"
expect "G7 empty conclusion on required check fails fork-ci" 1 "FAIL.*fork-ci"

M="$WORK/c_g7b"; mk_cli_mirror "$M" unknown-conclusion; run_compat_check "$M"
expect "G7 unknown conclusion on required check fails fork-ci" 1 "FAIL.*fork-ci"

M="$WORK/c_g8"; mk_cli_mirror "$M" stale-head; run_compat_check "$M"
expect "G8 success on a stale head fails fork-ci stale [pending]" 1 "FAIL.*fork-ci" "stale" "\[pending\]"

M="$WORK/c_g9"; mk_cli_mirror "$M" run-no-headsha; run_compat_check "$M"
expect "G9 required run without head_sha refuses (exit 2)" 2 "REFUSED"

M="$WORK/c_g9b"; mk_cli_mirror "$M" pr-no-headsha; run_compat_check "$M"
expect "G9 pr.json without head sha refuses (exit 2)" 2 "REFUSED"

M="$WORK/c_g10"; mk_cli_mirror "$M" optional-skipped; run_compat_check "$M"
expect "G10 optional skipped check never fails a green profiled set" 0 "GATE A: PASS"

# G11-G13 registry refusals.
M="$WORK/c_good"
write_compat_profiles no-cli; run_compat_check "$M"
expect "G11 repo without a reviewed profile refuses" 2 "REFUSED unknown/unreviewed CI profile"

write_compat_profiles badjson; run_compat_check "$M"
expect "G12 unparseable profile file refuses as malformed" 2 "REFUSED malformed CI profile"

write_compat_profiles bad-schema; run_compat_check "$M"
expect "G12 bad schema_version refuses as malformed" 2 "REFUSED malformed CI profile"

write_compat_profiles empty-required; run_compat_check "$M"
expect "G12 empty required list refuses as malformed" 2 "REFUSED malformed CI profile"

write_compat_profiles nonstring; run_compat_check "$M"
expect "G12 non-string required entry refuses as malformed" 2 "REFUSED malformed CI profile"

write_compat_profiles smuggled; run_compat_check "$M"
expect "G12 review check smuggled into fork_required refuses as malformed" 2 "REFUSED malformed CI profile"

write_compat_profiles drift; run_compat_check "$M"
expect "G13 workflow blob drift refuses as profile-stale" 2 "REFUSED profile-stale"

write_compat_profiles good

# G14 review independence: a green profile cannot rescue a scoreless review.
M="$WORK/c_g14"; mk_cli_mirror "$M" review-no-score; run_compat_check "$M"
expect "G14 Paperclip Review success without 5/5 still fails our-review" 1 "FAIL.*our-review"

# G15-G20 ready-side gates in the profiled world.
mk_compat_ready_world() { # dir upstream-mutation upstream-runs-mutation
  local f="$1"; shift
  mkdir -p "$f/mirror" "$f/upstream"
  mk_cli_mirror "$f/mirror" good
  # shellcheck disable=SC2086
  mk_upstream "$f/upstream" $1
  mk_cli_upstream_runs "$f/upstream" "${2:-good}"
}

F="$WORK/c_g15"; mk_compat_ready_world "$F" stale-score; run_compat_ready "$F"
expect "G15 Greptile 5/5 on an old head blocks ready (stale)" 1 "FAIL.*greptile"

F="$WORK/c_g16"; mk_compat_ready_world "$F" score-4; run_compat_ready "$F"
expect "G16 Greptile 4/5 blocks ready" 1 "FAIL.*greptile"

F="$WORK/c_g17"; mk_compat_ready_world "$F" open-thread; run_compat_ready "$F"
expect "G17 unresolved thread blocks ready despite Greptile 5/5" 1 "FAIL.*greptile"

F="$WORK/c_g18"; mk_compat_ready_world "$F" no-link; run_compat_ready "$F"
expect "G18 Greptile score without reviewed-commit link blocks ready" 1 "FAIL.*greptile"

F="$WORK/c_g19"; mk_compat_ready_world "$F" unchecked-boxes; run_compat_ready "$F"
if [ "$RC" -eq 1 ] && { grep -q "FAIL.*up-checklist" <<<"$OUT" || grep -q "FAIL.*lint" <<<"$OUT"; }; then
  ok "G19 ready refused when lint@ready fails on an unticked non-pending box"
else bad "G19 ready refused when lint@ready fails on an unticked non-pending box" "rc=$RC out=$OUT"; fi

F="$WORK/c_g20"; mk_compat_ready_world "$F" edit-after-pass; run_compat_ready "$F"
expect "G20 stale reputation text hash blocks ready" 1 "FAIL.*reputation"

# G11 status arm: an unreviewed upstream profile refuses ready, never passes.
write_compat_profiles upstream-pending
F="$WORK/c_ready_good"; run_compat_ready "$F"
expect "G11 pending-live-confirmation upstream profile refuses ready" 2 "REFUSED unknown/unreviewed CI profile"
write_compat_profiles good

# G22 audit backstop in the profiled world.
F="$WORK/c_g22"; mkdir -p "$F/mirror" "$F/upstream"
mk_cli_mirror "$F/mirror" good; mk_upstream "$F/upstream" published v2 no-reputation; mk_cli_upstream_runs "$F/upstream" good
run_compat_audit "$F"
expect "G22 audit flags a non-draft profiled head with no passing record for draft conversion" 1 "convert to draft now"

# G23 upstream-side mirror of G1/G2/G5/G8 against upstream-ci.
F="$WORK/c_g23a"; mk_compat_ready_world "$F" "" missing-one; run_compat_ready "$F"
expect "G23 missing profiled upstream check fails upstream-ci [pending]" 1 "FAIL.*upstream-ci" "\[pending\]"

F="$WORK/c_g23b"; mk_compat_ready_world "$F" "" red-latest; run_compat_ready "$F"
expect "G23 red latest upstream run fails upstream-ci (latest wins)" 1 "FAIL.*upstream-ci"

F="$WORK/c_g23c"; mk_compat_ready_world "$F" "" skipped; run_compat_ready "$F"
expect "G23 skipped required upstream check fails upstream-ci" 1 "FAIL.*upstream-ci"

F="$WORK/c_g23d"; mk_compat_ready_world "$F" "" stale-head; run_compat_ready "$F"
expect "G23 upstream success on a stale head fails upstream-ci stale [pending]" 1 "FAIL.*upstream-ci" "stale" "\[pending\]"

# Keeper-repo mirror: the preserved TogetherWeOwn/paperclip profile keeps
# the legacy any-run strictness for extras (strict_extra). Args: dir [mutation]
mk_keeper_mirror() {
  local dir="$1" mut="${2:-good}"
  mk_mirror "$dir"
  python3 - "$dir/check_runs.json" "$SHA" "$mut" <<'PYRUNS'
import json, sys
path, sha, mut = sys.argv[1], sys.argv[2], sys.argv[3]
def r(name, status, concl, i, hs="HEAD", app="github-actions", out=None, ts="2026-10-06T03:00:00Z"):
    d = {"name": name, "status": status, "conclusion": concl,
         "completed_at": ts, "started_at": ts, "id": i,
         "app": {"slug": app}, "output": out or {"title": "", "summary": ""}}
    if hs == "HEAD":
        d["head_sha"] = sha
    elif hs is not None:
        d["head_sha"] = hs
    return d
runs = [r("ci / verify", "completed", "success", 11),
        r("ci / e2e", "completed", "success", 12),
        r("Paperclip Review", "completed", "success", 13,
          out={"title": "5/5", "summary": "## Paperclip Review — 5/5"})]
if mut == "bad-extra":
    runs.append(r("docs-link-check", "completed", "failure", 31, ts="2026-10-06T04:00:00Z"))
elif mut == "pending-extra":
    runs.append(r("docs-link-check", "queued", None, 31, ts="2026-10-06T04:00:00Z"))
elif mut == "skipped-extra":
    runs.append(r("docs-link-check", "completed", "skipped", 31, ts="2026-10-06T04:00:00Z"))
json.dump({"total_count": len(runs), "check_runs": runs}, open(path, "w"))
PYRUNS
  # Same pinned workflow manifest the keeper evidence carries (fixture form).
  cat > "$dir/workflows.json" <<'JSONWF'
[{"file": "pr-test-build.yml", "blob_sha": "blob-pr-test-build.yml"},
 {"file": "pr-path-guard.yml", "blob_sha": "blob-pr-path-guard.yml"},
 {"file": "agents-md-guard.yml", "blob_sha": "blob-agents-md-guard.yml"}]
JSONWF
}
run_keeper_check() { OUT="$("$CTOOL" check --mirror-pr 52 --fork-repo TogetherWeOwn/paperclip --fixtures "$1" 2>&1)"; RC=$?; }

# G24-G28 preserved keepers: a failed extra fails, an unfinished extra
# pends, a skipped extra passes, and a non-keeper ignores the same extra.
write_compat_profiles good
K="$WORK/c_k24"; mk_keeper_mirror "$K" good; run_keeper_check "$K"
expect "G24 keeper mirror passes Gate A with no extras" 0 "GATE A: PASS"

K="$WORK/c_k25"; mk_keeper_mirror "$K" bad-extra; run_keeper_check "$K"
expect "G25 keeper fails fork-ci on a failed extra run" 1 "FAIL.*fork-ci" "docs-link-check"

K="$WORK/c_k26"; mk_keeper_mirror "$K" pending-extra; run_keeper_check "$K"
expect "G26 keeper fails fork-ci [pending] on an unfinished extra run" 1 "FAIL.*fork-ci" "\[pending\]"

K="$WORK/c_k27"; mk_keeper_mirror "$K" skipped-extra; run_keeper_check "$K"
expect "G27 keeper passes Gate A with a skipped extra" 0 "GATE A: PASS"

M="$WORK/c_k28"; mk_cli_mirror "$M" bad-extra; run_compat_check "$M"
expect "G28 non-keeper ignores the same failed extra" 0 "GATE A: PASS"

# Keeper upstream mirror: the preserved paperclipai/paperclip profile keeps
# the legacy any-run strictness for extras on the upstream-ci side.
# Overwrites an upstream fixture's check runs with the keeper upstream
# names on the exact head. Args: dir [mutation]
mk_keeper_upstream_runs() {
  python3 - "$1/check_runs.json" "$SHA" "${2:-good}" <<'PYRUNS'
import json, sys
path, sha, mut = sys.argv[1], sys.argv[2], sys.argv[3]
def r(name, status, concl, i, ts="2026-10-06T03:00:00Z"):
    return {"name": name, "status": status, "conclusion": concl,
            "completed_at": ts, "started_at": ts, "id": i,
            "head_sha": sha, "app": {"slug": "github-actions"},
            "output": {"title": "", "summary": ""}}
runs = [r("ci / verify", "completed", "success", 41),
        r("ci / e2e", "completed", "success", 42),
        r("Greptile Review", "completed", "success", 43)]
if mut == "bad-extra":
    runs.append(r("docs-link-check", "completed", "failure", 51, ts="2026-10-06T04:00:00Z"))
elif mut == "pending-extra":
    runs.append(r("docs-link-check", "queued", None, 51, ts="2026-10-06T04:00:00Z"))
json.dump({"total_count": len(runs), "check_runs": runs}, open(path, "w"))
PYRUNS
}
mk_keeper_ready_world() { # dir upstream-runs-mutation
  local f="$1"
  mkdir -p "$f/mirror" "$f/upstream"
  mk_keeper_mirror "$f/mirror" good
  mk_upstream "$f/upstream"
  mk_keeper_upstream_runs "$f/upstream" "${2:-good}"
}
run_keeper_ready() { OUT="$("$CTOOL" ready --upstream-pr 15142 --upstream-repo paperclipai/paperclip --mirror-pr 52 --fork-repo TogetherWeOwn/paperclip --fixtures "$1" --dry-run 2>&1)"; RC=$?; }

# G29-G31 preserved upstream keeper: a failed extra fails upstream-ci, an
# unfinished extra pends it, and the world is ready with no extras.
write_compat_profiles good
F="$WORK/c_k29"; mk_keeper_ready_world "$F" good; run_keeper_ready "$F"
expect "G29 keeper upstream world is ready with no extras" 0 "READY"

F="$WORK/c_k30"; mk_keeper_ready_world "$F" bad-extra; run_keeper_ready "$F"
expect "G30 keeper fails upstream-ci on a failed extra run" 1 "FAIL.*upstream-ci" "docs-link-check"

F="$WORK/c_k31"; mk_keeper_ready_world "$F" pending-extra; run_keeper_ready "$F"
expect "G31 keeper fails upstream-ci [pending] on an unfinished extra run" 1 "FAIL.*upstream-ci" "\[pending\]"

# ===========================================================================
hdr "dispatch: unmeasured runs never pass"
OUT="$("$TOOL" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ]; then ok "no subcommand refuses (exit 2)"; else bad "no subcommand refuses (exit 2)" "rc=$RC"; fi
OUT="$("$TOOL" frobnicate 2>&1)"; RC=$?
if [ "$RC" -eq 4 ]; then ok "unknown command is usage (exit 4)"; else bad "unknown command is usage (exit 4)" "rc=$RC"; fi

# ===========================================================================
printf '\n\033[1m%s\033[0m\n' "summary"
printf '  %s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
