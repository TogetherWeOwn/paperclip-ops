// Persisted QA-smoke intent state (design §7 row 2, slice S9a).
//
// One intent per `qa-smoke:{repo}:{sha}`, beside `receipts.json`,
// `claims.json`, `pending.json` and `redmain.json` in the SAME state
// directory, with its own namespace digest. Entries remember the staging
// environment, the routed QA owner, and the exact fixed-words intent text S10
// promotion will post once the issue-creation verb exists. A re-deploy of the
// same SHA adds nothing; a new SHA records fresh. Recorded entries are kept
// as the audit trail and pruned oldest-first only when the entry budget is
// exceeded.
//
// Same hardening as the sibling stores: absolute directory, 0600 files owned
// by this user, byte and entry budgets, fail-closed errors, namespace-mismatch
// refusal, no lock of its own (the caller holds the receipt-store lock for the
// whole pass). Loss of the state directory re-records one intent per staging
// deploy, which is noisy but safe: intents create no task and no wake in this
// slice.
//
// The watcher (closeout-deploy.js) takes only the small async store
// interface below, so tests use the memory store and production uses the file
// store without the decision code knowing which.
import { constants } from 'node:fs'
import { open, rename, unlink } from 'node:fs/promises'
import { join, isAbsolute } from 'node:path'
import { randomUUID, createHash } from 'node:crypto'

export const QASMOKE_FILE = 'qasmoke.json'
// `.` and `..` are excluded so a key can never smuggle a dot segment.
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const QA_OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
const SAFE_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const QASMOKE_TAG = 'qa-smoke'
const HEX = /^[a-f0-9]{64}$/
const MS_LIMIT = 8.64e15

const hash = (text) => createHash('sha256').update(text).digest('hex')
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isMs = (value) => Number.isInteger(value) && value >= 0 && value <= MS_LIMIT

export function qasmokeKey(repository, sha) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'qa-smoke repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'qa-smoke sha is invalid')
  return `${QASMOKE_TAG}:${repository}:${sha}`
}

function checkEntry(entry) {
  requireValue(entry && typeof entry === 'object' && !Array.isArray(entry) &&
    Object.keys(entry).length >= 7 && Object.keys(entry).length <= 9, 'qa-smoke entry is invalid')
  requireValue(typeof entry.key === 'string' && entry.key === qasmokeKey(entry.repository, entry.sha),
    'qa-smoke entry key does not match its repository and sha')
  requireValue(typeof entry.repository === 'string' && REPOSITORY.test(entry.repository),
    'qa-smoke entry repository is invalid')
  requireValue(typeof entry.sha === 'string' && SHA.test(entry.sha),
    'qa-smoke entry sha is invalid')
  requireValue(typeof entry.environment === 'string' && entry.environment.trim().length > 0 &&
    entry.environment.length <= 255 && SAFE_NAME.test(entry.environment.trim()),
    'qa-smoke entry environment is invalid')
  requireValue(isMs(entry.firstSeenMs), 'qa-smoke entry first-seen time is invalid')
  requireValue(typeof entry.qaOwner === 'string' && QA_OWNER_ROLE.test(entry.qaOwner),
    'qa-smoke entry owner is invalid')
  requireValue(typeof entry.body === 'string' && entry.body.length > 0 && entry.body.length <= 4096,
    'qa-smoke entry body is invalid')
  requireValue(entry.status === 'recorded', 'qa-smoke entry status is unknown')
}

export function checkQasmokeState(state, maxEntries = 1000) {
  requireValue(state && typeof state === 'object' && !Array.isArray(state) &&
    Object.keys(state).length === 3, 'qa-smoke state envelope is invalid')
  requireValue(state.version === 1 && typeof state.namespace === 'string' &&
    HEX.test(state.namespace), 'qa-smoke state version or namespace is invalid')
  requireValue(Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000,
    'qa-smoke storage budget is invalid')
  requireValue(Array.isArray(state.intents) && state.intents.length <= maxEntries,
    'qa-smoke state exceeds its capacity')
  const seen = new Set()
  for (const entry of state.intents) {
    checkEntry(entry)
    // Exactly one intent per key. Recorded entries are never updated: a
    // repeat deploy of the same SHA is a counter, not a second entry.
    requireValue(!seen.has(entry.key), 'qa-smoke state holds two intents for one key')
    seen.add(entry.key)
  }
  return state
}

