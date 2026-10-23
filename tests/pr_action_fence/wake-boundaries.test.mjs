// Event/tracker action wakes: planners and directly callable final transports.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createConnection } from 'node:net'
import { promises as dns } from 'node:dns'
import { createConsumer } from '../../gh-event-capture/src/consumer.js'
import { createPaperclipAdapter } from '../../gh-event-capture/src/paperclip-adapter.js'
import { claimKey, extractIssueRef } from '../../gh-event-capture/src/bridge.js'
import { agentPr } from '../../gh-event-capture/test/bridge-fixtures.mjs'
import { setup } from '../../plugins/pr-tracker/src/worker/setup.js'
import { COMPANY, HEAD, INVALID, ISSUE, NEW_HEAD, NOW, REPO,
  assertDenial, context, freeze, settled } from './node-support.mjs'

test('offline containment traps network and unauthorized subprocess fallback', () => {
  assert.throws(() => fetch('https://example.invalid'), /OFFLINE TRAP/)
  assert.throws(() => createConnection({ host: 'example.invalid', port: 443 }), /OFFLINE TRAP/)
  assert.throws(() => dns.lookup('example.invalid'), /OFFLINE TRAP/)
  assert.throws(() => execFileSync('gh', ['pr', 'merge', '1']), /OFFLINE TRAP/)
})

// Reuse the repository's existing opaque classifier fixture; never add board
// provenance or internal identifiers to a new public fixture.
const issueRef = extractIssueRef(agentPr.head.ref)
const issue = freeze({ id: ISSUE, identifier: issueRef, companyId: COMPANY,
  status: 'in_progress', assigneeAgentId: 'fixture', assigneeUserId: null, blockedBy: [] })

function captureHarness(repository = REPO, head = HEAD) {
  const comments = [], claims = [], products = []
  const pr = freeze({ ...structuredClone(agentPr), number: 1,
    title: 'Offline fixture', body: '', html_url: `https://github.com/${repository}/pull/1`,
    head: { ref: agentPr.head.ref, sha: head }, base: { repo: { full_name: repository } },
    state: 'open', draft: true, merged: false, reviewDecision: 'APPROVED' })
  const stored = { delivery_id: 'fixture-evidence', body_truncated: 0, event: 'pull_request',
    body: JSON.stringify({ action: 'opened', repository: { full_name: repository }, pull_request: pr }) }
  const board = {
    async getIssue() { return structuredClone(issue) },
    async listWorkProducts() { return products },
    async listInteractions() { return [] },
    async createWorkProduct(_id, body) { const p = { id: 'fixture-product', ...body }; products.push(p); return p },
    async updateWorkProduct(id, body) { Object.assign(products.find((p) => p.id === id), body); return products[0] },
    async commentIfEligible(...args) { comments.push(args); return { sent: true, comment: { id: 'fixture-comment' } } },
  }
  const capture = {
    async getPullRequestDelivery() { return stored },
    async claim(value) { claims.push(value); return { ok: true, claimed: true,
      claim_key: claimKey({ issueRef: value.issue_ref, headSha: value.head_sha, kind: value.kind }) } },
  }
  const consumer = createConsumer({ github: { getPullRequest: async () => structuredClone(pr) },
    board, capture, allowedRepositories: [repository] })
  const delivery = (eventHead = head) => ({ event: 'check_suite', delivery_id: 'fixture-suite', body_truncated: 0,
    body: JSON.stringify({ action: 'completed', repository: { full_name: repository },
      check_suite: { status: 'completed', conclusion: 'success', head_sha: eventHead, pull_requests: [{ number: 1 }] } }) })
  return { consumer, delivery, comments, claims, products }
}

test('capture event sender refuses protected action comment, including repeat/future green', async () => {
  for (const head of [HEAD, NEW_HEAD]) {
    const h = captureHarness(REPO, head)
    const first = await h.consumer.processDelivery(h.delivery())
    await h.consumer.processDelivery(h.delivery())
    assert.equal(h.comments.length, 0, 'event capture dispatched a protected action comment')
    assertDenial(first)
  }
})

test('capture delayed old-head delivery performs diagnostics but never wakes', async () => {
  const h = captureHarness(REPO, NEW_HEAD)
  await h.consumer.processDelivery(h.delivery(HEAD))
  assert.equal(h.comments.length, 0)
})

test('capture unrelated positive control actually reaches its final comment', async () => {
  const h = captureHarness('example/other')
  await h.consumer.processDelivery(h.delivery())
  assert.equal(h.comments.length, 1)
  assert.equal(h.claims.length, 1)
})

async function boardHarness(repository = REPO, options = {}) {
  const posts = [], reads = []
  const adapter = createPaperclipAdapter({ baseUrl: 'https://board.example.invalid',
    token: 'offline-placeholder', commentToken: 'offline-comment-placeholder',
    companyId: COMPANY, allowedRepositories: [repository], ...options,
    fetchImpl: async (url, init) => {
      if (init.method !== 'GET') posts.push({ url, body: JSON.parse(init.body) })
      else reads.push(url)
      const data = url.endsWith('/interactions') ? []
        : init.method === 'POST' ? { id: 'fixture-comment' } : structuredClone(issue)
      return new Response(JSON.stringify(data), { status: init.method === 'POST' ? 201 : 200,
        headers: { 'content-type': 'application/json' } })
    } })
  await adapter.getIssue(ISSUE)
  return { adapter, posts, reads }
}

