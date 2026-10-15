// Persisted deploy-failure intent state (design §7 row 3, slice S9b).
//
// One intent per `deploy-fail:{repo}:{sha}`, beside `receipts.json`,
// `claims.json`, `pending.json`, `redmain.json` and `qasmoke.json` in the SAME
// state directory, with its own namespace digest. Entries remember the
// environment (staging AND production both count — environment never filters
// failures), the log URL pointer when the delivery carries one, the routed
// deploy owner, and the exact fixed-words intent text a later promotion slice
// will post once the issue-creation verb exists. A repeat delivery for the
// same SHA adds nothing; a new SHA records fresh. Recorded entries are kept
// as the audit trail and pruned oldest-first only when the entry budget is
// exceeded.
//
// Same hardening as the sibling stores: absolute directory, 0600 files owned
// by this user, byte and entry budgets, fail-closed errors, namespace-mismatch
// refusal, no lock of its own (the caller holds the receipt-store lock for the
// whole pass). Loss of the state directory re-records one intent per failed
// deploy, which is noisy but safe: intents create no task and no wake in this
// slice.
//
// The watcher (closeout-deploy.js `createDeployFailWatcher`) takes only the
// small async store interface below, so tests use the memory store and
// production uses the file store without the decision code knowing which.
import { constants } from 'node:fs'
import { open, rename, unlink } from 'node:fs/promises'
import { join, isAbsolute } from 'node:path'
import { randomUUID, createHash } from 'node:crypto'

export const DEPLOYFAIL_FILE = 'deployfail.json'
// `.` and `..` are excluded so a key can never smuggle a dot segment.
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const DEPLOY_OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
const SAFE_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const DEPLOYFAIL_TAG = 'deploy-fail'
const HEX = /^[a-f0-9]{64}$/
const MS_LIMIT = 8.64e15
const MAX_LOG_URL = 2048

const hash = (text) => createHash('sha256').update(text).digest('hex')
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isMs = (value) => Number.isInteger(value) && value >= 0 && value <= MS_LIMIT

/**
 * A log URL pointer is either absent (null) or a single https URL with no
 * credentials and no whitespace. Anything else is rejected: a `javascript:`
 * or `data:` URL must never reach intent text, and a userinfo (`user@host`)
 * pointer could carry a secret into the store.
 */
export function checkLogUrl(value) {
  if (value === null) return null
  requireValue(typeof value === 'string' && value.length > 0 && value.length <= MAX_LOG_URL,
    'deploy-fail log URL is invalid')
  requireValue(!/\s/.test(value), 'deploy-fail log URL is invalid')
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new Error('deploy-fail log URL is invalid')
  }
  requireValue(parsed.protocol === 'https:' && parsed.hostname.length > 0,
    'deploy-fail log URL is invalid')
  requireValue(parsed.username === '' && parsed.password === '',
    'deploy-fail log URL is invalid')
  return value
}

export function deployFailKey(repository, sha) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'deploy-fail repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'deploy-fail sha is invalid')
  return `${DEPLOYFAIL_TAG}:${repository}:${sha}`
}

function checkEntry(entry) {
  requireValue(entry && typeof entry === 'object' && !Array.isArray(entry) &&
    Object.keys(entry).length >= 8 && Object.keys(entry).length <= 10, 'deploy-fail entry is invalid')
  requireValue(typeof entry.key === 'string' && entry.key === deployFailKey(entry.repository, entry.sha),
    'deploy-fail entry key does not match its repository and sha')
  requireValue(typeof entry.repository === 'string' && REPOSITORY.test(entry.repository),
    'deploy-fail entry repository is invalid')
  requireValue(typeof entry.sha === 'string' && SHA.test(entry.sha),
    'deploy-fail entry sha is invalid')
  requireValue(typeof entry.environment === 'string' && entry.environment.trim().length > 0 &&
    entry.environment.length <= 255 && SAFE_NAME.test(entry.environment.trim()),
    'deploy-fail entry environment is invalid')
  requireValue('logUrl' in entry && (entry.logUrl === null || typeof entry.logUrl === 'string'),
    'deploy-fail entry log URL is invalid')
  checkLogUrl(entry.logUrl)
  requireValue(isMs(entry.firstSeenMs), 'deploy-fail entry first-seen time is invalid')
  requireValue(typeof entry.deployOwner === 'string' && DEPLOY_OWNER_ROLE.test(entry.deployOwner),
    'deploy-fail entry owner is invalid')
  requireValue(typeof entry.body === 'string' && entry.body.length > 0 && entry.body.length <= 4096,
    'deploy-fail entry body is invalid')
  requireValue(entry.status === 'recorded', 'deploy-fail entry status is unknown')
}

