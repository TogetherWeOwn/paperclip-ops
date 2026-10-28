// Source evidence: green required CI and an independent Paperclip Review,
// authenticated per exact commit, before the rule may post `approved`.
//
// Offline. Positive rows run GitHub-shaped check-run and pull-request
// responses (trimmed real captures of Bot main 7a5043e4b3 / PR #626, see
// fixtures/README.md) through the real reader and verifier; refusal rows
// mutate one fact of that fixture and demand a fail-closed result with the
// right stable reason. No network, credential, webhook delivery or environment.
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { verifySourceEvidence } from '../src/source.js'
import { createGithubReader } from '../src/github.js'
import { findPolicy } from '../src/environments.js'
import { REASON } from '../src/decide.js'
import {
  contractFixture, jsonResponse, wire, SOURCE, HEAD, PLAN_HEAD, REPO, PR_HEADS, CI_APP_ID, REVIEW_APP_ID,
  REAL_MAIN_CHECKS, REAL_MAIN_PULLS, REAL_HEAD_CHECKS, reviewRun,
} from './contract-fixture.mjs'

const EVIDENCE = findPolicy({ repository: REPO, environment: 'staging-migrate-apply' }).sourceEvidence
const SHAS = [SOURCE, HEAD, PLAN_HEAD]
const CI = { ok: false, reason: REASON.SOURCE_CI_UNVERIFIED }
const REVIEW = { ok: false, reason: REASON.SOURCE_REVIEW_UNVERIFIED }

const verify = (f, shas = SHAS, evidence = EVIDENCE) => verifySourceEvidence({ github: f.github, repository: REPO, shas, evidence })
const ciRun = (f, sha, name) => f.checkRuns[sha].find((r) => r.name === name)
const review = (f, sha) => f.checkRuns[PR_HEADS[sha]][0]

test('a green merged source, plan head and workflow head are each authenticated', async () => {
  const f = contractFixture()
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
  // CI on the commit itself, then pull-request association, then the review on
  // the pull request head: two check-run reads and one pull read per commit.
  assert.equal(f.calls.checkRuns, 6)
  assert.equal(f.calls.pulls, 3)
  const reads = f.requests.map((r) => r.url)
  for (const sha of SHAS) {
    assert.ok(reads.includes(`https://api.github.com/repos/${REPO}/commits/${sha}/check-runs?filter=latest&per_page=100`))
    assert.ok(reads.includes(`https://api.github.com/repos/${REPO}/commits/${sha}/pulls?per_page=100`))
    assert.ok(reads.includes(`https://api.github.com/repos/${REPO}/commits/${PR_HEADS[sha]}/check-runs?filter=latest&per_page=100`))
  }
})

test('a commit named twice is read once', async () => {
  const f = contractFixture()
  assert.deepEqual(await verify(f, [SOURCE, SOURCE, HEAD]), { ok: true, shas: [SOURCE, HEAD] })
  assert.equal(f.calls.pulls, 2)
})

test('trimmed real GitHub responses for Bot main 7a5043e4b3 and PR #626 head verify', async () => {
  const source = REAL_MAIN_PULLS[0].merge_commit_sha
  const prHead = REAL_MAIN_PULLS[0].head.sha
  const prefix = `https://api.github.com/repos/${REPO}`
  const served = new Map([
    [`${prefix}/commits/${source}/check-runs?filter=latest&per_page=100`, REAL_MAIN_CHECKS],
    [`${prefix}/commits/${source}/pulls?per_page=100`, REAL_MAIN_PULLS],
    [`${prefix}/commits/${prHead}/check-runs?filter=latest&per_page=100`, REAL_HEAD_CHECKS],
  ])
  const requests = []
  const fetchImpl = async (url) => { requests.push(String(url)); return served.has(String(url)) ? jsonResponse(served.get(String(url))) : new Response('{}', { status: 404 }) }
  const github = createGithubReader({ fetchImpl, token: 'offline-stub', allowedRepositories: [REPO] })
  assert.deepEqual(await verifySourceEvidence({ github, repository: REPO, shas: [source], evidence: EVIDENCE }), { ok: true, shas: [source] })
  assert.equal(requests.length, 3)
  const real = REAL_HEAD_CHECKS.check_runs.find((r) => r.name === 'Paperclip Review')
  assert.equal(real.app.id, REVIEW_APP_ID)
  assert.equal(real.output.title, '5/5')
  assert.ok(real.external_id.endsWith(`:${REAL_MAIN_PULLS[0].number}:${prHead}`))
})

