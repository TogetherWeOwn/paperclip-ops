import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';
import {
  HTTP_CHECK, TLS_CHECK, WEBHOOK, READ_TOKEN, DRILL_TOKEN,
  config, denyNetwork, assertNoSensitiveText,
} from './support.mjs';
import {
  MAIN, PREVIOUS, OTHER, VERSION, REPOSITORY,
  admissionFixture, dispatchInputs, execute, route,
} from './release-support.mjs';

const deniedNetwork = denyNetwork();
afterEach(() => assert.equal(deniedNetwork.attempts.length, 0));
after(() => deniedNetwork.restore());
const CANARY = 'SYNTHETIC_PRIVATE_CANARY_DO_NOT_ECHO';

function admissionResult(fixture, expected) {
  const result = execute('admission', fixture);
  assertNoSensitiveText({ stdout: result.stdout, stderr: result.stderr }, [
    'example-only-github-token', 'example-only-plan-read-token',
  ]);
  assert.equal(result.exitCode === 0, expected, expected
    ? 'Complete same-source CI, native review and explicit Workers Free evidence must admit'
    : 'Missing, mismatched or unsafe release evidence must fail closed');
  if (expected) {
    for (const id of ['source-checks', 'associated-prs', 'pr-head-checks', 'subscriptions']) {
      assert.ok(result.calls.some((call) => call.fixture === id), `Admission must actually evaluate ${id}`);
    }
    assert.ok(result.calls.every((call) => call.authenticated));
  }
  return result;
}

test('manual release inputs expose no public provider version UUID parameter', () => {
  assert.deepEqual(dispatchInputs(), ['operation', 'source_sha', 'target']);
});

for (const target of ['staging', 'production']) {
  test(`${target} deploy admits only the workflow main source with complete CI, native review and explicit free proof`, () => {
    admissionResult(admissionFixture({ target }), true);
  });
  test(`${target} rollback rechecks the recorded old source instead of substituting unrelated current main`, () => {
    const fixture = admissionFixture({ target, operation: 'rollback' });
    assert.notEqual(fixture.env.SOURCE_SHA, fixture.env.GITHUB_SHA);
    assert.equal(JSON.parse(fixture.env.ROLLBACK_RECORD_JSON).versionId, VERSION);
    const result = admissionResult(fixture, true);
    assert.ok(result.calls.some((call) => call.path.endsWith(`/commits/${PREVIOUS}/check-runs`)));
    assert.ok(result.calls.some((call) => call.path.endsWith(`/commits/${PREVIOUS}/pulls`)));
  });
}

for (const source of ['', 'main', '1'.repeat(39), 'g'.repeat(40), OTHER]) {
  test(`deploy refuses ${source === OTHER ? 'a different valid SHA' : 'a missing or malformed source'} (${source.length} characters)`, () => {
    const fixture = admissionFixture();
    fixture.env.SOURCE_SHA = source;
    admissionResult(fixture, false);
  });
}

for (const marker of ['', OTHER]) {
  test(`production deploy refuses ${marker ? 'a different-source' : 'missing'} staging evidence`, () => {
    const fixture = admissionFixture({ target: 'production' });
    fixture.env.STAGING_VERIFIED_SHA = marker;
    admissionResult(fixture, false);
  });
}

for (const [label, record] of [
  ['missing rollback record', ''],
  ['malformed rollback record', 'not JSON'],
  ['missing version', JSON.stringify({ sourceSha: PREVIOUS })],
  ['non-UUID version', JSON.stringify({ sourceSha: PREVIOUS, versionId: 'example-only-invalid-version' })],
  ['mismatched recorded source', JSON.stringify({ sourceSha: MAIN, versionId: VERSION })],
]) {
  test(`rollback refuses ${label}`, () => {
    const fixture = admissionFixture({ operation: 'rollback' });
    fixture.env.ROLLBACK_RECORD_JSON = record;
    admissionResult(fixture, false);
  });
}

test('rollback cannot borrow green current-main CI when its own recorded source is red', () => {
  const fixture = admissionFixture({ operation: 'rollback' });
  route(fixture, 'source-checks').json.check_runs[0].conclusion = 'failure';
  admissionResult(fixture, false);
});

for (const state of ['missing', 'queued', 'failure']) {
  test(`source ci-ok ${state} refuses admission`, () => {
    const fixture = admissionFixture();
    const checks = route(fixture, 'source-checks').json;
    if (state === 'missing') { checks.check_runs = []; checks.total_count = 0; }
    else if (state === 'queued') { checks.check_runs[0].status = 'queued'; checks.check_runs[0].conclusion = null; }
    else checks.check_runs[0].conclusion = 'failure';
    admissionResult(fixture, false);
  });
}

