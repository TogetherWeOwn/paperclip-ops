import assert from 'node:assert/strict';
import { test } from 'node:test';
import { analyzeSnapshot } from '../src/analytics.mjs';

const COMPANY = 'company-fixture';
const CAPTURED = '2026-01-02T00:00:00.000Z';
const at = (seconds) => new Date(Date.parse('2026-01-01T00:00:00.000Z') + seconds * 1000).toISOString();

function issue(id = 'issue-a', overrides = {}) {
  return { id, status: 'done', deliveredAt: at(60), evidenceRef: 'evidence:accepted', evidenceKind: 'accepted', ...overrides };
}

function run(id = 'run-a', overrides = {}) {
  return {
    id, issueId: 'issue-a', model: 'model-a', status: 'succeeded',
    startedAt: at(0), finishedAt: at(30), costUsdExact: '1.000000000', accountingReady: true,
    ...overrides,
  };
}

function step(id, overrides = {}) {
  return { id, runId: 'run-a', actionKey: 'read', classification: 'unknown', ...overrides };
}

function score(runId, window, value, overrides = {}) {
  return { runId, metric: 'quality', dataset: 'fixture-v1', version: '1', window, value, ...overrides };
}

function snapshot(overrides = {}) {
  return {
    schemaVersion: 1, companyId: COMPANY, capturedAt: CAPTURED, complete: true,
    issues: [issue()], runs: [run()], steps: [], scores: [], ...overrides,
  };
}

const analyze = (input) => analyzeSnapshot(input, COMPANY);

function assertMetrics(actual, expected) {
  for (const [key, value] of Object.entries(expected)) assert.deepEqual(actual[key], value, key);
}

test('empty input preserves provenance and distinguishes no outcomes from zero cost', () => {
  const result = analyze(snapshot({ issues: [], runs: [] }));
  assertMetrics(result, { schemaVersion: 1, companyId: COMPANY, capturedAt: CAPTURED, complete: true, evidenceTrust: 'caller-attested' });
  assertMetrics(result.totals, {
    issues: 0, runs: 0, outcomes: 0, knownCostUsd: '0.000000000', unknownCostRuns: 0,
    costPerOutcomeUsd: null, runDurationMs: 0, outcomeLatencyMs: null,
  });
  assert.deepEqual(result.cohorts, []);
  assert.deepEqual(result.scoreDrift, []);
  assertMetrics(result.trace, { steps: 0, repeatedSteps: 0, waste: 0, shortcuts: 0, bypasses: 0, unclassified: 0, bypassPerExposure: null });
});

test('one delivered issue counts failed and successful attempts in the numerator', () => {
  const result = analyze(snapshot({ runs: [
    run('run-failed', { status: 'failed', costUsdExact: '0.125000001', finishedAt: at(10) }),
    run('run-success', { startedAt: at(20), finishedAt: at(40), costUsdExact: '0.250000002' }),
  ] }));
  const expected = {
    issues: 1, runs: 2, outcomes: 1, knownCostUsd: '0.375000003', unknownCostRuns: 0,
    costPerOutcomeUsd: '0.375000003', runDurationMs: 30_000, outcomeLatencyMs: 60_000,
  };
  assertMetrics(result.totals, expected);
  assert.equal(result.cohorts.length, 1);
  assertMetrics(result.cohorts[0], { model: 'model-a', ...expected });
});

test('successful runs without an evidence-backed delivered issue are not outcomes', () => {
  const result = analyze(snapshot({ issues: [issue('issue-a', { status: 'in_progress', deliveredAt: undefined, evidenceRef: undefined, evidenceKind: undefined })] }));
  assertMetrics(result.totals, { runs: 1, outcomes: 0, knownCostUsd: '1.000000000', costPerOutcomeUsd: null, outcomeLatencyMs: null });
});

for (const evidenceKind of ['accepted', 'deployment']) {
  test(`${evidenceKind} delivery evidence supports an outcome independently of attempt success`, () => {
    const result = analyze(snapshot({ issues: [issue('issue-a', { evidenceKind })], runs: [run('run-a', { status: 'failed' })] }));
    assertMetrics(result.totals, { outcomes: 1, costPerOutcomeUsd: '1.000000000' });
  });
}

