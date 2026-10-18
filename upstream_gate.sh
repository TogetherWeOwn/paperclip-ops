#!/usr/bin/env bash
# ===========================================================================
# upstream_gate.sh — one deterministic publish tool for upstream PRs (case_3cd3dc9f).
#
# OWNER REQUIREMENT (2026-10-06): "I require all checks and CI and reviews to
# pass before we submit an upstream PR. How can we deterministically ensure
# that?" Answer: code decides, never a ticked box.
#
# GATE A, in our fork (nothing public yet). Every upstream change is first a
# PR in TogetherWeOwn/paperclip against mirror-base/<upstream-base-sha>.
# Pass on the EXACT head SHA means ALL of:
#   1. fork-ci ......... upstream's own CI workflows all green in the fork
#                        (latest run per check name wins, including queued runs;
#                        any failure, or any non-completed run, is FAIL;
#                        zero observed signals is REFUSED, never pass)
#   2. our-review ...... Paperclip Review check run is success with 5/5
#                        in its output on that SHA
#   3. checklist ....... an upstream PR body is generatable with every
#                        section filled from the mirror PR and every [x]
#                        truthful (Greptile box stays [ ] until `ready`)
#   4. no-internal-ids  title, body, branch and commit messages hold no
#                        TOG-/PAP-/PAPA- ids, no deploymentexample host, no tailnet
#                        name, no localhost/agent links
#   5. single-commit ... the mirror PR holds exactly one commit
#
# SUBCOMMANDS
#   check   --mirror-pr N   evaluate Gate A for the mirror PR head (read-only)
#   publish --mirror-pr N --title T --upstream-repo O/R [--serial]
#                               [--maintainer-split-url U]
#                               refuse unless the static Gate A items and the
#                               pre-push linter pass and no gate has failed,
#                               then open the upstream PR AS A DRAFT with the
#                               code-filled body. Parallel by default (case_214e61d3):
#                               fork CI and our review may still be running
#                               beside Greptile; --serial wants them green first.
#                               Also applies the batch policy (fewer, complete
#                               PRs: refuses overlap with our open PRs unless a
#                               maintainer asked to split).
#   status  --upstream-pr N --upstream-repo O/R [--json]
#                               the three parallel gates (fork CI, our review,
#                               Greptile) on one exact head: green, pending or
#                               red each; exit 0 all green, 3 pending, 1 red.
#   lint    --upstream-pr N --upstream-repo O/R [--stage first-push|ready]
#                               the pre-push linter on a PR (before the first
#                               push of a branch: upstream_lint.py lint).
#   reputation --upstream-pr N --upstream-repo O/R [--post-receipt]
#                               second-model reputation pass on the final text;
#                               a different model family, zero findings, bound to
#                               this head and this exact text.
#   receipts --upstream-pr N --upstream-repo O/R
#                               the markdown receipt block for the card, read back
#                               from GitHub, never typed.
#   ready   --upstream-pr N [--mirror-pr M]
#                               refuse unless, on the same upstream head:
#                               upstream CI green incl. Greptile Review success,
#                               Greptile confidence 5/5 ON THAT HEAD with no
#                               unresolved review threads, Gate A still passing
#                               on the mirror side, the linter clean at stage
#                               `ready`, and a clean second-model reputation
#                               receipt for this head and this text. Then tick the
#                               boxes the code measured and mark ready.
#   audit   --upstream-pr N [--enforce] [--json]
#                               watcher backstop: a non-draft upstream PR whose
#                               head has no passing record is reported with
#                               "convert to draft now" plus failing items;
#                               --enforce performs the conversion. The audit also
#                               scores mergeability (mergeable=false or
#                               mergeable_state=dirty is FAIL; mergeable=null is
#                               REFUSED, never a pass). The mergeable transient
#                               blocks PASS but never vetoes conversion when
#                               another item has a measured FAIL (draft is the
#                               safe state); all other refusals still veto.
#                               Mergeability is scored from the post-fetch
#                               re-read, so a transient null on the first GET
#                               does not hide a dirty PR. --json --enforce runs
#                               the same conversion and reports it in the JSON
#                               record ("converted": true, plus "refused": true
#                               when the mergeable transient rides along)
#                               instead of prose.
#
# EXIT CODES — gate on these, never on prose:
#   0  pass (check), or the action completed (publish/ready/audit clean)
#   1  gate FAIL — at least one item measured and failing (itemized on stdout)
#   2  REFUSED — something could not be measured (denied API, no signal,
#                missing input). NEVER a pass. Same convention as
#                gh_ci_status.sh (case_b730ec29) and upstream_filing_readiness.sh.
#   3  PENDING (status only) — nothing failed, a gate is still running
#   4  usage error
#
# MEASURED FACTS (not assumptions), 2026-10-06:
#   - Upstream Greptile REVIEWS DRAFT PRs: draft #15296 shows "Greptile
#     Review: success", draft #15304 shows it failing. So `ready` scores
#     Greptile while the PR is still a draft; no ready->score->draft loop.
#   - Greptile score lives in an issue comment from greptile-apps[bot]
#     holding "<!-- greptile_confidence_score:N -->" plus a
#     "Last reviewed commit: [...](.../commit/<sha>)" line. The score
#     counts ONLY when that SHA equals the PR head.
#   - Paperclip Review score lives in the check run output:
#     name "Paperclip Review", conclusion success, title/summary "5/5".
#   - One commit SHA can be the head of TWO fork PRs (measured: f5bca33 is
#     head of both #13 and #52). Check runs attach to COMMITS, not PRs, so
#     latest-per-name wins across every run on that SHA, fail-closed.
#
# SEAMS (same convention as gh_ci_status.sh / sibling_guard.sh):
#   GH_TOKEN    required for live mode (never on argv; inherited env only)
#   GH_API_URL  default https://api.github.com (test seam)
#   GH_BIN      default gh. Test seam for the gh binary itself: PATH
#               prepending does NOT work on this host (BASH_ENV rewrites
#               PATH for every non-interactive shell), so injection goes
#               through this variable.
#   --fixtures DIR
#               offline mode: read pr.json / check_runs.json / commits.json /
#               comments.json / threads.json from DIR (check) or from
#               DIR/mirror + DIR/upstream (ready, audit). The SAME eval code
#               runs in both modes; live mode only adds the fetch step.
#
# BUNDLE RULE (part of this gate's design): never run `gh pr create` or
# `gh pr ready` against a repo outside TogetherWeOwn directly; always use
# this tool. Raw gh calls skip every check below and are how a red head
# reaches a maintainer.
# ===========================================================================
set -uo pipefail

ME="$(basename "${BASH_SOURCE[0]}")"
SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EXIT_PASS=0; EXIT_FAIL=1; EXIT_REFUSED=2; EXIT_PENDING=3; EXIT_USAGE=4

API="${GH_API_URL:-https://api.github.com}"
# Which gh binary to shell out to. A seam, not a preference: this host
# resets PATH via BASH_ENV for every non-interactive shell, so a test that
# prepends a fake gh to PATH is silently ignored (measured 2026-10-06).
# GH_BIN survives that reset because only PATH is rewritten.
GH="${GH_BIN:-gh}"
FORK_REPO_DEFAULT="TogetherWeOwn/paperclip"

die()  { echo "$ME: $*" >&2; exit $EXIT_USAGE; }
refuse() { echo "$ME: REFUSED: $*" >&2; exit $EXIT_REFUSED; }

# Evaluator lines that already carry a space-separated REFUSED token pass
# through verbatim (exact-token assertions); legacy TAB lines keep the old
# prefix form so unenrolled-repo output is byte-identical.
emit_refused() {
  case "$1" in
    "REFUSED "*) printf '%s\n' "$1";;
    *) echo "REFUSED ${1#REFUSED}";;
  esac
}

command -v jq >/dev/null 2>&1 || { echo "$ME: jq is required" >&2; exit $EXIT_USAGE; }
command -v python3 >/dev/null 2>&1 || { echo "$ME: python3 is required" >&2; exit $EXIT_USAGE; }

WORK=""
trap '[ -n "$WORK" ] && rm -rf "$WORK"' EXIT
new_work() { WORK="$(mktemp -d "${TMPDIR:-/tmp}/upstream_gate.XXXXXXXX")" || refuse "cannot create work dir"; }

need_gh() {
  command -v "$GH" >/dev/null 2>&1 || refuse "gh CLI is required for live mode (use --fixtures for offline eval)"
}

# --- API fetch layer (live mode only) -----------------------------------------
# Writes the standard layout into $1: pr.json check_runs.json commits.json
# comments.json threads.json. Anything unmeasurable is a refusal, never empty.
api_get() { # path -> stdout; caller checks status
  local path="$1" out="$2"
  local code
  if [ -n "${GH_TOKEN:-}" ]; then
    local hdrs="$WORK/hdrs"; [ -f "$hdrs" ] || {
      : > "$hdrs"; chmod 600 "$hdrs"
      { printf 'Authorization: Bearer %s\n' "$GH_TOKEN"
        printf 'Accept: application/vnd.github+json\n'
        printf 'X-GitHub-Api-Version: 2022-11-28\n'; } > "$hdrs"
    }
    code="$(curl -sS -m 60 -o "$out" -w '%{http_code}' -H "@$hdrs" "$API$path" 2>/dev/null || echo 000)"
  else
    # No --paginate here, ever: on object responses (check runs) gh
    # concatenates per-page OBJECTS into invalid JSON. Callers page
    # explicitly through fetch_all_check_runs / fetch_all_items.
    code="$("$GH" api "$path" > "$out" 2>"$WORK/gh.err" && echo 200 || echo 000)"
  fi
  printf '%s' "$code"
}

# Array endpoints (commits, comments) are paged the same explicit way, for
# the same reason: --paginate is banned in api_get.
fetch_all_items() { # path-prefix label dest
  local path="$1" dest="$2" label="$3" page=1 n=100
  local pages="$WORK/ap_pages"; mkdir -p "$pages"; rm -f "$pages"/p*
  while [ "$n" -eq 100 ]; do
    local out="$pages/p$page.json" code
    code="$(api_get "$path&page=$page" "$out")"
    [ "$code" = "200" ] || refuse "$label page $page returned HTTP $code"
    python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert isinstance(v, list)' "$out" \
      || refuse "$label page $page did not parse as a JSON array"
    n="$(jq 'length' "$out")"
    page=$((page+1))
    [ "$page" -gt 20 ] && refuse "$label exceeds 2000 entries (paging cap)"
  done
  jq -s 'add' "$pages"/p*.json > "$dest"
}

# Resolve the mirror PR HEADED by a SHA through the commits-pulls endpoint,
# which is indexed by the commit itself: no 100-PR cap, no list-filtering,
# no --jq/--arg quirks. The endpoint lists PRs ASSOCIATED WITH the commit
# (containing it, not headed by it), so the filter must pin .head.sha —
# without it, audit can score Gate A against a fork PR on another head.
# Prefer the mirror-base PR when two fork PRs share the head (measured:
# f5bca33 heads both #13 and #52).
#
# Fail-closed contract: prints the number, or nothing when no fork PR is
# headed by the SHA (exit 0 either way). A fetch failure exits 2 — callers
# must NOT read that as "no mirror", or an outage converts ready PRs to draft.
mirror_for_sha() { # repo sha -> number on stdout (empty when none)
  local repo="$1" sha="$2" code
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || refuse "refusing a non-SHA mirror lookup key: $sha"
  fetch_all_items "/repos/$repo/commits/$sha/pulls?per_page=100" \
    "$WORK/mirror_pulls.json" "mirror lookup for $sha on $repo"
  jq -r --arg sha "$sha" '([.[] | select(.head.sha == $sha and ((.base.ref // "") | startswith("mirror-base")))] | sort_by(.number) | .[0].number)
    // (([.[] | select(.head.sha == $sha and .state == "open")] | sort_by(.number) | .[0].number))
    // empty' "$WORK/mirror_pulls.json" 2>/dev/null \
    || refuse "mirror lookup result for $sha did not parse"
}

