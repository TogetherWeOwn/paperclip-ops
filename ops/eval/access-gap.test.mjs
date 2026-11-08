import assert from 'node:assert/strict';
import test from 'node:test';
import { canonical, evaluateAccessGaps, gapKey, hash, LIMIT } from './access-gap.mjs';

// Frozen, synthetic source-carrier fixtures only. No API, subprocess, filesystem,
// wall clock, or operational authorization is involved in these assertions.
const STREAMS = ['interactions', 'runs', 'toolOutputs', 'cards', 'cardEvents', 'incidents', 'providers'];
const SHA = 'a'.repeat(64);
const PREVIOUS_SHA = 'b'.repeat(64);
const at = (day, time = '00:00:00.000') => `2030-01-${String(day).padStart(2, '0')}T${time}Z`;
const DURING = at(2, '12:00:00.000');

function sortedRecords(records) {
  // coverage() receives deduplicated records and hashes canonical records sorted
  // by their serialized value, NOT insertion order or just their IDs.
  return [...new Map(records.map(record => [record.id, record])).values()]
    .map(record => canonical(record))
    .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b), 'en'));
}
function seal(input) {
  input.coverage = Object.fromEntries(STREAMS.map(stream => {
    const records = sortedRecords(input[stream]);
    const revision = `fixture-${stream}-r1`;
    return [stream, {
      complete: true, errors: [], truncated: false, pages: [1],
      since: input.window.since, until: input.window.until,
      recordCount: records.length, recordsSha256: hash(records), revision,
      sourceSha256: hash({ stream, revision, records }),
    }];
  }));
  return input;
}
function fixture() {
  return seal({
    schema: 'access-gap-input.v1', window: { since: at(2), until: at(3) },
    asOf: at(3), operatorId: 'operator-a', newCardsAlreadyCreated: 0,
    ...Object.fromEntries(STREAMS.map(stream => [stream, []])),
  });
}
function provider(group = 'forge', packagePresent = false) {
  const operations = [
    { id: 'issues.list', scope: 'repo:issues', tier: 'read', tool: 'forge_issues_list', entitled: true, status: 'reviewed' },
    { id: 'issues.create', scope: 'repo:issues', tier: 'write', tool: 'forge_issues_create', entitled: true, status: 'reviewed' },
    { id: 'repos.admin', scope: 'repo:admin', tier: 'reserved', tool: 'forge_repos_admin', entitled: true, status: 'reviewed' },
  ];
  return {
    id: `provider-${group}`, group, package: `mcp-${group}`, packagePresent,
    inventoryComplete: true, policyComplete: true, policyRevision: 'policy-r1',
    tools: packagePresent ? operations.map(op => ({ operation: op.id, name: op.tool })) : [],
    grants: [],
    capabilityMap: { revision: 'map-r1', sha256: SHA, sourceUrl: 'https://example.invalid/map', operations },
    snapshot: { revision: 'api-r2', sha256: SHA, sourceUrl: 'https://example.invalid/openapi', operations: operations.map(op => op.id) },
  };
}
function interaction(id, extra = {}) {
  return {
    id, createdAt: DURING, addresseeUserId: 'operator-a', requestedByAgentId: 'agent-a',
    status: 'pending', kind: 'question',
    requestEvidence: { complete: true, sourceInteractionId: id, kinds: ['scoped_tool_use'] },
    ...extra,
  };
}
function addIncident(input, {
  suffix = 'a', group = 'forge', agentId = 'agent-a', operation = 'issues.list',
  evidenceKeys, text = 'missing grant', materialReproduced = false, ownerSeed = false,
  schemaRevision,
} = {}) {
  const p = input.providers.find(row => row.group === group);
  const op = p.capabilityMap.operations.find(row => row.id === operation);
  const run = { id: `run-${suffix}`, agentId, createdAt: DURING };
  const output = { id: `output-${suffix}`, runId: run.id, callId: `call-${suffix}`, at: DURING, origin: 'tool_result', text };
  if (evidenceKeys === undefined) {
    input.runs.push(run);
    input.toolOutputs.push(output);
  }
  const incident = {
    id: `incident-${suffix}`, at: DURING, group, agentId, operation,
    tier: op.tier, targetScope: op.scope,
    evidenceKeys: evidenceKeys ?? [`tool:${output.id}`],
    validation: { completeRequest: true, currentCapabilityPolicy: true, revision: 'validation-r1', materialReproduced, ownerSeed },
    ...(schemaRevision === undefined ? {} : { schemaRevision }),
  };
  input.incidents.push(incident);
  return { incident, run, output };
}
function packageFixture() {
  const input = fixture();
  input.providers.push(provider());
  addIncident(input, { suffix: 'a' });
  addIncident(input, { suffix: 'b' });
  return seal(input);
}
function driftFixture() {
  const input = fixture(), p = provider('forge', true);
  p.snapshot.previousSha256 = PREVIOUS_SHA;
  p.snapshot.addedOperations = ['issues.create', 'repos.admin'];
  p.tools = p.tools.filter(tool => tool.operation === 'issues.list');
  input.providers.push(p);
  addIncident(input, { suffix: 'write', operation: 'issues.create', schemaRevision: p.snapshot.revision });
  addIncident(input, { suffix: 'reserved', operation: 'repos.admin', schemaRevision: p.snapshot.revision });
  return seal(input);
}
const evaluate = input => evaluateAccessGaps(seal(input));
const ids = rows => rows.map(row => row.id).sort();
function noRemediation(result) {
  assert.deepEqual(result.proposals, [], 'must not propose wider access');
  assert.deepEqual(result.updates, [], 'must not smuggle remediation into an existing card');
}
function withoutInputHash(result) {
  const { inputSha256, ...rest } = result;
  assert.match(inputSha256, /^[a-f0-9]{64}$/);
  return rest;
}
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

