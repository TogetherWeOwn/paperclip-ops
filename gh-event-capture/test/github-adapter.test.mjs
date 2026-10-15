import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { createGithubAdapter } from '../src/github-adapter.js'
import { createConsumer } from '../src/consumer.js'
import { classifyCloseout, closeoutPolicy } from '../src/closeout.js'

const repository = 'TogetherWeOwn/kofra'
const sha = 'a'.repeat(40)
const pr = () => ({ number: 42, url: `https://github.com/${repository}/pull/42`,
  title: 'Bridge PR', body: '', state: 'OPEN', isDraft: false, merged: false,
  headRefName: 'case_22b092f5-bridge', headRefOid: sha, reviewDecision: null,
  author: { __typename: 'Bot', login: 'togetherweown' }, baseRepository: { nameWithOwner: repository } })
const envelope = (fields) => ({ data: { repository: { nameWithOwner: repository, ...fields } } })
const page = (numbers, hasNextPage = false, endCursor = null, totalCount = numbers.length) => envelope({
  pullRequests: { nodes: numbers.map((number) => ({ number })), totalCount, pageInfo: { hasNextPage, endCursor } },
})
function fake(responses, options = {}) {
  const calls = []
  const adapter = createGithubAdapter({ allowedRepositories: [repository], ...options,
    async run(...args) {
      calls.push(args)
      assert.ok(responses.length, 'unexpected extra GitHub request')
      const response = responses.shift()
      if (response instanceof Error) throw response
      return { stdout: JSON.stringify(response) }
    },
  })
  return { adapter, calls }
}

test('PR adapter queries lifecycle and reviews together through bounded shell-free gh', async () => {
  const { adapter, calls } = fake([envelope({ pullRequest: pr() })])
  assert.deepEqual(await adapter.getPullRequest(repository, 42), {
    number: 42, html_url: pr().url, title: 'Bridge PR', body: '',
    state: 'open', draft: false, merged: false, user: { type: 'Bot', login: 'togetherweown[bot]' },
    head: { ref: 'case_22b092f5-bridge', sha }, base: { repo: { full_name: repository } }, reviewDecision: null,
  })
  assert.equal(calls.length, 1)
  const [file, args, options] = calls[0]
  assert.equal(file, 'gh')
  assert.deepEqual(args.slice(0, 4), ['api', '--hostname', 'github.com', 'graphql'])
  const query = args[5]
  for (const field of ['headRefOid', 'reviewDecision', 'state', 'author', 'body', 'baseRepository']) assert.ok(query.includes(field))
  assert.ok(args.includes('number=42'))
  assert.ok(args.includes('owner=TogetherWeOwn'))
  assert.ok(args.includes('name=kofra'))
  assert.deepEqual(options, { encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4194304 })
  assert.equal(args.some((arg) => /token|authorization/i.test(arg)), false)
})

test('all lifecycles and review verdicts normalize without inventing missing reviews', async () => {
  for (const state of ['OPEN', 'CLOSED', 'MERGED']) {
    for (const reviewDecision of [null, 'REVIEW_REQUIRED', 'APPROVED', 'CHANGES_REQUESTED']) {
      const input = { ...pr(), state, merged: state === 'MERGED', reviewDecision, isDraft: state === 'OPEN' }
      const { adapter } = fake([envelope({ pullRequest: input })])
      const result = await adapter.getPullRequest(repository, 42)
      assert.equal(result.state, state === 'OPEN' ? 'open' : 'closed')
      assert.equal(result.merged, state === 'MERGED')
      assert.equal(result.draft, state === 'OPEN')
      assert.equal(result.reviewDecision, reviewDecision)
    }
  }
})

test('Bot login normalization does not promote users or deleted authors', async () => {
  for (const [author, expected] of [
    [{ __typename: 'Bot', login: 'togetherweown' }, { type: 'Bot', login: 'togetherweown[bot]' }],
    [{ __typename: 'Bot', login: 'togetherweown[bot]' }, { type: 'Bot', login: 'togetherweown[bot]' }],
    [{ __typename: 'User', login: 'togetherweown' }, { type: 'User', login: 'togetherweown' }],
    [null, null],
  ]) {
    const { adapter } = fake([envelope({ pullRequest: { ...pr(), author } })])
    assert.deepEqual((await adapter.getPullRequest(repository, 42)).user, expected)
  }
})