# One commit SHA can be the head of two PRs and accumulate >100 check runs
# (measured: f5bca33, ~150 runs across fork #13 and #52). A single page-1
# fetch would silently drop the LATEST runs — usually the red ones. So all
# pages are fetched and merged. `gh api --paginate` must NOT be used here:
# it concatenates per-page OBJECTS into invalid JSON (measured 2026-10-06).
fetch_all_check_runs() { # repo sha dest
  local repo="$1" sha="$2" dest="$3" page=1 n=100
  local pages="$WORK/cr_pages"; mkdir -p "$pages"; rm -f "$pages"/p*
  while [ "$n" -eq 100 ]; do
    local out="$pages/p$page.json" code
    code="$(api_get "/repos/$repo/commits/$sha/check-runs?per_page=100&page=$page" "$out")"
    [ "$code" = "200" ] || refuse "check-runs page $page for $sha on $repo returned HTTP $code"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$out" \
      || refuse "check-runs page $page for $sha did not parse as JSON"
    n="$(jq '.check_runs | length' "$out")"
    page=$((page+1))
    [ "$page" -gt 10 ] && refuse "more than 1000 check runs on $sha (paging cap)"
  done
  jq -s '{total_count: ([.[].check_runs | length] | add),
          check_runs: [.[].check_runs[]]}' "$pages"/p*.json > "$dest"
}

fetch_review_threads_page() { # repo number cursor output -> one GraphQL page
  local repo="$1" num="$2" after="$3" dest="$4"
  local owner="${repo%%/*}" name="${repo##*/}"
  local query='query($owner: String!, $name: String!, $num: Int!, $after: String) { repository(owner: $owner, name: $name) { pullRequest(number: $num) { reviewThreads(first: 100, after: $after) { nodes { isResolved } pageInfo { hasNextPage endCursor } } } } }'
  if [ -n "${GH_TOKEN:-}" ]; then
    local hdrs="$WORK/hdrs"; [ -f "$hdrs" ] || {
      : > "$hdrs"; chmod 600 "$hdrs"
      { printf 'Authorization: Bearer %s\n' "$GH_TOKEN"; printf 'Accept: application/vnd.github+json\n'; printf 'X-GitHub-Api-Version: 2022-11-28\n'; } > "$hdrs"
    }
    local gql code
    gql="$(jq -n --arg o "$owner" --arg n "$name" --argjson num "$num" --arg after "$after" --arg query "$query" '{query:$query,variables:{owner:$o,name:$n,num:$num,after:(if $after == "" then null else $after end)}}')"
    code="$(printf '%s' "$gql" | curl -sS -m 60 -o "$dest" -w '%{http_code}' -H "@$hdrs" -H 'Content-Type: application/json' --data @- "$API/graphql" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] || refuse "GraphQL reviewThreads for PR $num returned HTTP $code"
  else
    command -v "$GH" >/dev/null 2>&1 || refuse "no GH_TOKEN and no gh CLI: review threads unmeasurable"
    local after_arg=null
    [ -n "$after" ] && after_arg="$after"
    "$GH" api graphql -F owner="$owner" -F name="$name" -F num="$num" -F after="$after_arg" -f query="$query" > "$dest" 2>"$WORK/threads.err" || refuse "reviewThreads for PR $num on $repo unreadable: $(head -c 300 "$WORK/threads.err")"
  fi
}

fetch_pr_layout() { # repo number destdir
  local repo="$1" num="$2" dest="$3"
  local code
  mkdir -p "$dest"
  code="$(api_get "/repos/$repo/pulls/$num" "$dest/pr.json")"
  [ "$code" = "200" ] || refuse "GET pulls/$num on $repo returned HTTP $code (cannot measure this PR)"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$dest/pr.json" \
    || refuse "pull $num on $repo did not parse as JSON"
  local sha
  sha="$(jq -r '.head.sha // empty' "$dest/pr.json")"
  [ -n "$sha" ] || refuse "pull $num on $repo has no head.sha"
  jq -cS '{head_sha: .head.sha, base_sha: .base.sha, title: .title, body: .body, draft: .draft, state: .state, updated_at: .updated_at}' \
    "$dest/pr.json" > "$dest/pr_snapshot_before.json" \
    || refuse "cannot record PR $num snapshot on $repo"
  fetch_all_check_runs "$repo" "$sha" "$dest/check_runs.json"
  fetch_all_items "/repos/$repo/pulls/$num/commits?per_page=100" "$dest/commits.json" \
    "commits for PR $num on $repo"
  fetch_all_items "/repos/$repo/issues/$num/comments?per_page=100" "$dest/comments.json" \
    "comments for PR $num on $repo"
  # case_214e61d3: the diff, for the pre-push linter. Measured, never assumed: an
  # unreadable page refuses. (The lint/reputation receipts are comments on the
  # mirror PR, already in comments.json.)
  fetch_all_items "/repos/$repo/pulls/$num/files?per_page=100" "$dest/files.json" \
    "files for PR $num on $repo"
  # Paginate reviewThreads, refusing an incomplete or malformed GraphQL envelope.
  local cursor="" has_next=true page=0 nodes="$WORK/review_threads.jsonl"
  : > "$nodes"
  while [ "$has_next" = true ]; do
    local pagefile="$WORK/threads_page.json"
    fetch_review_threads_page "$repo" "$num" "$cursor" "$pagefile"
    jq -e '.data.repository.pullRequest.reviewThreads as $t |
      (($t.nodes | type) == "array") and
      (($t.pageInfo.hasNextPage | type) == "boolean") and
      (($t.pageInfo.endCursor == null) or (($t.pageInfo.endCursor | type) == "string"))' \
      "$pagefile" >/dev/null 2>&1 || refuse "reviewThreads page $((page + 1)) for PR $num did not parse"
    jq -c '.data.repository.pullRequest.reviewThreads.nodes[]' "$pagefile" >> "$nodes" \
      || refuse "reviewThreads nodes for PR $num did not parse"
    has_next="$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' "$pagefile")"
    cursor="$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' "$pagefile")"
    [ "$has_next" != "true" ] || [ -n "$cursor" ] || refuse "reviewThreads page for PR $num has no next cursor"
    page=$((page+1))
    [ "$page" -lt 100 ] || [ "$has_next" != "true" ] || refuse "reviewThreads for PR $num exceeds 100 pages"
  done
  jq -s '.' "$nodes" > "$dest/threads.json" \
    || refuse "could not assemble reviewThreads for PR $num"
  local snapshot_code
  snapshot_code="$(api_get "/repos/$repo/pulls/$num" "$dest/pr_snapshot_after.json")"
  [ "$snapshot_code" = "200" ] \
    || refuse "cannot recheck PR #$num on $repo after fetch (HTTP $snapshot_code)"
  jq -cS '{head_sha: .head.sha, base_sha: .base.sha, title: .title, body: .body, draft: .draft, state: .state, updated_at: .updated_at}' \
    "$dest/pr_snapshot_after.json" > "$dest/pr_snapshot_current.json" \
    || refuse "cannot parse PR #$num snapshot recheck on $repo"
  cmp -s "$dest/pr_snapshot_before.json" "$dest/pr_snapshot_current.json" \
    || refuse "PR #$num on $repo changed during fetch; refusing a mixed snapshot"
  printf '%s' "$sha"
}

# --- evaluators (identical in live and fixture mode) --------------------------
# Each reads the standard layout dir and appends TSV records:
#   <item>\t<pass|fail>\t<detail>
# Missing/unparseable input is a REFUSAL (exit 2), never a fail, never a pass.

# --- reviewed CI profiles (fail-closed registry) ------------------------------
# ONE reviewed source of truth, co-located with this tool so suite copies run
# standalone: upstream_ci_profiles.json next to this file (resolved via
# SELFDIR, never CWD). Schema v1: {schema_version: 1, profiles:
# {owner/repo: {fork_required, upstream_required, status, evidence}}}.
# There is NO cross-repo fallback in fixture mode: a repo with no reviewed
# entry, or a malformed registry, is REFUSED (exit 2). In live mode a repo
# with no entry keeps today's hardcoded defaults (unenrolled-repo degradation:
# the live FAKE/ACME stub worlds and unrelated filings are unaffected); a
# present-but-unreviewed or malformed entry still refuses. A missing registry
# file (old tool copies) also keeps today's behavior. See
# docs/upstream/compatibility-contract.md for the full contract.
CI_PROFILES_FILE="$SELFDIR/upstream_ci_profiles.json"
# Repos whose REQUIRED lists keep review names verbatim (preserved defaults).
CI_REVIEW_KEEPERS="TogetherWeOwn/paperclip|paperclipai/paperclip"

# Resolve a profile entry: stdout is one JSON object
# {required, manifest, policy_ref} on exit 0, a REFUSED TSV line on exit 2, or
# the word LEGACY on exit 3 (live mode, unenrolled repo: use today's defaults).
ci_profile_resolve() { # repo side mode(fixtures|live)
  python3 - "$CI_PROFILES_FILE" "$1" "$2" "$3" <<'EOF'
import json, sys
reg_path, repo, side, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
keepers = {"TogetherWeOwn/paperclip", "paperclipai/paperclip"}
try:
    with open(reg_path, encoding="utf-8") as fh:
        reg = json.load(fh)
except (OSError, ValueError) as exc:
    print("REFUSED malformed CI profile: registry unreadable (%s)" % exc); sys.exit(2)
if not isinstance(reg, dict) or reg.get("schema_version") != 1 \
        or not isinstance(reg.get("profiles"), dict):
    print("REFUSED malformed CI profile: bad schema_version or profiles"); sys.exit(2)
entry = reg["profiles"].get(repo)
if entry is None:
    # Unenrolled repo: fail-closed in fixtures, today's defaults live.
    if mode == "fixtures":
        print("REFUSED unknown/unreviewed CI profile for %s" % repo); sys.exit(2)
    print("LEGACY"); sys.exit(3)
if not isinstance(entry, dict) or entry.get("status") != "reviewed":
    print("REFUSED unknown/unreviewed CI profile for %s" % repo); sys.exit(2)
key = "fork_required" if side == "fork" else "upstream_required"
required = entry.get(key)
if not isinstance(required, list) or not required \
        or any(not isinstance(n, str) or not n for n in required):
    print("REFUSED malformed CI profile: bad %s for %s" % (key, repo)); sys.exit(2)
for lst in ("fork_required", "upstream_required"):
    v = entry.get(lst)
    if not isinstance(v, list) or not v \
            or any(not isinstance(n, str) or not n for n in v):
        print("REFUSED malformed CI profile: bad %s for %s" % (lst, repo)); sys.exit(2)
fork_names, up_names = entry["fork_required"], entry["upstream_required"]
if repo not in keepers:
    # A review must never be double-counted as CI: Paperclip Review is the
    # fork-side independent gate (no profile may require it), and a check run
    # named Greptile Review is only a legitimate upstream-side CI signal.
    # (Greptile Review in upstream_required is the reviewed shape for repos
    # where Greptile posts check runs.)
    if "Paperclip Review" in fork_names or "Paperclip Review" in up_names \
            or "Greptile Review" in fork_names:
        print("REFUSED malformed CI profile: review check in *_required for %s" % repo); sys.exit(2)
ev = entry.get("evidence")
if not isinstance(ev, dict) or not isinstance(ev.get("policy_ref"), str) or not ev.get("policy_ref") \
        or not isinstance(ev.get("workflows_manifest"), list) \
        or not isinstance(ev.get("check_run_names"), list) \
        or not isinstance(ev.get("checked_at"), str):
    print("REFUSED malformed CI profile: bad evidence for %s" % repo); sys.exit(2)
for m in ev["workflows_manifest"]:
    if not isinstance(m, dict) or not isinstance(m.get("file"), str) or not m.get("file") \
            or not isinstance(m.get("blob_sha"), str) or not m.get("blob_sha"):
        print("REFUSED malformed CI profile: bad workflows_manifest for %s" % repo); sys.exit(2)
print(json.dumps({"required": required, "manifest": ev["workflows_manifest"],
                  "policy_ref": ev["policy_ref"]}))
EOF
}

