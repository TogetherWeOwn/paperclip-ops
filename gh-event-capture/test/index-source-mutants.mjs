// Run each hand mutant in a throwaway copy, never in the author's worktree.
import { cp, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { assertionKilled } from './index-mutant-reporter.mjs'

const root = fileURLToPath(new URL('../', import.meta.url))
const mutants = [
  ['warm-up gate removed', 'const index = warm ? buildPrTaskIndex', 'const index = state ? buildPrTaskIndex',
    'warm-up gate skips partial index'],
  ['URL derivation accepts a non-allowed repo',
    "if (!key || !allowed.has(key.slice(0, key.lastIndexOf('#'))))",
    'if (!key)', 'URL derivation refuses non-allowed'],
  ['closed issue retained', 'const entries = openIssues.map((issue) => {',
    `const entries = [...openIssues, ...(state?.entries ?? []).filter((e) =>
      !openIssues.some((i) => i.id === e.issueId)).map((e) => ({ id: e.issueId,
        identifier: e.identifier, status: e.status }))].map((issue) => {`,
    'an issue leaving the open set is dropped'],
  ['read cap ignored', 'due.slice(0, indexReadsPerPass)', 'due', 'warm-up gate skips partial index'],
  ['short issue page mistaken for EOF', 'if (page.length === 0) break', 'if (page.length < OPEN_ISSUE_LIST_PAGE_SIZE) break',
    'short identity pages cannot hide a conflicting claimant', 'src/paperclip-adapter.js'],
  ['cache key budget removed', 'requireValue(bytes <= maxKeyBytes)', '',
    'key capacity is enforced before serialization'],
  ['small cache allocates maximum buffer', 'Buffer.alloc(info.size + 1)', 'Buffer.alloc(MAX_BYTES + 1)',
    'cache reads allocate only file size'],
  ['over-cap membership serves a warm subset', '} catch { state = null; counters.indexRefreshErrors++ }',
    '} catch { counters.indexRefreshErrors++ }', 'key capacity is enforced before serialization'],
  ['plugin operation scope omitted', 'includePluginOperations=true', 'includePluginOperations=false',
    'identity pagination includes the 1001st claimant', 'src/paperclip-adapter.js'],
  ['index deadline isolation removed', 'Math.floor(limits.durationMs / 4)', 'limits.durationMs',
    'slow index refresh reserves time', 'src/consumer-runner.js'],
  ['index fetch bypasses its abort budget', 'fetchImpl: indexBudget.fetch(transport)', 'fetchImpl: transport',
    'in-flight index deadline aborts only', 'src/consumer-runner.js'],
  ['index filesystem stall blocks production', 'shadowIndex = await beforeIndexDeadline(indexTask, indexBudget.signal)',
    'shadowIndex = await indexTask', 'cache save deadline yields production and drains before unlock',
    'src/consumer-runner.js', 'test/index-io-deadline.test.mjs'],
  ['index filesystem task survives unlock', 'if (indexTask !== null) await indexTask',
    'if (indexTask !== null) void indexTask', 'cache load deadline yields production and drains before unlock',
    'src/consumer-runner.js', 'test/index-io-deadline.test.mjs'],
  ['structured assertion code ignored', "failure.code === 'ERR_ASSERTION'", 'true',
    'mutant classifier uses the selected structured assertion code', 'test/index-mutant-reporter.mjs',
    'test/index-mutant-reporter.test.mjs'],
  ['unexpired failure cooldown bypassed', 'entries.filter((e) => (retryAfter[e.issueId] ?? 0) <= startedAt)', '[...entries]',
    'a failed work-product read retains'],
  ['retry cooldown uses pass-start time', 'retryAfter[e.issueId] = currentTime() + RETRY_MS',
    'retryAfter[e.issueId] = startedAt + RETRY_MS', 'failed read cooldown starts at failure time'],
  ['cache time regresses within a pass',
    'lastTime = Math.max(lastTime, nowMs === undefined ? Date.now() : startedAt)',
    'lastTime = Math.max(startedAt, nowMs === undefined ? Date.now() : startedAt)',
    'failed read cooldown survives rollback before save'],
  ['far-future cache pins retry eligibility',
    'if (state && futureSkew > MAX_FUTURE_SKEW_MS) state = null',
    'if (false) state = null', 'far-future valid cache does not pin retry eligibility'],
  ['expired retry forgets attempt ordering', 'Math.max(e.readAt ?? -1, (retryAfter[e.issueId] ?? 0) - RETRY_MS)', 'e.readAt ?? -1',
    'failed oldest cannot starve'],
  ['noncanonical external URL hides a conflict', 'external ?? keyFrom(row.externalId, URL)', 'external',
    'recognizable external URL conflicts cannot be bypassed'],
]
let failures = 0
for (const [name, before, after, testName, sourceFile = 'src/pr-task-index-source.js',
  testFile = 'test/pr-task-index-source.test.mjs'] of mutants) {
  const directory = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || process.env.PAPERCLIP_SCRATCH_DIR || tmpdir(), 'index-mutant-'))
  try {
    await cp(join(root, 'src'), join(directory, 'src'), { recursive: true })
    await cp(join(root, 'test'), join(directory, 'test'), { recursive: true })
    await writeFile(join(directory, 'package.json'), '{"type":"module"}\n')
    const path = join(directory, sourceFile)
    const source = await readFile(path, 'utf8')
    if (source.split(before).length !== 2) throw new Error(`mutant anchor changed: ${name}`)
    await writeFile(path, source.replace(before, after))
    const result = spawnSync(process.execPath, ['--test',
      `--test-reporter=${join(directory, 'test/index-mutant-reporter.mjs')}`,
      `--test-name-pattern=${testName}`, join(directory, testFile)],
    { encoding: 'utf8', timeout: 30000 })
    const killed = assertionKilled(result, testName)
    console.log(`${killed ? 'KILLED' : 'SURVIVED/UNVERIFIED'}: ${name}`)
    if (!killed) { failures++; console.log(result.stdout, result.stderr) }
  } finally { await rm(directory, { recursive: true, force: true }) }
}
if (failures) process.exitCode = 1
