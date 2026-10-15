// Offline integration suite for the protection-rule endpoint.
//
// No credentials, no network, no clock. Every transport behind the webhook is
// a stub: GitHub reads, board reads and the callback post are scripted per
// test, and the suite asserts the (HTTP status, posted verdict) pair — never
// message text. The rows that carry this suite:
//
//   * the full APPROVE path posts `approved` exactly once, and only then;
//   * every evidence or approval gap posts `rejected`, never `approved`;
//   * an unverified delivery reaches no transport at all;
//   * a missing secret answers 503 without minting or posting anything.

import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createApp, MAX_ACCEPTED_BODY_BYTES } from '../src/app.js'
import { contractFixture, jsonResponse, HASH, HEAD, RUN_ID, SOURCE, PLAN_HEAD, PR_HEADS } from './contract-fixture.mjs'
import { EVENT, REASON } from '../src/decide.js'
import { CEO_AGENT_ID } from '../src/board.js'
import { PROTECTED_ENVIRONMENTS } from '../src/environments.js'

const SECRET = 'test-webhook-secret-not-a-real-one'
const APP_ID = 'test-app-id'
const APP_KEY = 'test-app-key-PRIVATE KEY-marker-not-a-real-one'
const BOARD_TOKEN = 'test-board-token-not-a-real-one'
const BOARD_ORIGIN = 'https://board.example.invalid'

const PLAN_POLICY = PROTECTED_ENVIRONMENTS.find((p) => p.mode === 'plan')
const PLAN_ENVIRONMENT = PLAN_POLICY.environment
const PLAN_ISSUE = PLAN_POLICY.goIssueId
const REPO = PLAN_POLICY.repository
const SHA_POLICY = PROTECTED_ENVIRONMENTS.find((p) => p.mode === 'sha')
const SHA_ENVIRONMENT = SHA_POLICY.environment
const SHA_REPO = SHA_POLICY.repository
const SHA_ISSUE = SHA_POLICY.goIssueId

const INSTALLATION_ID = 155772577
const CALLBACK = `https://api.github.com/repos/${REPO}/actions/runs/${RUN_ID}/deployment_protection_rule`
const SHA_CALLBACK = `https://api.github.com/repos/${SHA_REPO}/actions/runs/${RUN_ID}/deployment_protection_rule`

async function sign(secret, bodyText) {
  const key = await globalThis.crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = new Uint8Array(await globalThis.crypto.subtle.sign('HMAC', key, new TextEncoder().encode(bodyText)))
  return 'sha256=' + [...mac].map((b) => b.toString(16).padStart(2, '0')).join('')
}

function payload(overrides = {}) {
  return {
    action: 'requested',
    environment: PLAN_ENVIRONMENT,
    event: 'workflow_dispatch',
    sha: HEAD,
    ref: 'refs/heads/main',
    deployment_callback_url: CALLBACK,
    deployment: { run_id: RUN_ID, sha: HEAD, environment: PLAN_ENVIRONMENT },
    repository: { full_name: REPO },
    installation: { id: INSTALLATION_ID },
    ...overrides,
  }
}

function shaPayload(overrides = {}) {
  return {
    action: 'requested',
    environment: SHA_ENVIRONMENT,
    event: 'workflow_dispatch',
    sha: HEAD,
    ref: 'refs/heads/main',
    deployment_callback_url: SHA_CALLBACK,
    deployment: { run_id: RUN_ID, sha: HEAD, environment: SHA_ENVIRONMENT },
    repository: { full_name: SHA_REPO },
    installation: { id: INSTALLATION_ID },
    ...overrides,
  }
}