test('partial/mismatched PR fields, reviews and author types fail closed', async () => {
  const cases = [null, {}, { ...pr(), number: 43 }, { ...pr(), url: 'https://example.com/42' },
    { ...pr(), baseRepository: { nameWithOwner: 'Other/repo' } }, { ...pr(), headRefOid: 'abc' },
    { ...pr(), headRefName: null }, { ...pr(), title: '' }, { ...pr(), body: null },
    { ...pr(), state: 'unknown' }, { ...pr(), merged: true }, { ...pr(), state: 'MERGED' },
    { ...pr(), isDraft: null }, { ...pr(), reviewDecision: 'unreviewed' },
    { ...pr(), author: {} }, { ...pr(), author: { __typename: 'User', login: 'togetherweown[bot]' } },
    { ...pr(), author: { __typename: 'Unknown', login: 'togetherweown' } },
    { ...pr(), author: { __typename: 'Bot', login: 'bad/login' } },
  ]
  for (const key of ['reviewDecision', 'author', 'body', 'headRefOid', 'merged', 'state']) {
    const missing = pr(); delete missing[key]; cases.push(missing)
  }
  for (const input of cases) {
    const { adapter, calls } = fake([envelope({ pullRequest: input })])
    await assert.rejects(adapter.getPullRequest(repository, 42))
    assert.equal(calls.length, 1)
  }
})

test('HTTP-200 GraphQL errors and incomplete envelopes do not become empty successful data', async () => {
  for (const response of [null, [], {}, { errors: [{ message: 'PRIVATE' }], ...envelope({ pullRequest: pr() }) },
    { errors: 'PRIVATE', ...envelope({ pullRequest: pr() }) }, { errors: null, ...envelope({ pullRequest: pr() }) },
    { data: { repository: null } }, { data: { repository: { nameWithOwner: 'Other/repo', pullRequest: pr() } } }]) {
    const { adapter } = fake([response])
    await assert.rejects(adapter.getPullRequest(repository, 42), (error) => {
      assert.equal(String(error).includes('PRIVATE'), false)
      return true
    })
  }
  const { adapter } = fake([{ errors: [], ...envelope({ pullRequest: pr() }) }])
  assert.equal((await adapter.getPullRequest(repository, 42)).number, 42)
})

test('scope and integer validation reject before subprocess execution', async () => {
  const { adapter, calls } = fake([])
  for (const repo of ['Other/repo', 'togetherweown/kofra', 'TogetherWeOwn/kofra;uname', null]) {
    await assert.rejects(adapter.getPullRequest(repo, 42))
    await assert.rejects(adapter.listOpenPullRequests(repo))
  }
  for (const number of [null, '42', 0, -1, 1.5, 2147483648]) await assert.rejects(adapter.getPullRequest(repository, number))
  assert.equal(calls.length, 0)
  for (const allowedRepositories of [[], null, ['foo'], [null], ['owner/repo\n'], ['owner/repo;uname']]) {
    assert.throws(() => createGithubAdapter({ allowedRepositories }))
  }
  for (const maxPages of [0, 1.5, 1001]) assert.throws(() => createGithubAdapter({ allowedRepositories: [repository], maxPages }))
})

test('open PR scan traverses all pages, uses opaque cursors as argv, and has no 100-result truncation', async () => {
  const cursor = 'opaque;$(do-not-execute)'
  const { adapter, calls } = fake([
    page(Array.from({ length: 100 }, (_, i) => i + 1), true, cursor, 102), page([101, 102], false, 'last', 102),
  ])
  const refs = await adapter.listOpenPullRequests(repository)
  assert.equal(refs.length, 102)
  assert.deepEqual(refs[101], { repository, number: 102 })
  assert.equal(calls.length, 2)
  assert.equal(calls[0][1].some((arg) => arg.startsWith('after=')), false)
  assert.ok(calls[1][1].includes(`after=${cursor}`))
  assert.ok(calls[0][1][5].includes('states: [OPEN]'))
  assert.ok(calls[0][1][5].includes('field: CREATED_AT'))
})

test('zero open PRs is valid only with explicit complete page metadata', async () => {
  const { adapter } = fake([page([])])
  assert.deepEqual(await adapter.listOpenPullRequests(repository), [])
  for (const response of [envelope({ pullRequests: null }), envelope({ pullRequests: { nodes: [] } }),
    page([], true, null, 1), page([], false, null, 1)]) {
    const { adapter } = fake([response])
    await assert.rejects(adapter.listOpenPullRequests(repository))
  }
})

