#!/usr/bin/env bash
# ===========================================================================
# test_red_main_poll.sh — offline suite for red_main_poll.sh AND the
# read-only host wrapper ops/case_e77a1e99/red_main_poll_check.sh (case_e77a1e99).
#
# No network, no credentials, no board writes: a stub GitHub API on
# 127.0.0.1 serves a distinct CI case off each repo NAME (same convention as
# test_gh_ci_status.sh, which serves cases off ref names), and the board read
# arrives through INCIDENT_SOURCE_CMD. The stub also serves the real
# gh_ci_status.sh binary from this repo, so verdict classification is the
# production reader's, not a reimplementation of it.
#
# WHAT THIS SUITE IS BUILT TO CATCH, beyond the happy path:
#
#  * A SHA-KEYED SIGNATURE. Rule 1 says the signature keys on the failure
#    set, not the head SHA: the stub serves two red repos with the SAME two
#    failing jobs at DIFFERENT head SHAs, and the suite pins identical
#    signatures — plus a third repo with a different failing set pinning a
#    different signature. Keying on the SHA would open one card per commit
#    for the whole life of the red.
#  * AN ORDER-DEPENDENT SIGNATURE. The Checks API returns runs in an order
#    no caller should depend on; the stub serves the same two failures in
#    reversed order and the suite pins the same signature (rule 2).
#  * A CASE-SPLIT SIGNATURE. The contract keys on the lowercased set: a repo
#    whose only difference is "Broker Suite" vs "broker suite" must hash to
#    the same signature, not open a second incident.
#  * A DUPLICATE PER TICK. The suite seeds an open board card carrying the
#    exact contract tag and pins incidentExists:true plus zero drafts on a
#    repeat poll (the DONE acceptance: exactly one card per repo+signature).
#  * A GREEN THAT WAS NEVER OBSERVED. Denied, pending, empty-signal and
#    never-ran (case_0020a490) repos must exit 3 with no entry — never an empty
#    fragment that reads as "all green" (rule 4). Never-ran in particular
#    must not draft a repo-lead incident: there is nothing in the diff to
#    fix, so an incident card would send a human chasing a billing problem
#    as a code breakage.
#  * A BLIND BOARD READ AS "NO INCIDENT". With INCIDENT_SOURCE_CMD failing,
#    both subcommands must exit 3 and print no snapshot and no drafts — an
#    `incidentExists: false` nobody measured manufactures a duplicate per
#    tick (rule 3).
#  * A PHANTOM EMPTY-JOBS SIGNATURE. A fail verdict with zero failing check
#    runs (external commit-status red) must key on the reader reason, never
#    hash the empty set — two status-reds with different reasons must not
#    share one signature.
#  * A BRANCH THAT MOVES MID-PAGINATION (case_baa5bc77). The poller resolves
#    `main` once and pages check runs at the pinned commit; the stub serves
#    o/red-moving with a short page 2 at the moving ref (which the pre-fix
#    poller keyed on the reader reason, rotating the incident signature for
#    the same breakage) and a stable page 2 at the pinned commit.
#  * AN UNRESOLVABLE PIN (case_baa5bc77 review). If `main` itself does not resolve,
#    the repo is UNKNOWN with no entry — never a verdict read at the moving
#    ref paired with a refused check-set read, which would key the incident on
#    the reader reason and rotate the signature for the same breakage. The stub
#    serves o/red-resolve-fail with a 502 on the poller's first resolve only.
# ===========================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${RED_MAIN_POLL_SH:-$HERE/red_main_poll.sh}"
PASS=0; FAIL=0

WORK="$(mktemp -d)"
cleanup() {
  [[ -z "${HANG_PID:-}" ]] || { kill "$HANG_PID" 2>/dev/null || true; wait "$HANG_PID" 2>/dev/null || true; }
  [[ -z "${STUB_PID:-}" ]] || { kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; }
  rm -rf "$WORK"
}
trap cleanup EXIT

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

for c in node curl jq sha1sum python3 timeout; do
  command -v "$c" >/dev/null 2>&1 || { echo "test_red_main_poll: $c is required" >&2; exit 2; }
done
[[ -x "$TOOL" ]] || { echo "test_red_main_poll: tool missing at $TOOL" >&2; exit 2; }
[[ -x "$HERE/gh_ci_status.sh" ]] || { echo "test_red_main_poll: real reader missing at $HERE/gh_ci_status.sh -- run bash scripts/sync-toolkit.sh first (INSTALL.md §3 restores the vendored reader)" >&2; exit 2; }

# ---------------------------------------------------------------------------
# The stub: serves gh_ci_status.sh's three endpoints plus raw check-runs,
# keyed off the repo NAME (the ref is always `main` here).
#
#   o/red-same-a, o/red-same-b  same two failures, different head SHAs
#   o/red-reorder               same two failures, reversed run order
#   o/red-case                  same two failures, one name capitalised
#   o/red-other                 a different failing set
#   o/red-status-only           empty check runs, failing commit status
#   o/green                     all success
#   o/denied                    403 everywhere
#   o/empty                     200 with zero runs/statuses everywhere
#   o/pending                   one job in_progress
#   o/never-ran                 4x completed/failure with the case_0020a490 billing
#                               annotation at failure level (genuine non-start)
#   o/evil-name                 a failing run literally named "HEAD_SHA=x"
cat > "$WORK/stub-api.js" <<'STUB'
const http = require('http')

// IDs are literal per repo: the reader's annotations probe keys on run id,
// so random ids would unmoor the never-ran classification from the repo it
// belongs to. Durations are 3s (like the real 4f2bea9 non-start): the reader
// only probes failures at or under GH_CI_NONSTART_MAX_SECONDS, so a 5-minute
// fixture duration would skip the probe and read as a genuine red.
const completed = (id, name, conclusion, sha) => ({ name, status: 'completed', conclusion, head_sha: sha, id, started_at: '2026-10-03T14:00:00Z', completed_at: '2026-10-03T14:00:03Z' })
const running = (id, name, sha) => ({ name, status: 'in_progress', conclusion: null, head_sha: sha, id, started_at: '2026-10-03T14:00:00Z', completed_at: null })

const SHA_A = 'aaa0000000000000000000000000000000000001'
const SHA_B = 'bbb0000000000000000000000000000000000002'
const SHA_M = 'ccc0000000000000000000000000000000000003'

const fail2A = [completed(810001, 'broker suite', 'failure', SHA_A), completed(810002, 'Offline suites', 'failure', SHA_A), completed(810003, 'lint', 'success', SHA_A)]
const fail2B = [completed(820001, 'broker suite', 'failure', SHA_B), completed(820002, 'Offline suites', 'failure', SHA_B), completed(820003, 'lint', 'success', SHA_B)]
const fail2R = [completed(830003, 'lint', 'success', SHA_A), completed(830002, 'Offline suites', 'failure', SHA_A), completed(830001, 'broker suite', 'failure', SHA_A)]
const fail2C = [completed(840001, 'Broker Suite', 'failure', SHA_A), completed(840002, 'Offline suites', 'failure', SHA_A), completed(840003, 'lint', 'success', SHA_A)]
const failOther = [completed(850001, 'e2e staging', 'failure', SHA_A), completed(850002, 'lint', 'success', SHA_A)]
const greenRuns = [completed(860001, 'broker suite', 'success', SHA_A), completed(860002, 'Offline suites', 'success', SHA_A)]
const evilRuns = [completed(870001, 'HEAD_SHA=zzz', 'failure', SHA_A), completed(870002, 'lint', 'success', SHA_A)]
const neverRanRuns = [completed(890001, 'broker suite', 'failure', SHA_A), completed(890002, 'Offline suites', 'failure', SHA_A), completed(890003, 'lint', 'success', SHA_A)]
const annBilling = [{ annotation_level: 'failure', message: 'The job was not started because the account hit its spending limit.' }]

const failPunct = [completed(880001, 'lint_b', 'failure', SHA_A), completed(880002, 'Lint.c', 'failure', SHA_A), completed(880003, 'lint-a', 'failure', SHA_A), completed(880004, 'lint a', 'failure', SHA_A), completed(880005, 'unit', 'success', SHA_A)]
// A full first page of successes plus the failure on page 2: a single-page
// failing_checks sees only page 1 and keys the incident on the reader reason
// instead of the check set. Served as an array; the handler answers ?page=.
const pagedFirst = Array.from({ length: 100 }, (_, i) => completed(900100 + i, 'job-' + (i + 1), 'success', SHA_A))
const pagedFailPages = [{ total_count: 101, check_runs: pagedFirst },
                        { total_count: 101, check_runs: [completed(909999, 'late-failure', 'failure', SHA_A)] }]
