// Persisted red-main incident state (design §5.6, slice S8).
//
// One open incident per repo, keyed `main-red:{repo}`, beside `receipts.json`,
// `claims.json` and `pending.json` in the SAME state directory, with its own
// namespace digest. Entries remember the first red main SHA, the routed CI
// owner, and the exact fixed-words incident/recovery texts S10 promotion will
// post once the issue-creation verb exists. A re-red after recovery appends a
// fresh entry under the same key; recovered entries are kept as the audit
// trail and pruned oldest-first only when the entry budget is exceeded. Open
// entries are never pruned: dropping one would re-open a duplicate incident.
//
// Same hardening as the sibling stores: absolute directory, 0600 files owned
// by this user, byte and entry budgets, fail-closed errors, namespace-mismatch
// refusal, no lock of its own (the caller holds the receipt-store lock for the
// whole pass). Loss of the state directory re-opens one incident per red repo,
// which is noisy but safe: it never suppresses a wake for a green repo, and
// suppression consults the live store every decision.
//
// The watcher (closeout-redmain.js) takes only the small async store
// interface below, so tests use the memory store and production uses the file
// store without the decision code knowing which.
import { constants } from 'node:fs'
import { open, rename, unlink } from 'node:fs/promises'
import { join, isAbsolute } from 'node:path'
import { randomUUID, createHash } from 'node:crypto'

export const REDMAIN_FILE = 'redmain.json'
// `.` and `..` are excluded so a key can never smuggle a dot segment.
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
const REDMAIN_TAG = 'main-red'
const HEX = /^[a-f0-9]{64}$/
const MS_LIMIT = 8.64e15

const hash = (text) => createHash('sha256').update(text).digest('hex')
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isMs = (value) => Number.isInteger(value) && value >= 0 && value <= MS_LIMIT

export function redmainKey(repository) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'red-main repository is invalid')
  return `${REDMAIN_TAG}:${repository}`
}

function checkEntry(entry) {
  requireValue(entry && typeof entry === 'object' && !Array.isArray(entry) &&
    Object.keys(entry).length >= 7 && Object.keys(entry).length <= 10, 'red-main entry is invalid')
  requireValue(typeof entry.key === 'string' && entry.key === redmainKey(entry.repository),
    'red-main entry key does not match its repository')
  requireValue(typeof entry.repository === 'string' && REPOSITORY.test(entry.repository),
    'red-main entry repository is invalid')
  requireValue(typeof entry.firstRedSha === 'string' && SHA.test(entry.firstRedSha),
    'red-main entry first-red sha is invalid')
  requireValue(isMs(entry.openedMs), 'red-main entry open time is invalid')
  requireValue(typeof entry.ciOwner === 'string' && OWNER_ROLE.test(entry.ciOwner),
    'red-main entry owner is invalid')
  requireValue(typeof entry.body === 'string' && entry.body.length > 0 && entry.body.length <= 4096,
    'red-main entry body is invalid')
  requireValue(entry.status === 'open' || entry.status === 'recovered', 'red-main entry status is unknown')
  if (entry.status === 'recovered') {
    requireValue(typeof entry.recoveredSha === 'string' && SHA.test(entry.recoveredSha),
      'red-main recovered entry has no recovery sha')
    requireValue(isMs(entry.recoveredMs) && entry.recoveredMs >= entry.openedMs,
      'red-main recovered entry has no valid recovery time')
    requireValue(typeof entry.recoveryBody === 'string' && entry.recoveryBody.length > 0 &&
      entry.recoveryBody.length <= 4096, 'red-main recovered entry has no recovery text')
  } else {
    requireValue(entry.recoveredSha === undefined && entry.recoveredMs === undefined &&
      entry.recoveryBody === undefined, 'red-main open entry carries recovery fields')
  }
}

export function checkRedmainState(state, maxEntries = 1000) {
  requireValue(state && typeof state === 'object' && !Array.isArray(state) &&
    Object.keys(state).length === 3, 'red-main state envelope is invalid')
  requireValue(state.version === 1 && typeof state.namespace === 'string' &&
    HEX.test(state.namespace), 'red-main state version or namespace is invalid')
  requireValue(Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000,
    'red-main storage budget is invalid')
  requireValue(Array.isArray(state.incidents) && state.incidents.length <= maxEntries,
    'red-main state exceeds its capacity')
  const open = new Set()
  for (const entry of state.incidents) {
    checkEntry(entry)
    // Exactly one OPEN incident per repo. Recovered entries may repeat the
    // key: they are the audit trail of earlier reds under the same identity.
    if (entry.status === 'open') {
      requireValue(!open.has(entry.key), 'red-main state holds two open incidents for one repo')
      open.add(entry.key)
    }
  }
  return state
}