test('missing pages, duplicate identities, changing counts and repeated cursors refuse partial backfills', async () => {
  for (const responses of [
    [page([1, 1])], [page([null])], [page([1], false, null, 2)], [page([1], false, null, 0)],
    [page([1], true, 'c1', 2), page([2], false, null, 3)],
    [page([1], true, 'c1', 2), page([1], false, null, 2)],
    [page([1], true, 'c1', 3), page([2], true, 'c1', 3)],
    [page([1], true, '', 2)], [page([1], true, undefined, 2)],
    [page([1], true, 'c1', 2), new Error('timeout PRIVATE')],
  ]) {
    const { adapter } = fake(responses)
    await assert.rejects(adapter.listOpenPullRequests(repository))
  }
})

test('page budget is a loud failure, not a silently limited scan', async () => {
  const { adapter, calls } = fake([page([1], true, 'c1', 2)], { maxPages: 1 })
  await assert.rejects(adapter.listOpenPullRequests(repository), /budget exceeded/)
  assert.equal(calls.length, 1)
  const complete = fake([page([1])], { maxPages: 1 })
  assert.deepEqual(await complete.adapter.listOpenPullRequests(repository), [{ repository, number: 1 }])
})

test('transport/parse failures redact error streams and never retry', async () => {
  let calls = 0
  for (const output of ['not-json PRIVATE', '{"data":', null]) {
    const adapter = createGithubAdapter({ allowedRepositories: [repository], run: async () => {
      calls++
      if (output === null) throw Object.assign(new Error('PRIVATE'), { stdout: 'PRIVATE', stderr: 'PRIVATE' })
      return { stdout: output, stderr: 'PRIVATE' }
    } })
    await assert.rejects(adapter.getPullRequest(repository, 42), (error) => {
      assert.match(error.message, /GitHub query failed/)
      assert.equal(Object.hasOwn(error, 'cause'), false)
      assert.equal(Object.hasOwn(error, 'stdout'), false)
      assert.equal(Object.hasOwn(error, 'stderr'), false)
      assert.equal(error.stack.includes('PRIVATE'), false)
      return true
    })
  }
  assert.equal(calls, 3)
})

test('real child-process stdout succeeds, while nonzero exit never promotes its valid JSON', async () => {
  const execute = promisify(execFile)
  for (const code of [0, 1]) {
    const adapter = createGithubAdapter({ allowedRepositories: [repository], run: async (file, args, options) => {
      assert.equal(file, 'gh')
      return execute(process.execPath, ['-e',
        'process.stdout.write(process.argv[1]); process.stderr.write("PRIVATE"); process.exit(Number(process.argv[2]))',
        JSON.stringify(envelope({ pullRequest: pr() })), String(code)], { ...options, env: {} })
    } })
    if (code === 0) assert.equal((await adapter.getPullRequest(repository, 42)).head.sha, sha)
    else await assert.rejects(adapter.getPullRequest(repository, 42), /GitHub query failed/)
  }
})

test('GraphQL adapter feeds the real consumer backfill and preserves review/lifecycle mapping', async () => {
  const { adapter, calls } = fake([page([42]), envelope({ pullRequest: {
    ...pr(), reviewDecision: 'CHANGES_REQUESTED', isDraft: true,
  } })])
  const writes = []
  const consumer = createConsumer({ github: adapter, capture: {}, allowedRepositories: [repository], board: {
    async getIssue(ref) { assert.equal(ref, 'case_22b092f5'); return { id: 'issue', identifier: ref } },
    async listWorkProducts() { return [] },
    async createWorkProduct(id, body) { writes.push({ id, body }); return { id: 'product', ...body } },
  } })
  assert.deepEqual(await consumer.backfill(await adapter.listOpenPullRequests(repository)), { reconciled: 1 })
  assert.equal(writes.length, 1)
  assert.equal(writes[0].body.reviewState, 'changes_requested')
  assert.equal(writes[0].body.status, 'draft')
  assert.equal(writes[0].body.metadata.headSha, sha)
  assert.equal(calls.length, 2)
})

