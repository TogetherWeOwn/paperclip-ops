// Run by hand before push. Never edits the checkout: every mutant gets its
// own run-owned scratch copy, a green baseline and a parse check before tests.
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const source = path.resolve(__dirname, '..')
if (!process.env.PAPERCLIP_SCRATCH_DIR) throw Error('run-owned scratch is required')
const scratch = fs.mkdtempSync(path.join(process.env.PAPERCLIP_SCRATCH_DIR, 'claim-mutants-'))
const files = ['test/evidence.test.mjs', 'test/app.test.mjs', 'test/decide.test.mjs', 'test/source.test.mjs', 'test/environments.test.mjs']
const env = { PATH: process.env.PATH }
function run(cwd, args) { return spawnSync(process.execPath, args, { cwd, env, encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024 }) }
const baseline = run(source, ['--test', '--test-reporter=tap', ...files])
if (baseline.status !== 0) throw Error('baseline is not green: ' + baseline.stdout + baseline.stderr)
const mutants = [
  ['artifact-name', 'src/evidence.js', "export const PLAN_ARTIFACT_NAME = 'staging-migrate-manifest'", "export const PLAN_ARTIFACT_NAME = 'staging-migrate-manifest.json'"],
  ['whole-json-hash', 'src/evidence.js', 'const computed = await projectionHash(document)', 'const computed = await sha256Hex(new TextEncoder().encode(JSON.stringify(document, (_, v) => typeof v === "bigint" ? Number(v) : v)))'],
  ['prefix-run-id', 'src/evidence.js', 'planRunId: claim.plan_run_id,', 'planRunId: claim.plan_run_id.slice(0, 10),'],
  ['plan-provenance', 'src/evidence.js', 'validateRun(run, repository, claim.planRunId)', 'void run'],
  ['embedded-digest-only', 'src/evidence.js', 'computed === document.plan_manifest_sha256 && computed === claim.planManifestSha256', 'document.plan_manifest_sha256 === claim.planManifestSha256'],
  ['target-unbound', 'src/evidence.js', 'document.target.host === claim.target.host && document.target.database === claim.target.database', 'true'],
  ['failed-claim-job', 'src/evidence.js', "job.status === 'completed' && job.conclusion === conclusion", 'true'],
  ['legacy-descriptor-refusal', 'src/archive.js', 'const descriptor = (flags & 8) !== 0', 'requireValue((flags & 8) === 0, "legacy descriptor refusal"); const descriptor = false'],
  ['no-ceo-go', 'src/decide.js', '!Array.isArray(goHashes) || !goHashes.includes(claim.planManifestSha256)', 'false'],
  ['rerun-inherits-claim', 'src/app.js', 'if (claim !== null && plan !== null) await confirmApplyClaim({ github, repository, claim, environment })', 'void claim'],
  // Source evidence (case_b995c00c): CI and independent review on every trusted commit.
  ['ci-app-unpinned', 'src/source.js', 'run.name === name && run.appId === ci.appId', 'run.name === name'],
  ['ci-conclusion-ignored', 'src/source.js', "run.headSha === sha && run.status === 'completed' && run.conclusion === 'success'),", 'run.headSha === sha),'],
  ['ci-head-unbound', 'src/source.js', 'found.every((run) => run.headSha === sha && ', 'found.every((run) => '],
  ['ci-absence-allowed', 'src/source.js', 'found.length >= 1, `required check is absent', 'found.length >= 0, `required check is absent'],
  ['review-app-unpinned', 'src/source.js', 'run.name === review.check && run.appId === review.appId', 'run.name === review.check'],
  ['review-name-prefix', 'src/source.js', 'run.name === review.check && ', 'run.name.startsWith(review.check) && '],
  ['review-ambiguity-allowed', 'src/source.js', 'found.length === 1, `review is absent or ambiguous', 'found.length >= 1, `review is absent or ambiguous'],
  ['review-head-unbound', 'src/source.js', "requireValue(run.headSha === pull.headSha, 'review is bound to another head')", 'void run'],
  ['review-pull-binding-ignored', 'src/source.js', "typeof run.externalId === 'string' && run.externalId.endsWith(`:${pull.number}:${pull.headSha}`), 'review is recorded against another pull request'", "true, 'review is recorded against another pull request'"],
  ['review-conclusion-ignored', 'src/source.js', "run.status === 'completed' && run.conclusion === 'success', 'review is not successful'", "true, 'review is not successful'"],
  ['review-score-ignored', 'src/source.js', 'requireValue(run.title === review.score,', 'requireValue(true,'],
  ['review-after-merge-allowed', 'src/source.js', "requireValue(isoMs(run.completedAt) <= mergedAt, 'review finished after the merge')", 'void mergedAt'],
  ['pull-merge-commit-unbound', 'src/source.js', 'pulls.filter((pull) => pull.mergeCommitSha === sha)', 'pulls'],
  ['pull-ambiguity-allowed', 'src/source.js', 'merged.length === 1,', 'merged.length >= 1,'],
  ['pull-base-branch-ignored', 'src/source.js', "pull.baseRef === 'main' && ", ''],
  ['pull-base-repository-ignored', 'src/source.js', 'pull.baseRepository === repository && ', ''],
  ['pull-state-ignored', 'src/source.js', "pull.state === 'closed' && ", ''],
  ['pull-page-truncation-allowed', 'src/github.js', 'data.length < 100, \'GitHub pull-request list', 'data.length <= 100, \'GitHub pull-request list'],
  ['check-run-filter-dropped', 'src/github.js', 'check-runs?filter=latest&per_page=100', 'check-runs?per_page=100'],
  ['workflow-and-plan-heads-unchecked', 'src/app.js', 'shas: [claim.sourceSha, claim.workflowHeadSha, plan.headSha],', 'shas: [claim.sourceSha],'],
  ['source-evidence-optional', 'src/decide.js', "if (!isSourceEvidence(source)) return { state: 'rejected', reason: REASON.SOURCE_EVIDENCE_MISSING }", 'if (!isSourceEvidence(source)) source = { ok: true, shas: [claim.sourceSha, claim.workflowHeadSha, plan.headSha] }'],
  ['source-failure-ignored', 'src/decide.js', "if (source.ok === false) return { state: 'rejected', reason: source.reason }", 'if (source.ok === false) source.shas = [claim.sourceSha, claim.workflowHeadSha, plan.headSha]'],
  ['source-coverage-ignored', 'src/decide.js', '![claim.sourceSha, claim.workflowHeadSha, plan.headSha].every((entry) => source.shas.includes(entry))', 'false'],
  ['policy-evidence-optional', 'src/environments.js', "if (!block || typeof block !== 'object' || Array.isArray(block)) invalid('protected environment source evidence is missing')", 'if (!block) return'],
]
for (const [name, file, before, after] of mutants) {
  const copy = path.join(scratch, name)
  fs.cpSync(source, copy, { recursive: true })
  const target = path.join(copy, file)
  const text = fs.readFileSync(target, 'utf8')
  if (text.split(before).length !== 2) throw Error('mutation anchor is not unique: ' + name)
  fs.writeFileSync(target, text.replace(before, after))
  if (run(copy, ['--check', file]).status !== 0) throw Error('mutant does not parse: ' + name)
  const result = run(copy, ['--test', '--test-reporter=tap', ...files])
  fs.writeFileSync(path.join(copy, 'proof.tap'), result.stdout + result.stderr)
  if (result.error || result.signal || result.status !== 1 || !/^not ok \d+/m.test(result.stdout)) throw Error('mutant survived or suite did not run: ' + name)
  console.log('KILLED ' + name)
}
console.log('All ' + mutants.length + ' hand-run mutants red; baseline green. Proofs: ' + scratch)
