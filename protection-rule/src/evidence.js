// Staging evidence is published BEFORE the protected apply job can start.
// Authenticate the claim job/run/attempt, then the prior successful plan run.
// The approval key is the Rust projection, NOT a hash of JSON archive bytes.
import { extractManifest, MAX_ZIP_BYTES } from './archive.js'
import { isRunId, runId, versionString } from './document.js'
export { extractManifest, MAX_MANIFEST_BYTES, MAX_ZIP_BYTES } from './archive.js'

export const APPLY_REPOSITORY = 'TogetherWeOwn/two-bot-next'
export const WORKFLOW_PATH = '.github/workflows/staging-migrate.yml'
export const APPLY_ENVIRONMENT = 'staging-migrate-apply'
export const PLAN_ARTIFACT_NAME = 'staging-migrate-manifest'
export const CLAIM_ARTIFACT_NAME = 'staging-migrate-apply-claim'
const PLAN_JOB = 'staging-migrate (plan)'
const CLAIM_JOB = 'staging-migrate (claim)'
const APPLY_JOB = 'staging-migrate (apply)'
const SHA40 = /^[0-9a-f]{40}$/
const SHA256 = /^[0-9a-f]{64}$/
const SHA384 = /^[0-9a-f]{96}$/

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
function sha(value, pattern) { return typeof value === 'string' && pattern.test(value) }
function bareRef(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.:#/-]{1,200}$/.test(value) && !value.includes('://')
}
function targetIdentity(target) {
  requireValue(object(target) && Object.keys(target).sort().join(',') === 'database,host', 'target shape is invalid')
  requireValue(typeof target.host === 'string' && target.host.length <= 253 && /^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/.test(target.host), 'target host is not bare')
  requireValue(typeof target.database === 'string' && /^[A-Za-z0-9_]{1,63}$/.test(target.database), 'target database is not bare')
  requireValue(!/prod/i.test(target.host + target.database) && !/-pooler/i.test(target.host), 'target is not staging direct')
}
function versions(values, claim = false) {
  requireValue(Array.isArray(values) && values.length <= 10000, 'version list is invalid')
  const out = values.map((v) => versionString(v, claim))
  for (let i = 1; i < out.length; i++) requireValue(BigInt(out[i - 1]) < BigInt(out[i]), 'versions are not strictly ascending')
  return out
}
function sameList(a, b) { return a.length === b.length && a.every((value, i) => value === b[i]) }

export async function sha256Hex(bytes) {
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes))
  return [...digest].map((b) => b.toString(16).padStart(2, '0')).join('')
}

// Exact projection in crates/cutover/src/staging_migrate.rs::manifest_hash:
// source_sha\n; pending version\n; version:description:sha384\n, in producer order.
// Rust serializes versions as JSON i64s; parseDocument preserves them as BigInt
// outside the safe Number range, and claim versions stay canonical strings.
export async function projectionHash(document) {
  requireValue(object(document) && sha(document.source_sha, SHA40), 'manifest source is invalid')
  const pending = versions(document.pending_before)
  requireValue(Array.isArray(document.source_migrations) && document.source_migrations.length <= 10000, 'migration table is invalid')
  let projection = document.source_sha + '\n' + pending.map((v) => v + '\n').join('')
  const migrations = new Map()
  let previous = null
  for (const entry of document.source_migrations) {
    requireValue(object(entry), 'migration row is invalid')
    const version = versionString(entry.version)
    requireValue(previous === null || BigInt(previous) < BigInt(version), 'migration versions are ambiguous')
    requireValue(typeof entry.description === 'string' && !/[\r\n\0]/.test(entry.description) && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(entry.description), 'migration description is ambiguous')
    requireValue(sha(entry.sha384, SHA384) && entry.sha384_recomputed === entry.sha384, 'migration SQL checksum disagrees')
    migrations.set(version, entry.sha384)
    projection += `${version}:${entry.description}:${entry.sha384}\n`
    previous = version
  }
  requireValue(pending.every((v) => migrations.has(v)), 'pending version is outside source')
  return sha256Hex(new TextEncoder().encode(projection))
}