test('empty, completely covered input is known zero, not an invented rate or improvement', () => {
  const result = evaluateAccessGaps(fixture());
  assert.equal(result.schema, 'access-gap.v1');
  assert.equal(result.rules, 'access-gap-rules.v1');
  for (const stream of STREAMS) assert.equal(result.coverage[stream].status, 'complete');
  assert.equal(result.metrics.operatorInteractions.observed, 0);
  assert.equal(result.metrics.operatorInteractions.agentDoablePer100Interactions, null);
  assert.equal(result.metrics.operatorInteractions.unknownFraction, null);
  assert.equal(result.metrics.accessFailures.observedUniqueRuns, 0);
  assert.equal(result.metrics.accessFailures.per100CoveredRuns, null);
  assert.equal(result.metrics.accessStock.observed, 0);
  assert.equal(result.metrics.accessFlow.observed, 0);
  assert.equal(result.legacyMetrics, null);
  assert.equal(result.legacyCoverage.status, 'UNKNOWN');
  assert.match(result.authority, /No .* grants permission or proves access improved/);
  noRemediation(result);
});

test('schema and complete UTC-day windows are required', async t => {
  const cases = [
    ['wrong schema', b => { b.schema = 'access-gap-input.v0'; }],
    ['offset instead of UTC', b => { b.window.since = '2030-01-02T00:00:00+00:00'; }],
    ['timezone omitted', b => { b.asOf = '2030-01-03T00:00:00'; }],
    ['invalid calendar date', b => { b.window.since = '2030-02-30T00:00:00.000Z'; }],
    ['partial first day', b => { b.window.since = at(2, '00:00:00.001'); }],
    ['partial last day', b => { b.window.until = at(2, '23:59:59.999'); }],
    ['empty window', b => { b.window.until = b.window.since; }],
    ['reversed window', b => { b.window.since = at(4); }],
    ['unobserved window', b => { b.asOf = at(2, '23:59:59.999'); }],
  ];
  for (const [name, damage] of cases) await t.test(name, () => {
    const input = fixture(); damage(input);
    assert.throws(() => evaluateAccessGaps(input));
  });
});

test('UTC windows accept whole-day multi-day/leap-day spans and explicit seconds', () => {
  const input = fixture();
  input.window = { since: '2032-02-28T00:00:00Z', until: '2032-03-01T00:00:00Z' };
  input.asOf = '2032-03-01T12:00:00Z';
  input.interactions.push(interaction('leap-day', { createdAt: '2032-02-29T23:59:59.999Z' }));
  assert.equal(evaluate(input).metrics.operatorInteractions.observed, 1);
});

test('half-open UTC boundaries use creation time for pending and resolved Operator requests', () => {
  const input = fixture();
  input.interactions.push(
    interaction('start-pending', { createdAt: at(2), status: 'pending' }),
    interaction('end-resolved', { createdAt: at(2, '23:59:59.999'), status: 'resolved', resolvedAt: at(3) }),
    interaction('old-resolved-now', { createdAt: at(1, '23:59:59.999'), status: 'resolved', resolvedAt: DURING }),
    interaction('next-day', { createdAt: at(3), status: 'pending' }),
    interaction('another-addressee', { addresseeUserId: 'someone-else' }),
  );
  const result = evaluate(input);
  assert.deepEqual(ids(result.interactions), ['end-resolved', 'start-pending']);
  assert.deepEqual(result.interactions.map(row => row.outcome).sort(), ['pending', 'resolved']);
  assert.equal(result.metrics.operatorInteractions.observed, 2);
  assert.equal(result.metrics.operatorInteractions.totalInteractions, 3);
  assert.equal(result.metrics.operatorInteractions.agentDoablePer100Interactions, 2 / 3 * 100);
});

test('run and tool timestamps must both belong to the half-open window', () => {
  const input = fixture();
  for (const [id, createdAt, eventAt] of [
    ['start', at(2), at(2)], ['last', at(2), at(2, '23:59:59.999')],
    ['old-run', at(1), DURING], ['future-run', at(3), DURING],
    ['old-output', DURING, at(1, '23:59:59.999')], ['future-output', DURING, at(3)],
  ]) {
    input.runs.push({ id, createdAt, agentId: 'agent-a' });
    input.toolOutputs.push({ id: `output-${id}`, runId: id, callId: `call-${id}`, at: eventAt, origin: 'tool_result', text: 'permission denied' });
  }
  const result = evaluate(input);
  assert.deepEqual(ids(result.failures), ['output-last', 'output-start']);
  assert.equal(result.metrics.accessFailures.totalRuns, 4);
  assert.equal(result.metrics.accessFailures.per100CoveredRuns, 50);
});

test('malformed timestamps inside evidence are not silently treated as out-of-window', () => {
  const input = fixture();
  input.interactions.push(interaction('bad-time', { createdAt: '2030-01-02T12:00:00+00:00' }));
  assert.throws(() => evaluate(input), /UTC/);
});

test('identical streaming IDs deduplicate independently in every stream', async t => {
  const input = packageFixture();
  input.interactions.push(interaction('request-a'));
  input.cards.push({ id: 'card-a', status: 'blocked', accessRelated: true });
  input.cardEvents.push({ id: 'event-a', cardId: 'card-a', kind: 'blocked', at: DURING, accessRelated: true });
  const baseline = withoutInputHash(evaluate(input));
  for (const stream of STREAMS) await t.test(stream, () => {
    const copy = structuredClone(input);
    copy[stream].push(structuredClone(copy[stream][0]));
    assert.deepEqual(withoutInputHash(evaluate(copy)), baseline);
    assert.equal(evaluate(copy).coverage[stream].observed, input[stream].length);
  });
});