export function freshQasmokeState(namespace) {
  requireValue(typeof namespace === 'string' && HEX.test(namespace),
    'qa-smoke namespace must be a SHA-256 digest')
  return { version: 1, namespace, intents: [] }
}

/**
 * Derive this store's namespace from the receipt namespace of the same scope.
 * Same inputs (origins, company, repository scope, mode), different digest, so
 * a scope or mode change starts with no intents instead of inheriting another
 * scope's silence, and intent state can never be mistaken for receipts,
 * claims, pending debounce or red-main incidents.
 */
export function qasmokeNamespace(receiptNamespaceDigest) {
  requireValue(typeof receiptNamespaceDigest === 'string' && HEX.test(receiptNamespaceDigest),
    'qa-smoke namespace requires a receipt namespace digest')
  return hash(`qasmoke-v1:${receiptNamespaceDigest}`)
}

/**
 * The recorded intent for a (repo, sha), or null. Absence means "not yet
 * recorded", never "dropped": drops are logged by the watcher, not stored.
 */
export function findIntent(state, repository, sha, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'qa-smoke repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'qa-smoke sha is invalid')
  checkQasmokeState(state, maxEntries)
  return state.intents.find((e) => e.key === qasmokeKey(repository, sha)) ?? null
}

/**
 * Record a fresh intent. Throws when one already exists for the key: the
 * watcher must check first, and a repeat deploy of the same SHA adds
 * nothing. Prunes the oldest entries past the budget.
 */
export function applyRecord(state, { repository, sha, environment, firstSeenMs, qaOwner, body }, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'qa-smoke repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'qa-smoke sha is invalid')
  requireValue(typeof environment === 'string' && environment.trim().length > 0 &&
    environment.length <= 255 && SAFE_NAME.test(environment.trim()),
    'qa-smoke environment is invalid')
  requireValue(isMs(firstSeenMs), 'qa-smoke first-seen time is invalid')
  requireValue(typeof qaOwner === 'string' && QA_OWNER_ROLE.test(qaOwner), 'qa-smoke owner is invalid')
  requireValue(typeof body === 'string' && body.length > 0 && body.length <= 4096, 'qa-smoke body is invalid')
  checkQasmokeState(state, maxEntries)
  requireValue(findIntent(state, repository, sha, maxEntries) === null,
    'qa-smoke intent is already recorded for this key; a repeat adds nothing')
  state.intents.push({ key: qasmokeKey(repository, sha), repository, sha,
    environment: environment.trim(), firstSeenMs, qaOwner, body, status: 'recorded' })
  // Prune oldest first. Every entry is equal history (no open/recovered
  // split), so oldest-first is the only fair eviction.
  let over = state.intents.length - maxEntries
  if (over > 0) {
    const ordered = state.intents
      .map((entry, index) => ({ entry, index }))
      .sort((a, b) => a.entry.firstSeenMs - b.entry.firstSeenMs)
    const drop = new Set()
    for (const { index } of ordered) {
      if (over <= 0) break
      drop.add(index)
      over--
    }
    state.intents = state.intents.filter((_, index) => !drop.has(index))
  }
  return state.intents[state.intents.length - 1]
}

async function syncDirectory(directory) {
  const handle = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW)
  try { await handle.sync() } finally { await handle.close() }
}