// A branch that moves mid-pagination (case_baa5bc77): page 1 at the moving ref
// looks like the red-paged head, but by page 2 the branch points at a newer
// commit whose runs are not there yet, so the page comes back short and a
// reader paging at `main` misreads a live red as unknown. The pinned commit
// SHA_M keeps both pages stable, with the failure on page 2.
const pagedFirstMoving = Array.from({ length: 100 }, (_, i) => completed(910100 + i, 'job-' + (i + 1), 'success', SHA_M))
const moveFailPage = { total_count: 101, check_runs: [completed(919999, 'move-failure', 'failure', SHA_M)] }
// A branch that moves BETWEEN the reader's lookup and failing_checks' lookup
// (case_baa5bc77 review): the first answer for `main` is SHA_R1 (red), every later
// one is SHA_R2 (still running, nothing failing). Pre-fix the verdict came
// from R1 and the failing checks from R2, so the key fell back to the reader
// reason; resolved once, both read R1.
const SHA_R1 = 'ddd0000000000000000000000000000000000004'
const SHA_R2 = 'eee0000000000000000000000000000000000005'
const raceCalls = {}
const resolveFailCalls = {}
const raceRed = [completed(930001, 'race-failure', 'failure', SHA_R1), completed(930002, 'lint', 'success', SHA_R1)]
const raceNew = [running(930101, 'race-failure', SHA_R2)]
const CASES = {
  'o/red-punct':   { checks: { total_count: 5, check_runs: failPunct }, statuses: 403, runs: 403 },
  'o/red-same-a':  { checks: { total_count: 3, check_runs: fail2A }, statuses: 403, runs: 403 },
  'o/red-same-b':  { checks: { total_count: 3, check_runs: fail2B }, statuses: 403, runs: 403 },
  'o/red-reorder': { checks: { total_count: 3, check_runs: fail2R }, statuses: 403, runs: 403 },
  'o/red-case':    { checks: { total_count: 3, check_runs: fail2C }, statuses: 403, runs: 403 },
  'o/red-other':   { checks: { total_count: 2, check_runs: failOther }, statuses: 403, runs: 403 },
  'o/red-status-only': { checks: { total_count: 0, check_runs: [] },
                         statuses: { state: 'failure', statuses: [{ context: 'buildkite', state: 'failure' }] },
                         runs: { total_count: 0, workflow_runs: [] } },
  'o/red-status-checks-denied': { checks: 403,
                         statuses: { state: 'failure', statuses: [{ context: 'buildkite', state: 'failure' }] },
                         runs: 403 },
  'o/green':       { checks: { total_count: 2, check_runs: greenRuns }, statuses: 403, runs: 403 },
  'o/denied':      { checks: 403, statuses: 403, runs: 403 },
  'o/empty':       { checks: { total_count: 0, check_runs: [] }, statuses: { state: 'pending', statuses: [] }, runs: { total_count: 0, workflow_runs: [] } },
  'o/pending':     { checks: { total_count: 2, check_runs: [completed('broker suite', 'success', SHA_A), running('Offline suites', SHA_A)] }, statuses: 403, runs: 403 },
  'o/never-ran':   { checks: { total_count: 3, check_runs: neverRanRuns }, statuses: 403, runs: 403 },
  'o/evil-name':   { checks: { total_count: 2, check_runs: evilRuns }, statuses: 403, runs: 403 },
  'o/red-paged':   { checks: pagedFailPages, statuses: 403, runs: { total_count: 0, workflow_runs: [] } },
  // A short first page (case_d7320ba5 review): total_count advertises 200 rows but
  // page 1 serves 100, so failing_checks cannot trust the read and the poller
  // reports UNKNOWN instead of keying on the reader reason (same as toolkit).
  'o/red-paged-short': { checks: [{ total_count: 200, check_runs: [completed(909001, 'early-failure', 'failure', SHA_A)].concat(pagedFirst.slice(0, 99)) }], statuses: 403, runs: { total_count: 0, workflow_runs: [] } },
  // A 200 whose body is not a usable check-runs object (case_d7320ba5 review):
  // no check_runs array, so failing_checks cannot trust the read and the
  // poller reports UNKNOWN instead of keying on the reader reason (toolkit
  // guards on (.check_runs | type) == "array", same here).
  'o/red-bad-body': { checks: { message: 'Server Error' },
                         statuses: { state: 'failure', statuses: [{ context: 'buildkite', state: 'failure' }] },
                         runs: { total_count: 0, workflow_runs: [] } },
  // The check-runs body for o/red-moving is ref-aware (see the handler): the
  // placeholder below is never served; a leak would read as unknown, not red.
  'o/red-moving':  { checks: 'ref-aware-see-handler', statuses: 403, runs: 403 },
  'o/red-race':    { checks: 'ref-aware-see-handler', statuses: 403, runs: 403 },
  'o/red-resolve-fail': { checks: { total_count: 3, check_runs: fail2A }, statuses: 403, runs: 403 },
}

// ---------------------------------------------------------------------------
// The Paperclip API half of the stub: just enough of the issues list and the
// two write routes for the board read and the host wrapper. It emulates the
// behaviours the live API was MEASURED to have on 2026-10-04, because a stub
// that is kinder than the real thing proves nothing:
//   * `per_page` is ignored; a list with no `limit` returns the newest 500
//     rows; `limit` is clamped to 1000;
//   * `q` is a case-insensitive SUBSTRING match over title AND description
//     (the live API matches comments too), so it returns a superset of the
//     cards whose title carries a key;
//   * a missing bearer token is a 401 with an error object.
// The company id in the path picks the scenario, so no state is shared
// between scenarios:
//   co-window   1050 open cards, newest first; the incident card sits at
//               index 800, BEYOND the 500-row default window
//   co-flood    1200 cards whose description matches q: a full page
//   co-down     500 with a body of `[]` (a status that must not be trusted
//               even though the body parses)
//   co-denied   401 with an error object
//   co-shape    200 with an object that is not a list of issues
//   co-wrap*    in-memory board: POSTed cards come back on the next GET
//   co-wrap-fail  same, but every write is a 500
//   rollup      the bounded rollup parent: GET single-issue reads it,
//               GET comments lists its thread. Comment POSTs are refused
//               with the deployed run gate (403
//               cross_issue_influence_run_context_required, live
//               2026-10-05/06): the read-only timer must never attempt
//               this route. No company-wide route is ever needed for it.
//   rollup-fail same parent, same refusal (the name is historical).
const fs = require('fs')
const crypto = require('crypto')
const LOG = process.env.STUB_LOG
const logReq = (o) => { if (LOG) fs.appendFileSync(LOG, JSON.stringify(o) + '\n') }
const sig8 = (names) => crypto.createHash('sha1').update(names.join('\n')).digest('hex').slice(0, 8)
// Independent oracle for the contract key of o/red-same-a: sha1 over the sorted,
// lowercased failing-check names joined by newlines (no trailing newline).
const INCIDENT_TAG = 'red-main:v1:o/red-same-a:' + sig8(['broker suite', 'offline suites'])
const WRAP = {}
let wrapSeq = 0
const noiseCard = (i, desc) => ({ id: 'n-' + i, title: 'Unrelated card ' + i, description: desc || '' })
const windowBoard = () => {
  const rows = []
  for (let i = 0; i < 1050; i++) rows.push(noiseCard(i))
  rows[800] = { id: 'incident-1', title: '[' + INCIDENT_TAG + '] Red main: o/red-same-a', description: 'incident' }
  return rows
}
const floodBoard = () => {
  const rows = []
  for (let i = 0; i < 1200; i++) rows.push(noiseCard(i, 'mentions red-main:v1 in passing'))
  return rows
}
const paperclip = (req, res, url) => {
  const u = new URL(url, 'http://stub')
  const m = u.pathname.match(/^\/api\/companies\/([^/]+)\/issues$/)
  const c = u.pathname.match(/^\/api\/issues\/([^/]+)\/comments$/)
  const s = u.pathname.match(/^\/api\/issues\/([^/]+)$/)
  if (!m && !c && !s) return false
  res.setHeader('Content-Type', 'application/json')
  const auth = req.headers.authorization || ''
  const cid = m ? m[1] : ''
  const iid = s ? s[1] : (c ? c[1] : '')
  const chunks = []
  req.on('data', (d) => chunks.push(d))
  req.on('end', () => {
    const body = Buffer.concat(chunks).toString()
    logReq({ method: req.method, path: u.pathname, query: u.search, auth, body })
    if (!auth.startsWith('Bearer ') || auth.length < 12) { res.statusCode = 401; return res.end(JSON.stringify({ error: 'Unauthorized' })) }
    // Bounded rollup thread: single-issue routes only.
    if (req.method === 'GET' && s) {
      if (iid === 'rollup' || iid === 'rollup-fail') {
        res.statusCode = 200
        return res.end(JSON.stringify({ id: iid, title: 'Red-main findings rollup', status: 'in_progress' }))
      }
      res.statusCode = 404
      return res.end(JSON.stringify({ error: 'Not found' }))
    }
    if (req.method === 'GET' && c) {
      res.statusCode = 200
      return res.end(JSON.stringify([]))
    }
    // Deployed run gate (live 2026-10-05/06): runless agent comment
    // writes are refused before scope is consulted. The read-only timer
    // must never reach this branch; every attempt is logged for the
    // writes() counter and refused.
    if (req.method === 'POST' && c) {
      res.statusCode = 403
      return res.end(JSON.stringify({ error: 'Cross-issue influence requires a heartbeat run', code: 'cross_issue_influence_run_context_required' }))
    }
    if (req.method === 'POST') {
      if (cid === 'co-wrap-fail') { res.statusCode = 500; return res.end(JSON.stringify({ error: 'boom' })) }
      if (m) {
        const card = JSON.parse(body)
        card.id = 'iss-' + (++wrapSeq)
        ;(WRAP[cid] = WRAP[cid] || []).push(card)
        res.statusCode = 201
        return res.end(JSON.stringify({ id: card.id }))
      }
      res.statusCode = 201
      return res.end(JSON.stringify({ id: 'comment-1' }))
    }
    // GET list
    if (cid === 'co-down') { res.statusCode = 500; return res.end('[]') }
    if (cid === 'co-denied') { res.statusCode = 401; return res.end(JSON.stringify({ error: 'Unauthorized' })) }
    if (cid === 'co-shape') { res.statusCode = 200; return res.end(JSON.stringify({ error: 'not a list' })) }
    let rows = cid === 'co-window' ? windowBoard() : cid === 'co-flood' ? floodBoard() : (WRAP[cid] || [])
    const q = (u.searchParams.get('q') || '').toLowerCase()
    if (q) rows = rows.filter((r) => ((r.title || '') + ' ' + (r.description || '')).toLowerCase().includes(q))
    const limit = Math.min(parseInt(u.searchParams.get('limit') || '500', 10) || 500, 1000)
    res.statusCode = 200
    res.end(JSON.stringify(rows.slice(0, limit)))
  })
  return true
}