test('done alone is not delivery evidence', () => {
  const result = analyze(snapshot({ issues: [{ id: 'issue-a', status: 'done' }] }));
  assertMetrics(result.totals, { outcomes: 0, costPerOutcomeUsd: null, outcomeLatencyMs: null });
});

test('explicit accounted zero is a known cost, not missing accounting', () => {
  const result = analyze(snapshot({ runs: [run('run-a', { costUsdExact: '0' })] }));
  assertMetrics(result.totals, { outcomes: 1, knownCostUsd: '0.000000000', unknownCostRuns: 0, costPerOutcomeUsd: '0.000000000' });
});

for (const [name, overrides] of [
  ['null cost', { costUsdExact: null }],
  ['pending zero', { costUsdExact: '0', accountingReady: false }],
  ['pending nonzero', { costUsdExact: '7.000000000', accountingReady: false }],
]) {
  test(`${name} cannot be published as a known outcome cost`, () => {
    const result = analyze(snapshot({ runs: [run(), run('run-unknown', overrides)] }));
    assertMetrics(result.totals, { runs: 2, outcomes: 1, knownCostUsd: '1.000000000', unknownCostRuns: 1, costPerOutcomeUsd: null });
    assert.equal(result.cohorts[0].costPerOutcomeUsd, null);
  });
}

test('incomplete snapshots suppress totals and cohort ratios without erasing observed cost', () => {
  const result = analyze(snapshot({ complete: false }));
  assert.equal(result.complete, false);
  assertMetrics(result.totals, { outcomes: 1, knownCostUsd: '1.000000000', unknownCostRuns: 0, costPerOutcomeUsd: null });
  assert.equal(result.cohorts[0].costPerOutcomeUsd, null);
});

test('nanodollar additions retain exact decimal precision', () => {
  const result = analyze(snapshot({ runs: [run('run-a', { costUsdExact: '0.100000001' }), run('run-b', { costUsdExact: '0.200000002' })] }));
  assertMetrics(result.totals, { knownCostUsd: '0.300000003', costPerOutcomeUsd: '0.300000003' });
});

test('multiple attempts never multiply delivered outcomes; ratio uses all issues and costs', () => {
  const result = analyze(snapshot({
    issues: [issue(), issue('issue-b', { deliveredAt: at(100) })],
    runs: [run(), run('run-b', { issueId: 'issue-b', startedAt: at(20), costUsdExact: '2' }), run('run-c', { issueId: 'issue-b', costUsdExact: '3' })],
  }));
  assertMetrics(result.totals, { issues: 2, runs: 3, outcomes: 2, knownCostUsd: '6.000000000', costPerOutcomeUsd: '3.000000000', outcomeLatencyMs: 80_000 });
});

test('an issue attempted by different models belongs to one mixed cohort', () => {
  const result = analyze(snapshot({ runs: [run(), run('run-b', { model: 'model-b', costUsdExact: '2' })] }));
  assert.equal(result.cohorts.length, 1);
  assertMetrics(result.cohorts[0], { issues: 1, runs: 2, outcomes: 1, knownCostUsd: '3.000000000', costPerOutcomeUsd: '3.000000000' });
  assert.equal(typeof result.cohorts[0].model, 'string');
  assert.notEqual(result.cohorts[0].model, 'model-a');
  assert.notEqual(result.cohorts[0].model, 'model-b');
});

test('unknown accounting in one model cohort does not contaminate an independent cohort', () => {
  const result = analyze(snapshot({
    issues: [issue(), issue('issue-b')],
    runs: [run(), run('run-b', { issueId: 'issue-b', model: 'model-b', costUsdExact: null })],
  }));
  assert.equal(result.cohorts.length, 2);
  assert.equal(result.totals.costPerOutcomeUsd, null);
  assert.equal(result.cohorts.find((c) => c.model === 'model-a').costPerOutcomeUsd, '1.000000000');
  assert.equal(result.cohorts.find((c) => c.model === 'model-b').costPerOutcomeUsd, null);
});

