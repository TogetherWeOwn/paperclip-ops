const SCALE = 1_000_000_000n;
const LIMIT = 10_000;
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'timed_out']);
const RUN_STATUSES = new Set([...TERMINAL, 'queued', 'running']);
const ISSUE_STATUSES = new Set(['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']);
const CLASSES = new Set(['useful', 'waste', 'shortcut', 'bypass', 'unknown']);

function requireValue(ok, reason) {
  if (!ok) throw new TypeError(reason);
}

function text(value, field) {
  requireValue(typeof value === 'string' && value.trim().length > 0 && value.length <= 512, `Invalid ${field}`);
  return value;
}

function timestamp(value, field) {
  requireValue(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$/.test(value), `Invalid ${field}`);
  const result = Date.parse(value);
  requireValue(Number.isFinite(result), `Invalid ${field}`);
  // Date.parse normalizes impossible calendar dates such as February 30.
  const calendar = value.slice(0, 10);
  requireValue(new Date(`${calendar}T00:00:00Z`).toISOString().slice(0, 10) === calendar, `Invalid ${field}`);
  return result;
}

function usd(value) {
  requireValue(typeof value === 'string' && /^(0|[1-9]\d{0,14})(\.\d{1,9})?$/.test(value), 'Invalid costUsdExact');
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole) * SCALE + BigInt(fraction.padEnd(9, '0'));
}

function money(value) {
  return `${value / SCALE}.${(value % SCALE).toString().padStart(9, '0')}`;
}

function rows(value, name) {
  requireValue(Array.isArray(value) && value.length <= LIMIT, `Invalid or oversized ${name}`);
  return value;
}

function record(value, companyId) {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value), 'Expected record');
  requireValue(value.companyId === undefined || value.companyId === companyId, 'Cross-company record');
}

function index(records, companyId, kind) {
  const result = new Map();
  for (const item of records) {
    record(item, companyId);
    const id = text(item.id, `${kind} id`);
    requireValue(!result.has(id), `Duplicate ${kind} id`);
    result.set(id, item);
  }
  return result;
}

function summary(issues, runs, complete, delivered) {
  let knownCost = 0n;
  let unknownCostRuns = 0;
  let runDurationMs = 0;
  for (const run of runs) {
    if (run.accountingReady && TERMINAL.has(run.status) && run.costUsdExact !== null) knownCost += usd(run.costUsdExact);
    else unknownCostRuns++;
    if (run.finishedAt !== null) runDurationMs += Date.parse(run.finishedAt) - Date.parse(run.startedAt);
  }
  const outcomes = issues.filter((issue) => delivered.has(issue.id));
  const latencies = outcomes.map((issue) => {
    const starts = runs.filter((run) => run.issueId === issue.id).map((run) => Date.parse(run.startedAt));
    return starts.length ? Date.parse(issue.deliveredAt) - Math.min(...starts) : null;
  });
  return {
    issues: issues.length,
    runs: runs.length,
    outcomes: outcomes.length,
    knownCostUsd: money(knownCost),
    unknownCostRuns,
    // Integer division rounds down at one nanodollar, never through binary floating point.
    costPerOutcomeUsd: complete && !unknownCostRuns && outcomes.length && issues.every((issue) => runs.some((run) => run.issueId === issue.id))
      ? money(knownCost / BigInt(outcomes.length)) : null,
    runDurationMs,
    outcomeLatencyMs: outcomes.length && latencies.every((value) => value !== null)
      ? latencies.reduce((sum, value) => sum + value, 0) / outcomes.length : null,
  };
}

