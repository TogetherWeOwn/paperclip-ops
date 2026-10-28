/**
 * Offline native tool boundary contract. Every identity and sentinel is synthetic.
 * Run: node --test plugins/native-runner-coverage/test/worker.test.mjs
 * No SDK bootstrap, service calls, credentials or live store reads are needed.
 */
import test from "node:test";
import assert from "node:assert/strict";

const COMPANY = "fixture-tool-company";
const AGENTS = ["fixture-tool-agent-a", "fixture-tool-agent-b", "fixture-tool-agent-c"];
const SENSITIVE = "FAKE_ONLY_SENSITIVE_MESSAGE_SENTINEL";
const STORE_KEYS = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];
const OUTPUT_KEYS = [
  "status", "reasonCodes", "pathClass", "evidenceBasis", "lifecycle",
  "leaseState", "authority", "contentScan", "stores", "backend", "target", "bindingCheck",
  "schema_id", "nextActions",
];

function policy() {
  return { companyId: COMPANY, agentIds: [...AGENTS] };
}

function fixture(agentId = AGENTS[0], companyId = COMPANY) {
  const binding = {
    companyId, agentId, runId: "fixture-tool-run", sessionId: "fixture-tool-session",
  };
  return {
    binding: { ...binding },
    agent: { id: agentId, companyId, adapterType: "paperclip_runner" },
    run: {
      id: binding.runId, companyId, agentId, status: "running", sessionId: binding.sessionId,
    },
    observation: {
      binding: { ...binding }, basis: "reported", provider: "codex",
      target: "local", lifecycle: "live", stores: { mcpHeader: "present" },
    },
  };
}

function fakeHost(configValue, configError) {
  const calls = { registrations: [], config: [], forbidden: [] };
  const guard = (object, prefix) => new Proxy(object, {
    get(target, key) {
      if (Object.hasOwn(target, key)) return target[key];
      calls.forbidden.push(`${prefix}.${String(key)}`);
      throw new Error("Unexpected host capability access");
    },
  });
  const ctx = guard({
    tools: guard({
      register(...args) { calls.registrations.push(args); },
    }, "tools"),
    config: guard({
      async get(...args) {
        calls.config.push(args);
        if (configError) throw configError;
        return structuredClone(configValue);
      },
    }, "config"),
  }, "ctx");
  return { ctx, calls };
}

async function registered(t, ...configArgs) {
  const [configValue, configError] = configArgs.length === 0 ? [policy()] : configArgs;
  const host = fakeHost(configValue, configError);
  const sideEffects = [];
  for (const name of ["log", "info", "warn", "error", "debug", "trace"]) {
    t.mock.method(console, name, () => { sideEffects.push(`console.${name}`); });
  }
  t.mock.method(globalThis, "fetch", () => {
    sideEffects.push("fetch");
    throw new Error("Network calls are forbidden in the offline fixture");
  });
  t.after(() => {
    assert.deepEqual(host.calls.forbidden, [], "only registration and scoped config reads are allowed");
    assert.deepEqual(sideEffects, [], "no logging or HTTP calls are allowed");
  });
  // Import the testable module, never the separate SDK bootstrap entrypoint.
  const { registerCoverageTool } = await import("../worker.mjs");
  assert.equal(typeof registerCoverageTool, "function");
  await registerCoverageTool(host.ctx);
  assert.equal(host.calls.registrations.length, 1);
  assert.deepEqual(host.calls.config, [], "setup must not read unscoped instance config");
  const [name, declaration, handler] = host.calls.registrations[0];
  assert.equal(name, "native_runner_coverage");
  assert.ok(declaration && typeof declaration === "object");
  assert.equal(typeof handler, "function");
  return { ...host, declaration, handler };
}

