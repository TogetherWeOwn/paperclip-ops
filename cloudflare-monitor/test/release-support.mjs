import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const MAIN = '1'.repeat(40);
export const PR_HEAD = '2'.repeat(40);
export const PREVIOUS = '3'.repeat(40);
export const OTHER = '4'.repeat(40);
export const ACCOUNT = '0'.repeat(32);
export const REPOSITORY = 'example/monitor';
export const VERSION = ['0'.repeat(8), '0'.repeat(4), '4' + '0'.repeat(3), '8' + '0'.repeat(3), '0'.repeat(12)].join('-');
const workflow = readFileSync(new URL('../../.github/workflows/cloudflare-monitor-deploy.yml', import.meta.url), 'utf8');

export function scalar(name) {
  const lines = workflow.split('\n');
  const start = lines.findIndex((line) => line.trim() === `- name: ${name}`);
  assert.ok(start >= 0, 'The published executable step must exist');
  const run = lines.findIndex((line, index) => index > start && /^\s+run: \|\s*$/.test(line));
  assert.ok(run >= 0, 'The selected workflow step must have a literal executable scalar');
  const indentation = lines[run].search(/\S/);
  const body = [];
  for (const line of lines.slice(run + 1)) {
    if (line.trim() && line.search(/\S/) <= indentation) break;
    body.push(line.slice(indentation + 2));
  }
  return body;
}

export function heredoc(name, interpreter, delimiter) {
  const lines = scalar(name);
  const start = lines.findIndex((line) => line.trim() === `${interpreter} <<'${delimiter}'`);
  assert.ok(start >= 0, 'The published interpreter framing must be extractable without executing the shell');
  const end = lines.findIndex((line, index) => index > start && line.trim() === delimiter);
  assert.ok(end > start, 'The opaque heredoc must have a closing delimiter');
  return lines.slice(start + 1, end).join('\n');
}

export const admissionProgram = heredoc('Verify exact source, review and free-plan admission', 'python3 -I -', 'PY');
export const secretsProgram = heredoc('Deploy or roll back without publishing private provider output', 'node --input-type=module', 'JS');

export function dispatchInputs() {
  const lines = workflow.split('\n');
  const start = lines.findIndex((line) => line.trim() === 'inputs:');
  assert.ok(start >= 0);
  const level = lines[start].search(/\S/);
  const names = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() && line.search(/\S/) <= level) break;
    if (line.search(/\S/) === level + 2 && /^\s*[a-z_]+:\s*$/.test(line)) names.push(line.trim().slice(0, -1));
  }
  return names.sort();
}

export function check(name, head, extra = {}) {
  return {
    id: 100,
    name,
    status: 'completed',
    conclusion: 'success',
    head_sha: head,
    app: { id: 1000, slug: 'github-actions' },
    output: { title: 'passed', summary: 'Example-only check result' },
    ...extra,
  };
}

export function freeSubscription() {
  return {
    id: '0'.repeat(32),
    currency: 'USD',
    current_period_start: '2030-01-01T00:00:00Z',
    current_period_end: '2030-02-01T00:00:00Z',
    frequency: 'monthly',
    price: 0,
    state: 'Provisioned',
    component_values: [],
    rate_plan: {
      id: 'workers_free', public_name: 'Workers Free', currency: 'USD',
      scope: 'account', externally_managed: false, is_contract: false, components: [],
    },
  };
}

