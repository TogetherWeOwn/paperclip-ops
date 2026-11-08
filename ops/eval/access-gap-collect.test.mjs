import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { collectAccessInputs, extractToolOutputs, receipt } from './access-gap-collect.mjs';
import { evaluateAccessGaps } from './access-gap.mjs';
import { paperclipGet, main } from './access-gap-cli.mjs';

const window = { since: '2026-01-01T00:00:00Z', until: '2026-01-02T00:00:00Z' };
const at = '2026-01-01T12:00:00Z';
const run = { id: 'run1', agentId: 'agent1', createdAt: at };
const frames = events => events.map(event => JSON.stringify({ ts: at, stream: 'stdout', chunk: JSON.stringify(event) })).join('\n');
const result = (callId, text) => ({ type: 'acpx.tool_call', toolCallId: callId, status: 'failed', text });
const options = get => ({ company: 'company1', operatorId: 'operator1', window, asOf: window.until, get,
  collectInteractions: async ({ get }) => {
    await get('/companies/company1/issues?limit=250&offset=0&includeHidden=true');
    return { interactions: [{ id: 'ix1', createdAt: at, addresseeUserId: 'operator1', status: 'accepted' }],
      coverage: { complete: true, issuePaginationComplete: true, truncated: false } };
  }, metadata: { newCardsAlreadyCreated: 0 } });
function mock(path) {
  if (path.includes('/issues?')) return [{ id: 'card1', status: 'done' }];
  if (path.endsWith('/agents')) return [{ id: 'agent1' }];
  if (path.includes('/heartbeat-runs?')) return [run];
  if (path.includes('/log?')) return { content: frames([result('c1', 'HTTP 403 missing grant')]), nextOffset: null };
  throw new Error('unexpected route');
}

