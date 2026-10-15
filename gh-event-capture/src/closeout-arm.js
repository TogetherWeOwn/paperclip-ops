// Phase-B arm-at-open helper (design: docs/decisions/case_439e6875-pr-closeout-v2-design.md
// §4, slice S6a). The PR-opening agent calls this once, at open. It arms
// auto-merge with EXACTLY `gh pr merge <n> --repo <repo> --auto --squash
// --match-head-commit <sha>` — squash only, title/body from the PR — and it
// REFUSES (no arm call, logged reason + counter) unless every gate below
// holds. Never `--admin`, never a manual merge, never a ruleset edit.
//
// The gate is read LIVE from the base branch ruleset on every run (never
// cached, never a flag): the `Paperclip Review` check must be required AND
// pinned to the review-bot App (the ruleset entry's `integration_id`; an
// any-source entry with a null `integration_id` is unpinned by construction); the required CI set (`check`, `gitleaks`,
// `pr-lint`) must be present; the repo must allow auto-merge; merges must be
// squash-only. A repo where the check is still advisory is refused, and it
// keeps today's reviewer-merges flow. Drafts and `hold`-labeled PRs are
// refused. Closed or already-armed PRs are no-ops, never re-armed.
//
// Agent-side only, like S5b: the reconciler never arms (S6b owns disarm), so
// this module takes an arm writer the reconciler never receives, and the
// disarm duty takes no arm writer. Alongside a successful arm the runner
// opens the S5b merge confirmation on the owning issue (injectable, so tests
// never touch the network).
//
// Three pieces:
//
// 1. decideArm: the pure decision. Inputs are the S3a snapshot fields plus
//    one live branch-gate read. Arm verdict or a fixed-word refusal reason.
// 2. createArmer: the runner. Per PR it reads one authoritative snapshot and
//    one live gate, decides, and either arms (+ confirmation) or records the
//    refusal. One unreadable PR is a counted error record, never a thrown
//    run: one bad PR must not blind the rest.
// 3. Live defaults: createBranchGateReader (REST repo settings + branch
//    rulesets, allowlist-scoped to one repo) and createAutoMergeArmer (the
//    exact argv, nothing else). The CLI wires these with the S3a snapshot
//    reader from github-adapter.js.
//
// Importable (`decideArm`, `armPolicy`, `createArmer`, `buildArmArgv`,
// `createBranchGateReader`, `createAutoMergeArmer`) and shell-callable
// (`node src/closeout-arm.js --issue <uuid> --repo <owner/repo> --pr <N>`;
// `--dry-run` reads and prints the decision without arming). Credentials
// travel by inherited environment, never on argv.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { createGithubAdapter } from './github-adapter.js'
import { openMergeConfirmation } from './merge-confirmation.js'

const execute = promisify(execFile)

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i
const REVIEW_CHECK = 'Paperclip Review'
const REQUIRED_CI = ['check', 'gitleaks', 'pr-lint']
const MERGE_METHODS = new Set(['merge', 'squash', 'rebase'])
const MAX_ARM_REFS = 100

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isRef = (value) => value && typeof value === 'object' && !Array.isArray(value) &&
  typeof value.repository === 'string' && REPOSITORY.test(value.repository) &&
  Number.isSafeInteger(value.number) && value.number > 0 && value.number <= 2147483647