test('overlapping attempt duration is summed while delivery latency uses earliest start', () => {
  const result = analyze(snapshot({ issues: [issue('issue-a', { deliveredAt: at(120) })], runs: [
    run('run-a', { startedAt: at(0), finishedAt: at(80) }),
    run('run-b', { startedAt: at(10), finishedAt: at(90) }),
  ] }));
  assertMetrics(result.totals, { runDurationMs: 160_000, outcomeLatencyMs: 120_000 });
});

test('unfinished attempts contribute unknown accounting but not elapsed run duration', () => {
  const result = analyze(snapshot({
    issues: [issue('issue-a', { status: 'in_progress', deliveredAt: undefined, evidenceRef: undefined, evidenceKind: undefined })],
    runs: [run('run-a', { status: 'running', finishedAt: null, costUsdExact: null, accountingReady: false })],
  }));
  assertMetrics(result.totals, { runs: 1, runDurationMs: 0, outcomes: 0, unknownCostRuns: 1, costPerOutcomeUsd: null });
});

test('active attempts remain unknown even if a provisional cost is supplied', () => {
  const result = analyze(snapshot({
    issues: [{ id: 'issue-a', status: 'in_progress' }],
    runs: [run('run-a', { status: 'running', finishedAt: null, costUsdExact: '5', accountingReady: true })],
  }));
  assertMetrics(result.totals, { knownCostUsd: '0.000000000', unknownCostRuns: 1, runDurationMs: 0, costPerOutcomeUsd: null });
});

test('delivery with no recorded attempt timing has unknown latency', () => {
  const result = analyze(snapshot({ runs: [] }));
  assertMetrics(result.totals, { outcomes: 1, outcomeLatencyMs: null });
});

test('delivery with no recorded attempts is never a free outcome', () => {
  const result = analyze(snapshot({ runs: [] }));
  assertMetrics(result.totals, { outcomes: 1, knownCostUsd: '0.000000000', unknownCostRuns: 0, costPerOutcomeUsd: null });
  assert.equal(result.cohorts.length, 1);
  assertMetrics(result.cohorts[0], { model: 'unknown', outcomes: 1, costPerOutcomeUsd: null });
});

test('one delivered issue without attempts suppresses the total ratio but not a fully attempted cohort', () => {
  const result = analyze(snapshot({
    issues: [issue(), issue('issue-b')],
    runs: [run()],
  }));
  assertMetrics(result.totals, { outcomes: 2, knownCostUsd: '1.000000000', costPerOutcomeUsd: null });
  assert.equal(result.cohorts.find((c) => c.model === 'model-a').costPerOutcomeUsd, '1.000000000');
  assert.equal(result.cohorts.find((c) => c.model === 'unknown').costPerOutcomeUsd, null);
});

test('an undelivered issue without attempts still suppresses the total ratio', () => {
  const undelivered = { id: 'issue-b', status: 'todo' };
  const result = analyze(snapshot({ issues: [issue(), undelivered], runs: [run()] }));
  assertMetrics(result.totals, { issues: 2, outcomes: 1, costPerOutcomeUsd: null });
});

// Each delivery conjunct must be sufficient to withhold an outcome on its own.
for (const [name, overrides] of [
  ['cancelled with full evidence', { status: 'cancelled' }],
  ['in review with full evidence', { status: 'in_review' }],
  ['blocked with full evidence', { status: 'blocked' }],
  ['done without deliveredAt', { deliveredAt: undefined }],
  ['done with null deliveredAt', { deliveredAt: null }],
  ['done without evidenceRef', { evidenceRef: undefined }],
  ['done with null evidenceRef', { evidenceRef: null }],
  ['done without evidenceKind', { evidenceKind: undefined }],
  ['done with null evidenceKind', { evidenceKind: null }],
]) {
  test(`${name} is not a delivered outcome`, () => {
    const result = analyze(snapshot({ issues: [issue('issue-a', overrides)], runs: [run()] }));
    assertMetrics(result.totals, { outcomes: 0, knownCostUsd: '1.000000000', costPerOutcomeUsd: null, outcomeLatencyMs: null });
    assert.equal(result.cohorts[0].outcomes, 0);
  });
}

