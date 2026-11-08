import { createHash } from 'node:crypto';

export const SCHEMA = 'access-gap.v1';
export const RULES = 'access-gap-rules.v1';
export const LIMIT = 20000;
const TIERS = ['read', 'write', 'reserved'];
const INTENTS = {
  scoped_tool_use: 'agent_doable_with_access', host_os: 'host_only',
  board_setting: 'board_only', credential_create_rotate: 'owner_credential',
};
export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  return value;
}
export const hash = value => createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex');
const sort = rows => rows.sort((a, b) => JSON.stringify(canonical(a)).localeCompare(JSON.stringify(canonical(b)), 'en'));
const unique = values => [...new Set(values)].sort();
function need(ok, message) { if (!ok) throw new Error(message); }
function id(value) { need(typeof value === 'string' && /^[A-Za-z0-9_.:/@-]{1,256}$/.test(value), 'Invalid evidence identifier'); return value; }
export function utc(value) {
  need(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value), 'Expected UTC instant');
  const ms = Date.parse(value);
  need(Number.isFinite(ms) && new Date(ms).toISOString() === value.replace(/Z$/, value.includes('.') ? 'Z' : '.000Z'), 'Invalid calendar instant');
  return ms;
}
function dedupe(rows) {
  need(Array.isArray(rows) && rows.length <= LIMIT, 'Missing or excessive rows');
  const found = new Map();
  for (const row of rows) {
    id(row?.id);
    if (found.has(row.id)) need(hash(found.get(row.id)) === hash(row), 'Conflicting duplicate evidence');
    else found.set(row.id, row);
  }
  return sort([...found.values()]);
}
export function coverage(rows, receipt, window) {
  const reasons = [];
  if (receipt?.complete !== true) reasons.push('not_complete');
  if (receipt?.truncated !== false) reasons.push('truncation_unknown_or_true');
  if (!Array.isArray(receipt?.errors) || receipt.errors.length) reasons.push('unavailable_input');
  if (receipt?.since !== window.since || receipt?.until !== window.until) reasons.push('window_mismatch');
  if (!Array.isArray(receipt?.pages) || !receipt.pages.length || receipt.pages.some((p, i) => p !== i + 1)) reasons.push('pagination_gap');
  if (receipt?.recordCount !== rows.length || receipt?.recordsSha256 !== hash(sort([...rows]))) reasons.push('membership_mismatch');
  if (!receipt?.revision || !/^[a-f0-9]{64}$/.test(receipt?.sourceSha256 ?? '')) reasons.push('missing_provenance');
  return { status: reasons.length ? 'UNKNOWN' : 'complete', reasons, observed: rows.length,
    revision: receipt?.revision ?? null, sourceSha256: receipt?.sourceSha256 ?? null };
}