// Stub transport. `githubMode` scripts the GitHub half, `boardComments` the
// board half; every callback post is recorded in `posts`. The JWT mint is
// stubbed per-harness (its real path is covered by `mint` tests against a
// generated key): the stub answers with a token except in `mint-fails`, where
// it throws the way the real minter does on a bad key or a refused mint.
function harness({ githubMode = 'happy', boardComments = [], boardCommentsByIssue = null, appOverrides = {} } = {}) {
  let mints = 0
  const mintToken = async () => {
    mints++
    if (githubMode === 'mint-fails') throw new Error('GitHub installation token mint failed')
    return 'stub-installation-token'
  }
  const posts = []
  const fixture = contractFixture()
  const calls = fixture.calls
  calls.board = 0
  if (githubMode === 'no-claim') fixture.applyArtifacts = []
  if (githubMode === 'plan-missing-artifact') fixture.planArtifacts = []
  if (githubMode === 'plan-commit-mismatch') fixture.manifest.source_sha = 'e'.repeat(40)
  if (githubMode === 'run-mismatch-sha') fixture.applyRun.head_sha = 'd'.repeat(40)
  if (githubMode === 'plan-hash-mismatch') fixture.manifest.source_migrations[0].description = 'altered'
  if (githubMode === 'redirect-download') fixture.redirect = true
  const requests = fixture.requests
  const fetchImpl = async (url, init = {}) => {
    const target = String(url)
    const method = init.method ?? 'GET'
    requests.push({ url: target, authorization: init.headers?.authorization ?? null })
    if (target === `https://api.github.com/repos/${SHA_REPO}/actions/runs/${RUN_ID}` && method === 'GET') {
      calls.run++
      return jsonResponse({ ...fixture.applyRun, repository: { full_name: SHA_REPO }, head_repository: { full_name: SHA_REPO } })
    }
    if (target.includes('/api/issues/') && target.endsWith('/comments?order=asc') && method === 'GET') {
      calls.board++
      if (githubMode === 'board-fails') return new Response('{}', { status: 500 })
      // Per-card comments: a harness serving a GO on one card does not serve
      // it on another. Keyed lists take precedence; the flat list is the
      // legacy default for callers that pass one.
      if (boardCommentsByIssue !== null) {
        const match = /\/api\/issues\/([^/]+)\/comments\?order=asc$/.exec(target)
        return Response.json(boardCommentsByIssue[match?.[1]] ?? [])
      }
      return Response.json(boardComments)
    }
    // Any protection-rule review endpoint answers: the suite models GitHub
    // faithfully enough that a post to the WRONG run would succeed (204) —
    // so a dropped run binding shows up as a post, not as an unstubbed error.
    if (/\/actions\/runs\/\d+\/deployment_protection_rule$/.test(target) && method === 'POST') {
      posts.push({ url: target, ...JSON.parse(init.body) })
      if (githubMode === 'callback-fails') return new Response('{}', { status: 500 })
      return new Response(null, { status: 204 })
    }
    return fixture.fetch(url, init)
  }
  const app = createApp({
    webhookSecret: SECRET, appId: APP_ID, appPrivateKey: APP_KEY,
    boardToken: BOARD_TOKEN,
    boardOrigin: BOARD_ORIGIN, fetchImpl, mintToken, ...appOverrides,
  })
  const callsProxy = new Proxy(calls, { get: (t, k) => (k === 'mint' ? mints : t[k]) })
  return { app, posts, calls: callsProxy, requests, fixture,
    ready: Promise.resolve(), digest: () => HASH }
}

async function delivery(app, body, { event = EVENT, signature = undefined } = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  const headers = new Headers({
    'content-type': 'application/json',
    'x-github-event': event,
    'x-github-delivery': 'd-1',
  })
  const sig = signature === undefined ? await sign(SECRET, text) : signature
  if (sig !== null) headers.set('x-hub-signature-256', sig)
  return app(new Request('https://protection-rule.example.invalid/protection-rule', { method: 'POST', headers, body: text }))
}

function goComment(hash, overrides = {}) {
  return { id: 'c1', authorAgentId: CEO_AGENT_ID, authorUserId: null, body: `GO ${hash}`, ...overrides }
}

// Bind CEO GO to the actual Rust golden projection, not JSON bytes.
async function approvedHarness(options = {}, commentOverrides = {}) {
  const h = harness({ ...options, boardComments: [] })
  await h.ready
  const digest = h.digest()
  const comments = options.boardComments === undefined ? [goComment(digest, commentOverrides)] : options.boardComments
  // Rebuild with the real comments: same stub identity, real GO set.
  const h2 = harness({ ...options, boardComments: comments })
  await h2.ready
  return h2
}

// The artifact redirect hop crosses origins: followed without the token, and
// the approve row still verifies end to end.
test('artifact redirect to object storage verifies without sending the token cross-origin', async () => {
  const { app, posts, requests } = await approvedHarness({ githubMode: 'redirect-download' })
  const res = await delivery(app, payload())
  const body = await res.json()
  assert.equal(body.state, 'approved')
  assert.deepEqual(posts.map((p) => p.state), ['approved'])
  const objectHop = requests.find((r) => r.url === 'https://objects.example.invalid/zip')
  assert.ok(objectHop, 'redirect hop was followed')
  assert.equal(objectHop.authorization, null)
})