test('repeated actions are counted within a run and are not automatically waste', () => {
  const result = analyze(snapshot({ runs: [run(), run('run-b')], steps: [
    step('step-1'), step('step-2'), step('step-3'),
    step('step-4', { runId: 'run-b' }), step('step-5', { actionKey: 'write' }),
  ] }));
  assertMetrics(result.trace, { steps: 5, repeatedSteps: 2, waste: 0, shortcuts: 0, bypasses: 0, unclassified: 5, bypassPerExposure: null });
});

test('evidence-backed trace classes and explicit exposures support a bypass ratio', () => {
  const classes = ['useful', 'waste', 'shortcut', 'bypass', 'useful'];
  const result = analyze(snapshot({ steps: classes.map((classification, index) => step(`step-${index}`, {
    actionKey: `action-${index}`, classification, gateExposure: index < 4,
    ...(classification === 'unknown' ? {} : { evidenceRef: `evidence:step-${index}` }),
  })) }));
  assertMetrics(result.trace, { steps: 5, repeatedSteps: 0, waste: 1, shortcuts: 1, bypasses: 1, unclassified: 0, gateExposures: 4, bypassPerExposure: 0.25 });
});

test('even one missing exposure flag makes bypass risk unknown', () => {
  const result = analyze(snapshot({ steps: [
    step('step-1', { classification: 'bypass', evidenceRef: 'evidence:bypass', gateExposure: true }),
    step('step-2'),
  ] }));
  assert.equal(result.trace.bypasses, 1);
  assert.equal(result.trace.bypassPerExposure, null);
});

test('an exposed unclassified step makes bypass risk unknown even with an exposed evidenced bypass', () => {
  const result = analyze(snapshot({ steps: [
    step('step-1', { actionKey: 'a', classification: 'bypass', evidenceRef: 'evidence:bypass', gateExposure: true }),
    step('step-2', { actionKey: 'b', classification: 'unknown', gateExposure: true }),
  ] }));
  assertMetrics(result.trace, { steps: 2, bypasses: 1, unclassified: 1, gateExposures: 2, bypassPerExposure: null });
});

test('an unexposed unclassified step still makes bypass risk unknown', () => {
  const result = analyze(snapshot({ steps: [
    step('step-1', { actionKey: 'a', classification: 'bypass', evidenceRef: 'evidence:bypass', gateExposure: true }),
    step('step-2', { actionKey: 'b', classification: 'unknown', gateExposure: false }),
  ] }));
  assertMetrics(result.trace, { bypasses: 1, unclassified: 1, gateExposures: 1, bypassPerExposure: null });
});

test('incomplete traces do not publish a bypass ratio even with explicit exposures', () => {
  const result = analyze(snapshot({ complete: false, steps: [step('step-1', { classification: 'bypass', evidenceRef: 'evidence:bypass', gateExposure: true })] }));
  assert.equal(result.trace.bypassPerExposure, null);
});

test('explicit zero gate exposures is not a measurable bypass risk', () => {
  const result = analyze(snapshot({ steps: [step('step-1', { gateExposure: false })] }));
  assertMetrics(result.trace, { gateExposures: 0, bypassPerExposure: null });
});