test('structured completions only: prose, quoted rules and source reads are not failures', () => {
  const content = frames([
    { type: 'assistant', message: { content: [{ type: 'text', text: 'HTTP 403 permission denied' }] } },
    { type: 'user', message: { content: [{ type: 'text', text: 'HTTP 403 permission denied' }] } },
    result('read1', 'HTTP 403 permission denied'),
    { type: 'acpx.tool_call', toolCallId: 'read1', name: 'Read /repo/SKILL.md', status: 'in_progress' },
    { type: 'acpx.tool_call', toolCallId: 'source1', name: 'Read /repo/fixture.json', status: 'in_progress' },
    result('source1', 'permission denied'), result('real', 'HTTP 403'),
  ]);
  const out = extractToolOutputs(content, run);
  assert.equal(out.malformed, 0);
  assert.deepEqual(out.rows.map(x => x.callId), ['real']);
});
test('stream-json result joins call id; expected negative tests stay diagnostic', () => {
  const content = frames([
    { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name: 'Bash', input: { command: 'node --test refusals.test.mjs' } }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: [{ type: 'text', text: 'HTTP 403 policy_denied' }] }] } },
    result('c2', 'missing credentials'), result('c2', 'missing credentials'),
  ]);
  const out = extractToolOutputs(content, run, { expectedRefusalCallIds: ['c2'] });
  assert.equal(out.rows.length, 2);
  assert.ok(out.rows.every(x => x.expectedRefusal));
});
test('malformed and missing timestamp frames cannot assert complete log coverage', () => {
  assert.equal(extractToolOutputs('not json\n' + JSON.stringify(result('a', '403')), run).malformed, 2);
});
test('GET-only collection reuses M26 and produces verifiable receipts', async () => {
  const paths = [];
  const input = await collectAccessInputs(options(async path => { paths.push(path); return mock(path); }));
  const scored = evaluateAccessGaps(input);
  assert.equal(scored.coverage.interactions.status, 'complete');
  assert.equal(scored.coverage.runs.status, 'complete');
  assert.equal(scored.coverage.toolOutputs.status, 'complete');
  assert.equal(scored.metrics.accessFailures.observedUniqueRuns, 1);
  assert.equal(scored.interactions[0].outcome, 'accepted');
  assert.equal(scored.interactions[0].category, 'unknown-mixed');
  assert.equal(scored.coverage.providers.status, 'UNKNOWN');
  assert.equal(scored.legacyCoverage.status, 'UNKNOWN');
  assert.ok(paths.every(path => !/secret|inventory|database/.test(path)));
});
test('denied M26 inputs are UNKNOWN, not a measured zero', async () => {
  const o = options(mock); o.collectInteractions = async () => { throw new Error('403 secret-url'); };
  const input = await collectAccessInputs(o);
  const out = evaluateAccessGaps(input);
  assert.equal(out.metrics.operatorInteractions.status, 'UNKNOWN');
  assert.equal(out.metrics.operatorInteractions.agentDoablePer100Interactions, null);
  assert.ok(!JSON.stringify(out).includes('secret-url'));
});
test('run caps, denied logs and unproved EOF defeat coverage', async () => {
  for (const mode of ['cap', 'denied', 'eof', 'malformed']) {
    const input = await collectAccessInputs(options(async path => {
      if (mode === 'cap' && path.includes('/heartbeat-runs?')) return Array.from({ length: 1000 }, () => run);
      if (path.includes('/log?')) {
        if (mode === 'denied') throw new Error('403');
        if (mode === 'eof') return { content: '', nextOffset: 0 };
        if (mode === 'malformed') return { content: 'cut frame', nextOffset: null };
      }
      return mock(path);
    }));
    assert.equal(evaluateAccessGaps(input).metrics.accessFailures.status, 'UNKNOWN', mode);
  }
});
test('pagination caps remain UNKNOWN even with observed failures', async () => {
  const o = options(async path => path.includes('/log?') ? { content: frames([result('c', '403')]), nextOffset: 1 } : mock(path));
  o.maxLogPages = 1;
  const input = await collectAccessInputs(o);
  assert.equal(input.toolOutputs.length, 1);
  assert.equal(evaluateAccessGaps(input).coverage.toolOutputs.status, 'UNKNOWN');
});
test('bounded GET adapter refuses secret and cross-company routes before fetch', async () => {
  let calls = 0;
  const get = paperclipGet({ base: 'https://example.invalid/api', key: 'fixture-only', company: 'company1', fetchImpl: async () => { calls++; return new Response('[]'); } });
  for (const path of ['/agents/me/secrets', '/companies/company2/agents', '/issues/../secrets/interactions', '//other.invalid/']) await assert.rejects(get(path));
  assert.equal(calls, 0);
  await get('/companies/company1/agents');
  assert.equal(calls, 1);
});
test('GET transport is redirect-free, byte/read-bounded and never publishes credentials', async () => {
  let saved;
  const get = paperclipGet({ base: 'https://example.invalid', key: 'fixture-only', company: 'company1', maxReads: 1,
    fetchImpl: async (url, init) => { saved = { url, init }; return new Response('[]'); } });
  assert.deepEqual(await get('/companies/company1/agents'), []);
  assert.equal(saved.init.method, 'GET'); assert.equal(saved.init.redirect, 'error');
  await assert.rejects(get('/companies/company1/agents'), /collection_budget/);
  const huge = paperclipGet({ base: 'https://example.invalid', key: 'fixture-only', company: 'company1', maxBytes: 1, fetchImpl: async () => new Response('[1]') });
  await assert.rejects(huge('/companies/company1/agents'), /response_byte_cap/);
  assert.throws(() => paperclipGet({ base: 'https://user:password@example.invalid', key: 'fixture-only', company: 'company1' }));
});
test('offline CLI refuses symlinks, duplicate keys and invalid UTF-8', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'access-gap-cli-'));
  try {
    const file = join(dir, 'input.json'), link = join(dir, 'link.json');
    writeFileSync(file, '{}'); symlinkSync(file, link);
    await assert.rejects(main(['--from-file', link]));
    writeFileSync(file, '{"schema":1,"schema":2}'); await assert.rejects(main(['--from-file', file]));
    writeFileSync(file, Buffer.from([0xff])); await assert.rejects(main(['--from-file', file]));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test('offline CLI includes executable hashes without fetching any service', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'access-gap-cli-'));
  try {
    const input = { schema: 'access-gap-input.v1', operatorId: 'operator1', window, asOf: window.until, newCardsAlreadyCreated: 0, coverage: {} };
    for (const stream of ['interactions', 'runs', 'toolOutputs', 'cards', 'cardEvents', 'incidents', 'providers']) {
      input[stream] = []; input.coverage[stream] = receipt([], window, true);
    }
    const file = join(dir, 'input.json'); writeFileSync(file, JSON.stringify(input));
    const out = await main(['--from-file', file]);
    assert.equal(out.schema, 'access-gap.v1');
    assert.equal(Object.keys(out.adapter.sourceHashes).length, 4);
    assert.equal(out.adapter.authorization, false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
