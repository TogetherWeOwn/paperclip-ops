#!/usr/bin/env node
// Managed unmerged-prs candidate. Publication is a verification carrier, not installation.
// Legacy: recently done cards (--days 3), raw per-work-product rows and observed counts.
// --inventory adds m25-inventory/v2; --from-file replays a frozen snapshot, without credentials.
// --fix-labels remains limited to legacy candidates, but refuses writes without source authorization.
import { readFileSync, writeFileSync, lstatSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const sort = values => [...values].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b), 'en'));
const timestamp = v => typeof v === 'string' && Number.isFinite(Date.parse(v)) ? Date.parse(v) : NaN;
const validRepo = s => typeof s === 'string' && /^[a-z0-9][a-z0-9-]*\/[a-z0-9_.-]+$/i.test(s) && !['.', '..'].includes(s.split('/')[1]);
const hasError = b => Boolean(b && ((Object.hasOwn(b, 'error') && b.error !== null) || (Object.hasOwn(b, 'errors') && (!Array.isArray(b.errors) || b.errors.length)) || b.truncated || b.partial || b.incomplete_results));

export function canonicalPR(value) {
  if (typeof value !== 'string') return null;
  const m = /^https:\/\/github\.com\/([a-z0-9][a-z0-9-]*)\/([a-z0-9_.-]+)\/pull\/([1-9][0-9]*)\/?$/i.exec(value);
  if (!m || !validRepo(`${m[1]}/${m[2]}`)) return null;
  const number = Number(m[3]);
  if (!Number.isSafeInteger(number)) return null;
  const repo = `${m[1]}/${m[2]}`.toLowerCase();
  return { repo, number, url: `https://github.com/${repo}/pull/${number}` };
}

// Both native offsets and cursor connections are bounded and must explicitly exhaust.
// Short arrays do NOT establish exhaustion: servers may silently lower requested limits.
export async function collectPages(request, { name, maxPages = 10000, connection = b => b } = {}) {
  const rows = [], seen = new Set(), cursors = new Set();
  let position = 0, expectedTotal = null;
  const fail = problem => ({ name, rows, complete: false, problem });
  for (let page = 0; page < maxPages; page++) {
    let response;
    try { response = await request(position, page + 1); } catch { return fail('unavailable'); }
    const body = response?.body;
    let data;
    try { data = connection(body); } catch { return fail('malformed'); }
    const list = Array.isArray(data) ? data : data?.items ?? data?.nodes;
    if (!Array.isArray(list)) return fail('malformed');
    const valid = list.filter(row => row && typeof row === 'object' && !Array.isArray(row));
    const signature = JSON.stringify(list);
    if (list.length && seen.has(signature)) return fail('repeated-page');
    seen.add(signature); rows.push(...valid);
    if (valid.length !== list.length) return fail('malformed-row');
    if (response.ok !== true || hasError(body) || hasError(data)) return fail('partial-or-error');
    if (data.totalCount !== undefined) {
      if (!Number.isSafeInteger(data.totalCount) || data.totalCount < 0 || (expectedTotal !== null && expectedTotal !== data.totalCount)) return fail('malformed-total');
      expectedTotal = data.totalCount;
    }
    const info = data?.pageInfo ?? data?.pagination;
    if (info !== undefined) {
      if (!info || typeof info.hasNextPage !== 'boolean') return fail('malformed-pagination');
      if (!info.hasNextPage) {
        if (expectedTotal !== null && expectedTotal !== rows.length) return fail('total-mismatch');
        return { name, rows, complete: true };
      }
      const cursor = info.endCursor ?? info.nextCursor;
      if (typeof cursor !== 'string' || !cursor || cursors.has(cursor)) return fail('repeated-or-missing-cursor');
      cursors.add(cursor); position = cursor;
    } else {
      if (!list.length) {
        if (expectedTotal !== null && expectedTotal !== rows.length) return fail('total-mismatch');
        return { name, rows, complete: true };
      }
      position += list.length;
    }
  }
  return fail('page-cap');
}

function evidenceLink(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && !u.username && !u.password && Boolean(u.hostname) && !/[<>\s]/.test(value);
  } catch { return false; }
}