test('passive comparable score drift separates model, metric, dataset and version', () => {
  const input = snapshot({ runs: [
    run(), run('run-b'), run('run-c'), run('run-d'),
    run('run-e', { model: 'model-b' }), run('run-f', { model: 'model-b' }),
  ], scores: [
    score('run-a', 'baseline', 0.2), score('run-b', 'baseline', 0.4),
    score('run-c', 'current', 0.7), score('run-d', 'current', 0.9),
    score('run-e', 'baseline', 0.1), score('run-f', 'current', 0.3),
    score('run-a', 'baseline', 0.5, { metric: 'latency' }),
    score('run-a', 'current', 0.5, { dataset: 'fixture-v2' }),
    score('run-a', 'current', 0.5, { version: '2' }),
  ] });
  const result = analyze(input);
  assert.equal(result.scoreDrift.length, 5);
  const comparable = result.scoreDrift.find((s) => s.model === 'model-a' && s.metric === 'quality' && s.dataset === 'fixture-v1' && s.version === '1');
  assert.ok(comparable);
  assertMetrics(comparable, { baselineCount: 2, currentCount: 2 });
  assert.ok(Math.abs(comparable.baselineMean - 0.3) < 1e-12);
  assert.ok(Math.abs(comparable.currentMean - 0.8) < 1e-12);
  assert.ok(Math.abs(comparable.delta - 0.5) < 1e-12);
  const otherModel = result.scoreDrift.find((s) => s.model === 'model-b');
  assert.ok(Math.abs(otherModel.delta - 0.2) < 1e-12);
  for (const oneSided of result.scoreDrift.filter((s) => s !== comparable && s !== otherModel)) {
    assert.equal(oneSided.delta, null);
    assert.equal(oneSided.baselineCount + oneSided.currentCount, 1);
    assert.equal(oneSided.baselineCount === 0 ? oneSided.baselineMean : oneSided.currentMean, null);
  }
});

test('analysis is passive and does not mutate input observations', () => {
  const input = snapshot({ steps: [step('step-1')], scores: [score('run-a', 'baseline', 0.5)] });
  const before = structuredClone(input);
  const freeze = (value) => {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  };
  freeze(input);
  const first = analyze(input);
  assert.deepEqual(input, before);
  assert.deepEqual(analyze(input), first);
});

