import { test } from 'node:test'
import assert from 'node:assert/strict'
import { armPolicy, decideArm, buildArmArgv, createArmer,
  createAutoMergeArmer, createBranchGateReader } from '../src/closeout-arm.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const APP_ID = 123456
const policy = armPolicy({ reviewAppId: APP_ID })
const issueId = '00000000-0000-4000-8000-000000000010'
const head = (c) => c.repeat(40)

function snapshot(number, over = {}) {
  return { repository: repo, number, headSha: head('a'), baseRef: 'main',
    state: 'open', draft: false, autoMerge: false, labels: [], ...over }
}

function gate(over = {}) {
  return { repository: repo, branch: 'main',
    review: { context: 'Paperclip Review', appId: APP_ID },
    requiredChecks: ['check', 'gitleaks', 'pr-lint'],
    allowAutoMerge: true, mergeMethods: ['squash'], ...over }
}

// Injected doubles. They implement EXACTLY the four arm dependencies: a
// snapshot reader, a branch-gate reader, an arm writer, and a confirmation
// opener. Any disarm or merge call path would have nowhere to land: there
// is no such dependency to call.
function doubles({ snapshots, gates, failArm = [], failConfirm = [], failGate = [] } = {}) {
  const armCalls = []
  const confirmCalls = []
  const github = {
    async getCloseoutSnapshot(repository, number, { nowMs } = {}) {
      const found = snapshots.get(`${repository}#${number}`)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      assert.ok(Number.isInteger(nowMs))
      return found
    },
    async getBranchGate(repository, branch) {
      if (failGate.includes(branch)) throw new Error('GitHub gate read failed')
      const found = gates.get(`${repository}#${branch}`)
      if (!found) throw new Error('GitHub gate read failed')
      return found
    },
  }
  const armAutoMerge = async (repository, number, headSha) => {
    armCalls.push(['pr', 'merge', String(number), '--repo', repository,
      '--auto', '--squash', '--match-head-commit', headSha])
    if (failArm.includes(number)) throw new Error('GitHub arm failed')
    return armCalls.at(-1)
  }
  const openConfirmation = async ({ issueId: id, repository, number }) => {
    confirmCalls.push([id, repository, number])
    if (failConfirm.includes(number)) throw new Error('merge confirmation post failed')
    return { id: 'interaction-1' }
  }
  return { github, armAutoMerge, openConfirmation, armCalls, confirmCalls }
}

function armedWorld(number, snapOver = {}, gateOver = {}) {
  const snapshots = new Map([[`${repo}#${number}`, snapshot(number, snapOver)]])
  const gates = new Map([[`${repo}#main`, gate(gateOver)]])
  return doubles({ snapshots, gates })
}

test('all-green promoted repo arms with the exact match-head-commit argv and opens the confirmation', async () => {
  const { github, armAutoMerge, openConfirmation, armCalls, confirmCalls } = armedWorld(1, { headSha: head('b') })
  const { records, counters, refusals } = await createArmer({ github, armAutoMerge, openConfirmation, policy })
    .runArm({ references: [{ repository: repo, number: 1 }], issueId, nowMs: 1000 })
  assert.deepEqual(counters, { total: 1, evaluated: 1, armed: 1, refused: 0, errors: 0, confirmations: 1 })
  assert.deepEqual(refusals, {})
  assert.equal(records[0].decision, 'armed')
  assert.equal(records[0].reason, 'armed')
  assert.equal(records[0].confirmed, true)
  assert.equal(records[0].headSha, head('b'))
  assert.match(records[0].log, new RegExp(`^closeout-arm ${repo}#1@${head('b')}: armed \\(armed\\)$`))
  assert.deepEqual(armCalls, [['pr', 'merge', '1', '--repo', repo,
    '--auto', '--squash', '--match-head-commit', head('b')]])
  assert.deepEqual(confirmCalls, [[issueId, repo, 1]])
})

test('each refusal case makes no arm call and opens no confirmation', async () => {
  const cases = [
    ['draft PR', { draft: true }, {}, 'draft'],
    ['held PR', { labels: ['hold'] }, {}, 'held'],
    ['closed PR', { state: 'closed' }, {}, 'not-open'],
    ['already-armed PR', { autoMerge: true }, {}, 'already-armed'],
    ['review check absent', {}, { review: null }, 'review-check-unpinned'],
    ['review check from another app', {}, { review: { context: 'Paperclip Review', appId: 999 } },
      'review-check-unpinned'],
    ['CI set incomplete', {}, { requiredChecks: ['check', 'gitleaks'] }, 'ci-set-incomplete'],
    ['auto-merge off', {}, { allowAutoMerge: false }, 'auto-merge-off'],
    ['non-squash allowed', {}, { mergeMethods: ['merge', 'squash', 'rebase'] }, 'non-squash-allowed'],
    ['squash disabled', {}, { mergeMethods: ['merge'] }, 'non-squash-allowed'],
  ]
  for (const [name, snapOver, gateOver, reason] of cases) {
    const { github, armAutoMerge, openConfirmation, armCalls, confirmCalls } =
      armedWorld(1, snapOver, gateOver)
    const { records, counters, refusals } =
      await createArmer({ github, armAutoMerge, openConfirmation, policy })
        .runArm({ references: [{ repository: repo, number: 1 }], issueId, nowMs: 1000 })
    assert.equal(records[0].decision, 'none', name)
    assert.equal(records[0].reason, reason, name)
    assert.equal(records[0].confirmed, false, name)
    assert.match(records[0].log, new RegExp(`none \\(${reason}\\)$`), name)
    assert.deepEqual(armCalls, [], name)
    assert.deepEqual(confirmCalls, [], name)
    assert.equal(counters.refused, 1, name)
    assert.deepEqual(refusals, { [reason]: 1 }, name)
  }
})

