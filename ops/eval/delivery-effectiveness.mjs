#!/usr/bin/env node
// Frozen evidence accounting only. This module never authorizes an operation.
import { createHash } from 'node:crypto';
import { openSync, fstatSync, readSync, closeSync, readFileSync, realpathSync, constants } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

export const LIMITS = Object.freeze({ bytes: 2 * 1024 * 1024, records: 10000, members: 1000, depth: 32 });
const KINDS = new Set(['head', 'started', 'blocked', 'unblocked', 'reviewing', 'ready', 'review_arrived', 'review_completed', 'admission', 'application', 'approval', 'run_started', 'fixture_pass', 'merged', 'closed', 'reopened', 'accepted_artifact', 'accepted_delivery', 'release_accepted', 'cancelled', 'withdrawn', 'failed', 'rework', 'rollback', 'security']);
const SHA = /^[a-f0-9]{64}$/, HEAD = /^[a-f0-9]{40}$/;
const time = s => Date.parse(s);
const cmp = (a, b) => a < b ? -1 : a > b ? 1 : 0;
function requireThat(ok, code) { if (!ok) throw Error(`INVALID_${code}`); }
function fields(o, allowed, required = allowed) {
  requireThat(o && typeof o === 'object' && !Array.isArray(o), 'OBJECT');
  if (Object.keys(o).some(k => !allowed.includes(k)) || required.some(k => !Object.hasOwn(o, k))) throw Error('INVALID_FIELDS');
}
function text(v) { requireThat(typeof v === 'string' && v.length > 0 && v.length <= 512, 'TEXT'); }
function utc(v) {
  requireThat(typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(v) && Number.isFinite(time(v)), 'UTC');
  requireThat(new Date(time(v)).toISOString() === v.replace(/(?<=:\d{2})Z$/, '.000Z'), 'UTC');
}
function array(v) { requireThat(Array.isArray(v), 'ARRAY'); }
function integer(v) { requireThat(Number.isSafeInteger(v) && v >= 0, 'INTEGER'); }
function normalize(value, depth = 0) {
  requireThat(depth <= LIMITS.depth, 'DEPTH');
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') { requireThat(Number.isFinite(value), 'NUMBER'); return value; }
  requireThat(value && typeof value === 'object', 'JSON');
  if (Array.isArray(value)) return value.map(v => normalize(v, depth + 1)).sort((a, b) => cmp(JSON.stringify(a), JSON.stringify(b)));
  requireThat(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null, 'JSON');
  return Object.fromEntries(Object.keys(value).sort(cmp).map(k => [k, normalize(value[k], depth + 1)]));
}
export function canonicalStringify(value) { return JSON.stringify(normalize(value)); }
function canonicalHashRaw(value) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }
export function canonicalHash(value) { return canonicalHashRaw(normalize(value)); }
function unique(rows) {
  const seen = new Map();
  for (const row of rows) {
    text(row.id);
    if (seen.has(row.id)) {
      if (canonicalStringify(seen.get(row.id)) !== canonicalStringify(row)) throw Error('CONFLICTING_ID');
      continue;
    }
    seen.set(row.id, row);
  }
  return [...seen.values()].sort((a, b) => cmp(a.id, b.id));
}
function validate(input) {
  fields(input, ['schema', 'policy', 'asOfUTC', 'scope', 'windows', 'provenance', 'members', 'membershipSha256', 'events', 'runs', 'coverage', 'confounders']);
  for (const key of ['members', 'events', 'runs', 'coverage']) array(input[key]);
  if (input.members.length > LIMITS.members || input.events.length + input.runs.length + input.coverage.length > LIMITS.records) throw Error('BOUNDS_RECORDS');
  requireThat(Buffer.byteLength(JSON.stringify(input)) <= LIMITS.bytes, 'BYTES');
  const b = normalize(input);
  for (const k of ['members', 'events', 'runs', 'coverage']) b[k] = unique(b[k]);
  requireThat(b.schema === 'delivery-bundle.v1' && b.policy === 'delivery-policy.v1', 'VERSION');
  utc(b.asOfUTC);
  fields(b.scope, ['repository', 'workclass', 'correction']);
  requireThat(/^https:\/\/github\.com\/[a-z0-9][a-z0-9-]*\/[a-z0-9._-]+$/.test(b.scope.repository) && !['.', '..'].includes(b.scope.repository.split('/').at(-1)), 'REPOSITORY');
  text(b.scope.workclass); text(b.scope.correction);
  fields(b.windows, ['pre', 'post']);
  for (const w of Object.values(b.windows)) { fields(w, ['startUTC', 'endUTC']); utc(w.startUTC); utc(w.endUTC); requireThat(time(w.startUTC) < time(w.endUTC), 'WINDOW'); }
  requireThat(time(b.windows.pre.endUTC) === time(b.windows.post.startUTC) && time(b.windows.pre.endUTC) - time(b.windows.pre.startUTC) === time(b.windows.post.endUTC) - time(b.windows.post.startUTC), 'MATCHED_WINDOWS');
  requireThat(time(b.asOfUTC) >= time(b.windows.post.startUTC), 'ASOF');
  fields(b.provenance, ['sources', 'sourceSha256', 'testSha256', 'contractSha256']);
  for (const k of ['sourceSha256', 'testSha256', 'contractSha256']) requireThat(SHA.test(b.provenance[k]), 'HASH');
  array(b.provenance.sources); requireThat(b.provenance.sources.length > 0, 'PROVENANCE');
  b.provenance.sources = unique(b.provenance.sources);
  for (const s of b.provenance.sources) { fields(s, ['id', 'revision', 'sha256']); text(s.id); text(s.revision); requireThat(SHA.test(s.sha256), 'HASH'); }
  if (b.membershipSha256 !== canonicalHash(b.members)) throw Error('MEMBERSHIP_HASH');
  const sources = new Set(), prs = new Set(), members = new Map();
  for (const m of b.members) {
    fields(m, ['id', 'sourceId', 'executorId', 'reviewerIds', 'pr', 'arrivedAtUTC', 'releaseRequired']);
    text(m.id); text(m.sourceId); text(m.executorId); utc(m.arrivedAtUTC); array(m.reviewerIds);
    requireThat(m.reviewerIds.length > 0 && !m.reviewerIds.includes(m.executorId), 'REVIEWER'); m.reviewerIds.forEach(text);
    requireThat(typeof m.releaseRequired === 'boolean' && (m.pr === null || Number.isSafeInteger(m.pr) && m.pr > 0), 'MEMBER');
    if (sources.has(m.sourceId) || m.pr !== null && prs.has(m.pr)) throw Error('AMBIGUOUS_MAPPING');
    sources.add(m.sourceId); if (m.pr !== null) prs.add(m.pr); members.set(m.id, m);
  }
  for (const e of b.events) {
    fields(e, ['id', 'unitId', 'kind', 'atUTC', 'head', 'actorId', 'receipt', 'correction'], ['id', 'unitId', 'kind', 'atUTC']);
    requireThat(members.has(e.unitId) && KINDS.has(e.kind), 'EVENT'); utc(e.atUTC);
    requireThat(time(e.atUTC) >= time(members.get(e.unitId).arrivedAtUTC) && time(e.atUTC) <= time(b.asOfUTC), 'EVENT_TIME');
    if (e.head !== undefined) requireThat(HEAD.test(e.head) && members.get(e.unitId).pr !== null, 'HEAD');
    if (e.actorId !== undefined) text(e.actorId);
    if (e.correction !== undefined) text(e.correction);
    if (e.receipt !== undefined) {
      fields(e.receipt, ['id', 'kind', 'revision', 'sha256', 'persisted']);
      text(e.receipt.id); text(e.receipt.kind); text(e.receipt.revision);
      requireThat(SHA.test(e.receipt.sha256) && typeof e.receipt.persisted === 'boolean', 'RECEIPT');
    }
  }
  for (const r of b.runs) {
    fields(r, ['id', 'unitId', 'sourceId', 'executorId', 'membershipSha256', 'atUTC', 'tokens', 'runMs', 'subscriptionEquivalentUSD']);
    requireThat(members.has(r.unitId), 'RUN'); text(r.sourceId); text(r.executorId); utc(r.atUTC);
    requireThat(SHA.test(r.membershipSha256) && time(r.atUTC) <= time(b.asOfUTC), 'RUN');
    integer(r.tokens); integer(r.runMs); requireThat(Number.isFinite(r.subscriptionEquivalentUSD) && r.subscriptionEquivalentUSD >= 0, 'COST');
  }
  const coverageKeys = new Set();
  for (const c of b.coverage) {
    fields(c, ['id', 'unitId', 'stream', 'fromUTC', 'throughUTC', 'complete', 'expectedPages', 'fetchedPages', 'errors', 'truncated', 'expectedRecords', 'recordIds', 'recordsSha256']);
    requireThat(members.has(c.unitId) && ['events', 'runs'].includes(c.stream), 'COVERAGE');
    const key = `${c.unitId}:${c.stream}`; requireThat(!coverageKeys.has(key), 'COVERAGE_JOIN'); coverageKeys.add(key);
    utc(c.fromUTC); utc(c.throughUTC); integer(c.expectedPages); integer(c.expectedRecords);
    array(c.fetchedPages); array(c.errors); array(c.recordIds); c.fetchedPages.forEach(integer); c.recordIds.forEach(text); c.errors.forEach(text);
    requireThat(typeof c.complete === 'boolean' && typeof c.truncated === 'boolean' && SHA.test(c.recordsSha256), 'COVERAGE');
  }
  fields(b.confounders, ['concurrentExposure', 'changedMix', 'comparableUntreated']);
  text(b.confounders.concurrentExposure); requireThat([true, false, null].includes(b.confounders.changedMix) && [true, false, null].includes(b.confounders.comparableUntreated), 'CONFOUNDERS');
  return b;
}
function covered(b, m, stream) {
  const c = b.coverage.find(c => c.unitId === m.id && c.stream === stream);
  if (!c) return false;
  const rows = b[stream].filter(e => e.unitId === m.id), pages = [...new Set(c.fetchedPages)].sort((a, b) => a - b);
  return c.complete === true && c.errors.length === 0 && c.truncated === false && c.expectedPages > 0 &&
    c.expectedPages === pages.length && pages.every((p, i) => p === i + 1) && c.fetchedPages.length === pages.length &&
    time(c.fromUTC) <= time(m.arrivedAtUTC) && time(c.throughUTC) >= time(b.asOfUTC) && c.expectedRecords === rows.length &&
    canonicalHash(c.recordIds) === canonicalHash(rows.map(e => e.id)) && c.recordsSha256 === canonicalHash(rows);
}
function receiptValid(e, kind) {
  const r = e.receipt;
  return Boolean(r && r.kind === kind && r.persisted === true);
}
function accepted(e, m, s, kind) {
  return receiptValid(e, kind) && m.reviewerIds.includes(e.actorId) && e.head && e.head === s.head;
}
function resetEvidence(s) { s.merge = null; s.delivery = null; s.artifact = null; s.release = null; s.cleanup = null; s.completedAt = null; }
function category(s, m) {
  if (s.cleanup) return 'authorized_cleanup';
  if (s.delivery && s.merge && (!m.releaseRequired || s.release)) return 'accepted_delivery';
  if (s.artifact) return 'accepted_artifact';
  if (s.merge) return 'merged_awaiting_acceptance';
  if (!s.started) return 'unstarted_backlog';
  return 'started_wip';
}
const terminal = c => ['accepted_delivery', 'accepted_artifact', 'authorized_cleanup'].includes(c);
function terminalAt(s, m) {
  const c = category(s, m);
  if (c === 'authorized_cleanup') return time(s.cleanup.atUTC);
  if (s.completedAt !== null) return s.completedAt;
  if (c === 'accepted_artifact') return time(s.artifact.atUTC);
  if (c === 'accepted_delivery') return Math.max(time(s.delivery.atUTC), time(s.merge.atUTC), m.releaseRequired ? time(s.release.atUTC) : 0);
  return null;
}
function replay(b, m, end, start = end, observedEnd = end) {
  const s = { head: null, started: null, blockedAt: null, ready: null, mergeReady: null, reviewArrivals: new Map(), invalidated: false, issues: [], blockedMs: 0, reviewLatencies: [] };
  resetEvidence(s);
  let previous = time(m.arrivedAtUTC);
  const rows = b.events.filter(e => e.unitId === m.id && time(e.atUTC) < end).sort((a, b) => time(a.atUTC) - time(b.atUTC) || cmp(a.kind === 'merged' ? '' : a.kind, b.kind === 'merged' ? '' : b.kind) || cmp(a.id, b.id));
  const charge = until => { if (s.started && s.blockedAt && !terminal(category(s, m))) s.blockedMs += Math.max(0, until - Math.max(previous, start)); previous = until; };
  for (const e of rows) {
    charge(time(e.atUTC));
    if (e.kind === 'head') {
      if (!e.head) s.issues.push('missing_head');
      if (s.head && s.head !== e.head) {
        s.invalidated ||= Boolean(s.merge || s.delivery || s.artifact || s.release);
        resetEvidence(s); // head drift
        s.ready = null; s.mergeReady = null; s.reviewArrivals.clear();
      }
      s.head = e.head || null;
    }
    if (e.kind === 'started' && !s.started) s.started = e.atUTC;
    if (e.kind === 'blocked') s.blockedAt = e.atUTC;
    if (e.kind === 'unblocked') s.blockedAt = null;
    if (e.kind === 'ready') s.ready = e.atUTC;
    if (['review_arrived', 'review_completed'].includes(e.kind)) {
      const reviewer = e.actorId || (m.reviewerIds.length === 1 ? m.reviewerIds[0] : null);
      if (!reviewer || !m.reviewerIds.includes(reviewer)) s.issues.push('ambiguous_review');
      else if (e.kind === 'review_arrived') {
        if (s.reviewArrivals.has(reviewer)) { s.reviewArrivals.set(reviewer, null); s.issues.push('ambiguous_review'); }
        else s.reviewArrivals.set(reviewer, e.atUTC);
      } else {
        const arrived = s.reviewArrivals.get(reviewer);
        if (!arrived) s.issues.push('ambiguous_review');
        else if (time(e.atUTC) >= start) s.reviewLatencies.push(time(e.atUTC) - time(arrived));
        s.reviewArrivals.delete(reviewer);
      }
    }
    if (e.kind === 'reopened') { resetEvidence(s); // reopen
      s.ready = null; s.mergeReady = null; s.reviewArrivals.clear(); }
    if (e.kind === 'merged') {
      if (e.head && e.head === s.head && s.started && !s.merge) { s.merge = e; s.mergeReady = s.ready; }
      else if (!e.head || e.head !== s.head || !s.started) s.issues.push('invalid_merge');
    }
    if (e.kind === 'closed' && !s.merge && !s.cleanup) s.issues.push('unauthorized_close');
    if (['cancelled', 'withdrawn'].includes(e.kind)) {
      if (receiptValid(e, 'withdrawal') && m.reviewerIds.includes(e.actorId)) s.cleanup = e;
      else s.issues.push('unauthorized_cleanup');
    }
    if (['accepted_artifact', 'accepted_delivery', 'release_accepted'].includes(e.kind)) {
      const kind = e.kind === 'release_accepted' ? 'release' : 'acceptance';
      if (s.started && accepted(e, m, s, kind)) {
        if (e.kind === 'accepted_artifact') { s.artifact = e; }
        if (e.kind === 'accepted_delivery') {
          if (!s.merge) s.issues.push('missing_merge');
          else s.delivery = e;
        }
        if (e.kind === 'release_accepted') s.release = e;
      } else s.issues.push('invalid_acceptance');
    }
    if (terminal(category(s, m))) { s.invalidated = false; s.completedAt ??= terminalAt(s, m); }
  }
  charge(observedEnd);
  return s;
}
function quantile(values, p) {
  if (!values.length) return null;
  return [...values].sort((a, b) => a - b)[Math.ceil(p * values.length) - 1];
}
const quantiles = a => ({ n: a.length, p50: quantile(a, 0.5), p90: quantile(a, 0.9) });
function windowScore(b, w) {
  const start = time(w.startUTC), end = Math.min(time(w.endUTC), time(b.asOfUTC) + 1);
  const observedEnd = Math.min(time(w.endUTC), time(b.asOfUTC));
  const buckets = () => Object.fromEntries(['accepted_delivery', 'accepted_artifact', 'merged_awaiting_acceptance', 'authorized_cleanup', 'unstarted_backlog', 'started_wip'].map(k => [k, 0]));
  const opening = buckets(), closing = buckets(), waits = [], latencies = [], readyLatencies = [], reviewLatencies = [];
  let blockedMs = 0, completed = 0;
  for (const m of b.members) {
    if (time(m.arrivedAtUTC) < start) opening[category(replay(b, m, start), m)]++;
    if (time(m.arrivedAtUTC) >= end) continue;
    const s = replay(b, m, end, start, observedEnd), c = category(s, m), finished = terminalAt(s, m);
    closing[c]++; blockedMs += s.blockedMs; reviewLatencies.push(...s.reviewLatencies);
    if (['accepted_delivery', 'accepted_artifact'].includes(c) && finished >= start && finished < end) { completed++; if (s.started) latencies.push(finished - time(s.started)); }
    if (s.merge && s.mergeReady && time(s.merge.atUTC) >= start) readyLatencies.push(time(s.merge.atUTC) - time(s.mergeReady));
    if (s.started && !terminal(c)) waits.push({ unitId: m.id, kind: 'started', censored: true, elapsedMs: observedEnd - time(s.started) });
    for (const [reviewerId, arrived] of s.reviewArrivals) {
      if (arrived && !terminal(c)) waits.push({ unitId: m.id, reviewerId, kind: 'review', censored: true, elapsedMs: observedEnd - time(arrived) });
    }
  }
  const rows = b.events.filter(e => time(e.atUTC) >= start && time(e.atUTC) < end), merges = rows.filter(e => e.kind === 'merged');
  const counts = kind => rows.filter(e => e.kind === kind).length;
  return { startUTC: w.startUTC, endUTC: w.endUTC, observedThroughUTC: new Date(observedEnd).toISOString(), fullyObserved: time(w.endUTC) <= time(b.asOfUTC), opening, closing,
    arrivals: b.members.filter(m => time(m.arrivedAtUTC) >= start && time(m.arrivedAtUTC) < end).length,
    reopens: counts('reopened'), cancellations: counts('cancelled'), withdrawals: counts('withdrawn'),
    merges: new Set(merges.map(e => e.unitId)).size, completed, completedLatencyMs: quantiles(latencies), readyToMergeMs: quantiles(readyLatencies),
    reviewArrivals: counts('review_arrived'), reviewCompletions: counts('review_completed'), reviewLatencyMs: quantiles(reviewLatencies), blockedMs, censoredWaits: waits,
    guardrails: Object.fromEntries(['failed', 'rework', 'rollback', 'security'].map(k => [k, counts(k)])) };
}
export function scoreDelivery(bundle) {
  const b = validate(bundle);
  const asOf = time(b.asOfUTC);
  const states = b.members.filter(m => time(m.arrivedAtUTC) <= asOf).map(m => ({ m, s: replay(b, m, asOf + 1) }));
  const accounting = b.members.length > 0 && b.members.every(m => covered(b, m, 'events'));
  const treated = b.members.length > 0 && b.members.every(m => b.events.some(e => e.unitId === m.id && (e.kind === 'admission' || e.kind === 'application') && e.correction === b.scope.correction && receiptValid(e, e.kind) && time(e.atUTC) >= time(b.windows.post.startUTC) && time(e.atUTC) <= asOf));
  const acceptance = accounting && states.every(({ m, s }) => !s.issues.length && !s.invalidated && category(s, m) !== 'merged_awaiting_acceptance');
  const observed = { pre: windowScore(b, b.windows.pre), post: windowScore(b, b.windows.post) };
  const matchedExposure = b.members.every(m => b.events.some(e => e.unitId === m.id && ['admission', 'application'].includes(e.kind) && e.correction === b.scope.correction && receiptValid(e, e.kind) && time(e.atUTC) === time(b.windows.post.startUTC)));
  const comparison = accounting && treated && matchedExposure && acceptance && observed.pre.fullyObserved && observed.post.fullyObserved && b.confounders.concurrentExposure === 'none' && b.confounders.changedMix === false && observed.pre.completed >= 2 && observed.post.completed >= 2;
  const joined = b.runs.length > 0 && b.members.every(m => covered(b, m, 'runs')) && b.runs.every(r => {
    const m = b.members.find(m => m.id === r.unitId);
    return r.sourceId === m.sourceId && r.executorId === m.executorId && r.membershipSha256 === b.membershipSha256 && time(r.atUTC) >= time(m.arrivedAtUTC);
  });
  const totals = b.runs.reduce((sum, r) => ({ tokens: sum.tokens + r.tokens, runMs: sum.runMs + r.runMs, subscriptionEquivalentUSD: sum.subscriptionEquivalentUSD + r.subscriptionEquivalentUSD }), { tokens: 0, runMs: 0, subscriptionEquivalentUSD: 0 });
  const safeTotals = Number.isSafeInteger(totals.tokens) && Number.isSafeInteger(totals.runMs) && Number.isFinite(totals.subscriptionEquivalentUSD);
  const claim = (ok, why) => ({ status: ok ? 'SUFFICIENT' : 'UNKNOWN', limits: ok ? [] : why });
  const result = {
    schema: 'delivery-score.v1', policy: b.policy, scope: b.scope, asOfUTC: b.asOfUTC,
    provenance: { ...b.provenance, inputSha256: canonicalHash(b), membershipSha256: b.membershipSha256, hashMeaning: 'Supplied source receipts are assertions, not independently authenticated proof.' },
    fixtureAssertions: { verdict: 'NOT_SUPPLIED', note: 'Only the separate executed assertion harness produces a test verdict.' },
    evidence: { accounting: claim(accounting, ['Missing, contradictory or incomplete event coverage.']), treatment: claim(accounting && treated, ['Actual per-unit persisted admission/application is unproved.']), acceptance: claim(acceptance, ['Pending, stale-head, unauthorized or missing applicable acceptance.']),
      comparison: { status: comparison ? 'SUFFICIENT_DESCRIPTIVE_ONLY' : 'INCONCLUSIVE', limits: comparison ? [] : ['Incomplete exposure/history, unproved treatment, concurrent exposure/changed mix or fewer than two completed observations in either window.'] } },
    observed,
    descriptiveComparison: { status: comparison ? 'OBSERVED_ONLY' : 'INCONCLUSIVE', completedDelta: comparison ? observed.post.completed - observed.pre.completed : null, p50LatencyDeltaMs: comparison ? observed.post.completedLatencyMs.p50 - observed.pre.completedLatencyMs.p50 : null },
    causalEffectiveness: { status: 'NOT_ESTABLISHED', note: 'Deterministic arithmetic and an asserted comparable cohort do not identify a causal effect.' },
    cost: joined && safeTotals ? { status: 'SUFFICIENT', basis: 'Immutable cohort/source/executor/run joins; subscription-equivalent, not cash billing.', ...totals } : { status: 'UNKNOWN', tokens: null, runMs: null, subscriptionEquivalentUSD: null },
    operationalAuthorization: false,
  };
  return normalize({ ...result, outputSha256: canonicalHash(result) });
}

