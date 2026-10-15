// Mutate isolated package copies only; structural contracts do not claim live
// systemd process-tree behavior. That is an authorized operator acceptance gate.
import assert from 'node:assert/strict'
import { cp, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
const source = dirname(dirname(fileURLToPath(import.meta.url)))
const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'package-mutants-'))
const mutants = [
  { name: 'complete control-group cleanup', file: 'systemd/gh-product-bridge.service', from: 'KillMode=control-group', to: 'KillMode=process',
    test: 'oneshot services bound start plus stop and kill the complete control group' },
  { name: 'oneshot outer deadline', file: 'systemd/gh-product-bridge.service', from: 'TimeoutStartSec=50s', to: 'RuntimeMaxSec=50s',
    test: 'oneshot services bound start plus stop and kill the complete control group' },
  { name: 'independent health timer', file: 'systemd/gh-product-bridge-health.timer', from: '[Unit]', to: '[Unit]\nRequires=gh-product-bridge.timer',
    test: 'main timer is 60 seconds and health timer is independent, including main never firing' },
  { name: 'exclusive manifest creation', file: 'scripts/prepare-runtime.mjs', from: "open(output, 'wx', 0o600)", to: "open(output, 'w', 0o600)",
    test: 'offline manifest preparation uses private inputs and refuses to overwrite existing state' },
  { name: 'private authorized wrapper', file: 'src/runtime-github.js', from: '(wrapper.mode & 0o077)', to: 'false',
    test: 'host preflight refuses missing, public or symlinked gh wrapper without running it' },
  { name: 'check-in single gated attempt', file: 'systemd/gh-product-bridge-checkin.timer', from: 'OnCalendar=*-*-* *:*:00', to: 'OnCalendar=*-*-* *:*:00,30',
    test: 'check-in pair is a 60s timer-driven single gated attempt, installable beside the four base units' },
]
async function run(name, mutant) {
  const directory = join(root, name)
  for (const part of ['src', 'scripts', 'templates', 'systemd', 'test']) await cp(join(source, part), join(directory, part), { recursive: true })
  await writeFile(join(directory, 'package.json'), '{"type":"module"}\n')
  if (mutant) {
    const path = join(directory, mutant.file), text = await readFile(path, 'utf8')
    assert.ok(text.includes(mutant.from), `${name}: mutation not applied`)
    await writeFile(path, text.replaceAll(mutant.from, mutant.to))
  }
  const result = spawnSync(process.execPath, ['--test', '--test-reporter=tap', 'test/runtime-package.test.mjs'],
    { cwd: directory, encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 })
  assert.equal(result.error, undefined, `${name}: test process did not complete`)
  assert.equal(result.signal, null, `${name}: test process was killed`)
  if (mutant) {
    assert.equal(result.status, 1, `${name}: mutant survived or crashed outside tests\n${result.stdout}\n${result.stderr}`)
    assert.ok(result.stdout.split('\n').some(line => line.startsWith('not ok ') && line.endsWith(` - ${mutant.test}`)),
      `${name}: expected behavioral regression did not fail\n${result.stdout}\n${result.stderr}`)
    console.log(`KILLED ${mutant.name}: ${mutant.test}`)
  } else {
    assert.equal(result.status, 0, `${name}: positive control failed\n${result.stdout}\n${result.stderr}`)
    assert.match(result.stdout, /# tests 8\n/)
    assert.match(result.stdout, /# pass 8\n/)
    console.log(`PASS ${name}: 8/8 controls`)
  }
}
try {
  await run('control-before')
  for (let i = 0; i < mutants.length; i++) await run(`mutant-${i}`, mutants[i])
  await run('control-after')
} finally { await rm(root, { recursive: true, force: true }) }
