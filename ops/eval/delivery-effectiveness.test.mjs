#!/usr/bin/env node
// Synthetic frozen-clock assertions. --verify also proves semantic negative controls.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, relative } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const args = process.argv.slice(2);
const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : null;
const subject = resolve(option('--subject') || fileURLToPath(new URL('./delivery-effectiveness.mjs', import.meta.url)));
const { scoreDelivery, canonicalStringify, canonicalHash, LIMITS } = await import(pathToFileURL(subject));
const sha = 'a'.repeat(64), head = 'b'.repeat(40), newerHead = 'c'.repeat(40);
const at = day => `2030-01-${String(day).padStart(2, '0')}T00:00:00.000Z`;
const receipt = kind => ({ id: `receipt-${kind}`, kind, revision: 'revision-1', sha256: sha, persisted: true });
const event = (id, kind, day, extra = {}) => ({ id, unitId: 'unit-a', kind, atUTC: at(day), ...extra });
function fixture() {
  const b = {
    schema: 'delivery-bundle.v1', policy: 'delivery-policy.v1', asOfUTC: at(9),
    scope: { repository: 'https://github.com/example/project', workclass: 'producer-finishing', correction: 'admission-change' },
    windows: { pre: { startUTC: at(1), endUTC: at(5) }, post: { startUTC: at(5), endUTC: at(9) } },
    provenance: { sources: [{ id: 'frozen-source', revision: 'revision-1', sha256: sha }], sourceSha256: sha, testSha256: sha, contractSha256: sha },
    members: [{ id: 'unit-a', sourceId: 'source-a', executorId: 'builder-a', reviewerIds: ['reviewer-a'], pr: 12, arrivedAtUTC: at(1), releaseRequired: false }],
    events: [event('head-1', 'head', 1, { head }), event('start-1', 'started', 2),
      event('admit-1', 'admission', 5, { correction: 'admission-change', receipt: receipt('admission') }),
      event('ready-1', 'ready', 5), event('merge-1', 'merged', 6, { head }), event('close-1', 'closed', 6),
      event('accept-1', 'accepted_delivery', 7, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') })],
    runs: [], coverage: [], confounders: { concurrentExposure: 'none', changedMix: false, comparableUntreated: false },
  };
  return seal(b);
}
function seal(b) {
  b.membershipSha256 = canonicalHash(b.members);
  b.coverage = b.members.flatMap(m => ['events', 'runs'].map(stream => {
    const rows = b[stream].filter(e => e.unitId === m.id);
    return { id: `${m.id}-${stream}`, unitId: m.id, stream, fromUTC: m.arrivedAtUTC, throughUTC: b.asOfUTC,
      complete: true, expectedPages: 1, fetchedPages: [1], errors: [], truncated: false,
      expectedRecords: rows.length, recordIds: rows.map(e => e.id), recordsSha256: canonicalHash(rows) };
  }));
  return b;
}
function copyEvent(e, unitId) {
  const copy = { ...structuredClone(e), id: `${e.id}-${unitId}`, unitId };
  if (copy.receipt) copy.receipt.id += `-${unitId}`;
  return copy;
}
function comparisonFixture() {
  const b = fixture();
  b.events.push(event('artifact-pre', 'accepted_artifact', 3, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }), event('reopen-post', 'reopened', 5));
  b.members.push({ ...structuredClone(b.members[0]), id: 'unit-b', sourceId: 'source-b', pr: 13 });
  b.events.push(...b.events.map(e => copyEvent(e, 'unit-b')));
  return seal(b);
}
const score = b => scoreDelivery(b);
const cases = new Map();
function check(name, body) { cases.set(name, body); }
function eq(actual, expected, name) { assert.deepEqual(actual, expected, `SEMANTIC:${name}`); }
const post = b => score(b).observed.post;
check('positive', () => {
  const b = fixture(), r = score(b);
  eq(r.evidence.accounting.status, 'SUFFICIENT', 'positive');
  eq(r.observed.pre.closing.started_wip, 1, 'positive');
  eq(r.observed.post.opening.started_wip, 1, 'positive');
  eq(r.observed.post.closing.accepted_delivery, 1, 'positive');
  eq(r.observed.post.completedLatencyMs.p50, 5 * 86400000, 'positive');
  eq(r.operationalAuthorization, false, 'positive');
  eq(r.fixtureAssertions.verdict, 'NOT_SUPPLIED', 'positive');
});
for (const [name, damage] of [
  ['missing-page', c => { c.expectedPages = 2; }],
  ['fetch-error', c => { c.errors = ['unavailable']; }],
  ['truncated', c => { c.truncated = true; }],
  ['missing-record', c => { c.recordIds.pop(); }],
  ['false-complete', c => { c.complete = false; }],
]) check(name, () => { const b = fixture(); damage(b.coverage[0]); eq(score(b).evidence.accounting.status, 'UNKNOWN', name); });
check('missing-coverage', () => { const b = fixture(); b.coverage = []; eq(score(b).evidence.accounting.status, 'UNKNOWN', 'missing-coverage'); });
check('duplicate-conflict', () => { const b = fixture(); b.events.push({ ...b.events[0], head: newerHead }); assert.throws(() => score(b), /CONFLICTING_ID/, 'SEMANTIC:duplicate-conflict'); });
check('safe-dedupe', () => { const b = fixture(), r = score(b); b.events.push({ ...b.events[0] }); eq(score(b), r, 'safe-dedupe'); });
check('ambiguous-join', () => { const b = fixture(); b.members.push({ ...b.members[0], id: 'unit-b' }); seal(b); assert.throws(() => score(b), /AMBIGUOUS_MAPPING/, 'SEMANTIC:ambiguous-join'); });
check('membership-pin', () => { const b = fixture(); b.members[0].sourceId = 'different'; assert.throws(() => score(b), /MEMBERSHIP_HASH/, 'SEMANTIC:membership-pin'); });
check('malformed', () => {
  for (const change of [b => { b.asOfUTC = '2030-02-30T00:00:00Z'; }, b => { b.events[0].atUTC = '2030-01-01T00:00:00+00:00'; }, b => { b.events[0].kind = 'surprise'; }, b => { b.windows.pre.startUTC = at(5); }, b => { b.extra = true; }, b => { b.events[0].extra = true; }]) {
    const b = fixture(); change(b); assert.throws(() => score(b), /INVALID_/, 'SEMANTIC:malformed');
  }
});
check('bounds', () => { const b = fixture(); b.events = Array(LIMITS.records + 1).fill(b.events[0]); assert.throws(() => score(b), /BOUNDS/, 'SEMANTIC:bounds'); });
for (const kind of ['approval', 'run_started']) check(kind, () => { const b = fixture(); const e = b.events.find(e => e.kind === 'admission'); e.kind = kind; e.receipt = receipt(kind); seal(b); eq(score(b).evidence.treatment.status, 'UNKNOWN', kind); });
check('unpersisted', () => { const b = fixture(); b.events.find(e => e.kind === 'admission').receipt.persisted = false; seal(b); eq(score(b).evidence.treatment.status, 'UNKNOWN', 'unpersisted'); });
check('producer-only', () => {
  const b = fixture(); b.events.find(e => e.kind === 'accepted_delivery').kind = 'accepted_artifact'; seal(b);
  eq(post(b).closing.accepted_artifact, 1, 'producer-only'); eq(post(b).closing.accepted_delivery, 0, 'producer-only');
  b.events.find(e => e.kind === 'accepted_artifact').kind = 'fixture_pass'; seal(b);
  eq(post(b).closing.accepted_artifact, 0, 'producer-only'); eq(post(b).closing.accepted_delivery, 0, 'producer-only');
});
check('confounding', () => { const b = comparisonFixture(); b.confounders.concurrentExposure = 'other-policy-or-unknown'; eq(score(b).evidence.comparison.status, 'INCONCLUSIVE', 'confounding'); b.confounders.concurrentExposure = 'none'; b.confounders.changedMix = true; eq(score(b).evidence.comparison.status, 'INCONCLUSIVE', 'confounding'); });
check('changed-mix', () => { const b = comparisonFixture(); b.confounders.changedMix = true; eq(score(b).evidence.comparison.status, 'INCONCLUSIVE', 'changed-mix'); });
check('fixture-not-live', () => { const b = fixture(), e = b.events.find(e => e.kind === 'accepted_delivery'); e.kind = 'fixture_pass'; delete e.receipt; delete e.actorId; seal(b); eq(post(b).closing.accepted_delivery, 0, 'fixture-not-live'); });
check('utc', () => { const b = fixture(); b.events[0].atUTC = '2030-01-01T00:00:00+00:00'; seal(b); assert.throws(() => score(b), /INVALID_UTC/, 'SEMANTIC:utc'); });
check('no-causal-claim', () => { const b = fixture(); b.confounders.comparableUntreated = true; eq(score(b).causalEffectiveness.status, 'NOT_ESTABLISHED', 'no-causal-claim'); });
check('merge-close', () => { eq(post(fixture()).merges, 1, 'merge-close'); });
check('reopen', () => { const b = fixture(); b.events.push(event('reopen-1', 'reopened', 8)); seal(b); eq(post(b).closing.accepted_delivery, 0, 'reopen'); eq(post(b).closing.started_wip, 1, 'reopen'); });
check('unauthorized-close', () => { const b = fixture(); b.events = b.events.filter(e => !['merged', 'accepted_delivery'].includes(e.kind)); seal(b); eq(post(b).closing.started_wip, 1, 'unauthorized-close'); eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'unauthorized-close'); });
check('authorized-withdrawal', () => { const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind)); b.events.push(event('withdraw-1', 'withdrawn', 6, { actorId: 'reviewer-a', receipt: receipt('withdrawal') })); seal(b); eq(post(b).closing.authorized_cleanup, 1, 'authorized-withdrawal'); eq(post(b).closing.accepted_delivery, 0, 'authorized-withdrawal'); });
check('blocked-started', () => { const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind)); b.events.push(event('block-1', 'blocked', 6)); seal(b); eq(post(b).closing.started_wip, 1, 'blocked-started'); eq(post(b).censoredWaits.length, 1, 'blocked-started'); eq(post(b).blockedMs, 3 * 86400000, 'blocked-started'); });
check('unstarted-blocked', () => { const b = fixture(); b.members[0].pr = null; b.events = [event('block-1', 'blocked', 6)]; seal(b); eq(post(b).closing.started_wip, 0, 'unstarted-blocked'); eq(post(b).closing.unstarted_backlog, 1, 'unstarted-blocked'); });
check('head-drift', () => { const b = fixture(); b.events.push(event('head-2', 'head', 8, { head: newerHead })); seal(b); eq(post(b).closing.accepted_delivery, 0, 'head-drift'); eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'head-drift'); });
check('release', () => { const b = fixture(); b.members[0].releaseRequired = true; seal(b); eq(post(b).closing.merged_awaiting_acceptance, 1, 'release'); b.events.push(event('release-1', 'release_accepted', 8, { head, actorId: 'reviewer-a', receipt: receipt('release') })); seal(b); eq(post(b).closing.accepted_delivery, 1, 'release'); });
check('independence', () => { const b = fixture(); b.events.find(e => e.kind === 'accepted_delivery').actorId = 'builder-a'; seal(b); eq(post(b).closing.accepted_delivery, 0, 'independence'); });
check('zero-latency', () => { const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind)); seal(b); eq(post(b).completedLatencyMs.p50, null, 'zero-latency'); eq(post(b).completedLatencyMs.p90, null, 'zero-latency'); });
check('half-open', () => { const b = fixture(); b.events.find(e => e.kind === 'accepted_delivery').atUTC = at(9); seal(b); eq(post(b).completed, 0, 'half-open'); eq(post(b).closing.accepted_delivery, 0, 'half-open'); });
check('censor-window', () => { const b = fixture(); b.asOfUTC = at(8); seal(b); eq(post(b).fullyObserved, false, 'censor-window'); eq(score(b).evidence.comparison.status, 'INCONCLUSIVE', 'censor-window'); });
check('ordering', () => { const b = fixture(), before = canonicalStringify(b), r = canonicalStringify(score(b)); b.events.reverse(); b.coverage.reverse(); b.coverage.forEach(c => c.recordIds.reverse()); eq(canonicalStringify(score(b)), r, 'ordering'); b.events.reverse(); b.coverage.reverse(); b.coverage.forEach(c => c.recordIds.reverse()); eq(canonicalStringify(b), before, 'ordering'); });
check('frozen-clock', () => { const b = fixture(), r = score(b), old = Date.now; try { Date.now = () => { assert.fail('SEMANTIC:frozen-clock wall clock read'); }; eq(score(b), r, 'frozen-clock'); } finally { Date.now = old; } });
check('cost', () => { const b = fixture(); eq(score(b).cost.status, 'UNKNOWN', 'cost'); b.runs.push({ id: 'run-a', unitId: 'unit-a', sourceId: 'source-a', executorId: 'builder-a', membershipSha256: b.membershipSha256, atUTC: at(6), tokens: 100, runMs: 1000, subscriptionEquivalentUSD: 0.01 }); seal(b); eq(score(b).cost.status, 'SUFFICIENT', 'cost'); eq(score(b).cost.tokens, 100, 'cost'); b.runs[0].sourceId = 'other-source'; seal(b); eq(score(b).cost.status, 'UNKNOWN', 'cost'); });
check('nearest-rank', () => { const b = fixture(); const extra = structuredClone(b.members[0]); extra.id = 'unit-b'; extra.sourceId = 'source-b'; extra.pr = 13; b.members.push(extra); b.events.push(...b.events.filter(e => !['started', 'accepted_delivery'].includes(e.kind)).map(e => copyEvent(e, 'unit-b')), { ...event('start-b', 'started', 3), unitId: 'unit-b' }, { ...event('accept-b', 'accepted_delivery', 7, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }), unitId: 'unit-b' }); seal(b); eq(post(b).completedLatencyMs.p50, 4 * 86400000, 'nearest-rank'); eq(post(b).completedLatencyMs.p90, 5 * 86400000, 'nearest-rank'); });
check('complete-comparison', () => {
  const b = comparisonFixture(), r = score(b);
  eq(r.observed.pre.completed, 2, 'complete-comparison'); eq(r.observed.post.completed, 2, 'complete-comparison');
  eq(r.descriptiveComparison.status, 'OBSERVED_ONLY', 'complete-comparison');
  eq(r.causalEffectiveness.status, 'NOT_ESTABLISHED', 'complete-comparison');
  eq(r.fixtureAssertions.verdict, 'NOT_SUPPLIED', 'complete-comparison');
});
check('matched-exposure', () => { const b = comparisonFixture(); b.events.filter(e => e.kind === 'admission').forEach(e => { e.atUTC = at(6); }); seal(b); eq(score(b).evidence.treatment.status, 'SUFFICIENT', 'matched-exposure'); eq(score(b).evidence.comparison.status, 'INCONCLUSIVE', 'matched-exposure'); });
check('cli', () => {
  const dir = mkdtempSync(join(tmpdir(), 'delivery-input-'));
  try {
    const p = join(dir, 'input.json'); writeFileSync(p, JSON.stringify(fixture()));
    const run = (...a) => spawnSync(process.execPath, [subject, ...a], { encoding: 'utf8', timeout: 15000 });
    eq(run('--from-file', p).status, 0, 'cli'); eq(run().status, 2, 'cli');
    writeFileSync(p, ' '.repeat(LIMITS.bytes + 1)); eq(run('--from-file', p).status, 2, 'cli');
    writeFileSync(p, '{"schema":1,"schema":2}'); eq(run('--from-file', p).status, 2, 'cli');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

check('repeat-terminal', () => {
  for (const kind of ['accepted_delivery', 'accepted_artifact', 'release_accepted']) {
    const b = fixture();
    b.members[0].releaseRequired = kind === 'release_accepted';
    b.events = b.events.filter(e => !['ready', 'merged', 'closed', 'accepted_delivery'].includes(e.kind));
    b.events.push(event('merge-pre', 'merged', 3, { head }),
      event('accept-pre', kind === 'accepted_artifact' ? kind : 'accepted_delivery', 4, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }));
    if (b.members[0].releaseRequired) b.events.push(event('release-pre', 'release_accepted', 4, { head, actorId: 'reviewer-a', receipt: receipt('release') }));
    b.events.push(event('repeat-post', kind, 7, { head, actorId: 'reviewer-a', receipt: receipt(kind === 'release_accepted' ? 'release' : 'acceptance') }));
    b.members.push({ ...structuredClone(b.members[0]), id: 'unit-b', sourceId: 'source-b', pr: 13 });
    b.events.push(...b.events.map(e => copyEvent(e, 'unit-b')));
    seal(b); const r = score(b);
    eq(r.observed.pre.completed, 2, 'repeat-terminal'); eq(r.observed.post.completed, 0, 'repeat-terminal');
    eq(r.descriptiveComparison.status, 'INCONCLUSIVE', 'repeat-terminal');
  }
});
check('terminal-upgrade', () => {
  const b = fixture(); b.events.push(event('artifact-pre', 'accepted_artifact', 3, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') })); seal(b);
  eq(score(b).observed.pre.completed, 1, 'terminal-upgrade'); eq(post(b).closing.accepted_delivery, 1, 'terminal-upgrade'); eq(post(b).completed, 0, 'terminal-upgrade');
});
check('acceptance-needs-merge', () => {
  const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed'].includes(e.kind)); seal(b);
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'acceptance-needs-merge'); eq(post(b).completed, 0, 'acceptance-needs-merge');
});
check('head-requalification', () => {
  for (const day of [3, 8]) {
    const b = fixture(); b.events.push(event('head-2', 'head', day, { head: newerHead }));
    if (day === 3) b.events.filter(e => ['merged', 'accepted_delivery'].includes(e.kind)).forEach(e => { e.head = newerHead; });
    else b.events.push(event('merge-2', 'merged', 8, { atUTC: '2030-01-08T01:00:00.000Z', head: newerHead }), event('accept-2', 'accepted_delivery', 8, { atUTC: '2030-01-08T02:00:00.000Z', head: newerHead, actorId: 'reviewer-a', receipt: receipt('acceptance') }));
    seal(b); eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'head-requalification'); eq(post(b).closing.accepted_delivery, 1, 'head-requalification');
  }
});
check('withdrawal-close', () => {
  const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind));
  b.events.push(event('withdraw-1', 'withdrawn', 6, { actorId: 'reviewer-a', receipt: receipt('withdrawal') }), event('close-later', 'closed', 7)); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'withdrawal-close'); eq(post(b).closing.authorized_cleanup, 1, 'withdrawal-close'); eq(post(b).completed, 0, 'withdrawal-close');
});
check('ready-pair', () => {
  const b = fixture(); b.events.push(event('ready-late', 'ready', 8)); seal(b);
  eq(post(b).readyToMergeMs, { n: 1, p50: 86400000, p90: 86400000 }, 'ready-pair');
  b.events = b.events.filter(e => e.id !== 'ready-late');
  b.events.push(event('head-new', 'head', 8, { head: newerHead }), event('merge-new', 'merged', 8, { atUTC: '2030-01-08T01:00:00.000Z', head: newerHead })); seal(b);
  eq(post(b).readyToMergeMs.n, 0, 'ready-pair');
});
check('overlapping-reviews', () => {
  const b = fixture(); b.members[0].reviewerIds.push('reviewer-b');
  b.events.push(event('review-a', 'review_arrived', 5, { actorId: 'reviewer-a' }), event('review-b', 'review_arrived', 6, { actorId: 'reviewer-b' }),
    event('review-a-end', 'review_completed', 7, { actorId: 'reviewer-a' }), event('review-b-end', 'review_completed', 8, { actorId: 'reviewer-b' })); seal(b);
  eq(post(b).reviewLatencyMs, { n: 2, p50: 2 * 86400000, p90: 2 * 86400000 }, 'overlapping-reviews');
  b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery', 'review_completed'].includes(e.kind)); seal(b);
  eq(post(b).censoredWaits.filter(w => w.kind === 'review').length, 2, 'overlapping-reviews');
});
check('ambiguous-reviews', () => {
  const b = fixture(); b.members[0].reviewerIds.push('reviewer-b');
  b.events.push(event('review-a', 'review_arrived', 5), event('review-end', 'review_completed', 8)); seal(b);
  eq(post(b).reviewLatencyMs.n, 0, 'ambiguous-reviews'); eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'ambiguous-reviews');
});
function reviewInvalidationFixture(kind) {
  const b = fixture(), currentHead = kind === 'head' ? newerHead : head;
  b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind));
  b.events.push(event('review-start', 'review_arrived', 5, { actorId: 'reviewer-a' }),
    event('invalidate-review', kind, 6, kind === 'head' ? { head: newerHead } : {}),
    event('review-end', 'review_completed', 7, { actorId: 'reviewer-a' }),
    event('fresh-merge', 'merged', 8, { atUTC: '2030-01-08T01:00:00.000Z', head: currentHead }),
    event('fresh-accept', 'accepted_delivery', 8, { atUTC: '2030-01-08T02:00:00.000Z', head: currentHead, actorId: 'reviewer-a', receipt: receipt('acceptance') }));
  return seal(b);
}
check('review-invalidation', () => {
  for (const kind of ['head', 'reopened']) {
    const b = reviewInvalidationFixture(kind);
    eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'review-invalidation');
    eq(post(b).closing.accepted_delivery, 1, 'review-invalidation');
    b.events = b.events.filter(e => !['fresh-merge', 'fresh-accept'].includes(e.id)); seal(b);
    eq(post(b).closing.accepted_delivery, 0, 'review-invalidation'); eq(post(b).closing.started_wip, 1, 'review-invalidation');
  }
});
check('invalidated-review-latency', () => {
  for (const kind of ['head', 'reopened']) {
    const b = reviewInvalidationFixture(kind);
    eq(post(b).reviewLatencyMs, { n: 0, p50: null, p90: null }, 'invalidated-review-latency');
    b.asOfUTC = '2030-01-06T12:00:00.000Z'; b.events = b.events.filter(e => e.atUTC <= b.asOfUTC); seal(b);
    eq(post(b).censoredWaits.filter(w => w.kind === 'review').length, 0, 'invalidated-review-latency');
    const fresh = reviewInvalidationFixture(kind);
    fresh.events.push(event('review-fresh', 'review_arrived', 7, { atUTC: '2030-01-07T01:00:00.000Z', actorId: 'reviewer-a' }),
      event('review-fresh-end', 'review_completed', 7, { atUTC: '2030-01-07T02:00:00.000Z', actorId: 'reviewer-a' })); seal(fresh);
    eq(post(fresh).reviewLatencyMs, { n: 1, p50: 3600000, p90: 3600000 }, 'invalidated-review-latency');
    eq(score(fresh).evidence.acceptance.status, 'SUFFICIENT', 'invalidated-review-latency');
  }
});
check('review-invalidation-ambiguity', () => {
  for (const kind of ['head', 'reopened']) {
    const b = reviewInvalidationFixture(kind); b.events = b.events.filter(e => e.id !== 'review-start'); seal(b);
    eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'review-invalidation-ambiguity');
    const overlap = reviewInvalidationFixture(kind);
    overlap.events.push(event('overlap', 'review_arrived', 6, { atUTC: '2030-01-06T01:00:00.000Z', actorId: 'reviewer-a' })); seal(overlap);
    eq(score(overlap).evidence.acceptance.status, 'UNKNOWN', 'review-invalidation-ambiguity');
    eq(post(overlap).reviewLatencyMs.n, 0, 'review-invalidation-ambiguity');
  }
});
check('partial-asof', () => {
  const b = fixture(); b.asOfUTC = at(8); b.events.find(e => e.kind === 'accepted_delivery').atUTC = at(8); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'partial-asof'); eq(post(b).completed, 1, 'partial-asof'); eq(post(b).closing.accepted_delivery, 1, 'partial-asof');
  eq(post(b).observedThroughUTC, at(8), 'partial-asof');
});
check('symlink-cli', () => {
  const dir = mkdtempSync(join(tmpdir(), 'delivery-symlink-'));
  try {
    const p = join(dir, 'input.json'), link = join(dir, 'entry.mjs'), inputLink = join(dir, 'input-link.json');
    writeFileSync(p, JSON.stringify(fixture())); symlinkSync(subject, link); symlinkSync(p, inputLink);
    const run = (...a) => spawnSync(process.execPath, [link, ...a], { encoding: 'utf8', timeout: 15000 });
    const r = run('--from-file', p); eq(r.status, 0, 'symlink-cli'); eq(Boolean(r.stdout.trim()), true, 'symlink-cli');
    eq(JSON.parse(r.stdout).schema, 'delivery-score.v1', 'symlink-cli'); eq(run().status, 2, 'symlink-cli'); eq(run('--from-file', inputLink).status, 2, 'symlink-cli');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
check('aggregate-cost', () => {
  for (const field of ['tokens', 'runMs', 'subscriptionEquivalentUSD']) {
    const b = fixture(), r = { id: 'run-a', unitId: 'unit-a', sourceId: 'source-a', executorId: 'builder-a', membershipSha256: b.membershipSha256, atUTC: at(6), tokens: 1, runMs: 1, subscriptionEquivalentUSD: 1 };
    r[field] = field === 'subscriptionEquivalentUSD' ? Number.MAX_VALUE : Number.MAX_SAFE_INTEGER;
    b.runs = [r, { ...r, id: 'run-b', [field]: field === 'subscriptionEquivalentUSD' ? Number.MAX_VALUE : 2 }]; seal(b);
    eq(score(b).cost, { status: 'UNKNOWN', tokens: null, runMs: null, subscriptionEquivalentUSD: null }, 'aggregate-cost');
  }
});
check('dot-repository', () => { const b = fixture(); b.scope.repository = 'https://github.com/example/.github'; assert.doesNotThrow(() => eq(score(b).scope.repository, b.scope.repository, 'dot-repository'), 'SEMANTIC:dot-repository'); });

check('pending-acceptance', () => {
  for (const started of [false, true]) {
    const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind) && (started || e.kind !== 'started')); seal(b);
    eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'pending-acceptance');
  }
});
check('reopen-acceptance', () => {
  const b = fixture(); b.events.push(event('reopen', 'reopened', 8)); seal(b);
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'reopen-acceptance');
  b.events.push(event('merge-fresh', 'merged', 8, { atUTC: '2030-01-08T01:00:00.000Z', head }),
    event('accept-fresh', 'accepted_delivery', 8, { atUTC: '2030-01-08T02:00:00.000Z', head, actorId: 'reviewer-a', receipt: receipt('acceptance') })); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'reopen-acceptance');
});
check('future-member', () => {
  const b = fixture(); b.members[0].arrivedAtUTC = at(10); b.events = []; seal(b);
  b.coverage.forEach(c => { c.fromUTC = at(9); c.throughUTC = at(10); });
  assert.throws(() => score(b), /INVALID_MEMBER_TIME/, 'SEMANTIC:future-member');
});
check('coverage-order', () => {
  const b = fixture(); b.coverage[0].fromUTC = at(9); b.coverage[0].throughUTC = at(1);
  assert.throws(() => score(b), /INVALID_COVERAGE_TIME/, 'SEMANTIC:coverage-order');
});
check('duplicate-reviewer', () => {
  const b = fixture(); b.members[0].reviewerIds.push('reviewer-a'); seal(b);
  assert.throws(() => score(b), /INVALID_REVIEWER/, 'SEMANTIC:duplicate-reviewer');
});
check('receipt-unit', () => {
  const b = comparisonFixture(), admissions = b.events.filter(e => e.kind === 'admission');
  admissions[1].receipt.id = admissions[0].receipt.id; seal(b);
  assert.throws(() => score(b), /INVALID_RECEIPT_UNIT/, 'SEMANTIC:receipt-unit');
});
check('cleanup-head', () => {
  const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind));
  b.events.push(event('withdraw', 'withdrawn', 6, { actorId: 'reviewer-a', receipt: receipt('withdrawal') }), event('head-new', 'head', 8, { head: newerHead })); seal(b);
  eq(post(b).closing.authorized_cleanup, 1, 'cleanup-head'); eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'cleanup-head');
});
check('cli-score-body', () => {
  const dir = mkdtempSync(join(tmpdir(), 'delivery-cli-score-'));
  try {
    const p = join(dir, 'input.json'), b = fixture(); writeFileSync(p, JSON.stringify(b));
    const r = spawnSync(process.execPath, [subject, '--from-file', p], { encoding: 'utf8', timeout: 15000 });
    eq(r.status, 0, 'cli-score-body'); const { adapter, ...output } = JSON.parse(r.stdout);
    eq(output, score(b), 'cli-score-body'); eq(typeof adapter.sourceSha256, 'string', 'cli-score-body');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

check('terminal-review-wait', () => {
  const b = fixture(); b.members[0].reviewerIds.push('reviewer-b');
  b.events.push(event('review-b', 'review_arrived', 6, { actorId: 'reviewer-b' })); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'terminal-review-wait');
  eq(post(b).censoredWaits, [{ unitId: 'unit-a', reviewerId: 'reviewer-b', kind: 'review', censored: true, elapsedMs: 3 * 86400000 }], 'terminal-review-wait');
  b.events.push(event('review-b-end', 'review_completed', 8, { actorId: 'reviewer-b' })); seal(b);
  eq(post(b).censoredWaits, [], 'terminal-review-wait');
  eq(post(b).reviewLatencyMs, { n: 1, p50: 2 * 86400000, p90: 2 * 86400000 }, 'terminal-review-wait');
});
check('review-ambiguity-drain', () => {
  const b = fixture();
  b.events.push(event('arrival-1', 'review_arrived', 3), event('arrival-2', 'review_arrived', 4),
    event('completion-1', 'review_completed', 5), event('arrival-3', 'review_arrived', 6), event('completion-2', 'review_completed', 7)); seal(b);
  eq(post(b).reviewLatencyMs, { n: 0, p50: null, p90: null }, 'review-ambiguity-drain');
  eq(post(b).censoredWaits.filter(w => w.kind === 'review'), [], 'review-ambiguity-drain');
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'review-ambiguity-drain');
  b.events.push(event('completion-3', 'review_completed', 8),
    event('arrival-fresh', 'review_arrived', 8, { atUTC: '2030-01-08T01:00:00.000Z' }),
    event('completion-fresh', 'review_completed', 8, { atUTC: '2030-01-08T03:00:00.000Z' })); seal(b);
  eq(post(b).reviewLatencyMs, { n: 1, p50: 7200000, p90: 7200000 }, 'review-ambiguity-drain');
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'review-ambiguity-drain');
});
for (const field of ['sourceSha256', 'testSha256', 'contractSha256', 'sourceReceipt', 'acceptanceReceipt', 'eventHead', 'runMembership', 'coverageRecords', 'membershipSha256']) {
  const name = `primitive-${field}`;
  check(name, () => {
    const b = fixture();
    if (field === 'sourceReceipt') b.provenance.sources[0].sha256 = [sha];
    else if (field === 'acceptanceReceipt') b.events.find(e => e.kind === 'accepted_delivery').receipt.sha256 = [sha];
    else if (field === 'eventHead') b.events[0].head = [head];
    else if (field === 'runMembership') b.runs.push({ id: 'run-a', unitId: 'unit-a', sourceId: 'source-a', executorId: 'builder-a', membershipSha256: [b.membershipSha256], atUTC: at(6), tokens: 1, runMs: 1, subscriptionEquivalentUSD: 0 });
    else if (field === 'coverageRecords') b.coverage[0].recordsSha256 = [b.coverage[0].recordsSha256];
    else if (field === 'membershipSha256') b.membershipSha256 = [b.membershipSha256];
    else b.provenance[field] = [sha];
    // Coverage remains valid for the receipt/head negatives: refusal is the field type, not missing evidence.
    if (['sourceReceipt', 'acceptanceReceipt', 'eventHead', 'runMembership'].includes(field)) seal(b);
    assert.throws(() => score(b), /INVALID_(HASH|HEAD|RECEIPT|RUN|COVERAGE)|MEMBERSHIP_HASH/, `SEMANTIC:${name}`);
  });
}
check('repository-bound', () => {
  const b = fixture(), prefix = 'https://github.com/example/';
  b.scope.repository = prefix + 'x'.repeat(512 - prefix.length);
  eq(score(b).scope.repository.length, 512, 'repository-bound');
  b.scope.repository += 'x';
  assert.throws(() => score(b), /INVALID_TEXT/, 'SEMANTIC:repository-bound');
  b.scope.repository = ['https://github.com/example/project'];
  assert.throws(() => score(b), /INVALID_TEXT/, 'SEMANTIC:repository-bound');
});
check('tied-heads', () => {
  const b = fixture(); b.events.push(event('head-z', 'head', 1, { head: newerHead }));
  b.events.filter(e => ['merged', 'accepted_delivery'].includes(e.kind)).forEach(e => { e.head = newerHead; }); seal(b);
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'tied-heads');
  eq(post(b).completed, 0, 'tied-heads');
  const r = canonicalStringify(score(b)); b.events.reverse();
  eq(canonicalStringify(score(b)), r, 'tied-heads');
  // Renaming tied event IDs must not select sufficient evidence for either head.
  b.events.find(e => e.id === 'head-1').id = 'head-zz'; seal(b);
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'tied-heads');
  b.events.push(event('head-resolved', 'head', 3, { head: newerHead })); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'tied-heads');
  const same = fixture(); same.events.push(event('head-identical', 'head', 1, { head })); seal(same);
  eq(score(same).evidence.acceptance.status, 'SUFFICIENT', 'tied-heads');
  eq(score(fixture()).evidence.acceptance.status, 'SUFFICIENT', 'tied-heads');
});
check('artifact-close', () => {
  const b = fixture(); b.events = b.events.filter(e => !['merged', 'closed', 'accepted_delivery'].includes(e.kind));
  b.events.push(event('artifact-pre', 'accepted_artifact', 3, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }), event('normal-close', 'closed', 6)); seal(b);
  eq(score(b).evidence.acceptance.status, 'SUFFICIENT', 'artifact-close');
  eq(score(b).observed.pre.completed, 1, 'artifact-close'); eq(post(b).completed, 0, 'artifact-close');
  eq(post(b).closing.accepted_artifact, 1, 'artifact-close'); eq(post(b).closing.accepted_delivery, 0, 'artifact-close'); eq(post(b).merges, 0, 'artifact-close');
  b.events = b.events.filter(e => e.kind !== 'accepted_artifact'); seal(b);
  eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'artifact-close'); eq(post(b).completed, 0, 'artifact-close'); eq(post(b).closing.started_wip, 1, 'artifact-close');
});
check('prior-start', () => {
  for (const kind of ['accepted_artifact', 'accepted_delivery', 'release_accepted']) {
    const b = fixture(); b.events = b.events.filter(e => !['started', 'merged', 'closed', 'accepted_delivery'].includes(e.kind));
    b.events.push(event('unstarted-accept', kind, 7, { head, actorId: 'reviewer-a', receipt: receipt(kind === 'release_accepted' ? 'release' : 'acceptance') })); seal(b);
    eq(score(b).evidence.acceptance.status, 'UNKNOWN', 'prior-start');
    eq(post(b).completed, 0, 'prior-start');
  }
});
check('relative-subject', () => {
  const run = spawnSync(process.execPath, [fileURLToPath(import.meta.url), '--subject', relative(process.cwd(), subject), '--case', 'symlink-cli'], { encoding: 'utf8', timeout: 15000 });
  eq(run.status, 0, 'relative-subject');
  eq(run.stdout.includes('PASS symlink-cli'), true, 'relative-subject');
});

