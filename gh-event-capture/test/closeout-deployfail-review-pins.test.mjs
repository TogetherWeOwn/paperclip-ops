import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { runProductPass } from '../src/consumer-runner.js'
import { receiptNamespace } from '../src/receipt-cycle.js'
import { loadSweepState } from '../src/sweep-store.js'
import { createApp } from '../src/app.js'
import { createMemoryStore } from '../src/store-memory.js'

// Builder-owned review pins for the deployment-failure shadow observer.
// Not protected acceptance: these pin runner integration order (success
// drains before failure) and catch-path emission that the acceptance
// contract covers for siblings but did not pin for this observer.
// No live services.
const A = 'ExampleOrg/widget'
const wall = Date.parse('2026-10-20T12:00:00Z')
const privateText = 'UNTRUSTED-DEPLOY-PROSE'
const safeLog = `https://logs.example.test/${privateText}`
const sha = 'a'.repeat(40)
const enrolled = (repos = [A]) => ({ deployFailShadow: { enabledRepositories: repos } })
const failPayload = (over = {}) => ({ repository: { full_name: A }, deployment_status: { state: 'failure',
  description: privateText, log_url: safeLog },
  deployment: { sha, environment: 'production', description: privateText }, ...over })
const successPayload = (over = {}) => ({ repository: { full_name: A }, deployment_status: { state: 'success' },
  deployment: { sha, environment: 'staging' }, ...over })
const delivery = (id = 'deployfail-1', body = failPayload(), over = {}) => ({ delivery_id: id,
  repository: A, event: 'deployment_status', received_ms: wall - 1000, body_truncated: 0,
  body: JSON.stringify(body), ...over })

async function fixture(t, { closeout, repos = [A], open = { [A]: [7] }, limits = {}, rows = [delivery()] } = {}) {
  t.mock.method(Date, 'now', () => wall)
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'deployfail-pins-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.example.test',
    boardOrigin: 'https://board.example.test', companyId: '00000000-0000-4000-8000-000000000009',
    allowedRepositories: repos, stateDirectory: join(root, 'state'), captureTokenFile: join(root, 'capture.key'),
    boardTokenFile: join(root, 'board.key'), limits, ...(closeout === undefined ? {} : { closeout }) }
  const store = createMemoryStore()
  for (const row of rows) await store.append(row)
  const app = createApp({ store, webhookSecret: 'fixture', queryToken: 'fixture', now: () => wall })
  const h = { root, config, calls: [], logs: [], fetchHook: null, primaryHook: null }
  h.fetch = async (url, init) => {
    h.calls.push(['http', init.method, url])
    assert.equal(init.method, 'GET', 'observer must never claim, create tasks or wake')
    assert.equal(new URL(url).origin, config.captureOrigin, 'no board or other service reads with failure observation alone')
    assert.equal(init.signal.aborted, false)
    if (h.fetchHook) { const result = await h.fetchHook(new URL(url), init); if (result) return result }
    return app(new Request(url, init))
  }
  const envelope = (repository, fields) => ({ data: { repository: { nameWithOwner: repository, ...fields } } })
  h.run = async (file, args, options) => {
    assert.equal(file, 'fixture-gh')
    assert.equal(options.shell, false)
    assert.deepEqual(args.slice(0, 3), ['api', '--hostname', 'github.com'])
    if (args[3] !== 'graphql') {
      assert.match(args[3], /^repos\/ExampleOrg\/(widget|gadget)\/branches\/main\/protection$/)
      h.calls.push(['protection'])
      return { stdout: JSON.stringify({ required_status_checks: { contexts: ['ci-ok'], checks: [] } }) }
    }
    const query = args[5]
    const repository = `${args.find((v) => v.startsWith('owner=')).slice(6)}/${args.find((v) => v.startsWith('name=')).slice(5)}`
    if (query.includes('BridgeOpenPullRequests')) {
      h.calls.push(['list', repository])
      const numbers = open[repository] ?? []
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: numbers.length,
        nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false, endCursor: null } } })) }
    }
    if (query.includes('BridgeMainHead')) {
      h.calls.push(['main', repository])
      return { stdout: JSON.stringify(envelope(repository, { defaultBranchRef: { name: 'main', target: {
        __typename: 'Commit', oid: sha, pushedDate: '2026-10-20T09:00:00Z',
        statusCheckRollup: { contexts: { nodes: [{ __typename: 'CheckRun',
          name: 'ci-ok', status: 'COMPLETED', conclusion: 'FAILURE', completedAt: '2026-10-20T09:01:00Z' }], pageInfo: { hasNextPage: false } } } } } })) }
    }
    const snapshot = query.includes('BridgeCloseoutSnapshot')
    assert.ok(snapshot || query.includes('BridgePullRequest'), 'no writes or other GitHub operations')
    h.calls.push([snapshot ? 'snapshot' : 'primary', repository])
    if (!snapshot && h.primaryHook) await h.primaryHook(options)
    return { stdout: JSON.stringify(envelope(repository, { isPrivate: false, pullRequest: {
      number: 7, url: `https://github.com/${repository}/pull/7`, title: privateText, body: privateText,
      state: 'OPEN', isDraft: false, merged: false, headRefName: 'feature-unmapped', headRefOid: sha,
      baseRefName: 'main', reviewDecision: null, mergeStateStatus: 'DIRTY', autoMergeRequest: { enabledAt: 'unused' },
      author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: repository },
      labels: { nodes: [], pageInfo: { hasNextPage: false } }, commits: { nodes: [{ commit: { oid: sha,
        pushedDate: '2026-10-20T09:00:00Z', statusCheckRollup: { contexts: { nodes: [],
          pageInfo: { hasNextPage: false } } } } }] },
    } })) }
  }
  h.pass = (options = {}) => runProductPass({ config, captureToken: 'fixture', boardToken: 'fixture',
    githubExecutable: 'fixture-gh' }, { fetchImpl: h.fetch, run: h.run,
    log: (line) => h.logs.push(JSON.parse(line)), ...options })
  h.state = () => loadSweepState({ directory: config.stateDirectory, namespace: receiptNamespace(config) })
  h.summaryLogs = () => h.logs.filter((line) => line.event === 'closeout-deployfail-shadow')
  return h
}

