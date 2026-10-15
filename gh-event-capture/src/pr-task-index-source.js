// Board-backed index for shadow review only. The caller holds the receipt lock;
// all network I/O uses its budget-wrapped board adapter. Never writes to the board.
import { constants } from 'node:fs'
import { open, rename, unlink } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { isAbsolute, join } from 'node:path'
import { buildPrTaskIndex } from './pr-task-index.js'
import { OPEN_ISSUE_LIST_MAX_ROWS as MAX_ISSUES } from './paperclip-adapter.js'

export const INDEX_FILE = 'pr-task-index.json'
const MAX_BYTES = 16 * 1024 * 1024
const RETRY_MS = 60000
// Match the runner's maximum pass duration; larger future times must not pin retries.
const MAX_FUTURE_SKEW_MS = 300000
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/
const REF = /^TOG-[1-9]\d*$/
const OPEN = new Set(['todo', 'in_progress', 'in_review', 'blocked'])
const REPO = '[A-Za-z0-9][A-Za-z0-9-]*/(?!\\.{1,2}(?:#|/))[A-Za-z0-9_.-]+'
const KEY = new RegExp(`^(${REPO})#([1-9]\\d*)$`)
const URL = new RegExp(`^https://github\\.com/(${REPO})/pull/([1-9]\\d*)$`)
const plain = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const ms = (v) => Number.isSafeInteger(v) && v >= 0
const fields = (v, keys) => plain(v) && Object.keys(v).length === keys.length && keys.every((k) => Object.hasOwn(v, k))
function requireValue(condition) {
  if (!condition) throw new Error('PR task index source is invalid')
}
function keyFrom(value, pattern) {
  const match = typeof value === 'string' && pattern.exec(value)
  return match && Number.isSafeInteger(Number(match[2])) ? `${match[1]}#${match[2]}` : null
}
// Validated identities/keys are ASCII and need no JSON escapes. Count keys
// without serializing them; reserve maximum timestamp/status and retry overhead.
function keyBytes(keys) {
  return keys.length === 0 ? 0 : keys.reduce((bytes, key) => bytes + key.length + 3, -1)
}
function boundedStateBytes(state) {
  const envelope = JSON.stringify({ ...state, builtAt: Number.MAX_SAFE_INTEGER, entries: [], retryAfter: {} })
  return Buffer.byteLength(envelope) + state.entries.reduce((bytes, entry) => bytes +
    Buffer.byteLength(JSON.stringify({ ...entry, status: 'in_progress', readAt: Number.MAX_SAFE_INTEGER, keys: [] })) +
    keyBytes(entry.keys) + 57, 0)
}
function checkState(state, namespace) {
  requireValue(fields(state, ['version', 'namespace', 'builtAt', 'entries', 'retryAfter']) && state.version === 1 &&
    state.namespace === namespace && ms(state.builtAt) && Array.isArray(state.entries) && state.entries.length <= MAX_ISSUES &&
    plain(state.retryAfter))
  const ids = new Set()
  const refs = new Set()
  for (const e of state.entries) {
    requireValue(fields(e, ['issueId', 'identifier', 'status', 'readAt', 'keys']) &&
      typeof e.issueId === 'string' && UUID.test(e.issueId) && typeof e.identifier === 'string' &&
      REF.test(e.identifier) && OPEN.has(e.status) && !ids.has(e.issueId) && !refs.has(e.identifier) &&
      (e.readAt === null || (ms(e.readAt) && e.readAt <= state.builtAt)) && Array.isArray(e.keys) &&
      e.keys.length <= 10000 && (e.readAt !== null || e.keys.length === 0) &&
      e.keys.every((key) => keyFrom(key, KEY) === key) && new Set(e.keys).size === e.keys.length)
    ids.add(e.issueId); refs.add(e.identifier)
  }
  requireValue(Object.entries(state.retryAfter).every(([id, at]) => ids.has(id) && ms(at) && at <= state.builtAt + RETRY_MS))
  requireValue(boundedStateBytes(state) <= MAX_BYTES)
  return state
}