for (const name of ['ci-ok', 'pr-lint', 'public-safety']) {
  for (const state of ['missing', 'incomplete', 'red']) {
    test(`PR-head ${name} ${state} refuses admission`, () => {
      const fixture = admissionFixture();
      const checks = route(fixture, 'pr-head-checks').json;
      if (state === 'missing') checks.check_runs = checks.check_runs.filter((check) => check.name !== name);
      else {
        const check = checks.check_runs.find((candidate) => candidate.name === name);
        check.status = state === 'incomplete' ? 'in_progress' : 'completed';
        check.conclusion = state === 'incomplete' ? null : 'failure';
      }
      checks.total_count = checks.check_runs.length;
      admissionResult(fixture, false);
    });
  }
}

for (const [label, change] of [
  ['missing native review', (checks) => { checks.check_runs = checks.check_runs.filter((check) => check.app.slug !== 'togetherweown-reviewer'); }],
  ['untrusted issuing app', (checks) => { checks.check_runs.at(-1).app.slug = 'example-reviewer'; }],
  ['incomplete native review', (checks) => { checks.check_runs.at(-1).status = 'in_progress'; }],
  ['failed native review', (checks) => { checks.check_runs.at(-1).conclusion = 'failure'; }],
  ['different reviewed head', (checks) => {
    checks.check_runs.at(-1).head_sha = OTHER;
    checks.check_runs.at(-1).output.summary = `Reviewed head: ${OTHER}`;
  }],
  ['missing reviewed head', (checks) => {
    delete checks.check_runs.at(-1).head_sha;
    checks.check_runs.at(-1).output.summary = 'No reviewed head supplied';
  }],
  ['4/5 title', (checks) => { checks.check_runs.at(-1).output.title = '4/5'; }],
  ['title containing but not equal to 5/5', (checks) => { checks.check_runs.at(-1).output.title = '15/5'; }],
  ['title with a 5/5 suffix', (checks) => { checks.check_runs.at(-1).output.title = 'pending 5/5'; }],
]) {
  test(`native review gate refuses ${label}`, () => {
    const fixture = admissionFixture();
    const checks = route(fixture, 'pr-head-checks').json;
    change(checks);
    checks.total_count = checks.check_runs.length;
    admissionResult(fixture, false);
  });
}

for (const [label, change] of [
  ['no associated PR', (prs) => { prs.length = 0; }],
  ['two matching merged PRs', (prs) => { prs.push({ ...prs[0], number: 8 }); }],
  ['unmerged PR', (prs) => { prs[0].merged = false; prs[0].merged_at = null; prs[0].state = 'open'; }],
  ['non-main merge', (prs) => { prs[0].base.ref = 'example-branch'; }],
  ['mismatched merge SHA', (prs) => { prs[0].merge_commit_sha = OTHER; }],
  ['HOLD title', (prs) => { prs[0].title = 'feat(monitoring): [HOLD] example release'; }],
  ['HOLD label', (prs) => { prs[0].labels = [{ name: 'HOLD' }]; }],
]) {
  test(`merged-PR admission refuses ${label}`, () => {
    const fixture = admissionFixture();
    change(route(fixture, 'associated-prs').json);
    admissionResult(fixture, false);
  });
}

test('incomplete PR-head check inventory is not complete review proof', () => {
  const fixture = admissionFixture();
  route(fixture, 'pr-head-checks').json.total_count += 1;
  admissionResult(fixture, false);
});

test('a next-page PR association cannot be silently ignored when establishing a unique merged PR', () => {
  const fixture = admissionFixture();
  const first = route(fixture, 'associated-prs');
  first.headers = { Link: `<https://api.github.com/repos/${REPOSITORY}/commits/${MAIN}/pulls?page=2>; rel="next"` };
  fixture.routes.unshift({ ...structuredClone(first), id: 'associated-prs-page-two', query: { page: '2' }, headers: {}, json: [{ ...first.json[0], number: 8 }] });
  admissionResult(fixture, false);
});

for (const [label, change] of [
  ['absent inventory', (r) => { r.json.result = []; r.json.result_info.count = 0; r.json.result_info.total_count = 0; }],
  ['missing inventory result', (r) => { delete r.json.result; }],
  ['non-array inventory', (r) => { r.json.result = {}; }],
  ['unknown zero-price plan', (r) => { r.json.result[0].rate_plan.id = 'example-unknown'; r.json.result[0].rate_plan.public_name = 'Example Unknown'; }],
  ['positive price', (r) => { r.json.result[0].price = 5; }],
  ['missing price', (r) => { delete r.json.result[0].price; }],
  ['string price rather than explicit numeric zero', (r) => { r.json.result[0].price = '0'; }],
  ['boolean price rather than explicit numeric zero', (r) => { r.json.result[0].price = false; }],
  ['contract plan', (r) => { r.json.result[0].rate_plan.is_contract = true; }],
  ['priced component value', (r) => { r.json.result[0].component_values = [{ name: 'example-component', price: 1, value: 1 }]; }],
  ['priced rate-plan component', (r) => { r.json.result[0].rate_plan.components = [{ name: 'example-component', price: 1 }]; }],
  ['incomplete inventory', (r) => { r.json.result_info.total_count = 2; r.json.result_info.total_pages = 2; }],
  ['provider denial', (r) => { r.status = 403; }],
  ['provider transport failure', (r) => { r.transportError = true; }],
  ['malformed provider JSON', (r) => { r.raw = 'example-only malformed provider response'; }],
  ['unsuccessful provider envelope', (r) => { r.json.success = false; }],
]) {
  test(`billing admission fails closed on ${label}`, () => {
    const fixture = admissionFixture();
    change(route(fixture, 'subscriptions'));
    admissionResult(fixture, false);
  });
}

