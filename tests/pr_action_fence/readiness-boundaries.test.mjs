// Real readiness planners, event runner, sweep and BOTH independent final arms.
// The mutation oracles sit below production code. They never implement a fence.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { decideReadinessActions, createReconciler } from '../../gh-event-capture/src/readiness-reconcile.js'
import { createGithubAdapter } from '../../gh-event-capture/src/github-adapter.js'
import { armPolicy, decideArm, createArmer, createAutoMergeArmer } from '../../gh-event-capture/src/closeout-arm.js'
import { runSweep } from '../../gh-event-capture/scripts/readiness-sweep.mjs'
import { CHECKS, DENY, HEAD, INVALID, ISSUE, NEW_HEAD, NOW, REPO, REQUIRED,
  assertDenial, context, ghSpy, readinessDouble, scratch, settled, snapshot } from './node-support.mjs'

const gate = (repository = REPO) => ({ repository, branch: 'main',
  review: { context: 'Paperclip Review', appId: 123 }, requiredChecks: [...REQUIRED],
  allowAutoMerge: true, mergeMethods: ['squash'] })
const policy = (repository = REPO) => ({ enabledRepositories: [repository], holdLabels: ['hold'] })
const arm = armPolicy({ reviewAppId: 123, requiredCi: [...REQUIRED] })
const input = (snap) => ({ snapshot: snap, body: snap.body, requiredChecks: [...REQUIRED], nowMs: NOW })
const refs = (repository = REPO, number = 1) => [{ repository, number }]

for (const [name, overrides] of [
  ['green draft', { draft: true }], ['already ready with future five-of-five', { draft: false }],
  ['ordinary HOLD that still permits body maintenance', { labels: ['hold'] }],
  ['new head with future five-of-five', { headSha: NEW_HEAD }],
  ['red ready record otherwise converted to draft', { checks: [{ name: 'ci-ok', status: 'completed', conclusion: 'failure', completedMs: 1 }] }],
]) {
  test(`readiness generation: ${name} produces no protected write plan`, () => {
    const decision = decideReadinessActions(input(snapshot(REPO, 1, overrides)), policy())
    assert.deepEqual(decision.actions, [], 'protected readiness generator emitted a write plan')
    assertDenial(decision)
  })
  test(`readiness final consumption: ${name}, including duplicate event`, async () => {
    const h = readinessDouble(snapshot(REPO, 1, overrides))
    const reconciler = createReconciler({ github: h.github, policy: policy() })
    const first = await reconciler.runReconcile({ references: refs(), nowMs: NOW })
    const second = await reconciler.runReconcile({ references: refs(), nowMs: NOW })
    assert.equal(h.writes.length, 0, 'protected readiness event reached a mutation boundary')
    assertDenial([first, second])
  })
}

test('pre-existing auto-merge is a finding/HOLD, not already-compliant success', async () => {
  const snap = snapshot(REPO, 1, { autoMerge: true })
  const h = readinessDouble(snap)
  const ready = await createReconciler({ github: h.github, policy: policy() })
    .runReconcile({ references: refs(), nowMs: NOW })
  const decided = decideArm({ snapshot: snap, gate: gate(), policy: arm })
  assert.equal(h.writes.length, 0, 'existing remote request must not permit even body maintenance')
  assertDenial([ready, decided])
  assert.match(JSON.stringify([ready, decided]), /finding|hold|pre-existing|auto.merge/i)
  assert.notEqual(decided.reason, 'already-armed', 'a remote promotion request is not compliance')
})

