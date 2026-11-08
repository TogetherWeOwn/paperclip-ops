import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateAccessGaps, gapKey } from './access-gap.mjs';
import { extractToolOutputs, collectAccessInputs, receipt } from './access-gap-collect.mjs';

const window = { since: '2026-01-01T00:00:00Z', until: '2026-01-02T00:00:00Z' };
const at = '2026-01-01T12:00:00Z';
const run = { id: 'r1', agentId: 'a1', createdAt: at };
const done = (id, output, status = 'failed') => ({ type: 'acpx.tool_call', toolCallId: id, text: output, status });
const wrap = event => JSON.stringify({ ts: at, stream: 'stdout', chunk: JSON.stringify(event) }) + '\n';
const streams = ['interactions', 'runs', 'toolOutputs', 'cards', 'cardEvents', 'providers', 'incidents'];
function input() {
  return { schema: 'access-gap-input.v1', window, asOf: window.until, operatorId: 'operator', newCardsAlreadyCreated: 0,
    ...Object.fromEntries(streams.map(s => [s, []])) };
}
function seal(data) {
  data.coverage = Object.fromEntries(streams.map(s => [s, receipt(data[s], window, true)]));
  return data;
}
function candidate() {
  const data = input();
  data.providers = [{ id: 'p1', group: 'forge', package: 'forge-broker', packagePresent: true,
    inventoryComplete: true, tools: [{ operation: 'list', name: 'list_tool' }],
    policyComplete: true, policyRevision: 'policy1', grants: [
      { agentId: 'a1', scope: 'repo1', tier: 'read', operationIds: ['list'], eligible: true, granted: false, prohibited: false },
    ], capabilityMap: { revision: 'map1', sha256: 'a'.repeat(64), sourceUrl: 'https://example.invalid/map',
      operations: [{ id: 'list', scope: 'repo1', tier: 'read', tool: 'list_tool', entitled: true, status: 'reviewed' }] },
    snapshot: { revision: 's2', sha256: 'b'.repeat(64), sourceUrl: 'https://example.invalid/schema', operations: ['list'] } }];
  for (const n of [1, 2]) {
    data.runs.push({ ...run, id: `r${n}` });
    data.toolOutputs.push({ id: `out${n}`, runId: `r${n}`, callId: `c${n}`, at, origin: 'tool_result', text: 'missing grant' });
    data.incidents.push({ id: `i${n}`, at, group: 'forge', operation: 'list', targetScope: 'repo1', tier: 'read', agentId: 'a1', evidenceKeys: [`tool:out${n}`],
      validation: { revision: 'v1', completeRequest: true, currentCapabilityPolicy: true } });
  }
  return data;
}
const score = data => evaluateAccessGaps(seal(data));
function collection(getLog, metadata = {}, runRecord = run) {
  return collectAccessInputs({ company: 'company', operatorId: 'operator', window, asOf: window.until,
    metadata: { newCardsAlreadyCreated: 0, ...metadata },
    collectInteractions: async ({ get }) => {
      await get('/companies/company/issues?limit=250&offset=0&includeHidden=true');
      return { interactions: [], coverage: { complete: true, issuePaginationComplete: true, truncated: false } };
    }, get: async path => {
      if (path.includes('/issues?')) return [{ id: 'card1', status: 'done' }];
      if (path.endsWith('/agents')) return [{ id: 'a1' }];
      if (path.includes('/heartbeat-runs?')) return [runRecord];
      if (path.includes('/log?')) return getLog(path);
      throw Error('unexpected_mock_route');
    } });
}

