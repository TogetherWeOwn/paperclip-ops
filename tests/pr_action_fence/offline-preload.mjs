// Load BEFORE production imports. Only the pure local Python evaluator may spawn.
// Its process is separately stripped of credentials and denied network/process I/O.
import cp from 'node:child_process'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import tls from 'node:tls'
import dns from 'node:dns'
import dgram from 'node:dgram'
import { syncBuiltinESMExports } from 'node:module'
import { promisify } from 'node:util'
import { resolve, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const helper = resolve(root, 'ops/productivity/pr_action_policy.py')
const originalExecFile = cp.execFile
const originalExecFileSync = cp.execFileSync
const originalSpawnSync = cp.spawnSync
const keep = ['PATH', 'HOME', 'TMPDIR', 'PAPERCLIP_RUN_SCRATCH_DIR', 'PAPERCLIP_SCRATCH_DIR', 'NODE_TEST_CONTEXT']
const env = Object.fromEntries(keep.filter((key) => key in process.env).map((key) => [key, process.env[key]]))
for (const key of Object.keys(process.env)) delete process.env[key]
Object.assign(process.env, env)

const bootstrap = `import runpy,sys,socket,subprocess,os,urllib.request
from unittest.mock import patch
def deny(*a,**k): raise AssertionError('OFFLINE TRAP: evaluator attempted external I/O')
p=sys.argv.pop(1)
with patch.object(socket,'socket',side_effect=deny),patch.object(socket,'create_connection',side_effect=deny),patch.object(urllib.request,'urlopen',side_effect=deny),patch.object(subprocess,'Popen',side_effect=deny),patch.object(os,'system',side_effect=deny):
 sys.argv[0]=p
 runpy.run_path(p,run_name='__main__')`

export function deny() { throw new Error('OFFLINE TRAP: unexpected network or process access') }
function approved(file, argv, options = {}) {
  if (!['python3', 'python'].includes(basename(file)) || !Array.isArray(argv) || options.shell) deny()
  // No -c/-m/interpreter flag from the consumer; the test supplies its own trap.
  const index = argv.findIndex((arg) => !String(arg).startsWith('-'))
  if (index < 0 || resolve(argv[index]) !== helper || argv.slice(0, index).some((a) => !['-B', '-I'].includes(a))) deny()
  if (!Number.isFinite(options.timeout) || options.timeout <= 0 || options.timeout > 10000) deny()
  if (!Number.isFinite(options.maxBuffer) || options.maxBuffer <= 0 || options.maxBuffer > 1024 * 1024) deny()
  return { argv: ['-B', '-c', bootstrap, helper, ...argv.slice(index + 1)],
    options: { ...options, cwd: root, shell: false, env: { ...env, PYTHONDONTWRITEBYTECODE: '1' } } }
}
function execFile(file, argv, options, callback) {
  const input = approved(file, argv, options)
  return originalExecFile(file, input.argv, input.options, callback)
}
execFile[promisify.custom] = (file, argv, options) => new Promise((resolveResult, reject) => {
  execFile(file, argv, options, (error, stdout, stderr) => error ? reject(error) : resolveResult({ stdout, stderr }))
})
cp.execFile = execFile
cp.execFileSync = (file, argv, options) => {
  const input = approved(file, argv, options)
  return originalExecFileSync(file, input.argv, input.options)
}
cp.spawnSync = (file, argv, options) => {
  const input = approved(file, argv, options)
  return originalSpawnSync(file, input.argv, input.options)
}
for (const name of ['exec', 'execSync', 'spawn', 'fork']) cp[name] = deny
for (const api of [http, https]) for (const name of ['request', 'get']) api[name] = deny
for (const name of ['connect', 'createConnection', 'createServer']) net[name] = deny
net.Socket.prototype.connect = deny
net.Server.prototype.listen = deny
tls.connect = deny
for (const api of [dns, dns.promises]) {
  for (const name of Object.keys(api)) if (/^(lookup|resolve|reverse)/.test(name)) api[name] = deny
}
dgram.createSocket = deny
globalThis.fetch = deny
globalThis.WebSocket = class { constructor() { deny() } }
syncBuiltinESMExports()