test('a missing or empty review title falls back to the first summary line', async () => {
  for (const title of [null, '']) {
    const f = contractFixture()
    for (const sha of SHAS) {
      Object.assign(review(f, sha).output, { title, summary: '5/5\nAdditional details' })
    }
    assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
  }
})

test('a nonempty review title takes precedence over the summary', async () => {
  const f = contractFixture()
  for (const sha of SHAS) {
    Object.assign(review(f, sha).output, { title: '4/5', summary: '5/5\nAdditional details' })
  }
  assert.deepEqual(await verify(f), REVIEW)
})

test('the policy pins CI to GitHub Actions and the review to the review App, with different ids', () => {
  assert.equal(EVIDENCE.ci.appId, CI_APP_ID)
  assert.equal(EVIDENCE.review.appId, REVIEW_APP_ID)
  assert.notEqual(CI_APP_ID, REVIEW_APP_ID)
})

test('several green runs of one check name all count, and an unrelated associated pull request is ignored', async () => {
  const f = contractFixture()
  const extra = { ...structuredClone(ciRun(f, SOURCE, 'gitleaks')), id: 9 }
  f.checkRuns[SOURCE].push(extra)
  const other = { ...structuredClone(f.pulls[SOURCE][0]), number: 999, state: 'open', merged_at: null, merge_commit_sha: 'f'.repeat(40) }
  f.pulls[SOURCE].push(other)
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('a review that finished in the same second as the merge still counts', async () => {
  const f = contractFixture()
  review(f, SOURCE).completed_at = f.pulls[SOURCE][0].merged_at
  assert.equal((await verify(f)).ok, true)
})

const ciNegatives = {
  'ci-ok absent': (f) => { f.checkRuns[SOURCE] = f.checkRuns[SOURCE].filter((r) => r.name !== 'ci-ok') },
  'gitleaks absent': (f) => { f.checkRuns[SOURCE] = f.checkRuns[SOURCE].filter((r) => r.name !== 'gitleaks') },
  'pr-lint absent': (f) => { f.checkRuns[SOURCE] = f.checkRuns[SOURCE].filter((r) => r.name !== 'pr-lint') },
  'no check runs at all': (f) => { f.checkRuns[SOURCE] = [] },
  'ci-ok failed': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'failure' },
  'ci-ok cancelled': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'cancelled' },
  'ci-ok timed out': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'timed_out' },
  'ci-ok skipped is not green': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'skipped' },
  'ci-ok neutral is not green': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'neutral' },
  'ci-ok action required': (f) => { ciRun(f, SOURCE, 'ci-ok').conclusion = 'action_required' },
  'ci-ok still running': (f) => { Object.assign(ciRun(f, SOURCE, 'ci-ok'), { status: 'in_progress', conclusion: null }) },
  'ci-ok queued': (f) => { Object.assign(ciRun(f, SOURCE, 'ci-ok'), { status: 'queued', conclusion: null }) },
  'conclusion on an unfinished run is not trusted': (f) => { ciRun(f, SOURCE, 'ci-ok').status = 'in_progress' },
  'ci-ok produced by another App': (f) => { ciRun(f, SOURCE, 'ci-ok').app = { id: REVIEW_APP_ID, slug: 'other' } },
  'ci-ok with no producing App': (f) => { ciRun(f, SOURCE, 'ci-ok').app = null },
  'ci-ok bound to another commit': (f) => { ciRun(f, SOURCE, 'ci-ok').head_sha = HEAD },
  'a red duplicate beside a green run': (f) => { f.checkRuns[SOURCE].push({ ...structuredClone(ciRun(f, SOURCE, 'gitleaks')), id: 9, conclusion: 'failure' }) },
  'a green run from another App cannot stand in for the pinned one': (f) => {
    const real = ciRun(f, SOURCE, 'pr-lint')
    f.checkRuns[SOURCE] = f.checkRuns[SOURCE].filter((r) => r !== real)
    f.checkRuns[SOURCE].push({ ...real, app: { id: 1, slug: 'impostor' } })
  },
  'workflow head CI red': (f) => { ciRun(f, HEAD, 'ci-ok').conclusion = 'failure' },
  'plan head CI red': (f) => { ciRun(f, PLAN_HEAD, 'gitleaks').conclusion = 'failure' },
  'workflow head CI absent': (f) => { f.checkRuns[HEAD] = [] },
  'plan head CI pending': (f) => { Object.assign(ciRun(f, PLAN_HEAD, 'pr-lint'), { status: 'queued', conclusion: null }) },
  'check list too large for one page': (f) => { f.checkRuns[SOURCE] = Array.from({ length: 101 }, (_, i) => ({ ...structuredClone(ciRun(f, SOURCE, 'ci-ok')), id: 100 + i })) },
  'check run head is not a SHA': (f) => { ciRun(f, SOURCE, 'ci-ok').head_sha = 'main' },
  'check run has no name': (f) => { delete ciRun(f, SOURCE, 'ci-ok').name },
}
for (const [name, mutate] of Object.entries(ciNegatives)) {
  test('CI refuses ' + name, async () => {
    const f = contractFixture()
    mutate(f)
    assert.deepEqual(await verify(f), CI)
  })
}