const srv = http.createServer((req, res) => {
  const url = req.url
  if (paperclip(req, res, url)) return
  res.setHeader('Content-Type', 'application/json')
  const send = (val) => {
    if (val === 403) { res.statusCode = 403; return res.end(JSON.stringify({ message: 'Resource not accessible by integration' })) }
    if (val === 422) { res.statusCode = 422; return res.end(JSON.stringify({ message: 'No commit found' })) }
    res.statusCode = 200
    res.end(JSON.stringify(val))
  }
  // GET /repos/{o}/{r}/commits/main: the commit the reader resolves once per
  // repo. Answered with the canned SHA_A the fixtures already carry, so the
  // reader reads every source at one commit instead of a moving branch name.
  // o/red-moving resolves to its own SHA_M (case_baa5bc77): the poller's
  // failing_checks must resolve the same way and page at the pinned commit.
  const resolve = url.match(/\/repos\/([^/]+\/[^/]+)\/commits\/([^/?]+)(?:\?|$)/)
  if (resolve) {
    if (!CASES[resolve[1]]) { res.statusCode = 404; return res.end(JSON.stringify({ message: 'Not Found' })) }
    if (resolve[1] === 'o/red-moving') return send({ sha: SHA_M })
    // o/red-resolve-fail (case_baa5bc77 review): the poller's own first resolve of
    // `main` answers 502 once; every later resolve succeeds, so only a
    // fail-closed poller reports UNKNOWN while a fail-open one keys the entry
    // on the reader reason.
    if (resolve[1] === 'o/red-resolve-fail') { resolveFailCalls[resolve[1]] = (resolveFailCalls[resolve[1]] || 0) + 1; if (resolveFailCalls[resolve[1]] === 1) { res.statusCode = 502; return res.end(JSON.stringify({ message: 'Bad Gateway' })) } return send({ sha: SHA_A }) }
    if (resolve[1] === 'o/red-race') { raceCalls[resolve[1]] = (raceCalls[resolve[1]] || 0) + 1; return send({ sha: raceCalls[resolve[1]] === 1 ? SHA_R1 : SHA_R2 }) }
    return send({ sha: SHA_A })
  }
  let m = url.match(/\/repos\/([^/]+\/[^/]+)\/commits\/([^/]+)\/check-runs/)
  if (m) {
    const spec = CASES[m[1]]
    if (!spec) { res.statusCode = 404; return res.end(JSON.stringify({ message: 'Not Found' })) }
    if (m[1] === 'o/red-race') {
      res.statusCode = 200
      return res.end(JSON.stringify(m[2] === SHA_R1 ? { total_count: 2, check_runs: raceRed } : { total_count: 1, check_runs: raceNew }))
    }
    // Ref-aware body for o/red-moving (case_baa5bc77): at the pinned SHA_M both
    // pages are stable with the failure on page 2; at the moving `main` ref
    // page 1 still claims 101 rows but page 2 finds the branch moved on and
    // comes back short, which is exactly what made the pre-fix failing_checks
    // fall back to the reader reason and rotate the incident key.
    if (m[1] === 'o/red-moving') {
      const pm = (url.match(/[?&]page=(\d+)/) || [])[1] || '1'
      if (m[2] === SHA_M) {
        res.statusCode = 200
        return res.end(JSON.stringify(pm === '1' ? { total_count: 101, check_runs: pagedFirstMoving } : moveFailPage))
      }
      if (pm === '1') {
        res.statusCode = 200
        return res.end(JSON.stringify({ total_count: 101, check_runs: pagedFirstMoving }))
      }
      res.statusCode = 200
      return res.end(JSON.stringify({ total_count: 101, check_runs: [] }))
    }
    // Paged check-runs bodies are arrays: element 0 is page 1. A caller that
    // never sends page= gets element 0, which is what made the pre-fix
    // failing_checks miss a page-2 failure.
    if (Array.isArray(spec.checks)) {
      const pm = (url.match(/[?&]page=(\d+)/) || [])[1]
      const body = spec.checks[pm ? parseInt(pm, 10) - 1 : 0]
      if (body === undefined) { res.statusCode = 404; return res.end(JSON.stringify({ message: 'Not Found' })) }
      res.statusCode = 200
      return res.end(JSON.stringify(body))
    }
    return send(spec.checks)
  }
  m = url.match(/\/repos\/([^/]+\/[^/]+)\/commits\/([^/]+)\/status/)
  if (m) {
    const spec = CASES[m[1]]
    if (!spec) { res.statusCode = 404; return res.end(JSON.stringify({ message: 'Not Found' })) }
    return send(spec.statuses)
  }
  m = url.match(/\/repos\/([^/]+\/[^/]+)\/check-runs\/([^/]+)\/annotations/)
  if (m) {
    // Only o/never-ran's literal ids (890001/890002) carry the billing text:
    // every other repo's short failures probe as genuine reds. Literal ids
    // are what make this keying sound — random ids would unmoor it.
    if (['890001', '890002'].includes(m[2])) return send(annBilling)
    return send([{ annotation_level: 'failure', message: 'Process completed with exit code 1.' }])
  }
  m = url.match(/\/repos\/([^/]+\/[^/]+)\/actions\/runs/)
  if (m) {
    const repo = url.match(/head_sha=([^&]+)/) ? m[1] : m[1]
    const spec = CASES[repo]
    if (!spec) { res.statusCode = 404; return res.end(JSON.stringify({ message: 'Not Found' })) }
    return send(spec.runs)
  }
  res.statusCode = 404
  res.end(JSON.stringify({ message: 'Not Found: ' + url }))
})
srv.listen(0, '127.0.0.1', () => process.stdout.write(String(srv.address().port) + '\n'))
STUB

# The stub appends one JSON line per Paperclip API request here; it reads the
# path at start-up, so it is exported BEFORE the stub launches.
export STUB_LOG="$WORK/api.log"
: > "$STUB_LOG"
node "$WORK/stub-api.js" > "$WORK/port.txt" 2>"$WORK/stub.err" &
STUB_PID=$!
for _ in $(seq 1 50); do [[ -s "$WORK/port.txt" ]] && break; sleep 0.1; done
PORT="$(tr -d '\n' < "$WORK/port.txt")"
[[ -n "$PORT" ]] || { echo "test_red_main_poll: stub API did not start"; cat "$WORK/stub.err"; exit 2; }
export GH_API_URL="http://127.0.0.1:$PORT"
export GH_TOKEN="not-a-real-token"


# Board seam: titles arrive through INCIDENT_SOURCE_CMD as a JSON array.
TITLES="$WORK/titles.json"
echo '[]' > "$TITLES"
export INCIDENT_SOURCE_CMD="cat $TITLES"

cat > "$WORK/failing-ci-reader.sh" <<'READER'
#!/usr/bin/env bash
printf '%s\n' '{"verdict":"fail","reason":"measured red"}'
exit 1
READER
chmod +x "$WORK/failing-ci-reader.sh"

start_hang_server() {
  cat > "$WORK/hang-server.py" <<'PY'
import socket
import sys
import time

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 0))
server.listen(1)
print(server.getsockname()[1], flush=True)
connection, _ = server.accept()
with connection:
    with open(sys.argv[1], "w") as accepted:
        accepted.write("yes\n")
    time.sleep(60)
PY
  : > "$WORK/hang-port.txt"
  : > "$WORK/hang-accepted"
  python3 "$WORK/hang-server.py" "$WORK/hang-accepted" > "$WORK/hang-port.txt" 2> "$WORK/hang-server.err" &
  HANG_PID=$!
  for _ in $(seq 1 50); do [[ -s "$WORK/hang-port.txt" ]] && break; sleep 0.05; done
  HANG_PORT="$(tr -d '\n' < "$WORK/hang-port.txt")"
  [[ -n "$HANG_PORT" ]] || return 1
  HANG_URL="http://127.0.0.1:$HANG_PORT"
}
stop_hang_server() {
  [[ -n "${HANG_PID:-}" ]] || return 0
  kill "$HANG_PID" 2>/dev/null || true
  wait "$HANG_PID" 2>/dev/null || true
  HANG_PID=
}

# snapshot <repos...> -> sets OUT (fragment), RC.
snapshot() { OUT="$("$TOOL" snapshot "$@" 2>"$WORK/err.txt")"; RC=$?; ERR="$(cat "$WORK/err.txt")"; return 0; }
# propose <repos...> -> sets OUT (drafts), RC.
propose() { OUT="$("$TOOL" propose "$@" 2>"$WORK/err.txt")"; RC=$?; ERR="$(cat "$WORK/err.txt")"; return 0; }
sig_of() { printf '%s' "$OUT" | jq -r --arg r "$1" '.redMains[] | select(.repo==$r) | .signature'; }
tag_of() { printf '%s' "$OUT" | jq -r --arg r "$1" '.redMains[] | select(.repo==$r) | .dedupeTag'; }

hdr "Red entries carry the contract key format"
snapshot o/red-same-a
[[ "$RC" -eq 1 ]] && ok "red snapshot exits 1" || bad "red snapshot exits 1" "got exit=$RC"
TAG="$(tag_of o/red-same-a)"
[[ "$TAG" == red-main:v1:o/red-same-a:* ]] && ok "dedupe tag matches contract format red-main:v1:{owner}/{repo}:{sig8}" || bad "dedupe tag matches contract format red-main:v1:{owner}/{repo}:{sig8}" "got $TAG"
SIG="${TAG##*:}"
[[ "$SIG" =~ ^[0-9a-f]{8}$ ]] && ok "sig8 is 8 lowercase hex chars" || bad "sig8 is 8 lowercase hex chars" "got $SIG"
printf '%s' "$OUT" | jq -e '.redMains[0].incidentExists == false' >/dev/null 2>&1 \
  && ok "untracked red asserts incidentExists:false" \
  || bad "untracked red asserts incidentExists:false" "$OUT"
printf '%s' "$OUT" | jq -e '.redMains[0].suggestedSeverity == "S2"' >/dev/null 2>&1 \
  && ok "non-prod-path red suggests S2" \
  || bad "non-prod-path red suggests S2" "$OUT"

hdr "One card per repo plus signature, not per commit (rule 1)"
snapshot o/red-same-a o/red-same-b
SA="$(sig_of o/red-same-a)"; SB="$(sig_of o/red-same-b)"
[[ -n "$SA" && "$SA" == "$SB" ]] && ok "same failing set at different heads hashes to one signature" || bad "same failing set at different heads hashes to one signature" "a=$SA b=$SB"
[[ "$RC" -eq 1 ]] && ok "two same-signature reds still exit 1" || bad "two same-signature reds still exit 1" "got $RC"
HA="$(printf '%s' "$OUT" | jq -r '.redMains[] | select(.repo=="o/red-same-a") | .headSha')"
HB="$(printf '%s' "$OUT" | jq -r '.redMains[] | select(.repo=="o/red-same-b") | .headSha')"
[[ "$HA" != "$HB" && -n "$HA" && -n "$HB" ]] && ok "heads differ while the signature is shared (SHA is evidence, not key)" || bad "heads differ while the signature is shared (SHA is evidence, not key)" "a=$HA b=$HB"
snapshot o/red-same-a o/red-other
SA="$(sig_of o/red-same-a)"; SO="$(sig_of o/red-other)"
[[ -n "$SA" && -n "$SO" && "$SA" != "$SO" ]] && ok "a different failing set hashes to a different signature" || bad "a different failing set hashes to a different signature" "same=$SA other=$SO"

