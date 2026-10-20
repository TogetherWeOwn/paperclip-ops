// S9a: staging deploy-success observation only. Enrollment here is never
// intent-writing authority: the duty receives EMPTY live enrollment and a
// refusing intent store. No suppression input: red-main counters and private
// incident state are not connected to this hook.
import { createDeployWatcher } from './closeout-deploy.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const REASONS = ['budget-deferred', 'observation-failed']
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

// The parent closeout key whitelist is shared with S3c. This child has only
// enrollment: its name fixes the mode to shadow; no live/apply/mode key exists.
export function closeoutDeploySettings(config) {
  const block = config?.closeout?.deployShadow
  if (block === undefined) return null
  requireValue(block !== null && typeof block === 'object' && !Array.isArray(block) &&
    Object.keys(block).every((key) => key === 'enabledRepositories'),
  'closeout deploy shadow configuration holds an unknown key')
  const { enabledRepositories } = block
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'closeout deploy shadow enabledRepositories must be unique owner/repo names')
  requireValue(Array.isArray(config.allowedRepositories) &&
    enabledRepositories.every((r) => config.allowedRepositories.includes(r)),
  'closeout deploy shadow repositories must be allowed repositories')
  return enabledRepositories.length === 0 ? null
    : Object.freeze({ enabledRepositories: new Set(enabledRepositories) })
}

// Local admission refusal before reserve(), not a whole-pass request stop.
export class DeployShadowDeferred extends Error {}

export function createDeployShadowHook({ settings, capture, check, stopReason = () => null,
  log = () => {}, nowMs = () => Date.now() }) {
  requireValue(settings?.enabledRepositories instanceof Set && capture &&
    typeof capture.listDeliveries === 'function' && typeof capture.getDelivery === 'function' &&
    typeof check === 'function' && typeof stopReason === 'function' && typeof log === 'function' &&
    typeof nowMs === 'function', 'deploy shadow dependencies are invalid')
  const counters = { total: 0, deliveries: 0, eligible: 0, recorded: 0, wouldRecord: 0,
    alreadyRecorded: 0, suppressed: 0, dropped: 0, nonSuccess: 0, nonStaging: 0,
    overBudget: 0, renderFailed: 0, errors: 0, deferred: 0, logErrors: 0 }
  const byReason = Object.fromEntries(REASONS.map((reason) => [reason, 0]))
  let deferred = false
  const watcher = createDeployWatcher({
    capture: {
      listDeliveries: async (...args) => {
        try { return await capture.listDeliveries(...args) }
        catch (error) { deferred = error instanceof DeployShadowDeferred; throw error }
      },
      getDelivery: async (...args) => {
        let row
        try { row = await capture.getDelivery(...args) }
        catch (error) { deferred = error instanceof DeployShadowDeferred; throw error }
        // The adapter checks event membership, not the requested event. A row
        // listed as deployment_status that reads back otherwise changed
        // identity; the duty must not classify it. Fixed words only.
        requireValue(row?.event === 'deployment_status', 'deploy shadow delivery changed event')
        return row
      },
    },
    intents: {
      // Hook-local memory only: nothing was ever recorded, so no key is known.
      findIntent: async () => null,
      recordIntent: async () => { throw new Error('shadow deploy mutation refused') },
    },
    policy: { enabledRepositories: [] },
    allowedRepositories: [...settings.enabledRepositories],
    isSuppressed: null,
    nowMs,
  })

  // Only a deadline originating inside the post-primary drain is contained.
  // Admission guards every transport call, including each listing page and
  // body read, so request-budget can never originate here. Cancellation
  // always escapes. Duty records are never forwarded: reasons carry SHAs.
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
    try { result = await watcher.runDeploy({ repositories: [repository] }) }
    catch {
      try { check() }
      catch { containStop(); return }
      counters.total++
      counters.errors++
      byReason['observation-failed']++
      return
    }
    // The duty counts transport errors, including aborts. Re-check the shared
    // budget before merging a verdict: a stop during the scan is a deferral
    // (deadline) or an escape (anything else), never a measured observation.
    try { check() }
    catch { containStop(); return }
    counters.total++
    if (deferred) {
      counters.deferred++
      byReason['budget-deferred']++
      return
    }
    for (const key of ['deliveries', 'eligible', 'recorded', 'wouldRecord',
      'alreadyRecorded', 'suppressed', 'dropped', 'nonSuccess', 'nonStaging',
      'overBudget', 'renderFailed', 'errors']) counters[key] += result.counters[key]
  }
  const summary = () => ({ counters: { ...counters }, byReason: { ...byReason } })
  function emit({ ignoreStop = false } = {}) {
    if (!ignoreStop) check()
    // Fixed counters/reason labels only, as in the sibling drains. Never
    // forward duty records/log strings: delivery bodies carry untrusted text.
    try { log(JSON.stringify({ event: 'closeout-deploy-shadow', ...summary() })) }
    catch { counters.logErrors++ }
  }
  return { observe, summary, emit }
}