test('capture direct board sender has an independent final guard', async () => {
  const h = await boardHarness()
  const result = await settled(() => h.adapter.commentIfEligible(ISSUE,
    { body: 'Offline PR action fixture' }, context()))
  assert.equal(h.posts.length, 0, 'consumer-only guard left board transport bypassable')
  assertDenial(result)
})

test('capture direct sender unrelated positive retains non-reopen semantics', async () => {
  const repository = 'example/other'
  const h = await boardHarness(repository)
  await h.adapter.commentIfEligible(ISSUE, { body: 'Offline PR action fixture' }, context(repository))
  assert.equal(h.posts.length, 1)
  assert.deepEqual(h.posts[0].body, { body: 'Offline PR action fixture', reopen: false, resume: false })
})

function trackerHarness(repository = REPO, policyRun = undefined) {
  const wakes = [], store = {}
  let head = HEAD
  const key = `${repository}#1`
  const tracking = freeze({ repository, number: 1, issueId: ISSUE, identifier: 'fixture',
    cardOpen: true, title: 'Offline fixture', prUrl: `https://github.com/${repository}/pull/1`,
    kind: 'org', uiStatus: 'ours-to-do', owner: 'fixture', openedAtMs: NOW - 86400000 })
  const snap = () => ({ repository, number: 1, state: 'open', merged: false, headSha: head,
    ci: { rollup: 'green', sinceMs: null }, mergeable: 'mergeable', reviewDecision: 'approved',
    threads: [], comments: [], lastOurResponseAtMs: null, lastMaintainerAtMs: NOW - 3600000,
    fetchedAtMs: NOW, readOk: true })
  const ctx = {
    jobs: { register() {} },
    issues: { async requestWakeup(...args) { wakes.push(args); return { ok: true } } },
    secrets: { async resolve() { return '' } },
    state: { async get() { return store.value ?? null }, async set(value) { store.value = value.state } },
  }
  const api = setup(ctx, { companyId: COMPANY, configPath: 'offline',
    secretRef: { type: 'secret_ref', secretId: 'offline-placeholder', version: 'latest' },
    scope: { upstream: [], org: [repository] }, policyRun,
    collect: async () => [{ key, status: 200, etag: 'fixture-etag',
      input: { prev: null, next: snap(), tracking: structuredClone(tracking), nowMs: NOW } }] })
  return { api, wakes, key, store, push: () => { head = NEW_HEAD }, tracking }
}

test('tracker automatic digest final wake is fenced after a hypothetical new head', async () => {
  const h = trackerHarness()
  await h.api.tick()
  h.push()
  const result = await h.api.tick()
  assert.equal(h.wakes.length, 0, 'tracker automatic requestWakeup bypassed review-only subject fence')
  assertDenial(result)
})

test('tracker manual wake final sender cannot bypass the subject fence', async () => {
  const h = trackerHarness()
  await h.api.tick()
  const result = await settled(() => h.api.performAction({ action: 'wake', key: h.key }))
  assert.equal(h.wakes.length, 0, 'tracker manual action bypassed automatic-path filtering')
  assertDenial(result)
})

test('tracker direct deliver rejects a pre-policy digest, independently of polling', async () => {
  const h = trackerHarness()
  const result = await settled(() => h.api.deliver({ action: 'digest', repository: REPO, number: 1,
    issueId: ISSUE, identifier: 'fixture', dedupeKey: 'fixture-queued-digest', reopen: false,
    headSha: HEAD, expectedHead: HEAD, observedHead: HEAD, policyDigest: '0'.repeat(64),
    changes: [{ kind: 'head', detail: 'offline head change' }] }))
  assert.equal(h.wakes.length, 0, 'direct tracker deliver trusted queued action as permission')
  assertDenial(result)
})

test('tracker unrelated automatic and manual positive controls really deliver', async () => {
  const h = trackerHarness('example/other')
  await h.api.tick()
  h.push()
  await h.api.tick()
  await h.api.performAction({ action: 'wake', key: h.key })
  assert.equal(h.wakes.length, 2, 'a blanket deny is not a shared fence')
})

for (const [name, policyRun] of [
  ['absent', async () => { throw Object.assign(new Error('helper absent'), { code: 'ENOENT' }) }],
  ['timeout', async () => { throw Object.assign(new Error('helper timeout'), { code: 'ETIMEDOUT' }) }],
  ['malformed', async () => ({ stdout: 'not-json', stderr: '' })],
]) {
  test(`tracker final manual sender fails closed on ${name} helper even for unrelated input`, async () => {
    const h = trackerHarness('example/other', policyRun)
    await h.api.tick()
    const result = await settled(() => h.api.performAction({ action: 'wake', key: h.key }))
    assert.equal(h.wakes.length, 0)
    assertDenial(result, INVALID)
  })
  test(`capture direct sender fails closed on ${name} helper even for unrelated input`, async () => {
    const h = await boardHarness('example/other', { policyRun })
    const result = await settled(() => h.adapter.commentIfEligible(ISSUE,
      { body: 'Offline PR action fixture' }, context('example/other')))
    assert.equal(h.posts.length, 0)
    assertDenial(result, INVALID)
  })
}
