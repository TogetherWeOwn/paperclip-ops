// Systemd unit gate: static contracts first, systemd-analyze second.
//
// Exit 0 PASS, exit 1 FAIL (contract violation or analyze rejection),
// exit 2 UNAVAILABLE (units unreadable, or systemd-analyze missing —
// static contracts are then the only evidence; never PASS).
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const units = ['gh-product-bridge.service', 'gh-product-bridge.timer', 'gh-product-bridge-health.service', 'gh-product-bridge-health.timer', 'gh-product-bridge-checkin.service', 'gh-product-bridge-checkin.timer', 'readiness-sweep.service', 'readiness-sweep.timer']

function parseUnit(name) {
  const sections = {}
  let section = null
  for (const row of readFileSync(join(root, 'systemd', name), 'utf8').split('\n')) {
    const line = row.trim()
    if (!line || line.startsWith('#')) continue
    if (line.startsWith('[')) { section = line.slice(1, -1); sections[section] = {}; continue }
    const index = line.indexOf('=')
    if (index <= 0 || !section) throw new Error(`${name}: unparsable line: ${row}`)
    const key = line.slice(0, index)
    if (sections[section][key] !== undefined) throw new Error(`${name}: shadowed directive: ${key}`)
    sections[section][key] = line.slice(index + 1)
  }
  return sections
}

function seconds(value, name, key) {
  const match = /^(\d+)(s|min)$/.exec(value)
  if (!match) throw new Error(`${name}: ${key} is not an explicit duration: ${value}`)
  return parseInt(match[1]) * (match[2] === 'min' ? 60 : 1)
}

// Static contract for the readiness pair (case_4b213922 pre-enablement item 2):
// the 5-minute pass must finish before the next tick even at the latest
// allowed start delay, run with the minimal credential posture, and bind the
// timer to exactly this service. Mirrors the runtime-package.test.mjs
// contract; both must agree.
function readinessContracts() {
  const failures = []
  const check = (ok, message) => { if (!ok) failures.push(message) }
  let service
  let timer
  try {
    service = parseUnit('readiness-sweep.service')
    timer = parseUnit('readiness-sweep.timer')
  } catch (error) {
    return [`readiness units are unreadable: ${error.message}`]
  }
  const s = service.Service ?? {}
  const t = timer.Timer ?? {}
  check(timer.Unit && Object.keys(timer.Unit).length === 1 && timer.Unit.Description,
    'readiness-sweep.timer [Unit] carries Description only (no dependency on other units)')
  check(t.OnCalendar === '*-*-* *:0/5:00', `readiness-sweep.timer fires every 5 minutes (got ${t.OnCalendar})`)
  check(t.Unit === 'readiness-sweep.service', 'readiness-sweep.timer binds readiness-sweep.service')
  check(t.Persistent === 'false', 'readiness-sweep.timer is not persistent (no catch-up storms)')
  check(timer.Install?.WantedBy === 'timers.target', 'readiness-sweep.timer is wanted by timers.target')
  try {
    const deadline = seconds(s.TimeoutStartSec, 'readiness-sweep.service', 'TimeoutStartSec') +
      seconds(s.TimeoutStopSec, 'readiness-sweep.service', 'TimeoutStopSec')
    const maxDelay = seconds(t.AccuracySec, 'readiness-sweep.timer', 'AccuracySec') +
      seconds(t.RandomizedDelaySec, 'readiness-sweep.timer', 'RandomizedDelaySec')
    check(deadline + maxDelay < 300, `deadline ${deadline}s + max delay ${maxDelay}s must fit the 300s period`)
  } catch (error) {
    failures.push(error.message)
  }
  check(s.Type === 'oneshot', 'readiness-sweep.service is oneshot (one supervised pass per tick)')
  check(s.Restart === 'no', 'readiness-sweep.service never restarts itself')
  check(s.UMask === '0077', 'readiness-sweep.service masks group/other file bits')
  check(s.NoNewPrivileges === 'yes', 'readiness-sweep.service cannot gain privileges')
  check(s.TimeoutStartFailureMode === 'terminate', 'readiness-sweep.service terminates on start timeout')
  check(s.KillMode === 'control-group', 'readiness-sweep.service kills the complete control group')
  check(s.ExecStart === '/usr/bin/node %h/.local/lib/readiness-sweep/current/gh-event-capture/scripts/readiness-sweep.mjs %h/secure-drop/readiness-sweep/policy.json',
    'readiness-sweep.service ExecStart is the exact pinned entrypoint plus the 0600 policy path')
  check(!String(s.ExecStart).includes('GH_TOKEN'), 'readiness-sweep.service ExecStart carries no token material')
  const argvHead = String(s.ExecStart).split('readiness-sweep.mjs')[0].trim().split(/\s+/)
  check(!argvHead.some((part) => part === 'gh' || part.endsWith('/gh')),
    'readiness-sweep.service argv carries no bare gh before the script')
  for (const name of ['PAPERCLIP_API_KEY', 'PAPERCLIP_RUN_ID', 'GH_TOKEN', 'GITHUB_TOKEN', 'NODE_OPTIONS']) {
    check(String(s.UnsetEnvironment).split(' ').includes(name), `readiness-sweep.service unsets ${name}`)
  }
  check(s.Environment === '"PATH=/usr/local/bin:/usr/bin:/bin"',
    'readiness-sweep.service child PATH is the exact minimal set')
  // GH_CONFIG_DIR isolation is per-pass at runtime (prepareIsolatedGhConfig):
  // the unit must not pin one statically, or every pass would share it.
  check(!('GH_CONFIG_DIR' in s) && !String(s.Environment).includes('GH_CONFIG_DIR') &&
    !String(s.ExecStart).includes('GH_CONFIG_DIR'),
    'readiness-sweep.service sets no static GH_CONFIG_DIR (isolation is per-pass)')
  check(s.StandardOutput === 'journal' && s.StandardError === 'journal',
    'readiness-sweep.service streams records to the journal (SIGKILL observability)')
  return failures
}

const staticFailures = readinessContracts()
if (staticFailures.length > 0) {
  for (const failure of staticFailures) console.error(`FAIL: ${failure}`)
  process.exitCode = 1
} else {
  console.log('PASS: readiness-sweep.service/.timer static contracts')
}

const result = spawnSync('systemd-analyze', ['--user', 'verify', ...units.map(name => join(root, 'systemd', name))], { encoding: 'utf8', timeout: 30000 })
if (result.error?.code === 'ENOENT') {
  console.error('UNAVAILABLE: systemd-analyze; only static unit contracts have been tested')
  if (process.exitCode !== 1) process.exitCode = 2
} else if (result.error || result.signal || result.status !== 0) {
  console.error('FAIL: systemd-analyze did not validate the packaged user units')
  if (result.stderr) console.error(result.stderr)
  process.exitCode = 1
} else if (process.exitCode !== 1) {
  console.log('PASS: systemd-analyze user-unit verification (not live service acceptance)')
}