const isBranch = (value) => typeof value === 'string' && value.length > 0 && value.length <= 255 &&
  !/[\0-\x1f\x7f ~^:?*[\\]/.test(value) && !/(^|\/)\.\.(\/|$)/.test(value) &&
  !/^[/.]/.test(value) && !/[/.]$/.test(value)

/**
 * Validate and freeze the arm policy. Fails loudly. There is deliberately no
 * enabled-repositories flag: promotion IS the gate, read live from the base
 * branch rules on every run. `reviewAppId` (the review-bot App's
 * `integration_id`) has no default — a guessed pin is worse than no pin, so
 * the caller names the App it trusts.
 *
 * @param {{ holdLabels?: string[], requiredCi?: string[], reviewCheck?: string, reviewAppId?: number }} policy
 */
export function armPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'arm policy is required')
  const { holdLabels = ['hold'], requiredCi = REQUIRED_CI, reviewCheck = REVIEW_CHECK,
    reviewAppId = null } = policy
  requireValue(Array.isArray(holdLabels) && holdLabels.length > 0 &&
    holdLabels.every((l) => typeof l === 'string' && l.length > 0),
  'holdLabels must be a non-empty list of label names')
  requireValue(Array.isArray(requiredCi) && requiredCi.length > 0 &&
    requiredCi.every((c) => typeof c === 'string' && c.length > 0),
  'requiredCi must be a non-empty list of check names')
  requireValue(typeof reviewCheck === 'string' && reviewCheck.length > 0,
    'reviewCheck must name the required review check')
  requireValue(Number.isSafeInteger(reviewAppId) && reviewAppId > 0,
    'reviewAppId must be the review-bot App integration id')
  return Object.freeze({
    holdLabels: Object.freeze([...holdLabels]),
    requiredCi: Object.freeze([...requiredCi]),
    reviewCheck,
    reviewAppId,
  })
}

function validateArmSnapshot(s) {
  requireValue(s && typeof s === 'object' && !Array.isArray(s), 'arm snapshot is required')
  requireValue(typeof s.repository === 'string' && REPOSITORY.test(s.repository), 'arm snapshot repository is invalid')
  requireValue(Number.isSafeInteger(s.number) && s.number > 0, 'arm snapshot PR number is invalid')
  requireValue(typeof s.headSha === 'string' && SHA.test(s.headSha), 'arm snapshot head sha is invalid')
  requireValue(typeof s.baseRef === 'string' && isBranch(s.baseRef), 'arm snapshot base ref is invalid')
  requireValue(s.state === 'open' || s.state === 'closed', 'arm snapshot state is unknown')
  // Explicit booleans are "known"; a missing field is a dropped read, which
  // must throw rather than read as a draft (spurious refusal) or as
  // armable (spurious arm).
  requireValue(typeof s.draft === 'boolean' && typeof s.autoMerge === 'boolean',
    'arm snapshot lifecycle is incomplete')
  requireValue(Array.isArray(s.labels) && s.labels.every((l) => typeof l === 'string'),
    'arm snapshot labels are invalid')
}

function validateGate(gate) {
  requireValue(gate && typeof gate === 'object' && !Array.isArray(gate), 'arm branch gate is required')
  requireValue(typeof gate.repository === 'string' && REPOSITORY.test(gate.repository),
    'arm branch gate repository is invalid')
  requireValue(isBranch(gate.branch), 'arm branch gate branch is invalid')
  requireValue(gate.review === null ||
    (typeof gate.review === 'object' && !Array.isArray(gate.review) &&
      typeof gate.review.context === 'string' && gate.review.context.length > 0 &&
      Number.isSafeInteger(gate.review.appId) && gate.review.appId > 0),
  'arm branch gate review pin is invalid')
  requireValue(Array.isArray(gate.requiredChecks) &&
    gate.requiredChecks.every((c) => typeof c === 'string' && c.length > 0),
  'arm branch gate required checks are invalid')
  requireValue(typeof gate.allowAutoMerge === 'boolean', 'arm branch gate auto-merge flag is invalid')
  requireValue(Array.isArray(gate.mergeMethods) && gate.mergeMethods.length > 0 &&
    gate.mergeMethods.every((m) => MERGE_METHODS.has(m)),
  'arm branch gate merge methods are invalid')
}

/**
 * @param {{ snapshot: object, gate: object, policy: ReturnType<typeof armPolicy> }} input
 * @returns {{ armed: true, repository, number, headSha } | { armed: false, reason }}
 *   an arm verdict, or a refusal with a fixed-word reason. Never throws for a
 *   refusal: every refusal below is a decided outcome, not a read failure.
 */