// Any unreadable, unsafe, corrupt or foreign-scope file means a complete re-warm,
// never trust the valid-looking subset of an invalid file.
export async function loadPrTaskIndex({ directory, namespace }) {
  let file
  try {
    file = await open(join(directory, INDEX_FILE), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const info = await file.stat()
    requireValue(info.isFile() && info.uid === process.getuid() && (info.mode & 0o077) === 0 && info.size <= MAX_BYTES)
    const buffer = Buffer.alloc(info.size + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await file.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    requireValue(length <= info.size)
    return checkState(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length))), namespace)
  } catch { return null } finally { if (file) await file.close().catch(() => {}) }
}

async function saveIndex(directory, state) {
  const text = JSON.stringify(checkState(state, state.namespace)) + '\n'
  requireValue(Buffer.byteLength(text) <= MAX_BYTES)
  const temp = join(directory, `.pr-task-index-${randomUUID()}.tmp`)
  let file
  try {
    file = await open(temp, 'wx', 0o600)
    await file.writeFile(text)
    await file.sync()
    await file.close()
    file = null
    await rename(temp, join(directory, INDEX_FILE))
    const parent = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW)
    try { await parent.sync() } finally { await parent.close() }
  } finally {
    if (file) await file.close()
    await unlink(temp).catch((e) => { if (e.code !== 'ENOENT') throw e })
  }
}

function deriveKeys(products, allowed, counters, maxKeyBytes) {
  const keys = new Set()
  let bytes = -1
  for (const row of products) {
    if (row.type !== 'pull_request' || row.provider !== 'github') continue
    const external = keyFrom(row.externalId, KEY)
    const url = keyFrom(row.url, URL)
    const externalIdentity = external ?? keyFrom(row.externalId, URL)
    // Recognizable noncanonical external URLs can conflict, but never supply a key.
    if (externalIdentity && row.url !== null && row.url !== `https://github.com/${externalIdentity.replace('#', '/pull/')}`) {
      counters.conflict++
      continue
    }
    const key = external ?? url
    if (!key || !allowed.has(key.slice(0, key.lastIndexOf('#')))) { counters.noKey++; continue }
    if (!keys.has(key)) {
      bytes += key.length + 3
      requireValue(bytes <= maxKeyBytes)
      keys.add(key)
    }
    if (!external) counters.viaUrl++
  }
  return [...keys].sort()
}

/** One full open-set list plus a bounded incremental refresh. Failed reads retain
 * that issue's last good entry; a failed list retains the complete previous set.
 * A partially warmed set is never handed to the shadow. Counts contain no text. */