// JSON.parse alone discards duplicate object keys. This bounded adapter refuses them.
export function parseFrozen(text) {
  requireThat(Buffer.byteLength(text) <= LIMITS.bytes, 'BYTES');
  let i = 0;
  const ws = () => { while (/[\x20\t\r\n]/.test(text[i] || '\0')) i++; };
  function string() {
    const start = i++;
    while (i < text.length) {
      if (text[i] === '\\') { i += 2; continue; }
      if (text[i++] === '"') return JSON.parse(text.slice(start, i));
    }
    throw Error('INVALID_JSON');
  }
  function value(depth) {
    requireThat(depth <= LIMITS.depth, 'DEPTH'); ws();
    if (text[i] === '"') return string();
    if (text[i] === '{') {
      i++; ws(); const o = Object.create(null);
      if (text[i] === '}') { i++; return o; }
      while (true) {
        ws(); requireThat(text[i] === '"', 'JSON'); const k = string(); ws(); requireThat(text[i++] === ':', 'JSON');
        requireThat(!Object.hasOwn(o, k), 'DUPLICATE_KEY'); o[k] = value(depth + 1); ws();
        const end = text[i++]; if (end === '}') return o; requireThat(end === ',', 'JSON');
      }
    }
    if (text[i] === '[') {
      i++; ws(); const a = []; if (text[i] === ']') { i++; return a; }
      while (true) { requireThat(a.length <= LIMITS.records, 'BOUNDS'); a.push(value(depth + 1)); ws(); const end = text[i++]; if (end === ']') return a; requireThat(end === ',', 'JSON'); }
    }
    const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(i));
    requireThat(Boolean(match), 'JSON'); i += match[0].length; return JSON.parse(match[0]);
  }
  const result = value(0); ws(); requireThat(i === text.length, 'JSON'); return result;
}
function main(args) {
  requireThat(args.length === 2 && args[0] === '--from-file', 'USAGE: --from-file <bounded-frozen.json>');
  const fd = openSync(args[1], constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  let input;
  try {
    const stat = fstatSync(fd); requireThat(stat.isFile() && stat.size <= LIMITS.bytes, 'FILE_BOUNDS');
    const bytes = Buffer.alloc(LIMITS.bytes + 1); let used = 0, n;
    while ((n = readSync(fd, bytes, used, bytes.length - used, null)) > 0) { used += n; requireThat(used <= LIMITS.bytes, 'BYTES'); }
    input = parseFrozen(new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, used)));
  } finally { closeSync(fd); }
  const output = scoreDelivery(input);
  // The adapter, unlike the pure scorer, can bind the actual executable bytes.
  const sourceSha256 = createHash('sha256').update(readFileSync(fileURLToPath(import.meta.url))).digest('hex');
  console.log(canonicalStringify({ ...output, adapter: { sourceSha256, suppliedSourceMatches: input.provenance.sourceSha256 === sourceSha256 } }));
}
function isMain() {
  try { return Boolean(process.argv[1]) && realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url)); }
  catch { return false; }
}
if (isMain()) {
  try { main(process.argv.slice(2)); } catch (e) { console.error(JSON.stringify({ error: String(e.message), operationalAuthorization: false })); process.exitCode = 2; }
}
