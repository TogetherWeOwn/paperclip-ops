import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdtemp, mkdir, writeFile, chmod, rm, symlink } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import { prepareRuntime } from '../scripts/prepare-runtime.mjs'
import { preflightHost } from '../scripts/preflight-host.mjs'
import { loadRuntimeManifest, sweepBacklog } from '../src/runtime-cli.js'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const units = ['gh-product-bridge.service', 'gh-product-bridge.timer', 'gh-product-bridge-health.service', 'gh-product-bridge-health.timer']
async function unit(name) {
  const sections = {}
  let section
  for (const row of (await readFile(join(root, 'systemd', name), 'utf8')).split('\n')) {
    const line = row.trim()
    if (!line || line.startsWith('#')) continue
    if (line.startsWith('[')) { section = line.slice(1, -1); sections[section] = {}; continue }
    const index = line.indexOf('=')
    assert.ok(index > 0 && section)
    const key = line.slice(0, index)
    assert.equal(sections[section][key], undefined, 'no shadowing directives')
    sections[section][key] = line.slice(index + 1)
  }
  return sections
}

test('oneshot services bound start plus stop and kill the complete control group', async () => {
  for (const [name, deadline, command] of [[units[0], 55, 'run'], [units[2], 15, 'watch']]) {
    const { Service: s } = await unit(name)
    assert.equal(s.Type, 'oneshot')
    assert.equal(s.TimeoutStartFailureMode, 'terminate')
    assert.equal(parseInt(s.TimeoutStartSec) + parseInt(s.TimeoutStopSec), deadline)
    assert.equal(s.RuntimeMaxSec, undefined, 'RuntimeMaxSec does not bound a oneshot')
    assert.equal(s.KillMode, 'control-group')
    assert.equal(s.SendSIGKILL, 'yes')
    assert.equal(s.FinalKillSignal, 'SIGKILL')
    assert.equal(s.KillSignal, 'SIGTERM')
    assert.equal(s.Restart, 'no')
    assert.equal(s.UMask, '0077')
    assert.equal(s.ExecStart, `/usr/bin/node %h/.local/lib/gh-product-bridge/current/gh-event-capture/src/runtime-cli.js ${command} %h/secure-drop/gh-product-bridge/runtime.json`)
    assert.ok(s.UnsetEnvironment.includes('PAPERCLIP_API_KEY'))
    assert.ok(s.UnsetEnvironment.includes('NODE_OPTIONS'))
    assert.equal(s.Environment, '"PATH=%h/secure-drop/gh-product-bridge/bin:/usr/local/bin:/usr/bin:/bin"')
  }
})

test('main timer is 60 seconds and health timer is independent, including main never firing', async () => {
  const main = await unit(units[1])
  const health = await unit(units[3])
  assert.equal(main.Timer.OnCalendar, '*-*-* *:*:00')
  assert.equal(health.Timer.OnCalendar, '*-*-* *:*:00,30')
  assert.equal(main.Timer.Unit, units[0])
  assert.equal(health.Timer.Unit, units[2])
  for (const name of units) assert.deepEqual(Object.keys((await unit(name)).Unit), ['Description'], 'no dependency on the main timer/service')
  for (const timer of [main, health]) {
    assert.equal(timer.Timer.AccuracySec, '1s')
    assert.equal(timer.Timer.RandomizedDelaySec, '0')
    assert.equal(timer.Timer.Persistent, 'false')
    assert.equal(timer.Install.WantedBy, 'timers.target')
  }
})

test('check-in pair is a 60s timer-driven single gated attempt, installable beside the four base units', async () => {
  const service = await unit('gh-product-bridge-checkin.service')
  const timer = await unit('gh-product-bridge-checkin.timer')
  assert.equal(timer.Timer.OnCalendar, '*-*-* *:*:00')
  assert.equal(timer.Timer.Unit, 'gh-product-bridge-checkin.service')
  assert.equal(timer.Timer.AccuracySec, '1s')
  assert.equal(timer.Timer.RandomizedDelaySec, '0')
  assert.equal(timer.Timer.Persistent, 'false')
  assert.equal(timer.Install.WantedBy, 'timers.target')
  assert.deepEqual(Object.keys(timer.Unit), ['Description'], 'no dependency on the main timer/service')
  assert.equal(service.Service.Type, 'oneshot')
  assert.equal(service.Service.Restart, 'no')
  assert.equal(service.Service.ExecStart,
    '/usr/bin/node %h/.local/lib/gh-product-bridge/current/gh-event-capture/scripts/healthchecks-checkin.mjs %h/secure-drop/gh-product-bridge/runtime.json')
  assert.ok(service.Service.UnsetEnvironment.includes('PAPERCLIP_API_KEY'))
  assert.ok(service.Service.UnsetEnvironment.includes('GH_TOKEN'))
  assert.ok(!service.Service.ExecStart.includes('healthchecks-ping.key'),
    'ping file resolves sibling-side, never on argv')
})

test('the retired host-PEM readiness path stays absent (one credential route only)', async () => {
  // The sweep moved to the Actions job-token route when the host-PEM path
  // was retired (staged but never enabled): its systemd pair, PEM-mint
  // wrapper, and binding doc were deleted, not left beside this route. If
  // any of these files reappear, the repo holds two credential routes
  // again — fail loudly instead of shipping that.
  for (const rel of ['systemd/readiness-sweep.service', 'systemd/readiness-sweep.timer',
    'scripts/gh-readiness-wrapper.mjs', 'UNATTENDED-GH-READINESS.md']) {
    await assert.rejects(readFile(join(root, rel), 'utf8'), { code: 'ENOENT' }, `${rel} stays deleted`)
  }
})

