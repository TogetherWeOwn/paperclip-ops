// One product-only event pass. No timer, retries, wake transport or background loop.
import { execFile } from 'node:child_process'
import { performance } from 'node:perf_hooks'
import { createCaptureAdapter } from './capture-adapter.js'
import { createGithubAdapter } from './github-adapter.js'
import { createPaperclipAdapter } from './paperclip-adapter.js'
import { createConsumer } from './consumer.js'
import { withReceiptStore } from './receipt-store.js'
import { receiptNamespace, runReceiptCycle } from './receipt-cycle.js'
import { loadSweepState, saveSweepState } from './sweep-store.js'
import { runSweepCycle, sweepLimits } from './sweep-cycle.js'
import { loadPassSchedule, savePassSchedule } from './pass-schedule.js'
import { refreshPrTaskIndex } from './pr-task-index-source.js'
import { closeoutShadowSettings, createShadowReviewHook } from './closeout-shadow-wiring.js'
import { closeoutDisarmSettings, createDisarmShadowHook, DisarmShadowDeferred } from './closeout-disarm-wiring.js'
import { closeoutRedmainSettings, createRedmainShadowHook, RedmainShadowDeferred } from './closeout-redmain-wiring.js'
import { closeoutDeploySettings, createDeployShadowHook, DeployShadowDeferred } from './closeout-deploy-wiring.js'

// execFile's abort callback can fire before child close. Await close explicitly;
// a SIGTERM-ignoring gh process must not survive this pass's deadline.
export function executeGithub(file, args, options) {
  return new Promise((resolve, reject) => {
    const { signal, ...childOptions } = options
    if (signal?.aborted) { reject(new Error('child execution cancelled')); return }
    let result
    let failure
    let cancelled = false
    // execFile's built-in AbortSignal path can send SIGTERM despite killSignal.
    // Own abort explicitly so a signal-ignoring child is killed, then drained.
    const child = execFile(file, args, { ...childOptions, killSignal: 'SIGKILL' }, (error, stdout) => {
      failure = error
      result = { stdout }
    })
    const cancel = () => { cancelled = true; child.kill('SIGKILL') }
    signal?.addEventListener('abort', cancel, { once: true })
    if (signal?.aborted) cancel()
    child.once('close', () => {
      signal?.removeEventListener('abort', cancel)
      if (cancelled) reject(new Error('child execution cancelled'))
      else if (failure) reject(failure)
      else resolve(result)
    })
  })
}
const MODE = 'products-only-v1'
const DEFAULTS = { maxRequests: 300, durationMs: 45000, maxDeliveries: 100,
  pageSize: 100, maxPages: 100, maxReceipts: 100000, maxReceiptBytes: 16 * 1024 * 1024,
  maxSweepItems: 50, maxSweepAttempts: 5, reviewIntervalMs: 600000 }
const MAXIMA = { maxRequests: 10000, durationMs: 300000, maxDeliveries: 10000,
  pageSize: 500, maxPages: 1000, maxReceipts: 1000000, maxReceiptBytes: 128 * 1024 * 1024 }
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
export function runnerLimits(value = {}) {
  requireValue(value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).every((k) => Object.hasOwn(DEFAULTS, k)), 'invalid runner limits')
  const limits = { ...DEFAULTS, ...value }
  const { maxSweepItems, maxSweepAttempts, reviewIntervalMs, ...rest } = limits
  requireValue(Object.entries(rest).every(([key, n]) => Number.isInteger(n) && n > 0 && n <= MAXIMA[key]) &&
    limits.maxReceiptBytes >= 1024, 'invalid runner limits')
  // Sweep bounds live in one table (sweep-cycle.js); the runner only allowlists the keys.
  sweepLimits({ maxItems: maxSweepItems, maxAttempts: maxSweepAttempts, reviewIntervalMs })
  return limits
}