test('conflicting duplicate IDs fail closed in every stream', async t => {
  const input = packageFixture();
  input.interactions.push(interaction('request-a'));
  input.cards.push({ id: 'card-a', status: 'backlog' });
  input.cardEvents.push({ id: 'event-a', cardId: 'card-a', kind: 'opened', at: DURING });
  for (const stream of STREAMS) await t.test(stream, () => {
    const copy = structuredClone(input);
    copy[stream].push({ ...structuredClone(copy[stream][0]), conflictingPayload: true });
    assert.throws(() => evaluate(copy), /Conflicting duplicate evidence/);
  });
});

test('invalid evidence IDs, duplicate provider groups and excessive input are rejected', () => {
  const invalid = fixture(); invalid.runs.push({ id: '../invalid id', createdAt: DURING });
  assert.throws(() => evaluate(invalid), /identifier/);
  const duplicate = fixture();
  duplicate.providers.push(provider(), { ...provider(), id: 'provider-other-id' });
  assert.throws(() => evaluate(duplicate), /Duplicate provider group/);
  const excessive = fixture();
  excessive.runs = Array.from({ length: LIMIT + 1 }, (_, i) => ({ id: `run-${i}`, createdAt: DURING }));
  assert.throws(() => evaluateAccessGaps(excessive), /excessive rows/);
});

test('canonical sorted receipt membership survives stream and object-key reordering', () => {
  const input = packageFixture();
  input.interactions.push(interaction('z-request'), interaction('a-request'));
  seal(input);
  const result = evaluateAccessGaps(input);
  for (const stream of STREAMS) {
    input[stream].reverse();
    input[stream] = input[stream].map(row => Object.fromEntries(Object.entries(row).reverse()));
  }
  assert.deepEqual(withoutInputHash(evaluateAccessGaps(input)), withoutInputHash(result));
});

test('evaluation is deterministic and does not mutate a frozen source carrier', () => {
  const input = freeze(packageFixture());
  const before = JSON.stringify(input);
  assert.deepEqual(evaluateAccessGaps(input), evaluateAccessGaps(input));
  assert.equal(JSON.stringify(input), before);
});

test('missing coverage is UNKNOWN separately for every source, never inferred from empty rows', async t => {
  for (const stream of STREAMS) await t.test(stream, () => {
    const input = fixture(); delete input.coverage[stream];
    const result = evaluateAccessGaps(input);
    assert.equal(result.coverage[stream].status, 'UNKNOWN');
    assert.ok(result.coverage[stream].reasons.length > 0);
    noRemediation(result);
  });
});

test('denied, truncated, unpinned and incomplete coverage retain observations but suppress rates', async t => {
  const cases = [
    ['denied', receipt => { receipt.errors = ['403 denied']; }, 'unavailable_input'],
    ['truncated', receipt => { receipt.truncated = true; }, 'truncation_unknown_or_true'],
    ['unknown truncation', receipt => { delete receipt.truncated; }, 'truncation_unknown_or_true'],
    ['not complete', receipt => { receipt.complete = false; }, 'not_complete'],
    ['missing errors', receipt => { delete receipt.errors; }, 'unavailable_input'],
    ['missing page', receipt => { receipt.pages = [1, 3]; }, 'pagination_gap'],
    ['no page receipt', receipt => { receipt.pages = []; }, 'pagination_gap'],
    ['wrong window', receipt => { receipt.since = at(1); }, 'window_mismatch'],
    ['wrong count', receipt => { receipt.recordCount += 1; }, 'membership_mismatch'],
    ['wrong hash', receipt => { receipt.recordsSha256 = PREVIOUS_SHA; }, 'membership_mismatch'],
    ['missing revision', receipt => { delete receipt.revision; }, 'missing_provenance'],
    ['invalid source digest', receipt => { receipt.sourceSha256 = 'not-a-sha'; }, 'missing_provenance'],
  ];
  for (const [name, damage, reason] of cases) await t.test(name, () => {
    const input = packageFixture(); input.interactions.push(interaction('request-a')); seal(input);
    damage(input.coverage.interactions);
    const result = evaluateAccessGaps(input);
    assert.equal(result.coverage.interactions.status, 'UNKNOWN');
    assert.ok(result.coverage.interactions.reasons.includes(reason));
    assert.equal(result.metrics.operatorInteractions.status, 'UNKNOWN');
    assert.equal(result.metrics.operatorInteractions.observed, 1);
    assert.equal(result.metrics.operatorInteractions.agentDoablePer100Interactions, null);
    assert.equal(result.metrics.operatorInteractions.unknownFraction, null);
  });
});

test('missing run or output coverage makes the failure rate UNKNOWN, not zero', async t => {
  for (const stream of ['runs', 'toolOutputs']) await t.test(stream, () => {
    const input = packageFixture(); delete input.coverage[stream];
    const result = evaluateAccessGaps(input);
    assert.equal(result.metrics.accessFailures.status, 'UNKNOWN');
    assert.equal(result.metrics.accessFailures.observedUniqueRuns, 2);
    assert.equal(result.metrics.accessFailures.per100CoveredRuns, null);
  });
});