test('mutant M1 (unpinned check still arms) is killed: app-id mismatch refuses', () => {
  const verdict = decideArm({ snapshot: snapshot(1),
    gate: gate({ review: { context: 'Paperclip Review', appId: APP_ID + 1 } }), policy })
  assert.deepEqual(verdict, { armed: false, reason: 'review-check-unpinned' })
})

test('mutant M2 (draft still arms) is killed: draft refuses before the gate is read', () => {
  const verdict = decideArm({ snapshot: snapshot(1, { draft: true }), gate: gate(), policy })
  assert.deepEqual(verdict, { armed: false, reason: 'draft' })
})

test('mutant M3 (match-head-commit missing) is killed: the argv pins the exact head', () => {
  assert.deepEqual(buildArmArgv(repo, 7, head('c')),
    ['pr', 'merge', '7', '--repo', repo, '--auto', '--squash', '--match-head-commit', head('c')])
  assert.ok(!buildArmArgv(repo, 7, head('c')).includes('--admin'))
})

test('an any-source entry for the review check never counts as pinned', () => {
  // The ruleset lists the check name with a null source: the reader maps
  // it into requiredChecks but leaves review null, and the decision refuses.
  const verdict = decideArm({ snapshot: snapshot(1),
    gate: gate({ review: null, requiredChecks: ['Paperclip Review', 'check', 'gitleaks', 'pr-lint'] }),
    policy })
  assert.deepEqual(verdict, { armed: false, reason: 'review-check-unpinned' })
})

test('unreadable snapshot or gate is a counted error, never a thrown run', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1)]])
  const gates = new Map()
  const { github, armAutoMerge, openConfirmation } = doubles({ snapshots, gates })
  const { records, counters } = await createArmer({ github, armAutoMerge, openConfirmation, policy })
    .runArm({ references: [{ repository: repo, number: 1 }, { repository: repo, number: 2 }],
      issueId, nowMs: 1000 })
  assert.equal(records[0].reason, 'gate-read-failed')
  assert.equal(records[1].reason, 'snapshot-read-failed')
  assert.deepEqual([counters.total, counters.errors, counters.armed, counters.refused], [2, 2, 0, 0])
  for (const r of records) assert.equal(r.decision, 'none')
})

test('a failed arm opens no confirmation; a failed confirmation after an arm is a counted error', async () => {
  const snapshots = new Map([[`${repo}#3`, snapshot(3)], [`${repo}#4`, snapshot(4)]])
  const gates = new Map([[`${repo}#main`, gate()]])
  const { github, armAutoMerge, openConfirmation, confirmCalls } =
    doubles({ snapshots, gates, failArm: [3], failConfirm: [4] })
  const { records, counters } = await createArmer({ github, armAutoMerge, openConfirmation, policy })
    .runArm({ references: [{ repository: repo, number: 3 }, { repository: repo, number: 4 }],
      issueId, nowMs: 1000 })
  assert.equal(records[0].reason, 'arm-failed')
  assert.equal(records[0].confirmed, false)
  assert.equal(records[1].decision, 'armed')
  assert.equal(records[1].confirmed, false)
  assert.equal(records[1].error, 'confirmation-failed')
  assert.deepEqual(confirmCalls, [[issueId, repo, 4]])
  assert.deepEqual([counters.armed, counters.errors, counters.confirmations], [1, 2, 0])
})

test('the writer runs exactly the pinned argv and nothing else', async () => {
  const seen = []
  const armAutoMerge = createAutoMergeArmer({ run: async (cmd, argv) => {
    seen.push([cmd, ...argv])
    return { stdout: '' }
  } })
  const argv = await armAutoMerge(repo, 9, head('d'))
  assert.deepEqual(seen, [['gh', 'pr', 'merge', '9', '--repo', repo,
    '--auto', '--squash', '--match-head-commit', head('d')]])
  assert.deepEqual(argv, seen[0].slice(1))
  await assert.rejects(armAutoMerge(repo, 9, 'not-a-sha'), /arm head sha is invalid/)
})

