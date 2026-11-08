import { hash, utc, LIMIT } from './access-gap.mjs';

const instruction = /(?:^|[/\\\s])(?:SKILL|AGENTS|CLAUDE)\.md\b|\/skills\/[^\s]*\/files\b/i;
const sourceRead = /^(?:Read|Grep|WebFetch)\b|\b(?:cat|head|tail|sed|awk|rg|grep)\b/i;
const tests = /\b(?:node\s+--test|pytest|unittest|cargo\s+test|npm\s+(?:run\s+)?test)\b/;
const compare = (a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b), 'en');
export function receipt(rows, window, complete, errors = [], pages = [1], revision = 'paperclip-get.v1') {
  return { ...window, complete, truncated: !complete, errors, pages, revision,
    sourceSha256: hash(rows), recordCount: rows.length,
    recordsSha256: hash([...rows].map(orderKeys).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b), 'en'))) };
}
function orderKeys(value) {
  if (Array.isArray(value)) return value.map(orderKeys);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, orderKeys(value[k])]));
  return value;
}
function text(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.filter(v => v?.type === 'text').map(v => v.text ?? '').join('\n');
  return '';
}

// Parse structured results only. Runtime stderr and user prose are not themselves tool results.
// Two passes keep source/negative-test exclusions even if result frames arrive before call frames.
export function extractToolOutputs(content, run, { expectedRefusalCallIds = [] } = {}) {
  const frames = [], excluded = new Set(), expected = new Set(expectedRefusalCallIds);
  let malformed = 0;
  for (const raw of content.split('\n')) {
    if (!raw.trim()) continue;
    try {
      const rec = JSON.parse(raw);
      const chunks = typeof rec.chunk === 'string' ? rec.chunk.split('\n') : [null];
      for (const chunk of chunks) {
        if (chunk !== null && !chunk.trim()) continue;
        let event;
        try { event = chunk === null ? rec : JSON.parse(chunk); }
        catch { malformed++; continue; }
        frames.push({ event, at: rec.ts ?? event.timestamp ?? null });
      }
    } catch { malformed++; }
  }
  for (const { event } of frames) {
    if (event.type === 'acpx.tool_call' && event.toolCallId) {
      const command = `${event.name ?? ''}\n${event.status === 'completed' || event.status === 'failed' ? '' : event.text ?? ''}`;
      if (instruction.test(command) || sourceRead.test(event.name ?? '')) excluded.add(event.toolCallId);
      if (tests.test(command)) expected.add(event.toolCallId);
    }
    for (const c of event.message?.content ?? []) if (c.type === 'tool_use') {
      const command = `${c.name ?? ''} ${c.input?.command ?? c.input?.file_path ?? c.input?.url ?? ''}`;
      if (instruction.test(command) || sourceRead.test(command)) excluded.add(c.id);
      if (tests.test(command)) expected.add(c.id);
    }
  }
  const outputs = new Map();
  for (const { event, at } of frames) {
    const results = event.type === 'acpx.tool_call' && ['completed', 'failed'].includes(event.status)
      ? [{ callId: event.toolCallId, output: text(event.text) }]
      : event.type === 'user' && Array.isArray(event.message?.content)
        ? event.message.content.filter(c => c.type === 'tool_result').map(c => ({ callId: c.tool_use_id, output: text(c.content) })) : [];
    for (const result of results) {
      if (!result.callId || excluded.has(result.callId) || !result.output) continue;
      let timestamp = at;
      try { utc(timestamp); } catch { timestamp = null; }
      if (!timestamp) { malformed++; continue; }
      const record = { id: `${run.id}:${result.callId}:${hash(result.output)}`, runId: run.id, callId: result.callId,
        at: timestamp, origin: 'tool_result', text: result.output, instructionRead: false,
        expectedRefusal: expected.has(result.callId) };
      // A replayed streaming completion is one result. Conflicting/multiple output chunks
      // remain separate evidence; the scorer counts runs, not streaming hits.
      outputs.set(record.id, record);
    }
  }
  return { rows: [...outputs.values()].sort(compare), malformed };
}
function list(data, key) {
  const rows = Array.isArray(data) ? data : data?.[key] ?? data?.items;
  if (!Array.isArray(rows)) throw new Error('unknown_list');
  return rows;
}
function more(data) {
  return !Array.isArray(data) && (data.hasMore === true || data.nextCursor || data.next || data.pagination?.hasMore === true || data.pagination?.nextCursor);
}
const safeId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);