function check(response, input = fixture()) {
  assert.ok(response && typeof response === "object" && !Array.isArray(response));
  assert.deepEqual(Object.keys(response).sort(), ["content", "data"]);
  assert.equal(typeof response.content, "string");
  const output = response.data;
  assert.ok(output && typeof output === "object" && !Array.isArray(output));
  assert.ok(Object.keys(output).every((key) => OUTPUT_KEYS.includes(key)), "output must be an allowlist");
  assert.ok(["UNKNOWN", "ADVERSE", "REFUSED"].includes(output.status));
  assert.ok(Array.isArray(output.reasonCodes));
  assert.ok(output.reasonCodes.every((code) => typeof code === "string" && code.length > 0));
  assert.ok(["UNKNOWN", "DOT_STARTUP_ONLY", "DOT_RETAINED", "CODEX_LOCAL_MCP_HEADER"].includes(output.pathClass));
  assert.ok(["metadata", "reported", "source"].includes(output.evidenceBasis));
  assert.ok(["UNKNOWN", "live", "warm", "detached", "recovery", "terminal"].includes(output.lifecycle));
  assert.ok(["UNKNOWN", "unexpired_record", "expired_record", "revoked_record"].includes(output.leaseState));
  assert.equal(output.authority, "UNKNOWN");
  assert.equal(output.contentScan, "NOT_PERFORMED");
  assert.deepEqual(Object.keys(output.stores).sort(), [...STORE_KEYS].sort());
  for (const state of Object.values(output.stores)) {
    assert.ok(["UNKNOWN", "PRESENT", "NOT_FOUND_IN_OBSERVED_SCOPE"].includes(state));
  }
  if (Object.hasOwn(output, "backend")) assert.equal(output.backend, "UNKNOWN");
  if (Object.hasOwn(output, "target")) assert.ok(["UNKNOWN", "local", "remote", "custom"].includes(output.target));
  if (Object.hasOwn(output, "bindingCheck")) {
    assert.ok(["REPORTED_CONSISTENT", "INCOMPLETE", "REFUSED"].includes(output.bindingCheck));
  }
  const serialized = JSON.stringify(response);
  for (const sentinel of [COMPANY, ...AGENTS, ...Object.values(input.binding ?? {}), SENSITIVE]) {
    assert.equal(serialized.includes(sentinel), false, "output must not echo identities or content");
  }
  return output;
}

function unknownPath(output) {
  assert.equal(output.pathClass, "UNKNOWN");
  assert.deepEqual(output.stores, Object.fromEntries(STORE_KEYS.map((key) => [key, "UNKNOWN"])));
}

function adverse(output) {
  assert.equal(output.status, "ADVERSE");
  assert.equal(output.pathClass, "CODEX_LOCAL_MCP_HEADER");
  assert.equal(output.evidenceBasis, "reported");
  assert.equal(output.stores.mcpHeader, "PRESENT");
}

test("registers exactly one native tool without a setup-time config read or SDK bootstrap", async (t) => {
  const { declaration } = await registered(t);
  assert.deepEqual([...declaration.parametersSchema.required].sort(), ["agent", "binding", "run"]);
  assert.deepEqual(Object.keys(declaration.parametersSchema.properties).sort(), ["agent", "binding", "observation", "run"]);
});

for (const agentId of AGENTS) {
  test(`valid bound local header for ${agentId} is adverse and reported`, async (t) => {
    const { handler, calls } = await registered(t);
    const input = fixture(agentId);
    adverse(check(await handler(input, { companyId: COMPANY }), input));
    assert.deepEqual(calls.config, [[COMPANY]], "read config using the invocation company exactly once");
  });
}

test("wrong invocation company refuses even when caller metadata and scope match config", async (t) => {
  const { handler, calls } = await registered(t);
  const input = { ...fixture(), scope: policy() };
  const output = check(await handler(input, { companyId: "fixture-other-invocation-company" }), input);
  assert.equal(output.status, "REFUSED");
  unknownPath(output);
  assert.deepEqual(calls.config, [["fixture-other-invocation-company"]]);
});