# Strict A.4 predicate over profiled required names. Argv: layoutdir label
# required_json manifest_json provided_json("null" skips drift, "MALFORMED"
# refuses). Prints one PASS/FAIL/REFUSED TSV line; exit 0 verdict, 2 refused.
ci_profile_predicate() {
  python3 - "$1" "$2" "$3" "$4" "$5" <<'EOF'
import json, sys
d, label, required, manifest, provided = (
    sys.argv[1], sys.argv[2], json.loads(sys.argv[3]),
    json.loads(sys.argv[4]), sys.argv[5])
try:
    runs = json.load(open(d + "/check_runs.json"))
    cr = runs.get("check_runs", runs if isinstance(runs, list) else [])
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED cannot parse check_runs.json/pr.json: %s" % e); sys.exit(2)
sha = (pr.get("head") or {}).get("sha", "") if isinstance(pr, dict) else ""
if not isinstance(sha, str) or not sha:
    print("REFUSED %s: pull request has no head.sha" % label); sys.exit(2)
if manifest:
    if provided == "null":
        pass  # legacy layout without a pinned manifest: drift unchecked
    elif provided == "MALFORMED":
        print("REFUSED profile-stale: pinned workflows manifest did not parse"); sys.exit(2)
    else:
        try:
            pinned = {e["file"]: e["blob_sha"] for e in json.loads(provided)}
        except (ValueError, AttributeError, TypeError, KeyError):
            print("REFUSED profile-stale: pinned workflows manifest did not parse"); sys.exit(2)
        for m in manifest:
            if pinned.get(m["file"]) != m["blob_sha"]:
                print("REFUSED profile-stale: %s changed; refresh profile + fixtures" % m["file"]); sys.exit(2)
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED check run has no numeric id for %s" % name); sys.exit(2)
    key = (run_id, r.get("started_at") or r.get("completed_at") or "")
    if name not in latest or key > latest[name][0]:
        latest[name] = (key, r)
if not latest:
    print("REFUSED zero check runs observed for %s (not started, or denied)" % sha[:12]); sys.exit(2)
# The check-runs API always returns head_sha; a payload where NO run carries
# one predates head-pinning (legacy fixtures/stubs) and skips head comparison.
# A required run without head_sha where others carry it is REFUSED, never passed.
pinned_payload = any(isinstance(r.get("head_sha"), str) and r.get("head_sha")
                     for _, r in latest.values())
missing, pend, stale, failing = [], [], [], []
for name in required:
    if name not in latest:
        missing.append(name); continue
    r = latest[name][1]
    hs = r.get("head_sha")
    if not hs:
        if pinned_payload:
            print("REFUSED %s: required run %s has no head_sha (expected %s)" % (
                label, name, sha[:12])); sys.exit(2)
        # legacy payload: head assumed, status/conclusion still enforced
    elif hs != sha:
        stale.append("%s(head %s)" % (name, str(hs)[:12])); continue
    if r.get("status") != "completed":
        pend.append("%s:%s" % (name, r.get("status"))); continue
    if r.get("conclusion") != "success":
        failing.append("%s(%s id %s)" % (name, r.get("conclusion"), r.get("id")))
if failing:
    print("FAIL\t%s failing on %s: %s" % (label, sha[:12], ", ".join(failing))); sys.exit(0)
if stale:
    print("FAIL\t%s stale runs on %s: %s is stale [pending]" % (
        label, sha[:12], ", ".join(stale))); sys.exit(0)
if pend:
    print("FAIL\t%s not concluded on %s: %s [pending]" % (
        label, sha[:12], ", ".join(pend))); sys.exit(0)
if missing:
    print("FAIL\t%s missing required checks on %s: %s [pending]" % (
        label, sha[:12], ", ".join(missing))); sys.exit(0)
print("PASS\t%s green on %s: %d required checks, latest per name, incl. %s" % (
    label, sha[:12], len(required), ", ".join(required)))
EOF
}

# Dispatch: profiled evaluation for reviewed entries, legacy defaults for
# unenrolled repos in live mode (or a missing registry file), refusal for
# unenrolled/malformed entries in fixture mode. Argv: layoutdir repo side label fixtures
profile_eval() {
  local dir="$1" repo="$2" side="$3" label="$4" fixt="$5"
  if [ ! -f "$CI_PROFILES_FILE" ]; then
    if [ "$side" = "fork" ]; then eval_fork_ci_legacy "$dir"; else eval_upstream_ci_legacy "$dir"; fi
    return $?
  fi
  local mode="live"; [ -n "$fixt" ] && mode="fixtures"
  local res code required manifest policy provided
  res="$(ci_profile_resolve "$repo" "$side" "$mode")"; code=$?
  if [ "$code" -eq 3 ]; then
    if [ "$side" = "fork" ]; then eval_fork_ci_legacy "$dir"; else eval_upstream_ci_legacy "$dir"; fi
    return $?
  fi
  [ "$code" -eq 2 ] && { echo "$res"; return 2; }
  [ "$code" -eq 0 ] || { echo "REFUSED cannot resolve CI profile for $repo"; return 2; }
  required="$(jq -c '.required' <<<"$res")"
  manifest="$(jq -c '.manifest' <<<"$res")"
  policy="$(jq -r '.policy_ref' <<<"$res")"
  if [ -n "$fixt" ]; then
    if [ -f "$dir/workflows.json" ]; then
      provided="$(jq -c '.' "$dir/workflows.json" 2>/dev/null)" || provided="MALFORMED"
    else provided="null"; fi
  elif [ "$manifest" = "[]" ]; then provided="null"
  else
    # Live drift re-resolution: the pinned blob SHAs must still match the
    # policy branch head. Any mismatch or unreadable file refuses.
    local branch="${policy%%@*}" n i file want out got c
    [ -n "$branch" ] || { echo "REFUSED profile-stale: entry for $repo has no policy branch"; return 2; }
    n="$(jq -r 'length' <<<"$manifest")"; provided="$manifest"
    for i in $(seq 0 $((n-1))); do
      file="$(jq -r ".[$i].file" <<<"$manifest")"
      want="$(jq -r ".[$i].blob_sha" <<<"$manifest")"
      out="$WORK/drift_$side_$i.json"
      c="$(api_get "/repos/$repo/contents/.github/workflows/$file?ref=$branch" "$out")"
      [ "$c" = "200" ] || { echo "REFUSED profile-stale: cannot re-resolve $file on $repo@$branch (HTTP $c); refresh profile + fixtures"; return 2; }
      got="$(jq -r '.sha // empty' "$out")"
      [ "$got" = "$want" ] || { echo "REFUSED profile-stale: $file changed on $repo@$branch; refresh profile + fixtures"; return 2; }
    done
  fi
  ci_profile_predicate "$dir" "$label" "$required" "$manifest" "$provided"
}

eval_fork_ci() { # layoutdir [repo] [fixtures]
  profile_eval "$1" "${2:-$FORK_REPO_DEFAULT}" fork "fork-ci" "${3:-}"
}

eval_fork_ci_legacy() { # layoutdir (today's defaults, byte-identical)
  python3 - "$1" <<'EOF'
import json, sys
d = sys.argv[1]
try:
    runs = json.load(open(d + "/check_runs.json"))
    cr = runs.get("check_runs", runs if isinstance(runs, list) else [])
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse check_runs.json/pr.json: %s" % e); sys.exit(2)
sha = pr.get("head", {}).get("sha", "")
# Latest run per check name wins (re-runs, queued runs and shared-SHA PRs).
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (run_id, r.get("started_at") or r.get("completed_at") or "")
    if name not in latest or key > latest[name][0]:
        latest[name] = (key, r)
if not latest:
    print("REFUSED\tzero check runs observed for %s (not started, or denied)" % sha[:12]); sys.exit(2)
BAD = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
fail, pending = [], []
for name, (_, r) in sorted(latest.items()):
    if r.get("status") != "completed":
        pending.append("%s:%s" % (name, r.get("status"))); continue
    if (r.get("conclusion") or "") in BAD:
        fail.append(name)
REQUIRED = ["ci / verify", "ci / e2e", "Paperclip Review"]
missing = [n for n in REQUIRED if n not in latest]
if fail:
    print("FAIL\tfork-ci failing on %s: %s" % (sha[:12], ", ".join(fail))); sys.exit(0)
if pending:
    print("FAIL\tfork-ci not concluded on %s: %s [pending]" % (sha[:12], ", ".join(pending))); sys.exit(0)
if missing:
    print("FAIL\tfork-ci missing required checks on %s: %s [pending]" % (sha[:12], ", ".join(missing))); sys.exit(0)
print("PASS\tfork-ci green on %s: %d checks, latest per name, incl. %s" % (
    sha[:12], len(latest), ", ".join(REQUIRED)))
EOF
}

eval_our_review() { # layoutdir
  python3 - "$1" <<'EOF'
import json, sys
d = sys.argv[1]
try:
    runs = json.load(open(d + "/check_runs.json"))
    cr = runs.get("check_runs", runs if isinstance(runs, list) else [])
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse check_runs.json/pr.json: %s" % e); sys.exit(2)
sha = pr.get("head", {}).get("sha", "")
cands = [r for r in cr if r.get("name") == "Paperclip Review"]
if not cands:
    print("FAIL\tour-review: no Paperclip Review check run on %s [pending]" % sha[:12]); sys.exit(0)
def run_key(r):
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tPaperclip Review check run has no numeric id"); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    return (run_id, r.get("started_at") or r.get("completed_at") or "")
cands.sort(key=run_key)
r = cands[-1]
if r.get("status") != "completed":
    print("FAIL\tour-review: latest Paperclip Review is %s on %s [pending]" % (
        r.get("status"), sha[:12])); sys.exit(0)
if r.get("conclusion") != "success":
    print("FAIL\tour-review: latest Paperclip Review is %s/%s on %s" % (
        r.get("status"), r.get("conclusion"), sha[:12])); sys.exit(0)
out = json.dumps(r.get("output") or {})
if "5/5" not in out:
    print("FAIL\tour-review: success without a 5/5 score on %s" % sha[:12]); sys.exit(0)
print("PASS\tour-review 5/5 on %s" % sha[:12])
EOF
}

eval_single_commit() { # layoutdir
  python3 - "$1" <<'EOF'
import json, sys
d = sys.argv[1]
try:
    commits = json.load(open(d + "/commits.json"))
    if isinstance(commits, dict): commits = [commits]
except Exception as e:
    print("REFUSED\tcannot parse commits.json: %s" % e); sys.exit(2)
if len(commits) == 1:
    print("PASS\tsingle-commit: exactly 1 commit (%s)" % (commits[0].get("sha", "?")[:12])); sys.exit(0)
print("FAIL\tsingle-commit: %d commits, need exactly 1 (squash the branch)" % len(commits))
EOF
}