test('finite, source-bound typed intents classify; missing, mixed and prose claims stay unknown', () => {
  const input = fixture();
  for (const [id, kinds] of [
    ['agent', ['scoped_tool_use']], ['host', ['host_os']], ['board', ['board_setting']],
    ['credential', ['credential_create_rotate']], ['mixed', ['host_os', 'scoped_tool_use']],
    ['unsupported', ['please_grant_admin']], ['empty', []],
  ]) input.interactions.push(interaction(id, { requestEvidence: { complete: true, sourceInteractionId: id, kinds } }));
  input.interactions.push(
    interaction('missing', { requestEvidence: undefined, body: 'This is definitely agent-doable with access.' }),
    interaction('incomplete', { requestEvidence: { complete: false, sourceInteractionId: 'incomplete', kinds: ['scoped_tool_use'] } }),
    interaction('wrong-source', { requestEvidence: { complete: true, sourceInteractionId: 'agent', kinds: ['scoped_tool_use'] } }),
  );
  const result = evaluate(input);
  assert.deepEqual(result.metrics.operatorInteractions.byCategory, {
    agent_doable_with_access: 1, host_only: 1, board_only: 1, owner_credential: 1, 'unknown-mixed': 6,
  });
  assert.equal(result.metrics.operatorInteractions.unknownFraction, 60);
  assert.equal(result.metrics.operatorInteractions.agentDoablePer100Interactions, 10);
});

test('model prose and instruction-reading output are not access-failure evidence', () => {
  const input = fixture();
  input.runs.push({ id: 'run-a', agentId: 'agent-a', createdAt: DURING });
  for (const [id, extra] of [
    ['model', { origin: 'model' }], ['assistant', { origin: 'assistant' }],
    ['instruction', { origin: 'tool_result', instructionRead: true }], ['untyped', { origin: undefined }],
    ['structured', { origin: 'tool_result', text: { error: 'permission denied' } }],
  ]) input.toolOutputs.push({ id, runId: 'run-a', at: DURING, text: '403 permission denied; missing grant', ...extra });
  const result = evaluate(input);
  assert.deepEqual(result.failures, []);
  assert.deepEqual(result.expectedRefusals, []);
  assert.equal(result.metrics.accessFailures.observedUniqueRuns, 0);
  assert.equal(result.metrics.accessFailures.per100CoveredRuns, 0);
});

test('real tool failures emit hashes and locators, never raw tool text', () => {
  const input = packageFixture(), secret = 'fixture-secret-do-not-echo';
  input.toolOutputs[0].text = `401 missing credentials ${secret}`;
  const result = evaluate(input), failure = result.failures.find(row => row.id === 'output-a');
  assert.equal(failure.kind, 'credential_absence');
  assert.equal(failure.outputSha256, hash(input.toolOutputs[0].text));
  assert.equal(failure.callId, 'call-a');
  assert.equal(failure.runId, 'run-a');
  assert.equal(JSON.stringify(result).includes(secret), false);
  assert.equal(Object.hasOwn(failure, 'text'), false);
});

test('expected refusals are diagnostic only and can never support a wider-access proposal', () => {
  const input = packageFixture();
  input.toolOutputs.forEach(output => { output.expectedRefusal = true; });
  input.incidents.forEach(incident => { incident.validation.ownerSeed = true; });
  const result = evaluate(input);
  assert.equal(result.expectedRefusals.length, 2);
  assert.equal(result.failures.length, 0);
  assert.equal(result.metrics.accessFailures.observedUniqueRuns, 0);
  noRemediation(result);
  assert.equal(result.refused.length, 2);
});

test('overlapping signals and repeated outputs count unique failing runs, not additive incidents', () => {
  const input = fixture();
  input.runs.push(
    { id: 'run-a', agentId: 'agent-a', createdAt: DURING },
    { id: 'run-b', agentId: 'agent-b', createdAt: DURING },
    { id: 'run-clean', agentId: 'agent-a', createdAt: DURING },
  );
  for (const [id, runId, text] of [
    ['grant', 'run-a', 'missing grant'], ['credential', 'run-a', '401 missing credentials'],
    ['grant-again', 'run-a', 'missing grant'], ['policy', 'run-b', 'denied by policy'],
  ]) input.toolOutputs.push({ id, runId, at: DURING, origin: 'tool_result', text });
  const result = evaluate(input);
  assert.equal(result.failures.length, 4);
  assert.equal(result.metrics.accessFailures.observedUniqueRuns, 2);
  assert.equal(result.metrics.accessFailures.totalRuns, 3);
  assert.equal(result.metrics.accessFailures.per100CoveredRuns, 2 / 3 * 100);
  assert.deepEqual(result.metrics.accessFailures.byKind, { credential_absence: 1, missing_grant: 1, policy_refusal: 1 });
  assert.deepEqual(result.metrics.accessFailures.agents, ['agent-a', 'agent-b']);
});

test('current access stock and in-window status-event flow have different units', () => {
  const input = fixture();
  for (const status of ['blocked', 'deferred', 'backlog', 'done', 'cancelled', 'in_progress']) {
    input.cards.push({ id: `card-${status}`, status, accessRelated: true, createdAt: at(1) });
  }
  input.cards.push({ id: 'not-access', status: 'blocked', accessRelated: false });
  for (const [id, atTime, kind, accessRelated] of [
    ['start-block', at(2), 'blocked', true], ['second-block', DURING, 'blocked', true],
    ['last-defer', at(2, '23:59:59.999'), 'deferred', true],
    ['old-block', at(1), 'blocked', true], ['next-block', at(3), 'blocked', true],
    ['opening', DURING, 'opened', true], ['other-block', DURING, 'blocked', false],
  ]) input.cardEvents.push({ id, cardId: 'card-done', at: atTime, kind, accessRelated });
  const result = evaluate(input);
  assert.deepEqual(ids(result.stock), ['card-backlog', 'card-blocked', 'card-deferred']);
  assert.deepEqual(ids(result.flow), ['last-defer', 'second-block', 'start-block']);
  assert.equal(result.metrics.accessStock.observed, 3);
  assert.equal(result.metrics.accessFlow.observed, 3);
  assert.match(result.units.stock, /card ID/);
  assert.match(result.units.flow, /event ID/);
});

