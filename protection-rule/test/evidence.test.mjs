// Bot publisher/Rust golden vectors + actual GitHub-shaped metadata, offline.
// No DB, credential, webhook delivery or live environment operation.
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { projectionHash, readApplyClaim, verifyPlan, sha256Hex, extractManifest } from '../src/evidence.js'
import { parseDocument, runId, versionString } from '../src/document.js'
import { createGithubReader } from '../src/github.js'
import { zipOf, zipOfTexts } from './zip-fixture.mjs'
import { contractFixture, PLAN_TEXT, CLAIM_TEXT, CLAIM, HASH, HEAD, PLAN_HEAD, SOURCE, REPO, RUN_ID, PLAN_RUN_ID, wire } from './contract-fixture.mjs'

async function establish(f) {
  const claim = await readApplyClaim({ github: f.github, repository: REPO, applyRunId: RUN_ID })
  const plan = await verifyPlan({ github: f.github, repository: REPO, claim })
  return { claim, plan }
}

test('Bot golden Rust projection and pre-gate publisher claim agree losslessly', async () => {
  const document = parseDocument(PLAN_TEXT)
  assert.equal(document.pending_before[1], 9007199254740993n)
  assert.equal(await projectionHash(document), HASH)
  assert.notEqual(await sha256Hex(new TextEncoder().encode(PLAN_TEXT)), HASH)
  assert.equal(parseDocument(CLAIM_TEXT).plan_run_id, '37110947190')
  const f = contractFixture()
  const { claim, plan } = await establish(f)
  assert.equal(claim.planRunId, PLAN_RUN_ID)
  assert.equal(claim.workflowHeadSha, HEAD)
  assert.equal(plan.headSha, PLAN_HEAD) // Workflow heads may differ; source may NOT.
  assert.equal(plan.sourceSha, SOURCE)
  assert.equal(plan.manifestSha256, HASH)
  assert.ok(f.requests.some((r) => r.url.endsWith(`/actions/runs/${PLAN_RUN_ID}`)))
  assert.ok(!f.requests.some((r) => r.url.includes('/actions/runs/3711094719/')))
})

test('actual archiver@7.0.1 upload-artifact compression0 streams are compatible', async () => {
  const f = contractFixture()
  f.claimZip = new Uint8Array(readFileSync(new URL('./fixtures/staging-migrate-apply-claim.upload-artifact.zip', import.meta.url)))
  f.planZip = new Uint8Array(readFileSync(new URL('./fixtures/staging-migrate-plan.upload-artifact.zip', import.meta.url)))
  for (const zip of [f.claimZip, f.planZip]) {
    const view = new DataView(zip.buffer, zip.byteOffset, zip.length)
    assert.equal(view.getUint16(6, true) & 8, 8) // Real streams use descriptors.
    assert.equal(view.getUint16(8, true), 0)
  }
  assert.equal((await establish(f)).plan.manifestSha256, HASH)
})

test('artifact/job timestamp precision mismatch tolerates only the final second', async () => {
  const f = contractFixture()
  f.applyArtifacts[0].created_at = '2026-10-04T10:02:00.900Z'
  assert.equal((await establish(f)).plan.manifestSha256, HASH)
  f.applyArtifacts[0].created_at = '2026-10-04T10:02:01.001Z'
  await assert.rejects(establish(f))
})

test('GitHub int64 wire ID above JS safe range is never rounded', async () => {
  const f = contractFixture()
  const id = '9007199254740993'
  const github = createGithubReader({ token: 'offline-stub', allowedRepositories: [REPO], fetchImpl: async (url) => {
    assert.ok(String(url).endsWith('/' + id))
    return new Response(wire({ ...f.planRun, id: BigInt(id) }))
  } })
  assert.equal((await github.getRun(REPO, id)).id, id)
})

test('a protection-waiting apply job has not executed; no step marker is needed', async () => {
  const f = contractFixture()
  f.applyJobs.push({ id: 13, name: 'staging-migrate (apply)', run_id: f.applyRun.id, run_attempt: 2,
    head_sha: HEAD, head_branch: 'main', status: 'waiting', conclusion: null, steps: [] })
  assert.equal((await establish(f)).plan.manifestSha256, HASH)
})

