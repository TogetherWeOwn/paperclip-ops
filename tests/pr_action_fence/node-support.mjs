import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { join } from 'node:path'

export const REPO = 'TogetherWeOwn/CLIProxyAPI'
export const HEAD = '159e43cea3c56aeaf51313651cce425a06256939'
export const NEW_HEAD = 'b'.repeat(40)
export const ISSUE = '00000000-0000-4000-8000-000000000001'
export const COMPANY = '00000000-0000-4000-8000-000000000003'
export const NOW = 1792713600000
export const DENY = 'DENY_REVIEW_ONLY'
export const INVALID = 'DENY_INVALID_POLICY_OR_CONTEXT'
export const REQUIRED = Object.freeze(['ci-ok', 'gitleaks'])
export const CHECKS = Object.freeze([...REQUIRED, 'Paperclip Review'].map((name) => Object.freeze({
  name, status: 'completed', conclusion: 'success', completedMs: 1,
})))

export function freeze(value) {
  for (const child of Object.values(value)) if (child && typeof child === 'object') freeze(child)
  return Object.freeze(value)
}
export function snapshot(repository = REPO, number = 1, overrides = {}) {
  return freeze({ repository, number, headSha: HEAD, baseRef: 'main', state: 'open',
    merged: false, draft: false, autoMerge: false, body: '## What Changed\n\nOffline fixture\n',
    labels: [], mergeableState: 'clean', reviewDecision: 'APPROVED', headPushedMs: 1,
    nowMs: NOW, checks: CHECKS, ...overrides })
}
export function context(repository = REPO, number = 1, overrides = {}) {
  return freeze({ repository, number, expectedHead: HEAD, observedHead: HEAD,
    consumer: 'offline-acceptance', ...overrides })
}
export async function settled(fn) {
  try { return await fn() } catch (error) {
    // Containment traps are harness failures, never proof of a policy refusal.
    if (String(error?.message).includes('OFFLINE TRAP')) throw error
    return { error: error.message, code: error.code, decision: error.decision }
  }
}
export function assertDenial(value, code = DENY) {
  assert.ok(JSON.stringify(value).includes(code), `expected explicit ${code} receipt, not an accidental no-op`)
}
export async function scratch(t) {
  const base = process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? process.env.PAPERCLIP_SCRATCH_DIR
  assert.ok(base, 'Set PAPERCLIP_SCRATCH_DIR to run-owned scratch')
  const dir = await mkdtemp(join(base, 'fence-test-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  return dir
}

// Transport spy only; it contains no protected-subject matcher or policy logic.
export function ghSpy(repository = REPO, number = 1, head = HEAD) {
  const writes = [], reads = []
  const run = async (file, args, options) => {
    assert.equal(file, 'gh', 'the injected GitHub runner must not be used for the policy helper')
    assert.equal(options.shell, false)
    assert.ok(options.timeout > 0 && options.maxBuffer > 0)
    const query = args.find((a) => a.startsWith('query=')) ?? ''
    const mutation = /query=\s*mutation/.test(query)
    if (query && !mutation) {
      reads.push([...args])
      return { stdout: JSON.stringify({ data: { repository: {
        nameWithOwner: repository, isPrivate: false,
        pullRequest: { id: 'PR_fixture', number,
          url: `https://github.com/${repository}/pull/${number}`,
          baseRepository: { nameWithOwner: repository }, headRefOid: head,
          headRefName: 'test/fixture', baseRefName: 'main', state: 'OPEN', isDraft: true,
          merged: false, body: 'Offline fixture', title: 'Offline fixture',
          author: { __typename: 'User', login: 'fixture-author' }, reviewDecision: 'APPROVED',
          autoMergeRequest: null, mergeStateStatus: 'CLEAN',
          labels: { nodes: [], pageInfo: { hasNextPage: false } },
          commits: { nodes: [{ commit: { oid: head, pushedDate: '2026-10-01T00:00:00Z',
            statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] },
        },
      } } }) }
    }
    if (args.includes('rules/branches/main')) {
      reads.push([...args]); return { stdout: '[]' }
    }
    writes.push([...args])
    if (mutation) return { stdout: JSON.stringify({ data: {
      convertPullRequestToDraft: { pullRequest: { isDraft: true } },
    } }) }
    return { stdout: '{}' }
  }
  return { writes, reads, run }
}

export function readinessDouble(snap) {
  const writes = [], reads = []
  const github = {
    async getReadinessSnapshot() { reads.push('snapshot'); return structuredClone(snap) },
    async getRulesetRequiredChecks() { reads.push('rulesets'); return [...REQUIRED] },
    async listOpenPullRequests() { reads.push('list'); return [{ repository: snap.repository, number: snap.number }] },
  }
  for (const method of ['updatePullRequestBody', 'markReadyForReview', 'convertToDraft', 'enableAutoMerge']) {
    github[method] = async (...args) => { writes.push([method, ...args]) }
  }
  return { github, writes, reads }
}