function validateManifest(document, claim) {
  const fields = ['runner_version', 'tool', 'mode', 'source_sha', 'target', 'recovery_evidence_ref', 'acl_plan_ref', 'role', 'role_verified_connections', 'source_migrations', 'ledger_before', 'ledger_after', 'pending_before', 'expected_pending', 'plan_manifest_sha256', 'plan_run_id', 'plan_provenance_verified', 'applied_count']
  requireValue(object(document) && Object.keys(document).sort().join(',') === fields.sort().join(','), 'manifest schema differs')
  requireValue(document.runner_version === 1 && document.mode === 'plan', 'manifest is not a v1 plan')
  requireValue(document.source_sha === claim.source_sha, 'manifest source differs from claim')
  targetIdentity(document.target)
  requireValue(document.target.host === claim.target.host && document.target.database === claim.target.database, 'manifest target differs from claim')
  requireValue(bareRef(document.recovery_evidence_ref) && document.recovery_evidence_ref === claim.recovery_evidence_ref && bareRef(document.acl_plan_ref) && document.acl_plan_ref === claim.acl_plan_ref, 'manifest approval refs differ from claim')
  requireValue(document.role === 'two_bot_migrator_ro' && Number.isSafeInteger(document.role_verified_connections) && document.role_verified_connections > 0, 'plan role is unverified')
  requireValue(document.applied_count === 0 && document.plan_provenance_verified === false && document.plan_run_id === '', 'manifest is not a plan-only request')
  requireValue(sha(document.plan_manifest_sha256, SHA256), 'manifest digest is invalid')
  requireValue(sameList(versions(document.pending_before), versions(claim.expected_pending, true)), 'manifest pending differs from claim')
  versions(document.expected_pending) // Plan-mode input is validated but ignored by Rust.
  requireValue(object(document.tool) && document.tool.name === 'two-bot-cutover staging_migrate', 'manifest tool is invalid')
  requireValue(Array.isArray(document.ledger_before) && Array.isArray(document.ledger_after), 'plan ledger is invalid')
  const source = new Map(document.source_migrations.map((m) => [versionString(m.version), m.sha384]))
  function ledger(rows) {
    const identities = versions(rows.map((r) => r.version))
    rows.forEach((r, i) => requireValue(typeof r.description === 'string' && r.success === true && source.get(identities[i]) === r.sha384, 'plan ledger differs from source'))
    return rows.map((r, i) => `${identities[i]}:${r.description}:${r.sha384}`)
  }
  const before = ledger(document.ledger_before)
  requireValue(sameList(before, ledger(document.ledger_after)), 'plan changed its ledger')
  const applied = new Set(document.ledger_before.map((r) => versionString(r.version)))
  requireValue(sameList([...source.keys()].filter((v) => !applied.has(v)), versions(document.pending_before)), 'plan pending does not reconcile')
}

function validateRun(run, repository, id) {
  requireValue(object(run) && runId(run.id) === runId(id), 'workflow run differs from request')
  requireValue(run.repository === repository && run.headRepository === repository, 'workflow repository differs')
  requireValue(run.workflowPath === WORKFLOW_PATH && isRunId(run.workflowId), 'workflow path differs')
  requireValue(run.headBranch === 'main' && run.event === 'workflow_dispatch', 'workflow is not a main dispatch')
  requireValue(sha(run.headSha, SHA40) && isRunId(run.attempt), 'workflow head or attempt is invalid')
}
function jobFor(jobs, name, run, conclusion) {
  const found = jobs.filter((j) => j.name === name)
  requireValue(found.length === 1, 'workflow job is absent or ambiguous')
  const job = found[0]
  requireValue(job.runId === runId(run.id) && job.attempt === run.attempt && job.headSha === run.headSha && job.headBranch === 'main', 'workflow job provenance differs')
  requireValue(job.status === 'completed' && job.conclusion === conclusion, 'workflow job did not finish as required')
  return job
}
async function readArtifact(github, repository, run, job, name) {
  const artifacts = await github.listArtifacts(repository, run.id)
  const found = artifacts.filter((a) => a.name === name)
  requireValue(found.length === 1 && found[0].expired === false, 'artifact is absent, expired or ambiguous')
  const artifact = found[0]
  requireValue(artifact.runId === runId(run.id) && artifact.headSha === run.headSha && artifact.headBranch === 'main', 'artifact run binding differs')
  const created = Date.parse(artifact.createdAt)
  const start = Date.parse(job.startedAt)
  const end = Date.parse(job.completedAt)
  // Jobs may report whole seconds while artifact creation retains milliseconds.
  // Only that final reported second is tolerated, not an arbitrary stale window.
  const endExclusive = end + (/\.\d+Z$/.test(job.completedAt) ? 1 : 1000)
  requireValue(Number.isFinite(created) && Number.isFinite(start) && Number.isFinite(end) && start <= end && start <= created && created < endExclusive, 'artifact is from another attempt or job')
  requireValue(artifact.size > 0 && artifact.size <= MAX_ZIP_BYTES, 'artifact exceeds budget')
  const zip = await github.downloadBytes(artifact.downloadUrl, MAX_ZIP_BYTES)
  return extractManifest(zip, name + '.json').manifest
}