test('stock and flow keep UNKNOWN coverage even when observed counts are nonzero', () => {
  const input = fixture();
  input.cards.push({ id: 'card-a', status: 'blocked', accessRelated: true });
  input.cardEvents.push({ id: 'blocked-a', cardId: 'card-a', at: DURING, kind: 'blocked', accessRelated: true });
  seal(input); input.coverage.cards.errors = ['denied']; input.coverage.cardEvents.truncated = true;
  const result = evaluateAccessGaps(input);
  assert.deepEqual(result.metrics.accessStock, { status: 'UNKNOWN', observed: 1 });
  assert.deepEqual(result.metrics.accessFlow, { status: 'UNKNOWN', observed: 1 });
});

test('legacy metrics and their coverage are carried unchanged, not recomputed or relabeled', () => {
  const input = packageFixture();
  input.legacyMetrics = { delivery: { done: 7, denominator: 12 }, blocked: 2, rates: [null, 25], note: 'legacy units' };
  input.legacyCoverage = { status: 'UNKNOWN', reason: 'old-partial-source', fetchedPages: [1] };
  const before = structuredClone(input);
  const result = evaluateAccessGaps(freeze(input));
  assert.deepEqual(result.legacyMetrics, before.legacyMetrics);
  assert.deepEqual(result.legacyCoverage, before.legacyCoverage);
  assert.deepEqual(input, before);
});

test('gap keys are stable and include only the class-specific identity dimensions', () => {
  const base = { group: 'forge', gapClass: 'missing_group_package', targetScope: 'full-surface', tier: 'all' };
  assert.equal(gapKey(base), `gap:${hash(['forge', 'missing_group_package', 'full-surface', 'all', null, null])}`);
  assert.equal(gapKey(base), gapKey({ ...base, agentId: 'agent-b', schemaRevision: 'other', incidentalText: 'ignored' }));
  assert.notEqual(gapKey(base), gapKey({ ...base, group: 'another-provider' }));
  const grant = { ...base, gapClass: 'missing_policy_grant', targetScope: 'repo:issues', tier: 'read', agentId: 'agent-a' };
  for (const extra of [{ agentId: 'agent-b' }, { targetScope: 'repo:admin' }, { tier: 'write' }]) {
    assert.notEqual(gapKey(grant), gapKey({ ...grant, ...extra }));
  }
  assert.equal(gapKey(grant), gapKey({ ...grant, schemaRevision: 'not-part-of-grant-identity' }));
  const drift = { ...base, gapClass: 'provider_api_drift', schemaRevision: 'api-r2' };
  assert.notEqual(gapKey(drift), gapKey({ ...drift, schemaRevision: 'api-r3' }));
  assert.equal(gapKey(drift), gapKey({ ...drift, agentId: 'agent-b' }));
});

test('many incidents for one missing group produce one full-surface package, not feature cards', () => {
  const input = fixture(); input.providers.push(provider());
  for (const agentId of ['agent-a', 'agent-b']) {
    for (const operation of ['issues.list', 'issues.create', 'repos.admin']) {
      addIncident(input, { suffix: `${agentId}-${operation}`, agentId, operation });
    }
  }
  const result = evaluate(input);
  assert.equal(result.proposals.length, 1);
  assert.deepEqual(result.updates, []);
  assert.deepEqual(result.deferred, []);
  assert.deepEqual(result.refused, []);
  const proposal = result.proposals[0];
  assert.equal(proposal.gapClass, 'missing_group_package');
  assert.equal(proposal.package, 'mcp-forge');
  assert.equal(proposal.targetScope, 'full-surface');
  assert.equal(proposal.tier, 'all');
  assert.equal(proposal.agentId, null);
  assert.equal(proposal.incidentIds.length, 6);
  assert.equal(proposal.runs.length, 6);
  assert.deepEqual(proposal.agents, ['agent-a', 'agent-b']);
  assert.equal(proposal.authorization, 'proposal_only');
  assert.equal(proposal.route, 'existing_package_source_review');
  assert.equal(proposal.coverageStatus, 'complete');
  assert.equal(proposal.gapKey, gapKey(proposal));
  for (const tier of ['read', 'write', 'reserved']) {
    assert.deepEqual(result.providerCoverage[0].tiers[tier], { entitled: 1, implemented: 1, reviewed: 1, enabled: 0 });
  }
});

test('eligible missing grants remain separate per agent and never create a replacement package', () => {
  const input = fixture(), p = provider('forge', true); input.providers.push(p);
  for (const agentId of ['agent-a', 'agent-b']) {
    p.grants.push({ agentId, scope: 'repo:issues', tier: 'read', operationIds: ['issues.list'], eligible: true, granted: false, prohibited: false });
    for (let i = 0; i < 2; i++) addIncident(input, { suffix: `${agentId}-${i}`, agentId });
  }
  const result = evaluate(input);
  assert.equal(result.proposals.length, 2);
  assert.deepEqual(result.proposals.map(row => row.agentId).sort(), ['agent-a', 'agent-b']);
  assert.equal(new Set(result.proposals.map(row => row.gapKey)).size, 2);
  for (const proposal of result.proposals) {
    assert.equal(proposal.gapClass, 'missing_policy_grant');
    assert.equal(proposal.route, 'connection_config_authorization');
    assert.equal(proposal.authorization, 'proposal_only');
    assert.equal(proposal.tier, 'read');
    assert.equal(proposal.targetScope, 'repo:issues');
  }
});

