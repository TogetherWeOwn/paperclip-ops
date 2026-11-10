import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { canonicalPR, collect, collectPages, disposition, guardLabelFix, report } from './unmerged-prs.mjs';

const clock = '2026-11-10T06:00:00.000Z';
const url = n => `https://github.com/example/repo/pull/${n}`;
const pr = (number, state = 'OPEN', extra = {}) => ({ number, url: url(number), state, isDraft: false, headRefOid: 'a'.repeat(40), body: '', comments: [], commentsComplete: true, ...extra });
const card = (id, status, numbers) => ({ id, identifier: id, status, completedAt: clock, assigneeAgentId: `owner-${id}`, body: '', workProducts: numbers.map((n, i) => ({ id: `${id}-${i}`, type: 'pull_request', url: url(n), status: 'active' })), comments: [], commentsComplete: true });
function bundle() {
  return { schema: 'm25-snapshot/v2', complete: true, now: clock, days: 3, coverage: [], scope: { evidence: ['frozen audit'], claim: 'enumerated-repositories-only' }, repositories: [{ name: 'example/repo', archived: false }], cards: [card('active', 'backlog', [1]), card('done', 'done', [2, 4]), card('cancelled', 'cancelled', [3])], prs: [pr(1, 'OPEN', { isDraft: true, author: 'robot[bot]' }), pr(2), pr(3), pr(4, 'CLOSED'), pr(5)] };
}

test('legacy meanings, raw rows and three-day window remain separate from inventory', () => {
  const b = bundle(); const r = report(b, { inventory: true });
  assert.equal(r.windowDays, 3); assert.equal(r.doneCardsScanned, 1); assert.equal(r.prLinks, 2);
  assert.equal(r.counts.unmerged_pr_on_done, 1); assert.equal(r.counts.closed_unmerged_on_done, 1);
  assert.equal(r.counts.stale_pr_label, 1); assert.equal(r.unmergedOnDone[0].issue, 'done');
  assert.equal(r.inventory.schema, 'm25-inventory/v2');
  assert.deepEqual(r.inventory.open.map(p => p.ownership), ['nonterminal', 'done-only', 'cancelled-only', 'no-card']);
  assert.equal(r.inventory.open[0].isDraft, true); assert.equal(r.inventory.open[0].author, 'robot[bot]');
  assert.equal(r.inventory.operationalPermission, false);
  assert.equal(report(b).inventory, undefined);
});

test('canonical dedupe preserves all cards, owners and duplicate work products', () => {
  const b = bundle(); b.cards.push(card('other', 'todo', [1, 1]));
  b.cards[3].workProducts[0].url = 'https://GITHUB.com/Example/Repo/pull/1/';
  const row = report(b, { inventory: true }).inventory.open[0];
  assert.equal(row.provenance.length, 3); assert.deepEqual(row.owners, ['owner-active', 'owner-other']);
  assert.equal(row.duplicates, 2); assert.equal(row.primaryOwner, undefined);
});

test('strict PR URL validation never accepts suffixes, unsafe hosts or numbers', () => {
  for (const u of ['http://github.com/a/b/pull/1', 'https://github.com.evil/a/b/pull/1', 'https://x@github.com/a/b/pull/1', 'https://github.com/a/b/pull/1junk', 'https://github.com/a/b/pull/0', 'https://github.com/a/b/pull/01', 'https://github.com/a/b/pull/9007199254740992', 'https://github.com/a/b/pull/1/files', 'https://github.com/a/%62/pull/1', 'https://github.com/a/b/pull/1?q=1']) assert.equal(canonicalPR(u), null, u);
  assert.equal(canonicalPR('https://GITHUB.com/A/B/pull/12/').url, 'https://github.com/a/b/pull/12');
});

test('reason on either PR or source card, supersession is not delivery or reopen authority', () => {
  const reason = `Closed unmerged: superseded by ${url(6)}; unique work: kept at ${url(6)}; follow-ups: live cards https://example.test/cards/followup`;
  assert.equal(disposition({ body: reason, comments: [], commentsComplete: true }, []).status, 'reason-found');
  const d = disposition({ body: '', comments: [], commentsComplete: true }, [{ id: 'source', body: reason, commentsComplete: true, comments: [] }]);
  assert.equal(d.status, 'reason-found'); assert.equal(d.reasons[0].source, 'card:source:body'); assert.equal(d.delivered, false);
  assert.equal(disposition({ body: '', comments: [], commentsComplete: true }, []).status, 'absent-with-complete-read');
  assert.equal(disposition({ body: '', comments: [], commentsComplete: false }, []).status, 'UNKNOWN');
  assert.equal(disposition({ body: 'Closed unmerged: superseded by nonsense', comments: [], commentsComplete: true }, []).status, 'absent-with-complete-read');
  const b = bundle(); b.prs[3].body = reason; b.prs[3].commentsComplete = true; b.prs[3].comments = [];
  const inv = report(b, { inventory: true }).inventory;
  assert.equal(inv.closedUnmerged[0].disposition.status, 'reason-found');
  assert.equal(inv.open.some(p => p.number === 6), false, 'reason links never create ownership or discovered PRs');
});