const negatives = {
  'old step-name marker without pre-gate artifact': (f) => { f.applyArtifacts = []; f.applyJobs[0].steps = [{ name: `migrate-plan-binding: sha256=${HASH} run=${PLAN_RUN_ID}` }] },
  'old artifact NAME ending in .json': (f) => { f.planArtifacts[0].name += '.json' },
  'whole JSON-byte hash as approval key': async (f) => { f.claim.plan_manifest_sha256 = f.manifest.plan_manifest_sha256 = await sha256Hex(new TextEncoder().encode(PLAN_TEXT)) },
  'truncated eleven-digit producing run id': (f) => { f.claim.plan_run_id = PLAN_RUN_ID.slice(0, 10) },
  'apply repository differs': (f) => { f.applyRun.repository.full_name = 'Else/other' },
  'apply head repository differs': (f) => { f.applyRun.head_repository.full_name = 'Else/other' },
  'apply workflow differs': (f) => { f.applyRun.path = '.github/workflows/other.yml' },
  'apply branch differs': (f) => { f.applyRun.head_branch = 'feature' },
  'apply event differs': (f) => { f.applyRun.event = 'pull_request' },
  'apply run id differs': (f) => { f.applyRun.id += 1n },
  'apply run already finished': (f) => { f.applyRun.status = 'completed'; f.applyRun.conclusion = 'success' },
  'apply environment absent from pending requests': (f) => { f.pending = [] },
  'apply environment ambiguous': (f) => { f.pending.push(f.pending[0]) },
  'apply environment differs': (f) => { f.pending[0].environment.name = 'production' },
  'claim job absent': (f) => { f.applyJobs.pop() },
  'claim job failed': (f) => { f.applyJobs[1].conclusion = 'failure' },
  'claim job not completed': (f) => { f.applyJobs[1].status = 'in_progress' },
  'claim job ambiguous': (f) => { f.applyJobs.push(f.applyJobs[1]) },
  'claim job belongs to stale attempt': (f) => { f.applyJobs[1].run_attempt = 1 },
  'claim job belongs to another run': (f) => { f.applyJobs[1].run_id = 7 },
  'claim job belongs to another workflow SHA': (f) => { f.applyJobs[1].head_sha = SOURCE },
  'current plan job failed': (f) => { f.applyJobs[0].conclusion = 'failure' },
  'claim artifact missing': (f) => { f.applyArtifacts = [] },
  'claim artifact expired': (f) => { f.applyArtifacts[0].expired = true },
  'claim artifact ambiguous': (f) => { f.applyArtifacts.push(f.applyArtifacts[0]) },
  'claim artifact from old attempt time': (f) => { f.applyArtifacts[0].created_at = '2026-10-03T10:00:00Z' },
  'claim artifact created after job ended': (f) => { f.applyArtifacts[0].created_at = '2026-10-04T10:02:01Z' },
  'claim artifact attached to another run': (f) => { f.applyArtifacts[0].workflow_run.id = 8 },
  'claim artifact oversize': (f) => { f.applyArtifacts[0].size_in_bytes = 17000000 },
  'claim schema version differs': (f) => { f.claim.schema_version = 2 },
  'claim unknown field': (f) => { f.claim.extra = true },
  'claim kind differs': (f) => { f.claim.kind = 'other' },
  'claim repository differs': (f) => { f.claim.repository = 'Else/other' },
  'claim workflow differs': (f) => { f.claim.workflow_path = '.github/workflows/other.yml' },
  'claim environment differs': (f) => { f.claim.environment_name = 'production' },
  'claim run differs': (f) => { f.claim.apply_run_id = '7' },
  'claim stale attempt': (f) => { f.claim.apply_run_attempt = '1' },
  'claim workflow SHA differs': (f) => { f.claim.workflow_head_sha = SOURCE },
  'claim numeric id instead of decimal string': (f) => { f.claim.plan_run_id = 37110947190 },
  'claim rounded unsafe numeric id': (f) => { f.claim.plan_run_id = 9007199254740993n },
  'claim id has leading zero': (f) => { f.claim.plan_run_id = '0' + PLAN_RUN_ID },
  'claim id has trailing characters': (f) => { f.claim.plan_run_id += 'x' },
  'claim id is oversized': (f) => { f.claim.plan_run_id = '9'.repeat(30) },
  'claim id exceeds int64': (f) => { f.claim.plan_run_id = '10114896513660470' },
  'claim zero id': (f) => { f.claim.plan_run_id = '0' },
  'claim producing run is current apply': (f) => { f.claim.plan_run_id = CLAIM.apply_run_id },
  'claim pending Number instead of canonical string': (f) => { f.claim.expected_pending[0] = 1 },
  'claim pending duplicate': (f) => { f.claim.expected_pending.push('9007199254740993') },
  'claim pending out of order': (f) => { f.claim.expected_pending.reverse() },
  'claim source is not on main history': (f) => { f.ancestor = false },
  'claim invalid source SHA': (f) => { f.claim.source_sha = 'short' },
  'claim malformed digest': (f) => { f.claim.plan_manifest_sha256 = 'short' },
  'claim secret-like target': (f) => { f.claim.target.host = 'person-16ef0431@example.invalid' },
  'claim production-like target': (f) => { f.claim.target.database = 'production' },
  'claim pooled target': (f) => { f.claim.target.host = 'staging-pooler.invalid' },
  'claim unsafe evidence ref': (f) => { f.claim.acl_plan_ref = 'https://other' },
  'prior plan missing artifact': (f) => { f.planArtifacts = [] },
  'prior plan expired artifact': (f) => { f.planArtifacts[0].expired = true },
  'prior plan ambiguous artifact': (f) => { f.planArtifacts.push(f.planArtifacts[0]) },
  'prior plan artifact other head': (f) => { f.planArtifacts[0].workflow_run.head_sha = HEAD },
  'prior plan artifact stale attempt time': (f) => { f.planArtifacts[0].created_at = '2026-10-03T10:00:00Z' },
  'prior plan repository differs': (f) => { f.planRun.repository.full_name = 'Else/other' },
  'prior plan workflow differs': (f) => { f.planRun.path = '.github/workflows/other.yml' },
  'prior plan workflow id differs': (f) => { f.planRun.workflow_id = 100 },
  'prior plan branch differs': (f) => { f.planRun.head_branch = 'feature' },
  'prior plan event differs': (f) => { f.planRun.event = 'push' },
  'prior plan failed': (f) => { f.planRun.conclusion = 'failure' },
  'prior plan not finished': (f) => { f.planRun.status = 'in_progress' },
  'prior plan is apply-mode': (f) => { f.planJobs[1].conclusion = 'success' },
  'prior plan apply job absent': (f) => { f.planJobs.pop() },
  'prior plan job failed': (f) => { f.planJobs[0].conclusion = 'failure' },
  'prior plan job stale attempt': (f) => { f.planJobs[0].run_attempt = 2 },
  'manifest version differs': (f) => { f.manifest.runner_version = 2 },
  'manifest unknown field': (f) => { f.manifest.extra = 'ambiguous' },
  'manifest mode differs': (f) => { f.manifest.mode = 'apply' },
  'manifest source altered': (f) => { f.manifest.source_sha = 'd'.repeat(40) },
  'manifest target altered': (f) => { f.manifest.target.database = 'other_staging' },
  'manifest recovery ref altered': (f) => { f.manifest.recovery_evidence_ref = 'other' },
  'manifest ACL ref altered': (f) => { f.manifest.acl_plan_ref = 'other' },
  'manifest pending altered': (f) => { f.manifest.pending_before.pop() },
  'manifest SQL checksum altered consistently': (f) => { f.manifest.source_migrations[0].sha384 = f.manifest.source_migrations[0].sha384_recomputed = 'f'.repeat(96) },
  'manifest SQL checksum recomputation differs': (f) => { f.manifest.source_migrations[0].sha384_recomputed = 'f'.repeat(96) },
  'manifest source order altered': (f) => { f.manifest.source_migrations.reverse() },
  'manifest duplicate source version': (f) => { f.manifest.source_migrations.push(f.manifest.source_migrations[1]) },
  'manifest ambiguous description': (f) => { f.manifest.source_migrations[0].description = 'first\n2:other' },
  'manifest string instead of Rust numeric version': (f) => { f.manifest.pending_before[1] = '9007199254740993' },
  'manifest rounded i64': (f) => { f.manifest.pending_before[1] = 9007199254740992 },
  'manifest digest absent': (f) => { delete f.manifest.plan_manifest_sha256 },
  'manifest embedded digest alone is a lie': (f) => { f.manifest.plan_manifest_sha256 = HASH; f.manifest.source_migrations[0].description = 'altered' },
  'manifest not read-only': (f) => { f.manifest.role = 'two_bot_migrator' },
  'manifest role verification missing': (f) => { f.manifest.role_verified_connections = 0 },
  'manifest says DDL applied': (f) => { f.manifest.applied_count = 1 },
  'manifest ledger does not reconcile': (f) => { f.manifest.ledger_before = f.manifest.ledger_after = [{ ...f.manifest.source_migrations[0], success: true }] },
  'unsafe initial artifact URL': (f) => { f.applyArtifacts[0].archive_download_url = 'https://other.invalid/zip' },
  'duplicate claim field': (f) => { f.claimText = CLAIM_TEXT.replace('"schema_version": 1,', '"schema_version": 2, "schema_version": 1,') },
  'duplicate escaped manifest field': (f) => { f.planText = PLAN_TEXT.replace('"mode": "plan",', '"mode": "apply", "mo\\u0064e": "plan",') },
}
for (const [name, mutate] of Object.entries(negatives)) {
  test('refuses ' + name, async () => {
    const f = contractFixture()
    await mutate(f)
    await assert.rejects(establish(f))
  })
}