const reviewNegatives = {
  'commit pushed straight to main (no pull request)': (f) => { f.pulls[SOURCE] = [] },
  'pull request not merged': (f) => { Object.assign(f.pulls[SOURCE][0], { state: 'open', merged_at: null }) },
  'closed without merge': (f) => { f.pulls[SOURCE][0].merged_at = null },
  'merge commit of the pull request is another commit': (f) => { f.pulls[SOURCE][0].merge_commit_sha = 'f'.repeat(40) },
  'merge commit missing': (f) => { f.pulls[SOURCE][0].merge_commit_sha = null },
  'two pull requests claim the same merge commit': (f) => { f.pulls[SOURCE].push({ ...structuredClone(f.pulls[SOURCE][0]), number: 999 }) },
  'pull request merged into another branch': (f) => { f.pulls[SOURCE][0].base.ref = 'staging' },
  'pull request merged into another repository': (f) => { f.pulls[SOURCE][0].base.repo.full_name = 'Else/other' },
  'pull request has no head': (f) => { delete f.pulls[SOURCE][0].head.sha },
  'merge time malformed': (f) => { f.pulls[SOURCE][0].merged_at = 'yesterday' },
  // A full page may hide further associated pull requests, so even a page that
  // does contain the merged one is refused.
  'pull list is a full page and may be truncated': (f) => {
    const real = f.pulls[SOURCE][0]
    f.pulls[SOURCE] = [...Array.from({ length: 99 }, (_, i) => ({ ...structuredClone(real), number: 200 + i, merge_commit_sha: 'f'.repeat(40) })), real]
  },
  'pull request state open despite a merge time': (f) => { f.pulls[SOURCE][0].state = 'open' },
  'review absent': (f) => { f.checkRuns[PR_HEADS[SOURCE]] = [] },
  'review attached to the merge commit instead of the pull request head': (f) => { f.checkRuns[SOURCE].push(f.checkRuns[PR_HEADS[SOURCE]].pop()) },
  'review produced by another App': (f) => { review(f, SOURCE).app = { id: CI_APP_ID, slug: 'github-actions' } },
  'review with no producing App': (f) => { review(f, SOURCE).app = null },
  'review name differs in case': (f) => { review(f, SOURCE).name = 'paperclip review' },
  'review name has a suffix': (f) => { review(f, SOURCE).name = 'Paperclip Review ' },
  'two review runs on the head': (f) => { f.checkRuns[PR_HEADS[SOURCE]].push({ ...structuredClone(review(f, SOURCE)), id: 9 }) },
  'review still running': (f) => { Object.assign(review(f, SOURCE), { status: 'in_progress', conclusion: null }) },
  'review failed': (f) => { review(f, SOURCE).conclusion = 'failure' },
  'review left at action required after a rate limit': (f) => { review(f, SOURCE).conclusion = 'action_required' },
  'review neutral': (f) => { review(f, SOURCE).conclusion = 'neutral' },
  'review skipped': (f) => { review(f, SOURCE).conclusion = 'skipped' },
  'review cancelled': (f) => { review(f, SOURCE).conclusion = 'cancelled' },
  'review scored 4/5': (f) => { review(f, SOURCE).output.title = '4/5' },
  'review scored 3/5': (f) => { review(f, SOURCE).output.title = '3/5' },
  'review score has trailing text': (f) => { review(f, SOURCE).output.title = '5/5 ' },
  'review score has a suffix': (f) => { review(f, SOURCE).output.title = '5/5 approved' },
  'review has no score': (f) => { review(f, SOURCE).output = { title: null, summary: null } },
  'review has no output': (f) => { delete review(f, SOURCE).output },
  'review bound to another head': (f) => { review(f, SOURCE).head_sha = PR_HEADS[HEAD] },
  'review recorded against another pull request': (f) => { review(f, SOURCE).external_id = `x:999:${PR_HEADS[SOURCE]}` },
  'review recorded against another head': (f) => { review(f, SOURCE).external_id = `x:101:${PR_HEADS[HEAD]}` },
  'review with an empty external id': (f) => { review(f, SOURCE).external_id = '' },
  'review with no external id': (f) => { delete review(f, SOURCE).external_id },
  'review external id with a longer pull request number': (f) => { review(f, SOURCE).external_id = `x:1101:${PR_HEADS[SOURCE]}` },
  'review finished after the merge': (f) => { review(f, SOURCE).completed_at = '2026-10-07T06:09:14Z' },
  'review finished long after the merge': (f) => { review(f, SOURCE).completed_at = '2026-10-08T00:00:00Z' },
  'review has no finish time': (f) => { review(f, SOURCE).completed_at = null },
  'review finish time malformed': (f) => { review(f, SOURCE).completed_at = 'soon' },
  'workflow head review absent': (f) => { f.checkRuns[PR_HEADS[HEAD]] = [] },
  'workflow head review scored 4/5': (f) => { review(f, HEAD).output.title = '4/5' },
  'workflow head was not merged through a pull request': (f) => { f.pulls[HEAD] = [] },
  'plan head review absent': (f) => { f.checkRuns[PR_HEADS[PLAN_HEAD]] = [] },
  'plan head review failed': (f) => { review(f, PLAN_HEAD).conclusion = 'failure' },
  'plan head review produced by another App': (f) => { review(f, PLAN_HEAD).app.id = 1 },
  'one review cannot cover another pull request head': (f) => { f.checkRuns[PR_HEADS[PLAN_HEAD]] = [reviewRun(PR_HEADS[SOURCE], 101)] },
}
for (const [name, mutate] of Object.entries(reviewNegatives)) {
  test('review refuses ' + name, async () => {
    const f = contractFixture()
    mutate(f)
    assert.deepEqual(await verify(f), REVIEW)
  })
}