export function disposition(pr, cards) {
  const reasons = [];
  let complete = pr.commentsComplete === true && typeof pr.body === 'string';
  const sources = [{ name: 'pr', ...pr }, ...cards.map(c => ({ ...c, name: `card:${c.id}` }))];
  for (const s of sources) {
    if (s.commentsComplete !== true || typeof s.body !== 'string') complete = false;
    const texts = [{ body: s.body, source: `${s.name}:body` }, ...(s.comments ?? []).map(c => ({ body: c.body, source: `${s.name}:comment:${c.id}` }))];
    for (const item of texts) {
      if (typeof item.body !== 'string') { complete = false; continue; }
      for (const line of item.body.split(/\r?\n/)) {
        const m = /^Closed unmerged: (duplicate of|superseded by|need lost|mismatch|burden) (\S+); unique work: (none|kept at \S+); follow-ups: (none|live cards .+)$/.exec(line.trim());
        if (!m || !evidenceLink(m[2])) continue;
        if (m[3] !== 'none' && !evidenceLink(m[3].slice(8))) continue;
        if (m[4] !== 'none' && !m[4].slice(11).split(/,\s*/).every(evidenceLink)) continue;
        reasons.push({ source: item.source, reason: m[1], link: m[2], uniqueWork: m[3], followUps: m[4] });
      }
    }
  }
  return { status: reasons.length ? 'reason-found' : complete ? 'absent-with-complete-read' : 'UNKNOWN', readComplete: complete, reasons: sort(reasons), delivered: false, operationalPermission: false };
}

// Reads are not a transactional source-action grant. In particular, null run IDs do
// not prove that no queued run exists. Until a supported atomic guard is available,
// keep the option as an explicit refusal, never a check-then-write race.
export function guardLabelFix(issue, interactions, complete) {
  if (!issue || !complete || !Array.isArray(interactions)) return { status: 'UNKNOWN', reason: 'guard-read-incomplete' };
  if (issue.checkoutRunId || issue.executionRunId || issue.scheduledRetry) return { status: 'skipped', reason: 'live-or-scheduled-work' };
  if (issue.executionState?.currentParticipant || issue.executionState?.status === 'pending' || issue.status === 'in_review') return { status: 'skipped', reason: 'pending-review' };
  if (issue.assigneeUserId || issue.reviewAttention?.state === 'escalated' || interactions.some(i => i.status === 'pending')) return { status: 'skipped', reason: 'human-or-interaction-hold' };
  return { status: 'UNKNOWN', reason: 'atomic-source-authorization-unavailable' };
}