for (const runCtx of [undefined, null, {}, { companyId: undefined }, { companyId: null }, { companyId: "" }]) {
  test(`missing invocation company refuses before config lookup: ${JSON.stringify(runCtx)}`, async (t) => {
    const { handler, calls } = await registered(t);
    const output = check(await handler(fixture(), runCtx));
    assert.equal(output.status, "REFUSED");
    unknownPath(output);
    assert.deepEqual(calls.config, []);
  });
}

for (const [label, configValue] of [
  ["missing", undefined], ["null", null], ["empty", {}], ["not an object", "fixture-invalid-config"],
  ["missing company", { agentIds: AGENTS }],
  ["missing agents", { companyId: COMPANY }],
  ["two agents", { companyId: COMPANY, agentIds: AGENTS.slice(0, 2) }],
  ["four agents", { companyId: COMPANY, agentIds: [...AGENTS, "fixture-tool-outsider"] }],
  ["duplicate agents", { companyId: COMPANY, agentIds: [AGENTS[0], AGENTS[0], AGENTS[2]] }],
  ["invalid agent", { companyId: COMPANY, agentIds: [AGENTS[0], AGENTS[1], ""] }],
]) {
  test(`missing or bad config fails closed: ${label}`, async (t) => {
    const { handler, calls } = await registered(t, configValue);
    const output = check(await handler(fixture(), { companyId: COMPANY }));
    assert.ok(["UNKNOWN", "REFUSED"].includes(output.status));
    unknownPath(output);
    assert.deepEqual(calls.config, [[COMPANY]]);
  });
}

test("config rejection is sanitized UNKNOWN with no exception logging or message echo", async (t) => {
  const error = new Error(SENSITIVE);
  error.content = SENSITIVE;
  const { handler, calls } = await registered(t, policy(), error);
  const output = check(await handler(fixture(), { companyId: COMPANY }));
  assert.equal(output.status, "UNKNOWN");
  unknownPath(output);
  assert.deepEqual(calls.config, [[COMPANY]]);
});

test("caller scope is ignored rather than replacing the valid configured policy", async (t) => {
  const { handler, calls } = await registered(t);
  const input = fixture();
  input.scope = { companyId: "fixture-caller-company", agentIds: ["fixture-tool-outsider"] };
  adverse(check(await handler(input, { companyId: COMPANY }), input));
  assert.deepEqual(calls.config, [[COMPANY]]);
});

test("caller scope cannot admit a fourth agent even when every caller binding agrees", async (t) => {
  const { handler, calls } = await registered(t);
  const input = fixture("fixture-tool-outsider");
  input.scope = { companyId: COMPANY, agentIds: [AGENTS[0], AGENTS[1], input.agent.id] };
  const output = check(await handler(input, { companyId: COMPANY }), input);
  assert.equal(output.status, "REFUSED");
  unknownPath(output);
  assert.deepEqual(calls.config, [[COMPANY]]);
});

test("caller scope cannot replace the configured company with matching foreign metadata", async (t) => {
  const { handler } = await registered(t);
  const input = fixture(AGENTS[0], "fixture-caller-company");
  input.scope = { companyId: input.binding.companyId, agentIds: [...AGENTS] };
  const output = check(await handler(input, { companyId: COMPANY }), input);
  assert.equal(output.status, "REFUSED");
  unknownPath(output);
});

for (const basis of ["measured", "synthetic"]) {
  test(`caller ${basis} provenance is normalized to reported, never independently measured`, async (t) => {
    const { handler } = await registered(t);
    const input = fixture();
    input.observation.basis = basis;
    adverse(check(await handler(input, { companyId: COMPANY }), input));
  });
}

test("source-only claims stay source-only and cannot establish header coverage", async (t) => {
  const { handler } = await registered(t);
  const input = fixture();
  input.observation.basis = "source";
  const output = check(await handler(input, { companyId: COMPANY }), input);
  assert.equal(output.status, "UNKNOWN");
  assert.equal(output.evidenceBasis, "source");
  unknownPath(output);
});