export async function loadQasmokeState({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'qa-smoke directory must be absolute')
  requireValue(typeof namespace === 'string' && HEX.test(namespace), 'qa-smoke namespace must be a SHA-256 digest')
  requireValue(Number.isInteger(maxBytes) && maxBytes >= 1024 && maxBytes <= 16 * 1024 * 1024 &&
    Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000, 'qa-smoke storage budget is invalid')
  const path = join(directory, QASMOKE_FILE)
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('qa-smoke state could not be opened')
    return freshQasmokeState(namespace)
  }
  let text
  try {
    const info = await handle.stat()
    requireValue(info.isFile() && info.uid === process.getuid() && (info.mode & 0o077) === 0 &&
      info.size <= maxBytes, 'qa-smoke state is unsafe or exceeds its budget')
    const buffer = Buffer.alloc(maxBytes + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    requireValue(length <= maxBytes, 'qa-smoke state exceeds its byte budget')
    text = new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length))
  } catch {
    throw new Error('qa-smoke state could not be read; refusing to reset it')
  } finally { await handle.close() }
  let state
  try {
    state = JSON.parse(text)
  } catch {
    throw new Error('qa-smoke state is corrupt; refusing to reset it')
  }
  requireValue(state?.namespace === namespace,
    'qa-smoke state namespace does not match; refusing to mix origins, companies, scopes or modes')
  try {
    return checkQasmokeState(state, maxEntries)
  } catch {
    throw new Error('qa-smoke state shape is invalid; refusing to reset it')
  }
}

export async function saveQasmokeState({ directory, state, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'qa-smoke directory must be absolute')
  const text = JSON.stringify(checkQasmokeState(state, maxEntries)) + '\n'
  requireValue(Buffer.byteLength(text) <= maxBytes, 'qa-smoke state exceeds its byte budget')
  const path = join(directory, QASMOKE_FILE)
  const temp = join(directory, `.qasmoke-${randomUUID()}.tmp`)
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
    throw new Error('qa-smoke persistence failed; stop processing and replay on recovery')
  } finally {
    if (file) await file.close()
    await unlink(temp).catch((error) => { if (error.code !== 'ENOENT') throw error })
  }
}

/**
 * In-memory intent store for tests and for callers that persist elsewhere.
 * Same transition rules as the file store (one intent per key, repeat
 * throws), no I/O at all.
 */
export function createMemoryQasmokeStore(seed = null, maxEntries = 1000) {
  const state = seed ?? freshQasmokeState(hash('qasmoke-test'))
  checkQasmokeState(state, maxEntries)
  return {
    async findIntent(repository, sha) {
      const found = findIntent(state, repository, sha, maxEntries)
      return found === null ? null : { ...found }
    },
    async recordIntent({ key, repository, sha, environment, firstSeenMs, qaOwner, body }) {
      requireValue(key === qasmokeKey(repository, sha), 'qa-smoke intent key does not match its repository and sha')
      return { ...applyRecord(state, { repository, sha, environment, firstSeenMs, qaOwner, body }, maxEntries) }
    },
    state,
  }
}

/**
 * File-backed intent store. Loads once per construction site per pass is
 * the caller's choice; saves after every transition so a crash loses at most
 * the in-flight delivery. The caller must hold the receipt-store lock: like the
 * sibling stores this module takes no lock of its own.
 */
export function createFileQasmokeStore({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  const storage = { directory, namespace, maxBytes, maxEntries }
  return {
    async findIntent(repository, sha) {
      const state = await loadQasmokeState(storage)
      const found = findIntent(state, repository, sha, maxEntries)
      return found === null ? null : { ...found }
    },
    async recordIntent({ key, repository, sha, environment, firstSeenMs, qaOwner, body }) {
      requireValue(key === qasmokeKey(repository, sha), 'qa-smoke intent key does not match its repository and sha')
      const state = await loadQasmokeState(storage)
      const entry = applyRecord(state, { repository, sha, environment, firstSeenMs, qaOwner, body }, maxEntries)
      await saveQasmokeState({ directory, state, maxBytes, maxEntries })
      return { ...entry }
    },
  }
}