/** Analyze a bounded, explicitly selected native snapshot. Evidence remains caller-attested. */
export function analyzeSnapshot(snapshot, companyId) {
  text(companyId, 'company scope');
  record(snapshot, companyId);
  requireValue(snapshot.schemaVersion === 1 && snapshot.companyId === companyId, 'Invalid schemaVersion or company scope');
  requireValue(typeof snapshot.complete === 'boolean', 'complete must explicitly describe cohort coverage');
  const capturedAt = timestamp(snapshot.capturedAt, 'capturedAt');
  requireValue(capturedAt <= Date.now(), 'Future capturedAt');
  const issues = index(rows(snapshot.issues, 'issues'), companyId, 'issue');
  const runs = index(rows(snapshot.runs, 'runs'), companyId, 'run');
  const steps = index(rows(snapshot.steps ?? [], 'steps'), companyId, 'step');
  const scores = rows(snapshot.scores ?? [], 'scores');
  const delivered = new Set();
  for (const issue of issues.values()) {
    requireValue(ISSUE_STATUSES.has(issue.status), 'Invalid issue status');
    if (issue.deliveredAt !== undefined && issue.deliveredAt !== null) {
      requireValue(timestamp(issue.deliveredAt, 'deliveredAt') <= capturedAt, 'Future delivery');
    }
    if (issue.evidenceRef !== undefined && issue.evidenceRef !== null) text(issue.evidenceRef, 'evidenceRef');
    if (issue.evidenceKind !== undefined && issue.evidenceKind !== null) {
      requireValue(['accepted', 'deployment'].includes(issue.evidenceKind), 'Invalid outcome evidenceKind');
    }
    if (issue.status === 'done' && issue.deliveredAt && issue.evidenceRef && issue.evidenceKind) delivered.add(issue.id);
  }
  for (const run of runs.values()) {
    requireValue(issues.has(run.issueId), 'Orphan run');
    text(run.model, 'model');
    requireValue(RUN_STATUSES.has(run.status), 'Invalid run status');
    requireValue(typeof run.accountingReady === 'boolean', 'Invalid accountingReady');
    requireValue(run.costUsdExact === null || typeof run.costUsdExact === 'string', 'Missing costUsdExact');
    if (run.costUsdExact !== null) usd(run.costUsdExact);
    const start = timestamp(run.startedAt, 'startedAt');
    requireValue(start <= capturedAt, 'Future run');
    requireValue(run.finishedAt === null || typeof run.finishedAt === 'string', 'Missing finishedAt');
    if (run.finishedAt !== null) {
      const finish = timestamp(run.finishedAt, 'finishedAt');
      requireValue(finish >= start && finish <= capturedAt && TERMINAL.has(run.status), 'Invalid run interval or status');
    } else requireValue(!TERMINAL.has(run.status), 'Terminal run without finishedAt');
    const issue = issues.get(run.issueId);
    if (delivered.has(issue.id)) {
      requireValue(start <= Date.parse(issue.deliveredAt), 'Run starts after delivered outcome; split the cohort');
      requireValue(run.finishedAt !== null && Date.parse(run.finishedAt) <= Date.parse(issue.deliveredAt), 'Run extends beyond delivery; split the cohort');
    }
  }

  // Attribute a work unit once. Shared multi-model effort is not credited to each model.
  const groups = new Map();
  for (const issue of issues.values()) {
    const issueRuns = [...runs.values()].filter((run) => run.issueId === issue.id);
    const models = [...new Set(issueRuns.map((run) => run.model))].sort();
    const key = JSON.stringify(models);
    if (!groups.has(key)) groups.set(key, { model: models.length === 1 ? models[0] : models.length ? 'mixed' : 'unknown', models, issues: [], runs: [] });
    groups.get(key).issues.push(issue);
    groups.get(key).runs.push(...issueRuns);
  }
  const cohorts = [...groups.values()].map((group) => ({ model: group.model, models: group.models, ...summary(group.issues, group.runs, snapshot.complete, delivered) }));

  const trace = { steps: steps.size, repeatedSteps: 0, waste: 0, shortcuts: 0, bypasses: 0, unclassified: 0, gateExposures: 0, bypassPerExposure: null };
  const actions = new Set();
  let exposureComplete = snapshot.complete && steps.size > 0;
  for (const step of steps.values()) {
    requireValue(runs.has(step.runId), 'Orphan step');
    text(step.actionKey, 'actionKey');
    requireValue(CLASSES.has(step.classification), 'Invalid trace classification');
    requireValue(step.gateExposure === undefined || typeof step.gateExposure === 'boolean', 'Invalid gateExposure');
    if (step.classification !== 'unknown') text(step.evidenceRef, 'classification evidenceRef');
    requireValue(step.classification !== 'bypass' || step.gateExposure === true, 'Bypass requires gate exposure');
    const action = JSON.stringify([step.runId, step.actionKey]);
    if (actions.has(action)) trace.repeatedSteps++;
    actions.add(action);
    if (step.classification === 'waste') trace.waste++;
    if (step.classification === 'shortcut') trace.shortcuts++;
    if (step.classification === 'bypass') trace.bypasses++;
    if (step.classification === 'unknown') trace.unclassified++;
    if (step.gateExposure === true) trace.gateExposures++;
    if (step.gateExposure === undefined) exposureComplete = false;
  }
  if (exposureComplete && trace.gateExposures > 0 && trace.unclassified === 0) trace.bypassPerExposure = trace.bypasses / trace.gateExposures;

  const scoreGroups = new Map();
  const scoreKeys = new Set();
  for (const score of scores) {
    record(score, companyId);
    requireValue(runs.has(score.runId), 'Orphan score');
    for (const name of ['metric', 'dataset', 'version']) text(score[name], name);
    requireValue(['baseline', 'current'].includes(score.window), 'Invalid score window');
    requireValue(typeof score.value === 'number' && Number.isFinite(score.value) && Math.abs(score.value) <= 1e9, 'Invalid score value');
    const model = runs.get(score.runId).model;
    const key = JSON.stringify([model, score.metric, score.dataset, score.version]);
    // One score per run and comparable metric, even if labelled as both windows.
    const scoreKey = JSON.stringify([score.runId, score.metric, score.dataset, score.version]);
    requireValue(!scoreKeys.has(scoreKey), 'Duplicate score');
    scoreKeys.add(scoreKey);
    if (!scoreGroups.has(key)) scoreGroups.set(key, { model, metric: score.metric, dataset: score.dataset, version: score.version, baseline: [], current: [] });
    scoreGroups.get(key)[score.window].push(score.value);
  }
  const scoreDrift = [...scoreGroups.values()].map(({ baseline, current, ...group }) => {
    const mean = (values) => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    const baselineMean = mean(baseline);
    const currentMean = mean(current);
    return { ...group, baselineCount: baseline.length, currentCount: current.length, baselineMean, currentMean, delta: baselineMean !== null && currentMean !== null ? currentMean - baselineMean : null };
  });
  return {
    schemaVersion: 1, companyId, capturedAt: snapshot.capturedAt,
    evidenceTrust: 'caller-attested', complete: snapshot.complete,
    totals: summary([...issues.values()], [...runs.values()], snapshot.complete, delivered),
    cohorts, trace, scoreDrift,
  };
}