const headSha = 'b'.repeat(40)
const pushedAt = '2026-10-03T07:00:00.000Z'
const pushedMs = Date.parse(pushedAt)
const closeoutPr = (overrides = {}) => ({
  number: 42, url: `https://github.com/${repository}/pull/42`,
  title: 'Bridge PR', body: '', state: 'OPEN', isDraft: false, merged: false,
  headRefName: 'case_22b092f5-bridge', headRefOid: headSha, reviewDecision: null,
  mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
  author: { __typename: 'Bot', login: 'togetherweown' }, baseRepository: { nameWithOwner: repository },
  labels: { nodes: [], pageInfo: { hasNextPage: false } },
  commits: { nodes: [{ commit: { oid: headSha, pushedDate: pushedAt,
    statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] },
  ...overrides,
})
const snapEnvelope = (pullRequest) => ({ data: { repository: { nameWithOwner: repository, isPrivate: true, pullRequest } } })
const checkRun = (name, status, conclusion = null, completedAt = null) =>
  ({ __typename: 'CheckRun', name, status, conclusion, completedAt })
const statusCtx = (context, state, createdAt = pushedAt) =>
  ({ __typename: 'StatusContext', context, state, createdAt })
const rollupPr = (nodes) => closeoutPr({ commits: { nodes: [{ commit: { oid: headSha, pushedDate: pushedAt,
  statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } }] } })

test('closeout snapshot reads labels, merge state, auto-merge and checks in one bounded call', async () => {
  const nowMs = pushedMs + 60_000
  const { adapter, calls } = fake([snapEnvelope(closeoutPr({
    mergeStateStatus: 'DIRTY', autoMergeRequest: { enabledAt: pushedAt },
    labels: { nodes: [{ name: 'area/bridge' }], pageInfo: { hasNextPage: false } },
    commits: { nodes: [{ commit: { oid: headSha, pushedDate: pushedAt, statusCheckRollup: { contexts: {
      nodes: [checkRun('check', 'COMPLETED', 'SUCCESS', pushedAt), statusCtx('gitleaks', 'PENDING')],
      pageInfo: { hasNextPage: false } } } } }] },
  }))])
  assert.deepEqual(await adapter.getCloseoutSnapshot(repository, 42, { nowMs }), {
    repository, number: 42, headSha, baseRef: 'main', state: 'open', merged: false, draft: false,
    body: '', isPrivate: true, autoMerge: true, labels: ['area/bridge'], mergeableState: 'dirty', reviewDecision: null,
    headPushedMs: pushedMs, nowMs,
    checks: [
      { name: 'check', status: 'completed', conclusion: 'success', completedMs: pushedMs },
      { name: 'gitleaks', status: 'in_progress' },
    ],
  })
  assert.equal(calls.length, 1)
  const [file, args, options] = calls[0]
  assert.equal(file, 'gh')
  assert.deepEqual(args.slice(0, 4), ['api', '--hostname', 'github.com', 'graphql'])
  const query = args[5]
  for (const field of ['mergeStateStatus', 'autoMergeRequest', 'baseRefName', 'labels', 'statusCheckRollup', 'pushedDate']) {
    assert.ok(query.includes(field), `snapshot query is missing ${field}`)
  }
  assert.ok(args.includes('number=42'))
  assert.deepEqual(options, { encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4194304 })
  assert.equal(args.some((arg) => /token|authorization/i.test(arg)), false)
})

test('adapter snapshots feed the closeout classifier with no translation', async () => {
  const policy = closeoutPolicy({ requiredChecks: ['check'], missingGraceMs: 600_000, stalledGraceMs: 600_000 })
  const conflicted = fake([snapEnvelope(closeoutPr({ mergeStateStatus: 'DIRTY' }))])
  assert.equal(classifyCloseout(await conflicted.adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 }), policy).class, 'conflict')
  const readyAt = pushedMs + 600_001
  const ready = fake([snapEnvelope(rollupPr([checkRun('check', 'COMPLETED', 'SUCCESS', pushedAt)]))])
  const snapshot = await ready.adapter.getCloseoutSnapshot(repository, 42, { nowMs: readyAt })
  // An explicit null auto-merge request reads as unarmed, never as armed.
  assert.equal(snapshot.autoMerge, false)
  const decision = classifyCloseout({ ...snapshot, reviewDecision: 'APPROVED' }, policy)
  assert.equal(decision.class, 'stalled')
  assert.deepEqual(decision, { class: 'stalled', repository, number: 42, headSha, detail: { checks: [] } })
})