async function fixture(t) {
  const home = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'runtime-package-'))
  t.after(() => rm(home, { recursive: true, force: true }))
  const secure = join(home, 'secure-drop', 'gh-product-bridge')
  await mkdir(join(secure, 'bin'), { recursive: true, mode: 0o700 })
  const config = JSON.parse(await readFile(join(root, 'templates', 'consumer.json'), 'utf8'))
  Object.assign(config, { captureOrigin: 'https://capture.test', boardOrigin: 'https://board.test',
    companyId: '00000000-0000-4000-8000-000000000004', allowedRepositories: ['TogetherWeOwn/kofra'],
    stateDirectory: join(home, '.local', 'state', 'gh-product-bridge'),
    captureTokenFile: join(secure, 'capture.key'), boardTokenFile: join(secure, 'board.key') })
  await writeFile(config.captureTokenFile, 'test-capture-secret', { mode: 0o600 })
  await writeFile(config.boardTokenFile, 'test-board-secret', { mode: 0o600 })
  const configFile = join(secure, 'consumer.json'), hook = join(secure, 'alert-hook'), output = join(secure, 'runtime.json')
  await writeFile(configFile, JSON.stringify(config), { mode: 0o600 })
  // These fixtures fail if executed: preflight must only inspect, never probe.
  await writeFile(hook, '#!/bin/sh\nexit 77\n', { mode: 0o700 })
  await writeFile(join(secure, 'bin', 'gh'), '#!/bin/sh\nexit 77\n', { mode: 0o700 })
  const args = [configFile, hook, '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z', output]
  return { home, secure, config, configFile, hook, output, args, now: Date.parse('2026-09-02T00:00:00Z') }
}

test('offline manifest preparation uses private inputs and refuses to overwrite existing state', async (t) => {
  const h = await fixture(t)
  await prepareRuntime(h.args, h.now)
  const manifest = await loadRuntimeManifest(h.output)
  assert.match(manifest.namespace, /^[a-f0-9]{64}$/)
  assert.equal(manifest.stateDirectory, h.config.stateDirectory)
  const before = await readFile(h.output, 'utf8')
  assert.equal(before.includes('test-board-secret'), false)
  assert.equal(before.includes('test-capture-secret'), false)
  await assert.rejects(prepareRuntime(h.args, h.now), { code: 'EEXIST' })
  assert.equal(await readFile(h.output, 'utf8'), before)
  await preflightHost(h.home, h.now)
})

test('manifest preparation rejects expired, overlong, public and JWT credentials without producing a manifest', async (t) => {
  const h = await fixture(t)
  await assert.rejects(prepareRuntime(h.args, Date.parse('2026-10-02T00:00:00Z')))
  const tooLong = [...h.args]; tooLong[3] = '2026-10-02T00:00:00Z'
  await assert.rejects(prepareRuntime(tooLong, h.now))
  await chmod(h.configFile, 0o644)
  await assert.rejects(prepareRuntime(h.args, h.now))
  await chmod(h.configFile, 0o600)
  await writeFile(h.config.boardTokenFile, 'eyJ.run.jwt')
  await assert.rejects(prepareRuntime(h.args, h.now))
  await assert.rejects(readFile(h.output), { code: 'ENOENT' })
})

test('host preflight refuses missing, public or symlinked gh wrapper without running it', async (t) => {
  const h = await fixture(t)
  await prepareRuntime(h.args, h.now)
  const wrapper = join(h.secure, 'bin', 'gh')
  await chmod(wrapper, 0o755)
  await assert.rejects(preflightHost(h.home, h.now))
  await rm(wrapper)
  await assert.rejects(preflightHost(h.home, h.now))
  await symlink(h.hook, wrapper)
  await assert.rejects(preflightHost(h.home, h.now))
})

test('service backlog output preserves unknown ages and reports pending and poisoned work without raw data', () => {
  assert.deepEqual(sweepBacklog(null), { pendingScans: null, poisoned: null, oldestPendingSnapshotAgeMs: null })
  const sweeps = { pendingScans: 0, poisonedTotal: 1, repos: { 'private-repo-name': {
    backfill: { remaining: 2, poisoned: 0, listedAgeMs: 9000 },
    review: { remaining: 0, poisoned: 1, listedAgeMs: 15000 }
  } }, failures: [{ message: 'PRIVATE_PROVIDER_ERROR' }] }
  assert.deepEqual(sweepBacklog(sweeps), { pendingScans: 0, poisoned: 1, oldestPendingSnapshotAgeMs: 15000 })
  assert.equal(sweepBacklog({ ...sweeps, pendingScans: 1 }).oldestPendingSnapshotAgeMs, null)
  sweeps.repos['private-repo-name'].review.listedAgeMs = -1
  assert.equal(sweepBacklog(sweeps).oldestPendingSnapshotAgeMs, null)
  assert.equal(JSON.stringify(sweepBacklog(sweeps)).includes('PRIVATE'), false)
})

test('shipped consumer template is explicitly product-only and cannot accidentally provision a live scope', async () => {
  const template = JSON.parse(await readFile(join(root, 'templates', 'consumer.json'), 'utf8'))
  assert.equal(template.mode, 'products-only-v1')
  assert.equal(template.limits.durationMs, 45000)
  assert.equal(template.companyId, 'REPLACE_COMPANY_UUID')
  assert.equal(template.captureTokenFile.endsWith('/secure-drop/gh-product-bridge/capture.key'), true)
  assert.equal(template.boardTokenFile.endsWith('/secure-drop/gh-product-bridge/board.key'), true)
})