export async function refreshPrTaskIndex({ board, directory, namespace, allowedRepositories,
  indexReadsPerPass = 60, nowMs, canRead = () => true, log = () => {} }) {
  const wallStartedAt = nowMs === undefined ? Date.now() : nowMs
  requireValue(board && typeof board.listOpenIssues === 'function' && typeof board.listWorkProducts === 'function' &&
    typeof directory === 'string' && isAbsolute(directory) && /^[a-f0-9]{64}$/.test(namespace) &&
    Array.isArray(allowedRepositories) && Number.isInteger(indexReadsPerPass) && indexReadsPerPass >= 1 &&
    indexReadsPerPass <= 150 && ms(wallStartedAt) && typeof canRead === 'function' && typeof log === 'function')
  const allowed = new Set(allowedRepositories)
  let state = await loadPrTaskIndex({ directory, namespace })
  const futureSkew = (state?.builtAt ?? wallStartedAt) - wallStartedAt
  if (state && futureSkew > MAX_FUTURE_SKEW_MS) state = null
  // A validated cache may bridge a bounded rollback, but never pin time arbitrarily forward.
  const startedAt = nowMs === undefined && state && futureSkew > 0 ? state.builtAt : wallStartedAt
  if (state && state.builtAt > startedAt) state = null
  const counters = { refreshReads: 0, indexRefreshErrors: 0, viaUrl: 0, noKey: 0, conflict: 0 }
  let lastTime = startedAt
  const currentTime = () => {
    lastTime = Math.max(lastTime, nowMs === undefined ? Date.now() : startedAt)
    return lastTime
  }
  let openIssues
  try { openIssues = await board.listOpenIssues() } catch { counters.indexRefreshErrors++ }
  if (openIssues) {
    try {
      const previous = new Map((state?.entries ?? []).map((e) => [e.issueId, e]))
      const entries = openIssues.map((issue) => {
        const cached = previous.get(issue.id)
        return { issueId: issue.id, identifier: issue.identifier, status: issue.status,
          readAt: cached?.identifier === issue.identifier ? cached.readAt : null,
          keys: cached?.identifier === issue.identifier ? cached.keys : [] }
      })
      // Keep expired retries as attempt history until success. A time-only
      // cooldown cannot rotate failures when passes are further apart than it.
      const retryAfter = Object.fromEntries(entries.filter((e) => Object.hasOwn(state?.retryAfter ?? {}, e.issueId))
        .map((e) => [e.issueId, state.retryAfter[e.issueId]]))
      let stateBytes = boundedStateBytes({ version: 1, namespace, builtAt: startedAt, entries, retryAfter })
      requireValue(stateBytes <= MAX_BYTES)
      const attemptedAt = (e) => Math.max(e.readAt ?? -1, (retryAfter[e.issueId] ?? 0) - RETRY_MS)
      const due = entries.filter((e) => (retryAfter[e.issueId] ?? 0) <= startedAt)
        .sort((a, b) => Number(a.readAt !== null) - Number(b.readAt !== null) ||
          attemptedAt(a) - attemptedAt(b) || a.issueId.localeCompare(b.issueId))
      for (const e of due.slice(0, indexReadsPerPass)) {
        if (!canRead()) break
        counters.refreshReads++
        try {
          const products = await board.listWorkProducts(e.issueId)
          const oldBytes = keyBytes(e.keys)
          const keys = deriveKeys(products, allowed, counters, MAX_BYTES - stateBytes + oldBytes)
          stateBytes += keyBytes(keys) - oldBytes
          e.keys = keys
          e.readAt = currentTime()
          delete retryAfter[e.issueId]
        } catch { counters.indexRefreshErrors++; retryAfter[e.issueId] = currentTime() + RETRY_MS }
      }
      const next = { version: 1, namespace, builtAt: currentTime(), entries, retryAfter }
      // In this invocation, a failed persist must not resurrect a known-closed
      // issue. A later failed list can only fall back to the last persisted set.
      state = next
      try { await saveIndex(directory, next) } catch { counters.indexRefreshErrors++ }
    } catch { state = null; counters.indexRefreshErrors++ }
  }
  const entries = state?.entries ?? []
  const readCount = entries.filter((e) => e.readAt !== null).length
  const warm = state !== null && readCount === entries.length
  // Re-filter cached keys against the current scope as an additional backstop.
  const index = warm ? buildPrTaskIndex(entries.map((e) => ({
    issue: { id: e.issueId, identifier: e.identifier, status: e.status },
    products: e.keys.filter((key) => allowed.has(key.slice(0, key.lastIndexOf('#'))))
      .map((externalId) => ({ type: 'pull_request', provider: 'github', externalId })),
  }))) : null
  const observedAt = currentTime()
  const ages = entries.filter((e) => e.readAt !== null).map((e) => Math.max(0, observedAt - e.readAt))
  const summary = { event: 'closeout-index', state: warm ? 'index-warm' : state ? 'index-warming' : 'index-unavailable',
    openCount: entries.length, readCount, warm, ...counters, keyed: index?.claimed.size ?? 0,
    ambiguous: index?.ambiguous.size ?? 0, indexMaxAgeMs: Math.max(0, ...ages),
    olderThan6h: ages.filter((age) => age > 6 * 60 * 60 * 1000).length }
  try { log(JSON.stringify(summary)) } catch { /* Observational, including the sink. */ }
  return { index, summary }
}