test('snapshot check mapping covers run queues, status contexts and conclusion case', async () => {
  const { adapter } = fake([snapEnvelope(rollupPr([
    checkRun('queued-run', 'QUEUED'), checkRun('waiting-run', 'WAITING'), checkRun('requested-run', 'REQUESTED'),
    checkRun('running-run', 'IN_PROGRESS'), checkRun('slow-run', 'COMPLETED', 'TIMED_OUT', pushedAt),
    checkRun('future-run', 'COMPLETED', 'WEIRD_NEW_STATE', pushedAt),
    statusCtx('expected-status', 'EXPECTED'), statusCtx('bad-status', 'ERROR'), statusCtx('ok-status', 'SUCCESS'),
  ]))])
  const { checks } = await adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 })
  const byName = new Map(checks.map((c) => [c.name, c]))
  for (const name of ['queued-run', 'waiting-run', 'requested-run', 'expected-status']) {
    assert.equal(byName.get(name).status, 'queued')
    assert.ok(!Object.hasOwn(byName.get(name), 'conclusion'))
  }
  assert.deepEqual(byName.get('running-run'), { name: 'running-run', status: 'in_progress' })
  assert.equal(byName.get('slow-run').conclusion, 'timed_out')
  // Unknown conclusions pass through lowercased; closeout reads them as pending.
  assert.equal(byName.get('future-run').conclusion, 'weird_new_state')
  assert.equal(byName.get('bad-status').conclusion, 'failure')
  assert.equal(byName.get('ok-status').conclusion, 'success')
})

test('snapshot reads fail closed on truncated, moved or malformed GitHub state', async () => {
  const stale = (nodes) => closeoutPr({ commits: { nodes } })
  const cases = [
    // Lowercase is the REST form: reaching the adapter it means the wrong layer built the read.
    closeoutPr({ mergeStateStatus: 'dirty' }),
    closeoutPr({ mergeStateStatus: 'BOGUS' }),
    (() => { const p = closeoutPr(); delete p.mergeStateStatus; return p })(),
    (() => { const p = closeoutPr(); delete p.autoMergeRequest; return p })(),
    closeoutPr({ labels: { nodes: [], pageInfo: { hasNextPage: true } } }),
    (() => { const p = closeoutPr(); delete p.labels; return p })(),
    stale([]),
    stale([{ commit: { oid: 'c'.repeat(40), pushedDate: pushedAt, statusCheckRollup: null } }]),
    closeoutPr({ commits: { nodes: [{ commit: { oid: headSha, pushedDate: 'not-a-date', statusCheckRollup: null } }] } }),
    closeoutPr({ commits: { nodes: [{ commit: { oid: headSha, pushedDate: pushedAt,
      statusCheckRollup: { contexts: { nodes: [checkRun('a', 'COMPLETED', 'SUCCESS', pushedAt),
        checkRun('a', 'QUEUED')], pageInfo: { hasNextPage: false } } } } }] } }),
    rollupPr([checkRun('check', 'CANCELLED')]),
    rollupPr([{ __typename: 'Deployment', name: 'deploy' }]),
    rollupPr([checkRun('check', 'COMPLETED', null, pushedAt)]),
    rollupPr([statusCtx('gitleaks', 'BOGUS')]),
    closeoutPr({ baseRefName: '' }),
  ].filter(Boolean)
  for (const pullRequest of cases) {
    const { adapter, calls } = fake([snapEnvelope(pullRequest)])
    await assert.rejects(adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 }))
    assert.equal(calls.length, 1)
  }
  // A null rollup is "no checks yet", not a failed read.
  const { adapter } = fake([snapEnvelope(closeoutPr({ commits: { nodes: [{
    commit: { oid: headSha, pushedDate: pushedAt, statusCheckRollup: null } }] } }))])
  assert.deepEqual((await adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 })).checks, [])
  // The clock must never precede the push it just read.
  const early = fake([snapEnvelope(closeoutPr())])
  await assert.rejects(early.adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs - 1 }))
})

