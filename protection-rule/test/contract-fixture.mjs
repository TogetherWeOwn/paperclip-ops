import { readFileSync } from 'node:fs'
import { parseDocument } from '../src/document.js'
import { createGithubReader } from '../src/github.js'
import { zipOf } from './zip-fixture.mjs'

export const PLAN_TEXT = readFileSync(new URL('./fixtures/staging-migrate-plan.json', import.meta.url), 'utf8')
export const CLAIM_TEXT = readFileSync(new URL('./fixtures/staging-migrate-apply-claim.json', import.meta.url), 'utf8')
export const CLAIM = parseDocument(CLAIM_TEXT)
export const HASH = CLAIM.plan_manifest_sha256
export const HEAD = CLAIM.workflow_head_sha
export const SOURCE = CLAIM.source_sha
export const PLAN_HEAD = 'c'.repeat(40)
export const REPO = CLAIM.repository
export const RUN_ID = Number(CLAIM.apply_run_id)
export const PLAN_RUN_ID = CLAIM.plan_run_id

// Source evidence. Each trusted commit (migration source, apply workflow head,
// producing plan head) is a squash commit on main: its CI ran on it, and its
// independent review ran on the head of the merged pull request. The template
// objects are trimmed real GitHub responses for Bot main 7a5043e4b3 / PR #626 / head
// a4da131e36 (see fixtures/README.md); a fixture clones them and rebinds only
// the SHAs, the pull request number, the review's external_id and CI run ids.
const realJson = (name) => JSON.parse(readFileSync(new URL(`./fixtures/github/${name}`, import.meta.url), 'utf8'))
export const REAL_MAIN_CHECKS = realJson('main-7a5043e-check-runs.json')
export const REAL_MAIN_PULLS = realJson('main-7a5043e-pulls.json')
export const REAL_HEAD_CHECKS = realJson('pr626-head-a4da131-check-runs.json')
export const CI_APP_ID = 15368
export const REVIEW_APP_ID = 5191093
export const PR_HEADS = { [SOURCE]: '1'.repeat(40), [HEAD]: '2'.repeat(40), [PLAN_HEAD]: '3'.repeat(40) }
const PR_NUMBERS = { [SOURCE]: 101, [HEAD]: 102, [PLAN_HEAD]: 103 }
const clone = (value) => JSON.parse(JSON.stringify(value))
export function ciRuns(sha) {
  return REAL_MAIN_CHECKS.check_runs.filter((r) => ['ci-ok', 'gitleaks', 'pr-lint'].includes(r.name))
    .map((r, i) => ({ ...clone(r), head_sha: sha, id: r.id + i }))
}
export function reviewRun(prHead, number) {
  const real = REAL_HEAD_CHECKS.check_runs.find((r) => r.name === 'Paperclip Review')
  return { ...clone(real), head_sha: prHead, external_id: `${real.external_id.split(':')[0]}:${number}:${prHead}` }
}
export function mergedPull(sha) {
  const real = REAL_MAIN_PULLS[0]
  const pull = clone(real)
  pull.number = PR_NUMBERS[sha]
  pull.merge_commit_sha = sha
  pull.head.sha = PR_HEADS[sha]
  return pull
}

// JSON wire integers are emitted directly, not rounded by JSON.stringify.
export function wire(value) {
  if (typeof value === 'bigint') return value.toString()
  if (Array.isArray(value)) return '[' + value.map(wire).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.entries(value).map(([k, v]) => JSON.stringify(k) + ':' + wire(v)).join(',') + '}'
  return JSON.stringify(value)
}
export function jsonResponse(value) { return new Response(wire(value), { headers: { 'content-type': 'application/json' } }) }

