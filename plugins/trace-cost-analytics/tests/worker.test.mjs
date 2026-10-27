import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { manifest } from '../src/manifest.mjs';
import { registerOutcomeCostTool } from '../src/worker.mjs';

const COMPANY = 'company-fixture';
const OTHER = 'company-other';
const TOOL = 'analyze_outcome_costs';

function register() {
  const registrations = [];
  registerOutcomeCostTool({ tools: { register: (name, declaration, handler) => registrations.push({ name, declaration, handler }) } });
  return registrations;
}

function handler() {
  const [registration] = register();
  return registration.handler;
}

function snapshot(companyId = COMPANY, overrides = {}) {
  return {
    schemaVersion: 1, companyId, capturedAt: '2026-01-02T00:00:00.000Z', complete: true,
    issues: [{ id: 'issue-a', status: 'done', deliveredAt: '2026-01-01T00:01:00.000Z', evidenceRef: 'evidence:accepted', evidenceKind: 'accepted' }],
    runs: [{
      id: 'run-a', issueId: 'issue-a', model: 'model-a', status: 'succeeded',
      startedAt: '2026-01-01T00:00:00.000Z', finishedAt: '2026-01-01T00:00:30.000Z', costUsdExact: '1.250000000', accountingReady: true,
    }],
    steps: [], scores: [], ...overrides,
  };
}

test('registers exactly one tool named analyze_outcome_costs with the manifest declaration', () => {
  const registrations = register();
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].name, TOOL);
  assert.equal(typeof registrations[0].handler, 'function');
  assert.equal(registrations[0].declaration, manifest.tools[0]);
  assert.equal(registrations[0].declaration.name, registrations[0].name);
  assert.deepEqual(manifest.tools.map((tool) => tool.name), [TOOL]);
  assert.ok(manifest.capabilities.includes('agent.tools.register'));
});

test('takes the company scope from the host tool context and reports the analysis', async () => {
  const result = await handler()({ snapshot: snapshot() }, { companyId: COMPANY });
  assert.equal(result.data.ok, true);
  assert.equal(result.data.companyId, COMPANY);
  assert.equal(result.data.totals.outcomes, 1);
  assert.equal(result.data.totals.knownCostUsd, '1.250000000');
  assert.equal(result.data.totals.costPerOutcomeUsd, '1.250000000');
  assert.equal(result.data.evidenceTrust, 'caller-attested');
  assert.match(result.content, /1 caller-attested delivered outcomes across 1 runs/);
  assert.match(result.content, /Known spend USD 1\.250000000; 0 run costs unknown/);
  assert.match(result.content, /cost per outcome USD 1\.250000000/);
  assert.match(result.content, /not independently verified/);
});

test('reports unknown cost per outcome in the summary when it cannot be computed', async () => {
  const result = await handler()({ snapshot: snapshot(COMPANY, { runs: [] }) }, { companyId: COMPANY });
  assert.equal(result.data.ok, true);
  assert.equal(result.data.totals.costPerOutcomeUsd, null);
  assert.match(result.content, /cost per outcome USD unknown\./);
});

test('refuses a snapshot for a company other than the host tool context', async () => {
  const result = await handler()({ snapshot: snapshot(OTHER) }, { companyId: COMPANY });
  assert.deepEqual(result.data, { ok: false, reason: 'invalid-snapshot' });
  const reverse = await handler()({ snapshot: snapshot(COMPANY) }, { companyId: OTHER });
  assert.deepEqual(reverse.data, { ok: false, reason: 'invalid-snapshot' });
});

test('refuses records inside the snapshot that belong to another company', async () => {
  const input = snapshot();
  input.runs[0].companyId = OTHER;
  const result = await handler()({ snapshot: input }, { companyId: COMPANY });
  assert.deepEqual(result.data, { ok: false, reason: 'invalid-snapshot' });
});

test('model-supplied company overrides in arguments or snapshot cannot select another company', async () => {
  const run = handler();
  // Arguments cannot rebind scope: the host company still has to match the snapshot.
  const foreign = await run({ snapshot: snapshot(OTHER), companyId: OTHER, company: OTHER, tenant: OTHER }, { companyId: COMPANY });
  assert.deepEqual(foreign.data, { ok: false, reason: 'invalid-snapshot' });
  // A matching snapshot is analyzed under the host company regardless of extra argument fields.
  const own = await run({ snapshot: snapshot(COMPANY), companyId: OTHER }, { companyId: COMPANY });
  assert.equal(own.data.ok, true);
  assert.equal(own.data.companyId, COMPANY);
});

test('a missing host company scope fails closed before reading the snapshot', async () => {
  const run = handler();
  const expected = { ok: false, reason: 'missing-company-scope' };
  for (const toolCtx of [undefined, null, {}, { companyId: '' }, { companyId: null }]) {
    const result = await run({ snapshot: snapshot(COMPANY) }, toolCtx);
    assert.deepEqual(result.data, expected, JSON.stringify(toolCtx));
    assert.equal(result.content, 'Company scope is required.');
  }
  // A company named only inside the snapshot or arguments is not a substitute for host scope.
  assert.deepEqual((await run({ snapshot: snapshot(COMPANY), companyId: COMPANY }, {})).data, expected);
});

test('analyzer errors are surfaced as a structured refusal, not thrown or reported as success', async () => {
  const run = handler();
  const cases = [
    undefined, {}, { snapshot: null }, { snapshot: 'snapshot' }, { snapshot: [] },
    { snapshot: snapshot(COMPANY, { schemaVersion: 2 }) },
    { snapshot: snapshot(COMPANY, { complete: undefined }) },
    { snapshot: snapshot(COMPANY, { issues: 'nope' }) },
  ];
  for (const args of cases) {
    const result = await run(args, { companyId: COMPANY });
    assert.equal(result.data.ok, false, JSON.stringify(args));
    assert.equal(result.data.reason, 'invalid-snapshot');
    assert.match(result.content, /Nothing was changed/);
  }
  assert.equal((await run(undefined, { companyId: COMPANY })).data.ok, false);
});

test('a refusal never echoes snapshot identifiers or payload values', async () => {
  const input = snapshot(COMPANY);
  input.issues[0].id = 'issue-SECRET-IDENTIFIER';
  input.runs[0].issueId = 'issue-SECRET-IDENTIFIER';
  input.runs[0].costUsdExact = 'SECRET-PAYLOAD';
  const result = await handler()({ snapshot: input }, { companyId: COMPANY });
  assert.equal(result.data.ok, false);
  assert.deepEqual(Object.keys(result.data).sort(), ['ok', 'reason']);
  assert.doesNotMatch(JSON.stringify(result), /SECRET/);
});

test('analysis through the tool is passive and does not mutate the supplied snapshot', async () => {
  const input = snapshot();
  const before = structuredClone(input);
  await handler()({ snapshot: input }, { companyId: COMPANY });
  assert.deepEqual(input, before);
});

test('manifest worker entrypoint resolves to a module that runs the plugin worker', () => {
  assert.equal(typeof manifest.entrypoints?.worker, 'string');
  const source = readFileSync(new URL(`../${manifest.entrypoints.worker}`, import.meta.url), 'utf8');
  assert.match(source, /runWorker\s*\(/);
  assert.match(source, /export\s+default\b/);
});