test('archived repositories are excluded explicitly, not counted as live', () => {
  const b = bundle(); b.repositories[0].archived = true;
  const r = report(b, { inventory: true });
  assert.equal(r.inventory.open.length, 0); assert.deepEqual(r.inventory.excludedArchivedRepositories, ['example/repo']);
  assert.equal(r.counts.unmerged_pr_on_done, 1, 'legacy historical series remains unchanged');
});

test('every list walker exhausts multiple pages without trusting a short server cap', async () => {
  for (const name of ['repositories', 'prs', 'cards', 'work-products', 'comments', 'projects', 'workspaces', 'interactions']) {
    const requests = [];
    const pages = [[{ id: '1' }], [{ id: '2' }], []];
    const result = await collectPages(async position => { requests.push(position); return { ok: true, body: pages.shift() }; }, { name });
    assert.deepEqual(result.rows.map(x => x.id), ['1', '2']); assert.equal(result.complete, true); assert.equal(requests.length, 3);
  }
});

test('pagination errors, partial GraphQL, loops, caps and malformed responses retain observed rows as UNKNOWN', async () => {
  for (const broken of [{ ok: false, body: { items: [{ id: 'partial' }] } }, { ok: true, body: { items: [{ id: 'partial' }], errors: [{ message: 'unavailable' }] } }, { ok: true, body: { error: 'unavailable' } }, { ok: true, body: null }]) {
    let calls = 0;
    const r = await collectPages(async () => ++calls === 1 ? { ok: true, body: [{ id: 'first' }] } : broken, { name: 'case' });
    assert.equal(r.complete, false); assert.equal(r.rows[0].id, 'first');
    if (broken.body?.items) assert.equal(r.rows[1].id, 'partial');
  }
  const partial = await collectPages(async () => ({ ok: false, body: { data: { repository: { pullRequests: { nodes: [{ id: 'kept' }], pageInfo: { hasNextPage: false, endCursor: null } } } }, errors: [{ path: ['repository'] }] } }), { name: 'graphql', connection: x => x.data?.repository?.pullRequests });
  assert.equal(partial.rows[0].id, 'kept'); assert.equal(partial.complete, false);
  const loop = await collectPages(async () => ({ ok: true, body: { items: [{ id: 'x' }], pageInfo: { hasNextPage: true, endCursor: 'same' } } }), { name: 'loop' });
  assert.equal(loop.complete, false);
  const cap = await collectPages(async n => ({ ok: true, body: [{ id: String(n) }] }), { name: 'cap', maxPages: 2 });
  assert.equal(cap.complete, false); assert.equal(cap.rows.length, 2);
});

test('coverage gaps never become passing zero totals', () => {
  const b = bundle(); b.coverage.push({ name: 'cards', complete: false, problem: 'unavailable' });
  const r = report(b, { inventory: true });
  assert.equal(r.inventory.totals.open, null); assert.equal(r.inventory.observedLowerBounds.open, 4);
  assert.equal(r.inventory.open.find(p => p.number === 5).ownership, 'UNKNOWN');
  assert.equal(r.counts.unmerged_pr_on_done, 1, 'legacy remains its observed historical count');
  assert.equal(r.legacyCompleteness, 'UNKNOWN');
});

test('label fixes fail closed for live, pending-review, human holds and unknown authorization', () => {
  const idle = { status: 'done', checkoutRunId: null, executionRunId: null, scheduledRetry: null, assigneeUserId: null, executionState: null, reviewAttention: { state: 'none' } };
  for (const [field, value] of [['executionRunId', 'live'], ['scheduledRetry', {}], ['assigneeUserId', 'human'], ['executionState', { status: 'pending', currentParticipant: { agentId: 'reviewer' } }]]) assert.notEqual(guardLabelFix({ ...idle, [field]: value }, [], true).status, 'safe');
  assert.equal(guardLabelFix(idle, [{ status: 'pending', resolverPolicy: 'human_only' }], true).status, 'skipped');
  assert.equal(guardLabelFix(null, [], true).status, 'UNKNOWN');
  assert.equal(guardLabelFix(idle, [], false).status, 'UNKNOWN');
  assert.equal(guardLabelFix(idle, [], true).status, 'UNKNOWN', 'no documented atomic source authorization means no write');
});

