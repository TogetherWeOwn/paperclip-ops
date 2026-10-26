/**
 * Immutable, source-only acceptance for the paired result-contract build.
 * Run: node --test plugins/native-runner-coverage/test/result-contract.acceptance.test.mjs
 * Baseline behavior is observed only through public exports; no implementation
 * diff, SDK bootstrap, native/store API, installation or credentials are used.
 *
 * Public contract selected here: result-contract.mjs exports RESULT_SCHEMA and
 * REMEDIATION_CATALOGUE. Each result adds schema_id and nextActions, in reason
 * order, with one { reasonCode, action, instruction } per existing reason.
 * Instructions below are the complete fixed allowlist, not interpolated advice.
 * Existing tests remain unchanged. The builder must not edit this acceptance.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { classifyCoverage } from "../classifier.mjs";
import { registerCoverageTool } from "../worker.mjs";

const VERSION = "native-runner-coverage-result.v1";
const COMPANY = "fixture-result-company";
const AGENTS = ["fixture-result-agent-a", "fixture-result-agent-b", "fixture-result-agent-c"];
const NOW = "2030-06-15T12:00:00.000Z";
const SENTINEL = "FAKE_ONLY_RESULT_PRIVACY_SENTINEL";
const STORE_KEYS = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];
const BASE_REASONS = ["BACKEND_UNOBSERVABLE", "AUTHORITY_NOT_TESTED", "CONTENT_NOT_SCANNED"];
const REPORTED = [...BASE_REASONS, "COVERAGE_INCOMPLETE", "REPORTED_NOT_MEASURED"];
const ACTIONS = {
  CORRECT_SUPPLIED_METADATA: "Correct supplied metadata only from facts already available within the authorized scope; never invent missing observations.",
  USE_AUTHORIZED_NATIVE_SURFACE: "Use only an already-authorized native metadata surface if it exposes the missing fact; otherwise keep it UNKNOWN and report the unsupported boundary.",
  PRESERVE_UNKNOWN_AND_REPORT: "Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration.",
};
const REASON_ACTION = {
  BACKEND_UNOBSERVABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  AUTHORITY_NOT_TESTED: "PRESERVE_UNKNOWN_AND_REPORT",
  CONTENT_NOT_SCANNED: "PRESERVE_UNKNOWN_AND_REPORT",
  COVERAGE_INCOMPLETE: "USE_AUTHORIZED_NATIVE_SURFACE",
  INVALID_POLICY: "PRESERVE_UNKNOWN_AND_REPORT",
  COMPANY_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  AGENT_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  ADAPTER_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  RUN_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  SESSION_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  MISSING_BINDING_METADATA: "CORRECT_SUPPLIED_METADATA",
  MISSING_SESSION_JOIN: "USE_AUTHORIZED_NATIVE_SURFACE",
  MISSING_RECEIPT_BINDING: "CORRECT_SUPPLIED_METADATA",
  UNSUPPORTED_EVIDENCE_BASIS: "CORRECT_SUPPLIED_METADATA",
  SOURCE_ONLY: "PRESERVE_UNKNOWN_AND_REPORT",
  SYNTHETIC_FIXTURE: "PRESERVE_UNKNOWN_AND_REPORT",
  REPORTED_NOT_MEASURED: "PRESERVE_UNKNOWN_AND_REPORT",
  STORE_SCOPE_UNOBSERVABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE: "PRESERVE_UNKNOWN_AND_REPORT",
  RETAINED_PROVIDER_STATE: "PRESERVE_UNKNOWN_AND_REPORT",
  RETAINED_MCP_HEADER: "PRESERVE_UNKNOWN_AND_REPORT",
  MISSING_INVOCATION_COMPANY: "PRESERVE_UNKNOWN_AND_REPORT",
  CONFIG_UNAVAILABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  INVOCATION_COMPANY_MISMATCH: "PRESERVE_UNKNOWN_AND_REPORT",
};
const REASONS = Object.keys(REASON_ACTION).sort();
const LEGACY_KEYS = [
  "status", "reasonCodes", "pathClass", "evidenceBasis", "lifecycle", "leaseState",
  "authority", "contentScan", "stores", "backend", "target", "bindingCheck",
];
const RESULT_KEYS = [...LEGACY_KEYS, "schema_id", "nextActions"];
const enumeration = (values) => ({ type: "string", enum: values });
const EXPECTED_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: VERSION,
  type: "object",
  additionalProperties: false,
  required: RESULT_KEYS,
  properties: {
    schema_id: { type: "string", const: VERSION },
    status: enumeration(["UNKNOWN", "ADVERSE", "REFUSED"]),
    reasonCodes: { type: "array", minItems: 1, uniqueItems: true, items: enumeration(REASONS) },
    pathClass: enumeration(["UNKNOWN", "DOT_STARTUP_ONLY", "DOT_RETAINED", "CODEX_LOCAL_MCP_HEADER"]),
    evidenceBasis: enumeration(["metadata", "reported", "synthetic", "source"]),
    lifecycle: enumeration(["UNKNOWN", "live", "warm", "detached", "recovery", "terminal"]),
    leaseState: enumeration(["UNKNOWN", "unexpired_record", "expired_record", "revoked_record"]),
    authority: { type: "string", const: "UNKNOWN" },
    contentScan: { type: "string", const: "NOT_PERFORMED" },
    backend: { type: "string", const: "UNKNOWN" },
    target: enumeration(["UNKNOWN", "local", "remote", "custom"]),
    bindingCheck: enumeration(["REPORTED_CONSISTENT", "INCOMPLETE", "REFUSED"]),
    stores: {
      type: "object", additionalProperties: false, required: STORE_KEYS,
      properties: Object.fromEntries(STORE_KEYS.map((key) => [key,
        enumeration(["UNKNOWN", "PRESENT", "NOT_FOUND_IN_OBSERVED_SCOPE"])])),
    },
    nextActions: {
      type: "array", minItems: 1, uniqueItems: true,
      items: {
        type: "object", additionalProperties: false,
        required: ["reasonCode", "action", "instruction"],
        properties: {
          reasonCode: enumeration(REASONS),
          action: enumeration(Object.keys(ACTIONS)),
          instruction: enumeration(Object.values(ACTIONS)),
        },
      },
    },
  },
};

function policy() {
  return { companyId: COMPANY, agentIds: [...AGENTS] };
}

function fixture() {
  const binding = {
    companyId: COMPANY, agentId: AGENTS[0], runId: "fixture-result-run", sessionId: "fixture-result-session",
  };
  return {
    scope: policy(), binding: { ...binding },
    agent: { id: binding.agentId, companyId: COMPANY, adapterType: "paperclip_runner" },
    run: { id: binding.runId, companyId: COMPANY, agentId: binding.agentId, sessionId: binding.sessionId, status: "running" },
    observation: {
      basis: "reported", binding: { ...binding }, provider: "dot", target: "local", lifecycle: "live",
      stores: { controller: "present", journal: "not_found" },
    },
    now: NOW,
  };
}

function unknownStores() {
  return Object.fromEntries(STORE_KEYS.map((key) => [key, "UNKNOWN"]));
}

function want(overrides = {}) {
  return {
    status: "UNKNOWN", reasonCodes: [...BASE_REASONS, "COVERAGE_INCOMPLETE"],
    pathClass: "UNKNOWN", evidenceBasis: "metadata", lifecycle: "UNKNOWN", leaseState: "UNKNOWN",
    authority: "UNKNOWN", contentScan: "NOT_PERFORMED", stores: unknownStores(),
    backend: "UNKNOWN", target: "UNKNOWN", bindingCheck: "REPORTED_CONSISTENT", ...overrides,
  };
}

function local(overrides = {}) {
  return want({
    reasonCodes: [...REPORTED, "DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE"],
    pathClass: "DOT_STARTUP_ONLY", evidenceBasis: "reported", lifecycle: "live", target: "local",
    stores: { ...unknownStores(), controller: "PRESENT", journal: "NOT_FOUND_IN_OBSERVED_SCOPE" }, ...overrides,
  });
}

function stopped(reason, status = "REFUSED") {
  return want({ status, reasonCodes: [...BASE_REASONS, reason], bindingCheck: status === "REFUSED" ? "REFUSED" : "INCOMPLETE" });
}

const CASES = [
  { name: "Dot startup positive control", mutate() {}, expected: local() },
  {
    name: "Dot retained positive control", mutate(x) { x.observation.stores = { journal: "present", providerState: "present" }; },
    expected: local({ reasonCodes: [...REPORTED, "RETAINED_PROVIDER_STATE"], pathClass: "DOT_RETAINED", stores: { ...unknownStores(), journal: "PRESENT", providerState: "PRESENT" } }),
  },
  {
    name: "Codex header stays adverse", mutate(x) { x.observation.provider = "codex"; x.observation.stores = { mcpHeader: "present" }; },
    expected: local({ status: "ADVERSE", reasonCodes: [...REPORTED, "RETAINED_MCP_HEADER"], pathClass: "CODEX_LOCAL_MCP_HEADER", stores: { ...unknownStores(), mcpHeader: "PRESENT" } }),
  },
  { name: "no observation", mutate(x) { delete x.observation; }, expected: want() },
  {
    name: "synthetic fixture", pureOnly: true, mutate(x) { x.observation.basis = "synthetic"; },
    expected: local({ evidenceBasis: "synthetic", reasonCodes: [...BASE_REASONS, "COVERAGE_INCOMPLETE", "SYNTHETIC_FIXTURE", "DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE"] }),
  },
  { name: "source-only receipt", mutate(x) { x.observation.basis = "source"; }, expected: want({ evidenceBasis: "source", reasonCodes: [...BASE_REASONS, "COVERAGE_INCOMPLETE", "SOURCE_ONLY"] }) },
  ...["remote", "custom", "unrecognized-target"].map((target) => ({
    name: `${target} scope unavailable`, mutate(x) { x.observation.target = target; },
    expected: local({ reasonCodes: [...REPORTED, "STORE_SCOPE_UNOBSERVABLE"], pathClass: "UNKNOWN", stores: unknownStores(), target: target === "unrecognized-target" ? "UNKNOWN" : target }),
  })),
  { name: "empty stores", mutate(x) { x.observation.stores = {}; }, expected: local({ reasonCodes: REPORTED, pathClass: "UNKNOWN", stores: unknownStores() }) },
  { name: "unrecognized provider", mutate(x) { x.observation.provider = "other"; }, expected: local({ reasonCodes: REPORTED, pathClass: "UNKNOWN", stores: unknownStores() }) },
  { name: "invalid policy", pureOnly: true, mutate(x) { x.scope.agentIds = [...AGENTS, "fixture-fourth"]; }, expected: stopped("INVALID_POLICY") },
  { name: "company mismatch", mutate(x) { x.binding.companyId = "fixture-foreign-company"; }, expected: stopped("COMPANY_MISMATCH") },
  { name: "agent mismatch", mutate(x) { x.agent.id = AGENTS[1]; }, expected: stopped("AGENT_MISMATCH") },
  { name: "adapter mismatch", mutate(x) { x.agent.adapterType = "other"; }, expected: stopped("ADAPTER_MISMATCH") },
  { name: "run mismatch", mutate(x) { x.run.id = "fixture-other-run"; }, expected: stopped("RUN_MISMATCH") },
  { name: "session mismatch", mutate(x) { x.run.sessionId = "fixture-other-session"; }, expected: stopped("SESSION_MISMATCH") },
  { name: "missing binding metadata", mutate(x) { delete x.agent; }, expected: stopped("MISSING_BINDING_METADATA", "UNKNOWN") },
  { name: "missing session join", mutate(x) { delete x.run.sessionId; }, expected: stopped("MISSING_SESSION_JOIN", "UNKNOWN") },
  { name: "missing receipt binding", mutate(x) { delete x.observation.binding; }, expected: stopped("MISSING_RECEIPT_BINDING", "UNKNOWN") },
  { name: "unsupported evidence basis", pureOnly: true, mutate(x) { x.observation.basis = "measured"; }, expected: stopped("UNSUPPORTED_EVIDENCE_BASIS", "UNKNOWN") },
];

function inputFor(scenario) {
  const input = fixture();
  scenario.mutate(input);
  return input;
}

function legacy(result) {
  const { schema_id, nextActions, ...remaining } = result;
  return remaining;
}

function expectedActions(reasonCodes) {
  return reasonCodes.map((reasonCode) => {
    assert.ok(Object.hasOwn(REASON_ACTION, reasonCode), `unmapped reason: ${reasonCode}`);
    const action = REASON_ACTION[reasonCode];
    return { reasonCode, action, instruction: ACTIONS[action] };
  });
}

// Deliberately limited to the schema vocabulary above; no dependency or framework.
function validate(schema, value, path = "result") {
  if (schema.type === "object") {
    assert.ok(value && typeof value === "object" && !Array.isArray(value), `${path}: object`);
    for (const key of schema.required) assert.ok(Object.hasOwn(value, key), `${path}.${key}: required`);
    for (const key of Object.keys(value)) {
      assert.ok(Object.hasOwn(schema.properties, key), `${path}.${key}: unexpected field`);
      validate(schema.properties[key], value[key], `${path}.${key}`);
    }
  } else if (schema.type === "array") {
    assert.ok(Array.isArray(value), `${path}: array`);
    assert.ok(value.length >= schema.minItems, `${path}: minItems`);
    if (schema.uniqueItems) assert.equal(new Set(value.map((item) => JSON.stringify(item))).size, value.length, `${path}: uniqueItems`);
    value.forEach((item, index) => validate(schema.items, item, `${path}[${index}]`));
  } else {
    assert.equal(schema.type, "string", `${path}: unsupported schema type`);
    assert.equal(typeof value, "string", `${path}: string`);
  }
  if (schema.enum) assert.ok(schema.enum.includes(value), `${path}: enum`);
  if (Object.hasOwn(schema, "const")) assert.equal(value, schema.const, `${path}: const`);
}

function contract(result, expectedReasons) {
  assert.equal(result.schema_id, VERSION, "all outcomes need the additive result version");
  assert.deepEqual(Object.keys(result).sort(), [...RESULT_KEYS].sort(), "no private or arbitrary output fields");
  validate(EXPECTED_SCHEMA, result);
  assert.deepEqual(result.reasonCodes, expectedReasons, "existing reason codes and order must not change");
  assert.deepEqual(result.nextActions, expectedActions(expectedReasons), "one fixed allowlisted action per reason, including worker overrides");
  assert.deepEqual(JSON.parse(JSON.stringify(result)), result, "finite, deterministic JSON values only");
}

async function publishedContract() {
  let published;
  try {
    published = await import("../result-contract.mjs");
  } catch (error) {
    if (error.code !== "ERR_MODULE_NOT_FOUND" || !error.message.includes("/native-runner-coverage/result-contract.mjs")) throw error;
    assert.fail("Publish result-contract.mjs with RESULT_SCHEMA and REMEDIATION_CATALOGUE; the baseline has no versioned contract");
  }
  return published;
}

async function tool(t, { config = policy(), configError } = {}) {
  const calls = { config: [], forbidden: [], logs: [] };
  const denied = (object, prefix) => new Proxy(object, {
    get(target, key) {
      if (Object.hasOwn(target, key)) return target[key];
      calls.forbidden.push(`${prefix}.${String(key)}`);
      throw new Error("Offline capability denied");
    },
  });
  for (const name of ["log", "info", "warn", "error", "debug", "trace"]) {
    t.mock.method(console, name, (...args) => { calls.logs.push(args); });
  }
  t.mock.method(globalThis, "fetch", () => {
    calls.forbidden.push("fetch");
    throw new Error("Offline network denied");
  });
  let registration;
  const ctx = denied({
    tools: denied({ register(...args) { assert.equal(registration, undefined); registration = args; } }, "tools"),
    config: denied({ async get(...args) {
      calls.config.push(args);
      if (configError) throw configError;
      return structuredClone(config);
    } }, "config"),
  }, "ctx");
  t.after(() => {
    assert.deepEqual(calls.forbidden, [], "no native, store, network or mutation capability access");
    assert.deepEqual(calls.logs, [], "no console side effects, including sanitized hints or exceptions");
  });
  await registerCoverageTool(ctx);
  assert.deepEqual(calls.config, [], "registration is not an unscoped config read");
  assert.equal(registration?.[0], "native_runner_coverage");
  return { handler: registration[2], declaration: registration[1], calls };
}

function responseData(response) {
  assert.deepEqual(Object.keys(response).sort(), ["content", "data"]);
  assert.equal(typeof response.content, "string");
  assert.deepEqual(JSON.parse(response.content), response.data, "content and data have the same semantics");
  return response.data;
}

for (const scenario of CASES) {
  test(`positive control: classifier preserves ${scenario.name}`, () => {
    assert.deepEqual(legacy(classifyCoverage(inputFor(scenario))), scenario.expected);
  });
  test(`versioned contract: classifier ${scenario.name}`, () => {
    const input = inputFor(scenario);
    const before = structuredClone(input);
    const result = classifyCoverage(input);
    contract(result, scenario.expected.reasonCodes);
    assert.deepEqual(classifyCoverage(input), result, "same input, same complete result");
    assert.deepEqual(input, before, "advice must not mutate supplied metadata");
  });
  if (!scenario.pureOnly) {
    test(`positive control: worker preserves ${scenario.name}`, async (t) => {
      const { handler, calls } = await tool(t);
      assert.deepEqual(legacy(responseData(await handler(inputFor(scenario), { companyId: COMPANY }))), scenario.expected);
      assert.deepEqual(calls.config, [[COMPANY]]);
    });
    test(`versioned contract: worker ${scenario.name}`, async (t) => {
      const { handler } = await tool(t);
      const response = await handler(inputFor(scenario), { companyId: COMPANY });
      contract(responseData(response), scenario.expected.reasonCodes);
    });
  }
}

const FAILURES = [
  ...[undefined, null, {}, { companyId: "" }, { companyId: null }].map((runCtx, index) => ({
    name: `missing invocation company ${index}`, runCtx, expected: stopped("MISSING_INVOCATION_COMPANY"), configCalls: [],
  })),
  { name: "config lookup exception", configError: new Error(SENTINEL), runCtx: { companyId: COMPANY }, expected: stopped("CONFIG_UNAVAILABLE", "UNKNOWN") },
  { name: "invocation company mismatch", runCtx: { companyId: "fixture-other-invocation" }, expected: stopped("INVOCATION_COMPANY_MISMATCH") },
  { name: "missing config", config: null, runCtx: { companyId: COMPANY }, expected: stopped("INVOCATION_COMPANY_MISMATCH") },
  ...[
    AGENTS.slice(0, 2), [...AGENTS, "fixture-fourth"], [AGENTS[0], AGENTS[0], AGENTS[2]],
  ].map((agentIds, index) => ({
    name: `invalid configured agent policy ${index}`, config: { companyId: COMPANY, agentIds }, runCtx: { companyId: COMPANY }, expected: stopped("INVALID_POLICY"),
  })),
];
for (const scenario of FAILURES) {
  test(`positive control: worker ${scenario.name}`, async (t) => {
    const { handler, calls } = await tool(t, scenario);
    assert.deepEqual(legacy(responseData(await handler(fixture(), scenario.runCtx))), scenario.expected);
    assert.deepEqual(calls.config, scenario.configCalls ?? [[scenario.runCtx.companyId]]);
  });
  test(`versioned contract: worker ${scenario.name}`, async (t) => {
    const { handler } = await tool(t, scenario);
    contract(responseData(await handler(fixture(), scenario.runCtx)), scenario.expected.reasonCodes);
  });
}

test("published schema mechanically describes the exact result shape and all reason/action enums", async () => {
  const { RESULT_SCHEMA, REMEDIATION_CATALOGUE } = await publishedContract();
  assert.deepEqual(RESULT_SCHEMA, EXPECTED_SCHEMA);
  assert.deepEqual(Object.keys(REMEDIATION_CATALOGUE).sort(), REASONS, "complete, centrally owned reason catalogue");
  for (const reasonCode of REASONS) {
    const action = REASON_ACTION[reasonCode];
    assert.deepEqual(REMEDIATION_CATALOGUE[reasonCode], { action, instruction: ACTIONS[action] });
  }
  const covered = new Set([...CASES, ...FAILURES].flatMap((scenario) => scenario.expected.reasonCodes));
  assert.deepEqual([...covered].sort(), REASONS, "every catalogue reason has an executable outcome fixture");
});

test("published schema validates every classifier and worker result, not just the happy path", async (t) => {
  const { RESULT_SCHEMA } = await publishedContract();
  for (const scenario of CASES) validate(RESULT_SCHEMA, classifyCoverage(inputFor(scenario)));
  const { handler } = await tool(t);
  for (const scenario of CASES.filter((entry) => !entry.pureOnly)) {
    validate(RESULT_SCHEMA, responseData(await handler(inputFor(scenario), { companyId: COMPANY })));
  }
  for (const scenario of FAILURES) {
    const boundary = await tool(t, scenario);
    validate(RESULT_SCHEMA, responseData(await boundary.handler(fixture(), scenario.runCtx)));
  }
});

test("README result examples are discoverable and mechanically satisfy the published contract", async () => {
  // Reading this repository-owned document is not a native metadata/store read.
  const readme = await readFile(new URL("../README.md", import.meta.url), "utf8");
  const examples = [...readme.matchAll(/```json\s*\n([\s\S]*?)```/g)].map((match) => JSON.parse(match[1]));
  const results = examples.filter((example) => example?.schema_id === VERSION);
  assert.ok(results.length > 0, "publish a complete, synthetic versioned result example where consumers look");
  const { RESULT_SCHEMA } = await publishedContract();
  for (const result of results) {
    validate(RESULT_SCHEMA, result);
    contract(result, result.reasonCodes);
    assert.notEqual(result.evidenceBasis, "synthetic", "native tool examples must retain reported/source provenance");
  }
});

test("native tool discovery names the version, remediation field and repo-owned contract", async (t) => {
  const { declaration } = await tool(t);
  for (const token of [VERSION, "nextActions", "result-contract.mjs"]) {
    assert.ok(declaration.description.includes(token), `tool discovery must expose ${token}`);
  }
});

test("mutating returned actions cannot poison the central catalogue or later classifications", async () => {
  const input = fixture();
  const first = classifyCoverage(input);
  contract(first, local().reasonCodes);
  for (const mutate of [
    () => { first.nextActions[0].instruction = SENTINEL; },
    () => { first.nextActions.push({ reasonCode: SENTINEL, action: SENTINEL, instruction: SENTINEL }); },
  ]) {
    try { mutate(); } catch (error) { assert.ok(error instanceof TypeError, "a frozen public result is also safe"); }
  }
  contract(classifyCoverage(input), local().reasonCodes);
  const { REMEDIATION_CATALOGUE } = await publishedContract();
  for (const reasonCode of REASONS) {
    const action = REASON_ACTION[reasonCode];
    assert.deepEqual(REMEDIATION_CATALOGUE[reasonCode], { action, instruction: ACTIONS[action] });
  }
});

test("positive control: schema assertions reject green, measured, leaked, incomplete and non-finite results", () => {
  const valid = { ...local(), schema_id: VERSION, nextActions: expectedActions(local().reasonCodes) };
  validate(EXPECTED_SCHEMA, valid);
  for (const mutate of [
    (x) => { x.status = "SAFE"; }, (x) => { x.evidenceBasis = "measured"; },
    (x) => { x.authority = "VERIFIED"; }, (x) => { x.contentScan = "CLEAN"; },
    (x) => { delete x.schema_id; }, (x) => { x.schema_id = "unrecognized-version"; },
    (x) => { x.privateContent = SENTINEL; }, (x) => { delete x.stores.transcript; },
    (x) => { x.stores.sqlite = NaN; }, (x) => { x.reasonCodes.push("UNRECOGNIZED_REASON"); },
    (x) => { x.nextActions[0].instruction = SENTINEL; }, (x) => { x.nextActions = []; },
  ]) {
    const invalid = structuredClone(valid);
    mutate(invalid);
    assert.throws(() => validate(EXPECTED_SCHEMA, invalid), assert.AssertionError);
  }
});

for (const scenario of [CASES[0], CASES[2], CASES.find((entry) => entry.name === "session mismatch")]) {
  test(`fixed sanitized hints: ${scenario.name}`, async (t) => {
    const input = inputFor(scenario);
    const base = classifyCoverage(input);
    input.credentials = { bearer: SENTINEL };
    input.content = SENTINEL;
    input.agent.credentials = { bearer: SENTINEL };
    input.run.transcript = SENTINEL;
    input.observation.rawPath = `/synthetic/${SENTINEL}`;
    input.observation.content = SENTINEL;
    input.observation.stores.extra = SENTINEL;
    assert.deepEqual(classifyCoverage(input), base, "extra private properties cannot influence advice");
    const { handler } = await tool(t, { config: { ...policy(), privateConfig: SENTINEL } });
    const response = await handler(input, { companyId: COMPANY });
    const result = responseData(response);
    for (const secret of [SENTINEL, COMPANY, ...AGENTS, ...Object.values(input.binding)]) {
      assert.equal(JSON.stringify(response).includes(secret), false, "no input/config/identity interpolation");
    }
    contract(result, scenario.expected.reasonCodes);
    assert.deepEqual(result, base);
  });
}

test("config exception advice never echoes error message, stack or attached configuration", async (t) => {
  const error = new Error(SENTINEL);
  error.stack = `Error: ${SENTINEL}`;
  error.config = { privatePath: `/synthetic/${SENTINEL}`, credentials: SENTINEL };
  const { handler } = await tool(t, { configError: error });
  const response = await handler(fixture(), { companyId: COMPANY });
  assert.equal(JSON.stringify(response).includes(SENTINEL), false);
  contract(responseData(response), [...BASE_REASONS, "CONFIG_UNAVAILABLE"]);
});

test("positive control: configured company and exactly three agents cannot be replaced by caller scope", async (t) => {
  const { handler } = await tool(t);
  for (const agentId of AGENTS) {
    const input = fixture();
    input.binding.agentId = input.agent.id = input.run.agentId = input.observation.binding.agentId = agentId;
    input.scope = { companyId: "fixture-caller-company", agentIds: ["fixture-fourth"] };
    assert.deepEqual(legacy(responseData(await handler(input, { companyId: COMPANY }))), local());
  }
  const outsider = fixture();
  outsider.binding.agentId = outsider.agent.id = outsider.run.agentId = outsider.observation.binding.agentId = "fixture-fourth";
  outsider.scope.agentIds = [AGENTS[0], AGENTS[1], "fixture-fourth"];
  assert.deepEqual(legacy(responseData(await handler(outsider, { companyId: COMPANY }))), stopped("AGENT_MISMATCH"));
});

test("positive control: reported normalization, source provenance and server wall-clock custody survive", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: Date.parse(NOW) });
  const { handler } = await tool(t);
  for (const basis of ["synthetic", "measured", "reported"]) {
    for (const now of ["1900-01-01T00:00:00.000Z", "9999-01-01T00:00:00.000Z"]) {
      const input = fixture();
      input.observation.basis = basis;
      input.now = now;
      input.observation.lease = { expiresAt: NOW, revoked: false };
      assert.deepEqual(legacy(responseData(await handler(input, { companyId: COMPANY }))), local({ leaseState: "expired_record" }));
    }
  }
  const source = fixture();
  source.observation.basis = "source";
  assert.deepEqual(legacy(responseData(await handler(source, { companyId: COMPANY }))), want({ evidenceBasis: "source", reasonCodes: [...BASE_REASONS, "COVERAGE_INCOMPLETE", "SOURCE_ONLY"] }));
});

test("positive control: retained header remains adverse across lifecycle and lease records", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: Date.parse(NOW) });
  const { handler } = await tool(t);
  for (const lifecycle of ["live", "warm", "detached", "recovery", "terminal"]) {
    for (const [lease, leaseState] of [
      [{ expiresAt: "2030-06-15T12:01:00.000Z", revoked: false }, "unexpired_record"],
      [{ expiresAt: NOW, revoked: false }, "expired_record"],
      [{ expiresAt: NOW, revoked: true }, "revoked_record"],
    ]) {
      const input = inputFor(CASES[2]);
      input.observation.lifecycle = lifecycle;
      input.observation.lease = lease;
      input.run.status = lifecycle === "terminal" ? "succeeded" : "running";
      const output = responseData(await handler(input, { companyId: COMPANY }));
      assert.deepEqual(legacy(output), { ...CASES[2].expected, lifecycle, leaseState });
    }
  }
});

test("positive control: no capability, parameter, dependency or SDK transport expansion", async (t) => {
  const { default: manifest } = await import("../manifest.mjs");
  const { default: pkg } = await import("../package.json", { with: { type: "json" } });
  const { declaration } = await tool(t);
  assert.deepEqual(manifest.capabilities, ["agent.tools.register"]);
  assert.deepEqual(manifest.tools.map((entry) => entry.name), ["native_runner_coverage"]);
  assert.ok(["entrypoint.mjs", "./entrypoint.mjs"].includes(manifest.entrypoints.worker));
  for (const key of ["jobs", "routes", "webhooks"]) assert.ok(manifest[key] === undefined || manifest[key].length === 0);
  assert.deepEqual(Object.keys(declaration.parametersSchema.properties).sort(), ["agent", "binding", "observation", "run"]);
  assert.deepEqual([...declaration.parametersSchema.required].sort(), ["agent", "binding", "run"]);
  assert.deepEqual(pkg.dependencies ?? {}, {});
});