// An aborted write/child must settle before the lock is released. An index-only
// timeout may yield to production while that same lock remains held for drainage.
// The deadline stays armed through HTTP body reads and every awaited operation.
// One clock owns the deadline: `now` measures elapsed budget at each checkpoint and
// `setTimer` arms the abort that interrupts in-flight I/O between them. Injecting only
// `now` would leave that abort on the real clock, so a caller virtualising time — the
// deadline tests — must replace both or the two halves disagree.
export function createPassBudget({ maxRequests, durationMs, signal, now = () => performance.now(),
  setTimer = setTimeout, clearTimer = clearTimeout }) {
  runnerLimits({ maxRequests, durationMs })
  const controller = new AbortController()
  const started = now()
  let requests = 0
  let reason = null
  const stop = (why) => { reason ??= why; controller.abort() }
  const cancel = () => stop('cancelled')
  const timer = setTimer(() => stop('deadline'), durationMs)
  signal?.addEventListener('abort', cancel, { once: true })
  if (signal?.aborted) cancel()
  function check() {
    if (now() - started >= durationMs) stop('deadline')
    requireValue(reason === null, 'consumer pass stopped')
  }
  function reserve() {
    check()
    if (requests >= maxRequests) stop('request-budget')
    check()
    requests++
  }
  return {
    check,
    signal: controller.signal,
    snapshot: () => ({ requests, reason }),
    fetch: (fetchImpl) => async (url, init) => {
      reserve()
      return fetchImpl(url, { ...init, signal: AbortSignal.any([controller.signal, init.signal]) })
    },
    run: (run) => async (file, args, options = {}) => {
      reserve()
      const result = await run(file, args, { ...options, signal: controller.signal,
        timeout: Math.max(1, Math.min(options.timeout ?? 30000, Math.ceil(durationMs - (now() - started)))) })
      check()
      return result
    },
    close: () => { clearTimer(timer); signal?.removeEventListener('abort', cancel) },
  }
}

// This race only selects when production may start. The caller retains the
// receipt lock and drains task in finally, even if filesystem I/O is still pending.
async function beforeIndexDeadline(task, signal) {
  if (signal.aborted) return null
  let onAbort
  const expired = new Promise((resolve) => {
    onAbort = () => resolve(null)
    signal.addEventListener('abort', onAbort, { once: true })
    if (signal.aborted) onAbort()
  })
  try { return await Promise.race([task, expired]) }
  finally { signal.removeEventListener('abort', onAbort) }
}