for (const method of ['updatePullRequestBody', 'markReadyForReview', 'convertToDraft', 'enableAutoMerge']) {
  const invoke = (adapter, repository, number, ctx) => method === 'updatePullRequestBody'
    ? adapter[method](repository, number, 'Offline body fixture', ctx)
    : method === 'enableAutoMerge' ? adapter[method](repository, number, HEAD, ctx)
      : adapter[method](repository, number, ctx)

  test(`direct writer ${method}: bypassing planner still denies`, async () => {
    const h = ghSpy()
    const adapter = createGithubAdapter({ allowedRepositories: [REPO], run: h.run })
    const result = await settled(() => invoke(adapter, REPO, 1, context()))
    assert.equal(h.writes.length, 0, `missing independent final guard in ${method}`)
    assertDenial(result)
  })

  test(`direct writer ${method}: unrelated valid control reaches exact original boundary`, async () => {
    const h = ghSpy('example/other')
    const adapter = createGithubAdapter({ allowedRepositories: ['example/other'], run: h.run })
    await invoke(adapter, 'example/other', 1, context('example/other'))
    assert.equal(h.writes.length, 1, 'blanket deny or broken transport would make this control fail')
    if (method === 'enableAutoMerge') {
      assert.ok(h.writes[0].includes('--auto'))
      assert.equal(h.writes[0][h.writes[0].indexOf('--match-head-commit') + 1], HEAD)
    }
  })

  for (const [errorName, policyRun] of [
    ['absent helper', async () => { const e = new Error('helper absent'); e.code = 'ENOENT'; throw e }],
    ['runtime failure', async () => { throw new Error('helper failed') }],
    ['malformed decision', async () => ({ stdout: '{bad json', stderr: '' })],
    ['timeout', async () => { const e = new Error('helper timeout'); e.code = 'ETIMEDOUT'; throw e }],
    ['nonzero exit', async () => ({ stdout: '{"decision":"NOT_RESTRICTED"}', stderr: '', status: 20, code: 20 })],
  ]) {
    test(`direct writer ${method}: ${errorName} fails closed for unrelated subject too`, async () => {
      const h = ghSpy('example/other')
      const adapter = createGithubAdapter({ allowedRepositories: ['example/other'], run: h.run, policyRun })
      const result = await settled(() => invoke(adapter, 'example/other', 1, context('example/other')))
      assert.equal(h.writes.length, 0, `${method} ignored helper failure`)
      assertDenial(result, INVALID)
    })
  }

  test(`direct writer ${method}: missing trusted head context is denial, not a legacy bypass`, async () => {
    const h = ghSpy('example/other')
    const adapter = createGithubAdapter({ allowedRepositories: ['example/other'], run: h.run })
    const result = await settled(() => method === 'enableAutoMerge'
      ? adapter.enableAutoMerge('example/other', 1, undefined)
      : invoke(adapter, 'example/other', 1, undefined))
    assert.equal(h.writes.length, 0)
    assertDenial(result, INVALID)
  })

  test(`direct writer ${method}: head/policy drift invalidates queued work`, async () => {
    for (const overrides of [{ observedHead: NEW_HEAD }, { policyDigest: '0'.repeat(64) }]) {
      const h = ghSpy('example/other', 1, overrides.observedHead ?? HEAD)
      const adapter = createGithubAdapter({ allowedRepositories: ['example/other'], run: h.run })
      const result = await settled(() => invoke(adapter, 'example/other', 1, context('example/other', 1, overrides)))
      assert.equal(h.writes.length, 0, 'cached planner receipt incorrectly acted as permission')
      assertDenial(result, INVALID)
    }
  })
}

for (const repository of [REPO, 'TOGETHERWEOWN/CLIProxyAPI']) {
  test(`closeout separate planner denies ${repository}`, () => {
    const result = decideArm({ snapshot: snapshot(repository), gate: gate(repository), policy: arm })
    assert.equal(result.armed, false, 'independent closeout planner authorized the restricted subject')
    assertDenial(result)
  })
  test(`closeout separate final armer denies ${repository} even when called directly`, async () => {
    const h = ghSpy(repository)
    const armer = createAutoMergeArmer({ run: h.run })
    const result = await settled(() => armer(repository, 1, HEAD, context(repository)))
    assert.equal(h.writes.length, 0, 'independent --auto arm can merge immediately')
    assertDenial(result)
  })
}

for (const [name, policyRun] of [
  ['missing', async () => { throw Object.assign(new Error('helper absent'), { code: 'ENOENT' }) }],
  ['failure', async () => { throw new Error('helper failed') }],
  ['timeout', async () => { throw Object.assign(new Error('helper timeout'), { code: 'ETIMEDOUT' }) }],
  ['invalid-output', async () => ({ stdout: 'not-json', stderr: '' })],
  ['nonzero', async () => ({ stdout: '{"decision":"NOT_RESTRICTED"}', status: 20, code: 20 })],
]) {
  test(`independent closeout final armer fails closed on ${name} helper`, async () => {
    const h = ghSpy('example/other')
    const writer = createAutoMergeArmer({ run: h.run, policyRun })
    const result = await settled(() => writer('example/other', 1, HEAD, context('example/other')))
    assert.equal(h.writes.length, 0, 'independent armer ignored evaluator failure')
    assertDenial(result, INVALID)
  })
}