test('required checks merge legacy contexts and checks, sorted and deduped', async () => {
  const protection = { required_status_checks: { strict: true,
    contexts: ['b-check', 'a-check'], checks: [{ context: 'c-check', app_id: 123 }, { context: 'a-check', app_id: 456 }] } }
  const { adapter, calls } = fake([protection])
  assert.deepEqual(await adapter.getRequiredChecks(repository, 'main'), ['a-check', 'b-check', 'c-check'])
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0][1], ['api', '--hostname', 'github.com', `repos/${repository}/branches/main/protection`])
  const slashed = fake([{ required_status_checks: { contexts: [], checks: [] } }])
  await slashed.adapter.getRequiredChecks(repository, 'feature/x')
  assert.ok(slashed.calls[0][1].includes(`repos/${repository}/branches/feature%2Fx/protection`))
  const absent = fake([{ required_status_checks: null }])
  assert.deepEqual(await absent.adapter.getRequiredChecks(repository, 'main'), [])
})

test('required-check reads reject bad branches and bad protection before and after the call', async () => {
  const { adapter, calls } = fake([])
  for (const branch of [null, '', 'a/b/../../c', '..', '.hidden', 'trailing/', '/leading', 'bad~branch', 'bad:branch', 'x'.repeat(256)]) {
    await assert.rejects(adapter.getRequiredChecks(repository, branch))
    await assert.rejects(adapter.getRequiredChecks('Other/repo', 'main'))
  }
  assert.equal(calls.length, 0)
  for (const body of [{}, { required_status_checks: [] }, { required_status_checks: { contexts: ['a'] } },
    { required_status_checks: { contexts: [''], checks: [] } },
    { required_status_checks: { contexts: [], checks: [{ context: '' }] } }]) {
    const { adapter } = fake([body])
    await assert.rejects(adapter.getRequiredChecks(repository, 'main'))
  }
  const failing = createGithubAdapter({ allowedRepositories: [repository], run: async () => { throw new Error('PRIVATE') } })
  await assert.rejects(failing.getRequiredChecks(repository, 'main'), /GitHub request failed/)
})

// --- readiness reconcile surface (case_4b213922) -------------------------------

test('snapshot body passes through; a null body reads as null, never as empty', async () => {
  const { adapter } = fake([snapEnvelope(closeoutPr({ body: 'hello' }))])
  assert.equal((await adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 })).body, 'hello')
  const missing = fake([snapEnvelope((() => { const p = closeoutPr({ body: null }); return p })())])
  assert.equal((await missing.adapter.getCloseoutSnapshot(repository, 42, { nowMs: pushedMs + 1 })).body, null)
})

// GET /repos/{o}/{r}/rules/branches/{branch} returns the rules already
// applying to that branch — no ref-pattern matching on our side.
const ruleParameters = (contexts) => ({ strict_required_status_checks_policy: false,
  do_not_enforce_on_create: false, required_status_checks: contexts.map((context) => ({ context, integration_id: 15368 })) })
const branchRule = (over = {}) => ({ type: 'required_status_checks',
  parameters: ruleParameters(['ci-ok', 'gitleaks']), ruleset_source_type: 'Repository', ...over })

test('ruleset required checks collect branch rules, sorted and deduped', async () => {
  const { adapter, calls } = fake([[
    branchRule(),
    branchRule({ parameters: ruleParameters(['ci-ok']) }),
    { type: 'pull_request', parameters: {} },
  ]])
  assert.deepEqual(await adapter.getRulesetRequiredChecks(repository, 'main'), ['ci-ok', 'gitleaks'])
  assert.equal(calls.length, 1)
  // A plain GET: gh upgrades to POST once `-f` params are added, so none ride along.
  assert.deepEqual(calls[0][1],
    ['api', '--hostname', 'github.com', '--method', 'GET', `repos/${repository}/rules/branches/main`])
})