export function admissionFixture({ operation = 'deploy', target = 'staging' } = {}) {
  const source = operation === 'rollback' ? PREVIOUS : MAIN;
  const pr = {
    number: 7, title: 'feat: example monitor', state: 'closed', merged: true,
    merged_at: '2030-01-01T00:00:00Z', merge_commit_sha: source, labels: [],
    base: { ref: 'main', repo: { full_name: REPOSITORY } },
    head: { sha: PR_HEAD, repo: { full_name: REPOSITORY } },
  };
  const headChecks = [
    check('ci-ok', PR_HEAD), check('pr-lint', PR_HEAD), check('public-safety', PR_HEAD),
    check('Paperclip Review', PR_HEAD, {
      app: { id: 2000, slug: 'togetherweown-reviewer' },
      output: { title: '5/5', summary: `Reviewed head: ${PR_HEAD}` },
    }),
  ];
  const routes = [
    { id: 'source-checks', host: 'api.github.com', path: `/repos/${REPOSITORY}/commits/${source}/check-runs`, json: { total_count: 1, check_runs: [check('ci-ok', source)] } },
    { id: 'associated-prs', host: 'api.github.com', path: `/repos/${REPOSITORY}/commits/${source}/pulls`, json: [pr] },
    { id: 'pr-head-checks', host: 'api.github.com', path: `/repos/${REPOSITORY}/commits/${PR_HEAD}/check-runs`, json: { total_count: headChecks.length, check_runs: headChecks } },
    {
      id: 'subscriptions', host: 'api.cloudflare.com', path: `/client/v4/accounts/${ACCOUNT}/subscriptions`,
      json: { success: true, errors: [], messages: [], result: [freeSubscription()], result_info: { page: 1, per_page: 100, count: 1, total_count: 1, total_pages: 1 } },
    },
  ];
  if (source !== MAIN) routes.push({
    id: 'unrelated-main-checks', host: 'api.github.com', path: `/repos/${REPOSITORY}/commits/${MAIN}/check-runs`,
    json: { total_count: 1, check_runs: [check('ci-ok', MAIN)] },
  });
  return {
    env: {
      GH_TOKEN: 'example-only-github-token', SOURCE_SHA: source, TARGET: target, OPERATION: operation,
      STAGING_VERIFIED_SHA: MAIN,
      ROLLBACK_RECORD_JSON: JSON.stringify({ sourceSha: PREVIOUS, versionId: VERSION }),
      CLOUDFLARE_ACCOUNT_ID: ACCOUNT, PLAN_READ_TOKEN: 'example-only-plan-read-token',
      GITHUB_REPOSITORY: REPOSITORY, GITHUB_SHA: MAIN, GITHUB_REF: 'refs/heads/main',
      GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_API_URL: 'https://api.github.com',
    },
    routes,
  };
}

export function route(fixture, id) {
  const found = fixture.routes.find((item) => item.id === id);
  assert.ok(found, 'The selected synthetic provider fixture must exist');
  return found;
}

export function execute(kind, fixture) {
  const workspace = mkdtempSync(path.join(tmpdir(), 'monitor-release-'));
  try {
    writeFileSync(path.join(workspace, 'package.json'), JSON.stringify({ type: 'module' }));
    let command;
    let args;
    if (kind === 'admission') {
      command = '/usr/bin/python3';
      args = ['-I', '-B', fileURLToPath(new URL('./release-offline-harness.py', import.meta.url))];
    } else {
      command = process.execPath;
      copyFileSync(new URL('./release-runtime-harness.mjs', import.meta.url), path.join(workspace, 'runner.mjs'));
      // Preserve the helper's real validator dependency as an opaque, unmodified module.
      mkdirSync(path.join(workspace, 'src'));
      copyFileSync(new URL('../src/monitor.mjs', import.meta.url), path.join(workspace, 'src/monitor.mjs'));
      args = ['--experimental-vm-modules', '--permission', `--allow-fs-read=${workspace}`, `--allow-fs-write=${workspace}`, path.join(workspace, 'runner.mjs')];
    }
    const child = spawnSync(command, args, {
      cwd: workspace,
      env: { PATH: '/usr/bin:/bin', HOME: workspace, PYTHONIOENCODING: 'utf-8' },
      input: JSON.stringify({ ...fixture, workspace, program: kind === 'admission' ? admissionProgram : secretsProgram }),
      encoding: 'utf8', timeout: 10000, maxBuffer: 1024 * 1024,
    });
    assert.equal(child.status, 0, 'The isolated offline executable harness must start and return a result');
    const result = JSON.parse(child.stdout);
    assert.deepEqual(result.guardFailures, [], 'A step attempted an unmocked external operation');
    if (kind === 'admission') {
      assert.deepEqual(result.commands, [], 'No real provider command may be invoked');
      assert.deepEqual(result.unexpected, [], 'Every provider route must be supplied by an explicit fixture');
      assert.deepEqual(result.mutations, [], 'Admission must remain read-only, including on refusal');
    }
    return result;
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
}