hdr "Ordering and case do not rotate the signature (rule 2, contract lowercasing)"
snapshot o/red-same-a o/red-reorder o/red-case
SA="$(sig_of o/red-same-a)"; SR="$(sig_of o/red-reorder)"; SC="$(sig_of o/red-case)"
[[ "$SA" == "$SR" ]] && ok "reversed run order keeps the signature" || bad "reversed run order keeps the signature" "a=$SA r=$SR"
[[ "$SA" == "$SC" ]] && ok "capitalised job name keeps the signature" || bad "capitalised job name keeps the signature" "a=$SA c=$SC"

hdr "Repeat polls while tracked stay silent (the DONE acceptance)"
snapshot o/red-same-a
TAG="$(tag_of o/red-same-a)"
printf '[{"title": "[%s] Red main: o/red-same-a"}]' "$TAG" > "$TITLES"
snapshot o/red-same-a
printf '%s' "$OUT" | jq -e '.redMains[0].incidentExists == true' >/dev/null 2>&1 \
  && ok "seeded tag asserts incidentExists:true" \
  || bad "seeded tag asserts incidentExists:true" "$OUT"
[[ "$RC" -eq 1 ]] && ok "tracked red still exits 1 (red is reported, not hidden)" || bad "tracked red still exits 1 (red is reported, not hidden)" "got $RC"
propose o/red-same-a
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "propose drafts nothing for a tracked incident (exit 0, empty)" || bad "propose drafts nothing for a tracked incident (exit 0, empty)" "exit=$RC out=$OUT"
echo '[]' > "$TITLES"
propose o/red-same-a
[[ "$RC" -eq 1 ]] && ok "propose drafts the untracked incident (exit 1)" || bad "propose drafts the untracked incident (exit 1)" "exit=$RC"
grep -qF "[$TAG]" <<<"$OUT" \
  && ok "draft carries the contract tag for coalescing" \
  || bad "draft carries the contract tag for coalescing" "$OUT"
grep -qi "triage" <<<"$OUT" \
  && ok "draft names the triage route" \
  || bad "draft names the triage route" "$OUT"

hdr "Green stays silent"
snapshot o/green
[[ "$RC" -eq 0 ]] && ok "green exits 0" || bad "green exits 0" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
  && ok "green emits an empty redMains list" \
  || bad "green emits an empty redMains list" "$OUT"
propose o/green
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "propose on green is silent (exit 0, empty)" || bad "propose on green is silent (exit 0, empty)" "exit=$RC out=$OUT"

hdr "Unobservable CI is unknown, never green (rule 4)"
for repo in o/denied o/empty o/pending; do
  snapshot "$repo"
  [[ "$RC" -eq 3 ]] && ok "$repo exits 3" || bad "$repo exits 3" "got $RC"
  printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
    && ok "$repo emits no red entry" \
    || bad "$repo emits no red entry" "$OUT"
  if [[ -n "$ERR" ]]; then ok "blind spot reported on stderr for $repo"; else bad "blind spot reported on stderr for $repo" "empty stderr"; fi
done
snapshot o/never-ran
if [[ "$RC" -eq 3 ]]; then ok "never-ran exits 3, not 1"; else bad "never-ran exits 3, not 1" "got $RC"; fi
printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
  && ok "never-ran drafts no repo-lead incident (billing block is not a code red)" \
  || bad "never-ran drafts no repo-lead incident (billing block is not a code red)" "$OUT"
[[ "$ERR" == *"not started"* ]] \
  && ok "never-ran says not-started on stderr" \
  || bad "never-ran says not-started on stderr" "$ERR"
snapshot o/green o/denied
[[ "$RC" -eq 3 ]] && ok "green plus one blind repo exits 3 (incompleteness dominates)" || bad "green plus one blind repo exits 3 (incompleteness dominates)" "got $RC"
snapshot o/red-same-a o/denied
[[ "$RC" -eq 3 ]] && ok "red plus one blind repo exits 3 (incompleteness dominates the red)" || bad "red plus one blind repo exits 3 (incompleteness dominates the red)" "got $RC"
printf '%s' "$OUT" | jq -e '(.redMains | length) == 1' >/dev/null 2>&1 \
  && ok "the observed red is still reported under partial coverage" \
  || bad "the observed red is still reported under partial coverage" "$OUT"

hdr "A blind board is not 'no incident' (rule 3)"
export INCIDENT_SOURCE_CMD="false"
snapshot o/red-same-a; [[ "$RC" -eq 3 ]] && ok "snapshot exits 3 when the board is unreadable" || bad "snapshot exits 3 when the board is unreadable" "got $RC"
[[ -z "$OUT" ]] && ok "snapshot prints no fragment when the board is unreadable" || bad "snapshot prints no fragment when the board is unreadable" "$OUT"
propose o/red-same-a; [[ "$RC" -eq 3 ]] && ok "propose exits 3 when the board is unreadable" || bad "propose exits 3 when the board is unreadable" "got $RC"
[[ -z "$OUT" ]] && ok "propose drafts nothing when the board is unreadable" || bad "propose drafts nothing when the board is unreadable" "$OUT"
export INCIDENT_SOURCE_CMD="cat $TITLES"

hdr "Fail without check runs keys on the reader reason, not the empty set"
snapshot o/red-status-only
[[ "$RC" -eq 1 ]] && ok "status-sourced red exits 1" || bad "status-sourced red exits 1" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains[0].sigSource == "reason"' >/dev/null 2>&1 \
  && ok "status-sourced red records sigSource:reason" \
  || bad "status-sourced red records sigSource:reason" "$OUT"
printf '%s' "$OUT" | jq -e '(.redMains[0].signature | length) == 8' >/dev/null 2>&1 \
  && ok "reason-keyed signature is still 8 hex chars" \
  || bad "reason-keyed signature is still 8 hex chars" "$OUT"

hdr "A denied check-runs read keeps the reader reason, never hides the red"
snapshot o/red-status-checks-denied
[[ "$RC" -eq 1 ]] && ok "status red with denied check runs exits 1" || bad "status red with denied check runs exits 1" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains[0].sigSource == "reason"' >/dev/null 2>&1 \
  && ok "denied check runs record sigSource:reason" \
  || bad "denied check runs record sigSource:reason" "$OUT"
printf '%s' "$OUT" | jq -e '(.redMains[0].failingJobs | length) > 0' >/dev/null 2>&1 \
  && ok "denied check runs key on the reader reason, not the empty set" \
  || bad "denied check runs key on the reader reason, not the empty set" "$OUT"

hdr "A failure past page 1 keys on the check set, not the reason"
snapshot o/red-paged
[[ "$RC" -eq 1 ]] && ok "paged red exits 1" || bad "paged red exits 1" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains[0].sigSource == "checks"' >/dev/null 2>&1 \
  && ok "paged red records sigSource:checks" \
  || bad "paged red records sigSource:checks" "$OUT"
printf '%s' "$OUT" | jq -e '.redMains[0].failingJobs == "late-failure"' >/dev/null 2>&1 \
  && ok "paged red names the page-2 failure" \
  || bad "paged red names the page-2 failure" "$OUT"

hdr "A short check-runs page is unknown, never a reason-keyed entry"
snapshot o/red-paged-short
[[ "$RC" -eq 3 ]] && ok "short-paged snapshot exits 3" || bad "short-paged snapshot exits 3" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
  && ok "short-paged snapshot emits no red entry" \
  || bad "short-paged snapshot emits no red entry" "$OUT"
[[ "$ERR" == *"check-runs could not be read"* ]] \
  && ok "short-paged snapshot names the blind spot on stderr" \
  || bad "short-paged snapshot names the blind spot on stderr" "$ERR"

hdr "A bad check-runs body is unknown, never a reason-keyed entry"
snapshot o/red-bad-body
[[ "$RC" -eq 3 ]] && ok "bad-body snapshot exits 3" || bad "bad-body snapshot exits 3" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
  && ok "bad-body snapshot emits no red entry" \
  || bad "bad-body snapshot emits no red entry" "$OUT"
[[ "$ERR" == *"check-runs could not be read"* ]] \
  && ok "bad-body snapshot names the blind spot on stderr" \
  || bad "bad-body snapshot names the blind spot on stderr" "$ERR"

hdr "A branch that moves mid-pagination is read at the pinned commit (case_baa5bc77)"
snapshot o/red-moving
[[ "$RC" -eq 1 ]] && ok "moving-branch red is reported (exit 1)" || bad "moving-branch red is reported (exit 1)" "got $RC err=$ERR"
printf '%s' "$OUT" | jq -e '.redMains[0].sigSource == "checks"' >/dev/null 2>&1 \
  && ok "moving-branch red records sigSource:checks" \
  || bad "moving-branch red records sigSource:checks" "$OUT"
printf '%s' "$OUT" | jq -e '.redMains[0].failingJobs == "move-failure"' >/dev/null 2>&1 \
  && ok "moving-branch red names the page-2 failure at the pinned commit" \
  || bad "moving-branch red names the page-2 failure at the pinned commit" "$OUT"
printf '%s' "$OUT" | jq -e '.redMains[0].headSha == "ccc0000000000000000000000000000000000003"' >/dev/null 2>&1 \
  && ok "moving-branch red reports the pinned commit, not the moving ref" \
  || bad "moving-branch red reports the pinned commit, not the moving ref" "$OUT"