test('an unreadable pull-request or check-run read fails closed with the matching reason', async () => {
  const unreadable = (needle) => {
    const f = contractFixture()
    const original = f.fetch
    f.fetch = async (url, init) => (String(url).includes(needle) ? new Response('{}', { status: 500 }) : original(url, init))
    f.github = createGithubReader({ fetchImpl: f.fetch, token: 'offline-stub', allowedRepositories: [REPO] })
    return f
  }
  assert.deepEqual(await verify(unreadable(`/commits/${SOURCE}/check-runs`)), CI)
  assert.deepEqual(await verify(unreadable(`/commits/${HEAD}/pulls`)), REVIEW)
  assert.deepEqual(await verify(unreadable(`/commits/${PR_HEADS[SOURCE]}/check-runs`)), REVIEW)
  const garbled = contractFixture()
  const original = garbled.fetch
  garbled.fetch = async (url, init) => (String(url).includes('/pulls') ? new Response('[{') : original(url, init))
  garbled.github = createGithubReader({ fetchImpl: garbled.fetch, token: 'offline-stub', allowedRepositories: [REPO] })
  assert.deepEqual(await verify(garbled), REVIEW)
})

test('CI is reported before review when both are missing', async () => {
  const f = contractFixture()
  ciNegatives['ci-ok failed'](f)
  reviewNegatives['review absent'](f)
  assert.deepEqual(await verify(f), CI)
})

test('a refusal on any one trusted commit never partially passes', async () => {
  const f = contractFixture()
  review(f, PLAN_HEAD).conclusion = 'failure'
  const result = await verify(f)
  assert.equal(result.ok, false)
  assert.equal(result.shas, undefined)
})