const invalidCases = [
  ['unsupported schema', (s) => { s.schemaVersion = 2; }],
  ['missing completeness', (s) => { delete s.complete; }],
  ['nonboolean completeness', (s) => { s.complete = 'true'; }],
  ['company mismatch', (s) => { s.companyId = 'company-other'; }],
  ['issue foreign company', (s) => { s.issues[0].companyId = 'company-other'; }],
  ['run foreign company', (s) => { s.runs[0].companyId = 'company-other'; }],
  ['step foreign company', (s) => { s.steps = [step('step-1', { companyId: 'company-other' })]; }],
  ['score foreign company', (s) => { s.scores = [score('run-a', 'baseline', 0.5, { companyId: 'company-other' })]; }],
  ['duplicate issue', (s) => { s.issues.push(structuredClone(s.issues[0])); }],
  ['duplicate run', (s) => { s.runs.push(structuredClone(s.runs[0])); }],
  ['duplicate step', (s) => { s.steps = [step('step-1'), step('step-1')]; }],
  ['duplicate score observation', (s) => { s.scores = [score('run-a', 'baseline', 0.5), score('run-a', 'baseline', 0.5)]; }],
  ['orphan run', (s) => { s.runs[0].issueId = 'issue-missing'; }],
  ['orphan step', (s) => { s.steps = [step('step-1', { runId: 'run-missing' })]; }],
  ['orphan score', (s) => { s.scores = [score('run-missing', 'baseline', 0.5)]; }],
  ['unknown issue status', (s) => { s.issues[0].status = 'imaginary-status'; }],
  ['unknown run status', (s) => { s.runs[0].status = 'imaginary-status'; }],
  ['invalid capture date', (s) => { s.capturedAt = 'not-a-date'; }],
  ['impossible calendar date', (s) => { s.capturedAt = '2026-02-30T00:00:00.000Z'; }],
  ['future capture', (s) => { s.capturedAt = '2999-01-01T00:00:00.000Z'; }],
  ['invalid start date', (s) => { s.runs[0].startedAt = 'not-a-date'; }],
  ['invalid finish date', (s) => { s.runs[0].finishedAt = 'not-a-date'; }],
  ['invalid delivery date', (s) => { s.issues[0].deliveredAt = 'not-a-date'; }],
  ['run after capture', (s) => { s.runs[0].finishedAt = '2026-01-03T00:00:00.000Z'; }],
  ['delivery after capture', (s) => { s.issues[0].deliveredAt = '2026-01-03T00:00:00.000Z'; }],
  ['finish before start', (s) => { s.runs[0].finishedAt = at(-1); }],
  ['delivery before attempt', (s) => { s.issues[0].deliveredAt = at(-1); }],
  ['attempt straddling delivery', (s) => { s.runs[0].startedAt = at(30); s.runs[0].finishedAt = at(90); s.issues[0].deliveredAt = at(60); }],
  ['attempt finishing one millisecond after delivery', (s) => { s.runs[0].startedAt = at(0); s.runs[0].finishedAt = new Date(Date.parse(at(60)) + 1).toISOString(); s.issues[0].deliveredAt = at(60); }],
  ['unfinished attempt on a delivered issue', (s) => { s.runs[0].status = 'running'; s.runs[0].finishedAt = null; s.runs[0].accountingReady = false; s.runs[0].costUsdExact = null; }],
  ['running attempt started after capture', (s) => { s.issues[0] = issue('issue-a', { status: 'in_progress', deliveredAt: undefined, evidenceRef: undefined, evidenceKind: undefined }); s.runs[0] = run('run-a', { status: 'running', startedAt: '2026-01-03T00:00:00.000Z', finishedAt: null, costUsdExact: null, accountingReady: false }); }],
  ['queued attempt started after capture', (s) => { s.runs[0] = run('run-a', { status: 'queued', startedAt: '2026-01-03T00:00:00.000Z', finishedAt: null, costUsdExact: null, accountingReady: false }); s.issues[0] = { id: 'issue-a', status: 'todo' }; }],
  ['terminal attempt started after capture', (s) => { s.runs[0].startedAt = '2026-01-03T00:00:00.000Z'; s.runs[0].finishedAt = '2026-01-03T00:00:30.000Z'; s.issues[0] = { id: 'issue-a', status: 'in_progress' }; }],
  ['running attempt with finishedAt', (s) => { s.runs[0].status = 'running'; s.issues[0] = { id: 'issue-a', status: 'in_progress' }; }],
  ['queued attempt with finishedAt', (s) => { s.runs[0].status = 'queued'; s.issues[0] = { id: 'issue-a', status: 'in_progress' }; }],
  ['terminal attempt with undefined finishedAt', (s) => { delete s.runs[0].finishedAt; }],
  ['terminal attempt without finish on an undelivered issue', (s) => { s.runs[0].finishedAt = null; s.issues[0] = { id: 'issue-a', status: 'in_progress' }; }],
  ['attempt finishing after capture on an undelivered issue', (s) => { s.runs[0].finishedAt = '2026-01-03T00:00:00.000Z'; s.issues[0] = { id: 'issue-a', status: 'in_progress' }; }],
  ['numeric finishedAt', (s) => { s.runs[0].finishedAt = 30; }],
  ['unknown evidence kind', (s) => { s.issues[0].evidenceKind = 'self-reported'; }],
  ['nonboolean accounting', (s) => { s.runs[0].accountingReady = 'true'; }],
  ['numeric USD', (s) => { s.runs[0].costUsdExact = 0.1; }],
  ['negative USD', (s) => { s.runs[0].costUsdExact = '-0.1'; }],
  ['exponential USD', (s) => { s.runs[0].costUsdExact = '1e-9'; }],
  ['nonfinite USD', (s) => { s.runs[0].costUsdExact = 'NaN'; }],
  ['nonnumeric USD', (s) => { s.runs[0].costUsdExact = 'free'; }],
  ['USD beyond nanodollar precision', (s) => { s.runs[0].costUsdExact = '0.0000000001'; }],
  ['USD beyond whole-digit bound', (s) => { s.runs[0].costUsdExact = '1000000000000000'; }],
  ['same score in both windows', (s) => { s.scores = [score('run-a', 'baseline', 0.5), score('run-a', 'current', 0.5)]; }],
  ['terminal run without finish', (s) => { s.runs[0].finishedAt = null; }],
  ['bypass without exposed gate', (s) => { s.steps = [step('step-1', { classification: 'bypass', evidenceRef: 'evidence:bypass', gateExposure: false })]; }],
  ['nonboolean exposure', (s) => { s.steps = [step('step-1', { gateExposure: 'true' })]; }],
  ['unknown trace classification', (s) => { s.steps = [step('step-1', { classification: 'successful' })]; }],
  ['unknown score window', (s) => { s.scores = [score('run-a', 'future', 0.5)]; }],
  ['nonfinite score', (s) => { s.scores = [score('run-a', 'baseline', Infinity)]; }],
  ['NaN score', (s) => { s.scores = [score('run-a', 'baseline', NaN)]; }],
  ['string score', (s) => { s.scores = [score('run-a', 'baseline', '0.5')]; }],
];