function validateClaim(claim, run, environment) {
  const fields = ['schema_version', 'kind', 'repository', 'workflow_path', 'environment_name', 'apply_run_id', 'apply_run_attempt', 'plan_run_id', 'workflow_head_sha', 'source_sha', 'plan_manifest_sha256', 'target', 'expected_pending', 'recovery_evidence_ref', 'acl_plan_ref']
  requireValue(object(claim) && Object.keys(claim).sort().join(',') === fields.sort().join(','), 'claim schema differs')
  requireValue(claim.schema_version === 1 && claim.kind === CLAIM_ARTIFACT_NAME, 'claim version or kind differs')
  requireValue(claim.repository === APPLY_REPOSITORY && claim.workflow_path === WORKFLOW_PATH && claim.environment_name === environment, 'claim scope differs')
  requireValue(isRunId(claim.apply_run_id) && isRunId(claim.apply_run_attempt) && isRunId(claim.plan_run_id), 'claim run identities are invalid')
  requireValue(claim.apply_run_id === runId(run.id) && claim.apply_run_attempt === run.attempt && claim.plan_run_id !== claim.apply_run_id, 'claim run or attempt differs')
  requireValue(claim.workflow_head_sha === run.headSha && sha(claim.source_sha, SHA40) && sha(claim.plan_manifest_sha256, SHA256), 'claim SHA binding differs')
  targetIdentity(claim.target)
  versions(claim.expected_pending, true)
  requireValue(bareRef(claim.recovery_evidence_ref) && bareRef(claim.acl_plan_ref), 'claim approval refs are invalid')
}

export async function readApplyClaim({ github, repository, applyRunId, applyRun = null, environment = APPLY_ENVIRONMENT }) {
  requireValue(repository === APPLY_REPOSITORY && environment === APPLY_ENVIRONMENT, 'apply scope differs')
  const run = applyRun ?? await github.getRun(repository, applyRunId)
  validateRun(run, repository, applyRunId)
  requireValue(['waiting', 'in_progress', 'pending', 'queued'].includes(run.status) && run.conclusion === null, 'apply run is not pending')
  const pending = await github.getPendingDeployments(repository, run.id)
  requireValue(pending.filter((p) => p.environment === environment).length === 1, 'apply environment is not pending approval')
  const jobs = await github.getJobs(repository, run.id, run.attempt)
  jobFor(jobs, PLAN_JOB, run, 'success')
  const job = jobFor(jobs, CLAIM_JOB, run, 'success')
  const claim = await readArtifact(github, repository, run, job, CLAIM_ARTIFACT_NAME)
  validateClaim(claim, run, environment)
  requireValue(await github.isAncestor(repository, claim.source_sha, run.headSha), 'apply source is outside workflow main history')
  return { ...claim, planManifestSha256: claim.plan_manifest_sha256, planRunId: claim.plan_run_id, workflowHeadSha: run.headSha, sourceSha: claim.source_sha, workflowId: run.workflowId }
}

// Reconfirm immediately before deciding/posting: the run callback URL has no
// attempt component, so a rerun during artifact/GO reads must not inherit GO.
export async function confirmApplyClaim({ github, repository, claim, environment }) {
  const run = await github.getRun(repository, claim.apply_run_id)
  validateRun(run, repository, claim.apply_run_id)
  requireValue(run.attempt === claim.apply_run_attempt && run.headSha === claim.workflowHeadSha && run.workflowId === claim.workflowId, 'apply request changed during verification')
  requireValue(['waiting', 'in_progress', 'pending', 'queued'].includes(run.status) && run.conclusion === null, 'apply request is no longer pending')
  const pending = await github.getPendingDeployments(repository, run.id)
  requireValue(pending.filter((p) => p.environment === environment).length === 1, 'apply environment is no longer pending')
}

export async function verifyPlan({ github, repository, claim }) {
  requireValue(repository === APPLY_REPOSITORY && object(claim) && isRunId(claim.planRunId), 'plan scope or claim differs')
  const run = await github.getRun(repository, claim.planRunId)
  validateRun(run, repository, claim.planRunId)
  requireValue(run.workflowId === claim.workflowId && run.status === 'completed' && run.conclusion === 'success', 'producing workflow failed or differs')
  const jobs = await github.getJobs(repository, run.id, run.attempt)
  const job = jobFor(jobs, PLAN_JOB, run, 'success')
  // REST does not expose dispatch inputs. On this main-only workflow, apply
  // is skipped only for plan mode. A successful apply run is NOT a plan run.
  jobFor(jobs, APPLY_JOB, run, 'skipped')
  requireValue(await github.isAncestor(repository, claim.source_sha, run.headSha), 'plan source is outside workflow main history')
  const document = await readArtifact(github, repository, run, job, PLAN_ARTIFACT_NAME)
  const computed = await projectionHash(document)
  validateManifest(document, claim)
  requireValue(computed === document.plan_manifest_sha256 && computed === claim.planManifestSha256, 'Rust projection differs from plan or claim')
  return { runId: claim.planRunId, headSha: run.headSha, workflowHeadSha: claim.workflowHeadSha, sourceSha: document.source_sha, manifestSha256: computed }
}