// The approve row: every link verified, GO present, `approved` posted once.
test('verified prior plan plus actual pre-gate claim ZIP and CEO GO posts approved once', async () => {
  const { app, posts, calls, fixture } = await approvedHarness()
  fixture.claimZip = new Uint8Array(readFileSync(new URL('./fixtures/staging-migrate-apply-claim.upload-artifact.zip', import.meta.url)))
  fixture.planZip = new Uint8Array(readFileSync(new URL('./fixtures/staging-migrate-plan.upload-artifact.zip', import.meta.url)))
  const res = await delivery(app, payload())
  assert.equal(res.status, 200)
  const body = await res.json()
  assert.equal(body.state, 'approved')
  assert.equal(body.reason, REASON.APPROVED)
  assert.equal(body.callback_posted, true)
  assert.equal(posts.length, 1)
  assert.equal(posts[0].state, 'approved')
  assert.equal(posts[0].environment_name, PLAN_ENVIRONMENT)
  assert.equal(calls.mint, 1)
})

// Source evidence rows: ancestry plus a CEO GO is not review. Each row serves a
// fully verified plan AND a posted GO, breaks exactly one source fact, and
// demands `rejected` with that fact's own stable reason.
test('the approve row reads CI and review on all three trusted commits before it reconfirms', async () => {
  const { app, posts, calls, requests } = await approvedHarness()
  const body = await (await delivery(app, payload())).json()
  assert.equal(body.state, 'approved')
  assert.equal(posts.length, 1)
  assert.equal(calls.pulls, 3)
  assert.equal(calls.checkRuns, 6)
  const index = (needle) => requests.map((r) => r.url).findLastIndex((url) => url.includes(needle))
  assert.ok(index('/pulls?') < index(`/actions/runs/${RUN_ID}/pending_deployments`), 'reconfirmation is the last read before the verdict')
})

const sourceRows = [
  ['required CI red on the migration source', REASON.SOURCE_CI_UNVERIFIED, (f) => { f.checkRuns[SOURCE].find((r) => r.name === 'ci-ok').conclusion = 'failure' }],
  ['required CI pending on the apply workflow head', REASON.SOURCE_CI_UNVERIFIED, (f) => { Object.assign(f.checkRuns[HEAD].find((r) => r.name === 'pr-lint'), { status: 'in_progress', conclusion: null }) }],
  ['required CI red on the producing plan head', REASON.SOURCE_CI_UNVERIFIED, (f) => { f.checkRuns[PLAN_HEAD].find((r) => r.name === 'gitleaks').conclusion = 'failure' }],
  ['no pull request for the migration source (pushed to main)', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.pulls[SOURCE] = [] }],
  ['no review on the migration source pull request head', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[SOURCE]] = [] }],
  ['review below 5/5 on the migration source', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[SOURCE]][0].output.title = '4/5' }],
  ['review by another App on the migration source', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[SOURCE]][0].app.id = 1 }],
  ['review postdating the merge of the migration source', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[SOURCE]][0].completed_at = '2026-10-08T00:00:00Z' }],
  ['no review on the apply workflow head', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[HEAD]] = [] }],
  ['no review on the producing plan head', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[PLAN_HEAD]] = [] }],
  ['stale review for another pull request head', REASON.SOURCE_REVIEW_UNVERIFIED, (f) => { f.checkRuns[PR_HEADS[SOURCE]][0].head_sha = PR_HEADS[HEAD] }],
]
for (const [name, reason, mutate] of sourceRows) {
  test(`${name} posts rejected even with a verified plan and CEO GO`, async () => {
    const h = await approvedHarness()
    mutate(h.fixture)
    const body = await (await delivery(h.app, payload())).json()
    assert.equal(body.state, 'rejected')
    assert.equal(body.reason, reason)
    assert.equal(body.callback_posted, true)
    assert.deepEqual(h.posts.map((p) => p.state), ['rejected'])
    assert.ok(h.posts[0].comment.includes(`(${reason})`))
  })
}