export async function runProductPass({ config, captureToken, boardToken, runId = null, githubExecutable = 'gh' },
  { fetchImpl = globalThis.fetch, run = executeGithub, signal, clock = {}, log = () => {} } = {}) {
  requireValue(config?.mode === MODE, 'runner requires explicit products-only-v1 mode')
  const limits = runnerLimits(config.limits)
  const namespace = receiptNamespace(config)
  // Optional S3c shadow review: absent block = null = the unchanged sweep. A bad block refuses before any effect.
  const shadow = closeoutShadowSettings(config)
  const disarmSettings = closeoutDisarmSettings(config)
  const redmainSettings = closeoutRedmainSettings(config)
  const deploySettings = closeoutDeploySettings(config)
  const budget = createPassBudget({ ...limits, signal, ...clock })
  let disarmShadow = null
  let redmainShadow = null
  let deployShadow = null
  const disarmReferences = disarmSettings === null ? null : []
  const observation = () => ({
    ...(disarmShadow === null ? {} : { disarmShadow: disarmShadow.summary() }),
    ...(redmainShadow === null ? {} : { redmainShadow: redmainShadow.summary() }),
    ...(deployShadow === null ? {} : { deployShadow: deployShadow.summary() }),
  })
  let events = null
  let sweeps = null
  let shadowIndex = null
  try {
    const scoped = { allowedRepositories: config.allowedRepositories }
    const transport = budget.fetch(fetchImpl)
    const capture = createCaptureAdapter({ ...scoped, baseUrl: config.captureOrigin, queryToken: captureToken,
      fetchImpl: transport, pageSize: limits.pageSize, maxPages: limits.maxPages })
    const github = createGithubAdapter({ ...scoped,
      run: budget.run((_file, args, options) => run(githubExecutable, args, options)), maxPages: limits.maxPages })
    if (disarmSettings !== null) {
      // Scheduled refs are collected before mapping, but reads wait until both
      // primary streams and S3c/index work settle. Unknown event/review fanout
      // cannot steal primary requests or pin observation to one stream/repo.
      // The same counter/deadline admits only surplus. No mutators escape.
      const reader = createGithubAdapter({ ...scoped, maxPages: limits.maxPages,
        run: budget.run((_file, args, options) => run(githubExecutable, args, options)) })
      disarmShadow = createDisarmShadowHook({ settings: disarmSettings, check: budget.check, log,
        stopReason: () => budget.snapshot().reason,
        getCloseoutSnapshot: (...args) => {
          budget.check()
          if (budget.snapshot().requests >= limits.maxRequests) throw new DisarmShadowDeferred()
          return reader.getCloseoutSnapshot(...args)
        } })
    }
    if (redmainSettings !== null) {
      let admissionError = null
      const execute = budget.run((_file, args, options) => run(githubExecutable, args, options))
      const reader = createGithubAdapter({ ...scoped, maxPages: limits.maxPages,
        run: (...args) => {
          // getMainHeadChecks reads both the head and branch protection. Guard
          // EACH transport admission, not just the outer method's first call.
          budget.check()
          if (budget.snapshot().requests >= limits.maxRequests) {
            admissionError = new RedmainShadowDeferred()
            throw admissionError
          }
          return execute(...args)
        } })
      redmainShadow = createRedmainShadowHook({ settings: redmainSettings, check: budget.check, log,
        stopReason: () => budget.snapshot().reason,
        getMainHeadChecks: async (...args) => {
          admissionError = null
          try { return await reader.getMainHeadChecks(...args) }
          // The adapter sanitizes transport errors. Preserve only our local
          // admission sentinel across that boundary, never provider text.
          catch (error) { throw admissionError ?? error }
        } })
    }
    if (deploySettings !== null) {
      let admissionError = null
      const execute = transport
      const reader = createCaptureAdapter({ ...scoped, baseUrl: config.captureOrigin, queryToken: captureToken,
        fetchImpl: (url, init) => {
          // listDeliveries pages and each body read each admit separately:
          // guard EVERY transport admission, not just the outer call.
          budget.check()
          if (budget.snapshot().requests >= limits.maxRequests) {
            admissionError = new DeployShadowDeferred()
            throw admissionError
          }
          return execute(url, init)
        }, pageSize: limits.pageSize, maxPages: limits.maxPages })
      const guarded = (method) => async (...args) => {
        admissionError = null
        try { return await reader[method](...args) }
        // The adapter sanitizes transport errors. Preserve only our local
        // admission sentinel across that boundary, never provider text.
        catch (error) { throw admissionError ?? error }
      }
      deployShadow = createDeployShadowHook({ settings: deploySettings, check: budget.check, log,
        stopReason: () => budget.snapshot().reason,
        capture: { listDeliveries: guarded('listDeliveries'), getDelivery: guarded('getDelivery') } })
    }
    const board = createPaperclipAdapter({ ...scoped, baseUrl: config.boardOrigin, token: boardToken,
      companyId: config.companyId, runId, fetchImpl: transport })
    const consumer = createConsumer({ ...scoped, github, board, capture, mode: MODE })
    budget.check()
    // One lock and one total budget. Persist the next starting stream BEFORE
    // work so an exhausted/crashed capture pass cannot starve reconciliation.
    // schedule.json holds only fairness hints, separate from success receipts.
    // Declared outside the lock callback: the post-callback containment read
    // must see whether the drain was entered, and a callback-local binding
    // would throw ReferenceError on the success path (a cycle-failed outage).
    let shadowDrained = false
    await withReceiptStore({ directory: config.stateDirectory, namespace,
      maxReceipts: limits.maxReceipts, maxBytes: limits.maxReceiptBytes }, async (receipts) => {
      budget.check()
      const storage = { directory: config.stateDirectory, namespace }
      const schedule = await loadPassSchedule(storage)
      const saveSchedule = () => savePassSchedule({ ...storage, state: schedule })
      const first = schedule.nextStream
      schedule.nextStream = first === 'events' ? 'sweeps' : 'events'
      await saveSchedule()
      const guarded = { ...receipts, record: async (...args) => {
        budget.check()
        await receipts.record(...args)
      } }
      let indexTask = null
      try {
        if (shadow !== null) {
          // The quarter-duration deadline releases production even when local
          // cache I/O cannot be aborted. No late index result enters this shadow.
          const indexBudget = createPassBudget({ maxRequests: Math.max(1, Math.floor((limits.maxRequests - 1) / 2)),
            durationMs: Math.max(1, Math.floor(limits.durationMs / 4)), signal: budget.signal, ...clock })
          indexTask = (async () => {
            try {
              const indexBoard = createPaperclipAdapter({ ...scoped, baseUrl: config.boardOrigin, token: boardToken,
                companyId: config.companyId, runId, fetchImpl: indexBudget.fetch(transport) })
              return await refreshPrTaskIndex({ board: indexBoard, directory: config.stateDirectory, namespace,
                allowedRepositories: config.allowedRepositories, indexReadsPerPass: shadow.indexReadsPerPass, log,
                canRead: () => { try { indexBudget.check(); budget.check(); return true } catch { return false } } })
            } finally { indexBudget.close() }
          })()
          shadowIndex = await beforeIndexDeadline(indexTask, indexBudget.signal)
        }
        for (const stream of [first, schedule.nextStream]) {
          budget.check()
          if (stream === 'events') {
            events = await runReceiptCycle({ ...scoped, capture, consumer, receipts: guarded,
              mode: MODE, maxDeliveries: limits.maxDeliveries, beforeDelivery: budget.check,
              afterFingerprint: schedule.eventCursor,
              beforeAttempt: async fingerprint => { schedule.eventCursor = fingerprint; await saveSchedule() } })
          } else {
            const state = await loadSweepState(storage)
            sweeps = await runSweepCycle({ ...scoped, github, consumer, state,
              maxItems: limits.maxSweepItems, maxAttempts: limits.maxSweepAttempts,
              reviewIntervalMs: limits.reviewIntervalMs, beforeItem: budget.check,
              isStopped: () => budget.snapshot().reason !== null,
              afterRepository: schedule.sweepCursor,
              beforeRepository: async repository => { schedule.sweepCursor = repository; await saveSchedule() },
              beforeReconcile: disarmShadow === null ? null : async (reference) => {
                disarmReferences.push(reference)
              },
              // Shadow only: no board, no claim save, no comment credential reach this hook.
              closeoutReview: shadowIndex?.index == null ? null : createShadowReviewHook({ settings: shadow, namespace,
                directory: config.stateDirectory, github, index: shadowIndex.index, log,
                stopped: () => budget.snapshot().reason !== null }),
              // Wall time survives restarts; the shared monotonic budget bounds I/O.
              now: () => Date.now(), save: () => saveSweepState({ ...storage, state }) })
          }
          budget.check()
        }
      } finally {
        // Never release the receipt lock while a cache write/read is in flight.
        // A long drain can still exhaust the whole pass, after production ran.
        if (indexTask !== null) await indexTask
      }
      // Bounded by maxSweepItems; no persistent queue, replay or extra listing.
      // Keep the receipt lock and await every read through existing drainage.
      // The pre-drain check pins containment to stops that start INSIDE the
      // drain: an S3c index overrun that already stopped the pass (the finally
      // drain above holds the lock with no budget check) fails here exactly as
      // with the shadow OFF, instead of being hidden by observation.
      if (disarmShadow !== null || redmainShadow !== null || deployShadow !== null) {
        budget.check()
        shadowDrained = true
        if (disarmShadow !== null) {
          for (const reference of disarmReferences) await disarmShadow.observe(reference)
        }
        if (redmainShadow !== null) {
          for (const repository of redmainSettings.enabledRepositories) await redmainShadow.observe(repository)
        }
        if (deployShadow !== null) {
          for (const repository of deploySettings.enabledRepositories) await deployShadow.observe(repository)
        }
      }
    })
    // A deadline stop observed here arose from the post-primary shadow drain
    // (the pre-drain check threw earlier for anything already stopped), so it
    // keeps the primary outcome: the summary still emits and the pass stays
    // OK. External cancellation still stops the pass, as does a stop with no
    // shadow. Request-budget cannot trip inside the drain: admission defers at
    // requests >= maxRequests before reserve(), so it is never contained.
    const drainStop = budget.snapshot().reason
    const shadowContained = shadowDrained && drainStop === 'deadline'
    if (!shadowContained) budget.check()
    const ok = events.ok && sweeps.ok
    for (const hook of [disarmShadow, redmainShadow, deployShadow]) {
      if (hook === null) continue
      if (shadowContained) hook.emit({ ignoreStop: true })
      else { hook.emit(); budget.check() }
    }
    return { ok, mode: MODE, reason: ok ? null : events.ok ? 'sweeps-incomplete' : 'incomplete',
      requests: budget.snapshot().requests, events, sweeps, ...observation() }
  } catch {
    // Only fixed reason labels escape. Even local filesystem errors can contain
    // a credential path; adapters/provider errors must not enter service logs.
    for (const hook of [disarmShadow, redmainShadow, deployShadow]) {
      if (budget.snapshot().reason === null && hook !== null) {
        try { hook.emit() } catch { /* Its check can discover a whole-pass deadline. */ }
      }
    }
    return { ok: false, mode: MODE, reason: budget.snapshot().reason ?? 'cycle-failed',
      requests: budget.snapshot().requests, events, sweeps, ...observation() }
  } finally { budget.close() }
}