test("metadata without observation remains UNKNOWN and does not invent a reported path", async (t) => {
  const { handler } = await registered(t);
  const input = fixture();
  delete input.observation;
  const output = check(await handler(input, { companyId: COMPANY }), input);
  assert.equal(output.status, "UNKNOWN");
  assert.equal(output.evidenceBasis, "metadata");
  unknownPath(output);
});

for (const revoked of [false, true]) {
  test(`caller now cannot control lease expiry or establish authority (revoked=${revoked})`, async (t) => {
    const { handler, calls } = await registered(t);
    const input = fixture();
    input.observation.lease = { expiresAt: "2030-06-15T12:00:00.000Z", revoked };
    const baseline = check(await handler(input, { companyId: COMPANY }), input);
    adverse(baseline);
    for (const now of ["1900-01-01T00:00:00.000Z", "9999-01-01T00:00:00.000Z"]) {
      const output = check(await handler({ ...input, now }, { companyId: COMPANY }), input);
      assert.deepEqual(output, baseline, "caller now must not alter classification");
      assert.equal(output.authority, "UNKNOWN");
    }
    assert.deepEqual(calls.config, [[COMPANY], [COMPANY], [COMPANY]]);
  });
}

for (const refuse of [false, true]) {
  test(`drops extra credential, content and path properties on ${refuse ? "refusal" : "success"}`, async (t) => {
    const { handler } = await registered(t);
    const input = fixture();
    input.content = SENSITIVE;
    input.credentials = { bearer: SENSITIVE };
    input.agent.credentials = { bearer: SENSITIVE };
    input.run.transcript = SENSITIVE;
    input.observation.content = SENSITIVE;
    input.observation.rawPath = SENSITIVE;
    input.observation.stores.extra = SENSITIVE;
    if (refuse) input.observation.binding.runId = `fixture-mismatch-${SENSITIVE}`;
    const output = check(await handler(input, { companyId: COMPANY }), input);
    if (refuse) {
      assert.equal(output.status, "REFUSED");
      unknownPath(output);
    } else adverse(output);
  });
}

function noDefaults(schema) {
  if (!schema || typeof schema !== "object") return;
  assert.equal(Object.hasOwn(schema, "default"), false, "no implicit company or agent defaults");
  for (const value of Object.values(schema)) noDefaults(value);
}

test("manifest grants only one native tool and uses a separate SDK entrypoint", async () => {
  const { default: manifest } = await import("../manifest.mjs");
  assert.deepEqual(manifest.capabilities, ["agent.tools.register"]);
  assert.deepEqual(manifest.tools.map((tool) => tool.name), ["native_runner_coverage"]);
  assert.ok(["entrypoint.mjs", "./entrypoint.mjs"].includes(manifest.entrypoints.worker));
  for (const key of ["jobs", "routes", "webhooks"]) {
    assert.ok(manifest[key] === undefined || (Array.isArray(manifest[key]) && manifest[key].length === 0));
  }
  const params = manifest.tools[0].parametersSchema;
  assert.deepEqual([...params.required].sort(), ["agent", "binding", "run"]);
  assert.deepEqual(Object.keys(params.properties).sort(), ["agent", "binding", "observation", "run"]);
});

test("manifest instance policy requires one company and exactly three unique agents with no defaults", async () => {
  const { default: manifest } = await import("../manifest.mjs");
  const schema = manifest.instanceConfigSchema;
  assert.equal(schema.type, "object");
  assert.deepEqual([...schema.required].sort(), ["agentIds", "companyId"]);
  assert.deepEqual(Object.keys(schema.properties).sort(), ["agentIds", "companyId"]);
  assert.equal(schema.properties.companyId.type, "string");
  assert.ok(schema.properties.companyId.minLength >= 1);
  const agents = schema.properties.agentIds;
  assert.equal(agents.type, "array");
  assert.equal(agents.minItems, 3);
  assert.equal(agents.maxItems, 3);
  assert.equal(agents.uniqueItems, true);
  assert.equal(agents.items.type, "string");
  assert.ok(agents.items.minLength >= 1);
  noDefaults(schema);
});