export function report(input, { inventory = false } = {}) {
  if (input?.schema !== 'm25-snapshot/v2' || !Array.isArray(input.cards) || !Array.isArray(input.prs) || !Array.isArray(input.repositories) || !Array.isArray(input.coverage) || !Number.isFinite(timestamp(input.now))) throw new Error('invalid-snapshot');
  const days = input.days ?? 3;
  if (!Number.isFinite(days) || days < 0) throw new Error('invalid-window');
  const coverage = [...input.coverage];
  const gap = (name, problem) => coverage.push({ name, complete: false, problem });
  const cards = input.cards.filter(c => {
    if (!c || typeof c.id !== 'string' || !['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled'].includes(c.status) || !Array.isArray(c.workProducts)) { gap('cards', 'malformed-card'); return false; }
    if (c.status === 'done' && !Number.isFinite(timestamp(c.completedAt || c.updatedAt))) gap('cards', 'malformed-completion-time');
    return true;
  });
  const since = timestamp(input.now) - days * 86400000;
  const recent = cards.filter(c => c.status === 'done' && timestamp(c.completedAt || c.updatedAt) >= since);
  const links = [];
  for (const c of cards) for (const w of c.workProducts) {
    if (!w || typeof w !== 'object') { gap('work-products', 'malformed'); continue; }
    if (w.type !== 'pull_request') continue;
    const p = canonicalPR(w.url);
    if (!p) gap('ownership', 'malformed-pr-url');
    links.push({ issue: c.identifier, issueId: c.id, assigneeAgentId: c.assigneeAgentId ?? null, assigneeUserId: c.assigneeUserId ?? null, status: c.status, completedAt: c.completedAt ?? null, workProductId: w.id, wpStatus: w.status, url: w.url, repo: p?.repo ?? null, number: p?.number ?? null });
  }
  const states = new Map(), conflicts = new Set();
  for (const p of input.prs) {
    const canonical = canonicalPR(p?.url);
    if (!canonical || p.number !== canonical.number || !['OPEN', 'CLOSED', 'MERGED'].includes(p.state) || typeof p.isDraft !== 'boolean') { gap('prs', 'malformed-pr'); continue; }
    const old = states.get(canonical.url);
    if (conflicts.has(canonical.url)) continue;
    if (old && JSON.stringify(old) !== JSON.stringify({ ...p, ...canonical })) { gap('prs', 'conflicting-pr'); conflicts.add(canonical.url); states.delete(canonical.url); continue; }
    states.set(canonical.url, { ...p, ...canonical });
  }
  const legacyStates = input.legacyPrs ? new Map(input.legacyPrs.filter(p => canonicalPR(p?.url) && ['OPEN', 'CLOSED', 'MERGED'].includes(p.state) && typeof p.isDraft === 'boolean').map(p => [canonicalPR(p.url).url, p])) : states;
  const legacyLinks = links.filter(l => recent.some(c => c.id === l.issueId));
  const unmergedOnDone = [], closedOnDone = [], staleLabels = [], unknown = [];
  for (const l of legacyLinks) {
    const canonical = canonicalPR(l.url), p = canonical && legacyStates.get(canonical.url);
    if (!p) { unknown.push({ issue: l.issue, url: l.url, wpStatus: l.wpStatus }); continue; }
    const row = { issue: l.issue, issueId: l.issueId, assigneeAgentId: l.assigneeAgentId, pr: `${l.repo}#${l.number}`, url: l.url, wpStatus: l.wpStatus };
    if (p.state === 'OPEN') unmergedOnDone.push({ ...row, isDraft: p.isDraft, head: p.headRefOid });
    if (p.state === 'CLOSED') closedOnDone.push(row);
    const want = p.state === 'MERGED' ? 'merged' : p.state === 'CLOSED' ? 'closed' : null;
    if (want && l.wpStatus !== want) staleLabels.push({ ...row, workProductId: l.workProductId, want, mergeSha: p.mergeCommit?.oid ?? null });
  }
  const complete = input.complete === true && coverage.every(c => c.complete === true) && unknown.length === 0;
  const out = {
    generatedAt: input.now, windowDays: days, doneCardsScanned: recent.length, prLinks: legacyLinks.length,
    counts: { unmerged_pr_on_done: unmergedOnDone.length, closed_unmerged_on_done: closedOnDone.length, stale_pr_label: staleLabels.length, stale_pr_label_before_fix: staleLabels.length, unknown: unknown.length },
    unmergedOnDone, closedOnDone, staleLabels, labelsFixed: 0, unknown,
    legacyCompleteness: complete ? 'complete' : 'UNKNOWN', labelFixes: input.labelFixes ?? [],
  };
  if (!inventory) return out;
  const archived = new Set(), repos = new Set();
  for (const r of input.repositories) {
    if (!validRepo(r?.name) || typeof r.archived !== 'boolean') { gap('repositories', 'malformed-repository'); continue; }
    repos.add(r.name.toLowerCase());
    if (r.archived) archived.add(r.name.toLowerCase());
  }
  const open = [], closedUnmerged = [], inventoryStale = [];
  const inventoryComplete = complete && coverage.every(c => c.complete === true);
  for (const p of [...states.values()].sort((a, b) => a.url.localeCompare(b.url, 'en'))) {
    if (archived.has(p.repo)) continue;
    if (!repos.has(p.repo)) { gap('repositories', 'uncovered-pr-repository'); continue; }
    const provenance = links.filter(l => canonicalPR(l.url)?.url === p.url);
    const nonterminal = provenance.some(l => !['done', 'cancelled'].includes(l.status));
    const ownership = nonterminal ? 'nonterminal' : !inventoryComplete ? 'UNKNOWN' : !provenance.length ? 'no-card' : provenance.every(l => l.status === 'cancelled') ? 'cancelled-only' : provenance.every(l => l.status === 'done') ? 'done-only' : 'terminal-mixed';
    const row = { url: p.url, repo: p.repo, number: p.number, isDraft: p.isDraft, author: p.author ?? null, head: p.headRefOid ?? null, ownership, provenance: sort(provenance), owners: sort([...new Set(provenance.flatMap(l => [l.assigneeAgentId, l.assigneeUserId]).filter(Boolean))]), duplicates: Math.max(0, provenance.length - 1), operationalPermission: false };
    if (p.state === 'OPEN') open.push(row);
    if (p.state === 'CLOSED') closedUnmerged.push({ ...row, disposition: disposition(p, cards.filter(c => provenance.some(l => l.issueId === c.id))) });
    const want = p.state === 'MERGED' ? 'merged' : p.state === 'CLOSED' ? 'closed' : null;
    if (want) for (const l of provenance) if (l.wpStatus !== want) inventoryStale.push({ url: p.url, issueId: l.issueId, workProductId: l.workProductId, observed: l.wpStatus, want, operationalPermission: false });
  }
  const allComplete = inventoryComplete && coverage.every(c => c.complete === true);
  const lower = { open: open.length, openNonterminal: open.filter(p => p.ownership === 'nonterminal').length, openNoNonterminalOwner: open.filter(p => !['nonterminal', 'UNKNOWN'].includes(p.ownership)).length, closedUnmerged: closedUnmerged.length, staleLabels: inventoryStale.length };
  out.inventory = {
    schema: 'm25-inventory/v2', operationalPermission: false, scope: input.scope ?? { claim: 'UNKNOWN' }, completeness: allComplete ? 'complete' : 'UNKNOWN',
    totals: Object.fromEntries(Object.entries(lower).map(([k, v]) => [k, allComplete ? v : null])), observedLowerBounds: lower,
    coverage: sort(coverage), excludedArchivedRepositories: [...archived].sort(), open, closedUnmerged, staleLabels: sort(inventoryStale),
    recommendations: open.filter(p => p.ownership !== 'nonterminal').map(p => ({ url: p.url, owners: p.owners, action: 'source-owner: reuse existing open finishing envelope; resolve ownership and disposition; preserve provenance and obligations', operationalPermission: false })),
  };
  return out;
}