export function decideArm({ snapshot, gate, policy }) {
  validateArmSnapshot(snapshot)
  validateGate(gate)
  requireValue(policy && Array.isArray(policy.holdLabels) && Array.isArray(policy.requiredCi) &&
    typeof policy.reviewCheck === 'string' && Number.isSafeInteger(policy.reviewAppId),
  'arm policy is invalid')
  requireValue(gate.repository === snapshot.repository, 'arm branch gate is for another repository')
  if (snapshot.state !== 'open') return { armed: false, reason: 'not-open' }
  if (snapshot.autoMerge === true) return { armed: false, reason: 'already-armed' }
  if (snapshot.draft === true) return { armed: false, reason: 'draft' }
  if (snapshot.labels.some((l) => policy.holdLabels.includes(l))) return { armed: false, reason: 'held' }
  if (gate.review === null || gate.review.context !== policy.reviewCheck ||
    gate.review.appId !== policy.reviewAppId) return { armed: false, reason: 'review-check-unpinned' }
  if (!policy.requiredCi.every((c) => gate.requiredChecks.includes(c))) {
    return { armed: false, reason: 'ci-set-incomplete' }
  }
  if (gate.allowAutoMerge !== true) return { armed: false, reason: 'auto-merge-off' }
  if (gate.mergeMethods.length !== 1 || gate.mergeMethods[0] !== 'squash') {
    return { armed: false, reason: 'non-squash-allowed' }
  }
  return { repository: snapshot.repository, number: snapshot.number, headSha: snapshot.headSha, armed: true }
}

/**
 * The exact arm argv, built from validated inputs only. Exported so tests
 * pin it: a `--match-head-commit` dropped here would arm a floating head,
 * and an `--admin` smuggled here would bypass the gate this module exists
 * to enforce. Neither has any other call path.
 */
export function buildArmArgv(repository, number, headSha) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'arm repository is invalid')
  requireValue(Number.isSafeInteger(number) && number > 0 && number <= 2147483647, 'arm PR number is invalid')
  requireValue(typeof headSha === 'string' && SHA.test(headSha), 'arm head sha is invalid')
  return ['pr', 'merge', String(number), '--repo', repository,
    '--auto', '--squash', '--match-head-commit', headSha]
}

/**
 * The only writer this helper performs. No `--admin`, no manual merge, no
 * ruleset edit: the argv is fixed above, and inputs are re-validated here so
 * a caller cannot widen it. Scoped to one repository like every other call.
 */
export function createAutoMergeArmer({ run = execute } = {}) {
  requireValue(typeof run === 'function', 'arm transport is invalid')
  return async function armAutoMerge(repository, number, headSha) {
    const argv = buildArmArgv(repository, number, headSha)
    try {
      await run('gh', argv, { encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024 })
    } catch {
      // Child streams may carry credentials or private bodies.
      throw new Error('GitHub arm failed: transport, timeout or output limit')
    }
    return argv
  }
}

/**
 * The live branch-gate reader: repo settings plus the base branch's
 * rulesets (`rules/branches/{branch}`) in two REST calls, scoped to one
 * repository. The review pin comes from the `required_status_checks` rule
 * entries (context + `integration_id`); an any-source entry (null
 * `integration_id`) carries no source and never counts as pinned, but it
 * still names a required check and never breaks the read. Merge methods
 * come from the repo's allow_* flags. Anything missing or malformed
 * throws: an unreadable gate refuses by error, never by guessing.
 */
