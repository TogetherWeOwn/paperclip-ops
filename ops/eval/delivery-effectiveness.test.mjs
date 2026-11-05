#!/usr/bin/env node
// Synthetic frozen-clock assertions. --verify also proves semantic negative controls.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const args = process.argv.slice(2);
const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : null;
const subject = option('--subject') || fileURLToPath(new URL('./delivery-effectiveness.mjs', import.meta.url));
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
function comparisonFixture() {
  const b = fixture();
  b.events.push(event('artifact-pre', 'accepted_artifact', 3, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }), event('reopen-post', 'reopened', 5));
  b.members.push({ ...structuredClone(b.members[0]), id: 'unit-b', sourceId: 'source-b', pr: 13 });
  b.events.push(...b.events.map(e => ({ ...structuredClone(e), id: `${e.id}-b`, unitId: 'unit-b' })));
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
check('nearest-rank', () => { const b = fixture(); const extra = structuredClone(b.members[0]); extra.id = 'unit-b'; extra.sourceId = 'source-b'; extra.pr = 13; b.members.push(extra); b.events.push(...b.events.filter(e => !['started', 'accepted_delivery'].includes(e.kind)).map(e => ({ ...e, id: `${e.id}-b`, unitId: 'unit-b' })), { ...event('start-b', 'started', 3), unitId: 'unit-b' }, { ...event('accept-b', 'accepted_delivery', 7, { head, actorId: 'reviewer-a', receipt: receipt('acceptance') }), unitId: 'unit-b' }); seal(b); eq(post(b).completedLatencyMs.p50, 4 * 86400000, 'nearest-rank'); eq(post(b).completedLatencyMs.p90, 5 * 86400000, 'nearest-rank'); });
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
    ['unauthorized-close', "if (e.kind === 'closed' && !s.merge) s.issues.push('unauthorized_close');", "if (e.kind === 'closed' && !s.merge) s.cleanup = e;"],
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
  ];
  const source = readFileSync(subject, 'utf8'), dir = mkdtempSync(join(tmpdir(), 'delivery-mutants-'));
  try {
    for (const [name, from, to] of mutants) {
      assert(source.includes(from), `missing mutant anchor: ${name}`);
      const path = join(dir, `${name}.mjs`); writeFileSync(path, source.replace(from, to));
      const r = spawnSync(process.execPath, [fileURLToPath(import.meta.url), '--subject', path, '--case', name], { encoding: 'utf8', timeout: 15000 });
      process.stdout.write(r.stdout || ''); process.stdout.write(r.stderr || '');
      assert(r.status === 1 && r.stderr.includes('AssertionError') && r.stderr.includes(`SEMANTIC:${name}`), `mutant ${name} did not fail its designated semantic assertion (exit ${r.status})`);
      console.log(JSON.stringify({ mutant: name, mutation: { from, to }, command: `node delivery-effectiveness.test.mjs --subject <mutant> --case ${name}`, expected: `SEMANTIC:${name}`, exit: r.status, verdict: 'KILLED' }));
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
  console.log(JSON.stringify({ negativeControls: mutants.length, verdict: 'PASS' }));
}