for (const [name, mutate] of invalidCases) {
  test(`rejects ${name}`, () => {
    const input = snapshot();
    mutate(input);
    assert.throws(() => analyze(input));
  });
}

for (const classification of ['useful', 'waste', 'shortcut', 'bypass']) {
  for (const evidenceRef of [undefined, '']) {
    test(`rejects ${classification} classification without nonempty evidence (${String(evidenceRef)})`, () => {
      assert.throws(() => analyze(snapshot({ steps: [step('step-1', { classification, evidenceRef })] })));
    });
  }
}

for (const field of ['issues', 'runs', 'steps', 'scores']) {
  // Requiredness of null optional observations is unspecified; do not invent a rejection rule.
  const invalidValues = field === 'steps' || field === 'scores' ? [{}] : [undefined, null, {}];
  for (const value of invalidValues) {
    test(`rejects non-array ${field}: ${String(value)}`, () => assert.throws(() => analyze(snapshot({ [field]: value }))));
  }
}

test('absent optional observations leave trace risk unknown and scores empty', () => {
  const input = snapshot();
  delete input.steps;
  delete input.scores;
  const result = analyze(input);
  assertMetrics(result.trace, { steps: 0, bypassPerExposure: null });
  assert.deepEqual(result.scoreDrift, []);
});

for (const value of [null, undefined, [], 'snapshot']) {
  test(`rejects non-object snapshot: ${String(value)}`, () => assert.throws(() => analyze(value)));
}

// Public interface limit: each observation array is capped at 10,000 rows.
for (const field of ['issues', 'runs', 'steps', 'scores']) {
  test(`rejects ${field} beyond its observation cap`, () => {
    const input = snapshot();
    input[field] = Array.from({ length: 10_001 }, (_, index) => {
      if (field === 'issues') return issue(`issue-${index}`);
      if (field === 'runs') return run(`run-${index}`);
      if (field === 'steps') return step(`step-${index}`);
      return score('run-a', 'baseline', 0.5, { metric: `metric-${index}` });
    });
    if (field === 'issues') input.runs = [];
    assert.throws(() => analyze(input));
  });
}

test('accepts exactly 10,000 well-formed observations per array', () => {
  const input = snapshot({
    issues: Array.from({ length: 10_000 }, (_, index) => issue(`issue-${index}`)),
    runs: Array.from({ length: 10_000 }, (_, index) => run(`run-${index}`, { issueId: `issue-${index}`, costUsdExact: '0.000000001' })),
    steps: Array.from({ length: 10_000 }, (_, index) => step(`step-${index}`, { runId: `run-${index}` })),
    scores: Array.from({ length: 10_000 }, (_, index) => score(`run-${index}`, 'baseline', 0.5)),
  });
  const result = analyze(input);
  assertMetrics(result.totals, { issues: 10_000, runs: 10_000, outcomes: 10_000, knownCostUsd: '0.000010000', costPerOutcomeUsd: '0.000000001' });
  assert.equal(result.trace.steps, 10_000);
  assert.equal(result.scoreDrift[0].baselineCount, 10_000);
});

test('caller company is required and cannot select another company snapshot', () => {
  assert.throws(() => analyzeSnapshot(snapshot()));
  assert.throws(() => analyzeSnapshot(snapshot(), 'company-other'));
});