export function createBranchGateReader({ run = execute, repository } = {}) {
  requireValue(typeof run === 'function', 'gate transport is invalid')
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'gate repository is invalid')
  const scopedRepository = repository
  const [owner, name] = scopedRepository.split('/')

  async function rest(path) {
    let result
    try {
      const { stdout } = await run('gh', ['api', '--hostname', 'github.com', `repos/${owner}/${name}/${path}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
      })
      result = JSON.parse(stdout)
    } catch {
      throw new Error('GitHub gate read failed: transport, timeout, output limit or invalid JSON')
    }
    requireValue(result && typeof result === 'object' && !Array.isArray(result),
      'GitHub gate response is incomplete')
    return result
  }

  async function rules(branch) {
    let result
    try {
      const { stdout } = await run('gh', ['api', '--hostname', 'github.com',
        `repos/${owner}/${name}/rules/branches/${encodeURIComponent(branch)}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
      })
      result = JSON.parse(stdout)
    } catch {
      throw new Error('GitHub gate read failed: transport, timeout, output limit or invalid JSON')
    }
    // The rules endpoint answers a JSON list (one entry per ruleset rule):
    // a branch with no ruleset answers [], which reads truthfully as "no
    // requirements" and refuses below, never as an error.
    requireValue(Array.isArray(result), 'GitHub branch rules are incomplete')
    return result
  }

  return async function getBranchGate(repository, branch) {
    requireValue(repository === scopedRepository, 'gate repository is outside the reader scope')
    requireValue(isBranch(branch), 'gate branch is invalid')
    const repo = await rest('')
    requireValue(typeof repo.allow_auto_merge === 'boolean' &&
      typeof repo.allow_squash_merge === 'boolean' &&
      typeof repo.allow_merge_commit === 'boolean' &&
      typeof repo.allow_rebase_merge === 'boolean',
    'GitHub repository merge settings are incomplete')
    const requiredChecks = new Set()
    let review = null
    for (const rule of await rules(branch)) {
      if (rule?.type !== 'required_status_checks') continue
      const checks = rule?.parameters?.required_status_checks
      requireValue(Array.isArray(checks), 'GitHub required checks are incomplete')
      for (const check of checks) {
        requireValue(check && typeof check.context === 'string' && check.context.length > 0,
          'GitHub required check name is invalid')
        const source = check.integration_id ?? null
        requireValue(source === null || (Number.isSafeInteger(source) && source > 0),
          'GitHub required check source is invalid')
        requiredChecks.add(check.context)
        // The first pinned entry for the review check is the pin. An
        // any-source entry (null) never counts, and the decider still
        // compares the pin against the trusted App id from policy.
        if (review === null && check.context === REVIEW_CHECK &&
          Number.isSafeInteger(source) && source > 0) {
          review = { context: check.context, appId: source }
        }
      }
    }
    const mergeMethods = []
    if (repo.allow_merge_commit === true) mergeMethods.push('merge')
    if (repo.allow_squash_merge === true) mergeMethods.push('squash')
    if (repo.allow_rebase_merge === true) mergeMethods.push('rebase')
    return {
      repository, branch,
      review,
      requiredChecks: [...requiredChecks].sort(),
      allowAutoMerge: repo.allow_auto_merge,
      mergeMethods,
    }
  }
}

function emptyCounters() {
  return { total: 0, evaluated: 0, armed: 0, refused: 0, errors: 0, confirmations: 0 }
}

// Fixed words plus validated repo/number/head only. Label names, titles and
// bodies are never echoed: they are collaborator- or fork-controlled text.
function logLine(repository, number, headSha, decision, reason) {
  return `closeout-arm ${repository}#${number}@${headSha}: ${decision} (${reason})`
}

async function defaultOpenConfirmation({ issueId, repository, number }, env) {
  return openMergeConfirmation({
    issueId, repository, number,
    baseUrl: env.PAPERCLIP_API_URL,
    token: env.PAPERCLIP_API_KEY,
    runId: env.PAPERCLIP_RUN_ID ?? null,
  })
}

/**
 * Dependencies: github.getCloseoutSnapshot(repo, number, { nowMs }),
 * github.getBranchGate(repo, branch), armAutoMerge(repo, number, headSha), and
 * openConfirmation({ issueId, repository, number }). There is deliberately
 * no disarm and no merge dependency: this helper cannot perform either
 * operation even if asked to.
 */