export function freshRedmainState(namespace) {
  requireValue(typeof namespace === 'string' && HEX.test(namespace),
    'red-main namespace must be a SHA-256 digest')
  return { version: 1, namespace, incidents: [] }
}

/**
 * Derive this store's namespace from the receipt namespace of the same scope.
 * Same inputs (origins, company, repository scope, mode), different digest, so
 * a scope or mode change starts with no open incidents instead of inheriting
 * another scope's suppression, and incident state can never be mistaken for
 * receipts, claims or pending debounce.
 */
export function redmainNamespace(receiptNamespaceDigest) {
  requireValue(typeof receiptNamespaceDigest === 'string' && HEX.test(receiptNamespaceDigest),
    'red-main namespace requires a receipt namespace digest')
  return hash(`redmain-v1:${receiptNamespaceDigest}`)
}

/**
 * The open incident for a repo, or null. A recovered entry is history, not an
 * incident: only `open` suppresses wakes.
 */
export function findOpenIncident(state, repository, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'red-main repository is invalid')
  // The caller's configured budget, not the default: with maxEntries above
  // the default, validating against the default would throw on legitimate
  // history and wedge every later open and recovery.
  checkRedmainState(state, maxEntries)
  return state.incidents.find((e) => e.key === redmainKey(repository) && e.status === 'open') ?? null
}

/**
 * Open a fresh incident. Throws when one is already open for the repo: the
 * watcher must check first, and a second red SHA while one is open adds
 * nothing. Re-red after recovery appends under the same key. Prunes the
 * oldest recovered entries past the budget; open entries are never pruned.
 */
export function applyOpen(state, { repository, firstRedSha, openedMs, ciOwner, body }, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'red-main repository is invalid')
  requireValue(typeof firstRedSha === 'string' && SHA.test(firstRedSha), 'red-main first-red sha is invalid')
  requireValue(isMs(openedMs), 'red-main open time is invalid')
  requireValue(typeof ciOwner === 'string' && OWNER_ROLE.test(ciOwner), 'red-main owner is invalid')
  requireValue(typeof body === 'string' && body.length > 0 && body.length <= 4096, 'red-main body is invalid')
  checkRedmainState(state, maxEntries)
  requireValue(findOpenIncident(state, repository, maxEntries) === null,
    'red-main incident is already open for this repo; a second red SHA adds nothing')
  state.incidents.push({ key: redmainKey(repository), repository, firstRedSha, openedMs, ciOwner, body,
    status: 'open' })
  // Prune oldest recovered entries first. Open entries are exempt: evicting
  // one would re-open a duplicate incident on the next red read.
  const recovered = state.incidents
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry }) => entry.status === 'recovered')
    .sort((a, b) => a.entry.recoveredMs - b.entry.recoveredMs)
  let over = state.incidents.length - maxEntries
  const drop = new Set()
  for (const { entry, index } of recovered) {
    if (over <= 0) break
    drop.add(index)
    over--
  }
  requireValue(over <= 0, 'red-main entry budget exceeded by open incidents; refusing to evict an open incident')
  state.incidents = state.incidents.filter((_, index) => !drop.has(index))
  return state.incidents[state.incidents.length - 1]
}

/**
 * Recover the open incident with one recovery text. The entry stays as audit
 * history; only `open` suppresses. Throws when none is open: green with no
 * incident is a no-op, not a recovery.
 */
export function applyRecovery(state, repository, { recoveredSha, recoveredMs, recoveryBody }, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'red-main repository is invalid')
  requireValue(typeof recoveredSha === 'string' && SHA.test(recoveredSha), 'red-main recovery sha is invalid')
  requireValue(isMs(recoveredMs), 'red-main recovery time is invalid')
  requireValue(typeof recoveryBody === 'string' && recoveryBody.length > 0 && recoveryBody.length <= 4096,
    'red-main recovery text is invalid')
  checkRedmainState(state, maxEntries)
  const entry = findOpenIncident(state, repository, maxEntries)
  requireValue(entry !== null, 'red-main incident is not open for this repo; green with no incident recovers nothing')
  requireValue(recoveredMs >= entry.openedMs, 'red-main recovery time precedes the incident')
  entry.status = 'recovered'
  entry.recoveredSha = recoveredSha
  entry.recoveredMs = recoveredMs
  entry.recoveryBody = recoveryBody
  return entry
}

async function syncDirectory(directory) {
  const handle = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW)
  try { await handle.sync() } finally { await handle.close() }
}