eval_no_internal_ids() { # layoutdir
  python3 - "$1" <<'EOF'
import json, re, sys
d = sys.argv[1]
try:
    pr = json.load(open(d + "/pr.json"))
    commits = json.load(open(d + "/commits.json"))
    if isinstance(commits, dict): commits = [commits]
except Exception as e:
    print("REFUSED\tcannot parse pr.json/commits.json: %s" % e); sys.exit(2)
blobs = {"title": pr.get("title", ""), "body": pr.get("body", "") or "",
         "branch": (pr.get("head") or {}).get("ref", "")}
for c in commits:
    blobs["commit " + (c.get("sha", "?")[:12])] = c.get("commit", {}).get("message", "")
pats = [r"\bTOG-\d+", r"\bPAP-?\d+", r"\bPAPA-\d+", r"deploymentexample\.net",
        r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b", r"localhost", r"agent://", r"/PAP/issues/"]
hits = []
for where, text in blobs.items():
    for p in pats:
        m = re.search(p, text or "")
        if m: hits.append("%s holds %s" % (where, m.group(0)))
if hits:
    print("FAIL\tno-internal-ids: %s" % "; ".join(hits)); sys.exit(0)
print("PASS\tno-internal-ids: title, body, branch and %d commit message(s) clean" % len(commits))
EOF
}

# Mirror-side fields the upstream body is generated from. Every section must
# be present and non-placeholder; the Refs line is STRIPPED by the generator
# (private repo idiom) so it is required here but never published.
eval_checklist_inputs() { # layoutdir
  python3 - "$1" <<'EOF'
import json, re, sys
d = sys.argv[1]
try:
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse pr.json: %s" % e); sys.exit(2)
body = pr.get("body") or ""
def section(name):
    m = re.search(r"^##\s+%s\s*$" % re.escape(name), body, re.M | re.I)
    if not m: return ""
    rest = body[m.end():]
    n = re.search(r"^##\s+\S", rest, re.M)
    return (rest[:n.start()] if n else rest).strip()
need = ["Thinking Path", "What Changed", "Verification", "Risks", "Model Used"]
PLACE = re.compile(r"^\s*[-*]\s*$|Which subsystem|What problem|example|TODO|TBD|\[Which|\[What|\[Why", re.I | re.M)
missing = [s for s in need if not section(s) or PLACE.search(section(s))]
linked = section("Linked Issues or Issue Description")
if not linked or len(linked) < 5:
    missing.append("Linked Issues or Issue Description")
if missing:
    print("FAIL\tchecklist: mirror PR body missing/placeholder: %s" % ", ".join(missing)); sys.exit(0)
print("PASS\tchecklist: all source sections present; upstream body generatable with truthful boxes")
EOF
}

# The UPSTREAM PR body against the UPSTREAM template. Used by `ready` (new
# publishes, strict: no unchecked box may remain when maintainers are asked)
# and by `audit` (retrofit: sections must be filled; unchecked boxes fail
# only on a non-draft, where they contradict the ready state).
eval_upstream_checklist() { # layoutdir [strict]
  local strict="${2:-}"
  python3 - "$1" "$strict" <<'EOF'
import json, re, sys
d, strict = sys.argv[1], sys.argv[2]
try:
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse pr.json: %s" % e); sys.exit(2)
body = pr.get("body") or ""
def section(name):
    m = re.search(r"^##\s+%s\s*$" % re.escape(name), body, re.M | re.I)
    if not m: return ""
    rest = body[m.end():]
    n = re.search(r"^##\s+\S", rest, re.M)
    return (rest[:n.start()] if n else rest).strip()
need = ["Thinking Path", "Linked Issues or Issue Description", "What Changed",
        "Verification", "Risks", "Model Used", "Checklist"]
PLACE = re.compile(r"^\s*[-*]\s*$|Which subsystem|What problem|TODO|TBD|\[Which|\[What|\[Why", re.I | re.M)
missing = [s for s in need if not section(s) or PLACE.search(section(s))]
if missing:
    print("FAIL\tup-checklist: upstream body missing/placeholder: %s" % ", ".join(missing)); sys.exit(0)
draft = pr.get("draft", True)
unchecked = len(re.findall(r"(?m)^\s*-\s*\[\s\]", body))
if strict == "strict" or (not draft and unchecked):
    if unchecked:
        print("FAIL\tup-checklist: %d unchecked box(es) with maintainers asked (draft=%s)" % (
            unchecked, draft)); sys.exit(0)
print("PASS\tup-checklist: all 7 sections filled%s" % (
    "; all boxes ticked" if not unchecked else "; %d box(es) pending (still draft)" % unchecked))
EOF
}

eval_upstream_ci() { # layoutdir [repo] [fixtures] (upstream side)
  profile_eval "$1" "${2:-}" upstream "upstream-ci" "${3:-}"
}

eval_upstream_ci_legacy() { # layoutdir (today's defaults, byte-identical)
  python3 - "$1" <<'EOF'
import json, sys
d = sys.argv[1]
try:
    runs = json.load(open(d + "/check_runs.json"))
    cr = runs.get("check_runs", runs if isinstance(runs, list) else [])
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse check_runs.json/pr.json: %s" % e); sys.exit(2)
sha = pr.get("head", {}).get("sha", "")
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (run_id, r.get("started_at") or r.get("completed_at") or "")
    if name not in latest or key > latest[name][0]:
        latest[name] = (key, r)
if not latest:
    print("REFUSED\tzero check runs observed for %s" % sha[:12]); sys.exit(2)
BAD = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
fail = [n for n, (_, r) in latest.items()
        if r.get("status") == "completed" and (r.get("conclusion") or "") in BAD]
pend = [n for n, (_, r) in latest.items() if r.get("status") != "completed"]
REQUIRED = ["ci / verify", "ci / e2e", "Greptile Review"]
missing = [n for n in REQUIRED if n not in latest]
if fail:
    print("FAIL\tupstream-ci failing on %s: %s" % (sha[:12], ", ".join(sorted(fail)))); sys.exit(0)
if pend:
    print("FAIL\tupstream-ci not concluded on %s: %s [pending]" % (sha[:12], ", ".join(sorted(pend)))); sys.exit(0)
if missing:
    print("FAIL\tupstream-ci missing required checks on %s: %s [pending]" % (sha[:12], ", ".join(missing))); sys.exit(0)
print("PASS\tupstream-ci green on %s: %d checks incl. Greptile Review" % (sha[:12], len(latest)))
EOF
}

eval_greptile() { # layoutdir (upstream side)
  python3 - "$1" <<'EOF'
import json, re, sys
d = sys.argv[1]
try:
    pr = json.load(open(d + "/pr.json"))
    comments = json.load(open(d + "/comments.json"))
    if isinstance(comments, dict): comments = [comments]
    threads = json.load(open(d + "/threads.json"))
    if isinstance(threads, dict): threads = threads.get("nodes")
except Exception as e:
    print("REFUSED\tcannot parse pr/comments/threads JSON: %s" % e); sys.exit(2)
if not isinstance(pr, dict) or not isinstance(comments, list) or not isinstance(threads, list):
    print("REFUSED\tpr/comments/threads JSON has an invalid shape"); sys.exit(2)
sha = pr.get("head", {}).get("sha", "")
summaries = [c for c in comments
             if (c.get("user") or {}).get("login") in ("greptile-apps[bot]", "greptile[bot]")
             and "greptile_confidence_score" in (c.get("body") or "")]
if not summaries:
    print("FAIL\tgreptile: no Greptile summary comment on this PR yet [pending]"); sys.exit(0)
summaries.sort(key=lambda c: c.get("created_at") or c.get("updated_at") or "")
body = summaries[-1].get("body") or ""
m = re.search(r"greptile_confidence_score\s*:\s*(\d)", body)
score = int(m.group(1)) if m else -1
lm = re.search(r"commit/([0-9a-f]{7,40})", body)
reviewed = lm.group(1) if lm else ""
# The reviewed-commit link is REQUIRED, not optional: a score without it is
# a score for an unknown head, and scoring it would pass a 5/5 that may
# belong to a superseded commit.
if not reviewed:
    print("FAIL\tgreptile: score %d/5 names no reviewed commit (need Last reviewed commit == %s)" % (
        score, sha[:12])); sys.exit(0)
if not sha.startswith(reviewed) and not reviewed.startswith(sha[:len(reviewed)]):
    print("FAIL\tgreptile: latest score %d/5 reviewed %s, head is %s (stale) [pending]" % (
        score, reviewed[:12], sha[:12])); sys.exit(0)
if score != 5:
    print("FAIL\tgreptile: confidence %d/5 on %s, need 5/5" % (score, sha[:12])); sys.exit(0)
open_threads = [t for t in threads if not t.get("isResolved")]
if open_threads:
    print("FAIL\tgreptile: 5/5 but %d unresolved review thread(s)" % len(open_threads)); sys.exit(0)
print("PASS\tgreptile 5/5 on %s, no unresolved threads" % sha[:12])
EOF
}

eval_mergeable() { # layoutdir (upstream side)
  python3 - "$1" <<'EOF'
import json, os, sys
d = sys.argv[1]
try:
    pr = json.load(open(d + "/pr.json"))
except Exception as e:
    print("REFUSED\tcannot parse pr.json: %s" % e); sys.exit(2)
if not isinstance(pr, dict):
    print("REFUSED\tpr.json has an invalid shape"); sys.exit(2)
sha = pr.get("head", {}).get("sha", "")
# GitHub returns mergeable=null on the first GET after any head or base push
# while it (re)computes the merge. fetch_pr_layout re-GETs the PR after the
# check-run/commit/comment/file/thread fetch (pr_snapshot_after.json), so by
# scoring time the computed value is usually present. Prefer that post-fetch
# re-read when it exists (live path); fixtures carry only pr.json.
mergeable = pr.get("mergeable")
state = pr.get("mergeable_state")
after = d + "/pr_snapshot_after.json"
if os.path.exists(after):
    try:
        late = json.load(open(after))
        if isinstance(late, dict):
            # The re-read is newer; adopt its mergeability fields when present.
            if "mergeable" in late:
                mergeable = late.get("mergeable")
            if "mergeable_state" in late:
                state = late.get("mergeable_state")
    except Exception:
        pass
# A still-null mergeable is unmeasurable readiness state, not "mergeable".
# Refuse, never pass. A refusal blocks PASS but (in cmd_audit) never blocks
# conversion when another item has a measured FAIL.
if mergeable is None:
    print("REFUSED\tmergeable is null for %s (mergeable_state=%s; GitHub has not computed it)" % (sha[:12], state)); sys.exit(2)
if mergeable is False or state == "dirty":
    print("FAIL\tmergeable: %s is not mergeable (mergeable=%s mergeable_state=%s)" % (sha[:12], mergeable, state)); sys.exit(0)
print("PASS\tmergeable: %s is mergeable (mergeable_state=%s)" % (sha[:12], state))
EOF
}

# GATE_ALLOW_PENDING=1 (case_214e61d3, parallel gates): an item that FAILs only
# because it has not finished yet (tagged "[pending]" by its evaluator) prints
# as PEND and does not fail the run. A completed red is still FAIL. Used by
# `publish`, which opens the upstream DRAFT while fork CI and our review run
# beside Greptile. `ready` never sets it: nothing is pending when maintainers
# are asked.
run_gate_a() { # layoutdir [repo] [fixtures] -> prints item lines; exit 0 pass / 1 fail / 2 refused
  local dir="$1" repo="${2:-$FORK_REPO_DEFAULT}" fixt="${3:-}" line item state fail=0 code
  GATE_PENDING_COUNT=0
  for ev in "eval_fork_ci fork-ci" "eval_our_review our-review" \
            "eval_checklist_inputs checklist" "eval_no_internal_ids no-internal-ids" \
            "eval_single_commit single-commit"; do
    set -- $ev
    if [ "$1" = "eval_fork_ci" ]; then line="$("$1" "$dir" "$repo" "$fixt")"; code=$?
    else line="$($1 "$dir")"; code=$?; fi
    [ "$code" -eq 2 ] && { emit_refused "$line"; return 2; }
    [ "$code" -eq 0 ] || { echo "REFUSED $1 evaluator exited $code"; return 2; }
    case "$line" in
      FAIL*"[pending]"*)
        if [ "${GATE_ALLOW_PENDING:-0}" = 1 ]; then
          line="PEND${line#FAIL}"; GATE_PENDING_COUNT=$((GATE_PENDING_COUNT+1))
        else fail=1; fi;;
      FAIL*) fail=1;;
    esac
    echo "$line"
  done
  return $fail
}

# --- case_214e61d3 evaluators: pre-push linter, reputation receipt -----------------
# Bridges to the python engines. Globals set by the caller: LINT_REPO (the
# upstream owner/repo whose maintainer memory applies), LINT_STAGE
# (first-push|ready), LINT_LIVE=1 to also confirm the live PR template.
eval_lint() { # layoutdir
  local out code
  [ -d "${WORK:-}" ] || { printf 'REFUSED\tinternal: no scratch dir for the linter\n'; return 2; }
  [ -n "${LINT_REPO:-}" ] || { printf 'REFUSED\tlint has no upstream repo to read memory for\n'; return 2; }
  out="$(python3 "$SELFDIR/upstream_lint.py" lint --repo "$LINT_REPO" --layout "$1" \
    --stage "${LINT_STAGE:-first-push}" ${LINT_LIVE:+--live} --gate-line 2>"$WORK/lint.err")"; code=$?
  case "$code:$out" in
    0:PASS*|1:FAIL*) printf '%s\n' "$out"; return 0;;
    *)   printf 'REFUSED\t%s\n' "${out#REFUSED	}${out:+ }$(head -c 200 "$WORK/lint.err")"; return 2;;
  esac
}

eval_reputation() { # upstream-layoutdir mirror-comments-json (receipts are mirror PR comments)
  local out code
  [ -d "${WORK:-}" ] || { printf 'REFUSED\tinternal: no scratch dir for the reputation check\n'; return 2; }
  out="$(python3 "$SELFDIR/upstream_reputation.py" verify --upstream-layout "$1" --comments "$2" --gate-line 2>"$WORK/rep.err")"; code=$?
  case "$code:$out" in
    0:PASS*|1:FAIL*) printf '%s\n' "$out"; return 0;;
    *)   printf 'REFUSED\t%s\n' "${out#REFUSED	}${out:+ }$(head -c 200 "$WORK/rep.err")"; return 2;;
  esac
}