export function checkDeployfailState(state, maxEntries = 1000) {
  requireValue(state && typeof state === 'object' && !Array.isArray(state) &&
    Object.keys(state).length === 3, 'deploy-fail state envelope is invalid')
  requireValue(state.version === 1 && typeof state.namespace === 'string' &&
    HEX.test(state.namespace), 'deploy-fail state version or namespace is invalid')
  requireValue(Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000,
    'deploy-fail storage budget is invalid')
  requireValue(Array.isArray(state.intents) && state.intents.length <= maxEntries,
    'deploy-fail state exceeds its capacity')
  const seen = new Set()
  for (const entry of state.intents) {
    checkEntry(entry)
    // Exactly one intent per key. Recorded entries are never updated: a
    // repeat delivery for the same SHA is a counter, not a second entry.
    requireValue(!seen.has(entry.key), 'deploy-fail state holds two intents for one key')
    seen.add(entry.key)
  }
  return state
}

export function freshDeployfailState(namespace) {
  requireValue(typeof namespace === 'string' && HEX.test(namespace),
    'deploy-fail namespace must be a SHA-256 digest')
  return { version: 1, namespace, intents: [] }
}

/**
 * Derive this store's namespace from the receipt namespace of the same scope.
 * Same inputs (origins, company, repository scope, mode), different digest, so
 * a scope or mode change starts with no intents instead of inheriting another
 * scope's silence, and intent state can never be mistaken for receipts,
 * claims, pending debounce, red-main incidents or QA-smoke intents.
 */
export function deployfailNamespace(receiptNamespaceDigest) {
  requireValue(typeof receiptNamespaceDigest === 'string' && HEX.test(receiptNamespaceDigest),
    'deploy-fail namespace requires a receipt namespace digest')
  return hash(`deployfail-v1:${receiptNamespaceDigest}`)
}

/**
 * The recorded intent for a (repo, sha), or null. Absence means "not yet
 * recorded", never "dropped": drops are logged by the watcher, not stored.
 */
export function findIntent(state, repository, sha, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'deploy-fail repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'deploy-fail sha is invalid')
  checkDeployfailState(state, maxEntries)
  return state.intents.find((e) => e.key === deployFailKey(repository, sha)) ?? null
}

/**
 * Record a fresh intent. Throws when one already exists for the key: the
 * watcher must check first, and a repeat delivery for the same SHA adds
 * nothing. Prunes the oldest entries past the budget.
 */
export function applyRecord(state, { repository, sha, environment, logUrl = null, firstSeenMs, deployOwner, body }, maxEntries = 1000) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'deploy-fail repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'deploy-fail sha is invalid')
  requireValue(typeof environment === 'string' && environment.trim().length > 0 &&
    environment.length <= 255 && SAFE_NAME.test(environment.trim()),
    'deploy-fail environment is invalid')
  requireValue(logUrl === null || typeof logUrl === 'string', 'deploy-fail log URL is invalid')
  checkLogUrl(logUrl)
  requireValue(isMs(firstSeenMs), 'deploy-fail first-seen time is invalid')
  requireValue(typeof deployOwner === 'string' && DEPLOY_OWNER_ROLE.test(deployOwner), 'deploy-fail owner is invalid')
  requireValue(typeof body === 'string' && body.length > 0 && body.length <= 4096, 'deploy-fail body is invalid')
  checkDeployfailState(state, maxEntries)
  requireValue(findIntent(state, repository, sha, maxEntries) === null,
    'deploy-fail intent is already recorded for this key; a repeat adds nothing')
  state.intents.push({ key: deployFailKey(repository, sha), repository, sha,
    environment: environment.trim(), logUrl, firstSeenMs, deployOwner, body, status: 'recorded' })
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