test('frozen snapshot demonstrates separate legacy, live inventory and legitimate supersession', () => {
  const b = JSON.parse(readFileSync(new URL('./fixtures/unmerged-prs.snapshot.json', import.meta.url), 'utf8'));
  const r = report(b, { inventory: true });
  assert.equal(r.counts.closed_unmerged_on_done, 1);
  assert.equal(r.inventory.closedUnmerged[0].disposition.status, 'reason-found');
  assert.equal(r.inventory.open.length, 2);
});

test('collector wires exhaustive native lists, owner repository enumeration and partial aliases without writes', async () => {
  const oldCompany = process.env.PAPERCLIP_COMPANY_ID;
  process.env.PAPERCLIP_COMPANY_ID = 'fixture';
  const called = [];
  const sources = [card('active', 'backlog', [1]), card('done', 'done', [2])];
  const native = async path => {
    called.push(path); const u = new URL('https://fixture.test' + path); const offset = Number(u.searchParams.get('offset'));
    let rows;
    if (u.pathname.endsWith('/issues')) rows = sources.map(({ workProducts, ...c }) => c);
    else if (u.pathname.endsWith('/projects')) rows = [{ id: 'project' }];
    else if (u.pathname.endsWith('/workspaces')) rows = [{ sourceType: 'git_repo', repoUrl: 'https://github.com/example/repo' }];
    else if (u.pathname.endsWith('/work-products')) rows = sources.find(c => u.pathname.includes('/' + c.id + '/')).workProducts;
    else if (u.pathname.endsWith('/comments')) rows = [{ id: 'c1', body: '' }, { id: 'c2', body: '' }];
    else throw new Error('unexpected-native-route');
    return { ok: true, body: rows.slice(offset, offset + 1) };
  };
  const github = async (path, query) => {
    if (query) return { ok: false, body: { data: { p0: { pullRequest: { state: 'OPEN', isDraft: true, headRefOid: 'a'.repeat(40), body: '' } }, p1: { pullRequest: { state: 'CLOSED', isDraft: false, headRefOid: 'b'.repeat(40), body: '' } } }, errors: [{ path: ['p0'] }] } };
    const u = new URL('https://fixture.test/' + path), page = Number(u.searchParams.get('page'));
    if (u.pathname.endsWith('/repos')) return { ok: true, body: page === 1 ? [{ full_name: 'example/repo' }] : [] };
    if (u.pathname.endsWith('/pulls')) return { ok: true, body: page === 1 ? [{ number: 1, html_url: url(1), state: 'open', draft: true, head: { sha: 'a'.repeat(40) }, body: '' }] : [] };
    if (u.pathname.endsWith('/comments') || u.pathname.endsWith('/reviews')) return { ok: true, body: page < 3 ? [{ id: String(page), body: '' }] : [] };
    if (u.pathname.startsWith('/users/')) return { ok: true, body: { type: 'Organization' } };
    return { ok: true, body: { archived: false } };
  };
  try {
    const b = await collect({ inventory: true, now: clock, scope: { repositories: ['example/repo'], evidence: ['frozen audit'] } }, native, github);
    const r = report(b, { inventory: true });
    assert.equal(r.counts.closed_unmerged_on_done, 1, 'successful partial alias must survive');
    assert.equal(r.inventory.completeness, 'UNKNOWN');
    assert.equal(r.inventory.closedUnmerged[0].disposition.status, 'absent-with-complete-read');
    assert(called.some(p => p.includes('/workspaces?') && p.includes('offset=1')));
    assert(called.some(p => p.includes('/comments?') && p.includes('offset=2')));
    assert(called.every(p => !p.includes('checkout')), 'collector never checks out sources');
  } finally {
    if (oldCompany === undefined) delete process.env.PAPERCLIP_COMPANY_ID; else process.env.PAPERCLIP_COMPANY_ID = oldCompany;
  }
});

test('fixed-clock replay is byte-identical and does not mutate its input', () => {
  const b = bundle(); const before = JSON.stringify(b); const a = JSON.stringify(report(b, { inventory: true }));
  assert.equal(a, JSON.stringify(report(b, { inventory: true }))); assert.equal(JSON.stringify(b), before);
});