eval_v2_receipt() { # mirror-comments-json upstream-head -> 1|0; head is intentionally not bound
  python3 - "$1" "$2" "$SELFDIR" <<'EOF'
import json, re, sys
sys.path.insert(0, sys.argv[3])
import upstream_lint as ul
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        comments = json.load(fh)
    if not isinstance(comments, list):
        raise ValueError("mirror comments are not a JSON array")
    receipts = ul.receipts_from_comments(comments, ul.whoami())
except (OSError, ValueError, ul.Refused) as exc:
    print("REFUSED: %s" % exc)
    sys.exit(2)
print("1" if any(r.get("context") == "upstream-gate/lint" and r.get("state") == "success"
               and re.search(r"(?:^|\s)pipeline=v2(?:\s|$)", r.get("description") or "")
               for r in receipts) else "0")
EOF
}

# --- upstream body generator --------------------------------------------------
# Code-filled, never hand-ticked. The Greptile box stays [ ] at publish:
# Greptile has not scored this head yet, and ticking it would be the exact
# lie this tool exists to prevent. `ready` requires it instead.
gen_upstream_body() { # layoutdir title -> stdout
  local dir="$1" title="$2"
  python3 - "$dir" "$title" <<'EOF'
import json, re, sys
d, title = sys.argv[1], sys.argv[2]
pr = json.load(open(d + "/pr.json"))
sha = pr.get("head", {}).get("sha", "")
body = pr.get("body") or ""
mnum = re.search(r"/pull/(\d+)", json.dumps(pr.get("_mirror_url_hint", "")))
mirror_ref = pr.get("_mirror_ref", "")
def section(name):
    m = re.search(r"^##\s+%s\s*$" % re.escape(name), body, re.M | re.I)
    if not m: return ""
    rest = body[m.end():]
    n = re.search(r"^##\s+\S", rest, re.M)
    return (rest[:n.start()] if n else rest).strip()
def clean(t):
    # Strip private-repo idioms that must never go public.
    t = re.sub(r"(?m)^Refs:\s*TOG-\S*\s*$", "", t)
    t = re.sub(r"\bTOG-\d+\b", "(internal ref removed)", t)
    t = re.sub(r"\bPAP-?\d+\b", "(internal ref removed)", t)
    t = re.sub(r"workforce\.deploymentexample\.net\S*", "(internal link removed)", t)
    return t.strip()
think = clean(section("Thinking Path"))
linked = clean(section("Linked Issues or Issue Description"))
what = clean(section("What Changed"))
verif = clean(section("Verification"))
risks = clean(section("Risks"))
model = clean(section("Model Used"))
print("<!-- Write all pull request text in Simplified Technical English (ASD-STE100). -->")
print("")
print("## Thinking Path")
print("")
print(think)
print("")
print("## Linked Issues or Issue Description")
print("")
print(linked)
print("")
print("## What Changed")
print("")
print(what)
print("")
print("## Verification")
print("")
print(verif)
print("")
print("Checks that must pass on commit %s before this PR is marked ready for review:" % sha[:12])
print("- upstream CI workflows green in the fork (latest result per check)")
print("- Paperclip Review 5/5 with no open findings")
print("- Greptile 5/5 with no open comments")
print("- single clean commit; no internal references in title, body, branch or commits")
print("")
print("## Risks")
print("")
print(risks)
print("")
print("## Model Used")
print("")
print(model)
print("")
print("## Checklist")
for box in [
  "I have included a thinking path that traces from project context to this change",
  "I have specified the model used (with version and capability details)",
  "I have checked ROADMAP.md and confirmed this PR does not duplicate planned core work",
  "I have searched GitHub for duplicate or related PRs and linked them above",
  "I have either (a) linked existing issues with `Fixes: #` / `Closes #` / `Refs #` OR (b) described the issue in-PR following the relevant issue template",
  "I have not referenced internal/instance-local Paperclip issues or links (only public GitHub `#NNN` / `github.com/paperclipai/paperclip` URLs)",
  "My branch name describes the change (e.g. `docs/...`, `fix/...`) and contains no internal Paperclip ticket id or instance-derived details",
  "I have run tests locally and they pass",
  "I have added or updated tests where applicable",
  "I have updated relevant documentation to reflect my changes",
  "I have considered and documented any risks above",
]:
    print("- [x] " + box)
# case_214e61d3: with the gates running in parallel, CI may still be running at
# publish, so the CI box stays open too. `ready` ticks all three after the
# code has measured them; nobody hand-ticks a verified fact.
print("- [ ] All Paperclip CI gates are green (pending; `upstream-gate ready` ticks it after measuring)")
print("- [ ] Greptile is 5/5 with no open P2s, recommendations, or follow-ups (pending upstream review; `upstream-gate ready` requires it)")
print("- [ ] I will address all Greptile and reviewer comments before requesting merge (standing commitment)")
print("")
print("## Readiness (upstream-gate)")
print("")
print("- mirror: %s @ `%s`" % (mirror_ref, sha))
print("- pipeline: upstream-gate v2")
print("- at publish: pre-push lint PASS; fork CI, Paperclip Review and Greptile run on this head in parallel")
print("- marked ready for review only when all of them pass on this exact head")
EOF
}

assert_branch_head() { # repo branch expected_sha; stop before publish if mirror ref moved
  local repo="$1" branch="$2" expected="$3" code actual
  local ref="$WORK/branch_ref.json"
  code="$(api_get "/repos/$repo/git/ref/heads/$branch" "$ref")"
  [ "$code" = "200" ] || refuse "cannot recheck branch $branch on $repo before publish (HTTP $code)"
  actual="$(jq -r '.object.sha // empty' "$ref")"
  [ "$actual" = "$expected" ] || refuse "branch $branch moved during publish (validated $expected, current ${actual:-missing})"
}

assert_pr_head() { # repo number expected_sha; stop before ready if PR head moved
  local repo="$1" num="$2" expected="$3" code actual
  local pr="$WORK/head_recheck.json"
  code="$(api_get "/repos/$repo/pulls/$num" "$pr")"
  [ "$code" = "200" ] || refuse "cannot recheck PR #$num on $repo before ready (HTTP $code)"
  actual="$(jq -r '.head.sha // empty' "$pr")"
  [ "$actual" = "$expected" ] || refuse "PR #$num head moved during ready (validated $expected, current ${actual:-missing})"
}

# --- subcommands --------------------------------------------------------------

cmd_check() { # --mirror-pr N [--fork-repo R] [--fixtures D]
  local num="" repo="$FORK_REPO_DEFAULT" fixtures=""
  while [ $# -gt 0 ]; do case "$1" in
    --mirror-pr) num="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    *) die "check: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "check needs --mirror-pr N"
  local dir
  if [ -n "$fixtures" ]; then dir="$fixtures";
    [ -f "$dir/pr.json" ] || refuse "fixtures dir $fixtures has no pr.json"
  else
    # Read-only: the curl transport covers GH_TOKEN runs end to end, so gh
    # is required only when auth lives outside GH_TOKEN (measured: the
    # gh-less Offline container refused here, 2026-10-06).
    if [ -z "${GH_TOKEN:-}" ]; then need_gh; fi; new_work; dir="$WORK/mirror"
    fetch_pr_layout "$repo" "$num" "$dir" >/dev/null
  fi
  local sha; sha="$(jq -r '.head.sha' "$dir/pr.json")"
  printf 'Gate A  %s#%s  @ %s\n' "$repo" "$num" "$sha"
  run_gate_a "$dir" "$repo" "$fixtures"; local code=$?
  if [ "$code" -eq 0 ]; then printf 'GATE A: PASS\n'; return $EXIT_PASS; fi
  if [ "$code" -eq 2 ]; then printf 'GATE A: REFUSED\n'; return $EXIT_REFUSED; fi
  printf 'GATE A: FAIL\n'; return $EXIT_FAIL
}