export async function collectAccessInputs({ company, operatorId, window, asOf, get, collectInteractions,
  metadata = {}, pageSize = 250, scanLimit = LIMIT, maxLogPages = 10, logBytes = 250000 }) {
  if (!safeId(company) || !safeId(operatorId)) throw new Error('invalid_identity');
  for (const n of [pageSize, scanLimit, maxLogPages, logBytes]) if (!Number.isSafeInteger(n) || n <= 0) throw new Error('invalid_bound');
  if (scanLimit > LIMIT || maxLogPages > 40 || logBytes > 1000000) throw new Error('excessive_bound');
  const start = utc(window.since), end = utc(window.until);
  if (start >= end || end > utc(asOf)) throw new Error('incomplete_window');
  const calls = new Map(), observedIssues = new Map();
  const cachedGet = async path => {
    if (!calls.has(path)) calls.set(path, Promise.resolve().then(() => get(path)));
    return calls.get(path);
  };
  const input = { schema: 'access-gap-input.v1', window, asOf, operatorId, coverage: {}, newCardsAlreadyCreated: metadata.newCardsAlreadyCreated,
    legacyMetrics: metadata.legacyMetrics ?? null, legacyCoverage: metadata.legacyCoverage ?? { status: 'UNKNOWN', reason: 'harvest_not_supplied' } };
  // Reuse the installed M26 enumerator without changing any routing verdict or legacy metric.
  try {
    const result = await collectInteractions({ company, since: window.since, until: window.until, pageSize, scanLimit, concurrency: 2,
      get: async path => {
        const data = await cachedGet(path);
        if (path.startsWith(`/companies/${company}/issues?`)) for (const card of list(data, 'issues')) {
          if (safeId(card.id)) observedIssues.set(card.id, card);
        }
        return data;
      } });
    input.interactions = result.interactions.map(x => ({ ...x, requestEvidence: metadata.interactionAnnotations?.[x.id] ?? null }));
    input.coverage.interactions = receipt(input.interactions, window, result.coverage?.complete === true,
      result.coverage?.complete ? [] : ['m26_incomplete']);
    input.cards = [...observedIssues.values()].map(c => ({ id: c.id, status: c.status, ...(metadata.cardAnnotations?.[c.id] ?? {}) }));
    // Interaction failures do not defeat the independently enumerated issue inventory.
    input.coverage.cards = receipt(input.cards, window, result.coverage?.issuePaginationComplete === true && !result.coverage?.truncated,
      result.coverage?.issuePaginationComplete && !result.coverage?.truncated ? [] : ['issue_inventory_incomplete']);
  } catch {
    input.interactions = []; input.cards = [];
    for (const k of ['interactions', 'cards']) input.coverage[k] = receipt([], window, false, ['m26_unavailable']);
  }
  const runErrors = [], logErrors = [], runs = new Map(), outputs = new Map();
  try {
    const agentsData = await cachedGet(`/companies/${company}/agents`);
    const agents = list(agentsData, 'agents');
    if (more(agentsData) || agents.length > 500) throw new Error('agent_inventory_incomplete');
    for (const agent of agents) {
      if (!safeId(agent.id)) { runErrors.push('invalid_agent'); continue; }
      try {
        const data = await cachedGet(`/companies/${company}/heartbeat-runs?agentId=${agent.id}&limit=1000`);
        const records = list(data, 'runs');
        // The shipped API caps this route at 1,000 and ignores since/status filters.
        // At the cap, timestamp ties/order are not a proof of complete enumeration.
        if (records.length >= 1000 || more(data)) runErrors.push('run_list_capped');
        for (const run of records) {
          if (!safeId(run.id) || run.agentId !== agent.id) { runErrors.push('invalid_run'); continue; }
          const created = utc(run.createdAt);
          if (created < start || created >= end) continue;
          if (runs.size >= scanLimit) { runErrors.push('run_scan_capped'); break; }
          const normalized = { id: run.id, agentId: run.agentId, createdAt: run.createdAt };
          if (runs.has(run.id) && hash(runs.get(run.id)) !== hash(normalized)) runErrors.push('conflicting_run');
          runs.set(run.id, normalized);
        }
      } catch { runErrors.push('run_list_unavailable'); }
    }
  } catch { runErrors.push('agents_unavailable'); }
  for (const run of runs.values()) {
    let offset = 0, content = '', finished = false;
    try {
      for (let page = 0; page < maxLogPages; page++) {
        const data = await cachedGet(`/heartbeat-runs/${run.id}/log?offset=${offset}&limitBytes=${logBytes}`);
        if (typeof data.content !== 'string') throw new Error('invalid_log');
        content += data.content;
        if (Buffer.byteLength(content) > maxLogPages * logBytes) throw new Error('oversize_log');
        // Only explicit EOF is sufficient; a short chunk alone does not prove EOF.
        if (data.nextOffset === null || data.hasMore === false) { finished = true; break; }
        if (!Number.isSafeInteger(data.nextOffset) || data.nextOffset <= offset) throw new Error('unknown_log_continuation');
        offset = data.nextOffset;
      }
      if (!finished) logErrors.push('log_page_cap');
      const extracted = extractToolOutputs(content, run, { expectedRefusalCallIds: metadata.expectedRefusalCalls?.[run.id] ?? [] });
      if (extracted.malformed) logErrors.push('unparsed_log_frames');
      for (const output of extracted.rows) outputs.set(output.id, output);
    } catch { logErrors.push('log_unavailable'); }
  }
  input.runs = [...runs.values()].sort(compare);
  input.toolOutputs = [...outputs.values()].sort(compare);
  input.coverage.runs = receipt(input.runs, window, !runErrors.length, [...new Set(runErrors)].sort());
  input.coverage.toolOutputs = receipt(input.toolOutputs, window, !runErrors.length && !logErrors.length, [...new Set([...runErrors, ...logErrors])].sort());
  // These joins need version-pinned source evidence; no inferred host/provider inventory.
  for (const stream of ['cardEvents', 'incidents', 'providers']) {
    input[stream] = metadata[stream] ?? [];
    input.coverage[stream] = metadata.coverage?.[stream] ?? receipt(input[stream], window, false, ['metadata_not_supplied']);
  }
  return input;
}
