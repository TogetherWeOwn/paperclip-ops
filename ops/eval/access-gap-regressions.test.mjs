import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateAccessGaps, gapKey } from './access-gap.mjs';
import { extractToolOutputs, collectAccessInputs, receipt } from './access-gap-collect.mjs';

const window = { since: '2026-01-01T00:00:00Z', until: '2026-01-02T00:00:00Z' };
const at = '2026-01-01T12:00:00Z';
const run = { id: 'r1', agentId: 'a1', createdAt: at };
const done = (id, output, status = 'failed') => ({ type: 'acpx.tool_call', toolCallId: id, text: output, status });
const wrap = event => JSON.stringify({ ts: at, stream: 'stdout', chunk: JSON.stringify(event) }) + '\n';
const streams = ['interactions', 'runs', 'toolOutputs', 'cards', 'cardEvents', 'providers', 'incidents'];
function input() {
  return { schema: 'access-gap-input.v1', window, asOf: window.until, operatorId: 'operator', newCardsAlreadyCreated: 0,
    ...Object.fromEntries(streams.map(s => [s, []])) };
}
function seal(data) {
  data.coverage = Object.fromEntries(streams.map(s => [s, receipt(data[s], window, true)]));
  return data;
}
function candidate() {
  const data = input();
  data.providers = [{ id: 'p1', group: 'forge', package: 'forge-broker', packagePresent: true,
    inventoryComplete: true, tools: [{ operation: 'list', name: 'list_tool' }],
    policyComplete: true, policyRevision: 'policy1', grants: [
      { agentId: 'a1', scope: 'repo1', tier: 'read', operationIds: ['list'], eligible: true, granted: false, prohibited: false },
    ], capabilityMap: { revision: 'map1', sha256: 'a'.repeat(64), sourceUrl: 'https://example.invalid/map',
      operations: [{ id: 'list', scope: 'repo1', tier: 'read', tool: 'list_tool', entitled: true, status: 'reviewed' }] },
    snapshot: { revision: 's2', sha256: 'b'.repeat(64), sourceUrl: 'https://example.invalid/schema', operations: ['list'] } }];
  for (const n of [1, 2]) {
    data.runs.push({ ...run, id: `r${n}` });
    data.toolOutputs.push({ id: `out${n}`, runId: `r${n}`, callId: `c${n}`, at, origin: 'tool_result', text: 'missing grant' });
    data.incidents.push({ id: `i${n}`, at, group: 'forge', operation: 'list', targetScope: 'repo1', tier: 'read', agentId: 'a1', evidenceKeys: [`tool:out${n}`],
      validation: { revision: 'v1', completeRequest: true, currentCapabilityPolicy: true } });
  }
  return data;
}
const score = data => evaluateAccessGaps(seal(data));
function collection(getLog, metadata = {}) {
  return collectAccessInputs({ company: 'company', operatorId: 'operator', window, asOf: window.until,
    metadata: { newCardsAlreadyCreated: 0, ...metadata },
    collectInteractions: async ({ get }) => {
      await get('/companies/company/issues?limit=250&offset=0&includeHidden=true');
      return { interactions: [], coverage: { complete: true, issuePaginationComplete: true, truncated: false } };
    }, get: async path => {
      if (path.includes('/issues?')) return [{ id: 'card1', status: 'done' }];
      if (path.endsWith('/agents')) return [{ id: 'a1' }];
      if (path.includes('/heartbeat-runs?')) return [run];
      if (path.includes('/log?')) return getLog();
      throw Error('unexpected_mock_route');
    } });
}