// Recorded from GET /repos/TogetherWeOwn/paperclip-ops/rules/branches/main
// on 2026-10-07 (ruleset ids and the integration ids are public metadata).
// Three separate required_status_checks rules, from an organization ruleset
// and two repository rulesets, sit among rules that carry no check names.
const liveMainRules = [
  { type: 'required_status_checks', parameters: { strict_required_status_checks_policy: false,
    do_not_enforce_on_create: true, required_status_checks: [{ context: 'Paperclip Review', integration_id: 5191093 }] },
  ruleset_source_type: 'Organization', ruleset_source: 'TogetherWeOwn', ruleset_id: 24552433 },
  { type: 'deletion', ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 22798757 },
  { type: 'non_fast_forward', ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 22798757 },
  { type: 'required_linear_history', ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 22798757 },
  { type: 'pull_request', parameters: { required_approving_review_count: 0, dismiss_stale_reviews_on_push: true,
    required_reviewers: [], require_code_owner_review: false, dismissal_restriction: { enabled: false, allowed_actors: [] },
    require_last_push_approval: false, required_review_thread_resolution: true,
    require_extra_approval_for_unattributed_changes: false, allowed_merge_methods: ['squash'] },
  ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 22798757 },
  { type: 'required_status_checks', parameters: { strict_required_status_checks_policy: false,
    do_not_enforce_on_create: false, required_status_checks: [{ context: 'ci-ok', integration_id: 15368 }] },
  ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 22798757 },
  { type: 'required_status_checks', parameters: { strict_required_status_checks_policy: false,
    do_not_enforce_on_create: false, required_status_checks: [{ context: 'pr-lint', integration_id: 15368 }] },
  ruleset_source_type: 'Repository', ruleset_source: 'TogetherWeOwn/paperclip-ops', ruleset_id: 24079003 },
]

test('the recorded live main rules read as the three required checks and build a closeout policy', async () => {
  const { adapter } = fake([structuredClone(liveMainRules)])
  const names = await adapter.getRulesetRequiredChecks(repository, 'main')
  assert.deepEqual(names, ['Paperclip Review', 'ci-ok', 'pr-lint'])
  const policy = closeoutPolicy({ requiredChecks: names, reviewCheck: 'Paperclip Review',
    missingGraceMs: 600000, stalledGraceMs: 600000 })
  assert.equal(policy.reviewCheck, 'Paperclip Review')
  assert.deepEqual(policy.requiredChecks, ['Paperclip Review', 'ci-ok', 'pr-lint'])
})

test('a rule that names its checks under another key is incomplete, never "no required checks"', async () => {
  const { adapter } = fake([[{ type: 'required_status_checks', parameters: { required_checks: [{ context: 'ci-ok' }] } }]])
  await assert.rejects(adapter.getRulesetRequiredChecks(repository, 'main'), /required checks are incomplete/)
})

test('ruleset reads are branch-scoped by the server, one call per branch', async () => {
  const { adapter, calls } = fake([
    [branchRule()],
    [{ type: 'pull_request', parameters: {} }],
  ])
  assert.deepEqual(await adapter.getRulesetRequiredChecks(repository, 'main'), ['ci-ok', 'gitleaks'])
  assert.deepEqual(await adapter.getRulesetRequiredChecks(repository, 'other'), [])
  assert.equal(calls.length, 2)
  assert.ok(calls[1][1].at(-1).endsWith('/rules/branches/other'))
})

test('ruleset reads reject bad scope, bad branches and malformed lists', async () => {
  const { adapter, calls } = fake([])
  for (const branch of [null, '', 'a/b/../../c', '..']) {
    await assert.rejects(adapter.getRulesetRequiredChecks(repository, branch))
    await assert.rejects(adapter.getRulesetRequiredChecks('Other/repo', 'main'))
  }
  assert.equal(calls.length, 0)
  // No rules, or none matching, reads as "no required checks" — the duty
  // treats an empty set as undecidable rather than green.
  for (const body of [[], [{ type: 'pull_request', parameters: {} }]]) {
    const { adapter } = fake([body])
    assert.deepEqual(await adapter.getRulesetRequiredChecks(repository, 'main'), [])
  }
  for (const body of [{}, { rules: [] },
    [branchRule({ parameters: 'checks' })],
    [branchRule({ parameters: {} })],
    [branchRule({ parameters: ruleParameters(['']) })]]) {
    const { adapter } = fake([body])
    await assert.rejects(adapter.getRulesetRequiredChecks(repository, 'main'))
  }
  const failing = createGithubAdapter({ allowedRepositories: [repository], run: async () => { throw new Error('PRIVATE') } })
  await assert.rejects(failing.getRulesetRequiredChecks(repository, 'main'), /GitHub ruleset list failed/)
})