test('one explicit Workers Free record cannot authorize an additional paid subscription', () => {
  const fixture = admissionFixture();
  const response = route(fixture, 'subscriptions').json;
  const paid = structuredClone(response.result[0]);
  paid.price = 5;
  paid.rate_plan.id = 'workers_paid';
  paid.rate_plan.public_name = 'Workers Paid';
  response.result.push(paid);
  response.result_info.count = response.result_info.total_count = 2;
  admissionResult(fixture, false);
});

for (const key of ['GH_TOKEN', 'PLAN_READ_TOKEN']) {
  test(`missing ${key} capability cannot produce admission`, () => {
    const fixture = admissionFixture();
    fixture.env[key] = '';
    admissionResult(fixture, false);
  });
}

test('GitHub evidence transport failure cannot produce admission', () => {
  const fixture = admissionFixture();
  route(fixture, 'pr-head-checks').transportError = true;
  admissionResult(fixture, false);
});

function secretValues(target) {
  const values = {
    MONITOR_CONFIG: JSON.stringify(config([HTTP_CHECK], [TLS_CHECK])),
    ALERT_WEBHOOK_URL: WEBHOOK,
    CF_CERTIFICATES_READ_TOKEN: READ_TOKEN,
  };
  if (target === 'staging') values.DRILL_TOKEN = DRILL_TOKEN;
  return values;
}

function secretResult(target, json, expected) {
  const result = execute('secrets', { env: { TARGET: target, MONITOR_SECRETS_JSON: json } });
  assertNoSensitiveText({ stdout: result.stdout, stderr: result.stderr }, [
    CANARY, CANARY.slice(0, 8), READ_TOKEN, WEBHOOK, DRILL_TOKEN,
  ]);
  assert.equal(result.exitCode === 0, expected, expected
    ? 'Only the exact synthetic runtime binding set should materialize'
    : 'Invalid secret JSON or disallowed bindings must fail closed');
  if (!expected) assert.equal(result.files['runtime-secrets.json'], undefined, 'Invalid secret data must not materialize a runtime secret file');
  return result;
}

for (const target of ['staging', 'production']) {
  test(`${target} runtime secret scalar materializes only its exact binding set as private JSON`, () => {
    const values = secretValues(target);
    const result = secretResult(target, JSON.stringify(values), true);
    const file = result.files['runtime-secrets.json'];
    assert.ok(file);
    assert.equal(file.mode, 0o600);
    const actual = JSON.parse(file.text);
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(values).sort());
    for (const key of Object.keys(values)) assert.ok(actual[key] === values[key], 'Secret binding values must remain unchanged');
  });
  for (const key of ['MONITOR_CONFIG', 'ALERT_WEBHOOK_URL', 'CF_CERTIFICATES_READ_TOKEN']) {
    test(`${target} secret JSON refuses missing ${key}`, () => {
      const values = secretValues(target);
      delete values[key];
      secretResult(target, JSON.stringify(values), false);
    });
  }
  test(`${target} secret JSON refuses extra keys without echoing their contents`, () => {
    secretResult(target, JSON.stringify({ ...secretValues(target), EXTRA: CANARY }), false);
  });
}

test('production secret JSON refuses DRILL_TOKEN even when its value is empty', () => {
  secretResult('production', JSON.stringify({ ...secretValues('production'), DRILL_TOKEN: '' }), false);
});

test('staging secret JSON requires DRILL_TOKEN', () => {
  const values = secretValues('staging');
  delete values.DRILL_TOKEN;
  secretResult('staging', JSON.stringify(values), false);
});

for (const length of [31, 32]) {
  test(`staging DRILL_TOKEN length ${length} is ${length === 32 ? 'admitted' : 'refused'}`, () => {
    secretResult('staging', JSON.stringify({ ...secretValues('staging'), DRILL_TOKEN: 'x'.repeat(length) }), length === 32);
  });
}

for (const [label, value] of [
  ['malformed outer JSON with private content', CANARY],
  ['a JSON string', JSON.stringify(CANARY)],
  ['an array', JSON.stringify([CANARY])],
  ['null', 'null'],
  ['non-string binding value', JSON.stringify({ ...secretValues('production'), CF_CERTIFICATES_READ_TOKEN: { private: CANARY } })],
  ['malformed serialized MONITOR_CONFIG', JSON.stringify({ ...secretValues('production'), MONITOR_CONFIG: CANARY })],
]) {
  test(`runtime secret validation refuses ${label} without exposing contents`, () => {
    secretResult('production', value, false);
  });
}