test('review regression: explicit truncation defeats a parseable log EOF', async () => {
  const data = await collection(() => ({ content: wrap(done('c1', 'clean')), truncated: true, nextOffset: null }));
  const out = evaluateAccessGaps(data);
  assert.equal(out.coverage.toolOutputs.status, 'UNKNOWN');
  assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
});
test('review regression: unknown valid JSON event types defeat complete coverage', async () => {
  const data = await collection(() => ({ content: wrap({ type: 'function_call_output', output: 'permission denied' }), nextOffset: null }));
  assert.equal(evaluateAccessGaps(data).coverage.toolOutputs.status, 'UNKNOWN');
});
test('review regression: wrapper chunks reconstruct inner JSON across arbitrary boundaries', () => {
  const inner = JSON.stringify(done('c1', 'HTTP 403')) + '\n';
  const content = [inner.slice(0, 12), inner.slice(12, 39), inner.slice(39)]
    .map(chunk => JSON.stringify({ ts: at, stream: 'stdout', chunk })).join('\n');
  const out = extractToolOutputs(content, run);
  assert.equal(out.malformed, 0); assert.equal(out.rows.length, 1);
  assert.equal(out.rows[0].text, 'HTTP 403');
});
test('review regression: ACP Bash source reads never turn fixture text into denial evidence', () => {
  const content = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', status: 'in_progress', text: 'cat fixture.json' }) + wrap(done('c1', 'missing grant'));
  assert.deepEqual(extractToolOutputs(content, run).rows, []);
});
test('review regression: a successful quota inventory is not a quota failure', () => {
  const data = input(); data.runs = [run];
  data.toolOutputs = extractToolOutputs(wrap(done('c1', '{"quota":1000,"used":0}', 'completed')), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 0);
  data.toolOutputs = extractToolOutputs(wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c2', is_error: false, content: 'quota: 1000' }] } }), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 0);
  data.toolOutputs = extractToolOutputs(wrap(done('c3', 'HTTP 403 missing grant', 'completed')), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 1, 'wrapper completion is not HTTP success');
});
test('review regression: orphan run joins defeat complete failure rates', () => {
  const data = input(); data.runs = [run];
  data.toolOutputs = [{ id: 'orphan', runId: 'missing', callId: 'c1', at, origin: 'tool_result', text: 'permission denied' }];
  const out = score(data);
  assert.equal(out.metrics.accessFailures.status, 'UNKNOWN');
  assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
});
test('review regression: card metadata cannot replace native identity or current status', async () => {
  const data = await collection(() => ({ content: '', nextOffset: null }), { cardAnnotations: { card1: { id: 'forged', status: 'blocked', accessRelated: true } } });
  assert.equal(data.cards[0].id, 'card1'); assert.equal(data.cards[0].status, 'done');
  assert.equal(evaluateAccessGaps(data).metrics.accessStock.observed, 0);
});
test('review regression: missing card annotations cannot certify complete access stock', async () => {
  const data = await collection(() => ({ content: '', nextOffset: null }));
  assert.equal(evaluateAccessGaps(data).metrics.accessStock.status, 'UNKNOWN');
});
test('review regression: contradictory grant metadata cannot depend on array order', () => {
  const data = candidate(), p = data.providers[0];
  p.grants.push({ ...p.grants[0], prohibited: true });
  for (const grants of [p.grants, [...p.grants].reverse()]) {
    p.grants = grants; const out = score(data);
    assert.equal(out.providerCoverage[0].status, 'UNKNOWN'); assert.deepEqual(out.proposals, []);
  }
});
test('review regression: invalid previous digest cannot prove schema drift', () => {
  const data = candidate(), p = data.providers[0]; p.tools = [];
  p.snapshot.previousSha256 = 'unknown'; p.snapshot.addedOperations = ['list'];
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  assert.deepEqual(score(data).proposals, []);
});
test('review regression: installed additions with an existing grant need no drift or grant card', () => {
  const data = candidate(), p = data.providers[0];
  p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list']; p.grants[0].granted = true;
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  assert.deepEqual(score(data).proposals, []);
});
test('review regression: modern drift keys retain schema revision, unlike legacy feature keys', () => {
  const data = candidate(), p = data.providers[0]; p.tools = [];
  p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list'];
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  data.cards.push({ id: 'old', status: 'backlog', group: 'forge', gapClass: 'provider_api_drift',
    gapKey: gapKey({ group: 'forge', gapClass: 'provider_api_drift', targetScope: 'full-surface', tier: 'all', schemaRevision: 's1' }) });
  const out = score(data); assert.equal(out.proposals.length, 1); assert.deepEqual(out.updates, []);
});
test('review regression: inherited intent names remain unknown and counts reconcile', () => {
  const data = input();
  data.interactions = [{ id: 'ix1', createdAt: at, addresseeUserId: 'operator', requestEvidence: { complete: true, sourceInteractionId: 'ix1', kinds: ['constructor'] } }];
  const out = score(data); assert.equal(out.interactions[0].category, 'unknown-mixed');
  assert.equal(out.metrics.operatorInteractions.byCategory['unknown-mixed'], 1);
});
test('review regression: unauthorized or failed enablement cannot prove enabled or adopted', () => {
  for (const damage of [{ authorized: false }, { success: false }]) {
    const data = input();
    data.cardEvents = [
      { id: 'enable', cardId: 'card1', kind: 'enabled', at, authorized: true, success: true, version: 'v1', tier: 'read', agentId: 'a1', ...damage },
      { id: 'use', cardId: 'card1', kind: 'adopted', at, authorized: true, success: true, version: 'v1', tier: 'read', agentId: 'a1' },
    ];
    const out = score(data); assert.ok(out.outcomes.every(e => e.status === 'UNKNOWN'));
  }
});