test('policy denial, discovery faults and quota are repair/refusal signals, never permission authority', async t => {
  for (const text of ['denied by policy', 'tools/list 403', 'connection unavailable', '429 quota exceeded']) {
    await t.test(text, () => {
      const input = packageFixture();
      input.toolOutputs.forEach(output => { output.text = text; });
      input.incidents.forEach(incident => { incident.validation.ownerSeed = true; });
      const result = evaluate(input);
      noRemediation(result);
      assert.equal(result.refused.length, 2);
      assert.ok(result.refused.every(row => row.reason === 'repair_or_refusal_not_permission'));
    });
  }
});

test('an existing tool does not authorize prohibited, already granted or unproven grants', async t => {
  for (const [name, extra] of [
    ['prohibited', { eligible: true, granted: false, prohibited: true }],
    ['ineligible', { eligible: false, granted: false, prohibited: false }],
    ['already granted', { eligible: true, granted: true, prohibited: false }],
    ['unknown prohibition', { eligible: true, granted: false }],
    ['unknown eligibility', { granted: false, prohibited: false }],
  ]) await t.test(name, () => {
    const input = packageFixture(), p = input.providers[0];
    p.packagePresent = true; p.tools = p.capabilityMap.operations.map(op => ({ operation: op.id, name: op.tool }));
    p.grants.push({ agentId: 'agent-a', scope: 'repo:issues', tier: 'read', operationIds: ['issues.list'], ...extra });
    noRemediation(evaluate(input));
  });
});

test('multiple API additions to an existing package form one package/schema drift proposal', () => {
  const result = evaluateAccessGaps(driftFixture());
  assert.equal(result.proposals.length, 1, 'one package/schema change, not one proposal per operation, scope or tier');
  const proposal = result.proposals[0];
  assert.equal(proposal.gapClass, 'provider_api_drift');
  assert.equal(proposal.schemaRevision, 'api-r2');
  assert.equal(proposal.package, 'mcp-forge');
  assert.deepEqual(proposal.incidentIds, ['incident-reserved', 'incident-write']);
  assert.equal(proposal.route, 'existing_package_source_review');
  assert.equal(proposal.authorization, 'proposal_only');
});

test('multiple API additions update the same open drift card once', () => {
  const input = driftFixture();
  input.cards.push({ id: 'existing-drift', group: 'forge', gapClass: 'provider_api_drift', status: 'backlog', accessRelated: true });
  const result = evaluate(input);
  assert.deepEqual(result.proposals, []);
  assert.equal(result.updates.length, 1, 'aggregate all additions before updating the package card');
  assert.deepEqual(result.updates[0].existingCardIds, ['existing-drift']);
  assert.deepEqual(result.updates[0].incidentIds, ['incident-reserved', 'incident-write']);
});

test('unchanged snapshots or unproven additions cannot manufacture API drift', async t => {
  for (const [name, damage] of [
    ['unchanged snapshot', p => { p.snapshot.previousSha256 = p.snapshot.sha256; }],
    ['unknown previous snapshot', p => { delete p.snapshot.previousSha256; }],
    ['no proven additions', p => { p.snapshot.addedOperations = []; }],
    ['wrong incident revision', (p, input) => { input.incidents.forEach(row => { row.schemaRevision = 'api-r1'; }); }],
  ]) await t.test(name, () => {
    const input = driftFixture(); damage(input.providers[0], input);
    const result = evaluate(input);
    noRemediation(result);
    assert.equal(result.refused.length, 2);
  });
});

test('UNKNOWN provider snapshots, maps or inventories cannot support a proposal', async t => {
  for (const [name, damage] of [
    ['snapshot absent', p => { delete p.snapshot; }],
    ['snapshot revision absent', p => { delete p.snapshot.revision; }],
    ['snapshot digest absent', p => { delete p.snapshot.sha256; }],
    ['snapshot source absent', p => { delete p.snapshot.sourceUrl; }],
    ['snapshot operations absent', p => { delete p.snapshot.operations; }],
    ['map source absent', p => { delete p.capabilityMap.sourceUrl; }],
    ['tool inventory incomplete', p => { p.inventoryComplete = false; }],
    ['tool inventory absent', p => { delete p.tools; }],
    ['policy inventory incomplete', p => { p.policyComplete = false; }],
    ['policy grants absent', p => { delete p.grants; }],
    ['package presence unknown', p => { delete p.packagePresent; }],
  ]) await t.test(name, () => {
    const input = packageFixture(); damage(input.providers[0]);
    const result = evaluate(input);
    assert.equal(result.providerCoverage[0].status, 'UNKNOWN');
    noRemediation(result);
  });
});

test('unmapped API operations make full-surface coverage incomplete and block package proposals', () => {
  const input = packageFixture(); input.providers[0].snapshot.operations.push('unknown.newOperation');
  const result = evaluate(input);
  assert.equal(result.providerCoverage[0].status, 'UNKNOWN');
  assert.ok(result.providerCoverage[0].reasons.includes('unmapped_api_operation'));
  noRemediation(result);
});

test('duplicate mappings or unknown entitlement block full-surface proposals', async t => {
  for (const [name, damage] of [
    ['duplicate mapping', p => { p.capabilityMap.operations.push(structuredClone(p.capabilityMap.operations[0])); }],
    ['unknown entitlement', p => { delete p.capabilityMap.operations[2].entitled; }],
  ]) await t.test(name, () => {
    const input = packageFixture(); damage(input.providers[0]);
    const result = evaluate(input);
    assert.equal(result.providerCoverage[0].status, 'UNKNOWN');
    noRemediation(result);
  });
});