export function contractFixture() {
  const started = '2026-10-04T10:00:00Z'
  const created = '2026-10-04T10:01:00Z'
  const completed = '2026-10-04T10:02:00Z'
  const rawRun = (id, head, attempt, apply) => ({ id: BigInt(id), head_sha: head,
    head_branch: 'main', event: 'workflow_dispatch', html_url: `https://github.com/${REPO}/actions/runs/${id}`,
    repository: { full_name: REPO }, head_repository: { full_name: REPO },
    path: CLAIM.workflow_path, workflow_id: 99, run_attempt: attempt,
    status: apply ? 'in_progress' : 'completed', conclusion: apply ? null : 'success' })
  const rawJob = (id, name, run, conclusion = 'success') => ({ id, name, run_id: run.id,
    run_attempt: run.run_attempt, head_sha: run.head_sha, head_branch: 'main',
    status: 'completed', conclusion, started_at: started, completed_at: completed })
  const applyRun = rawRun(CLAIM.apply_run_id, HEAD, 2, true)
  const planRun = rawRun(PLAN_RUN_ID, PLAN_HEAD, 1, false)
  const applyJobs = [rawJob(11, 'staging-migrate (plan)', applyRun), rawJob(12, 'staging-migrate (claim)', applyRun)]
  const planJobs = [rawJob(21, 'staging-migrate (plan)', planRun), rawJob(22, 'staging-migrate (apply)', planRun, 'skipped')]
  const rawArtifact = (id, name, run) => ({ id, name, expired: false,
    archive_download_url: `https://api.github.com/repos/${REPO}/actions/artifacts/${id}/zip`,
    size_in_bytes: 4096, created_at: created,
    workflow_run: { id: run.id, head_sha: run.head_sha, head_branch: 'main' } })
  const applyArtifacts = [rawArtifact(31, 'staging-migrate-apply-claim', applyRun)]
  const planArtifacts = [rawArtifact(32, 'staging-migrate-manifest', planRun)]
  const f = { applyRun, planRun, applyJobs, planJobs, applyArtifacts, planArtifacts,
    claim: parseDocument(CLAIM_TEXT), manifest: parseDocument(PLAN_TEXT),
    pending: [{ environment: { id: 4, name: CLAIM.environment_name } }],
    ancestor: true, redirect: false, requests: [], calls: { run: 0, jobs: 0, artifacts: 0, downloads: 0, checkRuns: 0, pulls: 0 },
    claimText: null, planText: null,
    // sha -> raw check runs / raw pulls, served per exact commit.
    checkRuns: Object.fromEntries([SOURCE, HEAD, PLAN_HEAD].flatMap((sha) => [
      [sha, ciRuns(sha)], [PR_HEADS[sha], [reviewRun(PR_HEADS[sha], PR_NUMBERS[sha])]],
    ])),
    pulls: Object.fromEntries([SOURCE, HEAD, PLAN_HEAD].map((sha) => [sha, [mergedPull(sha)]])) }
  const prefix = `https://api.github.com/repos/${REPO}`
  const list = (key, rows) => jsonResponse({ total_count: rows.length, [key]: rows })
  f.fetch = async (url, init = {}) => {
    const target = String(url)
    f.requests.push({ url: target, authorization: init.headers?.authorization ?? null })
    if (target === `${prefix}/actions/runs/${RUN_ID}`) { f.calls.run++; return jsonResponse(f.applyRun) }
    if (target === `${prefix}/actions/runs/${PLAN_RUN_ID}`) { f.calls.run++; return jsonResponse(f.planRun) }
    if (target === `${prefix}/actions/runs/${RUN_ID}/attempts/2/jobs?per_page=100`) { f.calls.jobs++; return list('jobs', f.applyJobs) }
    if (target === `${prefix}/actions/runs/${PLAN_RUN_ID}/attempts/1/jobs?per_page=100`) { f.calls.jobs++; return list('jobs', f.planJobs) }
    if (target === `${prefix}/actions/runs/${RUN_ID}/pending_deployments`) return jsonResponse(f.pending)
    if (target === `${prefix}/actions/runs/${RUN_ID}/artifacts?per_page=100`) { f.calls.artifacts++; return list('artifacts', f.applyArtifacts) }
    if (target === `${prefix}/actions/runs/${PLAN_RUN_ID}/artifacts?per_page=100`) { f.calls.artifacts++; return list('artifacts', f.planArtifacts) }
    const checkRuns = /^.*\/commits\/([0-9a-f]{40})\/check-runs\?filter=latest&per_page=100$/.exec(target)
    if (checkRuns && target.startsWith(`${prefix}/`)) { f.calls.checkRuns++; return list('check_runs', f.checkRuns[checkRuns[1]] ?? []) }
    const pulls = /^.*\/commits\/([0-9a-f]{40})\/pulls\?per_page=100$/.exec(target)
    if (pulls && target.startsWith(`${prefix}/`)) { f.calls.pulls++; return jsonResponse(f.pulls[pulls[1]] ?? []) }
    if (target.startsWith(`${prefix}/compare/`)) return jsonResponse({ status: f.ancestor ? 'ahead' : 'diverged', merge_base_commit: { sha: SOURCE } })
    if (target === `${prefix}/actions/artifacts/31/zip`) {
      f.calls.downloads++
      return new Response(f.claimZip ?? zipOf(f.claimText ?? wire(f.claim), { name: 'staging-migrate-apply-claim.json', descriptor: true }))
    }
    if (target === `${prefix}/actions/artifacts/32/zip`) {
      f.calls.downloads++
      if (f.redirect) return new Response(null, { status: 302, headers: { location: 'https://objects.example.invalid/zip' } })
      return new Response(f.planZip ?? zipOf(f.planText ?? wire(f.manifest), { descriptor: true }))
    }
    if (target === 'https://objects.example.invalid/zip') return new Response(f.planZip ?? zipOf(f.planText ?? wire(f.manifest), { descriptor: true }))
    return new Response('{}', { status: 404 })
  }
  f.github = createGithubReader({ fetchImpl: f.fetch, token: 'offline-stub', allowedRepositories: [REPO] })
  return f
}