hdr "A branch that moves between the reader's lookup and the failing-check read keeps one commit (case_baa5bc77)"
snapshot o/red-race
[[ "$RC" -eq 1 ]] && ok "racing-branch red is reported (exit 1)" || bad "racing-branch red is reported (exit 1)" "got $RC err=$ERR"
printf '%s' "$OUT" | jq -e '.redMains[0].sigSource == "checks" and .redMains[0].failingJobs == "race-failure"' >/dev/null 2>&1 \
  && ok "racing-branch red keys on the check set read at the verdict's commit" \
  || bad "racing-branch red keys on the check set read at the verdict's commit" "$OUT"
printf '%s' "$OUT" | jq -e '.redMains[0].headSha == "ddd0000000000000000000000000000000000004"' >/dev/null 2>&1 \
  && ok "racing-branch red reports the commit the verdict was read at" \
  || bad "racing-branch red reports the commit the verdict was read at" "$OUT"

hdr "A main that does not resolve is unknown, never a reason-keyed entry (case_baa5bc77)"
snapshot o/red-resolve-fail
[[ "$RC" -eq 3 ]] && ok "unresolvable main exits 3" || bad "unresolvable main exits 3" "got $RC err=$ERR"
printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
  && ok "unresolvable main emits no red entry" \
  || bad "unresolvable main emits no red entry" "$OUT"
[[ "$ERR" == *"could not resolve main"* ]] \
  && ok "unresolvable main names the blind spot on stderr" \
  || bad "unresolvable main names the blind spot on stderr" "$ERR"

hdr "Attacker-influenced names cannot break the key (sentinel job name)"
snapshot o/evil-name
[[ "$RC" -eq 1 ]] && ok "evil-named red still exits 1" || bad "evil-named red still exits 1" "got $RC"
printf '%s' "$OUT" | jq -e '.redMains[0].headSha == "aaa0000000000000000000000000000000000001"' >/dev/null 2>&1 \
  && ok "head SHA comes from the body, not from a job name" \
  || bad "head SHA comes from the body, not from a job name" "$OUT"

hdr "Usage refuses loudly (exit 2)"
"$TOOL" snapshot >/dev/null 2>&1; [[ "$?" -eq 2 ]] && ok "snapshot with no repos exits 2" || bad "snapshot with no repos exits 2" "got $?"
"$TOOL" snapshot 'o/../evil' >/dev/null 2>&1; [[ "$?" -eq 2 ]] && ok "path-traversal repo is refused" || bad "path-traversal repo is refused" "got $?"
GH_TOKEN= "$TOOL" snapshot o/green >/dev/null 2>&1; [[ "$?" -eq 2 ]] && ok "missing GH_TOKEN exits 2" || bad "missing GH_TOKEN exits 2" "got $?"
GH_CI_STATUS_BIN=/nonexistent "$TOOL" snapshot o/green >/dev/null 2>&1; [[ "$?" -eq 2 ]] && ok "missing reader binary exits 2" || bad "missing reader binary exits 2" "got $?"

hdr "No credential on argv"
printf 'GH_TOKEN=canary-token-xyz slice-test' > "$WORK/probe.txt"
CANARY_OUT="$(GH_TOKEN=canary-token-xyz GH_API_URL="$GH_API_URL" INCIDENT_SOURCE_CMD="cat $TITLES" "$TOOL" snapshot o/green 2>/dev/null || true)"
[[ "$CANARY_OUT" != *canary-token-xyz* ]] || bad "token never appears on stdout" "$CANARY_OUT"
ok "token never appears on stdout"


# ---------------------------------------------------------------------------
# The LIVE board read (no INCIDENT_SOURCE_CMD): the stub emulates the measured
# behaviour of the real issues API, so these cases cannot be kinder than prod.
# ---------------------------------------------------------------------------
STUB_BASE="http://127.0.0.1:$PORT"
# The allowlist test hatch: every live-read base in this suite is the
# loopback stub, so opt in once and let the poll assertions see the tool,
# not the gate.
export PAPERCLIP_API_ALLOWLIST_TEST="1"
board_snapshot() { # <company> <repos...> -> OUT/RC/ERR through the live read path
  local co="$1"; shift
  OUT="$(env -u INCIDENT_SOURCE_CMD PAPERCLIP_API_URL="$STUB_BASE" PAPERCLIP_COMPANY_ID="$co" RED_MAIN_API_KEY=canary-board-key-000 "$TOOL" snapshot "$@" 2>"$WORK/err.txt")"; RC=$?; ERR="$(cat "$WORK/err.txt")"
  return 0
}
board_propose() {
  local co="$1"; shift
  OUT="$(env -u INCIDENT_SOURCE_CMD PAPERCLIP_API_URL="$STUB_BASE" PAPERCLIP_COMPANY_ID="$co" RED_MAIN_API_KEY=canary-board-key-000 "$TOOL" propose "$@" 2>"$WORK/err.txt")"; RC=$?; ERR="$(cat "$WORK/err.txt")"
  return 0
}