test('legacy per-feature open cards are reused as one package update, including at the daily cap', () => {
  const input = packageFixture(); input.newCardsAlreadyCreated = 3;
  input.cards.push(
    { id: 'old-read', group: 'forge', gapClass: 'missing_group_package', gapKey: 'legacy-read-key', targetScope: 'repo:issues', tier: 'read', status: 'blocked' },
    { id: 'old-write', group: 'forge', gapClass: 'missing_group_package', gapKey: 'legacy-write-key', targetScope: 'repo:issues', tier: 'write', status: 'deferred' },
    { id: 'closed-old', group: 'forge', gapClass: 'missing_group_package', status: 'done' },
    { id: 'unrelated', group: 'another-provider', gapClass: 'missing_group_package', status: 'backlog' },
  );
  const result = evaluate(input);
  assert.deepEqual(result.proposals, []);
  assert.equal(result.updates.length, 1);
  assert.deepEqual(result.updates[0].existingCardIds, ['old-read', 'old-write']);
  assert.equal(result.updates[0].targetScope, 'full-surface');
  assert.deepEqual(result.deferred, []);
});

test('closed, cancelled and done cards do not suppress a genuinely new gap', () => {
  const input = packageFixture();
  for (const status of ['closed', 'cancelled', 'done']) input.cards.push({ id: `card-${status}`, group: 'forge', gapClass: 'missing_group_package', status });
  const result = evaluate(input);
  assert.equal(result.proposals.length, 1);
  assert.deepEqual(result.updates, []);
});

test('unknown existing-card coverage defers new proposals rather than assuming no open card', () => {
  const input = packageFixture(); input.coverage.cards.errors = ['403 denied'];
  const result = evaluateAccessGaps(input);
  noRemediation(result);
  assert.equal(result.deferred.length, 1);
  assert.equal(result.deferred[0].reason, 'unknown_existing_cards');
});

test('partial card snapshots do not authorize updates to a supposedly canonical existing card', () => {
  const input = packageFixture();
  input.cards.push({ id: 'possibly-existing', group: 'forge', gapClass: 'missing_group_package', status: 'backlog' });
  seal(input); input.coverage.cards.truncated = true;
  noRemediation(evaluateAccessGaps(input));
});

test('the global three-new-card cap includes the already-created daily count across groups', async t => {
  for (const alreadyCreated of [0, 1, 2, 3]) await t.test(`already created ${alreadyCreated}`, () => {
    const input = fixture(); input.newCardsAlreadyCreated = alreadyCreated;
    for (const group of ['forge', 'docs', 'chat', 'storage', 'monitoring']) {
      input.providers.push(provider(group));
      addIncident(input, { suffix: `${group}-a`, group });
      addIncident(input, { suffix: `${group}-b`, group });
    }
    const result = evaluate(input);
    assert.equal(result.proposals.length, 3 - alreadyCreated);
    assert.equal(result.deferred.length, 2 + alreadyCreated);
    assert.ok(result.deferred.every(row => row.reason === 'daily_cap'));
    assert.equal(new Set([...result.proposals, ...result.deferred].map(row => row.gapKey)).size, 5);
    const reordered = structuredClone(input);
    STREAMS.forEach(stream => reordered[stream].reverse());
    assert.deepEqual(withoutInputHash(evaluate(reordered)), withoutInputHash(result));
  });
});

test('daily new-card accounting must be supplied as a bounded integer', () => {
  for (const count of [undefined, -1, 4, 0.5, '0', null]) {
    const input = fixture(); input.newCardsAlreadyCreated = count;
    assert.throws(() => evaluateAccessGaps(input), /daily card count/);
  }
});

test('one source run, repeated incidents and overlapping interaction/tool signals are insufficient', () => {
  const input = fixture(); input.providers.push(provider());
  const { incident, output } = addIncident(input);
  input.toolOutputs.push({ ...output, id: 'another-output', callId: 'another-call' });
  input.interactions.push(interaction('same-run-request', { sourceRunId: 'run-a' }));
  incident.evidenceKeys.push('tool:another-output', 'interaction:same-run-request');
  input.incidents.push({ ...structuredClone(incident), id: 'repeat-incident' });
  const result = evaluate(input);
  noRemediation(result);
  assert.equal(result.deferred.length, 1);
  assert.equal(result.deferred[0].reason, 'insufficient_distinct_source_events');
  assert.deepEqual(result.deferred[0].runs, ['run-a']);
});

test('independently reproduced material or explicit owner seed can support one-source proposals', async t => {
  for (const field of ['materialReproduced', 'ownerSeed']) await t.test(field, () => {
    const input = fixture(); input.providers.push(provider());
    addIncident(input, { [field]: true });
    const result = evaluate(input);
    assert.equal(result.proposals.length, 1);
    assert.equal(result.proposals[0][field], true);
    assert.equal(result.proposals[0].authorization, 'proposal_only');
  });
});

test('stock cards alone are not distinct request/failure events for new proposals', () => {
  const input = fixture(); input.providers.push(provider());
  input.cards.push({ id: 'stock-a', status: 'blocked', accessRelated: true }, { id: 'stock-b', status: 'backlog', accessRelated: true });
  addIncident(input, { evidenceKeys: ['card:stock-a', 'card:stock-b'] });
  const result = evaluate(input);
  noRemediation(result);
  assert.equal(result.deferred[0].reason, 'insufficient_distinct_source_events');
});

