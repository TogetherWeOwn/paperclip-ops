// S8b: main-head observation only. Enrollment here is never incident-writing
// authority: the duty receives EMPTY live enrollment and a refusing store.
import { createRedmainWatcher, DEFAULT_CI_OWNER } from './closeout-redmain.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const REASONS = ['flag-off-shadow', 'incident-already-open', 'main-green-no-incident', 'main-pending',
  'main-head-read-failed', 'undecidable-head', 'incident-store-unreadable',
  'incident-open-failed', 'incident-recover-failed', 'budget-deferred', 'observation-failed']
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

export function closeoutRedmainSettings(config) {
  const block = config?.closeout?.redmainShadow
  if (block === undefined) return null
  requireValue(block !== null && typeof block === 'object' && !Array.isArray(block) &&
    Object.keys(block).every((key) => key === 'enabledRepositories'),
  'closeout red-main shadow configuration holds an unknown key')
  const { enabledRepositories } = block
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'closeout red-main shadow enabledRepositories must be unique owner/repo names')
  requireValue(Array.isArray(config.allowedRepositories) &&
    enabledRepositories.every((r) => config.allowedRepositories.includes(r)),
  'closeout red-main shadow repositories must be allowed repositories')
  return enabledRepositories.length === 0 ? null
    : Object.freeze({ enabledRepositories: new Set(enabledRepositories) })
}

// Local admission refusal before reserve(), not a whole-pass request stop.
export class RedmainShadowDeferred extends Error {}

export function createRedmainShadowHook({ settings, getMainHeadChecks, check, stopReason = () => null,
  log = () => {}, nowMs = () => Date.now() }) {
  requireValue(settings?.enabledRepositories instanceof Set && typeof getMainHeadChecks === 'function' &&
    typeof check === 'function' && typeof stopReason === 'function' && typeof log === 'function' &&
    typeof nowMs === 'function', 'red-main shadow dependencies are invalid')
  const counters = { total: 0, evaluated: 0, red: 0, green: 0, pending: 0, opened: 0,
    wouldOpen: 0, alreadyOpen: 0, recovered: 0, wouldRecover: 0, noOp: 0, errors: 0,
    deferred: 0, logErrors: 0 }
  const byReason = Object.fromEntries(REASONS.map((reason) => [reason, 0]))
  const open = new Map()
  let deferred = false
  const watcher = createRedmainWatcher({ policy: { enabledRepositories: [] }, nowMs,
    github: { getMainHeadChecks: async (repository) => {
      try {
        const head = await getMainHeadChecks(repository)
        // The incident key is the requested repository, never a foreign read.
        requireValue(head?.repository === repository, 'red-main shadow head identity is invalid')
        return head
      } catch (error) { deferred = error instanceof RedmainShadowDeferred; throw error }
    } },
    incidents: {
      findOpen: async (repository) => open.get(repository) ?? null,
      openIncident: async () => { throw new Error('shadow red-main mutation refused') },
      closeIncident: async () => { throw new Error('shadow red-main mutation refused') },
    } })

  // Only a deadline originating inside the post-primary drain is contained.
  // Admission guards every transport call, including the required-check read,
  // so request-budget can never originate here. Cancellation always escapes.
  function containStop() {
    if (stopReason() !== 'deadline') throw new Error('consumer pass stopped')
    counters.total++
    counters.deferred++
    byReason['budget-deferred']++
  }
  async function observe(repository) {
    if (!settings.enabledRepositories.has(repository)) return
    try { check() }
    catch { containStop(); return }
    deferred = false
    let result
    try { result = await watcher.runRedmain({ repositories: [repository] }) }
    catch {
      try { check() }
      catch { containStop(); return }
      counters.total++
      counters.errors++
      byReason['observation-failed']++
      return
    }
    // The duty counts transport errors, including aborts. Re-check the shared
    // budget before counting a verdict or advancing even in-memory incidents.
    try { check() }
    catch { containStop(); return }
    counters.total++
    if (deferred) {
      counters.deferred++
      byReason['budget-deferred']++
      return
    }
    for (const key of ['evaluated', 'red', 'green', 'pending', 'opened', 'wouldOpen',
      'alreadyOpen', 'recovered', 'wouldRecover', 'noOp', 'errors']) counters[key] += result.counters[key]
    const record = result.records[0]
    if (Object.hasOwn(byReason, record.reason)) byReason[record.reason]++
    if (record.decision === 'would-open') {
      open.set(repository, { key: record.key, repository, firstRedSha: record.mainSha,
        ciOwner: DEFAULT_CI_OWNER, status: 'open' })
    } else if (record.decision === 'would-recover') open.delete(repository)
  }
  const summary = () => ({ counters: { ...counters }, byReason: { ...byReason } })
  function emit({ ignoreStop = false } = {}) {
    if (!ignoreStop) check()
    // Fixed counters/reason labels only, as in the disarm drain. Never forward
    // duty records/log strings: malformed heads can carry unvalidated SHAs.
    try { log(JSON.stringify({ event: 'closeout-redmain-shadow', ...summary() })) }
    catch { counters.logErrors++ }
  }
  return { observe, summary, emit }
}
