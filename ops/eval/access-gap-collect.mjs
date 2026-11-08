import { hash, utc, LIMIT, dedupe } from './access-gap.mjs';

const instruction = /(?:^|[/\\\s])(?:SKILL|AGENTS|CLAUDE)\.md\b|\/skills\/[^\s]*\/files\b/i;
const sourceTool = /^(?:Read|Grep|WebFetch)(?:\s|$)/;
const namespacedSourceTool = /^(?:functions\.(?:read|grep|webfetch)|mcp__filesystem__(?:read|read_file|read_text_file|read_multiple_files|grep|webfetch))(?:\s|$)/i;
const shellTool = /^(?:functions\.)?bash(?:\s|$)/i;
const sourceProgram = /^(?:cat|head|tail|sed|awk|rg|grep)$/i;
const commandLabel = /^`?(?:cat|head|tail|sed|awk|rg|grep)\s+\S/i;
const shellWord = token => token.replace(/'([^']*)'|"([^"\\]*)"|\\([\s\S])/g, (_, single, double, escaped) => single ?? double ?? escaped);
function tokenize(value) {
  // Keep quoted arguments and arithmetic shifts together. Heredoc bodies are
  // data only for non-shell consumers; expansion-bearing input is unsupported.
  const pattern = /[0-9]*(?:<<<|<<-?|<>|>>|>\||[<>]&|[<>])|&>>?|(?:\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*"|\$\(\((?:[^()]|\([^()]*\))*\)\)|\$(?!\(\()|[^\s"'\\|;&()<>$])+|[|;&()\n]/g;
  const tokens = [], heredocs = [];
  let match, redirect = null, consumed = 0;
  while ((match = pattern.exec(value))) {
    if (value.slice(consumed, match.index).trim()) return null;
    const token = match[0];
    consumed = pattern.lastIndex;
    for (const expression of token.match(/\$\(\((?:[^()]|\([^()]*\))*\)\)/g) ?? []) {
      if (/[$`\\'"]/.test(expression.slice(3, -2))) return null;
    }
    tokens.push(token);
    if (redirect) {
      const delimiter = shellWord(token);
      if (/[\r\n]/.test(delimiter)) return null;
      heredocs.push({ delimiter, tabs: redirect.endsWith('-'), expands: token === delimiter }); redirect = null;
      tokens.heredoc = true;
    } else if (/^[0-9]*<<-?$/.test(token)) redirect = token;
    if (token === '\n' && heredocs.length) {
      let offset = pattern.lastIndex;
      for (const { delimiter, tabs, expands } of heredocs) {
        let found = false;
        while (offset < value.length) {
          const end = value.indexOf('\n', offset);
          const line = value.slice(offset, end < 0 ? value.length : end);
          offset = end < 0 ? value.length : end + 1;
          if ((tabs ? line.replace(/^\t+/, '') : line) === delimiter) { found = true; break; }
          if (expands && /[$`\\]/.test(line)) return null;
        }
        if (!found) return null;
      }
      heredocs.length = 0; pattern.lastIndex = consumed = offset;
    }
  }
  return redirect || heredocs.length || value.slice(consumed).trim() ? null : tokens;
}
function sourceCommand(command) {
  if (typeof command !== 'string') return false;
  let tokens = tokenize(command);
  if (!tokens) return null;
  const hasHeredoc = tokens.heredoc;
  let program = true, wrapper = null, operand = null, redirect = false, loopHeader = false;
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (/^[|;&()\n]$/.test(token)) {
      if (loopHeader && /^[;\n]$/.test(token)) loopHeader = 'body';
      program = true; wrapper = operand = null; redirect = false; continue;
    }
    if (/^(?:[0-9]*[<>]|&>)/.test(token)) { redirect = true; continue; }
    if (redirect) { redirect = false; continue; }
    const word = shellWord(token);
    if (loopHeader) {
      // Positional loops allow do just after the name; in-lists need a separator.
      if (loopHeader === 'name') loopHeader = 'afterName';
      else if ((loopHeader === 'afterName' || loopHeader === 'body') && token === 'do') { loopHeader = false; program = true; }
      else if ((loopHeader === 'afterName' || loopHeader === 'body') && token === 'in') loopHeader = 'list';
      continue;
    }
    if (!program) continue;
    if (operand) {
      if (operand === 'split') {
        const split = tokenize(word);
        if (!split || split.heredoc) return null;
        tokens = tokens.slice(0, index + 1).concat(split, tokens.slice(index + 1));
      }
      operand = null; continue;
    }
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(token)) continue;
    if (wrapper && word.startsWith('-')) {
      if (word === '--') { wrapper = null; continue; }
      if (wrapper === 'env') {
        if (['-u', '--unset', '-C', '--chdir', '--argv0'].includes(word)) operand = 'value';
        if (['-S', '--split-string'].includes(word)) operand = 'split';
        if (word.startsWith('--split-string=')) {
          const split = tokenize(word.slice(15));
          if (!split || split.heredoc) return null;
          tokens = tokens.slice(0, index + 1).concat(split, tokens.slice(index + 1));
        }
      } else if (/^-[p]*[vV]/.test(word)) program = false; // command -v/-V queries, rather than executes.
      continue;
    }
    if (token === word && /^(?:for|select)$/.test(word)) { loopHeader = 'name'; continue; }
    if (token === word && /^(?:if|then|elif|else|while|until|do|!|\{)$/.test(word)) { wrapper = null; continue; }
    const executable = word.split('/').at(-1);
    if (executable === 'env' || executable === 'command') { wrapper = executable; continue; }
    if (sourceProgram.test(executable)) return true;
    // A shell can execute its stdin. Do not treat its heredoc as inert API data.
    if (hasHeredoc && /^(?:sh|ash|bash|rbash|dash|ksh|mksh|zsh|csh|tcsh|fish)$/.test(executable)) return null;
    program = false;
  }
  return false;
}
const sourceRead = (name, command, execute = false) => sourceTool.test(name) || namespacedSourceTool.test(name)
  || ((execute || shellTool.test(name)) && sourceCommand(command));
const tests = /\b(?:node\s+--test|pytest|unittest|cargo\s+test|npm\s+(?:run\s+)?test)\b/;
const compare = (a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b), 'en');
export function receipt(rows, window, complete, errors = [], pages = [1], revision = 'paperclip-get.v1') {
  const records = dedupe(rows);
  return { ...window, complete, truncated: !complete, errors, pages, revision,
    sourceSha256: hash(rows), recordCount: records.length, recordsSha256: hash(records) };
}
function text(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.filter(v => v?.type === 'text').map(v => v.text ?? '').join('\n');
  return '';
}

// Parse structured results only. Runtime stderr and user prose are not themselves tool results.
// Two passes keep source/negative-test exclusions even if result frames arrive before call frames.
export function extractToolOutputs(content, run, { expectedRefusalCallIds = [] } = {}) {
  const frames = [], excluded = new Set(), expected = new Set(expectedRefusalCallIds), pending = new Map();
  const known = new Set(['acpx.tool_call', 'assistant', 'user', 'system', 'result', 'stream_event',
    'rate_limit_event', 'acpx.agent_message', 'acpx.plan', 'acpx.usage_update', 'acpx.session_info']);
  let malformed = 0;
  const add = (event, at) => {
    if (!event || typeof event !== 'object' || !known.has(event.type)) { malformed++; return; }
    if (event.type === 'acpx.tool_call' && (typeof event.toolCallId !== 'string'
      || (event.name != null && typeof event.name !== 'string')
      || (event.rawInput?.command != null && typeof event.rawInput.command !== 'string')
      || (event.text != null && typeof event.text !== 'string'))) { malformed++; return; }
    if (event.message && Object.hasOwn(event.message, 'content')) {
      const content = event.message.content;
      const blocks = Array.isArray(content) ? content.filter(block => {
        const valid = block && typeof block === 'object' && typeof block.type === 'string';
        if (valid && block.type === 'tool_use') {
          if (typeof block.id === 'string' && typeof block.name === 'string'
            && ['command', 'file_path', 'url'].every(key => block.input?.[key] == null || typeof block.input[key] === 'string')) return true;
        } else if (valid && block.type === 'tool_result') {
          if (typeof block.tool_use_id === 'string') return true;
        } else if (valid) return true;
        malformed++; return false;
      }) : [];
      if (!Array.isArray(content)) malformed++;
      event = { ...event, message: { ...event.message, content: blocks } };
    }
    frames.push({ event, at: at ?? event.timestamp ?? null });
  };
  for (const raw of content.split('\n')) {
    if (!raw.trim()) continue;
    try {
      const rec = JSON.parse(raw);
      if (typeof rec.chunk !== 'string') { add(rec, rec.ts); continue; }
      const stream = rec.stream ?? 'stdout';
      const state = pending.get(stream) ?? { text: '', at: rec.ts };
      if (!state.text) state.at = rec.ts;
      state.text += rec.chunk;
      // Outer records and API pages may split inner JSON at any byte boundary.
      // Parse complete lines, or an entire complete frame without a final newline.
      let newline;
      while ((newline = state.text.indexOf('\n')) >= 0) {
        const line = state.text.slice(0, newline); state.text = state.text.slice(newline + 1);
        if (line.trim()) { try { add(JSON.parse(line), state.at); } catch { malformed++; } }
        state.at = rec.ts;
      }
      if (state.text.trim()) {
        try { const event = JSON.parse(state.text); add(event, state.at); state.text = ''; }
        catch { /* Keep an incomplete frame until the next chunk or EOF. */ }
      } else state.text = '';
      pending.set(stream, state);
    } catch { malformed++; }
  }
  for (const state of pending.values()) if (state.text.trim()) malformed++;
  for (const { event } of frames) {
    if (event.type === 'acpx.tool_call' && event.toolCallId) {
      const name = event.name ?? '';
      const shellCommand = event.rawInput?.command ?? (event.status === 'completed' || event.status === 'failed' ? '' : event.text ?? '');
      const command = `${name}\n${shellCommand}`;
      // ACP names can be command display labels; provider arguments alone are not shell identity.
      const source = sourceRead(name, shellCommand, event.kind === 'execute');
      if (source === null) malformed++;
      if (instruction.test(command) || source !== false || commandLabel.test(name)) excluded.add(event.toolCallId);
      if (tests.test(command)) expected.add(event.toolCallId);
    }
    for (const c of event.message?.content ?? []) if (c.type === 'tool_use') {
      const command = `${c.name ?? ''} ${c.input?.command ?? c.input?.file_path ?? c.input?.url ?? ''}`;
      const source = sourceRead(c.name ?? '', c.input?.command ?? '');
      if (source === null) malformed++;
      if (instruction.test(command) || source !== false) excluded.add(c.id);
      if (tests.test(command)) expected.add(c.id);
    }
  }
  const outputs = new Map();
  for (const { event, at } of frames) {
    const results = event.type === 'acpx.tool_call' && ['completed', 'failed'].includes(event.status)
      ? [{ callId: event.toolCallId, output: text(event.text), transportStatus: event.status, isError: event.is_error ?? null }]
      : event.type === 'user' && Array.isArray(event.message?.content)
        ? event.message.content.filter(c => c.type === 'tool_result').map(c => ({ callId: c.tool_use_id, output: text(c.content), transportStatus: 'result', isError: c.is_error ?? null })) : [];
    for (const result of results) {
      if (!result.callId || excluded.has(result.callId) || !result.output) continue;
      let timestamp = at;
      try { utc(timestamp); } catch { timestamp = null; }
      if (!timestamp) { malformed++; continue; }
      const record = { id: `${run.id}:${result.callId}:${hash(result.output)}`, runId: run.id, callId: result.callId,
        at: timestamp, origin: 'tool_result', text: result.output, instructionRead: false,
        transportStatus: result.transportStatus, isError: result.isError, expectedRefusal: expected.has(result.callId) };
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
function cardDedupeReady(card) {
  if (typeof card.accessRelated !== 'boolean') return false;
  if (!card.accessRelated || ['done', 'cancelled', 'closed'].includes(card.status)) return true;
  if (String(card.gapKey ?? '').startsWith('gap:')) return /^gap:[a-f0-9]{64}$/.test(card.gapKey);
  if (typeof card.group !== 'string' || !card.group) return false;
  if (card.gapClass === 'missing_group_package' || card.gapClass === 'provider_api_drift') return true;
  return card.gapClass === 'missing_policy_grant' && typeof card.agentId === 'string' && !!card.agentId
    && typeof card.targetScope === 'string' && !!card.targetScope && ['read', 'write', 'reserved'].includes(card.tier);
}

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
    input.cards = [...observedIssues.values()].map(c => {
      const a = metadata.cardAnnotations?.[c.id] ?? {};
      return { ...Object.fromEntries(['accessRelated', 'gapKey', 'group', 'gapClass', 'agentId', 'targetScope', 'tier', 'schemaRevision']
        .filter(k => Object.hasOwn(a, k)).map(k => [k, a[k]])), id: c.id, status: c.status };
    });
    // Native identity/status win; unclassified stock and dedupe inventory are not known zero.
    const cardsComplete = result.coverage?.issuePaginationComplete === true && !result.coverage?.truncated
      && input.cards.every(cardDedupeReady);
    input.coverage.cards = receipt(input.cards, window, cardsComplete,
      cardsComplete ? [] : ['issue_inventory_or_annotations_incomplete']);
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
          const normalized = { id: run.id, agentId: run.agentId, createdAt: run.createdAt,
            ...(run.logBytes == null ? {} : { logBytes: run.logBytes }) };
          if (runs.has(run.id) && hash(runs.get(run.id)) !== hash(normalized)) runErrors.push('conflicting_run');
          runs.set(run.id, normalized);
        }
      } catch { runErrors.push('run_list_unavailable'); }
    }
  } catch { runErrors.push('agents_unavailable'); }
  for (const run of [...runs.values()].sort(compare)) {
    let offset = 0, content = '', finished = false;
    try {
      for (let page = 0; page < maxLogPages; page++) {
        const data = await cachedGet(`/heartbeat-runs/${run.id}/log?offset=${offset}&limitBytes=${logBytes}`);
        if (typeof data.content !== 'string') throw new Error('invalid_log');
        if (data.truncated === true || data.complete === false || data.errors?.length) logErrors.push('log_reported_incomplete');
        content += data.content;
        const collectedBytes = Buffer.byteLength(content);
        if (collectedBytes > maxLogPages * logBytes) throw new Error('oversize_log');
        // Offsets address UTF-8 bytes, not string characters. Retain observed
        // evidence on a gap, but never certify a skipped/overlapping segment.
        if (data.nextOffset != null && (!Number.isSafeInteger(data.nextOffset) || data.nextOffset !== collectedBytes)) {
          logErrors.push('log_offset_gap'); break;
        }
        // Only explicit EOF is sufficient; a short chunk alone does not prove EOF.
        if (data.nextOffset === null || data.hasMore === false) {
          if (run.logBytes != null && (!Number.isSafeInteger(run.logBytes) || run.logBytes < 0 || run.logBytes !== collectedBytes)) logErrors.push('log_terminal_size_mismatch');
          finished = true; break;
        }
        if (!Number.isSafeInteger(data.nextOffset) || data.nextOffset <= offset) { logErrors.push('unknown_log_continuation'); break; }
        offset = data.nextOffset;
      }
      if (!finished) logErrors.push('log_page_cap');
    } catch { logErrors.push('log_unavailable'); }
    // A later denied page does not erase complete frames already observed.
    const extracted = extractToolOutputs(content, run, { expectedRefusalCallIds: metadata.expectedRefusalCalls?.[run.id] ?? [] });
    if (extracted.malformed) logErrors.push('unparsed_log_frames');
    for (const output of extracted.rows) {
      if (!outputs.has(output.id) && outputs.size >= LIMIT) { logErrors.push('tool_output_cap'); break; }
      outputs.set(output.id, output);
    }
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