function jsonFile(path) {
  if (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink() || lstatSync(path).size > 16 * 1024 * 1024) throw new Error('invalid-input-file');
  return JSON.parse(readFileSync(path, 'utf8'));
}

// Credentials are read only inside the requesting process. No redirects, raw errors,
// response dumps, shell interpolation or credential-bearing subprocess arguments.
async function nativeRequest(path) {
  const base = (process.env.PAPERCLIP_API_URL ?? '').replace(/\/$/, '').replace(/\/api$/, '');
  const u = new URL(base);
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || !process.env.PAPERCLIP_API_KEY) throw new Error('native-unavailable');
  const res = await fetch(`${base}/api${path}`, { headers: { Authorization: `Bearer ${process.env.PAPERCLIP_API_KEY}` }, redirect: 'error', signal: AbortSignal.timeout(30000) });
  const bytes = []; let size = 0;
  for await (const chunk of res.body) { size += chunk.length; if (size > 8 * 1024 * 1024) throw new Error('response-limit'); bytes.push(chunk); }
  return { ok: res.ok, body: JSON.parse(Buffer.concat(bytes).toString('utf8')) };
}

function githubRequest(path, query) {
  try {
    const argv = query ? ['api', 'graphql', '-f', `query=${query}`] : ['api', path];
    return { ok: true, body: JSON.parse(execFileSync('gh', argv, { encoding: 'utf8', timeout: 30000, maxBuffer: 8 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] })) };
  } catch (error) {
    // gh returns nonzero for partial GraphQL errors; keep usable aliases, never
    // treat its raw stdout/stderr as diagnostics or an empty successful response.
    try { return { ok: false, body: JSON.parse(error.stdout) }; } catch { return { ok: false, body: null }; }
  }
}