test('the gate reader maps repo settings and ruleset entries, and fails closed', async () => {
  const bodies = {
    '': JSON.stringify({ allow_auto_merge: true, allow_squash_merge: true,
      allow_merge_commit: false, allow_rebase_merge: false }),
    'rules/branches/main': JSON.stringify([
      { type: 'merge_queue', parameters: {} },
      { type: 'required_status_checks',
        parameters: { required_status_checks: [
          { context: 'check', integration_id: 15368 },
          { context: 'gitleaks', integration_id: 1 },
          { context: 'pr-lint', integration_id: 1 },
          { context: 'Paperclip Review', integration_id: APP_ID }] } },
    ]),
  }
  const getBranchGate = createBranchGateReader({ repository: repo,
    run: async (cmd, argv) => {
      assert.equal(cmd, 'gh')
      const path = argv.at(-1).replace(`repos/${repo.split('/')[0]}/${repo.split('/')[1]}/`, '')
      return { stdout: bodies[path] }
    } })
  assert.deepEqual(await getBranchGate(repo, 'main'), {
    repository: repo, branch: 'main',
    review: { context: 'Paperclip Review', appId: APP_ID },
    requiredChecks: ['Paperclip Review', 'check', 'gitleaks', 'pr-lint'],
    allowAutoMerge: true, mergeMethods: ['squash'],
  })
  await assert.rejects(getBranchGate('TogetherWeOwn/other', 'main'), /outside the reader scope/)
  const unprotected = createBranchGateReader({ repository: repo,
    run: async () => { throw new Error('exit 1') } })
  await assert.rejects(unprotected(repo, 'main'), /gate read failed/)
})

test('an any-source ruleset entry never breaks the gate read and never counts as pinned', async () => {
  // GitHub answers integration_id: null for a required check that accepts
  // any source. The reader keeps the name in requiredChecks, leaves the
  // review pin null, and the decision refuses review-check-unpinned.
  const bodies = {
    '': JSON.stringify({ allow_auto_merge: true, allow_squash_merge: true,
      allow_merge_commit: false, allow_rebase_merge: false }),
    'rules/branches/main': JSON.stringify([
      { type: 'required_status_checks',
        parameters: { required_status_checks: [
          { context: 'check', integration_id: null },
          { context: 'gitleaks', integration_id: 1 },
          { context: 'pr-lint', integration_id: 1 },
          { context: 'Paperclip Review', integration_id: null }] } },
    ]),
  }
  const getBranchGate = createBranchGateReader({ repository: repo,
    run: async (cmd, argv) => {
      assert.equal(cmd, 'gh')
      const path = argv.at(-1).replace(`repos/${repo.split('/')[0]}/${repo.split('/')[1]}/`, '')
      return { stdout: bodies[path] }
    } })
  const gate = await getBranchGate(repo, 'main')
  assert.deepEqual(gate.requiredChecks, ['Paperclip Review', 'check', 'gitleaks', 'pr-lint'])
  assert.equal(gate.review, null)
  assert.deepEqual(decideArm({ snapshot: snapshot(1), gate, policy }),
    { armed: false, reason: 'review-check-unpinned' })
})

test('a branch with no ruleset rules reads as unpinned, not as an error', async () => {
  const bodies = {
    '': JSON.stringify({ allow_auto_merge: true, allow_squash_merge: true,
      allow_merge_commit: false, allow_rebase_merge: false }),
    'rules/branches/main': JSON.stringify([
      { type: 'pull_request', parameters: { required_approving_review_count: 1 } },
    ]),
  }
  const getBranchGate = createBranchGateReader({ repository: repo,
    run: async (cmd, argv) => {
      assert.equal(cmd, 'gh')
      const path = argv.at(-1).replace(`repos/${repo.split('/')[0]}/${repo.split('/')[1]}/`, '')
      return { stdout: bodies[path] }
    } })
  const gate = await getBranchGate(repo, 'main')
  assert.deepEqual(gate.requiredChecks, [])
  assert.equal(gate.review, null)
  assert.deepEqual(decideArm({ snapshot: snapshot(1), gate, policy }),
    { armed: false, reason: 'review-check-unpinned' })
})

test('policy and input validation fail loudly', () => {
  assert.throws(() => armPolicy({}), /reviewAppId/)
  assert.throws(() => armPolicy({ reviewAppId: -1 }), /reviewAppId/)
  assert.throws(() => decideArm({ snapshot: null, gate: gate(), policy }), /snapshot is required/)
  assert.throws(() => decideArm({ snapshot: snapshot(1), gate: null, policy }), /branch gate is required/)
  assert.throws(() => decideArm({ snapshot: snapshot(1),
    gate: { ...gate(), repository: 'TogetherWeOwn/other' }, policy }), /another repository/)
  assert.throws(() => buildArmArgv('bad', 1, head('a')), /repository is invalid/)
})
