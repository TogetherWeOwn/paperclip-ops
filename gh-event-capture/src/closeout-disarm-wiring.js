// S6c: scheduled sweep observation only. No board, stores, debounce or event
// routing. Reuse S6b's classifier with EMPTY live enrollment and a refusing
// writer; observation enrollment must never become disarm authority.
import { createDisarmer } from './closeout-disarm.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const REASONS = ['armed-drafted', 'armed-held', 'armed-drafted-held', 'closed', 'unarmed', 'clean',
  'snapshot-read-failed', 'undecidable-snapshot', 'budget-deferred', 'observation-failed']
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

// The parent closeout key whitelist is shared with S3c. This child has only
// enrollment: its name fixes the mode to shadow; no live/apply/mode key exists.
export function closeoutDisarmSettings(config) {
  const block = config?.closeout?.disarmShadow
  if (block === undefined) return null
  requireValue(block !== null && typeof block === 'object' && !Array.isArray(block) &&
    Object.keys(block).every((key) => key === 'enabledRepositories'),
  'closeout disarm shadow configuration holds an unknown key')
  const { enabledRepositories } = block
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'closeout disarm shadow enabledRepositories must be unique owner/repo names')
  requireValue(Array.isArray(config.allowedRepositories) &&
    enabledRepositories.every((r) => config.allowedRepositories.includes(r)),
  'closeout disarm shadow repositories must be allowed repositories')
  return enabledRepositories.length === 0 ? null
    : Object.freeze({ enabledRepositories: new Set(enabledRepositories) })
}

// Local admission refusal, not a whole-pass stop. The transport uses the SAME
// pass counter; this only leaves request headroom for primary work.
export class DisarmShadowDeferred extends Error {}

export function createDisarmShadowHook({ settings, getCloseoutSnapshot, check, log = () => {}, stopReason = () => null }) {
  requireValue(settings?.enabledRepositories instanceof Set && typeof getCloseoutSnapshot === 'function' &&
    typeof check === 'function' && typeof log === 'function' && typeof stopReason === 'function',
  'disarm shadow dependencies are invalid')
  const counters = { total: 0, evaluated: 0, needsDisarm: 0, disarmed: 0, wouldDisarm: 0, noOp: 0,
    errors: 0, deferred: 0, logErrors: 0 }
  const byReason = Object.fromEntries(REASONS.map((reason) => [reason, 0]))
  let deferred = false
  const disarmer = createDisarmer({ policy: { enabledRepositories: [] }, github: {
    getCloseoutSnapshot: async (...args) => {
      try { return await getCloseoutSnapshot(...args) }
      catch (error) { deferred = error instanceof DisarmShadowDeferred; throw error }
    },
    disableAutoMerge: async () => { throw new Error('shadow disarm mutation refused') },
  } })

  // A deadline stop that fires while the post-primary drain is reading is
  // caused by observation, not primary work. Contain it as a local budget
  // deferral so enabling the shadow cannot fail an otherwise-OK pass.
  // External cancellation still stops the pass, as does any stop the caller
  // cannot classify (stopReason() === null). Request-budget is deliberately
  // not contained: admission defers at requests >= maxRequests before
  // reserve(), so no drain read can set it; a request-budget stop seen here
  // predates the drain and must fail the pass exactly as with the shadow OFF.
  function containStop() {
    const reason = stopReason()
    if (reason !== 'deadline') throw new Error('consumer pass stopped')
    counters.total++
    counters.deferred++
    byReason['budget-deferred']++
  }
  async function observe(reference) {
    if (!settings.enabledRepositories.has(reference.repository)) return
    try { check() }
    catch { containStop(); return }
    deferred = false
    let result
    try { result = await disarmer.runDisarm({ references: [reference] }) }
    catch {
      try { check() }
      catch { containStop(); return }
      counters.total++
      counters.errors++
      byReason['observation-failed']++
      return
    }
    // S6b catches snapshot errors, including cancellation. Re-check the shared
    // pass BEFORE counting a decision, another read or product checkpoint.
    try { check() }
    catch { containStop(); return }
    counters.total++
    if (deferred) {
      counters.deferred++
      byReason['budget-deferred']++
      return
    }
    for (const key of ['evaluated', 'needsDisarm', 'disarmed', 'wouldDisarm', 'noOp', 'errors']) {
      counters[key] += result.counters[key]
    }
    const reason = result.records[0].reason
    // Never emit helper records/log strings or unvalidated API fields.
    if (Object.hasOwn(byReason, reason)) byReason[reason]++
  }
  const summary = () => ({ counters: { ...counters }, byReason: { ...byReason } })
  function emit({ ignoreStop = false } = {}) {
    // A drain-contained stop still gets its summary: the runner passes
    // ignoreStop only when the stop arose from shadow reads after primary
    // success. Every other stopped pass stays silent, as before.
    if (!ignoreStop) check()
    try { log(JSON.stringify({ event: 'closeout-disarm-shadow', ...summary() })) }
    catch { counters.logErrors++ }
  }
  return { observe, summary, emit }
}
