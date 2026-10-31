import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { format } from 'node:util';
import vm from 'node:vm';

const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const workspace = path.resolve(request.workspace);
const stdout = [];
const stderr = [];
const guardFailures = [];
const scoped = (value) => {
  const filename = path.resolve(value instanceof URL ? value.pathname : String(value));
  if (filename !== workspace && !filename.startsWith(workspace + path.sep)) {
    guardFailures.push('external_file_access');
    throw new Error('offline_external_file_access_denied');
  }
  return filename;
};
const operations = {};
for (const method of ['writeFileSync', 'readFileSync', 'chmodSync', 'statSync', 'existsSync', 'unlinkSync', 'rmSync', 'mkdirSync']) {
  operations[method] = (filename, ...args) => fs[method](scoped(filename), ...args);
}
operations.promises = {};
for (const method of ['writeFile', 'readFile', 'chmod', 'stat', 'unlink', 'rm', 'mkdir']) {
  operations.promises[method] = (filename, ...args) => fs.promises[method](scoped(filename), ...args);
}
const capture = (lines) => (...args) => { lines.push(format(...args)); };
class WorkflowExit extends Error {
  constructor(code) { super('workflow_exit'); this.code = code; }
}
const environment = {
  ...request.env,
  HOME: workspace,
  RUNNER_TEMP: workspace,
  GITHUB_WORKSPACE: workspace,
  GITHUB_OUTPUT: path.join(workspace, 'github-output'),
  SECRETS_PATH: path.join(workspace, 'runtime-secrets.json'),
};
const processFacade = {
  env: environment,
  argv: ['node', '-'],
  cwd: () => workspace,
  exit: (code = 0) => { throw new WorkflowExit(code); },
  stdout: { write: (text) => { stdout.push(String(text)); } },
  stderr: { write: (text) => { stderr.push(String(text)); } },
};
const context = vm.createContext({
  process: processFacade,
  Buffer,
  URL,
  structuredClone,
  AbortController,
  AbortSignal,
  Headers,
  Request,
  Response,
  console: {
    log: capture(stdout), info: capture(stdout), debug: capture(stdout),
    error: capture(stderr), warn: capture(stderr),
  },
  fetch: async () => {
    guardFailures.push('unmocked_network_access');
    throw new Error('offline_network_denied');
  },
});
const exportsFor = (specifier) => {
  if (specifier === 'node:fs' || specifier === 'fs') return operations;
  if (specifier === 'node:fs/promises' || specifier === 'fs/promises') return operations.promises;
  if (specifier === 'node:path' || specifier === 'path') return path;
  if (specifier === 'node:os' || specifier === 'os') return { tmpdir: () => workspace };
  if (specifier === 'node:process' || specifier === 'process') return processFacade;
  guardFailures.push(`unapproved_module:${specifier}`);
  throw new Error('offline_module_denied');
};
let exitCode = 0;
try {
  const module = new vm.SourceTextModule(request.program, {
    context,
    identifier: path.join(workspace, 'opaque-runtime-secret.mjs'),
    initializeImportMeta: (meta) => { meta.url = new URL('opaque-runtime-secret.mjs', `file://${workspace}/`).href; },
  });
  const dependencies = new Map();
  await module.link((specifier, referencing) => {
    if (specifier.startsWith('./') || specifier.startsWith('../')) {
      const filename = scoped(path.resolve(path.dirname(referencing.identifier), specifier));
      if (!dependencies.has(filename)) dependencies.set(filename, new vm.SourceTextModule(fs.readFileSync(filename, 'utf8'), {
        context, identifier: filename,
      }));
      return dependencies.get(filename);
    }
    const values = exportsFor(specifier);
    return new vm.SyntheticModule(['default', ...Object.keys(values)], function initialize() {
      this.setExport('default', values);
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate({ timeout: 2000 });
} catch (error) {
  exitCode = error instanceof WorkflowExit ? error.code : 1;
  if (!(error instanceof WorkflowExit)) stderr.push(`${error.name}: ${error.message}`);
}
const files = {};
for (const name of fs.readdirSync(workspace)) {
  if (['runner.mjs', 'package.json'].includes(name)) continue;
  const filename = path.join(workspace, name);
  const stat = fs.statSync(filename);
  if (stat.isFile()) files[name] = { text: fs.readFileSync(filename, 'utf8'), mode: stat.mode & 0o777 };
}
assert.ok(Number.isInteger(exitCode));
process.stdout.write(JSON.stringify({ exitCode, stdout: stdout.join('\n'), stderr: stderr.join('\n'), guardFailures, files }));