test('bot regression: operations absent from the official snapshot cannot propose grants or drift', () => {
  for (const drift of [false, true]) {
    const data = candidate(), p = data.providers[0];
    p.snapshot.operations = [];
    if (drift) {
      p.tools = []; p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list'];
      data.incidents.forEach(i => { i.schemaRevision = 's2'; });
    }
    const out = score(data);
    assert.equal(out.providerCoverage[0].status, 'UNKNOWN');
    assert.ok(out.providerCoverage[0].reasons.includes('operation_absent_from_snapshot'));
    assert.deepEqual(out.proposals, []); assert.deepEqual(out.updates, []);
  }
});
test('bot regression: added operations must belong to the current official snapshot', () => {
  const data = candidate(), p = data.providers[0];
  p.tools = []; p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list', 'unlisted'];
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  const out = score(data);
  assert.equal(out.providerCoverage[0].status, 'UNKNOWN');
  assert.ok(out.providerCoverage[0].reasons.includes('invalid_snapshot_additions'));
  assert.deepEqual(out.proposals, []); assert.deepEqual(out.updates, []);
});
test('bot regression: coverage joins mapped operations to exact installed tools', () => {
  for (const status of ['implemented', 'reviewed', 'enabled']) {
    for (const tools of [[], [{ operation: 'list', name: 'wrong' }], [{ operation: 'other', name: 'list_tool' }]]) {
      const data = candidate(), p = data.providers[0];
      p.tools = tools; p.capabilityMap.operations[0].status = status;
      assert.deepEqual(score(data).providerCoverage[0].tiers.read, { entitled: 1, implemented: 0, reviewed: 0, enabled: 0 });
    }
    const data = candidate(), p = data.providers[0]; p.capabilityMap.operations[0].status = status;
    assert.deepEqual(score(data).providerCoverage[0].tiers.read,
      { entitled: 1, implemented: 1, reviewed: status === 'implemented' ? 0 : 1, enabled: status === 'enabled' ? 1 : 0 });
    p.inventoryComplete = false;
    assert.equal(score(data).providerCoverage[0].status, 'UNKNOWN');
  }
});
test('bot regression: byte-offset gaps and overlaps cannot certify complete log coverage', async () => {
  const content = wrap(done('c1', 'HTTP 403 — forbidden'));
  for (const delta of [-1, 1]) {
    const data = await collection(path => new URLSearchParams(path.split('?')[1]).get('offset') === '0'
      ? { content, nextOffset: Buffer.byteLength(content) + delta }
      : { content: '', nextOffset: null });
    const out = evaluateAccessGaps(data);
    assert.equal(out.coverage.toolOutputs.status, 'UNKNOWN');
    assert.ok(out.coverage.toolOutputs.reasons.includes('unavailable_input'));
    assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
    assert.equal(out.metrics.accessFailures.observedUniqueRuns, 1, 'retain observed lower-bound evidence');
  }
});
test('bot regression: UTF-8 contiguous log pages and matching terminal byte counts remain complete', async () => {
  const content = wrap(done('c1', 'HTTP 403 — forbidden')), size = Buffer.byteLength(content);
  const data = await collection(path => new URLSearchParams(path.split('?')[1]).get('offset') === '0'
    ? { content, nextOffset: size } : { content: '', nextOffset: null }, {}, { ...run, logBytes: size });
  const out = evaluateAccessGaps(data);
  assert.equal(out.coverage.toolOutputs.status, 'complete');
  assert.equal(out.metrics.accessFailures.observedUniqueRuns, 1);
});
test('bot regression: terminal byte mismatch and invalid EOF offsets defeat completeness', async () => {
  const content = wrap(done('c1', 'HTTP 403')), size = Buffer.byteLength(content);
  for (const [response, logBytes] of [
    [{ content, nextOffset: null }, size + 1],
    [{ content, nextOffset: null }, -1],
    [{ content, hasMore: false, nextOffset: size + 1 }, size],
  ]) {
    const out = evaluateAccessGaps(await collection(() => response, {}, { ...run, logBytes }));
    assert.equal(out.coverage.toolOutputs.status, 'UNKNOWN');
    assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
  }
});
test('bot regression: namespaced source-read tools exclude quoted denials in both trace formats', () => {
  for (const name of ['functions.Read', 'functions.Grep', 'functions.WebFetch', 'mcp__filesystem__Read']) {
    const acp = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name, status: 'in_progress' }) + wrap(done('c1', 'HTTP 403 missing grant'));
    assert.deepEqual(extractToolOutputs(acp, run).rows, [], name);
    const stream = wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name, input: {} }] } })
      + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } });
    assert.deepEqual(extractToolOutputs(stream, run).rows, [], name);
  }
  const acp = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'functions.provider_list', status: 'in_progress' }) + wrap(done('c1', 'HTTP 403'));
  assert.equal(extractToolOutputs(acp, run).rows.length, 1, 'real provider failures remain eligible');
});

