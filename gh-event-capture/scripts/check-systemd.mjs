// Systemd unit gate for the packaged bridge user units.
//
// Exit 0 PASS, exit 1 FAIL (systemd-analyze rejection),
// exit 2 UNAVAILABLE (systemd-analyze missing — never PASS).
//
// The readiness sweep is NOT a systemd unit: it runs as the event-driven
// Actions workflow .github/workflows/readiness-sweep.yml (the host-PEM
// wrapper path was retired and the readiness-sweep.service/.timer pair was
// deleted with it). This gate covers only the gh-product-bridge units below;
// the workflow's own contract is pinned by test/readiness-job-token.test.mjs.
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const units = ['gh-product-bridge.service', 'gh-product-bridge.timer', 'gh-product-bridge-health.service', 'gh-product-bridge-health.timer', 'gh-product-bridge-checkin.service', 'gh-product-bridge-checkin.timer']

const result = spawnSync('systemd-analyze', ['--user', 'verify', ...units.map(name => join(root, 'systemd', name))], { encoding: 'utf8', timeout: 30000 })
if (result.error?.code === 'ENOENT') {
  console.error('UNAVAILABLE: systemd-analyze; packaged user units are unverified')
  process.exitCode = 2
} else if (result.error || result.signal || result.status !== 0) {
  console.error('FAIL: systemd-analyze did not validate the packaged user units')
  if (result.stderr) console.error(result.stderr)
  process.exitCode = 1
} else {
  console.log('PASS: systemd-analyze user-unit verification (not live service acceptance)')
}