const selected = option('--case');
let failures = 0;
for (const [name, body] of cases) {
  if (selected && name !== selected) continue;
  try { body(); console.log(`PASS ${name}`); } catch (e) { failures++; console.error(`FAIL ${name}: ${e.stack}`); }
}
if (selected && !cases.has(selected)) throw Error('Unknown assertion');
if (failures) process.exit(1);
console.log(JSON.stringify({ assertionVerdict: 'PASS', cases: selected ? 1 : cases.size }));

if (args.includes('--verify')) {
  // Each replacement is a faulty implementation, never an alternate production mode.
  const mutants = [
    ['missing-page', 'c.expectedPages === pages.length', 'true'],
    ['fetch-error', 'c.errors.length === 0', 'true'],
    ['truncated', 'c.truncated === false', 'true'],
    ['missing-record', 'canonicalHash(c.recordIds) === canonicalHash(rows.map(e => e.id))', 'true'],
    ['false-complete', 'c.complete === true', 'true'],
    ['missing-coverage', 'if (!c) return false;', 'if (!c) return true;'],
    ['duplicate-conflict', "throw Error('CONFLICTING_ID')", 'continue'],
    ['ambiguous-join', "throw Error('AMBIGUOUS_MAPPING')", 'undefined'],
    ['membership-pin', "throw Error('MEMBERSHIP_HASH')", 'undefined'],
    ['malformed', "throw Error('INVALID_FIELDS')", 'undefined'],
    ['bounds', "throw Error('BOUNDS_RECORDS')", 'undefined'],
    ['approval', "e.kind === 'admission'", "['admission', 'approval'].includes(e.kind)"],
    ['run_started', "e.kind === 'admission'", "['admission', 'run_started'].includes(e.kind)"],
    ['unpersisted', 'r.persisted === true', 'true'],
    ['producer-only', "s.artifact = e;", 's.artifact = e; s.delivery = e;'],
    ['confounding', "b.confounders.concurrentExposure === 'none' && b.confounders.changedMix === false", 'true'],
    ['changed-mix', 'b.confounders.changedMix === false', 'true'],
    ['fixture-not-live', "if (e.kind === 'ready') s.ready = e.atUTC;", "if (e.kind === 'ready') s.ready = e.atUTC; if (e.kind === 'fixture_pass') s.delivery = e;"],
    ['utc', 'utc(e.atUTC);', ';'],
    ['no-causal-claim', "status: 'NOT_ESTABLISHED'", "status: 'ESTABLISHED'"],
    ['merge-close', 'merges: new Set(merges.map(e => e.unitId)).size', 'merges: merges.length + rows.filter(e => e.kind === "closed").length'],
    ['reopen', 'resetEvidence(s); // reopen', '// faulty retained terminal evidence'],
    ['unauthorized-close', "if (e.kind === 'closed' && !s.merge && !s.cleanup && !s.artifact) s.issues.push('unauthorized_close');", "if (e.kind === 'closed' && !s.merge) s.cleanup = e;"],
    ['blocked-started', "return 'started_wip';", "return s.blockedAt ? 'unstarted_backlog' : 'started_wip';"],
    ['unstarted-blocked', 'if (!s.started) return', 'if (!s.started && !s.blockedAt) return'],
    ['head-drift', 'resetEvidence(s); // head drift', '// faulty stale-head evidence'],
    ['release', '(!m.releaseRequired || s.release)', 'Boolean(s.release ||= s.delivery)'],
    ['independence', 'm.reviewerIds.includes(e.actorId)', 'true'],
    ['zero-latency', 'if (!values.length) return null;', 'if (!values.length) return 0;'],
    ['half-open', 'time(e.atUTC) < end', 'time(e.atUTC) <= end'],
    ['censor-window', 'fullyObserved: time(w.endUTC) <= time(b.asOfUTC)', 'fullyObserved: true'],
    ['ordering', 'inputSha256: canonicalHash(b)', 'inputSha256: canonicalHashRaw(bundle)'],
    ['nearest-rank', 'Math.ceil(p * values.length) - 1', 'Math.floor(p * values.length)'],
    ['matched-exposure', 'treated && matchedExposure && acceptance', 'treated && acceptance'],
    ['frozen-clock', 'const asOf = time(b.asOfUTC);', 'const asOf = Date.now();'],
    ['cost', 'r.sourceId === m.sourceId', 'true'],
    ['repeat-terminal', 'if (s.completedAt !== null) return s.completedAt;', '// faulty receipt refresh moves terminal time'],
    ['terminal-upgrade', 'if (s.completedAt !== null) return s.completedAt;', '// faulty artifact upgrade recounts a completion'],
    ['acceptance-needs-merge', "if (!s.merge) s.issues.push('missing_merge');", 'if (!s.merge) { s.merge = e; s.delivery = e; }'],
    ['head-requalification', 's.head = e.head || null;', "if (s.head) s.issues.push('head_drift'); s.head = e.head || null;"],
    ['withdrawal-close', "if (e.kind === 'closed' && !s.merge && !s.cleanup && !s.artifact)", "if (e.kind === 'closed' && !s.merge && !s.artifact)"],
    ['ready-pair', 'time(s.mergeReady)', 'time(s.ready)'],
    ['overlapping-reviews', 'const reviewer = e.actorId ||', 'const reviewer = m.reviewerIds[0] ||'],
    ['ambiguous-reviews', '(m.reviewerIds.length === 1 ? m.reviewerIds[0] : null)', 'm.reviewerIds[0]'],
    ['review-invalidation', 'for (const arrival of s.reviewArrivals.values()) if (arrival) arrival.invalidated = true;', 's.reviewArrivals.clear();'],
    ['invalidated-review-latency', '!arrived.invalidated && time(e.atUTC) >= start', 'time(e.atUTC) >= start'],
    ['review-invalidation-ambiguity', "if (!arrived) s.issues.push('ambiguous_review');", 'if (!arrived) {}'],
    ['partial-asof', 'time(b.asOfUTC) + 1', 'time(b.asOfUTC)'],
    ['symlink-cli', 'realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url))', 'resolve(process.argv[1]) === fileURLToPath(import.meta.url)'],
    ['aggregate-cost', 'joined && safeTotals', 'joined'],
    ['dot-repository', '[a-z0-9._-]+$', '[a-z0-9][a-z0-9._-]*$'],
    ['pending-acceptance', '!s.invalidated && terminal(category(s, m))', "!s.invalidated && category(s, m) !== 'merged_awaiting_acceptance'"],
    ['reopen-acceptance', '!s.invalidated && terminal(category(s, m))', "!s.invalidated && category(s, m) !== 'merged_awaiting_acceptance'"],
    ['future-member', "requireThat(time(m.arrivedAtUTC) <= time(b.asOfUTC), 'MEMBER_TIME');", ';'],
    ['coverage-order', "requireThat(time(c.fromUTC) <= time(c.throughUTC), 'COVERAGE_TIME');", ';'],
    ['duplicate-reviewer', 'new Set(m.reviewerIds).size === m.reviewerIds.length', 'true'],
    ['receipt-unit', "requireThat(!receiptUnit || receiptUnit === e.unitId, 'RECEIPT_UNIT');", ';'],
    ['cleanup-head', 's.cleanup = cleanup;', 's.cleanup = null;'],
    ['cli-score-body', 'const output = scoreDelivery(input);', 'const output = { ...scoreDelivery(input), operationalAuthorization: true };'],
    ['terminal-review-wait', 'arrived && !arrived.ambiguous && !arrived.invalidated) waits.push', 'arrived && !arrived.ambiguous && !arrived.invalidated && !terminal(c)) waits.push'],
    ['review-ambiguity-drain', 'if (--arrived.pending === 0)', 'if (true)'],
    ...['sourceSha256', 'testSha256', 'contractSha256', 'sourceReceipt', 'acceptanceReceipt', 'eventHead', 'runMembership', 'coverageRecords'].map(field =>
      [`primitive-${field}`, "typeof value === 'string' && pattern.test(value)", 'pattern.test(value)']),
    ['repository-bound', 'text(b.scope.repository);', ';'],
    ['tied-heads', 's.headAmbiguous = headsAt.get(time(e.atUTC)).size > 1;', 's.headAmbiguous = false;'],
    ['artifact-close', "if (e.kind === 'closed' && !s.merge && !s.cleanup && !s.artifact)", "if (e.kind === 'closed' && !s.merge && !s.cleanup)"],
    ['prior-start', 'if (s.started && accepted(e, m, s, kind))', 'if (accepted(e, m, s, kind))'],
    ['relative-subject', 'const subject = resolve(', 'const subject = (', 'harness'],
  ];
  const source = readFileSync(subject, 'utf8'), harness = readFileSync(fileURLToPath(import.meta.url), 'utf8'), dir = mkdtempSync(join(tmpdir(), 'delivery-mutants-'));
  try {
    for (const [name, from, to, target = 'scorer'] of mutants) {
      const original = target === 'harness' ? harness : source;
      assert(original.includes(from), `missing mutant anchor: ${name}`);
      const path = join(dir, `${name}.mjs`); writeFileSync(path, original.replace(from, to));
      const r = spawnSync(process.execPath, [target === 'harness' ? path : fileURLToPath(import.meta.url), '--subject', target === 'harness' ? subject : path, '--case', name], { encoding: 'utf8', timeout: 15000 });
      process.stdout.write(r.stdout || ''); process.stdout.write(r.stderr || '');
      assert(r.status === 1 && r.stderr.includes('AssertionError') && r.stderr.includes(`SEMANTIC:${name}`), `mutant ${name} did not fail its designated semantic assertion (exit ${r.status})`);
      console.log(JSON.stringify({ mutant: name, mutation: { target, from, to }, command: target === 'harness' ? `node <mutant-harness> --subject <scorer> --case ${name}` : `node delivery-effectiveness.test.mjs --subject <mutant> --case ${name}`, expected: `SEMANTIC:${name}`, exit: r.status, verdict: 'KILLED' }));
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
  console.log(JSON.stringify({ negativeControls: mutants.length, verdict: 'PASS' }));
}