export async function loadDeployfailState({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'deploy-fail directory must be absolute')
  requireValue(typeof namespace === 'string' && HEX.test(namespace), 'deploy-fail namespace must be a SHA-256 digest')
  requireValue(Number.isInteger(maxBytes) && maxBytes >= 1024 && maxBytes <= 16 * 1024 * 1024 &&
    Number.isInteger(maxEntries) && maxEntries > 0 && maxEntries <= 100000, 'deploy-fail storage budget is invalid')
  const path = join(directory, DEPLOYFAIL_FILE)
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('deploy-fail state could not be opened')
    return freshDeployfailState(namespace)
  }
  let text
  try {
    const info = await handle.stat()
    requireValue(info.isFile() && info.uid === process.getuid() && (info.mode & 0o077) === 0 &&
      info.size <= maxBytes, 'deploy-fail state is unsafe or exceeds its budget')
    const buffer = Buffer.alloc(maxBytes + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    requireValue(length <= maxBytes, 'deploy-fail state exceeds its byte budget')
    text = new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length))
  } catch {
    throw new Error('deploy-fail state could not be read; refusing to reset it')
  } finally { await handle.close() }
  let state
  try {
    state = JSON.parse(text)
  } catch {
    throw new Error('deploy-fail state is corrupt; refusing to reset it')
  }
  requireValue(state?.namespace === namespace,
    'deploy-fail state namespace does not match; refusing to mix origins, companies, scopes or modes')
  try {
    return checkDeployfailState(state, maxEntries)
  } catch {
    throw new Error('deploy-fail state shape is invalid; refusing to reset it')
  }
}

export async function saveDeployfailState({ directory, state, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  requireValue(typeof directory === 'string' && isAbsolute(directory), 'deploy-fail directory must be absolute')
  const text = JSON.stringify(checkDeployfailState(state, maxEntries)) + '\n'
  requireValue(Buffer.byteLength(text) <= maxBytes, 'deploy-fail state exceeds its byte budget')
  const path = join(directory, DEPLOYFAIL_FILE)
  const temp = join(directory, `.deployfail-${randomUUID()}.tmp`)
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
    throw new Error('deploy-fail persistence failed; stop processing and replay on recovery')
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
export function createMemoryDeployfailStore(seed = null, maxEntries = 1000) {
  const state = seed ?? freshDeployfailState(hash('deployfail-test'))
  checkDeployfailState(state, maxEntries)
  return {
    async findIntent(repository, sha) {
      const found = findIntent(state, repository, sha, maxEntries)
      return found === null ? null : { ...found }
    },
    async recordIntent({ key, repository, sha, environment, logUrl = null, firstSeenMs, deployOwner, body }) {
      requireValue(key === deployFailKey(repository, sha), 'deploy-fail intent key does not match its repository and sha')
      return { ...applyRecord(state, { repository, sha, environment, logUrl, firstSeenMs, deployOwner, body }, maxEntries) }
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
export function createFileDeployfailStore({ directory, namespace, maxBytes = 1024 * 1024, maxEntries = 1000 }) {
  const storage = { directory, namespace, maxBytes, maxEntries }
  return {
    async findIntent(repository, sha) {
      const state = await loadDeployfailState(storage)
      const found = findIntent(state, repository, sha, maxEntries)
      return found === null ? null : { ...found }
    },
    async recordIntent({ key, repository, sha, environment, logUrl = null, firstSeenMs, deployOwner, body }) {
      requireValue(key === deployFailKey(repository, sha), 'deploy-fail intent key does not match its repository and sha')
      const state = await loadDeployfailState(storage)
      const entry = applyRecord(state, { repository, sha, environment, logUrl, firstSeenMs, deployOwner, body }, maxEntries)
      await saveDeployfailState({ directory, state, maxBytes, maxEntries })
      return { ...entry }
    },
  }
}