test('independent closeout final armer quarantines stale head and policy digest', async () => {
  for (const overrides of [{ observedHead: NEW_HEAD }, { policyDigest: '0'.repeat(64) }]) {
    const h = ghSpy('example/other', 1, overrides.observedHead ?? HEAD)
    const writer = createAutoMergeArmer({ run: h.run })
    const result = await settled(() => writer('example/other', 1, HEAD, context('example/other', 1, overrides)))
    assert.equal(h.writes.length, 0)
    assertDenial(result, INVALID)
  }
})

test('closeout runArm opens no misleading merge confirmation after refusal', async () => {
  const writes = [], confirmations = []
  const result = await createArmer({
    github: { getCloseoutSnapshot: async () => snapshot(), getBranchGate: async () => gate() },
    armAutoMerge: async (...args) => { writes.push(args) },
    openConfirmation: async (value) => { confirmations.push(value) }, policy: arm,
  }).runArm({ references: refs(), issueId: ISSUE, nowMs: NOW })
  assert.equal(writes.length, 0)
  assert.equal(confirmations.length, 0, 'denial must precede both arm and confirmation')
  assertDenial(result)
})

test('closeout unrelated control retains exact-head squash arm and original HOLD gate', async () => {
  const repository = 'example/other'
  const h = ghSpy(repository)
  const armer = createAutoMergeArmer({ run: h.run })
  await armer(repository, 1, HEAD, context(repository))
  assert.deepEqual(h.writes, [['pr', 'merge', '1', '--repo', repository,
    '--auto', '--squash', '--match-head-commit', HEAD]])
  const held = decideArm({ snapshot: snapshot(repository, 1, { labels: ['hold'] }), gate: gate(repository), policy: arm })
  assert.equal(held.armed, false)
  assert.equal(held.reason, 'held')
})

test('sweep entry: manually injected mirror, enabled fixture only, produces no writes', async (t) => {
  const dir = await scratch(t)
  const file = join(dir, 'sweep-policy.json')
  await writeFile(file, JSON.stringify({ version: 1, ghExecutable: '/offline/gh',
    enabledRepositories: [REPO], scanRepositories: [REPO], holdLabels: ['hold'] }), { mode: 0o600 })
  const h = readinessDouble(snapshot(REPO, 1, { draft: true }))
  const result = await runSweep({ policyPath: file, github: h.github, nowMs: NOW, env: {} })
  assert.equal(h.writes.length, 0, 'sweep may not depend on inventory omission or invocation mode')
  assertDenial(result)
})

test('sweep unrelated positive control is not vacuously read-only', async (t) => {
  const repository = 'example/other'
  const dir = await scratch(t)
  const file = join(dir, 'sweep-policy.json')
  await writeFile(file, JSON.stringify({ version: 1, ghExecutable: '/offline/gh',
    enabledRepositories: [repository], scanRepositories: [repository], holdLabels: ['hold'] }), { mode: 0o600 })
  const h = readinessDouble(snapshot(repository, 1, { draft: true }))
  await runSweep({ policyPath: file, github: h.github, nowMs: NOW, env: {} })
  assert.deepEqual(h.writes.map(([method]) => method), ['updatePullRequestBody', 'markReadyForReview'])
})

test('readiness unrelated subjects preserve valid behavior and original HOLD', async () => {
  for (const [repository, number] of [['example/other', 1], [REPO, 2], [REPO, 10], [REPO, 11], ['TogetherWeOwn/CLIProxyAPI-extra', 1]]) {
    const h = readinessDouble(snapshot(repository, number))
    await createReconciler({ github: h.github, policy: policy(repository) })
      .runReconcile({ references: refs(repository, number), nowMs: NOW })
    assert.ok(h.writes.some(([name]) => name === 'enableAutoMerge'), 'valid positive must reach its arm')
    const held = decideReadinessActions(input(snapshot(repository, number, { labels: ['hold'] })), policy(repository))
    assert.ok(!held.actions.includes('mark-ready') && !held.actions.includes('enable-automerge'))
  }
})

test('disable-only adapter remains a separate permitted cleanup seam, never an enable fallback', async () => {
  const h = ghSpy()
  const adapter = createGithubAdapter({ allowedRepositories: [REPO], run: h.run })
  await adapter.disableAutoMerge(REPO, 1)
  assert.deepEqual(h.writes, [['pr', 'merge', '1', '--repo', REPO, '--disable-auto']])
})