test('bot regression: provider read-like names retain real failures in both trace formats', () => {
  for (const name of ['mcp__gmail__read', 'mcp__gmail__Read', 'github.read.issue', 'github.grep.issue',
    'github.webfetch', 'read', 'ReadMetadata', 'functions.ReadMetadata']) {
    const acp = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name, status: 'in_progress' })
      + wrap(done('c1', 'HTTP 403 missing grant'));
    const stream = wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name, input: {} }] } })
      + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } });
    for (const content of [acp, stream]) {
      const parsed = extractToolOutputs(content, run);
      assert.equal(parsed.malformed, 0); assert.equal(parsed.rows.length, 1, name);
      const data = input(); data.runs = [run]; data.toolOutputs = parsed.rows;
      assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 1, name);
    }
  }
});
test('bot regression: exact source tools and shell reads still exclude quoted failures', () => {
  for (const name of ['Read', 'Grep', 'WebFetch', 'functions.Read', 'mcp__filesystem__Read', 'Bash', 'functions.Bash']) {
    const command = 'CAT fixture.json';
    const acp = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name, text: command, status: 'in_progress' })
      + wrap(done('c1', 'HTTP 403 missing grant'));
    const stream = wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name, input: { command } }] } })
      + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } });
    for (const content of [acp, stream]) assert.deepEqual(extractToolOutputs(content, run).rows, [], name);
  }
});

test('bot regression: lowercase source identities and ACP command labels exclude quoted denials', () => {
  const cases = [];
  const result = wrap(done('c1', 'HTTP 403 missing grant'));
  for (const name of ['functions.read', 'functions.grep', 'functions.webfetch', 'mcp__filesystem__read', 'functions.bash', 'bash']) {
    cases.push([name, wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name, text: 'cat fixture.json', status: 'in_progress' }) + result]);
    cases.push([name, wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name, input: { command: 'cat fixture.json' } }] } })
      + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } })]);
  }
  for (const call of [
    { name: '`cat fixture.json`' }, { name: 'grep -n deny src' }, { name: 'rg deny src' },
    { name: 'Inspect fixture', kind: 'execute', rawInput: { command: 'cat fixture.json' } },
    { name: 'Inspect fixture', kind: 'execute', rawInput: { command: 'node --test fixture.test.mjs' } },
    { name: 'Bash', rawInput: { command: 'cat fixture.json' } },
  ]) cases.push([call.name, wrap({ type: 'acpx.tool_call', toolCallId: 'c1', status: 'in_progress', ...call }) + result]);
  const leaked = cases.filter(([, content]) => {
    const parsed = extractToolOutputs(content, run);
    assert.equal(parsed.malformed, 0);
    return parsed.rows.some(row => !row.expectedRefusal);
  }).map(([name]) => name);
  assert.deepEqual(leaked, [], 'source/test output must not become access-failure evidence');
  for (const name of ['read', 'grep', 'cat', 'github.grep.issue', 'mcp__gmail__read', 'mcp__gmail__grep']) {
    const content = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name, status: 'in_progress' }) + result;
    assert.equal(extractToolOutputs(content, run).rows.length, 1, name);
  }
  const provider = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'mcp__gmail__read', text: 'cat fixture.json', status: 'in_progress' }) + result;
  assert.equal(extractToolOutputs(provider, run).rows.length, 1, 'provider arguments are not shell identity');
});

test('bot regression: shell source detection distinguishes programs from API arguments', () => {
  const result = wrap(done('c1', 'HTTP 403 missing grant'));
  const commands = [
    ['curl https://api.example.invalid/items?q=cat', true],
    ['curl https://api.example.invalid/grep', true],
    ['curl -H "X-Query: cat" https://api.example.invalid/items', true],
    ['curl --data \'{"query":"cat; grep | head"}\' https://api.example.invalid/items', true],
    ['printf "%s" "cat fixture.json"', true],
    ['cat fixture.json', false],
    ['/usr/bin/grep deny fixture.json', false],
    ['printf "%s" fixture.json | cat', false],
    ['true && grep deny fixture.json', false],
    ['true; cat fixture.json', false],
    ['true\ncat fixture.json', false],
    ['MODE=read cat fixture.json', false],
  ];
  for (const [command, eligible] of commands) {
    const calls = [
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Fetch inventory', kind: 'execute', rawInput: { command }, status: 'in_progress' }) + result,
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', text: command, status: 'in_progress' }) + result,
      wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name: 'functions.Bash', input: { command } }] } })
        + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } }),
    ];
    for (const content of calls) {
      const parsed = extractToolOutputs(content, run);
      assert.equal(parsed.malformed, 0);
      assert.equal(parsed.rows.length, eligible ? 1 : 0, command);
      const data = input(); data.runs = [run]; data.toolOutputs = parsed.rows;
      assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, eligible ? 1 : 0, command);
    }
  }
});