hdr "A quiet incident card past the default board window is still found (rule 3b)"
# Positive control: the stub really does hide the card from an unfiltered list,
# so a green below is the query's doing, not the fixture's.
CTRL_N="$(curl -s -H 'Authorization: Bearer canary-board-key-000' "$STUB_BASE/api/companies/co-window/issues?per_page=100" | jq 'length')"
CTRL_HIT="$(curl -s -H 'Authorization: Bearer canary-board-key-000' "$STUB_BASE/api/companies/co-window/issues?per_page=100" | jq '[.[] | select(.title | startswith("[red-main:v1:"))] | length')"
[[ "$CTRL_N" == "500" && "$CTRL_HIT" == "0" ]] && ok "control: an unfiltered list is a 500-row window that does not hold the incident card" || bad "control: an unfiltered list is a 500-row window that does not hold the incident card" "n=$CTRL_N hit=$CTRL_HIT"
: > "$STUB_LOG"
board_snapshot co-window o/red-same-a
printf '%s' "$OUT" | jq -e '.redMains[0].incidentExists == true' >/dev/null 2>&1 \
  && ok "live read finds the incident card at row 800 of 1050 (incidentExists:true)" \
  || bad "live read finds the incident card at row 800 of 1050 (incidentExists:true)" "rc=$RC out=$OUT err=$ERR"
[[ "$RC" -eq 1 ]] && ok "tracked red on the live read still exits 1" || bad "tracked red on the live read still exits 1" "got $RC"
board_propose co-window o/red-same-a
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "propose drafts nothing: the live read saw the open card (no duplicate)" || bad "propose drafts nothing: the live read saw the open card (no duplicate)" "exit=$RC out=$OUT"
REQ_Q="$(jq -rs '[.[] | select(.method=="GET") | .query] | first // ""' "$STUB_LOG")"
[[ "$REQ_Q" == *"q=red-main:v1"* ]] && ok "board read filters server-side with q=red-main:v1" || bad "board read filters server-side with q=red-main:v1" "query=$REQ_Q"
[[ "$REQ_Q" == *"limit=1000"* ]] && ok "board read asks for limit=1000 (the hard cap)" || bad "board read asks for limit=1000 (the hard cap)" "query=$REQ_Q"
[[ "$REQ_Q" == *"backlog"* && "$REQ_Q" == *"blocked"* && "$REQ_Q" == *"in_review"* ]] && ok "board read covers every non-terminal status, backlog included" || bad "board read covers every non-terminal status, backlog included" "query=$REQ_Q"
AUTH="$(jq -rs '[.[] | select(.method=="GET") | .auth] | first // ""' "$STUB_LOG")"
[[ "$AUTH" == "Bearer canary-board-key-000" ]] && ok "board key reaches the API as a bearer token" || bad "board key reaches the API as a bearer token" "auth=$AUTH"
[[ "$OUT$ERR" != *canary-board-key-000* ]] && ok "board key never appears on stdout or stderr" || bad "board key never appears on stdout or stderr" "$OUT $ERR"

hdr "An unreadable or possibly-truncated board is unknown, never 'no incident' (rule 3b)"
board_snapshot co-flood o/red-same-a
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "a FULL page (1000 rows) is treated as truncated: exit 3, no fragment" || bad "a FULL page (1000 rows) is treated as truncated: exit 3, no fragment" "rc=$RC out=$OUT"
[[ "$ERR" == *"truncated"* ]] && ok "truncation is named on stderr" || bad "truncation is named on stderr" "$ERR"
board_propose co-flood o/red-same-a
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "propose drafts nothing against a possibly-truncated board" || bad "propose drafts nothing against a possibly-truncated board" "rc=$RC out=$OUT"
board_snapshot co-down o/red-same-a
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "HTTP 500 with a parseable [] body is unreadable, not empty" || bad "HTTP 500 with a parseable [] body is unreadable, not empty" "rc=$RC out=$OUT"
board_snapshot co-denied o/red-same-a
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "HTTP 401 is unreadable, not 'no incident'" || bad "HTTP 401 is unreadable, not 'no incident'" "rc=$RC out=$OUT"
board_snapshot co-shape o/red-same-a
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "a 200 whose body is not a list of issues is unreadable" || bad "a 200 whose body is not a list of issues is unreadable" "rc=$RC out=$OUT"
OUT="$(env INCIDENT_SOURCE_CMD=true "$TOOL" snapshot o/red-same-a 2>/dev/null)"; RC=$?
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "an empty board body is unreadable, not 'no incident'" || bad "an empty board body is unreadable, not 'no incident'" "rc=$RC out=$OUT"
OUT="$(env INCIDENT_SOURCE_CMD='echo {"issues":[{"title":"x"}]}' "$TOOL" snapshot o/green 2>/dev/null)"; RC=$?
[[ "$RC" -eq 0 ]] && ok "a wrapped {issues:[...]} body is still read" || bad "a wrapped {issues:[...]} body is still read" "rc=$RC out=$OUT"
OUT="$(env -u INCIDENT_SOURCE_CMD PAPERCLIP_API_URL="$STUB_BASE" PAPERCLIP_COMPANY_ID=co-window RED_MAIN_API_KEY= PAPERCLIP_API_KEY= "$TOOL" snapshot o/red-same-a 2>/dev/null)"; RC=$?
[[ "$RC" -eq 3 && -z "$OUT" ]] && ok "no board credential is unknown, not 'no incident'" || bad "no board credential is unknown, not 'no incident'" "rc=$RC out=$OUT"

hdr "Curl timeouts fail closed on a GitHub connection that never answers"
if start_hang_server; then
  start=$SECONDS
  OUT="$(timeout 4s env GH_API_URL="$HANG_URL" \
    GH_CI_STATUS_BIN="$WORK/failing-ci-reader.sh" INCIDENT_SOURCE_CMD="cat $TITLES" \
    RED_MAIN_POLL_CURL_CONNECT_TIMEOUT=1 RED_MAIN_POLL_CURL_MAX_TIME=1 \
    "$TOOL" snapshot o/red-timeout 2>"$WORK/err.txt")"
  RC=$?; elapsed=$((SECONDS - start)); ERR="$(cat "$WORK/err.txt")"
  [[ "$RC" -eq 3 ]] && ok "GitHub timeout exits 3 (unmeasurable), not a reason-keyed red" || bad "GitHub timeout exits 3 (unmeasurable), not a reason-keyed red" "rc=$RC out=$OUT err=$ERR"
  printf '%s' "$OUT" | jq -e '.redMains == []' >/dev/null 2>&1 \
    && ok "GitHub timeout emits no red entry" || bad "GitHub timeout emits no red entry" "$OUT"
  # The pin resolves first: a hung GitHub fails the resolve, not the
  # check-runs read (case_baa5bc77 pin architecture). Either way it is UNKNOWN.
  [[ "$ERR" == *"could not resolve main to a commit"* ]] \
    && ok "a hung GitHub resolve is named as unknown" || bad "a hung GitHub resolve is named as unknown" "$ERR"
  [[ -s "$WORK/hang-accepted" ]] && ok "the loopback server accepted the curl connection" || bad "the loopback server accepted the curl connection"
  [[ "$elapsed" -le 3 ]] && ok "the accepted loopback connection is bounded to a few seconds" || bad "the accepted loopback connection is bounded to a few seconds" "elapsed=${elapsed}s"
  [[ "$OUT$ERR" != *not-a-real-token* ]] \
    && ok "the GitHub token stays out of output" || bad "the GitHub token stays out of output"
else
  bad "the GitHub loopback hang server starts" "$(cat "$WORK/hang-server.err")"
fi
stop_hang_server

hdr "A hung CI reader run is bounded by RED_MAIN_POLL_READER_TIMEOUT"
# A reader that never returns (a hung gh_ci_status.sh run against a wedged
# endpoint). GitHub itself answers here via the stub so the pin resolves and
# the reader run is what hangs: the timeout wrapper must cut it off.
cat > "$WORK/hanging-ci-reader.sh" <<'READER'
#!/usr/bin/env bash
sleep 60
READER
chmod +x "$WORK/hanging-ci-reader.sh"
start=$SECONDS
OUT="$(timeout 10s env GH_TOKEN=canary-reader-token-000 \
  GH_CI_STATUS_BIN="$WORK/hanging-ci-reader.sh" INCIDENT_SOURCE_CMD="cat $TITLES" \
  RED_MAIN_POLL_CURL_CONNECT_TIMEOUT=1 RED_MAIN_POLL_CURL_MAX_TIME=2 \
  RED_MAIN_POLL_READER_TIMEOUT=2 \
  "$TOOL" snapshot o/red-same-a 2>"$WORK/err.txt")"
RC=$?; elapsed=$((SECONDS - start)); ERR="$(cat "$WORK/err.txt")"
[[ "$RC" -eq 3 ]] && ok "a hung CI reader exits 3 (unmeasurable), never a clean pass" || bad "a hung CI reader exits 3 (unmeasurable), never a clean pass" "rc=$RC out=$OUT err=$ERR"
[[ "$ERR" == *"unreadable CI verdict"* ]] \
  && ok "a timed-out reader is named as unknown" || bad "a timed-out reader is named as unknown" "$ERR"
[[ "$elapsed" -le 6 ]] && ok "the reader run is cut off within a few seconds" || bad "the reader run is cut off within a few seconds" "elapsed=${elapsed}s"
[[ "$OUT$ERR" != *canary-reader-token-000* ]] \
  && ok "the reader token stays out of output" || bad "the reader token stays out of output"

hdr "Curl timeouts fail closed on a board connection that never answers"
if start_hang_server; then
  start=$SECONDS
  OUT="$(timeout 4s env -u INCIDENT_SOURCE_CMD PAPERCLIP_API_URL="$HANG_URL" \
    PAPERCLIP_COMPANY_ID=co-timeout RED_MAIN_API_KEY=canary-board-key-000 \
    RED_MAIN_POLL_CURL_CONNECT_TIMEOUT=1 RED_MAIN_POLL_CURL_MAX_TIME=1 \
    "$TOOL" snapshot o/red-same-a 2>"$WORK/err.txt")"
  RC=$?; elapsed=$((SECONDS - start)); ERR="$(cat "$WORK/err.txt")"
  [[ "$RC" -eq 3 && -z "$OUT" ]] && ok "board timeout exits 3 with no snapshot" || bad "board timeout exits 3 with no snapshot" "rc=$RC out=$OUT err=$ERR"
  [[ -s "$WORK/hang-accepted" ]] && ok "the loopback server accepted the curl connection" || bad "the loopback server accepted the curl connection"
  [[ "$elapsed" -le 3 ]] && ok "the accepted loopback connection is bounded to a few seconds" || bad "the accepted loopback connection is bounded to a few seconds" "elapsed=${elapsed}s"
  [[ "$OUT$ERR" != *canary-board-key-000* ]] \
    && ok "the board key stays out of output" || bad "the board key stays out of output"
else
  bad "the board loopback hang server starts" "$(cat "$WORK/hang-server.err")"
fi
stop_hang_server
[[ "$(grep -cF -- 'curl --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME"' "$TOOL")" -eq 2 ]] \
  && ok "both red-main curl call sites carry connect and total bounds" \
  || bad "both red-main curl call sites carry connect and total bounds"

hdr "Signature order is bytes, not the locale"
# Names that a locale-aware sort would reorder (punctuation is ignored by
# en_US collation): the signature must equal sha1 over the C-byte-ordered set.
snapshot o/red-punct
SP="$(sig_of o/red-punct)"
WANT="$(printf 'lint a\nlint-a\nlint.c\nlint_b' | sha1sum | cut -c1-8)"
[[ "$SP" == "$WANT" ]] && ok "signature is sha1 over byte-ordered names (independent oracle)" || bad "signature is sha1 over byte-ordered names (independent oracle)" "got=$SP want=$WANT"
OUT="$(LC_ALL=en_US.UTF-8 "$TOOL" snapshot o/red-punct 2>/dev/null)"
[[ "$(sig_of o/red-punct)" == "$WANT" ]] && ok "the same signature under a non-C locale" || bad "the same signature under a non-C locale" "got=$(sig_of o/red-punct)"

# ---------------------------------------------------------------------------
# The host wrapper: what it records, and how often.
#
# The wrapper runs the poller and reader from TOOLKIT_DIR. Here that is a
# staged fake: a shim poller that records the environment the wrapper
# exported and then execs the REAL poller above (so exit codes and draft
# text stay production), plus a symlink to the real CI reader. There is
# deliberately NO writer double: the read-only timer must not source any
# writer, so a poison file stands where one would be — sourcing it aborts
# the wrapper with a named marker. Deployed, TOOLKIT_DIR is the pinned
# public toolkit clone (INSTALL.md §3), whose poller additionally never
# touches the company list -- proven by its own suite, not here.
# ---------------------------------------------------------------------------
WRAPPER="$HERE/ops/case_e77a1e99/red_main_poll_check.sh"
[[ -x "$WRAPPER" ]] || { echo "test_red_main_poll: wrapper missing at $WRAPPER" >&2; exit 2; }
FAKETOOLKIT="$WORK/faketoolkit"
mkdir -p "$FAKETOOLKIT"
ln -sf "$HERE/gh_ci_status.sh" "$FAKETOOLKIT/gh_ci_status.sh"
cat > "$FAKETOOLKIT/red_main_poll.sh" <<'SHIM'
#!/usr/bin/env bash
printf 'ROLLUP=%s BIN=%s\n' "${RED_MAIN_ROLLUP_PARENT_ID:-}" "${GH_CI_STATUS_BIN:-}" >> "$WRAP_ENV_LOG"
exec "$REAL_POLLER" "$@"
SHIM
chmod +x "$FAKETOOLKIT/red_main_poll.sh"
# Poison: the read-only timer has no POST path, so it must never source a
# writer. If it does, the tick aborts here with a named marker instead of
# silently gaining a write path the run gate would refuse.
cat > "$FAKETOOLKIT/post_rollup_comment.sh" <<'POISON'
echo "POISON: the read-only timer sourced post_rollup_comment.sh -- it has no POST path" >&2
return 1
POISON
# wrap <company> <state-dir> <parent> <repos...> -> sets WRC, WOUT, WERR. Extra env via WENV.
wrap() {
  local co="$1" st="$2" parent="$3"; shift 3
  env -u INCIDENT_SOURCE_CMD $WENV \
    PAPERCLIP_API_URL="$STUB_BASE" PAPERCLIP_COMPANY_ID="$co" \
    PAPERCLIP_AGENT_API_KEY=canary-agent-key-000 GH_TOKEN_CMD="echo not-a-real-token" \
    case_1a1b67a4_REPOS="$*" RED_MAIN_STATE_DIR="$st" \
    RED_MAIN_HANDOFF_DIR="${HO:-$WORK/handoff}" \
    RED_MAIN_TOOLKIT_DIR="$FAKETOOLKIT" RED_MAIN_ROLLUP_PARENT_ID="$parent" \
    WRAP_ENV_LOG="$WORK/wenv.log" REAL_POLLER="${WRAP_POLLER:-$HERE/red_main_poll.sh}" \
    "$WRAPPER" >"$WORK/wout.txt" 2>"$WORK/werr.txt"
  WRC=$?; WOUT="$(cat "$WORK/wout.txt")"; WERR="$(cat "$WORK/werr.txt")"
  return 0
}
WENV=""
# writes: comment POST attempts of any kind — the read-only timer must make
# zero, even though the stub would refuse them with the run gate. creates:
# company-issue POSTs (card creation — never constructible here either).
writes() { jq -rs '[.[] | select(.method=="POST" and (.path | test("^/api/issues/.*/comments$")))] | length' "$STUB_LOG"; }
creates() { jq -rs '[.[] | select(.method=="POST" and (.path | test("^/api/companies/.*/issues$")))] | length' "$STUB_LOG"; }
# props <state-dir> -> sorted proposal basenames, one per line.
props() { (ls "$1/proposals/" 2>/dev/null || true) | LC_ALL=C sort; }
nprops() { props "$1" | wc -l; }
firstline() { head -1 "$1"; }

hdr "Wrapper: green stays silent and records nothing"
: > "$STUB_LOG"; ST="$WORK/st-green"
wrap co-wrap-green "$ST" rollup o/green
[[ "$WRC" -eq 0 && "$(writes)" == "0" && "$(creates)" == "0" && "$(nprops "$ST")" == "0" ]] && ok "green tick: exit 0, zero board writes, no proposals" || bad "green tick: exit 0, zero board writes, no proposals" "rc=$WRC writes=$(writes) creates=$(creates) props=$(props "$ST") $WERR"

hdr "Wrapper: a persisting red is one proposal file, never a board write"
: > "$STUB_LOG"; ST="$WORK/st-red"
wrap co-wrap-red "$ST" rollup o/red-same-a
[[ "$WRC" -eq 1 && "$(writes)" == "0" && "$(creates)" == "0" && "$(nprops "$ST")" == "1" ]] && ok "first red tick records one proposal, fails the unit, writes nothing to the board" || bad "first red tick records one proposal, fails the unit, writes nothing to the board" "rc=$WRC writes=$(writes) creates=$(creates) props=$(props "$ST") $WERR"
T0="$(firstline "$ST/proposals/"*)"
[[ "$T0" == "# [red-main-poll] red-main drafts for the operator" ]] && ok "the proposal opens with the draft tag and title" || bad "the proposal opens with the draft tag and title" "first=$T0"
[[ "$T0" != *"[red-main:v1:"* ]] && ok "the proposal is not an incident card (no card is even constructible: zero company-wide writes)" || bad "the proposal is not an incident card" "first=$T0"
grep -qF "red-main:v1:o/red-same-a:" "$ST/proposals/"* \
  && ok "the proposal body carries the draft with its dedupe key" \
  || bad "the proposal body carries the draft with its dedupe key" "$(cut -c1-200 "$ST/proposals/"* | head -5)"
grep -qF "[red-main-poll] red-main drafts for the operator" <<<"$WOUT" \
  && ok "the proposal is printed to stdout (the unit journal)" \
  || bad "the proposal is printed to stdout (the unit journal)" "$(printf '%s' "$WOUT" | cut -c1-200)"
P1="$(props "$ST")"
wrap co-wrap-red "$ST" rollup o/red-same-a
[[ "$WOUT" != *"[red-main-poll] red-main drafts for the operator"* ]] || bad "a held tick re-prints the proposal" "stdout leaked a record"
wrap co-wrap-red "$ST" rollup o/red-same-a
[[ "$(props "$ST")" == "$P1" && "$(writes)" == "0" && "$WOUT" != *"[red-main-poll] red-main drafts for the operator"* ]] && ok "two more ticks on the same red record nothing new and still write nothing (cadence bound)" || bad "two more ticks on the same red record nothing new and still write nothing (cadence bound)" "props=$(props "$ST") writes=$(writes)"
# Age the state past the repost interval: the proposal is re-recorded.
DIG="$(awk '{print $2}' "$ST/draft.last")"
printf '%s %s\n' "$(( $(date +%s) - 90000 ))" "$DIG" > "$ST/draft.last"
wrap co-wrap-red "$ST" rollup o/red-same-a
[[ "$(nprops "$ST")" == "1" && "$(writes)" == "0" && "$WOUT" == *"[red-main-poll] red-main drafts for the operator"* ]] && ok "after the repost interval: the proposal is re-recorded, still no board write" || bad "after the repost interval: the proposal is re-recorded, still no board write" "props=$(props "$ST") writes=$(writes)"
wrap co-wrap-red "$ST" rollup o/red-same-a
[[ "$(nprops "$ST")" == "1" && "$WOUT" != *"[red-main-poll] red-main drafts for the operator"* ]] && ok "the record just made re-arms the bound (held tick prints nothing)" || bad "the record just made re-arms the bound (held tick prints nothing)" "props=$(props "$ST")"

hdr "Wrapper: the runless timer never attempts the refused POST"
# The stub refuses every comment POST with the deployed run gate. The
# read-only timer must make zero attempts: no 403 can fire without a POST.
: > "$STUB_LOG"; ST="$WORK/st-gate"
wrap co-wrap-gate "$ST" rollup o/red-same-a
[[ "$WRC" -eq 1 && "$(writes)" == "0" && "$(nprops "$ST")" == "1" ]] && ok "red tick under the live run gate: proposal recorded, zero POST attempts, unit failed" || bad "red tick under the live run gate: proposal recorded, zero POST attempts, unit failed" "rc=$WRC writes=$(writes) props=$(props "$ST") $WERR"
grep -q "POISON" "$WORK/werr.txt" \
  && bad "the wrapper sourced the writer double (no POST path allowed)" "$(cat "$WORK/werr.txt")" \
  || ok "the wrapper sourced no writer (poison file untouched)"

hdr "Wrapper: the draft digest is the SET of keys, not the text"
# Floor 0, so the only thing that can hold the second tick is an UNCHANGED
# digest: a digest over the text (which lists drafts in repo order) would differ
# and re-record, while the key set is the same.
: > "$STUB_LOG"; ST="$WORK/st-order"
WENV="RED_MAIN_CHANGE_FLOOR_SECONDS=0"
wrap co-wrap-order "$ST" rollup o/red-same-a o/red-same-b
P1="$(props "$ST")"
wrap co-wrap-order "$ST" rollup o/red-same-b o/red-same-a
WENV=""
[[ "$P1" == "$(props "$ST")" && "$WOUT" != *"[red-main-poll]"* ]] && ok "the same keys in a different repo order are one finding" || bad "the same keys in a different repo order are one finding" "props=$(props "$ST")"

hdr "Wrapper: a CHANGED finding records, but not faster than the floor"
: > "$STUB_LOG"; ST="$WORK/st-change"
wrap co-wrap-change "$ST" rollup o/red-same-a
printf '%s %s\n' "$(( $(date +%s) - 60 ))" "stale-digest-from-an-older-finding" > "$ST/draft.last"
wrap co-wrap-change "$ST" rollup o/red-same-a
[[ "$WOUT" != *"[red-main-poll] red-main drafts for the operator"* ]] && ok "a changed digest 60s after the last record is held by the floor" || bad "a changed digest 60s after the last record is held by the floor" "stdout leaked a record"
printf '%s %s\n' "$(( $(date +%s) - 4000 ))" "stale-digest-from-an-older-finding" > "$ST/draft.last"
wrap co-wrap-change "$ST" rollup o/red-same-a
[[ "$WOUT" == *"[red-main-poll] red-main drafts for the operator"* && "$(writes)" == "0" ]] && ok "a changed digest past the floor is recorded, still no board write" || bad "a changed digest past the floor is recorded, still no board write" "writes=$(writes)"

hdr "Wrapper: a clean tick re-arms the next red"
: > "$STUB_LOG"; ST="$WORK/st-clean"
wrap co-wrap-clean "$ST" rollup o/red-same-a
[[ -s "$ST/draft.last" && "$(nprops "$ST")" == "1" ]] || bad "precondition: state and proposal recorded after the first red" "missing $ST/draft.last"
wrap co-wrap-clean "$ST" rollup o/green
{ ls "$ST"/*.last >/dev/null 2>&1 || ls "$ST/proposals/"*.md >/dev/null 2>&1; } \
  && bad "a clean tick clears the cadence state and the outstanding proposals" "$(ls "$ST" "$ST/proposals" 2>/dev/null)" \
  || ok "a clean tick clears the cadence state and the outstanding proposals"
wrap co-wrap-clean "$ST" rollup o/red-same-a
[[ "$(nprops "$ST")" == "1" && "$WRC" -eq 1 && "$WOUT" == *"[red-main-poll] red-main drafts for the operator"* ]] && ok "red after green is recorded afresh, not swallowed by the old bound" || bad "red after green is recorded afresh, not swallowed by the old bound" "props=$(props "$ST") rc=$WRC"

hdr "Wrapper: a tick sweeps proposals of kinds it did not see"
: > "$STUB_LOG"; ST="$WORK/st-stale"
wrap co-wrap-stale "$ST" rollup o/red-same-a
[[ "$(nprops "$ST")" == "1" ]] || bad "precondition: one draft proposal after the first red" "props=$(props "$ST")"
printf 'stale error body\n' > "$ST/proposals/error-deadbeef.md"
printf '%s %s\n' "$(date +%s)" "stale-digest" > "$ST/error.last"
wrap co-wrap-stale "$ST" rollup o/red-same-a
{ ls "$ST"/proposals/error-*.md >/dev/null 2>&1 || ls "$ST/error.last" >/dev/null 2>&1; } \
  && bad "a draft-only tick sweeps the stale error proposal and stamp" "$(ls "$ST/proposals" 2>/dev/null)" \
  || ok "a draft-only tick sweeps the stale error proposal and stamp"
[[ "$(nprops "$ST")" == "1" && "$WRC" -eq 1 ]] || bad "the draft proposal survives its own tick's sweep" "props=$(props "$ST") rc=$WRC"

hdr "Wrapper: a coverage gap is its own proposal, beside the drafts"
: > "$STUB_LOG"; ST="$WORK/st-gap"
wrap co-wrap-gap "$ST" rollup o/denied
[[ "$WRC" -eq 1 && "$(writes)" == "0" && "$(firstline "$ST/proposals/"*)" == "# [red-main-poll-gap] red-main poll coverage gap" ]] && ok "an unobservable repo records a [red-main-poll-gap] proposal and fails the unit" || bad "an unobservable repo records a [red-main-poll-gap] proposal and fails the unit" "rc=$WRC writes=$(writes) $WERR"
: > "$STUB_LOG"; ST="$WORK/st-both"
wrap co-wrap-both "$ST" rollup o/red-same-a o/denied
[[ "$(nprops "$ST")" == "2" && "$(creates)" == "0" && "$(writes)" == "0" ]] && ok "red plus a blind repo records BOTH a draft proposal and a gap proposal" || bad "red plus a blind repo records BOTH a draft proposal and a gap proposal" "props=$(props "$ST") writes=$(writes) creates=$(creates) $WERR"
TT="$(for f in "$ST/proposals/"*; do firstline "$f"; done | LC_ALL=C sort | tr '\n' '|' | sed 's/|$//')"
[[ "$TT" == "# [red-main-poll-gap] red-main poll coverage gap|# [red-main-poll] red-main drafts for the operator" ]] && ok "the draft proposal and the gap proposal carry distinct tags" || bad "the draft proposal and the gap proposal carry distinct tags" "$TT"

hdr "Wrapper: the bounded environment reaches the toolkit scripts"
: > "$WORK/wenv.log"; ST="$WORK/st-wire"
wrap co-wrap-wire "$ST" rollup o/green
grep -q "ROLLUP=rollup " "$WORK/wenv.log" \
  && ok "the wrapper exports the rollup parent id to the poller" \
  || bad "the wrapper exports the rollup parent id to the poller" "$(cat "$WORK/wenv.log" 2>/dev/null)"
grep -q "BIN=$FAKETOOLKIT/gh_ci_status.sh$" "$WORK/wenv.log" \
  && ok "the wrapper points the CI reader at the toolkit clone" \
  || bad "the wrapper points the CI reader at the toolkit clone" "$(cat "$WORK/wenv.log" 2>/dev/null)"

hdr "Wrapper: every tick writes the handoff envelope the CEO routine reads"
HO="$WORK/handoff-green"; : > "$STUB_LOG"; ST="$WORK/st-handoff-green"
wrap co-handoff-green "$ST" rollup o/green
ENV_G="$HO/red-mains.json"
[[ "$WRC" -eq 0 && -f "$ENV_G" ]] && ok "a green tick writes the envelope and exits 0" || bad "a green tick writes the envelope and exits 0" "rc=$WRC $WERR"
[[ "$(jq -r '[.schema, (.exitCode|tostring), (.redMains|length|tostring)] | join("|")' "$ENV_G" 2>/dev/null)" == "watchdog.red-main-handoff.v1|0|0" ]] && ok "green envelope: schema, exit 0, no entries" || bad "green envelope: schema, exit 0, no entries" "$(cat "$ENV_G" 2>/dev/null)"
[[ "$(jq -r .measuredAt "$ENV_G" 2>/dev/null)" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] && ok "measuredAt is a UTC ISO time" || bad "measuredAt is a UTC ISO time" "$(jq -r .measuredAt "$ENV_G" 2>/dev/null)"
age=$(( $(date -u +%s) - $(date -u -d "$(jq -r .measuredAt "$ENV_G" 2>/dev/null)" +%s 2>/dev/null || echo 999999) ))
[[ "$age" -ge 0 && "$age" -le 120 ]] && ok "measuredAt is this tick's own clock (within two minutes)" || bad "measuredAt is this tick's own clock (within two minutes)" "age=${age}s"
[[ "$(stat -c %a "$ENV_G")" == "644" && "$(stat -c %a "$HO")" == "755" ]] && ok "envelope and directory are readable by the agent mount (0644 and 0755)" || bad "envelope and directory are readable by the agent mount (0644 and 0755)" "file=$(stat -c %a "$ENV_G") dir=$(stat -c %a "$HO")"
[[ -z "$(ls -A "$HO" | grep '^\.red-mains' || true)" ]] && ok "no temp file is left beside the envelope" || bad "no temp file is left beside the envelope" "$(ls -A "$HO")"

hdr "Wrapper: a red tick hands the snapshot entries to the routine"
HO="$WORK/handoff-red"; : > "$STUB_LOG"; ST="$WORK/st-handoff-red"
wrap co-handoff-red "$ST" rollup o/red-same-a
ENV_R="$HO/red-mains.json"
[[ "$(jq -r .exitCode "$ENV_R" 2>/dev/null)" == "1" && "$(jq -r '.redMains|length' "$ENV_R" 2>/dev/null)" == "1" ]] && ok "red envelope: snapshot exit 1, one entry" || bad "red envelope: snapshot exit 1, one entry" "$(cat "$ENV_R" 2>/dev/null)"
[[ "$(jq -r '.redMains[0].dedupeTag' "$ENV_R" 2>/dev/null)" == red-main:v1:o/red-same-a:* ]] && ok "the entry carries the contract dedupe tag" || bad "the entry carries the contract dedupe tag" "$(jq -c .redMains "$ENV_R" 2>/dev/null)"
jq -e 'keys == ["exitCode","measuredAt","redMains","schema"]' "$ENV_R" >/dev/null && ok "the envelope holds the snapshot fields and no drafts text" || bad "the envelope holds the snapshot fields and no drafts text" "$(jq -c 'keys' "$ENV_R" 2>/dev/null)"
[[ "$WRC" -eq 1 && "$(writes)" == "0" ]] && ok "the red tick still fails the unit and makes no board write" || bad "the red tick still fails the unit and makes no board write" "rc=$WRC writes=$(writes)"
! grep -qF -e canary-agent-key-000 -e not-a-real-token "$ENV_R" && ok "the envelope carries no credential" || bad "the envelope carries no credential" "a credential reached the handoff"

hdr "Wrapper: a snapshot that prints no array is handed off as unknown"
printf '#!/usr/bin/env bash\nexit 0\n' > "$WORK/poll-silent.sh"; chmod +x "$WORK/poll-silent.sh"
HO="$WORK/handoff-silent"; : > "$STUB_LOG"; ST="$WORK/st-handoff-silent"
WRAP_POLLER="$WORK/poll-silent.sh" wrap co-handoff-silent "$ST" rollup o/green
[[ "$(jq -r .exitCode "$HO/red-mains.json" 2>/dev/null)" == "3" && "$(jq -r '.redMains|length' "$HO/red-mains.json" 2>/dev/null)" == "0" ]] && ok "a snapshot with no array is an unknown handoff, never a clean tick" || bad "a snapshot with no array is an unknown handoff, never a clean tick" "$(cat "$HO/red-mains.json" 2>/dev/null)"

hdr "Wrapper: a repo listed twice is polled once"
HO="$WORK/handoff-dup"; : > "$STUB_LOG"; ST="$WORK/st-handoff-dup"
wrap co-handoff-dup "$ST" rollup o/red-same-a o/red-same-a
[[ "$(jq -r '.redMains|length' "$HO/red-mains.json" 2>/dev/null)" == "1" ]] && ok "a repo listed twice yields one handoff row" || bad "a repo listed twice yields one handoff row" "$(cat "$HO/red-mains.json" 2>/dev/null)"

hdr "Wrapper: a blind repo hands over exit 3, never an empty clean snapshot"
HO="$WORK/handoff-gap"; : > "$STUB_LOG"; ST="$WORK/st-handoff-gap"
wrap co-handoff-gap "$ST" rollup o/denied
[[ "$(jq -r .exitCode "$HO/red-mains.json" 2>/dev/null)" == "3" && "$(jq -r '.redMains|length' "$HO/red-mains.json" 2>/dev/null)" == "0" ]] && ok "an unobservable repo is exit 3 in the envelope" || bad "an unobservable repo is exit 3 in the envelope" "$(cat "$HO/red-mains.json" 2>/dev/null)"

hdr "Wrapper: an unwritable handoff directory fails loudly with no board write"
: > "$WORK/not-a-directory"
HO="$WORK/not-a-directory/handoff"; : > "$STUB_LOG"; ST="$WORK/st-handoff-broken"
wrap co-handoff-broken "$ST" rollup o/green
[[ "$WRC" -eq 1 && "$WERR" == *RED_MAIN_HANDOFF_DIR* && "$(writes)" == "0" ]] && ok "a handoff directory that cannot be created exits 1 and names the variable" || bad "a handoff directory that cannot be created exits 1 and names the variable" "rc=$WRC err=$WERR writes=$(writes)"
HO=""

hdr "Operator packet matches the code"
INSTALL="$HERE/ops/case_e77a1e99/INSTALL.md"
if grep -qF 'q=red-main:v1&limit' "$INSTALL"; then bad "INSTALL.md still documents the company-list board read" "stale read present"; else ok "INSTALL.md no longer documents the company-list board read"; fi
if grep -qF 'POST /api/companies/${PAPERCLIP_COMPANY_ID}/issues' "$INSTALL"; then bad "INSTALL.md still documents company-wide card creation" "stale write present"; else ok "INSTALL.md documents no company-wide card creation"; fi
if grep -qF 'appends one comment' "$INSTALL"; then bad "INSTALL.md still directs the timer to post comments" "stale timer POST present"; else ok "INSTALL.md directs no timer comment posts"; fi
if grep -q 'post_rollup_comment' "$WRAPPER"; then bad "the wrapper sources or calls the writer (no POST path allowed)"; else ok "the wrapper has no writer source and no POST path"; fi
for needle in RED_MAIN_ROLLUP_PARENT_ID parentIssueIds 'checks:read' RED_MAIN_TOOLKIT_DIR '[red-main-poll-gap]' '[red-main-poll-error]' RED_MAIN_REPOST_SECONDS RED_MAIN_STATE_DIR 'filed:' 'sync-toolkit.sh' 'cross_issue_influence_run_context_required' 'proposals/' RED_MAIN_HANDOFF_DIR 'red-mains.json' 'watchdog.red-main-handoff.v1' '--red-mains-handoff'; do
  grep -qF -- "$needle" "$INSTALL" && ok "INSTALL.md documents $needle" || bad "INSTALL.md documents $needle" "missing"
done
if grep -qF 'no `actions:read`' "$INSTALL"; then ok "INSTALL.md withholds actions:read"; else bad "INSTALL.md withholds actions:read" "missing"; fi

echo
printf 'red_main_poll: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