// Finite typed intent is a diagnosis only. Free text, quotes and mixed intent stay UNKNOWN.
function classify(interaction) {
  const intent = interaction.requestEvidence;
  const kinds = unique(intent?.kinds ?? []);
  const category = intent?.complete === true && intent?.sourceInteractionId === interaction.id && kinds.length === 1
    ? (Object.hasOwn(INTENTS, kinds[0]) ? INTENTS[kinds[0]] : 'unknown-mixed') : 'unknown-mixed';
  return { category, reason: category === 'unknown-mixed' ? 'no_single_supported_complete_intent' : `typed_${kinds[0]}` };
}
function errorClass(text) {
  const http = code => new RegExp(`(?:^${code}(?:\\s|$)|\\b(?:HTTP(?:/\\d(?:\\.\\d)?)?|status(?:Code)?|response|code)["\\s:=]*${code}\\b|->\\s*${code}\\b)`, 'i').test(text);
  if (/tools\/list[^\n]*403|discovery[^\n]*(?:failed|403)|connection[^\n]*(?:broken|unavailable)/i.test(text)) return 'discovery_fault';
  if (http(429) || /quota[^\n]*(?:exceeded|exhausted|insufficient)|(?:exceeded|exhausted|insufficient)[^\n]*quota|rate.limit(?:ed|[^\n]*(?:exceeded|exhausted|reached))/i.test(text)) return 'quota';
  if (/denied.by.policy|policy[_ -](?:denied|refusal)|forbidden.by.policy/i.test(text)) return 'policy_refusal';
  if (/missing.grant|not.enabled.for.(?:this.)?agent|capability.not.granted/i.test(text)) return 'missing_grant';
  if (http(401) || /missing.credentials?|credentials?.*(?:absent|unavailable)|github_identity_unavailable/i.test(text)) return 'credential_absence';
  if (/command not found|executable doesn't exist|unknown skill/i.test(text)) return 'missing_tool';
  if (http(403) || /permission denied|\bforbidden\b/i.test(text)) return 'unknown_access';
  return null;
}
export function gapKey({ group, gapClass, targetScope, tier, agentId = null, schemaRevision = null }) {
  return `gap:${hash([group, gapClass, targetScope, tier,
    gapClass === 'missing_policy_grant' ? agentId : null,
    gapClass === 'provider_api_drift' ? schemaRevision : null])}`;
}

function providerState(p) {
  const reasons = [];
  const map = p.capabilityMap, snapshot = p.snapshot;
  if (!map?.revision || !/^[a-f0-9]{64}$/.test(map?.sha256 ?? '') || !/^https:\/\//.test(map?.sourceUrl ?? '') || !Array.isArray(map?.operations)) reasons.push('missing_capability_map');
  if (!snapshot?.revision || !/^[a-f0-9]{64}$/.test(snapshot?.sha256 ?? '') || !/^https:\/\//.test(snapshot?.sourceUrl ?? '') || !Array.isArray(snapshot?.operations)) reasons.push('missing_provider_snapshot');
  if (p.inventoryComplete !== true || !Array.isArray(p.tools) || typeof p.packagePresent !== 'boolean') reasons.push('missing_tool_inventory');
  if (p.policyComplete !== true || !p.policyRevision || !Array.isArray(p.grants)) reasons.push('missing_policy_inventory');
  if (snapshot?.previousSha256 != null && !/^[a-f0-9]{64}$/.test(snapshot.previousSha256)) reasons.push('invalid_previous_snapshot');
  const grants = new Map();
  for (const grant of p.grants ?? []) for (const operation of grant.operationIds ?? []) {
    const key = hash([grant.agentId, grant.scope, grant.tier, operation]);
    const state = hash([grant.eligible, grant.granted, grant.prohibited]);
    if (grants.has(key) && grants.get(key) !== state) reasons.push('conflicting_policy_inventory');
    grants.set(key, state);
  }
  const ops = map?.operations ?? [];
  const operationIds = ops.map(op => op.id);
  if (new Set(operationIds).size !== operationIds.length) reasons.push('duplicate_operation_mapping');
  for (const op of ops) {
    if (!op.id || !op.scope || !TIERS.includes(op.tier) || typeof op.entitled !== 'boolean') reasons.push('incomplete_scope_mapping');
    if (op.entitled && (!op.tool || !['implemented', 'reviewed', 'enabled'].includes(op.status))) reasons.push('unimplemented_operation');
  }
  for (const op of snapshot?.operations ?? []) if (!ops.some(m => m.id === op)) reasons.push('unmapped_api_operation');
  return { group: p.group, package: p.package, status: reasons.length ? 'UNKNOWN' : 'complete', reasons: unique(reasons),
    snapshotRevision: snapshot?.revision ?? null, snapshotSha256: snapshot?.sha256 ?? null,
    policyRevision: p.policyRevision ?? null, capabilityMapSha256: map?.sha256 ?? null,
    tiers: Object.fromEntries(TIERS.map(tier => [tier, {
      entitled: ops.filter(op => op.tier === tier && op.entitled).length,
      implemented: ops.filter(op => op.tier === tier && op.entitled && ['implemented', 'reviewed', 'enabled'].includes(op.status)).length,
      reviewed: ops.filter(op => op.tier === tier && op.entitled && ['reviewed', 'enabled'].includes(op.status)).length,
      enabled: ops.filter(op => op.tier === tier && op.entitled && op.status === 'enabled').length,
    }])) };
}

export function evaluateAccessGaps(input) {
  need(input?.schema === 'access-gap-input.v1', 'Unsupported input schema');
  const { window, asOf, operatorId } = input;
  const start = utc(window?.since), end = utc(window?.until), observed = utc(asOf);
  need(end > start && end <= observed && start % 86400000 === 0 && end % 86400000 === 0, 'Expected complete UTC-day windows');
  id(operatorId);
  const inWindow = value => { const t = utc(value); return t >= start && t < end; };
  const streams = ['interactions', 'runs', 'toolOutputs', 'cards', 'cardEvents', 'incidents', 'providers'];
  const rows = {}, sources = {};
  for (const stream of streams) {
    rows[stream] = dedupe(input[stream] ?? []);
    sources[stream] = coverage(rows[stream], input.coverage?.[stream], window);
  }
  const allInteractions = rows.interactions.filter(x => inWindow(x.createdAt));
  const interactions = allInteractions.filter(x => x.addresseeUserId === operatorId).map(x => ({
    id: x.id, issueId: x.issueId ?? null, runId: x.sourceRunId ?? null, agentId: x.requestedByAgentId ?? null,
    outcome: x.status ?? 'unknown', platformConnectionRequest: x.kind === 'connection_request', ...classify(x),
  }));
  const runs = rows.runs.filter(x => inWindow(x.createdAt));
  const runById = new Map(runs.map(r => [r.id, r]));
  const evidence = new Map(interactions.map(x => [`interaction:${x.id}`, { ...x, source: 'interactions' }]));
  const failures = [], diagnostics = [];
  for (const event of rows.toolOutputs) {
    if (!inWindow(event.at)) continue;
    if (event.origin !== 'tool_result' || event.instructionRead === true || typeof event.text !== 'string') continue;
    const run = runById.get(event.runId);
    if (!run) {
      if (!rows.runs.some(r => r.id === event.runId)) {
        sources.toolOutputs.status = 'UNKNOWN'; sources.toolOutputs.reasons.push('unjoined_run');
      }
      continue;
    }
    const kind = errorClass(event.text);
    if (!kind) continue;
    const item = { id: event.id, evidenceKey: `tool:${event.id}`, runId: run.id, agentId: run.agentId,
      kind, outputSha256: hash(event.text), callId: event.callId };
    // Never emit raw output: it may contain credentials or rows. Native run/call IDs locate it.
    (event.expectedRefusal === true ? diagnostics : failures).push(item);
    if (event.expectedRefusal !== true) evidence.set(item.evidenceKey, { ...item, source: 'toolOutputs' });
  }
  const stock = rows.cards.filter(c => ['blocked', 'deferred', 'backlog'].includes(c.status) && c.accessRelated === true)
    .map(c => ({ id: c.id, status: c.status, gapKey: c.gapKey ?? null }));
  for (const card of stock) evidence.set(`card:${card.id}`, { ...card, source: 'cards' });
  const flow = rows.cardEvents.filter(e => inWindow(e.at) && e.accessRelated === true && ['blocked', 'deferred'].includes(e.kind))
    .map(e => ({ id: e.id, cardId: e.cardId, kind: e.kind }));
  const providerGroups = unique(rows.providers.map(p => id(p.group)));
  need(providerGroups.length === rows.providers.length, 'Duplicate provider group');
  const providerCoverage = rows.providers.map(p => { id(p.package); return providerState(p); });
  const candidates = new Map(), refused = [];
  for (const incident of rows.incidents) {
    if (!inWindow(incident.at)) continue;
    const p = rows.providers.find(p => p.group === incident.group);
    const state = providerCoverage.find(p => p.group === incident.group);
    const keys = unique(incident.evidenceKeys ?? []);
    const linked = keys.map(k => evidence.get(k));
    let reason = null;
    if (!p || !keys.length || linked.some(x => !x)) reason = 'missing_evidence_join';
    else if (linked.some(x => sources[x.source].status !== 'complete' || (x.source === 'toolOutputs' && sources.runs.status !== 'complete'))) reason = 'incomplete_event_source';
    else if (sources.incidents.status !== 'complete' || sources.providers.status !== 'complete') reason = 'incomplete_proposal_input';
    else if (incident.validation?.completeRequest !== true || incident.validation?.currentCapabilityPolicy !== true || !incident.validation?.revision) reason = 'request_not_independently_validated';
    else if (linked.some(x => x.kind === 'policy_refusal' || x.kind === 'discovery_fault' || x.kind === 'quota' || x.platformConnectionRequest)) reason = 'repair_or_refusal_not_permission';
    else if (linked.some(x => x.category && x.category !== 'agent_doable_with_access')) reason = 'not_agent_doable_evidence';
    else if (state.reasons.some(r => r !== 'unimplemented_operation')) reason = 'unknown_provider_inputs';
    const op = p?.capabilityMap?.operations?.find(op => op.id === incident.operation);
    if (!reason && (!op || op.entitled !== true || !TIERS.includes(op.tier) || !op.scope)) reason = 'unmapped_or_not_entitled';
    if (!reason && (incident.tier !== op.tier || incident.targetScope !== op.scope)) reason = 'scope_or_tier_mismatch';
    if (!reason && linked.some(e => e.agentId && e.agentId !== incident.agentId)) reason = 'agent_join_mismatch';
    if (reason) { refused.push({ id: incident.id, reason }); continue; }
    let gapClass;
    if (p.packagePresent === false) gapClass = 'missing_group_package';
    else if (incident.schemaRevision === p.snapshot.revision && /^[a-f0-9]{64}$/.test(p.snapshot.previousSha256 ?? '') && p.snapshot.previousSha256 !== p.snapshot.sha256
      && p.snapshot.addedOperations?.includes(incident.operation) && !p.tools.some(t => t.operation === op.id && t.name === op.tool)) gapClass = 'provider_api_drift';
    else if (p.tools?.some(t => t.operation === op.id && t.name === op.tool)) {
      const grant = p.grants?.find(g => g.agentId === incident.agentId && g.scope === op.scope && g.tier === op.tier && g.operationIds?.includes(op.id));
      if (grant?.eligible === true && grant.granted === false && grant.prohibited === false) gapClass = 'missing_policy_grant';
    }
    if (!gapClass) { refused.push({ id: incident.id, reason: 'no_eligible_remediation' }); continue; }
    const identity = { group: p.group, gapClass, targetScope: gapClass !== 'missing_policy_grant' ? 'full-surface' : op.scope,
      tier: gapClass !== 'missing_policy_grant' ? 'all' : op.tier,
      agentId: gapClass === 'missing_policy_grant' ? incident.agentId : null,
      schemaRevision: gapClass === 'provider_api_drift' ? p.snapshot.revision : null };
    if (identity.agentId !== null) id(identity.agentId);
    const key = gapKey(identity);
    const candidate = candidates.get(key) ?? { ...identity, gapKey: key, package: p.package,
      evidenceKeys: [], incidentIds: [], agents: [], runs: [], validationRevisions: [],
      capabilityMapSha256: state.capabilityMapSha256, snapshotSha256: state.snapshotSha256,
      policyRevision: state.policyRevision, coverageStatus: state.status, coverageCaveats: state.reasons,
      authorization: 'proposal_only', route: gapClass === 'missing_policy_grant' ? 'connection_config_authorization' : 'existing_package_source_review',
      materialReproduced: false, ownerSeed: false };
    candidate.evidenceKeys.push(...keys); candidate.incidentIds.push(incident.id);
    candidate.agents.push(incident.agentId); candidate.runs.push(...linked.map(e => e.runId).filter(Boolean));
    candidate.validationRevisions.push(incident.validation.revision);
    candidate.materialReproduced ||= incident.validation.materialReproduced === true;
    candidate.ownerSeed ||= incident.validation.ownerSeed === true;
    candidates.set(key, candidate);
  }
  const proposals = [], updates = [], deferred = [];
  const ranked = [...candidates.values()].map(c => {
    for (const k of ['evidenceKeys', 'incidentIds', 'agents', 'runs', 'validationRevisions']) c[k] = unique(c[k]);
    c.agents.forEach(id);
    return c;
  }).sort((a, b) => b.agents.length - a.agents.length || b.runs.length - a.runs.length || a.gapKey.localeCompare(b.gapKey, 'en'));
  need(Number.isSafeInteger(input.newCardsAlreadyCreated) && input.newCardsAlreadyCreated >= 0 && input.newCardsAlreadyCreated <= 3, 'Invalid daily card count');
  for (const c of ranked) {
    if (sources.cards.status !== 'complete') { deferred.push({ ...c, reason: 'unknown_existing_cards' }); continue; }
    const matches = rows.cards.filter(card => !['done', 'cancelled', 'closed'].includes(card.status) && (card.gapKey === c.gapKey
      || (!String(card.gapKey ?? '').startsWith('gap:') && card.group === c.group && card.gapClass === c.gapClass
        && (c.gapClass !== 'provider_api_drift' || !card.schemaRevision || card.schemaRevision === c.schemaRevision)
        && (c.gapClass !== 'missing_policy_grant' || (card.agentId === c.agentId && card.targetScope === c.targetScope && card.tier === c.tier)))));
    if (matches.length) { updates.push({ ...c, existingCardIds: unique(matches.map(m => m.id)) }); continue; }
    const sourceEvents = c.evidenceKeys.filter(k => k.startsWith('interaction:') || k.startsWith('tool:'));
    const distinctEvents = unique(sourceEvents.map(k => { const e = evidence.get(k); return e.runId ? `run:${e.runId}` : k; }));
    if (!c.materialReproduced && !c.ownerSeed && distinctEvents.length < 2) { deferred.push({ ...c, reason: 'insufficient_distinct_source_events' }); continue; }
    if (proposals.length + input.newCardsAlreadyCreated >= 3) { deferred.push({ ...c, reason: 'daily_cap' }); continue; }
    proposals.push(c);
  }
  const ledger = rows.cardEvents.filter(e => inWindow(e.at) && ['opened', 'merged', 'enabled', 'adopted'].includes(e.kind));
  const enabled = rows.cardEvents.filter(e => e.kind === 'enabled' && utc(e.at) <= observed && e.authorized === true && e.success !== false && e.version && TIERS.includes(e.tier) && e.agentId);
  const outcomes = ledger.map(e => ({ id: e.id, cardId: e.cardId, kind: e.kind, at: e.at,
    status: sources.cardEvents.status === 'complete'
      && (e.kind !== 'enabled' || enabled.some(x => x.id === e.id))
      && (e.kind !== 'adopted' || (e.authorized === true && e.success === true && enabled.some(x => x.cardId === e.cardId && x.version === e.version && x.tier === e.tier && x.agentId === e.agentId && utc(x.at) <= utc(e.at)))) ? 'evidenced' : 'UNKNOWN' }));
  const counts = Object.fromEntries([...Object.values(INTENTS), 'unknown-mixed'].map(category => [category, interactions.filter(x => x.category === category).length]));
  const failureRuns = unique(failures.map(e => e.runId));
  const pct = (n, d, complete) => complete && d > 0 ? n / d * 100 : null;
  const completeInteractions = sources.interactions.status === 'complete';
  const completeRuns = sources.runs.status === 'complete' && sources.toolOutputs.status === 'complete';
  return canonical({ schema: SCHEMA, rules: RULES, window, asOf, inputSha256: hash(input), coverage: sources,
    units: { interactions: 'unique interaction ID created in window', failures: 'unique run ID; overlapping signals not additive', stock: 'unique card ID at asOf', flow: 'unique status event ID' },
    metrics: { operatorInteractions: { status: completeInteractions ? 'complete' : 'UNKNOWN', observed: interactions.length, totalInteractions: allInteractions.length, byCategory: counts,
      agentDoablePer100Interactions: pct(counts.agent_doable_with_access, allInteractions.length, completeInteractions), unknownFraction: pct(counts['unknown-mixed'], interactions.length, completeInteractions) },
      accessFailures: { status: completeRuns ? 'complete' : 'UNKNOWN', observedUniqueRuns: failureRuns.length, totalRuns: runs.length,
        per100CoveredRuns: pct(failureRuns.length, runs.length, completeRuns), agents: unique(failures.map(e => e.agentId)),
        byKind: Object.fromEntries(unique(failures.map(e => e.kind)).map(kind => [kind, unique(failures.filter(e => e.kind === kind).map(e => e.runId)).length])) },
      accessStock: { status: sources.cards.status, observed: stock.length }, accessFlow: { status: sources.cardEvents.status, observed: flow.length } },
    interactions, failures, expectedRefusals: diagnostics, stock, flow, providerCoverage, proposals, updates, deferred, refused,
    outcomes, legacyMetrics: input.legacyMetrics ?? null,
    legacyCoverage: input.legacyCoverage ?? { status: 'UNKNOWN', reason: 'harvest_not_supplied' },
    authority: 'No classification, fixture, proposal or ledger state grants permission or proves access improved.' });
}