test('a malformed request or policy is a programming error, not an answer', async () => {
  const f = contractFixture()
  for (const shas of [[], null, 'a'.repeat(40), ['short'], [SOURCE, 7], Array.from({ length: 9 }, (_, i) => String(i).repeat(40))]) {
    await assert.rejects(verify(f, shas), JSON.stringify(shas))
  }
  for (const evidence of [null, {}, { ci: EVIDENCE.ci }, { review: EVIDENCE.review }]) await assert.rejects(verify(f, SHAS, evidence))
  assert.equal(f.requests.length, 0)
})

test('required names and scores come from the policy, not from constants in the verifier', async () => {
  const f = contractFixture()
  const strict = { ...EVIDENCE, ci: { ...EVIDENCE.ci, checks: [...EVIDENCE.ci.checks, 'codeql'] } }
  assert.deepEqual(await verify(f, SHAS, strict), CI)
  const other = { ...EVIDENCE, review: { ...EVIDENCE.review, check: 'Other Review' } }
  assert.deepEqual(await verify(f, SHAS, other), REVIEW)
  const higher = { ...EVIDENCE, review: { ...EVIDENCE.review, score: '10/10' } }
  assert.deepEqual(await verify(f, SHAS, higher), REVIEW)
})

// Reader contract: shape, scope and budgets of the two new GitHub reads.
test('check-run reader maps the producing App and refuses unscoped or malformed reads', async () => {
  const f = contractFixture()
  const runs = await f.github.listCheckRuns(REPO, SOURCE)
  assert.deepEqual(runs.map((r) => [r.name, r.appId, r.status, r.conclusion, r.headSha]).sort(),
    ['ci-ok', 'gitleaks', 'pr-lint'].map((n) => [n, CI_APP_ID, 'completed', 'success', SOURCE]))
  await assert.rejects(f.github.listCheckRuns('Else/other', SOURCE))
  await assert.rejects(f.github.listCheckRuns(REPO, 'main'))
  await assert.rejects(f.github.listPullsForCommit('Else/other', SOURCE))
  await assert.rejects(f.github.listPullsForCommit(REPO, 'HEAD'))
  assert.equal(f.requests.length, 1)
})

test('check-run reader refuses a list whose total disagrees with its rows', async () => {
  const f = contractFixture()
  const github = createGithubReader({ token: 'offline-stub', allowedRepositories: [REPO],
    fetchImpl: async () => new Response(wire({ total_count: 5, check_runs: f.checkRuns[SOURCE] })) })
  await assert.rejects(github.listCheckRuns(REPO, SOURCE))
})

test('pull reader refuses a non-list answer and keeps only what the verifier reads', async () => {
  const f = contractFixture()
  const github = createGithubReader({ token: 'offline-stub', allowedRepositories: [REPO],
    fetchImpl: async () => new Response(wire({ message: 'Not Found' })) })
  await assert.rejects(github.listPullsForCommit(REPO, SOURCE))
  const [pull] = await f.github.listPullsForCommit(REPO, SOURCE)
  assert.deepEqual(Object.keys(pull).sort(), ['baseRef', 'baseRepository', 'headSha', 'mergeCommitSha', 'mergedAt', 'number', 'state'])
  assert.equal(pull.mergeCommitSha, SOURCE)
  assert.equal(pull.headSha, PR_HEADS[SOURCE])
})

test('the committed fixtures retain their exact public snapshot bytes', () => {
  // Publication replaced only check-run external_id values with synthetic UUIDs.
  // Pin those sanitized bytes; pull provenance and all scored check fields remain unchanged.
  const pinned = {
    'main-7a5043e-check-runs.json': '7e4f439a72363666786b37ec64802cb1fa4b01f28773439737965b0b55c5a359',
    'main-7a5043e-pulls.json': '21c3511077c80df130f9b83178ec94077407d3222619af376b051fef139626ac',
    'pr626-head-a4da131-check-runs.json': '2ad3cffd32223821f95b23de334439bca6b61e14989240a006690cdf75631077',
  }
  for (const [name, digest] of Object.entries(pinned)) {
    assert.equal(createHash('sha256').update(readFileSync(new URL(`./fixtures/github/${name}`, import.meta.url))).digest('hex'), digest, name)
  }
  assert.equal(REAL_MAIN_PULLS[0].number, 626)
  assert.equal(REAL_MAIN_PULLS[0].merge_commit_sha, '7a5043e4b378e2648c9287ea00d916a16dacdb08')
  assert.equal(REAL_HEAD_CHECKS.check_runs[0].head_sha, 'a4da131e368e086b9c271f164226ac7387a41996')
})