test('body updates travel via a file, never argv', async () => {
  const { readFileSync } = await import('node:fs')
  let seen = null
  const adapter = createGithubAdapter({ allowedRepositories: [repository],
    async run(...args) {
      seen = args
      const file = args[1][args[1].indexOf('--input') + 1]
      // The file exists while gh runs and carries the exact JSON payload.
      assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { body: 'new body' })
      return { stdout: '{}' }
    } })
  await adapter.updatePullRequestBody(repository, 42, 'new body')
  assert.deepEqual(seen[1].slice(0, 6),
    ['api', '--hostname', 'github.com', '--method', 'PATCH', '--input'])
  assert.ok(seen[1].includes(`repos/${repository}/pulls/42`))
  assert.equal(seen[1].some((arg) => typeof arg === 'string' && arg.includes('new body')), false)
  assert.deepEqual(seen[2], { encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1048576 })
  const { adapter: bad, calls } = fake([])
  for (const body of [null, '', 'x'.repeat(65537)]) {
    await assert.rejects(bad.updatePullRequestBody(repository, 42, body))
    await assert.rejects(bad.updatePullRequestBody('Other/repo', 42, 'ok'))
    await assert.rejects(bad.updatePullRequestBody(repository, 0, 'ok'))
  }
  assert.equal(calls.length, 0)
})

test('mark-ready posts to ready_for_review and validates the response', async () => {
  const { adapter, calls } = fake([{ id: 1 }])
  await adapter.markReadyForReview(repository, 42)
  assert.deepEqual(calls[0][1], ['api', '--hostname', 'github.com', '--method', 'POST',
    `repos/${repository}/pulls/42/ready_for_review`])
  const empty = fake([null])
  await assert.rejects(empty.adapter.markReadyForReview(repository, 42))
  const failing = createGithubAdapter({ allowedRepositories: [repository], run: async () => { throw new Error('PRIVATE') } })
  await assert.rejects(failing.markReadyForReview(repository, 42), /GitHub mark-ready failed/)
})

test('convert-to-draft resolves the node id then mutates, confirming draft', async () => {
  const nodeId = 'UHVsbFJlcXVlc3Q0Mg=='
  const { adapter, calls } = fake([
    { data: { repository: { nameWithOwner: repository, pullRequest: { id: nodeId } } } },
    { data: { convertPullRequestToDraft: { pullRequest: { isDraft: true } } } },
  ])
  await adapter.convertToDraft(repository, 42)
  assert.equal(calls.length, 2)
  assert.ok(calls[0][1].join(' ').includes('pullRequest'))
  assert.ok(calls[1][1].some((arg) => arg.includes('convertPullRequestToDraft')))
  assert.ok(calls[1][1].some((arg) => arg === `pullRequestId=${nodeId}`))
  // A mutation that does not confirm a draft fails closed with its own message.
  const unconfirmed = fake([
    { data: { repository: { nameWithOwner: repository, pullRequest: { id: nodeId } } } },
    { data: { convertPullRequestToDraft: { pullRequest: { isDraft: false } } } },
  ])
  await assert.rejects(unconfirmed.adapter.convertToDraft(repository, 42), /did not confirm/)
  // A missing node id fails closed before any mutation runs.
  const noId = fake([{ data: { repository: { nameWithOwner: repository, pullRequest: { id: null } } } }])
  await assert.rejects(noId.adapter.convertToDraft(repository, 42), /node id/)
  assert.equal(noId.calls.length, 1)
})

test('enable-auto-merge arms squash bound to the evaluated head', async () => {
  const sha = 'a'.repeat(40)
  const { adapter, calls } = fake([{ stdout: '' }])
  await adapter.enableAutoMerge(repository, 42, sha)
  assert.deepEqual(calls[0][1],
    ['pr', 'merge', '42', '--repo', repository, '--auto', '--squash', '--match-head-commit', sha])
  const { adapter: bad, calls: noCalls } = fake([])
  await assert.rejects(bad.enableAutoMerge('Other/repo', 42, sha))
  await assert.rejects(bad.enableAutoMerge(repository, 0, sha))
  // The arm binds the gate's head: a missing or malformed SHA never arms.
  for (const head of [null, '', 'abc', 'A'.repeat(40), `${'a'.repeat(39)}g`]) {
    await assert.rejects(bad.enableAutoMerge(repository, 42, head))
  }
  assert.equal(noCalls.length, 0)
  const failing = createGithubAdapter({ allowedRepositories: [repository], run: async () => { throw new Error('PRIVATE') } })
  await assert.rejects(failing.enableAutoMerge(repository, 42, sha), /auto-merge arm failed/)
})