test('bot regression: shell prefixes and redirections exclude source reads without hiding API failures', () => {
  const commands = [
    ['env MODE=read cat fixture.json', false],
    ['/usr/bin/env -u MODE --chdir /tmp MODE=read /bin/cat fixture.json', false],
    ['env --unset=MODE -- cat fixture.json', false],
    ['env -S "MODE=read cat fixture.json"', false],
    ['command cat fixture.json', false],
    ['command -p grep deny fixture.json', false],
    ['if cat fixture.json; then true; fi', false],
    ['if true; then cat fixture.json; else true; fi', false],
    ['while grep deny fixture.json; do true; done', false],
    ['for name in cat grep; do cat fixture.json; done', false],
    ['! cat fixture.json', false],
    ['{ cat fixture.json; }', false],
    ['< fixture.json cat', false],
    ['2>/dev/null command cat fixture.json', false],
    ['env 2>/dev/null MODE=read cat fixture.json', false],
    ['command 2>&1 cat fixture.json', false],
    ['env MODE=cat curl https://api.example.invalid/items?q=grep', true],
    ['env -u cat curl -H "X-Query: cat" https://api.example.invalid/items', true],
    ['env --chdir cat curl https://api.example.invalid/items', true],
    ['command -p curl --data \'{"query":"cat; grep | head"}\' https://api.example.invalid/items', true],
    ['if true; then curl https://api.example.invalid/cat; fi', true],
    ['for name in cat grep; do curl https://api.example.invalid/items; done', true],
    ['< cat curl https://api.example.invalid/items', true],
    ['2>cat curl https://api.example.invalid/items', true],
    ['command -v cat', true],
    ['printf "%s" "env MODE=read cat fixture.json"', true],
  ];
  for (const [command, eligible] of commands) {
    const result = wrap(done('c1', 'HTTP 403 missing grant'));
    const calls = [
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Inspect request', kind: 'execute', rawInput: { command }, status: 'in_progress' }) + result,
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', text: command, status: 'in_progress' }) + result,
      wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name: 'functions.Bash', input: { command } }] } })
        + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } }),
    ];
    for (const content of calls) {
      const parsed = extractToolOutputs(content, run);
      assert.equal(parsed.malformed, 0);
      assert.equal(parsed.rows.length, eligible ? 1 : 0, command);
      const data = input(); data.runs = [run]; data.toolOutputs = parsed.rows;
      assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, eligible ? 1 : 0, command);
    }
  }
});

test('bot regression: quoted assignments and loop list keywords retain API denial evidence', () => {
  const commands = [];
  for (const program of ['cat', 'head', 'tail', 'sed', 'awk', 'rg', 'grep']) {
    for (const value of [`'${program}'`, `"${program}"`, `pre'${program}'"suffix"`, `a\\ b'${program}'`]) {
      commands.push([`env MODE=${value} curl https://api.example.invalid/items`, true]);
      commands.push([`MODE=${value} cat fixture.json`, false]);
    }
    for (const delimiter of [';', '\n']) {
      commands.push([`for name in do ${program}${delimiter} do curl https://api.example.invalid/items; done`, true]);
      commands.push([`for name in do ${program}${delimiter} do cat fixture.json; done`, false]);
    }
  }
  commands.push(['select name in do cat; do curl https://api.example.invalid/items; done', true]);
  const mismatches = [];
  for (const [command, eligible] of commands) {
    const result = wrap(done('c1', 'HTTP 403 missing grant'));
    const calls = [
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Inspect request', kind: 'execute', rawInput: { command }, status: 'in_progress' }) + result,
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', text: command, status: 'in_progress' }) + result,
      wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name: 'functions.Bash', input: { command } }] } })
        + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } }),
    ];
    for (const content of calls) {
      const parsed = extractToolOutputs(content, run);
      assert.equal(parsed.malformed, 0);
      const data = input(); data.runs = [run]; data.toolOutputs = parsed.rows;
      if (parsed.rows.length !== (eligible ? 1 : 0)
        || score(data).metrics.accessFailures.observedUniqueRuns !== (eligible ? 1 : 0)) mismatches.push(command);
    }
  }
  assert.deepEqual([...new Set(mismatches)], []);
});