export async function collect({ inventory = false, days = 3, now = new Date().toISOString(), scope = null, fixLabels = false } = {}, native = nativeRequest, github = githubRequest) {
  const company = process.env.PAPERCLIP_COMPANY_ID;
  if (!company) throw new Error('company-required');
  const coverage = [];
  const nativeList = async (path, name) => {
    const r = await collectPages(offset => native(`${path}${path.includes('?') ? '&' : '?'}limit=100&offset=${offset}`), { name });
    coverage.push({ name, complete: r.complete, ...(r.problem ? { problem: r.problem } : {}) }); return r;
  };
  const githubList = async (path, name) => {
    const r = await collectPages((_, page) => github(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`), { name });
    coverage.push({ name, complete: r.complete, ...(r.problem ? { problem: r.problem } : {}) }); return r;
  };
  const issueResult = await nativeList(`/companies/${company}/issues${inventory ? '' : '?status=done'}`, 'cards');
  const cards = issueResult.rows.filter(c => inventory || timestamp(c.completedAt || c.updatedAt) >= timestamp(now) - days * 86400000);
  for (const c of cards) c.workProducts = (await nativeList(`/issues/${encodeURIComponent(c.id)}/work-products`, `work-products:${c.id}`)).rows;
  const linked = cards.flatMap(c => c.workProducts.filter(w => w.type === 'pull_request').map(w => canonicalPR(w.url))).filter(Boolean);
  const prs = [], legacyPrs = [], repositories = [], repoNames = new Set(linked.map(p => p.repo));
  if (inventory) {
    const projects = await nativeList(`/companies/${company}/projects`, 'projects');
    for (const project of projects.rows) {
      const ws = await nativeList(`/projects/${encodeURIComponent(project.id)}/workspaces`, `workspaces:${project.id}`);
      for (const w of ws.rows) {
        if (w.sourceType !== 'git_repo') continue;
        const m = /^https:\/\/github\.com\/([a-z0-9][a-z0-9-]*\/[a-z0-9_.-]+?)(?:\.git)?\/?$/i.exec(w.repoUrl ?? '');
        if (m && validRepo(m[1])) repoNames.add(m[1].toLowerCase());
        else coverage.push({ name: 'workspace-repository', complete: false, problem: 'unsupported-or-malformed-url' });
      }
    }
    if (!scope || !Array.isArray(scope.repositories) || !Array.isArray(scope.evidence) || !scope.evidence.length) coverage.push({ name: 'daily-pr-flow-audit', complete: false, problem: 'scope-evidence-required' });
    for (const name of scope?.repositories ?? []) {
      if (validRepo(name)) repoNames.add(name.toLowerCase());
      else coverage.push({ name: 'daily-pr-flow-audit', complete: false, problem: 'malformed-repository' });
    }
    // Listing all visible repositories of each evidenced owner avoids linked-PR-only scope.
    for (const owner of [...new Set([...repoNames].map(r => r.split('/')[0]))].sort()) {
      let account; try { account = await github(`users/${owner}`); } catch { account = null; }
      if (!account?.ok || !['Organization', 'User'].includes(account.body?.type)) {
        coverage.push({ name: `repositories:${owner}`, complete: false, problem: 'owner-kind-unavailable' });
        continue;
      }
      const endpoint = account.body.type === 'Organization' ? `orgs/${owner}/repos?type=all` : `users/${owner}/repos?type=owner`;
      // The public user endpoint cannot prove coverage of private repositories.
      if (account.body.type === 'User') coverage.push({ name: `repositories:${owner}`, complete: false, problem: 'private-user-repository-coverage-unknown' });
      const repos = await githubList(endpoint, `repositories:${owner}`);
      for (const r of repos.rows) if (validRepo(r.full_name)) repoNames.add(r.full_name.toLowerCase());
      else coverage.push({ name: 'repositories', complete: false, problem: 'malformed-row' });
    }
    for (const name of [...repoNames].sort()) {
      let r; try { r = await github(`repos/${name}`); } catch { r = null; }
      if (!r?.ok || typeof r.body?.archived !== 'boolean') { coverage.push({ name: `repository:${name}`, complete: false, problem: 'unavailable' }); continue; }
      repositories.push({ name, archived: r.body.archived });
      if (r.body.archived) continue;
      const list = await githubList(`repos/${name}/pulls?state=open&sort=created&direction=asc`, `prs:${name}`);
      for (const p of list.rows) {
        const state = p.merged_at ? 'MERGED' : p.state === 'open' ? 'OPEN' : p.state === 'closed' ? 'CLOSED' : null;
        prs.push({ number: p.number, url: p.html_url, state, isDraft: p.draft, headRefOid: p.head?.sha, mergeCommit: p.merge_commit_sha ? { oid: p.merge_commit_sha } : null, author: p.user?.login ?? null, body: p.body ?? '', comments: [], commentsComplete: false });
      }
    }
  }
  {
    // Preserve the existing batched GraphQL read even for archived legacy links.
    const recentIds = new Set(cards.filter(c => c.status === 'done' && timestamp(c.completedAt || c.updatedAt) >= timestamp(now) - days * 86400000).map(c => c.id));
    const legacyLinked = cards.filter(c => recentIds.has(c.id)).flatMap(c => c.workProducts.filter(w => w.type === 'pull_request').map(w => canonicalPR(w.url))).filter(Boolean);
    const unique = [...new Map((inventory ? linked : legacyLinked).map(p => [p.url, p])).values()];
    for (let offset = 0; offset < unique.length; offset += 40) {
      const chunk = unique.slice(offset, offset + 40);
      const query = `query{${chunk.map((p, i) => { const [owner, name] = p.repo.split('/'); return `p${i}:repository(owner:${JSON.stringify(owner)},name:${JSON.stringify(name)}){pullRequest(number:${p.number}){state isDraft mergedAt mergeCommit{oid} headRefOid body author{login}}}`; }).join(' ')}}`;
      let result; try { result = await github(null, query); } catch { result = null; }
      for (const [i, p] of chunk.entries()) {
        const value = result?.body?.data?.[`p${i}`]?.pullRequest;
        const aliasError = result?.body?.errors?.some(e => !e.path || e.path[0] === `p${i}`);
        if (value) {
          const row = { ...value, ...p, author: value.author?.login ?? null };
          if (legacyLinked.some(l => l.url === p.url)) legacyPrs.push(row);
          if (inventory && !prs.some(existing => canonicalPR(existing.url)?.url === p.url)) prs.push(row);
        }
        if (!value || aliasError) coverage.push({ name: `pr:${p.url}`, complete: false, problem: 'partial-or-unavailable' });
      }
    }
  }
  if (inventory) {
    for (const p of prs.filter(p => p.state === 'CLOSED')) {
      const canonical = canonicalPR(p.url); if (!canonical) continue;
      p.comments = []; p.commentsComplete = true;
      for (const stream of [`issues/${canonical.number}/comments`, `pulls/${canonical.number}/comments`, `pulls/${canonical.number}/reviews`]) {
        const comments = await githubList(`repos/${canonical.repo}/${stream}`, `pr-reasons:${canonical.url}:${stream}`);
        p.comments.push(...comments.rows.map(c => ({ ...c, id: `${stream}:${c.id}` })));
        p.commentsComplete &&= comments.complete;
      }
    }
    for (const c of cards) {
      c.body = c.description ?? '';
      if (!c.workProducts.some(w => prs.some(p => p.state === 'CLOSED' && canonicalPR(w.url)?.url === canonicalPR(p.url)?.url))) continue;
      const comments = await nativeList(`/issues/${encodeURIComponent(c.id)}/comments`, `card-reasons:${c.id}`);
      c.comments = comments.rows; c.commentsComplete = comments.complete;
    }
  }
  const labelFixes = [];
  if (fixLabels) {
    const legacy = report({ schema: 'm25-snapshot/v2', complete: true, now, days, cards, prs, legacyPrs, repositories, coverage });
    for (const s of legacy.staleLabels) {
      let detail; try { detail = await native(`/issues/${encodeURIComponent(s.issueId)}`); } catch { detail = null; }
      const interactions = await nativeList(`/issues/${encodeURIComponent(s.issueId)}/interactions`, `guard-interactions:${s.issueId}`);
      labelFixes.push({ workProductId: s.workProductId, ...guardLabelFix(detail?.ok ? detail.body : null, interactions.rows, interactions.complete) });
    }
  }
  return { schema: 'm25-snapshot/v2', complete: coverage.every(c => c.complete), now, days, cards, prs, legacyPrs, repositories, coverage, labelFixes, scope: { claim: 'enumerated-repositories-only', evidence: scope?.evidence ?? [], repositories: [...repoNames].sort(), sources: ['native-project-workspaces', 'linked-work-products', 'github-owner-repository-lists', 'daily-pr-flow-audit'] } };
}

async function main(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const name = argv[i];
    if (['--inventory', '--fix-labels'].includes(name)) args[name.slice(2)] = true;
    else if (['--days', '--clock', '--from-file', '--scope-file', '--out', '--snapshot-out'].includes(name) && argv[i + 1] && !argv[i + 1].startsWith('--')) args[name.slice(2)] = argv[++i];
    else throw new Error('invalid-arguments');
  }
  if (args['from-file'] && args['fix-labels']) throw new Error('replay-is-read-only');
  const days = Number(args.days ?? 3);
  if (!Number.isFinite(days) || days < 0) throw new Error('invalid-window');
  const b = args['from-file'] ? jsonFile(args['from-file']) : await collect({ inventory: args.inventory === true, fixLabels: args['fix-labels'] === true, days, ...(args.clock ? { now: args.clock } : {}), scope: args['scope-file'] ? jsonFile(args['scope-file']) : null });
  if (args.clock) b.now = args.clock;
  if (args.days) b.days = days;
  const out = report(b, { inventory: args.inventory === true });
  const json = JSON.stringify(out, null, 2) + '\n';
  if (args['snapshot-out']) writeFileSync(args['snapshot-out'], JSON.stringify(b, null, 2) + '\n');
  if (args.out) writeFileSync(args.out, json); else process.stdout.write(json);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main(process.argv.slice(2)).catch(() => { process.stderr.write('unmerged-prs: unavailable or invalid input\n'); process.exitCode = 2; });