for (const descriptorSignature of [true, false]) {
  test(`accepts bounded stored ZIP descriptor signature=${descriptorSignature}`, () => {
    assert.equal(extractManifest(zipOf(PLAN_TEXT, { descriptor: true, descriptorSignature })).manifest.pending_before[1], 9007199254740993n)
  })
}
test('stored ZIP without descriptor is valid too', () => assert.equal(extractManifest(zipOf(PLAN_TEXT)).manifest.plan_manifest_sha256, HASH))
test('two files are ambiguous', () => assert.throws(() => extractManifest(zipOfTexts([PLAN_TEXT, PLAN_TEXT]))))
for (const [name, mutate] of Object.entries({
  'traversal filename': (b, v, c) => { b[30] = b[c + 46] = 47 },
  'encrypted entry': (b, v, c) => { v.setUint16(c + 8, 1, true) },
  'compressed entry': (b, v, c) => { v.setUint16(c + 10, 8, true) },
  'multi-disk': (b, v) => { v.setUint16(b.length - 18, 1, true) },
  'symlink': (b, v, c) => { v.setUint32(c + 38, 0xa1ff0000, true) },
  'local header mismatch': (b, v) => { v.setUint16(6, 0, true) },
  'descriptor mismatch': (b, v, c) => { v.setUint32(c - 4, 123, true) },
  'CRC corruption': (b) => { b[90] ^= 1 },
  'out-of-range data': (b, v, c) => { v.setUint32(c + 20, 1000000, true) },
  'out-of-range central': (b, v) => { v.setUint32(b.length - 6, b.length + 100, true) },
})) {
  test('refuses ZIP ' + name, () => {
    const b = zipOf(PLAN_TEXT, { descriptor: true })
    const v = new DataView(b.buffer)
    const central = v.getUint32(b.length - 6, true)
    mutate(b, v, central)
    assert.throws(() => extractManifest(b))
  })
}
test('JSON rejects duplicate, malformed, invalid UTF16 projection and out-of-range integers', async () => {
  for (const text of ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '[1,]', '01', '{"x":true}garbage', '1.0', '1e0']) assert.throws(() => parseDocument(text))
  for (const id of [0, 9007199254740992, '01', '1e3', '1.0', '1suffix', '9'.repeat(20)]) assert.throws(() => runId(id))
  for (const version of ['-0', '+1', '01', '10114896513660470']) assert.throws(() => versionString(version, true))
  const f = contractFixture(); f.manifest.source_migrations[0].description = '\ud800'
  await assert.rejects(projectionHash(f.manifest))
})
test('foreign repository never reaches transport', async () => {
  const f = contractFixture()
  await assert.rejects(readApplyClaim({ github: f.github, repository: 'Else/other', applyRunId: RUN_ID }))
  assert.equal(f.requests.length, 0)
})
test('API fails closed on unreadable run, incomplete list or truncated JSON', async () => {
  for (const response of [() => new Response('{}', { status: 500 }), () => new Response('{'), () => new Response(wire({ total_count: 101, artifacts: [] }))]) {
    const github = createGithubReader({ fetchImpl: async () => response(), token: 'offline-stub', allowedRepositories: [REPO] })
    await assert.rejects(github.listArtifacts(REPO, PLAN_RUN_ID))
  }
})
test('artifact byte budget enforced while streaming, before full body is read', async () => {
  let cancelled = false
  const github = createGithubReader({ token: 'offline-stub', allowedRepositories: [REPO], fetchImpl: async () => new Response(new ReadableStream({
    pull(controller) { controller.enqueue(new Uint8Array(1024)) }, cancel() { cancelled = true },
  })) })
  await assert.rejects(github.downloadBytes(`https://api.github.com/repos/${REPO}/actions/artifacts/32/zip`, 16))
  assert.equal(cancelled, true)
})
test('body timeout bounds a stalled artifact stream', async () => {
  const github = createGithubReader({ token: 'offline-stub', allowedRepositories: [REPO], timeoutMs: 10,
    fetchImpl: async () => new Response(new ReadableStream({ start() {} })) })
  await assert.rejects(github.downloadBytes(`https://api.github.com/repos/${REPO}/actions/artifacts/32/zip`, 16))
})
