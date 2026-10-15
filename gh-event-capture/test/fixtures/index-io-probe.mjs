// Fault only the index filesystem. All state/streams/transports are in memory.
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../src/', import.meta.url))
const phase = process.argv[2]
assert.ok(['save', 'load'].includes(phase))
const namespace = 'b'.repeat(64)
const issueId = '00000000-0000-4000-8000-000000000014'
const companyId = '00000000-0000-4000-8000-000000000015'
const repository = 'Example/repo'
const issue = { id: issueId, companyId, identifier: 'case_8c5fe4cc', status: 'in_progress' }
const cacheBytes = Buffer.from(JSON.stringify({ version: 1, namespace, builtAt: 0, entries: [{ issueId,
  identifier: 'case_8c5fe4cc', status: 'in_progress', readAt: 0, keys: [`${repository}#1`] }], retryAfter: {} }))
let now = 0
let locked = false
let fsSettled = false
let passSettled = false
let markIoStarted
let releaseIo
const ioStarted = new Promise((resolve) => { markIoStarted = resolve })
const ioReleased = new Promise((resolve) => { releaseIo = resolve })
const streams = []
const timers = []
const clock = { now: () => now, setTimer: (fire, ms) => {
  const timer = { ms, active: true, fire: () => { if (timer.active) { timer.active = false; fire() } } }
  timers.push(timer)
  return timer
}, clearTimer: (timer) => { timer.active = false } }
async function stallFilesystem() {
  assert.equal(locked, true)
  markIoStarted()
  await ioReleased
  assert.equal(locked, true, 'index I/O must drain before unlock')
  fsSettled = true
}
const memoryFs = {
  open: async (path, flags) => {
    if (path.endsWith('/pr-task-index.json')) {
      if (phase !== 'load') throw Object.assign(new Error('synthetic absent cache'), { code: 'ENOENT' })
      let position = 0
      return { stat: async () => { await stallFilesystem(); return { isFile: () => true, uid: process.getuid(),
        mode: 0o100600, size: cacheBytes.length } },
      read: async (buffer, offset, length) => {
        const size = Math.min(length, cacheBytes.length - position)
        cacheBytes.copy(buffer, offset, position, position + size)
        position += size
        return { bytesRead: size }
      }, close: async () => {} }
    }
    if (flags === 'wx') return { writeFile: async () => {}, sync: stallFilesystem, close: async () => {} }
    return { sync: async () => {}, close: async () => {} }
  }, rename: async () => { assert.equal(locked, true) }, unlink: async () => {},
}
const overrides = new Map([
  [resolve(root, 'receipt-store.js'), { withReceiptStore: async (_opts, callback) => {
    locked = true
    try { return await callback({ record: async () => {} }) }
    finally { assert.equal(fsSettled, true, 'cache I/O must not survive the lock'); locked = false }
  } }],
  [resolve(root, 'pass-schedule.js'), {
    loadPassSchedule: async () => ({ nextStream: 'events', eventCursor: null, sweepCursor: null }),
    savePassSchedule: async () => {},
  }],
  [resolve(root, 'sweep-store.js'), { loadSweepState: async () => ({}), saveSweepState: async () => {} }],
  [resolve(root, 'receipt-cycle.js'), { receiptNamespace: () => namespace, runReceiptCycle: async () => {
    assert.equal(fsSettled, false, 'production must start before the stalled index settles')
    streams.push('events')
    return { ok: true }
  } }],
  [resolve(root, 'sweep-cycle.js'), { sweepLimits: (value) => value, runSweepCycle: async (options) => {
    assert.equal(fsSettled, false)
    assert.equal(options.closeoutReview, null, 'a late index must not enter this shadow')
    streams.push('sweeps')
    return { ok: true }
  } }],
])
const context = vm.createContext({ Buffer, AbortController, AbortSignal, URL, URLSearchParams, TextEncoder, TextDecoder,
  Headers, Request, Response, setTimeout, clearTimeout, process: { getuid: () => process.getuid() } })
const modules = new Map()
function synthetic(identifier, values) {
  return new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key, value] of Object.entries(values)) this.setExport(key, value)
  }, { identifier, context })
}
async function moduleFor(specifier, referencingModule) {
  const identifier = specifier.startsWith('node:') ? specifier : resolve(dirname(referencingModule.identifier), specifier)
  const indexFs = identifier === 'node:fs/promises' && referencingModule.identifier === resolve(root, 'pr-task-index-source.js')
  const cacheId = indexFs ? 'synthetic:index-fs-only' : identifier
  if (modules.has(cacheId)) return modules.get(cacheId)
  const module = (async () => {
    if (indexFs) return synthetic(cacheId, memoryFs)
    if (identifier.startsWith('node:')) return synthetic(identifier, await import(identifier))
    if (overrides.has(identifier)) return synthetic(identifier, overrides.get(identifier))
    return new vm.SourceTextModule(readFileSync(identifier, 'utf8'), { identifier, context })
  })()
  modules.set(cacheId, module)
  return module
}
const runnerPath = resolve(root, 'consumer-runner.js')
const runner = new vm.SourceTextModule(readFileSync(runnerPath, 'utf8'), { identifier: runnerPath, context })
modules.set(runnerPath, Promise.resolve(runner))
await runner.link(moduleFor)
await runner.evaluate()
const config = { mode: 'products-only-v1', captureOrigin: 'https://capture.test', boardOrigin: 'https://board.test',
  companyId, allowedRepositories: [repository], stateDirectory: '/synthetic-state-never-written', limits: {},
  closeout: { shadow: { enabledRepositories: [repository], indexReadsPerPass: 1 } } }
const fetchImpl = async (url, init) => {
  assert.equal(init.method, 'GET')
  assert.equal(init.signal.aborted, false)
  const rows = url.includes('/work-products') || new URL(url).searchParams.has('afterId') ? [] : [issue]
  return new Response(JSON.stringify(rows), { headers: { 'content-type': 'application/json' } })
}
const pending = runner.namespace.runProductPass({ config, captureToken: 'SYNTHETIC', boardToken: 'SYNTHETIC' },
  { fetchImpl, clock, run: async () => { assert.fail('no external subprocesses permitted') } })
pending.then(() => { passSettled = true })
try {
  await ioStarted
  // Advance chronologically: the index deadline fires while cache I/O remains
  // unresolved, before the whole-pass deadline, just as independent timers do.
  now = 11251
  const indexTimer = timers.find((timer) => timer.ms === 11250)
  assert.ok(indexTimer)
  indexTimer.fire()
  for (let step = 0; step < 10; step++) await new Promise(setImmediate)
  assert.deepEqual(streams, ['events', 'sweeps'], 'index I/O must yield production at its own deadline')
  assert.equal(locked, true)
  assert.equal(passSettled, false, 'the pass must still await index drainage')
  assert.equal(fsSettled, false)
} finally {
  now = 45001
  for (const timer of timers) if (timer.active && now >= timer.ms) timer.fire()
  releaseIo()
  await pending
}
const result = await pending
assert.equal(result.ok, false, 'a whole-pass deadline must not become a false green')
assert.equal(result.reason, 'deadline')
assert.equal(result.events.ok, true)
assert.equal(result.sweeps.ok, true)
assert.equal(locked, false)
assert.equal(fsSettled, true)
console.log(`${phase}: production ran before drainage; whole-pass stop preserved; lock released after settlement`)