export function createArmer({ github, armAutoMerge, openConfirmation = null, policy,
  env = process.env, nowMs = () => Date.now() }) {
  requireValue(github && typeof github.getCloseoutSnapshot === 'function' &&
    typeof github.getBranchGate === 'function',
  'arm duty requires a snapshot reader and a branch-gate reader, and nothing else')
  requireValue(typeof armAutoMerge === 'function', 'arm duty requires an arm writer')
  requireValue(openConfirmation === null || typeof openConfirmation === 'function',
    'arm confirmation opener is invalid')
  const validated = armPolicy(policy)
  requireValue(typeof nowMs === 'function', 'arm clock is invalid')
  const confirm = openConfirmation ?? ((input) => defaultOpenConfirmation(input, env))

  /**
   * Arm each referenced PR once, at open. One unreadable PR is a counted
   * error record, never a thrown run. A refusal makes no gh call and opens
   * no confirmation; a failed arm opens no confirmation; a failed
   * confirmation after a successful arm is a counted error on an armed
   * record, never a silent loss.
   *
   * @returns {{ records, counters }} records carry { repository, number,
   *   headSha, decision: 'armed' | 'none', reason, confirmed, log }.
   */
  async function runArm({ references, issueId, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(references) && references.length > 0 && references.length <= MAX_ARM_REFS,
      'arm references are invalid')
    requireValue(typeof issueId === 'string' && UUID.test(issueId), 'arm needs an issue UUID')
    const now = nowOverride ?? nowMs()
    requireValue(Number.isInteger(now) && now >= 0, 'arm clock is invalid')
    const seen = new Set()
    const records = []
    const counters = emptyCounters()
    const refusals = {}
    for (const ref of references) {
      requireValue(isRef(ref), 'arm reference is invalid')
      const key = `${ref.repository}#${ref.number}`
      requireValue(!seen.has(key), 'arm references hold a duplicate PR')
      seen.add(key)
      counters.total++
      let snapshot
      try {
        snapshot = await github.getCloseoutSnapshot(ref.repository, ref.number, { nowMs: now })
      } catch {
        records.push({ ...ref, headSha: null, decision: 'none', reason: 'snapshot-read-failed', confirmed: false,
          log: logLine(ref.repository, ref.number, 'unknown', 'none', 'snapshot-read-failed'),
          error: 'snapshot-read-failed' })
        counters.errors++
        continue
      }
      let gate
      try {
        gate = await github.getBranchGate(ref.repository, snapshot.baseRef ?? null)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha ?? null, decision: 'none', reason: 'gate-read-failed',
          confirmed: false,
          log: logLine(ref.repository, ref.number, snapshot.headSha ?? 'unknown', 'none', 'gate-read-failed'),
          error: 'gate-read-failed' })
        counters.errors++
        continue
      }
      let verdict
      try {
        verdict = decideArm({ snapshot, gate, policy: validated })
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha ?? null, decision: 'none', reason: 'undecidable-snapshot',
          confirmed: false,
          log: logLine(ref.repository, ref.number, snapshot.headSha ?? 'unknown', 'none', 'undecidable-snapshot'),
          error: 'undecidable-snapshot' })
        counters.errors++
        continue
      }
      counters.evaluated++
      if (verdict.armed !== true) {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason: verdict.reason,
          confirmed: false,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', verdict.reason) })
        counters.refused++
        refusals[verdict.reason] = (refusals[verdict.reason] ?? 0) + 1
        continue
      }
      try {
        await armAutoMerge(ref.repository, ref.number, snapshot.headSha)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason: 'arm-failed',
          confirmed: false,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', 'arm-failed'),
          error: 'arm-failed' })
        counters.errors++
        continue
      }
      let confirmed = false
      try {
        await confirm({ issueId, repository: ref.repository, number: ref.number })
        confirmed = true
        counters.confirmations++
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'armed', reason: 'armed',
          confirmed: false,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'armed', 'armed') + ' confirmation-failed',
          error: 'confirmation-failed' })
        counters.armed++
        counters.errors++
        continue
      }
      records.push({ ...ref, headSha: snapshot.headSha, decision: 'armed', reason: 'armed',
        confirmed,
        log: logLine(ref.repository, ref.number, snapshot.headSha, 'armed', 'armed') })
      counters.armed++
    }
    return { records, counters, refusals }
  }

  return { runArm }
}