test('bot regression: positional loops exclude source reads without hiding API failures', () => {
  const commands = [];
  for (const keyword of ['for', 'select']) {
    for (const name of ['f', 'do', 'in']) {
      for (const separator of [' ', '; ', '\n', '\nin cat do; ']) {
        for (const program of ['cat', 'head', 'tail', 'sed', 'awk', 'rg', 'grep']) {
          commands.push([`${keyword} ${name}${separator}do ${program} fixture.json; done`, false]);
        }
        commands.push([`${keyword} ${name}${separator}do curl https://api.example.invalid/items; done`, true]);
        commands.push([`${keyword} ${name}${separator}do curl https://api.example.invalid/items; done; cat fixture.json`, false]);
      }
    }
  }
  const mismatches = [];
  for (const [command, eligible] of commands) {
    const result = wrap(done('c1', 'HTTP 403 missing grant'));
    const calls = [
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Inspect request', kind: 'execute', rawInput: { command }, status: 'in_progress' }) + result,
      wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', text: command, status: 'in_progress' }) + result,
      wrap({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'c1', name: 'functions.Bash', input: { command } }] } })
        + wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'HTTP 403 missing grant' }] } }),
    ];
    for (const content of calls) {
      const parsed = extractToolOutputs(content, run);
      assert.equal(parsed.malformed, 0);
      const data = input(); data.runs = [run]; data.toolOutputs = parsed.rows;
      if (parsed.rows.length !== (eligible ? 1 : 0)
        || score(data).metrics.accessFailures.observedUniqueRuns !== (eligible ? 1 : 0)) mismatches.push(command);
    }
  }
  assert.deepEqual([...new Set(mismatches)], []);
});