// Pins docs/deploy-shadow.md: the failure observer drains after the success
// observer. With 4 primary requests and 3 for one observer over two
// deliveries, maxRequests=7 lets the documented-first success observer
// consume the surplus and defers the failure observer. A swap mutant
// (failure first) flips wouldRecord/deferred and must fail this test.
test('success-before-failure drain order pins scarce-surplus priority', async (t) => {
  const rows = [delivery('fail-1'), delivery('ok-1', successPayload())]
  const h = await fixture(t, { closeout: { deployShadow: { enabledRepositories: [A] }, ...enrolled() },
    limits: { maxRequests: 7 }, rows })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.reason, null)
  assert.equal(result.requests, 7)
  assert.equal(result.deployShadow.counters.wouldRecord, 1)
  assert.equal(result.deployShadow.counters.deferred, 0)
  assert.equal(result.deployShadow.counters.nonSuccess, 1, 'success drain read both deliveries')
  assert.equal(result.deployFailShadow.counters.wouldRecord, 0)
  assert.equal(result.deployFailShadow.counters.deferred, 1)
  assert.equal(result.deployFailShadow.counters.errors, 0)
  assert.equal(h.summaryLogs().length, 1)
  assert.equal(h.logs.filter((line) => line.event === 'closeout-deploy-shadow').length, 1)
})

// Analogue of the red-main catch-path emit test: an unstopped cycle-failed
// pass still emits exactly one failure summary with empty counters.
// Removing deployFailShadow from the catch-path emit loop must fail this test.
test('cycle-failed catch path emits one empty failure summary', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.deployFailShadow.counters.wouldRecord, 1)
  h.logs.length = 0
  await writeFile(join(h.config.stateDirectory, 'schedule.json'), privateText, { mode: 0o600 })
  const broken = await h.pass()
  assert.equal(broken.ok, false)
  assert.equal(broken.reason, 'cycle-failed')
  assert.equal(broken.deployFailShadow.counters.total, 0)
  assert.equal(h.summaryLogs().length, 1, 'catch path emits once when no whole-pass stop exists')
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
})