function usage() {
  return [
    'usage: node src/closeout-arm.js --issue <uuid> --repo <owner/repo> --pr <N> [--dry-run]',
    '',
    'Arms Phase-B auto-merge once, at open, only when the base branch rules',
    'show a promoted gate. --dry-run reads and prints the decision without',
    'arming. Credentials come from the environment (gh credential for reads,',
    'PAPERCLIP_API_URL, PAPERCLIP_API_KEY, optional PAPERCLIP_RUN_ID for the',
    'merge confirmation), never from argv.',
  ].join('\n')
}

function parseArgs(argv) {
  const args = { issue: null, repo: null, pr: null, dryRun: false }
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i]
    if (flag === '--dry-run') { args.dryRun = true; continue }
    if (flag === '--help' || flag === '-h') return { help: true }
    const next = argv[i + 1]
    if ((flag === '--issue' || flag === '--repo' || flag === '--pr') && typeof next === 'string') {
      args[flag.slice(2)] = next
      i += 1
      continue
    }
    throw new Error(`unknown argument: ${flag}\n${usage()}`)
  }
  return args
}

function policyFromEnv(env) {
  const reviewAppId = Number(env.CLOSEOUT_REVIEW_APP_ID ?? '')
  return armPolicy({ reviewAppId })
}

async function main(argv, env) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write(`${usage()}\n`)
    return 0
  }
  if (args.issue === null || args.repo === null || args.pr === null) throw new Error(`missing argument\n${usage()}`)
  const number = Number(args.pr)
  const policy = policyFromEnv(env)
  const adapter = createGithubAdapter({ allowedRepositories: [args.repo] })
  const github = { getCloseoutSnapshot: adapter.getCloseoutSnapshot,
    getBranchGate: createBranchGateReader({ repository: args.repo }) }
  if (args.dryRun) {
    const snapshot = await github.getCloseoutSnapshot(args.repo, number, {})
    const gate = await github.getBranchGate(args.repo, snapshot.baseRef)
    const verdict = decideArm({ snapshot, gate, policy: policy })
    process.stdout.write(`${JSON.stringify({ ...verdict, argv: verdict.armed === true
      ? buildArmArgv(args.repo, number, snapshot.headSha) : null }, null, 2)}\n`)
    return 0
  }
  const { records } = await createArmer({ github, armAutoMerge: createAutoMergeArmer(), policy }).runArm({
    references: [{ repository: args.repo, number }], issueId: args.issue,
  })
  process.stdout.write(`${JSON.stringify(records[0], null, 2)}\n`)
  // An arm without its S5b confirmation is a loud failure, never success:
  // a rerun refuses `already-armed`, so exit non-zero must surface the
  // missing confirmation instead of reporting a clean arm.
  return records[0].decision === 'armed' && records[0].confirmed === true ? 0 : 1
}

// Shell entrypoint only; importing this module never arms.
const invokedAsScript = process.argv[1] !== undefined &&
  import.meta.url === new URL(`file://${process.argv[1]}`, 'file://').href
if (invokedAsScript) {
  let code = 0
  try {
    code = await main(process.argv.slice(2), process.env)
  } catch (error) {
    process.stderr.write(`closeout-arm: ${error instanceof Error ? error.message.split('\n')[0] : 'failed'}\n`)
    code = /unknown argument|missing argument/.test(error instanceof Error ? error.message : '') ? 2 : 1
  }
  process.exit(code)
}