test('review regression: explicit truncation defeats a parseable log EOF', async () => {
  const data = await collection(() => ({ content: wrap(done('c1', 'clean')), truncated: true, nextOffset: null }));
  const out = evaluateAccessGaps(data);
  assert.equal(out.coverage.toolOutputs.status, 'UNKNOWN');
  assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
});
test('review regression: unknown valid JSON event types defeat complete coverage', async () => {
  const data = await collection(() => ({ content: wrap({ type: 'function_call_output', output: 'permission denied' }), nextOffset: null }));
  assert.equal(evaluateAccessGaps(data).coverage.toolOutputs.status, 'UNKNOWN');
});
test('review regression: wrapper chunks reconstruct inner JSON across arbitrary boundaries', () => {
  const inner = JSON.stringify(done('c1', 'HTTP 403')) + '\n';
  const content = [inner.slice(0, 12), inner.slice(12, 39), inner.slice(39)]
    .map(chunk => JSON.stringify({ ts: at, stream: 'stdout', chunk })).join('\n');
  const out = extractToolOutputs(content, run);
  assert.equal(out.malformed, 0); assert.equal(out.rows.length, 1);
  assert.equal(out.rows[0].text, 'HTTP 403');
});
test('review regression: ACP Bash source reads never turn fixture text into denial evidence', () => {
  const content = wrap({ type: 'acpx.tool_call', toolCallId: 'c1', name: 'Bash', status: 'in_progress', text: 'cat fixture.json' }) + wrap(done('c1', 'missing grant'));
  assert.deepEqual(extractToolOutputs(content, run).rows, []);
});
test('review regression: a successful quota inventory is not a quota failure', () => {
  const data = input(); data.runs = [run];
  data.toolOutputs = extractToolOutputs(wrap(done('c1', '{"quota":1000,"used":0}', 'completed')), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 0);
  data.toolOutputs = extractToolOutputs(wrap({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'c2', is_error: false, content: 'quota: 1000' }] } }), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 0);
  data.toolOutputs = extractToolOutputs(wrap(done('c3', 'HTTP 403 missing grant', 'completed')), run).rows;
  assert.equal(score(data).metrics.accessFailures.observedUniqueRuns, 1, 'wrapper completion is not HTTP success');
});
test('review regression: orphan run joins defeat complete failure rates', () => {
  const data = input(); data.runs = [run];
  data.toolOutputs = [{ id: 'orphan', runId: 'missing', callId: 'c1', at, origin: 'tool_result', text: 'permission denied' }];
  const out = score(data);
  assert.equal(out.metrics.accessFailures.status, 'UNKNOWN');
  assert.equal(out.metrics.accessFailures.per100CoveredRuns, null);
});
test('review regression: card metadata cannot replace native identity or current status', async () => {
  const data = await collection(() => ({ content: '', nextOffset: null }), { cardAnnotations: { card1: { id: 'forged', status: 'blocked', accessRelated: true } } });
  assert.equal(data.cards[0].id, 'card1'); assert.equal(data.cards[0].status, 'done');
  assert.equal(evaluateAccessGaps(data).metrics.accessStock.observed, 0);
});
test('review regression: missing card annotations cannot certify complete access stock', async () => {
  const data = await collection(() => ({ content: '', nextOffset: null }));
  assert.equal(evaluateAccessGaps(data).metrics.accessStock.status, 'UNKNOWN');
});
test('review regression: contradictory grant metadata cannot depend on array order', () => {
  const data = candidate(), p = data.providers[0];
  p.grants.push({ ...p.grants[0], prohibited: true });
  for (const grants of [p.grants, [...p.grants].reverse()]) {
    p.grants = grants; const out = score(data);
    assert.equal(out.providerCoverage[0].status, 'UNKNOWN'); assert.deepEqual(out.proposals, []);
  }
});
test('review regression: invalid previous digest cannot prove schema drift', () => {
  const data = candidate(), p = data.providers[0]; p.tools = [];
  p.snapshot.previousSha256 = 'unknown'; p.snapshot.addedOperations = ['list'];
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  assert.deepEqual(score(data).proposals, []);
});
test('review regression: installed additions with an existing grant need no drift or grant card', () => {
  const data = candidate(), p = data.providers[0];
  p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list']; p.grants[0].granted = true;
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  assert.deepEqual(score(data).proposals, []);
});
test('review regression: modern drift keys retain schema revision, unlike legacy feature keys', () => {
  const data = candidate(), p = data.providers[0]; p.tools = [];
  p.snapshot.previousSha256 = 'c'.repeat(64); p.snapshot.addedOperations = ['list'];
  data.incidents.forEach(i => { i.schemaRevision = 's2'; });
  data.cards.push({ id: 'old', status: 'backlog', group: 'forge', gapClass: 'provider_api_drift',
    gapKey: gapKey({ group: 'forge', gapClass: 'provider_api_drift', targetScope: 'full-surface', tier: 'all', schemaRevision: 's1' }) });
  const out = score(data); assert.equal(out.proposals.length, 1); assert.deepEqual(out.updates, []);
});
test('review regression: inherited intent names remain unknown and counts reconcile', () => {
  const data = input();
  data.interactions = [{ id: 'ix1', createdAt: at, addresseeUserId: 'operator', requestEvidence: { complete: true, sourceInteractionId: 'ix1', kinds: ['constructor'] } }];
  const out = score(data); assert.equal(out.interactions[0].category, 'unknown-mixed');
  assert.equal(out.metrics.operatorInteractions.byCategory['unknown-mixed'], 1);
});
test('review regression: unauthorized or failed enablement cannot prove enabled or adopted', () => {
  for (const damage of [{ authorized: false }, { success: false }]) {
    const data = input();
    data.cardEvents = [
      { id: 'enable', cardId: 'card1', kind: 'enabled', at, authorized: true, success: true, version: 'v1', tier: 'read', agentId: 'a1', ...damage },
      { id: 'use', cardId: 'card1', kind: 'adopted', at, authorized: true, success: true, version: 'v1', tier: 'read', agentId: 'a1' },
    ];
    const out = score(data); assert.ok(out.outcomes.every(e => e.status === 'UNKNOWN'));
  }
});