test('an unreadable pull-request read posts rejected, never approved', async () => {
  const h = await approvedHarness()
  const original = h.fixture.fetch
  h.fixture.fetch = async (url, init) => (String(url).includes('/pulls?') ? new Response('{}', { status: 500 }) : original(url, init))
  const body = await (await delivery(h.app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.SOURCE_REVIEW_UNVERIFIED)
  assert.deepEqual(h.posts.map((p) => p.state), ['rejected'])
})

test('source evidence is read only after the claim and plan verify, and never in sha mode', async () => {
  const noClaim = await approvedHarness({ githubMode: 'no-claim' })
  assert.equal((await (await delivery(noClaim.app, payload())).json()).reason, REASON.CLAIM_MISSING)
  assert.equal(noClaim.calls.pulls + noClaim.calls.checkRuns, 0)
  const noPlan = await approvedHarness({ githubMode: 'plan-missing-artifact' })
  assert.equal((await (await delivery(noPlan.app, payload())).json()).state, 'rejected')
  assert.equal(noPlan.calls.pulls + noPlan.calls.checkRuns, 0)
  const sha = harness({ boardCommentsByIssue: { [SHA_ISSUE]: [goComment(HEAD)] } })
  assert.equal((await (await delivery(sha.app, shaPayload())).json()).state, 'approved')
  assert.equal(sha.calls.pulls + sha.calls.checkRuns, 0)
})

test('a plan entry without source evidence refuses to start', () => {
  const { sourceEvidence, ...bare } = PLAN_POLICY
  assert.ok(sourceEvidence)
  assert.throws(() => harness({ appOverrides: { policies: [bare] } }))
})

// Rejection rows: each gap posts `rejected`, never `approved`.
test('missing CEO GO posts rejected', async () => {
  const { app, posts } = await approvedHarness({ boardComments: [] })
  const res = await delivery(app, payload())
  const body = await res.json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('unserved environment on a known repo is refused with 400 and posts nothing', async () => {
  const { app, posts, calls } = harness({ boardComments: [goComment(HASH)] })
  const res = await delivery(app, payload({ environment: 'staging' }))
  const body = await res.json()
  assert.equal(res.status, 400)
  assert.equal(posts.length, 0)
  assert.equal(calls.run, 0)
  void body
})

test('wrong event posts rejected', async () => {
  const { app, posts } = harness({ boardComments: [goComment(HASH)] })
  const res = await delivery(app, payload(), { event: 'push' })
  const body = await res.json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.WRONG_EVENT)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('missing claim posts rejected', async () => {
  const { app, posts } = await approvedHarness({ githubMode: 'no-claim' })
  const res = await delivery(app, payload())
  const body = await res.json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.CLAIM_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('plan on another commit posts rejected', async () => {
  const { app, posts } = await approvedHarness({ githubMode: 'plan-commit-mismatch' })
  const body = await (await delivery(app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.PLAN_NOT_FOUND)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('request SHA outside the fresh apply run posts rejected before claim verification', async () => {
  const { app, posts } = await approvedHarness({ githubMode: 'run-mismatch-sha' })
  const body = await (await delivery(app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.CLAIM_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('foreign repository is refused with 400 and posts nothing', async () => {
  const { app, posts } = harness({ boardComments: [goComment(HASH)] })
  const res = await delivery(app, payload({ repository: { full_name: 'SomeoneElse/other' } }))
  assert.equal(res.status, 400)
  assert.equal(posts.length, 0)
})

for (const [field, value] of [['action', 'completed'], ['event', 'push'], ['ref', 'feature'], ['sha', 'd'.repeat(40)]]) {
  test(`staging request ${field} mismatch rejects`, async () => {
    const { app, posts } = await approvedHarness()
    const body = await (await delivery(app, payload({ [field]: value }))).json()
    assert.equal(body.state, 'rejected')
    assert.deepEqual(posts.map((p) => p.state), ['rejected'])
  })
}

test('a rerun during GO scan cannot inherit the verified claim', async () => {
  const h = await approvedHarness()
  const original = h.fixture.fetch
  h.fixture.fetch = async (url, init) => {
    if (h.calls.board > 0) h.fixture.applyRun.run_attempt = 3
    return original(url, init)
  }
  const body = await (await delivery(h.app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.deepEqual(h.posts.map((p) => p.state), ['rejected'])
})

test('GO from another agent is not an approval', async () => {
  const { app, posts } = await approvedHarness({}, { authorAgentId: '00000000-0000-4000-8000-000000000019' })
  const body = await (await delivery(app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('GO from a human user is not an approval', async () => {
  const { app, posts } = await approvedHarness({}, { authorUserId: 'some-human' })
  const body = await (await delivery(app, payload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_MISSING)
})

test('unsigned delivery is 401 and reaches no transport', async () => {
  const { app, posts, calls } = harness({ boardComments: [goComment(HASH)] })
  const res = await delivery(app, payload(), { signature: null })
  assert.equal(res.status, 401)
  assert.equal(posts.length, 0)
  assert.equal(calls.mint, 0)
  assert.equal(calls.board, 0)
})

test('missing secret is 503 and mints nothing', async () => {
  const { app, posts, calls } = harness({ appOverrides: { webhookSecret: '' } })
  const res = await delivery(app, payload())
  assert.equal(res.status, 503)
  assert.equal(posts.length, 0)
  assert.equal(calls.mint, 0)
})

test('oversized body is 413 before verification', async () => {
  const { app, posts } = harness()
  const big = 'x'.repeat(MAX_ACCEPTED_BODY_BYTES + 1)
  const headers = new Headers({
    'content-type': 'application/json',
    'content-length': String(big.length),
    'x-github-event': EVENT,
    'x-github-delivery': 'd-big',
    'x-hub-signature-256': 'sha256=' + '0'.repeat(64),
  })
  const res = await app(new Request('https://protection-rule.example.invalid/protection-rule', { method: 'POST', headers, body: big }))
  assert.equal(res.status, 413)
  assert.equal(posts.length, 0)
})

test('mint failure reports rejected with no verdict posted', async () => {
  const { app, posts, calls } = harness({ githubMode: 'mint-fails', boardComments: [goComment(HASH)] })
  const res = await delivery(app, payload())
  const body = await res.json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.CLAIM_MISSING)
  assert.equal(body.callback_posted, false)
  assert.equal(posts.length, 0) // both mints failed, so no review could be delivered
  assert.equal(calls.mint, 2) // slow-path mint plus one best-effort rejection mint, same identity
})

// A callback URL naming another run is refused before any token travels: the
// run id in the URL must equal the run just reviewed. Slow path (up-front
// gate, no mint) and fast path (verdict-post binding, no post) both refuse.
test('callback URL naming another run is refused with 400 and posts nothing', async () => {
  const { app, posts, calls } = await approvedHarness()
  const other = `https://api.github.com/repos/${REPO}/actions/runs/999/deployment_protection_rule`
  const res = await delivery(app, payload({ deployment_callback_url: other }))
  const body = await res.json()
  assert.equal(res.status, 400)
  assert.equal(posts.length, 0)
  assert.equal(calls.mint, 0)
  void body
})

test('fast-path callback URL naming another run is refused with 400 and posts nothing', async () => {
  const { app, posts } = harness({ boardComments: [goComment(HASH)] })
  const other = `https://api.github.com/repos/${REPO}/actions/runs/999/deployment_protection_rule`
  const res = await delivery(app, payload({ environment: 'staging', deployment_callback_url: other }))
  const body = await res.json()
  assert.equal(res.status, 400)
  assert.equal(posts.length, 0)
  void body
})

// sha mode rows: production deploys carry no plan artifact. The GO binds the
// exact head SHA on the environment's own card — a GO on any other card
// approves nothing, and a GO on this card for another SHA approves nothing.
test('sha mode: CEO GO for the exact head SHA posts approved once', async () => {
  const { app, posts, calls } = harness({ boardCommentsByIssue: { [SHA_ISSUE]: [goComment(HEAD)] } })
  const res = await delivery(app, shaPayload())
  const body = await res.json()
  assert.equal(res.status, 200)
  assert.equal(body.state, 'approved')
  assert.equal(body.reason, REASON.APPROVED)
  assert.equal(body.callback_posted, true)
  assert.equal(posts.length, 1)
  assert.equal(posts[0].state, 'approved')
  assert.equal(posts[0].environment_name, SHA_ENVIRONMENT)
  assert.equal(calls.mint, 1)
})

test('sha mode: GO on the wrong card approves nothing', async () => {
  // The GO sits on the plan card, not this environment's card.
  const { app, posts } = harness({ boardCommentsByIssue: { [PLAN_ISSUE]: [goComment(HEAD)] } })
  const body = await (await delivery(app, shaPayload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_SHA_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('sha mode: GO for another SHA approves nothing', async () => {
  const { app, posts } = harness({ boardCommentsByIssue: { [SHA_ISSUE]: [goComment('d'.repeat(40))] } })
  const body = await (await delivery(app, shaPayload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_SHA_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('sha mode: a plan-hash GO does not approve a production delivery', async () => {
  // The SHA card carries only a 64-hex plan-hash GO: the sha scanner reads
  // nothing from it, so the delivery rejects.
  const { app, posts } = harness({ boardCommentsByIssue: { [SHA_ISSUE]: [goComment(HASH)] } })
  const body = await (await delivery(app, shaPayload())).json()
  assert.equal(body.state, 'rejected')
  assert.equal(body.reason, REASON.GO_SHA_MISSING)
  assert.deepEqual(posts.map((p) => p.state), ['rejected'])
})

test('health is unauthenticated and says nothing about configuration', async () => {
  const { app } = harness()
  const res = await app(new Request('https://protection-rule.example.invalid/health'))
  assert.equal(res.status, 200)
  assert.deepEqual(await res.json(), { ok: true, service: 'protection-rule' })
})

test('unknown path is 404', async () => {
  const { app } = harness()
  const res = await app(new Request('https://protection-rule.example.invalid/elsewhere'))
  assert.equal(res.status, 404)
})
