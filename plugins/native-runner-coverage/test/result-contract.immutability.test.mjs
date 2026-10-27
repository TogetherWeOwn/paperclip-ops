/**
 * Pins the advertised immutability of the published result contract.
 * Run: node --test plugins/native-runner-coverage/test/result-contract.immutability.test.mjs
 *
 * RESULT_SCHEMA and REMEDIATION_CATALOGUE are shared module state: every
 * classifyCoverage call reads the catalogue, and consumers validate against the
 * schema. A shallow or missing freeze would let one caller poison advice or
 * schema for every later caller in the same process. This file runs in its own
 * node --test process, so a surviving mutant cannot leak into other suites.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { classifyCoverage } from "../classifier.mjs";
import { REMEDIATION_CATALOGUE, RESULT_SCHEMA } from "../result-contract.mjs";

const COMPANY = "fixture-freeze-company";
const AGENTS = ["fixture-freeze-agent-a", "fixture-freeze-agent-b", "fixture-freeze-agent-c"];
const SENTINEL = "FAKE_ONLY_CONTRACT_POISON_SENTINEL";

function fixture() {
  const binding = {
    companyId: COMPANY, agentId: AGENTS[0], runId: "fixture-freeze-run", sessionId: "fixture-freeze-session",
  };
  return {
    scope: { companyId: COMPANY, agentIds: [...AGENTS] },
    binding: { ...binding },
    agent: { id: binding.agentId, companyId: COMPANY, adapterType: "paperclip_runner" },
    run: { id: binding.runId, companyId: COMPANY, agentId: binding.agentId, sessionId: binding.sessionId, status: "running" },
    observation: {
      basis: "reported", binding: { ...binding }, provider: "dot", target: "local", lifecycle: "live",
      stores: { controller: "present", journal: "not_found" },
    },
    now: "2030-06-15T12:00:00.000Z",
  };
}

const INPUTS = {
  dotStartup: fixture,
  codexHeader: () => {
    const input = fixture();
    input.observation.provider = "codex";
    input.observation.stores = { mcpHeader: "present" };
    return input;
  },
  invalidInput: () => null,
  sessionMismatch: () => {
    const input = fixture();
    input.run.sessionId = "fixture-other-session";
    return input;
  },
};

function classifyAll() {
  return Object.fromEntries(Object.entries(INPUTS).map(([name, make]) => [name, classifyCoverage(make())]));
}

function* nodes(value, path) {
  if (!value || typeof value !== "object") return;
  yield [path, value];
  for (const [key, child] of Object.entries(value)) yield* nodes(child, `${path}.${key}`);
}

// Snapshots taken at import time, before any test attempts a mutation.
const SCHEMA_BEFORE = structuredClone(RESULT_SCHEMA);
const CATALOGUE_BEFORE = structuredClone(REMEDIATION_CATALOGUE);
const RESULTS_BEFORE = classifyAll();

test("every object and array in RESULT_SCHEMA and REMEDIATION_CATALOGUE is frozen (deep, not identity or shallow)", () => {
  let count = 0;
  for (const [root, name] of [[RESULT_SCHEMA, "RESULT_SCHEMA"], [REMEDIATION_CATALOGUE, "REMEDIATION_CATALOGUE"]]) {
    for (const [path, node] of nodes(root, name)) {
      assert.ok(Object.isFrozen(node), `${path} must be frozen`);
      count += 1;
    }
  }
  // Positive control: the walk reaches nested entries, enums and arrays.
  assert.ok(count > Object.keys(REMEDIATION_CATALOGUE).length + 20, `walked only ${count} nodes`);
});

const CATALOGUE_REASON = "BACKEND_UNOBSERVABLE";
const MUTATIONS = [
  ["catalogue entry instruction", () => { REMEDIATION_CATALOGUE[CATALOGUE_REASON].instruction = SENTINEL; }],
  ["catalogue entry action", () => { REMEDIATION_CATALOGUE[CATALOGUE_REASON].action = SENTINEL; }],
  ["catalogue entry replacement", () => { REMEDIATION_CATALOGUE[CATALOGUE_REASON] = { action: SENTINEL, instruction: SENTINEL }; }],
  ["catalogue reason deletion", () => { delete REMEDIATION_CATALOGUE.CONTENT_NOT_SCANNED; }],
  ["catalogue reason addition", () => { REMEDIATION_CATALOGUE[SENTINEL] = { action: SENTINEL, instruction: SENTINEL }; }],
  ["catalogue entry defineProperty", () => {
    Object.defineProperty(REMEDIATION_CATALOGUE.RETAINED_MCP_HEADER, "instruction", { value: SENTINEL });
  }],
  ["catalogue prototype swap", () => {
    Object.setPrototypeOf(REMEDIATION_CATALOGUE.AGENT_MISMATCH, { extra: SENTINEL });
  }],
  ["schema top-level flag", () => { RESULT_SCHEMA.additionalProperties = true; }],
  ["schema required list", () => { RESULT_SCHEMA.required.pop(); }],
  ["schema status enum", () => { RESULT_SCHEMA.properties.status.enum.push("SAFE"); }],
  ["schema const", () => { RESULT_SCHEMA.properties.authority.const = "VERIFIED"; }],
  ["schema reason enum", () => { RESULT_SCHEMA.properties.reasonCodes.items.enum[0] = SENTINEL; }],
  ["schema instruction enum", () => { RESULT_SCHEMA.properties.nextActions.items.properties.instruction.enum[0] = SENTINEL; }],
  ["schema store enum", () => { RESULT_SCHEMA.properties.stores.properties.controller.enum.push(SENTINEL); }],
];

for (const [name, mutate] of MUTATIONS) {
  test(`mutation attempt is refused: ${name}`, () => {
    assert.throws(mutate, TypeError, "ES module code is strict, so writes to a frozen contract must throw");
    assert.deepEqual(REMEDIATION_CATALOGUE, CATALOGUE_BEFORE, "catalogue unchanged after refused mutation");
    assert.deepEqual(RESULT_SCHEMA, SCHEMA_BEFORE, "schema unchanged after refused mutation");
  });
}

test("attempted catalogue poisoning cannot change later classifyCoverage output", () => {
  for (const [, mutate] of MUTATIONS) {
    try { mutate(); } catch (error) { assert.ok(error instanceof TypeError); }
  }
  const after = classifyAll();
  assert.deepEqual(after, RESULTS_BEFORE, "later classifications are identical to the pre-mutation baseline");
  assert.equal(JSON.stringify(after).includes(SENTINEL), false, "poison never reaches nextActions");
  // Positive control: the baseline really carries catalogue-derived advice for the poisoned reasons.
  const advised = Object.values(after).flatMap((result) => result.nextActions.map((entry) => entry.reasonCode));
  for (const reason of [CATALOGUE_REASON, "CONTENT_NOT_SCANNED", "RETAINED_MCP_HEADER", "SESSION_MISMATCH"]) {
    assert.ok(advised.includes(reason), `fixtures must exercise ${reason}`);
  }
});