test('missing joins, scope/agent mismatches and missing independent validation fail closed', async t => {
  for (const [name, damage] of [
    ['missing evidence join', b => { b.incidents[0].evidenceKeys = ['tool:nonexistent']; }],
    ['unvalidated request', b => { b.incidents[0].validation.completeRequest = false; }],
    ['unvalidated policy', b => { b.incidents[0].validation.currentCapabilityPolicy = false; }],
    ['missing validation revision', b => { delete b.incidents[0].validation.revision; }],
    ['scope mismatch', b => { b.incidents[0].targetScope = 'all:resources'; }],
    ['tier mismatch', b => { b.incidents[0].tier = 'reserved'; }],
    ['agent mismatch', b => { b.incidents[0].agentId = 'agent-b'; }],
    ['not entitled', b => { b.providers[0].capabilityMap.operations[0].entitled = false; }],
  ]) await t.test(name, () => {
    const input = fixture(); input.providers.push(provider()); addIncident(input, { ownerSeed: true });
    damage(input);
    const result = evaluate(input);
    noRemediation(result);
    assert.equal(result.refused.length, 1);
  });
});

test('incomplete incident, provider or linked event coverage cannot support proposals', async t => {
  for (const stream of ['incidents', 'providers', 'toolOutputs']) await t.test(stream, () => {
    const input = packageFixture(); input.coverage[stream].errors = ['denied'];
    noRemediation(evaluateAccessGaps(input));
  });
});

test('uncovered run membership cannot authorize a tool-derived proposal', () => {
  const input = packageFixture(); input.coverage.runs.errors = ['403 denied'];
  noRemediation(evaluateAccessGaps(input));
});

test('unknown, mixed, host-only, board-only and credential-owner requests never propose broader access', async t => {
  for (const kinds of [[], ['unsupported'], ['scoped_tool_use', 'host_os'], ['host_os'], ['board_setting'], ['credential_create_rotate']]) {
    await t.test(JSON.stringify(kinds), () => {
      const input = fixture(); input.providers.push(provider());
      input.interactions.push(interaction('request-a', { requestEvidence: { complete: true, sourceInteractionId: 'request-a', kinds } }));
      addIncident(input, { evidenceKeys: ['interaction:request-a'], ownerSeed: true });
      const result = evaluate(input);
      noRemediation(result);
      assert.equal(result.refused[0].reason, 'not_agent_doable_evidence');
    });
  }
});

test('platform connection requests are counted but never constitute permission authority', () => {
  const input = fixture(); input.providers.push(provider());
  input.interactions.push(interaction('connection-a', { kind: 'connection_request' }));
  addIncident(input, { evidenceKeys: ['interaction:connection-a'], ownerSeed: true });
  const result = evaluate(input);
  assert.equal(result.metrics.operatorInteractions.observed, 1);
  assert.equal(result.interactions[0].platformConnectionRequest, true);
  noRemediation(result);
  assert.equal(result.refused[0].reason, 'repair_or_refusal_not_permission');
});

function adoptionFixture() {
  const input = fixture();
  input.cardEvents.push(
    { id: 'enabled-a', cardId: 'card-a', kind: 'enabled', at: at(1), authorized: true, version: 'v2', tier: 'write', agentId: 'agent-a' },
    { id: 'adopted-a', cardId: 'card-a', kind: 'adopted', at: DURING, authorized: true, success: true, version: 'v2', tier: 'write', agentId: 'agent-a' },
  );
  return seal(input);
}

test('adoption needs earlier authorized enablement for the exact card, version, tier and agent', () => {
  const result = evaluateAccessGaps(adoptionFixture());
  assert.deepEqual(result.outcomes, [{ id: 'adopted-a', cardId: 'card-a', kind: 'adopted', at: DURING, status: 'evidenced' }]);
  assert.match(result.authority, /No .* ledger state grants permission/);
});

test('missing or mismatched enabled evidence leaves adoption UNKNOWN', async t => {
  for (const [name, damage] of [
    ['no enablement', b => { b.cardEvents.shift(); }],
    ['different card', b => { b.cardEvents[0].cardId = 'card-b'; }],
    ['different version', b => { b.cardEvents[0].version = 'v1'; }],
    ['different tier', b => { b.cardEvents[0].tier = 'read'; }],
    ['different agent', b => { b.cardEvents[0].agentId = 'agent-b'; }],
    ['missing version', b => { delete b.cardEvents[0].version; }],
    ['missing tier', b => { delete b.cardEvents[0].tier; }],
    ['missing agent', b => { delete b.cardEvents[0].agentId; }],
    ['unauthorized enablement', b => { b.cardEvents[0].authorized = false; }],
    ['enablement after adoption', b => { b.cardEvents[0].at = at(2, '13:00:00.000'); }],
    ['enablement after asOf', b => { b.cardEvents[0].at = at(4); }],
    ['unauthorized adoption', b => { b.cardEvents[1].authorized = false; }],
    ['failed adoption', b => { b.cardEvents[1].success = false; }],
    ['success not supplied', b => { delete b.cardEvents[1].success; }],
  ]) await t.test(name, () => {
    const input = adoptionFixture(); damage(input);
    const result = evaluate(input);
    assert.equal(result.outcomes.find(row => row.id === 'adopted-a').status, 'UNKNOWN');
  });
});

test('an incomplete enablement/adoption event source cannot prove adoption', () => {
  const input = adoptionFixture(); input.coverage.cardEvents.errors = ['403 denied'];
  const result = evaluateAccessGaps(input);
  assert.equal(result.coverage.cardEvents.status, 'UNKNOWN');
  assert.equal(result.outcomes.find(row => row.id === 'adopted-a').status, 'UNKNOWN');
});
