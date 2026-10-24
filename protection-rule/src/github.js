// Fresh GitHub reads for the protection rule. Use the installed credential
// path (an App installation token minted at request time from the configured
// App identity), never shell interpolation or tokens in arguments. No polling
// and no retries in this module: a read that cannot complete throws and the
// caller fails closed.
//
// Every decision input is recomputed from these reads, never from delivery
// payloads: webhook fields are claims, and a claim is only established when a
// fresh API read confirms it.

import { parseDocument, runId as decimalId } from './document.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/
const SHA40 = /^[0-9a-f]{40}$/

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

/**
 * Import an App RSA private key for RS256 JWT signing. Accepts PKCS#8
 * ("PRIVATE KEY" armor) and SEC1/RSA ("RSA PRIVATE KEY" armor) PEM blocks.
 * Throws on anything else — including OpenSSH and encrypted armor.
 *
 * @param {string} pem
 * @returns {Promise<CryptoKey>}
 */
async function importAppKey(pem) {
  try {
    const cleaned = pem.replace(/\r/g, '')
    const match = /-----BEGIN ([A-Za-z0-9 ]+)-----([\s\S]*?)-----END [A-Za-z0-9 ]+-----/.exec(cleaned)
    if (!match) throw new Error('no armor')
    const label = match[1].trim()
    let bytes = base64ToBytes(match[2].replace(/\s+/g, ''))
    if (label === 'PRIVATE KEY') {
      // PKCS#8 imports directly.
    } else if (label === 'RSA PRIVATE KEY') {
      // SEC1/RSA raw key: wrap in a PKCS#8 envelope (version 0, rsaEncryption).
      bytes = pkcs8WrapRsa(bytes)
    } else {
      throw new Error('unsupported armor')
    }
    return await globalThis.crypto.subtle.importKey('pkcs8', bytes,
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign'])
  } catch {
    throw new Error('GitHub App key is invalid')
  }
}

/**
 * Wrap raw RSA (SEC1) DER in a PKCS#8 envelope. Minimal DER writer for exactly
 * this shape: SEQUENCE { INTEGER 0, SEQUENCE { OID rsaEncryption, NULL },
 * OCTET STRING key }. Lengths use the short form where they fit and the
 * 0x81/0x82 long form otherwise — App keys are small enough that 0x83+ never
 * applies, and the size guard below enforces that assumption loudly.
 *
 * @param {Uint8Array} rsaDer
 * @returns {Uint8Array}
 */
function pkcs8WrapRsa(rsaDer) {
  const len = (n) => {
    if (n < 128) return [n]
    if (n < 256) return [0x81, n]
    if (n < 65536) return [0x82, n >> 8, n & 0xff]
    throw new Error('key too large')
  }
  const tlv = (tag, content) => [tag, ...len(content.length), ...content]
  const oidRsa = [0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01]
  const nullTag = [0x05, 0x00]
  const version = [0x02, 0x01, 0x00]
  const algorithm = tlv(0x30, [...oidRsa, ...nullTag])
  const octet = tlv(0x04, [...rsaDer])
  return Uint8Array.from(tlv(0x30, [...version, ...algorithm, ...octet]))
}

/**
 * Encode bytes as unpadded base64url. No Buffer: this runs on Workers too.
 *
 * @param {Uint8Array} bytes
 * @returns {string}
 */
function base64Url(bytes) {
  let text = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    text += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
  }
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Decode base64 (armor interior) to bytes. Throws on invalid input.
 *
 * @param {string} text
 * @returns {Uint8Array}
 */
function base64ToBytes(text) {
  const bin = atob(text)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/**
 * Mint an installation token for the claimed installation, using ONLY the
 * configured App identity. On ANY mint failure the caller fails closed; the
 * failure is never answered by trying another credential.
 *
 * @param {object} args
 * @param {(url: string, init?: object) => Promise<Response>} args.fetchImpl
 * @param {string | number} args.appId
 * @param {string} args.privateKeyPem
 * @param {number} args.installationId
 * @param {number} [args.nowMs]
 * @returns {Promise<string>} the installation token (never logged by callers)
 */
export async function mintInstallationToken({ fetchImpl, appId, privateKeyPem, installationId, nowMs = Date.now() }) {
  requireValue(typeof fetchImpl === 'function', 'GitHub transport is required')
  requireValue(
    (typeof appId === 'string' && appId.length > 0) || (typeof appId === 'number' && Number.isSafeInteger(appId)),
    'GitHub App identity is not configured',
  )
  requireValue(typeof privateKeyPem === 'string' && privateKeyPem.includes('PRIVATE KEY'), 'GitHub App key is not configured')
  requireValue(Number.isSafeInteger(installationId) && installationId > 0, 'GitHub installation is invalid')

  // WebCrypto RS256 via PKCS#8 import — no node:crypto, so this runs on the
  // request path (Workers) as well as in the test harness. A key that cannot
  // be imported or cannot sign fails closed: the verdict is REJECT.
  const key = await importAppKey(privateKeyPem)
  const issued = Math.floor(nowMs / 1000) - 60
  const header = base64Url(new TextEncoder().encode(JSON.stringify({ alg: 'RS256', typ: 'JWT' })))
  const body = base64Url(new TextEncoder().encode(JSON.stringify({ iss: String(appId), iat: issued, exp: issued + 600 })))
  const signingInput = new TextEncoder().encode(`${header}.${body}`)
  let signature
  try {
    signature = new Uint8Array(await globalThis.crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, signingInput))
  } catch {
    throw new Error('GitHub App key cannot sign')
  }
  const jwt = `${header}.${body}.${base64Url(signature)}`

  const response = await fetchImpl(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: 'POST',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${jwt}`,
      'x-github-api-version': '2022-11-28',
      'user-agent': 'case_61f942c4-protection-rule',
    },
  })
  if (response.status !== 201) throw new Error('GitHub installation token mint failed')
  const data = await response.json()
  requireValue(data && typeof data.token === 'string' && data.token.length > 0 && !/\s/.test(data.token),
    'GitHub installation token response is invalid')
  return data.token
}

/**
 * Create a scoped GitHub API reader bound to one installation token.
 *
 * @param {object} args
 * @param {(url: string, init?: object) => Promise<Response>} args.fetchImpl
 * @param {string} args.token            installation token; sent as a header, never logged
 * @param {string[]} args.allowedRepositories  explicit allowlist; every read is scoped to it
 * @param {number} [args.timeoutMs]
 */
export function createGithubReader({ fetchImpl, token, allowedRepositories, timeoutMs = 30000 }) {
  requireValue(typeof fetchImpl === 'function', 'GitHub transport is required')
  requireValue(typeof token === 'string' && token.length > 0 && !/\s/.test(token), 'GitHub credential is missing')
  requireValue(
    Array.isArray(allowedRepositories) &&
      allowedRepositories.length > 0 &&
      allowedRepositories.every((repo) => typeof repo === 'string' && REPOSITORY.test(repo)),
    'an explicit valid GitHub repository allowlist is required',
  )
  requireValue(Number.isInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 120000, 'GitHub timeout is invalid')
  const repositories = new Set(allowedRepositories)

  async function api(pathOrUrl, { method = 'GET', body, maxRedirects = 5 } = {}) {
    // Artifact downloads answer with a redirect to object storage. Redirects
    // are followed https-only and bounded — but the installation token travels
    // same-origin only. A cross-origin hop (the artifact redirect) is followed
    // WITHOUT Authorization, the way a pre-signed URL is fetched: sending the
    // token to object storage would hand GitHub authority to a third party.
    // Anything else — non-https, missing location, too many hops — fails the
    // read, which fails the verdict closed.
    requireValue(Number.isInteger(maxRedirects) && maxRedirects >= 0 && maxRedirects <= 10, 'GitHub redirect budget is invalid')
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      let next = pathOrUrl.startsWith('https://')
        ? pathOrUrl
        : `https://api.github.com${pathOrUrl}`
      for (let hop = 0; ; hop++) {
        const target = new URL(next)
        requireValue(target.protocol === 'https:' && !target.username && !target.password, 'GitHub request must use credential-free HTTPS')
        const sameOrigin = target.origin === 'https://api.github.com'
        const response = await fetchImpl(target.toString(), {
          method,
          signal: controller.signal,
          redirect: 'manual',
          headers: {
            accept: 'application/vnd.github+json',
            // Token to the API origin only — never to a redirect target.
            ...(sameOrigin ? { authorization: `Bearer ${token}` } : {}),
            'x-github-api-version': '2022-11-28',
            'user-agent': 'case_61f942c4-protection-rule',
            ...(body === undefined ? {} : { 'content-type': 'application/json' }),
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        })
        if (response.status === 301 || response.status === 302 || response.status === 303 || response.status === 307 || response.status === 308) {
          requireValue(hop < maxRedirects, 'GitHub redirect budget exceeded')
          const location = response.headers.get('location')
          requireValue(typeof location === 'string' && location.length > 0, 'GitHub redirect has no location')
          try { await response.body?.cancel() } catch { /* discarded, never buffered */ }
          next = new URL(location, target).toString()
          continue
        }
        return response
      }
    } finally {
      clearTimeout(timer)
    }
  }

  async function readBytes(response, maxBytes) {
    const declared = response.headers.get('content-length')
    requireValue(declared === null || (/^\d+$/.test(declared) && Number(declared) <= maxBytes), 'GitHub body exceeds budget')
    requireValue(response.body, 'GitHub body is missing')
    const reader = response.body.getReader()
    let timer
    try {
      return await Promise.race([
        (async () => {
          const chunks = []
          let size = 0
          for (;;) {
            const { done, value } = await reader.read()
            if (done) break
            size += value.length
            requireValue(size <= maxBytes, 'GitHub body exceeds budget')
            chunks.push(value)
          }
          requireValue(size > 0, 'GitHub body is empty')
          const bytes = new Uint8Array(size)
          let offset = 0
          for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length }
          return bytes
        })(),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('GitHub body timed out')), timeoutMs) }),
      ])
    } finally {
      clearTimeout(timer)
      void reader.cancel().catch(() => {})
    }
  }
  async function readJson(response) {
    return parseDocument(new TextDecoder('utf-8', { fatal: true }).decode(await readBytes(response, 2 * 1024 * 1024)))
  }
  function scope(repository) {
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    return `/repos/${repository}`
  }
  async function jsonRead(path) {
    const response = await api(path)
    requireValue(response.status === 200, 'GitHub read failed')
    return readJson(response)
  }
  function completeList(data, key) {
    requireValue(data && Array.isArray(data[key]) && Number.isSafeInteger(data.total_count) && data.total_count <= 100 && data.total_count === data[key].length, 'GitHub list is incomplete or exceeds budget')
    return data[key]
  }

  // Official run metadata authenticates workflow code, NOT the separately
  // checked-out source input: https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run
  async function getRun(repository, runId) {
    const run = await jsonRead(`${scope(repository)}/actions/runs/${decimalId(runId)}`)
    requireValue(decimalId(run.id) === decimalId(runId), 'GitHub run identity does not match')
    requireValue(typeof run.head_sha === 'string' && SHA40.test(run.head_sha), 'GitHub run head is invalid')
    requireValue(typeof run.event === 'string' && run.event.length > 0 && typeof run.html_url === 'string', 'GitHub run metadata is invalid')
    return { id: runId, headSha: run.head_sha, event: run.event, htmlUrl: run.html_url,
      repository: run.repository?.full_name, headRepository: run.head_repository?.full_name,
      workflowPath: run.path, workflowId: run.workflow_id == null ? null : decimalId(run.workflow_id),
      headBranch: run.head_branch, attempt: run.run_attempt == null ? null : decimalId(run.run_attempt),
      status: run.status, conclusion: run.conclusion }
  }
  // Attempt-scoped jobs prevent a successful job from a previous rerun from
  // authenticating this request. Never silently inspect only the first page.
  async function getJobs(repository, runId, attempt) {
    const data = await jsonRead(`${scope(repository)}/actions/runs/${decimalId(runId)}/attempts/${decimalId(attempt)}/jobs?per_page=100`)
    return completeList(data, 'jobs').map((job) => ({ id: decimalId(job.id), name: job.name,
      runId: decimalId(job.run_id), attempt: decimalId(job.run_attempt), headSha: job.head_sha,
      headBranch: job.head_branch, status: job.status, conclusion: job.conclusion,
      startedAt: job.started_at, completedAt: job.completed_at }))
  }
  async function getPendingDeployments(repository, runId) {
    const data = await jsonRead(`${scope(repository)}/actions/runs/${decimalId(runId)}/pending_deployments`)
    requireValue(Array.isArray(data) && data.length <= 100 && data.every((p) => typeof p.environment?.name === 'string'), 'GitHub pending environments are invalid')
    return data.map((p) => ({ environment: p.environment.name }))
  }
  async function isAncestor(repository, source, head) {
    requireValue(SHA40.test(source) && SHA40.test(head), 'GitHub comparison SHAs are invalid')
    const data = await jsonRead(`${scope(repository)}/compare/${source}...${head}`)
    return ['ahead', 'identical'].includes(data.status) && data.merge_base_commit?.sha === source
  }
  async function listArtifacts(repository, runId) {
    const prefix = scope(repository)
    const data = await jsonRead(`${prefix}/actions/runs/${decimalId(runId)}/artifacts?per_page=100`)
    return completeList(data, 'artifacts').map((artifact) => {
      const id = decimalId(artifact.id)
      requireValue(typeof artifact.name === 'string' && typeof artifact.expired === 'boolean', 'GitHub artifact metadata is invalid')
      // Only the exact artifact endpoint for this repository may start a
      // credentialed download. Object-storage redirects get NO token.
      const downloadUrl = `https://api.github.com${prefix}/actions/artifacts/${id}/zip`
      requireValue(artifact.archive_download_url === downloadUrl, 'GitHub artifact URL differs from its identity')
      requireValue(Number.isSafeInteger(artifact.size_in_bytes), 'GitHub artifact size is invalid')
      return { id, name: artifact.name, expired: artifact.expired, downloadUrl,
        size: artifact.size_in_bytes, createdAt: artifact.created_at,
        runId: decimalId(artifact.workflow_run?.id), headSha: artifact.workflow_run?.head_sha,
        headBranch: artifact.workflow_run?.head_branch }
    })
  }
  // Check runs on ONE exact commit. `filter=latest` is GitHub's own required-
  // check semantics: a superseded run never speaks for the commit. Each run
  // keeps its producing App id, because a check NAME is claimable by any App
  // (or commit status) with write access; only the pinned App id authenticates
  // who produced it. A list that does not fit one page is refused, never
  // silently truncated.
  async function listCheckRuns(repository, sha) {
    requireValue(SHA40.test(sha), 'GitHub check-run commit is invalid')
    const data = await jsonRead(`${scope(repository)}/commits/${sha}/check-runs?filter=latest&per_page=100`)
    return completeList(data, 'check_runs').map((run) => {
      requireValue(typeof run.name === 'string' && run.name.length > 0 && typeof run.status === 'string', 'GitHub check run metadata is invalid')
      requireValue(typeof run.head_sha === 'string' && SHA40.test(run.head_sha), 'GitHub check run head is invalid')
      let title = run.output?.title
      if (title == null || title === '') {
        title = typeof run.output?.summary === 'string' ? run.output.summary.split(/\r?\n/, 1)[0] : null
      }
      return { id: decimalId(run.id), name: run.name, headSha: run.head_sha, status: run.status,
        conclusion: run.conclusion ?? null, completedAt: run.completed_at ?? null,
        externalId: typeof run.external_id === 'string' ? run.external_id : null,
        appId: Number.isSafeInteger(run.app?.id) ? run.app.id : null,
        title: typeof title === 'string' ? title : null }
    })
  }
  // Pull requests GitHub associates with one commit. A squash merge puts a NEW
  // commit on main whose checks and review never ran on it: the review lives
  // on the pull request head, and `merge_commit_sha` is GitHub's own link from
  // the merged commit back to that head.
  async function listPullsForCommit(repository, sha) {
    requireValue(SHA40.test(sha), 'GitHub pull-request commit is invalid')
    const data = await jsonRead(`${scope(repository)}/commits/${sha}/pulls?per_page=100`)
    requireValue(Array.isArray(data) && data.length < 100, 'GitHub pull-request list is invalid or incomplete')
    return data.map((pull) => {
      requireValue(pull && Number.isSafeInteger(pull.number) && pull.number > 0 && typeof pull.state === 'string', 'GitHub pull request metadata is invalid')
      return { number: pull.number, state: pull.state, mergedAt: pull.merged_at ?? null,
        mergeCommitSha: pull.merge_commit_sha ?? null, baseRef: pull.base?.ref ?? null,
        baseRepository: pull.base?.repo?.full_name ?? null,
        headSha: typeof pull.head?.sha === 'string' && SHA40.test(pull.head.sha) ? pull.head.sha : null }
    })
  }
  async function downloadBytes(url, maxBytes) {
    const target = new URL(url)
    requireValue(target.origin === 'https://api.github.com' && !target.username && !target.password && !target.search && !target.hash, 'GitHub download origin is invalid')
    requireValue([...repositories].some((repo) => new RegExp(`^/repos/${repo.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/actions/artifacts/[1-9][0-9]*/zip$`).test(target.pathname)), 'GitHub download scope is invalid')
    requireValue(Number.isInteger(maxBytes) && maxBytes > 0 && maxBytes <= 64 * 1024 * 1024, 'GitHub download budget is invalid')
    const response = await api(url)
    requireValue(response.status === 200, 'GitHub download failed')
    return readBytes(response, maxBytes)
  }

  return { getRun, getJobs, getPendingDeployments, isAncestor, listArtifacts, listCheckRuns, listPullsForCommit, downloadBytes, api }
}