cmd_publish() { # --mirror-pr N --title T [--upstream-repo R] [--fork-repo R] [--dry-run] [--serial] [--maintainer-split-url U]
  local num="" title="" upstream="" repo="$FORK_REPO_DEFAULT" dry=0 serial=0 split=""
  while [ $# -gt 0 ]; do case "$1" in
    --mirror-pr) num="${2:-}"; shift 2;;
    --title) title="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --dry-run) dry=1; shift;;
    --serial) serial=1; shift;;
    --maintainer-split-url) split="${2:-}"; shift 2;;
    *) die "publish: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "publish needs --mirror-pr N"
  [ -n "$title" ] || die "publish needs --title 'type(scope): summary' (explicit, never invented)"
  [ -n "$upstream" ] || die "publish needs --upstream-repo owner/repo"
  grep -Eq '^(feat|fix|perf|refactor|test|docs|build|ci|chore|revert|style|security)\([a-z0-9_-]+\): [^.]' <<<"$title" \
    || die "title is not a Conventional Commits header: $title"
  [ ${#title} -le 100 ] || die "title exceeds 100 chars"
  need_gh; new_work; local dir="$WORK/mirror"
  local sha; sha="$(fetch_pr_layout "$repo" "$num" "$dir")" || exit $EXIT_REFUSED
  printf 'Gate A  %s#%s  @ %s\n' "$repo" "$num" "$sha"
  # case_214e61d3, parallel gates: the upstream DRAFT opens as soon as the static
  # items pass and nothing has failed; fork CI and our review may still be
  # running beside Greptile. A completed red is still a refusal. --serial
  # restores the old rule (everything green before the draft exists). The
  # maintainer ask, `ready`, never relaxes: all three must pass on one head.
  if [ "$serial" -eq 1 ]; then GATE_ALLOW_PENDING=0; else GATE_ALLOW_PENDING=1; fi
  run_gate_a "$dir" "$repo" ""; local gcode=$?
  GATE_ALLOW_PENDING=0
  [ "$gcode" -eq 0 ] || { printf 'GATE A: %s — publish refused\n' \
    "$([ "$gcode" -eq 2 ] && echo REFUSED || echo FAIL)"; return $EXIT_FAIL; }
  if [ "${GATE_PENDING_COUNT:-0}" -gt 0 ]; then
    printf 'GATE A: PASS so far; %s item(s) still running (parallel mode: draft opens now, ready waits for all)\n' "$GATE_PENDING_COUNT"
  else printf 'GATE A: PASS\n'; fi
  local branch base
  branch="$(jq -r '.head.ref' "$dir/pr.json")"
  base="$(jq -r '.base.ref' "$dir/pr.json")"
  case "$base" in mirror-base/*) ;;
    *) refuse "mirror PR #$num bases '$base', not a mirror-base/<sha> branch";;
  esac
  # Refuse duplicates: one branch, one upstream PR. Two traps, both
  # measured 2026-10-06: `pr list --jq` without --json never fires, and
  # `--head owner:branch` is unsupported syntax. So: bare --head, --state
  # all (a closed-then-republished branch must still match), owner filtered
  # in jq, and a failed lookup refuses instead of reading as "no dup".
  local forkowner="${repo%%/*}" dup code
  [[ "$branch" =~ ^[A-Za-z0-9._/-]+$ ]] || refuse "refusing an unsafe branch key in duplicate lookup"
  [[ "$forkowner" =~ ^[A-Za-z0-9-]+$ ]] || refuse "refusing an unsafe owner key in duplicate lookup"
  local dfilt="([.[] | select(.headRefName == \"$branch\" and .headRepositoryOwner.login == \"$forkowner\") | .number] | .[0]) // empty"
  dup="$("$GH" pr list --repo "$upstream" --head "$branch" --state all --limit 100 \
    --json number,headRefName,headRepositoryOwner --jq "$dfilt" 2>"$WORK/dup.err")"
  code=$?
  [ $code -eq 0 ] || refuse "duplicate-PR lookup failed (gh exit $code: $(head -c 200 "$WORK/dup.err"))"
  if [[ "$dup" =~ ^[0-9]+$ ]]; then
    # Measured state, not a measurement failure: exit 1 (gate fail), not 2.
    printf 'publish refused: upstream #%s already exists for %s (will not open a second)\n' "$dup" "$branch"
    return $EXIT_FAIL
  fi
  # Stamp mirror provenance for the generator (public fork link only — the
  # Paperclip run link is host-48c35084.example.invalid, i.e. internal, and excluded).
  python3 - "$dir/pr.json" "$repo" "$num" <<'EOF'
import json, sys
p, repo, num = sys.argv[1], sys.argv[2], sys.argv[3]
v = json.load(open(p))
v["_mirror_ref"] = "%s#%s" % (repo, num)
json.dump(v, open(p, "w"))
EOF
  gen_upstream_body "$dir" "$title" > "$WORK/upstream_body.md"
  # Final code check on the GENERATED body: every [x] must be true-by-gate,
  # and the body itself must hold no internal ids.
  if grep -En '\[(x|X)\].*[Gg]reptile is 5/5' "$WORK/upstream_body.md" >/dev/null; then
    refuse "generator ticked the Greptile 5/5 box at publish (must stay [ ] until ready)"
  fi
  if grep -En '\bTOG-[0-9]+|\bPAP-?[0-9]+|deploymentexample\.net|\.ts\.net|localhost|agent://' \
       "$WORK/upstream_body.md" >/dev/null; then
    refuse "generated upstream body holds internal references:"
  fi
  # case_214e61d3, the pre-push linter. It runs on the text and diff that are about
  # to go public (the generated body, the explicit title, the mirror commits and
  # files), against this upstream repo's maintainer memory. Fail closed: no
  # memory file, an unreadable diff or a changed upstream template all refuse.
  local pub="$WORK/pub"; mkdir -p "$pub"; cp "$dir"/*.json "$pub"/
  python3 - "$pub/pr.json" "$title" "$WORK/upstream_body.md" <<'EOF'
import json, sys
p, title, bodyf = sys.argv[1], sys.argv[2], sys.argv[3]
v = json.load(open(p)); v["title"] = title; v["body"] = open(bodyf).read()
json.dump(v, open(p, "w"))
EOF
  local lline lcode
  LINT_REPO="$upstream" LINT_STAGE=first-push LINT_LIVE=1
  lline="$(eval_lint "$pub")"; lcode=$?
  unset LINT_REPO LINT_STAGE LINT_LIVE
  [ $lcode -eq 2 ] && { echo "REFUSED ${lline#REFUSED}"; printf 'PRE-PUSH LINT: REFUSED — publish refused\n'; return $EXIT_REFUSED; }
  echo "$lline"
  case "$lline" in FAIL*)
    printf 'PRE-PUSH LINT: FAIL — publish refused (run: upstream_lint.py lint --layout <dir> for the itemized list)\n'
    python3 "$SELFDIR/upstream_lint.py" lint --repo "$upstream" --layout "$pub" --stage first-push 2>&1 | sed 's/^/  /'
    return $EXIT_FAIL;;
  esac
  # Batch policy: fewer, complete PRs. A second PR touching files one of our
  # open PRs already touches is folded into it unless a maintainer asked to
  # split (public link required).
  local bline bcode
  bline="$(python3 "$SELFDIR/upstream_lint.py" batch --repo "$upstream" --layout "$pub" \
    ${split:+--maintainer-split-url "$split"} --gate-line 2>"$WORK/batch.err")"; bcode=$?
  case "$bcode" in
    0|1) echo "$bline";;
    *)   echo "REFUSED ${bline#REFUSED	}$(head -c 200 "$WORK/batch.err")"; printf 'BATCH POLICY: REFUSED — publish refused\n'; return $EXIT_REFUSED;;
  esac
  [ "$bcode" -eq 0 ] || { printf 'BATCH POLICY: FAIL — publish refused\n'; return $EXIT_FAIL; }
  if [ "$dry" -eq 1 ]; then
    echo "--- upstream body (dry run, not opened) ---"
    cat "$WORK/upstream_body.md"
    return $EXIT_PASS
  fi
  assert_branch_head "$repo" "$branch" "$sha"
  # Persist trusted v2 provenance on the mirror before opening the upstream PR.
  # Without it, deleting the body marker could make a new pipeline PR look legacy.
  python3 "$SELFDIR/upstream_lint.py" receipt-post --repo "$repo" --pr "$num" --head "$sha" \
    --context upstream-gate/lint --state success \
    --description "pass first-push head=${sha:0:12} pipeline=v2" \
    || refuse "could not record the v2 lint receipt on the mirror PR; publish refused"
  # The bundle rule lives HERE, not in prose: this is the only path that runs
  # `gh pr create` outside TogetherWeOwn, and it is unreachable unless the
  # static gate items and the linter passed on this exact SHA above.
  assert_branch_head "$repo" "$branch" "$sha"
  "$GH" pr create --repo "$upstream" --head "${repo%%/*}:$branch" --base master \
    --draft --title "$title" --body-file "$WORK/upstream_body.md" \
    || refuse "gh pr create failed (see gh output above)"
  printf 'published as draft (static gate + lint PASS @ %s)\n' "$sha"
}

# --- case_214e61d3: shared pair loader, parallel-gate status, receipts ----------------
# Loads the upstream layout and its mirror (the fork PR heading the same SHA)
# into PAIR_UDIR / PAIR_MDIR / PAIR_SHA / PAIR_MIRROR. Live mode needs new_work;
# fixtures mode reads DIR/upstream and DIR/mirror. A mirror at another head is a
# refusal, same as `ready` and `audit`.
load_pair() { # upstream num mirror fork-repo fixtures [need_mirror=1]
  local upstream="$1" num="$2" mirror="$3" repo="$4" fixtures="$5" need_mirror="${6:-1}" msha
  PAIR_MIRROR="$mirror"
  if [ -n "$fixtures" ]; then
    new_work
    PAIR_UDIR="$fixtures/upstream"; PAIR_MDIR="$fixtures/mirror"
    for f in "$PAIR_UDIR/pr.json"; do
      [ -f "$f" ] || refuse "fixtures missing $f"
    done
    [ "$need_mirror" -eq 0 ] || [ -f "$PAIR_MDIR/pr.json" ] || refuse "fixtures missing $PAIR_MDIR/pr.json"
  else
    if [ -z "${GH_TOKEN:-}" ]; then need_gh; fi
    new_work; PAIR_UDIR="$WORK/upstream"; PAIR_MDIR="$WORK/mirror"
    PAIR_SHA="$(fetch_pr_layout "$upstream" "$num" "$PAIR_UDIR")" || exit $EXIT_REFUSED
    if [ "$need_mirror" -eq 0 ]; then :
    elif [ -z "$PAIR_MIRROR" ]; then
      PAIR_MIRROR="$(mirror_for_sha "$repo" "$PAIR_SHA")" || exit $EXIT_REFUSED
    fi
    if [ "$need_mirror" -eq 0 ]; then :
    elif [ -n "$PAIR_MIRROR" ]; then
      fetch_pr_layout "$repo" "$PAIR_MIRROR" "$PAIR_MDIR" >/dev/null
      msha="$(jq -r '.head.sha' "$PAIR_MDIR/pr.json")"
      [ "$msha" = "$PAIR_SHA" ] || refuse "mirror #$PAIR_MIRROR head $msha != upstream head $PAIR_SHA"
    else
      refuse "no fork PR in $repo holds head $PAIR_SHA (the gates need a mirror PR)"
    fi
  fi
  PAIR_SHA="$(jq -r '.head.sha' "$PAIR_UDIR/pr.json")"
}

# gate_state_of LINE EXITCODE -> green|pending|red|refused
gate_state_of() {
  [ "$2" -eq 2 ] && { echo refused; return; }
  case "$1" in
    PASS*) echo green;;
    FAIL*"[pending]"*) echo pending;;
    FAIL*) echo red;;
    *) echo refused;;
  esac
}

# status_collect UDIR MDIR UPSTREAM [FORK] [FIXTURES] -> $WORK/status.tsv
# The three parallel gates first, then what `ready` additionally requires.
status_collect() {
  local udir="$1" mdir="$2" upstream="$3" fork="${4:-$FORK_REPO_DEFAULT}" fixt="${5:-}"
  local spec gate fn dir line code isdraft repo
  : > "$WORK/status.tsv"
  for spec in "fork-ci|eval_fork_ci|$mdir" "our-review|eval_our_review|$mdir" \
              "greptile|eval_greptile|$udir" "upstream-ci|eval_upstream_ci|$udir"; do
    IFS='|' read -r gate fn dir <<<"$spec"
    case "$gate" in
      fork-ci) line="$($fn "$dir" "$fork" "$fixt")" ;;
      upstream-ci) line="$($fn "$dir" "$upstream" "$fixt")" ;;
      *) line="$($fn "$dir")" ;;
    esac; code=$?
    printf '%s\t%s\t%s\n' "$gate" "$(gate_state_of "$line" $code)" "${line#*	}" >> "$WORK/status.tsv"
  done
  isdraft="$(jq -r '.draft' "$udir/pr.json")"
  LINT_REPO="$upstream" LINT_STAGE="$([ "$isdraft" = true ] && echo first-push || echo ready)"
  line="$(eval_lint "$udir")"; code=$?
  unset LINT_REPO LINT_STAGE
  printf '%s\t%s\t%s\n' "lint" "$(gate_state_of "$line" $code)" "${line#*	}" >> "$WORK/status.tsv"
  if [ -f "$mdir/comments.json" ]; then
    line="$(eval_reputation "$udir" "$mdir/comments.json")"; code=$?
    printf '%s\t%s\t%s\n' "reputation" "$(gate_state_of "$line" $code)" "${line#*	}" >> "$WORK/status.tsv"
  fi
}

cmd_status() { # --upstream-pr N --upstream-repo R [--mirror-pr M] [--fork-repo R] [--fixtures D] [--json]
  local num="" mirror="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures="" json=0
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --mirror-pr) mirror="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    --json) json=1; shift;;
    *) die "status: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "status needs --upstream-pr N"
  [ -n "$upstream" ] || die "status needs --upstream-repo owner/repo"
  load_pair "$upstream" "$num" "$mirror" "$repo" "$fixtures" || return $?
  status_collect "$PAIR_UDIR" "$PAIR_MDIR" "$upstream" "$repo" "$fixtures"
  local isdraft; isdraft="$(jq -r '.draft' "$PAIR_UDIR/pr.json")"
  # The three parallel gates decide the verdict; the rest is shown for the
  # next step (lint/reputation block `ready`, not the "all three" verdict).
  local red="" pend="" refused="" green=0 g st d
  while IFS=$'\t' read -r g st d; do
    case "$g" in fork-ci|our-review|greptile) ;; *) continue;; esac
    case "$st" in
      green) green=$((green+1));;
      pending) pend="$pend $g";;
      red) red="$red $g";;
      *) refused="$refused $g";;
    esac
  done < "$WORK/status.tsv"
  local verdict code
  if [ -n "$refused" ]; then verdict="REFUSED (cannot measure:$refused)"; code=$EXIT_REFUSED
  elif [ -n "$red" ]; then verdict="RED (failing:$red)"; code=$EXIT_FAIL
  elif [ -n "$pend" ]; then verdict="PENDING (running:$pend)"; code=$EXIT_PENDING
  else verdict="ALL THREE GREEN"; code=$EXIT_PASS; fi
  if [ "$json" -eq 1 ]; then
    python3 - "$upstream" "$num" "$PAIR_SHA" "$isdraft" "$verdict" "$code" "$WORK/status.tsv" <<'EOF'
import json, sys
repo, num, sha, draft, verdict, code, tsv = sys.argv[1:8]
gates = {}
for ln in open(tsv).read().splitlines():
    g, st, d = (ln.split("\t", 2) + ["", ""])[:3]
    gates[g] = {"state": st, "detail": d}
print(json.dumps({"repo": repo, "pr": int(num), "head": sha, "draft": draft == "true",
                  "verdict": verdict, "all_green": code == "0", "exit": int(code), "gates": gates}))
EOF
    return $code
  fi
  printf 'status  %s#%s  @ %s  draft=%s\n' "$upstream" "$num" "$PAIR_SHA" "$isdraft"
  while IFS=$'\t' read -r g st d; do printf '  %-11s %-8s %s\n' "$g" "$st" "$d"; done < "$WORK/status.tsv"
  printf 'GATES: %s on %s\n' "$verdict" "${PAIR_SHA:0:12}"
  return $code
}

cmd_lint() { # --upstream-pr N --upstream-repo R [--stage first-push|ready] [--fixtures D]
  local num="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures="" stage="" mirror=""
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --mirror-pr) mirror="${2:-}"; shift 2;;
    --stage) stage="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    *) die "lint: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "lint needs --upstream-pr N (for a branch that is not a PR yet use: upstream_lint.py lint)"
  [ -n "$upstream" ] || die "lint needs --upstream-repo owner/repo"
  load_pair "$upstream" "$num" "$mirror" "$repo" "$fixtures" 0 || return $?
  [ -n "$stage" ] || stage="$([ "$(jq -r '.draft' "$PAIR_UDIR/pr.json")" = true ] && echo first-push || echo ready)"
  local live=""; [ -z "$fixtures" ] && live="--live"
  python3 "$SELFDIR/upstream_lint.py" lint --repo "$upstream" --layout "$PAIR_UDIR" --stage "$stage" $live
}

cmd_reputation() { # --upstream-pr N --upstream-repo R [--reviewer-model M] [--post-receipt] [--response-file F]
  local num="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures="" mirror="" model="" post=0 resp=""
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --mirror-pr) mirror="${2:-}"; shift 2;;
    --reviewer-model) model="${2:-}"; shift 2;;
    --post-receipt) post=1; shift;;
    --response-file) resp="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    *) die "reputation: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "reputation needs --upstream-pr N"
  [ -n "$upstream" ] || die "reputation needs --upstream-repo owner/repo"
  # The offline seam must never post: a file-backed "clean" receipt would
  # verify as a real second-model pass (Paperclip Review 3/5 at f57ac32).
  if [ "$post" -eq 1 ] && [ -n "$resp" ]; then refuse "--response-file with --post-receipt is refused"; fi
  load_pair "$upstream" "$num" "$mirror" "$repo" "$fixtures" || return $?
  status_collect "$PAIR_UDIR" "$PAIR_MDIR" "$upstream" "$repo" "$fixtures"
  # The reviewer is shown what code measured, so a claim like "all CI green"
  # can be checked against the real state rather than taken on trust.
  { echo "head $PAIR_SHA"
    while IFS=$'\t' read -r g st d; do
      case "$g" in reputation) continue;; esac
      echo "- $g: $st ($d)"
    done < "$WORK/status.tsv"; } > "$WORK/facts.txt"
  python3 "$SELFDIR/upstream_reputation.py" run --repo "$upstream" --pr "$num" --fork-repo "$repo" \
    ${PAIR_MIRROR:+--mirror-pr "$PAIR_MIRROR"} \
    --layout "$PAIR_UDIR" --facts-file "$WORK/facts.txt" \
    ${model:+--reviewer-model "$model"} ${resp:+--response-file "$resp"} \
    $([ "$post" -eq 1 ] && echo --post-receipt)
}

# receipts: the markdown block that goes on the card. Everything in it is read
# back from GitHub or measured now; nothing is typed by hand.
cmd_receipts() { # --upstream-pr N --upstream-repo R [--mirror-pr M] [--fork-repo R] [--fixtures D]
  local num="" mirror="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures=""
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --mirror-pr) mirror="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    *) die "receipts: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "receipts needs --upstream-pr N"
  [ -n "$upstream" ] || die "receipts needs --upstream-repo owner/repo"
  load_pair "$upstream" "$num" "$mirror" "$repo" "$fixtures" || return $?
  status_collect "$PAIR_UDIR" "$PAIR_MDIR" "$upstream" "$repo" "$fixtures"
  python3 - "$upstream" "$num" "$PAIR_SHA" "${PAIR_MIRROR:-}" "$repo" "$WORK/status.tsv" \
    "$PAIR_UDIR/pr.json" "$PAIR_MDIR/comments.json" "$SELFDIR" <<'EOF'
import json, os, sys, datetime
upstream, num, sha, mirror, fork, tsv, prjson, cjson, selfdir = sys.argv[1:10]
sys.path.insert(0, selfdir)
import upstream_lint as ul
pr = json.load(open(prjson))
comments = json.load(open(cjson)) if os.path.isfile(cjson) else []
try:
    sts = [r for r in ul.receipts_from_comments(comments, ul.whoami()) if r["head"] == sha]
except ul.Refused:
    sts = []
def latest(ctx):
    mine = [s for s in sts if s.get("context") == ctx]
    return mine[-1] if mine else None
print("### Upstream pipeline receipts: %s#%s @ `%s`" % (upstream, num, sha[:12]))
print("")
print("Generated by `upstream_gate.sh receipts` at %s. Every row is read back from GitHub or measured now." % (
    datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ")))
print("")
print("- upstream PR: https://github.com/%s/pull/%s (draft=%s)" % (upstream, num, str(pr.get("draft")).lower()))
if mirror:
    print("- mirror PR: https://github.com/%s/pull/%s" % (fork, mirror))
print("- head: `%s`" % sha)
print("")
print("| Stage | State | Evidence |")
print("|---|---|---|")
for ln in open(tsv).read().splitlines():
    g, st, d = (ln.split("\t", 2) + ["", ""])[:3]
    print("| %s | %s | %s |" % (g, st, d.replace("|", "/")))
print("")
for ctx in ("upstream-gate/lint", "upstream-gate/reputation", "upstream-gate/ready"):
    s = latest(ctx)
    print("- receipt `%s`: %s" % (ctx, ("%s, %s: %s" % (s.get("state"), (s.get("created_at") or "?")[:16], s.get("description"))) if s else "none recorded on this head"))
EOF
}

# case_214e61d3: tick ONLY the exact boxes the gate measures itself. Prints the
# count. Near-matches remain untouched so an unrelated claim cannot be marked.
tick_verified_boxes() { # in-pr.json out-pr.json -> stdout: number of boxes ticked
  python3 - "$1" "$2" <<'EOF'
import json, re, sys
pr = json.load(open(sys.argv[1]))
VERIFIED = {
    "all paperclip ci gates are green",
    "all paperclip ci gates are green (pending; upstream-gate ready ticks it after measuring)",
    "greptile is 5/5 with no open p2s, recommendations, or follow-ups",
    "greptile is 5/5 with no open p2s, recommendations, or follow-ups (pending upstream review; upstream-gate ready requires it)",
    "i will address all greptile and reviewer comments before requesting merge",
    "i will address all greptile and reviewer comments before requesting merge (standing commitment)",
}
n = 0
def tick(m):
    global n
    label = m.group(1).strip().lower().replace("`", "")
    if label in VERIFIED:
        n += 1
        return m.group(0).replace("[ ]", "[x]", 1)
    return m.group(0)
pr["body"] = re.sub(r"(?m)^[ \t]*-[ \t]*\[ \](.*)$", tick, pr.get("body") or "")
json.dump(pr, open(sys.argv[2], "w"))
print(n)
EOF
}

cmd_ready() { # --upstream-pr N [--mirror-pr M] [--upstream-repo R] [--fork-repo R] [--fixtures D] [--dry-run]
  local num="" mirror="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures="" dry=0
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --mirror-pr) mirror="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    --dry-run) dry=1; shift;;
    *) die "ready: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "ready needs --upstream-pr N"
  [ -n "$upstream" ] || die "ready needs --upstream-repo owner/repo"
  load_pair "$upstream" "$num" "$mirror" "$repo" "$fixtures" || return $?
  local udir="$PAIR_UDIR" mdir="$PAIR_MDIR" sha="$PAIR_SHA"
  printf 'ready  %s#%s  @ %s\n' "$upstream" "$num" "$sha"
  # The virtual body: boxes the gate measures itself are treated as ticked for
  # the checklist and lint evaluation, then really ticked below if (and only
  # if) every item passes.
  local vdir="$WORK/ready_v" nticks; mkdir -p "$vdir"; cp "$udir"/*.json "$vdir"/
  nticks="$(tick_verified_boxes "$udir/pr.json" "$vdir/pr.json")" || refuse "cannot prepare the ready-state body"
  local fail=0 line code
  line="$(eval_upstream_checklist "$vdir" strict)"; code=$?
  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }
  [ "$code" -eq 0 ] || refuse "upstream checklist evaluator exited $code"
  echo "$line"; case "$line" in FAIL*) fail=1;; esac
  for ev in "eval_upstream_ci upstream-ci" "eval_greptile greptile"; do
    set -- $ev
    if [ "$1" = "eval_upstream_ci" ]; then line="$("$1" "$udir" "$upstream" "$fixtures")"; code=$?
    else line="$($1 "$udir")"; code=$?; fi
    [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }
    [ "$code" -eq 0 ] || refuse "$1 evaluator exited $code"
    echo "$line"
    case "$line" in FAIL*) fail=1;; esac
  done
  echo "-- mirror Gate A still passing on $sha --"
  GATE_ALLOW_PENDING=0 run_gate_a "$mdir" "$repo" "$fixtures"; local gcode=$?
  [ "$gcode" -eq 2 ] && { echo "REFUSED mirror Gate A unmeasurable"; return $EXIT_REFUSED; }
  [ "$gcode" -eq 0 ] || fail=1
  echo "-- case_214e61d3: pre-push lint and second-model reputation pass on the same head --"
  LINT_REPO="$upstream" LINT_STAGE=ready; [ -z "$fixtures" ] && LINT_LIVE=1
  line="$(eval_lint "$vdir")"; code=$?
  unset LINT_REPO LINT_STAGE LINT_LIVE
  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }
  [ "$code" -eq 0 ] || refuse "pre-push linter evaluator exited $code"
  echo "$line"; case "$line" in FAIL*) fail=1;; esac
  [ -f "$mdir/comments.json" ] || refuse "mirror layout has no comments.json (the reputation receipt cannot be read)"
  line="$(eval_reputation "$vdir" "$mdir/comments.json")"; code=$?
  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }
  [ "$code" -eq 0 ] || refuse "reputation evaluator exited $code"
  echo "$line"; case "$line" in FAIL*) fail=1;; esac
  if [ "$fail" -ne 0 ]; then printf 'READY: REFUSED — items above fail\n'; return $EXIT_FAIL; fi
  local isdraft; isdraft="$(jq -r '.draft' "$udir/pr.json")"
  if [ -z "$fixtures" ] && [ "$isdraft" != "true" ]; then
    echo "already ready; head verified @ $sha"
    return $EXIT_PASS
  fi
  if [ -n "$fixtures" ] || [ "$dry" -eq 1 ]; then
    echo "READY: would tick $nticks gate-verified box(es) and mark #$num ready for review @ $sha (dry run)"
    return $EXIT_PASS
  fi
  # Recheck the head after evaluation and again immediately before marking ready.
  # A changed head invalidates the measured gates even when the PR number is stable.
  need_gh
  assert_pr_head "$upstream" "$num" "$sha"
  if [ "${nticks:-0}" -gt 0 ]; then
    # -j (join) writes the body byte-for-byte: -r appends a newline, which
    # changes the reputation text hash and makes the next audit FAIL the PR
    # that ready just marked (Paperclip Review 3/5 at f57ac32).
    jq -j '.body // ""' "$vdir/pr.json" > "$WORK/ready_body.md"
    "$GH" api -X PATCH "repos/$upstream/pulls/$num" -F "body=@$WORK/ready_body.md" >/dev/null \
      || refuse "could not tick the gate-verified boxes in the upstream body"
    echo "ticked $nticks gate-verified box(es) in the upstream body"
  fi
  assert_pr_head "$upstream" "$num" "$sha"
  "$GH" pr ready --repo "$upstream" "$num" || refuse "gh pr ready failed"
  printf 'marked ready for review @ %s\n' "$sha"
  python3 "$SELFDIR/upstream_lint.py" receipt-post --repo "$repo" --pr "$PAIR_MIRROR" --head "$sha" \
    --context upstream-gate/ready --state success \
    --description "ready: fork CI, Paperclip Review, Greptile, lint, reputation all pass head=${sha:0:12}" \
    || echo "warning: ready receipt not recorded on the mirror PR" >&2
}

cmd_audit() { # --upstream-pr N [--upstream-repo R] [--fork-repo R] [--fixtures D] [--enforce] [--json]
  local num="" upstream="" repo="$FORK_REPO_DEFAULT" fixtures="" enforce=0 json=0 nodeid="" draft_out=""
  while [ $# -gt 0 ]; do case "$1" in
    --upstream-pr) num="${2:-}"; shift 2;;
    --upstream-repo) upstream="${2:-}"; shift 2;;
    --fork-repo) repo="${2:-}"; shift 2;;
    --fixtures) fixtures="${2:-}"; shift 2;;
    --enforce) enforce=1; shift;;
    --json) json=1; shift;;
    *) die "audit: unknown option '$1'";;
  esac; done
  [ -n "$num" ] || die "audit needs --upstream-pr N"
  [ -n "$upstream" ] || die "audit needs --upstream-repo owner/repo"
  local udir mdir sha
  # The v2 evaluators write scratch files, so fixtures mode needs a work dir too.
  if [ -n "$fixtures" ]; then new_work; udir="$fixtures/upstream"; mdir="$fixtures/mirror"
  else
    # Read-only fetch: curl covers GH_TOKEN runs; gh is needed only for
    # the --enforce mutation below (or when GH_TOKEN is absent).
    if [ -z "${GH_TOKEN:-}" ]; then need_gh; fi; new_work; udir="$WORK/upstream"; mdir="$WORK/mirror"
    sha="$(fetch_pr_layout "$upstream" "$num" "$udir")" || exit $EXIT_REFUSED
    # mirror_for_sha exits 2 on lookup failure (fail-closed: an outage is
    # not "no mirror"). An empty result is a genuine miss, scored below.
    mirror="$(mirror_for_sha "$repo" "$sha")" || exit $EXIT_REFUSED
    if [ -n "$mirror" ]; then
      fetch_pr_layout "$repo" "$mirror" "$mdir" >/dev/null
      # Belt and braces with the .head.sha pin in mirror_for_sha: never
      # score Gate A against a mirror on another head (same check as ready).
      msha="$(jq -r '.head.sha' "$mdir/pr.json")"
      [ "$msha" = "$sha" ] || refuse "mirror #$mirror head $msha != upstream head $sha"
    else mkdir -p "$mdir"; fi
  fi
  sha="$(jq -r '.head.sha' "$udir/pr.json")"
  local isdraft; isdraft="$(jq -r '.draft' "$udir/pr.json")"
  # A missing draft flag is unmeasurable readiness state, not "not a draft":
  # converting on draft=null is the fail-open Finding 1 was built against.
  case "$isdraft" in true|false) ;;
    *) echo "REFUSED cannot read draft state for $upstream#$num" >&2; return $EXIT_REFUSED;;
  esac
  # Fail-closed accumulator: a refusal ("could not measure") always blocks PASS.
  # A lone refusal exits 2 with NO mutation. The single exception is the
  # transient mergeable-null: GitHub returns null on the first GET after any
  # head or base push, and that must not shield a dirty PR when another item
  # has a measured FAIL (moving a known-bad non-draft to draft is the safe
  # state). All other refusals (API errors, provenance, checklist, lint,
  # reputation) still veto conversion even alongside a FAIL.
  local items=() fail=0 refused=0 hard_refused=0 line code v2_receipt_error=""
  collect() { # evaluator dir label [hard: 1=vetoes conversion, 0=mergeable transient]
    local hard="${4:-1}"
    line="$($1 "$2")"; code=$?
    # An evaluator answers through its text (PASS/FAIL) and exits 0; any other
    # exit is "could not measure", never a blank passing item.
    if [ $code -ne 0 ]; then
      line="$(emit_refused "$line")"; refused=1
      [ "$hard" -eq 1 ] && hard_refused=1
    fi
    items+=("$3: $line")
    case "$line" in FAIL*) fail=1;; esac
  }
  collect_ci() { # evaluator dir label repo fixtures [hard]
    local hard="${6:-1}"
    line="$($1 "$2" "$4" "$5")"; code=$?
    if [ $code -ne 0 ]; then
      line="$(emit_refused "$line")"; refused=1
      [ "$hard" -eq 1 ] && hard_refused=1
    fi
    items+=("$3: $line")
    case "$line" in FAIL*) fail=1;; esac
  }
  collect_ci eval_upstream_ci "$udir" "upstream-ci" "$upstream" "$fixtures"
  collect eval_greptile "$udir" "greptile"
  # Retrofit checklist is scored on the UPSTREAM body (the artifact
  # maintainers read). Mirror stubs predate this gate and are scored only
  # by `check`/`publish` for NEW work, where the mirror carries the proposal.
  line="$(eval_upstream_checklist "$udir")"; code=$?
  [ $code -ne 0 ] && { line="$(emit_refused "$line")"; refused=1; hard_refused=1; }
  items+=("up-checklist: $line"); case "$line" in FAIL*) fail=1;; esac
  # Mergeable-null is the transient exception (hard=0): it blocks PASS but
  # never vetoes conversion of a measured FAIL.
  collect eval_mergeable "$udir" "mergeable" 0
  if [ -f "$mdir/pr.json" ]; then
    for ev in eval_fork_ci eval_our_review eval_no_internal_ids eval_single_commit; do
      collect_ci "$ev" "$mdir" "mirror" "$repo" "$fixtures"
    done
  else
    items+=("mirror: FAIL no fork PR holds head $sha (Gate A needs a mirror PR)"); fail=1
  fi
  # case_214e61d3. The body marker identifies v2 PRs, and a trusted v2 lint receipt
  # remains durable provenance if the marker is later removed or the head moves.
  # The lint and reputation evaluators still bind their results to current text.
  # For v2, a NON-draft needs the second-model pass for this exact head and text.
  local v2=0 upstream_body v2_receipt
  upstream_body="$(jq -r '.body // ""' "$udir/pr.json")" || refuse "cannot read upstream PR body"
  if grep -qF 'pipeline: upstream-gate v2' <<<"$upstream_body"; then
    v2=1
  elif [ -f "$mdir/comments.json" ]; then
    v2_receipt="$(eval_v2_receipt "$mdir/comments.json" "$sha")"; code=$?
    if [ "$code" -ne 0 ]; then v2_receipt_error="${v2_receipt:-provenance probe exited $code without a verdict}"
    elif [ "$v2_receipt" = "1" ]; then v2=1
    fi
  else
    v2_receipt_error="no matching mirror PR or comments are available; cannot verify markerless pipeline provenance"
  fi
  if [ -n "$v2_receipt_error" ]; then
    items+=("pipeline-provenance: REFUSED $v2_receipt_error"); refused=1; hard_refused=1
  fi
  if [ "$v2" -eq 1 ]; then
    LINT_REPO="$upstream" LINT_STAGE="$([ "$isdraft" = true ] && echo first-push || echo ready)"
    collect eval_lint "$udir" "lint"
    unset LINT_REPO LINT_STAGE
    if [ "$isdraft" != "true" ]; then
      if [ -f "$mdir/comments.json" ]; then
        line="$(eval_reputation "$udir" "$mdir/comments.json")"; code=$?
        [ $code -ne 0 ] && { line="$(emit_refused "$line")"; refused=1; hard_refused=1; }
        items+=("reputation: $line"); case "$line" in FAIL*) fail=1;; esac
      else
        items+=("reputation: FAIL no mirror comments to read the receipt from"); fail=1
      fi
    fi
  elif [ -z "$v2_receipt_error" ]; then
    items+=("pipeline: legacy PR (no v2 marker); lint and reputation receipt are advisory, not enforced")
  fi
  # A hard refusal, or any refusal without a measured FAIL, exits 2 with no
  # mutation. The mergeable transient (hard=0) rides along when a FAIL is
  # measured and conversion proceeds below.
  if [ "$hard_refused" -ne 0 ] || { [ "$refused" -ne 0 ] && [ "$fail" -eq 0 ]; }; then
    if [ "$json" -eq 1 ]; then
      python3 -c 'import json,sys; print(json.dumps({
        "repo": sys.argv[1], "pr": int(sys.argv[2]), "head": sys.argv[3],
        "draft": sys.argv[4] == "true", "pass": False, "refused": True, "v2": sys.argv[6] == "1",
        "converted": False,
        "items": sys.argv[5].split("\n")}))' \
        "$upstream" "$num" "$sha" "$isdraft" "$(printf '%s\n' "${items[@]}")" "$v2"
    else
      printf 'audit  %s#%s  @ %s  draft=%s\n' "$upstream" "$num" "$sha" "$isdraft"
      printf '  %s\n' "${items[@]}"
      echo "AUDIT: REFUSED — something could not be measured; no mutation made"
    fi
    return $EXIT_REFUSED
  fi
  # The convert-to-draft mutation runs for text and JSON alike: --json never
  # silences --enforce. It fires only on a measured FAIL of a non-draft (any
  # hard refusal returns above with no mutation; a passing or already-draft PR
  # has nothing to convert). The mergeable transient rides along in the record
  # but does not veto the conversion once a FAIL is measured.
  local converted=0
  if [ $fail -ne 0 ] && [ "$isdraft" != "true" ] && [ "$enforce" -eq 1 ] && [ -z "$fixtures" ]; then
    # The convert-to-draft mutation runs through gh (no curl path).
    need_gh
    # NOTE: `pr view --jq` without --json is refused by gh ("cannot use
    # --jq without specifying --json"); the id field must be named.
    nodeid="$("$GH" pr view "$num" --repo "$upstream" --json id --jq '.id' 2>/dev/null)" \
      || refuse "cannot read node id for #$num"
    [ -n "$nodeid" ] || refuse "cannot read node id for #$num"
    draft_out="$("$GH" api graphql -F pullRequestId="$nodeid" \
      -f query='mutation($pullRequestId: ID!) {
        convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
          pullRequest { isDraft } } }' --jq '.data.convertPullRequestToDraft.pullRequest.isDraft')" \
      || refuse "convert-to-draft mutation failed"
    grep -q true <<<"$draft_out" || refuse "convert-to-draft mutation failed"
    converted=1
  fi
  if [ "$json" -eq 1 ]; then
    python3 -c 'import json,sys; print(json.dumps({
      "repo": sys.argv[1], "pr": int(sys.argv[2]), "head": sys.argv[3],
      "draft": sys.argv[4] == "true", "pass": sys.argv[5] == "1", "v2": sys.argv[7] == "1",
      "converted": sys.argv[8] == "1", "refused": sys.argv[9] == "1",
      "items": sys.argv[6].split("\n")}))' \
      "$upstream" "$num" "$sha" "$isdraft" "$([ $fail -eq 0 ] && echo 1 || echo 0)" \
      "$(printf '%s\n' "${items[@]}")" "$v2" "$converted" "$refused"
    # A lone refusal already returned 2 above; reaching here with fail==0 is a
    # clean PASS. Any measured FAIL exits 1, even with refused items alongside.
    [ $fail -eq 0 ] && return $EXIT_PASS || return $EXIT_FAIL
  fi
  printf 'audit  %s#%s  @ %s  draft=%s\n' "$upstream" "$num" "$sha" "$isdraft"
  printf '  %s\n' "${items[@]}"
  if [ $fail -eq 0 ]; then echo "AUDIT: PASS (record head $sha)"; return $EXIT_PASS; fi
  if [ "$isdraft" = "true" ]; then
    echo "AUDIT: FAIL but already draft — fix items above, then re-run ready"
    return $EXIT_FAIL
  fi
  echo "AUDIT: FAIL and NOT A DRAFT — convert to draft now"
  if [ "$converted" -eq 1 ]; then
    echo "converted #$num to draft"
  fi
  return $EXIT_FAIL
}

usage() { sed -n '2,/^# MEASURED FACTS/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

main() {
  local cmd="${1:-}"; [ $# -gt 0 ] && shift
  case "$cmd" in
    check) cmd_check "$@";; publish) cmd_publish "$@";;
    ready) cmd_ready "$@";; audit) cmd_audit "$@";;
    status) cmd_status "$@";; lint) cmd_lint "$@";;
    reputation) cmd_reputation "$@";; receipts) cmd_receipts "$@";;
    -h|--help|help) usage; exit $EXIT_PASS;;
    "") echo "$ME: no command given — nothing was measured" >&2; usage >&2; exit $EXIT_REFUSED;;
    *) echo "$ME: unknown command '$cmd'" >&2; usage >&2; exit $EXIT_USAGE;;
  esac
}
main "$@"