export async function loadRedmainState({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'red-main directory must be absolute')
  requireValue(typeof namespace === 'string' && HEX.test(namespace), 'red-main namespace must be a SHA-256 digest')
  requireValue(Number.isInteger(maxBytes) && maxBytes >= 1024 && maxBytes <= 16 * 1024 * 1024 &&
    Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000, 'red-main storage budget is invalid')
  const path = join(directory, REDMAIN_FILE)
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('red-main state could not be opened')
    return freshRedmainState(namespace)
  }
  let text
  try {
    const info = await handle.stat()
    requireValue(info.isFile() && info.uid === process.getuid() && (info.mode & 0o077) === 0 &&
      info.size <= maxBytes, 'red-main state is unsafe or exceeds its budget')
    const buffer = Buffer.alloc(maxBytes + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    requireValue(length <= maxBytes, 'red-main state exceeds its byte budget')
    text = new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length))
  } catch {
    throw new Error('red-main state could not be read; refusing to reset it')
  } finally { await handle.close() }
  let state
  try {
    state = JSON.parse(text)
  } catch {
    throw new Error('red-main state is corrupt; refusing to reset it')
  }
  requireValue(state?.namespace === namespace,
    'red-main state namespace does not match; refusing to mix origins, companies, scopes or modes')
  try {
    return checkRedmainState(state, maxEntries)
  } catch {
    throw new Error('red-main state shape is invalid; refusing to reset it')
  }
}

export async function saveRedmainState({ directory, state, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'red-main directory must be absolute')
  const text = JSON.stringify(checkRedmainState(state, maxEntries)) + '\n'
  requireValue(Buffer.byteLength(text) <= maxBytes, 'red-main state exceeds its byte budget')
  const path = join(directory, REDMAIN_FILE)
  const temp = join(directory, `.redmain-${randomUUID()}.tmp`)
  let file
  try {
    file = await open(temp, 'wx', 0o600)
    await file.writeFile(text)
    await file.sync()
    await file.close()
    file = null
    await rename(temp, path)
    await syncDirectory(directory)
  } catch {
    throw new Error('red-main persistence failed; stop processing and replay on recovery')
  } finally {
    if (file) await file.close()
    await unlink(temp).catch((error) => { if (error.code !== 'ENOENT') throw error })
  }
}

/**
 * In-memory incident store for tests and for callers that persist elsewhere.
 * Same transition rules as the file store (single open per repo, recovery
 * needs an open incident), no I/O at all.
 */
export function createMemoryRedmainStore(seed = null, maxEntries = 1000) {
  const state = seed ?? freshRedmainState(hash('redmain-test'))
  checkRedmainState(state, maxEntries)
  return {
    async findOpen(repository) {
      const found = findOpenIncident(state, repository, maxEntries)
      return found === null ? null : { ...found }
    },
    async openIncident({ key, repository, firstRedSha, openedMs, ciOwner, body }) {
      requireValue(key === redmainKey(repository), 'red-main incident key does not match its repository')
      return { ...applyOpen(state, { repository, firstRedSha, openedMs, ciOwner, body }, maxEntries) }
    },
    async closeIncident(key, { recoveredSha, recoveredMs, recoveryBody }) {
      const open = state.incidents.find((e) => e.key === key && e.status === 'open') ?? null
      requireValue(open !== null, 'red-main incident is not open; refusing to close what was never opened')
      return { ...applyRecovery(state, open.repository, { recoveredSha, recoveredMs, recoveryBody }, maxEntries) }
    },
    state,
  }
}

/**
 * File-backed incident store. Loads once per construction site per pass is
 * the caller's choice; saves after every transition so a crash loses at most
 * the in-flight repo. The caller must hold the receipt-store lock: like the
 * sibling stores this module takes no lock of its own.
 */
export function createFileRedmainStore({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  const storage = { directory, namespace, maxBytes, maxEntries }
  return {
    async findOpen(repository) {
      const state = await loadRedmainState(storage)
      const found = findOpenIncident(state, repository, maxEntries)
      return found === null ? null : { ...found }
    },
    async openIncident({ key, repository, firstRedSha, openedMs, ciOwner, body }) {
      requireValue(key === redmainKey(repository), 'red-main incident key does not match its repository')
      const state = await loadRedmainState(storage)
      const entry = applyOpen(state, { repository, firstRedSha, openedMs, ciOwner, body }, maxEntries)
      await saveRedmainState({ directory, state, maxBytes, maxEntries })
      return { ...entry }
    },
    async closeIncident(key, { recoveredSha, recoveredMs, recoveryBody }) {
      const state = await loadRedmainState(storage)
      const open = state.incidents.find((e) => e.key === key && e.status === 'open') ?? null
      requireValue(open !== null, 'red-main incident is not open; refusing to close what was never opened')
      const entry = applyRecovery(state, open.repository, { recoveredSha, recoveredMs, recoveryBody }, maxEntries)
      await saveRedmainState({ directory, state, maxBytes, maxEntries })
      return { ...entry }
    },
  }
}
