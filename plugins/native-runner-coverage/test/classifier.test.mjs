/**
 * Offline acceptance contract. All identifiers and receipts are synthetic.
 * Run: node --test plugins/native-runner-coverage/test/classifier.test.mjs
 * No SDK, credentials, installs, filesystem scans or provider calls are needed.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { classifyCoverage } from "../classifier.mjs";

const COMPANY = "fixture-company";
const AGENTS = ["fixture-agent-a", "fixture-agent-b", "fixture-agent-c"];
const NOW = "2030-06-15T12:00:00.000Z";
const STORE_KEYS = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];
const STORE_STATES = ["UNKNOWN", "PRESENT", "NOT_FOUND_IN_OBSERVED_SCOPE"];
const LIFECYCLES = ["live", "warm", "detached", "recovery", "terminal"];
const PATH_CLASSES = ["UNKNOWN", "DOT_STARTUP_ONLY", "DOT_RETAINED", "CODEX_LOCAL_MCP_HEADER"];

function fixture() {
  const binding = {
    companyId: COMPANY,
    agentId: AGENTS[0],
    runId: "fixture-run",
    sessionId: "fixture-session",
  };
  return {
    scope: { companyId: COMPANY, agentIds: [...AGENTS] },
    binding: { ...binding },
    agent: { id: binding.agentId, companyId: COMPANY, adapterType: "paperclip_runner" },
    run: {
      id: binding.runId, companyId: COMPANY, agentId: binding.agentId,
      status: "running", sessionId: binding.sessionId,
    },
    observation: {
      basis: "synthetic",
      binding: { ...binding },
      provider: "dot",
      target: "local",
      lifecycle: "live",
      stores: {
        controller: "present", journal: "not_found", providerState: "unknown",
        mcpHeader: "not_found", transcript: "unknown", sqlite: "unknown",
        runLog: "unknown", optionalTrace: "unknown",
      },
    },
    now: NOW,
  };
}

function check(input) {
  let result;
  assert.doesNotThrow(() => { result = classifyCoverage(input); });
  assert.ok(result && typeof result === "object" && !Array.isArray(result));
  assert.ok(["UNKNOWN", "ADVERSE", "REFUSED"].includes(result.status));
  assert.ok(Array.isArray(result.reasonCodes));
  assert.ok(result.reasonCodes.every((code) => typeof code === "string" && code.length > 0));
  assert.ok(PATH_CLASSES.includes(result.pathClass));
  assert.ok(["metadata", "reported", "synthetic", "source"].includes(result.evidenceBasis));
  assert.ok(["UNKNOWN", ...LIFECYCLES].includes(result.lifecycle));
  assert.ok(["UNKNOWN", "unexpired_record", "expired_record", "revoked_record"].includes(result.leaseState));
  assert.equal(result.authority, "UNKNOWN");
  assert.equal(result.contentScan, "NOT_PERFORMED");
  assert.ok(result.stores && typeof result.stores === "object");
  assert.deepEqual(Object.keys(result.stores).sort(), [...STORE_KEYS].sort());
  for (const key of STORE_KEYS) {
    assert.ok(STORE_STATES.includes(result.stores[key]), `${key} must be a scoped enum`);
  }
  // Reason names are not frozen; the public output vocabulary is. These three
  // optional metadata fields allow additive integration without leaking input.
  const keys = [
    "status", "reasonCodes", "pathClass", "evidenceBasis", "lifecycle",
    "leaseState", "authority", "contentScan", "stores", "backend", "target", "bindingCheck",
    "schema_id", "nextActions",
  ];
  assert.ok(Object.keys(result).every((key) => keys.includes(key)));
  if (Object.hasOwn(result, "backend")) assert.equal(result.backend, "UNKNOWN");
  if (Object.hasOwn(result, "target")) {
    assert.ok(["UNKNOWN", "local", "remote", "custom"].includes(result.target));
  }
  if (Object.hasOwn(result, "bindingCheck")) {
    assert.ok(["REPORTED_CONSISTENT", "INCOMPLETE", "REFUSED"].includes(result.bindingCheck));
  }
  assert.doesNotThrow(() => JSON.stringify(result));
  return result;
}

function unknownStores(result) {
  assert.deepEqual(result.stores, Object.fromEntries(STORE_KEYS.map((key) => [key, "UNKNOWN"])));
}

function refused(input) {
  const result = check(input);
  assert.equal(result.status, "REFUSED");
  assert.ok(result.reasonCodes.length > 0, "refusals need machine-readable reasons");
  assert.equal(result.pathClass, "UNKNOWN");
  assert.equal(result.leaseState, "UNKNOWN");
  unknownStores(result);
  return result;
}

test("valid bound local receipt succeeds without claiming safety or authority", () => {
  const result = check(fixture());
  assert.notEqual(result.status, "REFUSED", "a deny-everything implementation is not acceptable");
  assert.equal(result.pathClass, "DOT_STARTUP_ONLY");
  assert.equal(result.evidenceBasis, "synthetic");
  assert.equal(result.lifecycle, "live");
  assert.equal(result.leaseState, "UNKNOWN");
  assert.equal(result.stores.controller, "PRESENT");
  assert.equal(result.stores.journal, "NOT_FOUND_IN_OBSERVED_SCOPE");
});

for (const agentId of AGENTS) {
  test(`each of the three allowlisted agents can have a consistent receipt: ${agentId}`, () => {
    const input = fixture();
    input.binding.agentId = agentId;
    input.agent.id = agentId;
    input.run.agentId = agentId;
    input.observation.binding.agentId = agentId;
    const result = check(input);
    assert.notEqual(result.status, "REFUSED");
    assert.equal(result.pathClass, "DOT_STARTUP_ONLY");
  });
}

for (const basis of ["synthetic", "reported"]) {
  for (const [name, provider, stores, pathClass] of [
    ["Dot startup only", "dot", { controller: "present", journal: "not_found" }, "DOT_STARTUP_ONLY"],
    ["Dot retained", "dot", { controller: "present", journal: "present", providerState: "present" }, "DOT_RETAINED"],
    ["Codex local MCP header", "codex", { mcpHeader: "present" }, "CODEX_LOCAL_MCP_HEADER"],
  ]) {
    test(`${basis} path control: ${name}`, () => {
      const input = fixture();
      input.observation.basis = basis;
      input.observation.provider = provider;
      input.observation.stores = stores;
      const result = check(input);
      assert.notEqual(result.status, "REFUSED");
      assert.equal(result.pathClass, pathClass);
      assert.equal(result.evidenceBasis, basis);
      if (provider === "codex") assert.equal(result.status, "ADVERSE");
    });
  }
}

for (const [label, mutate] of [
  ["binding company", (x) => { x.binding.companyId = "fixture-other-company"; }],
  ["agent company", (x) => { x.agent.companyId = "fixture-other-company"; }],
  ["run company", (x) => { x.run.companyId = "fixture-other-company"; }],
  ["receipt company", (x) => { x.observation.binding.companyId = "fixture-other-company"; }],
  ["scope company", (x) => { x.scope.companyId = "fixture-other-company"; }],
  ["binding agent outside allowlist", (x) => { x.binding.agentId = "fixture-outsider"; }],
  ["agent row outside allowlist", (x) => { x.agent.id = "fixture-outsider"; }],
  ["run agent outside allowlist", (x) => { x.run.agentId = "fixture-outsider"; }],
  ["receipt agent outside allowlist", (x) => { x.observation.binding.agentId = "fixture-outsider"; }],
  ["different allowlisted agent row", (x) => { x.agent.id = AGENTS[1]; }],
  ["different allowlisted run agent", (x) => { x.run.agentId = AGENTS[1]; }],
  ["different allowlisted receipt agent", (x) => { x.observation.binding.agentId = AGENTS[1]; }],
  ["binding run", (x) => { x.binding.runId = "fixture-other-run"; }],
  ["run row", (x) => { x.run.id = "fixture-other-run"; }],
  ["receipt run", (x) => { x.observation.binding.runId = "fixture-other-run"; }],
  ["binding session", (x) => { x.binding.sessionId = "fixture-other-session"; }],
  ["run session", (x) => { x.run.sessionId = "fixture-other-session"; }],
  ["receipt session", (x) => { x.observation.binding.sessionId = "fixture-other-session"; }],
  ["adapter type", (x) => { x.agent.adapterType = "fixture-other-adapter"; }],
]) {
  test(`refuses mismatched ${label}`, () => {
    const input = fixture();
    mutate(input);
    refused(input);
  });
}

for (const [label, agentIds] of [
  ["zero agents", []],
  ["one agent", [AGENTS[0]]],
  ["two agents", AGENTS.slice(0, 2)],
  ["four agents", [...AGENTS, "fixture-agent-d"]],
  ["three entries with a duplicate", [AGENTS[0], AGENTS[1], AGENTS[1]]],
  ["non-array agents", AGENTS.join(",")],
  ["missing agents", undefined],
]) {
  test(`refuses invalid scope policy: ${label}`, () => {
    const input = fixture();
    input.scope.agentIds = agentIds;
    refused(input);
  });
}

for (const companyId of [undefined, null, "", [COMPANY, "fixture-other-company"]]) {
  test(`refuses a scope without exactly one company: ${JSON.stringify(companyId)}`, () => {
    const input = fixture();
    input.scope.companyId = companyId;
    refused(input);
  });
}

test("cannot admit an outsider by making all claimed bindings agree", () => {
  const input = fixture();
  input.binding.agentId = "fixture-outsider";
  input.agent.id = "fixture-outsider";
  input.run.agentId = "fixture-outsider";
  input.observation.binding.agentId = "fixture-outsider";
  refused(input);
});

test("valid metadata without an observation is not refused or promoted to a measured path", () => {
  const input = fixture();
  delete input.observation;
  const result = check(input);
  assert.notEqual(result.status, "REFUSED");
  assert.equal(result.status, "UNKNOWN");
  assert.equal(result.pathClass, "UNKNOWN");
  assert.equal(result.evidenceBasis, "metadata");
  assert.equal(result.lifecycle, "UNKNOWN");
  assert.equal(result.leaseState, "UNKNOWN");
  unknownStores(result);
});

for (const [label, mutate] of [
  ["no run row", (x) => { delete x.run; }],
  ["no run id", (x) => { delete x.run.id; }],
  ["no expected run id", (x) => { delete x.binding.runId; }],
  ["no run session", (x) => { delete x.run.sessionId; }],
  ["no expected session", (x) => { delete x.binding.sessionId; delete x.run.sessionId; }],
  ["no receipt session", (x) => { delete x.observation.binding.sessionId; }],
  ["no receipt binding", (x) => { delete x.observation.binding; }],
  ["no session anywhere", (x) => {
    delete x.binding.sessionId; delete x.run.sessionId; delete x.observation.binding.sessionId;
  }],
  ["no path observation", (x) => { delete x.observation.stores; }],
  ["empty path observation", (x) => { x.observation.stores = {}; }],
]) {
  test(`incomplete evidence cannot establish path coverage: ${label}`, () => {
    const input = fixture();
    mutate(input);
    const result = check(input);
    assert.equal(result.pathClass, "UNKNOWN");
    unknownStores(result);
  });
}

for (const provider of ["dot", "codex"]) {
  test(`source-only ${provider} claims do not establish measured store coverage`, () => {
    const input = fixture();
    input.observation.basis = "source";
    input.observation.provider = provider;
    input.observation.stores = Object.fromEntries(STORE_KEYS.map((key) => [key, "present"]));
    const result = check(input);
    assert.notEqual(result.status, "REFUSED");
    assert.equal(result.evidenceBasis, "source");
    assert.equal(result.pathClass, "UNKNOWN");
    unknownStores(result);
  });
}

for (const target of ["remote", "custom"]) {
  for (const provider of ["dot", "codex"]) {
    test(`${target} ${provider} receipt cannot assert local store coverage`, () => {
      const input = fixture();
      input.observation.target = target;
      input.observation.provider = provider;
      input.observation.stores = Object.fromEntries(STORE_KEYS.map((key) => [key, "present"]));
      const result = check(input);
      assert.equal(result.pathClass, "UNKNOWN");
      unknownStores(result);
    });
  }
}

for (const key of STORE_KEYS) {
  for (const [inputState, outputState] of [
    ["present", "PRESENT"],
    ["not_found", "NOT_FOUND_IN_OBSERVED_SCOPE"],
    ["unknown", "UNKNOWN"],
  ]) {
    test(`local store mapping is scoped: ${key}=${inputState}`, () => {
      const input = fixture();
      input.observation.stores = { [key]: inputState };
      const result = check(input);
      assert.notEqual(result.status, "REFUSED");
      assert.equal(result.stores[key], outputState);
      for (const other of STORE_KEYS.filter((item) => item !== key)) {
        assert.equal(result.stores[other], "UNKNOWN", `${other} was not observed`);
      }
      if (key === "mcpHeader" && inputState === "present") assert.equal(result.status, "ADVERSE");
    });
  }
}

for (const [label, stores] of [
  ["unknown journal", { controller: "present", journal: "unknown" }],
  ["absent journal", { controller: "present" }],
  ["absent controller", { journal: "not_found" }],
  ["unknown provider state", { controller: "present", journal: "present", providerState: "unknown" }],
]) {
  test(`partial Dot evidence cannot imply a complete path: ${label}`, () => {
    const input = fixture();
    input.observation.stores = stores;
    assert.equal(check(input).pathClass, "UNKNOWN");
  });
}

for (const lifecycle of LIFECYCLES) {
  test(`Dot retention is not erased by lifecycle ${lifecycle}`, () => {
    const input = fixture();
    input.observation.lifecycle = lifecycle;
    input.run.status = lifecycle === "terminal" ? "succeeded" : "running";
    input.observation.stores = { controller: "present", journal: "present", providerState: "present" };
    const result = check(input);
    assert.notEqual(result.status, "REFUSED");
    assert.equal(result.lifecycle, lifecycle);
    assert.equal(result.pathClass, "DOT_RETAINED");
    assert.equal(result.stores.journal, "PRESENT");
    assert.equal(result.stores.providerState, "PRESENT");
  });
}

const LEASES = [
  ["unexpired", { expiresAt: "2030-06-15T12:01:00.000Z", revoked: false }, "unexpired_record"],
  ["expired", { expiresAt: "2030-06-15T11:59:00.000Z", revoked: false }, "expired_record"],
  ["expiry boundary", { expiresAt: NOW, revoked: false }, "expired_record"],
  ["revoked", { expiresAt: "2030-06-15T12:01:00.000Z", revoked: true }, "revoked_record"],
  ["revoked and expired", { expiresAt: "2030-06-15T11:59:00.000Z", revoked: true }, "revoked_record"],
];

for (const [label, lease, leaseState] of LEASES) {
  test(`${label} lease is record metadata, never authority proof`, () => {
    const input = fixture();
    input.observation.lease = lease;
    const result = check(input);
    assert.notEqual(result.status, "REFUSED");
    assert.equal(result.leaseState, leaseState);
    assert.equal(result.authority, "UNKNOWN");
  });
  for (const lifecycle of LIFECYCLES) {
    test(`local retained header stays adverse: ${lifecycle}, ${label} lease`, () => {
      const input = fixture();
      input.observation.provider = "codex";
      input.observation.lifecycle = lifecycle;
      input.observation.lease = lease;
      input.observation.stores = { mcpHeader: "present" };
      input.run.status = lifecycle === "terminal" ? "succeeded" : "running";
      const result = check(input);
      assert.equal(result.status, "ADVERSE");
      assert.equal(result.pathClass, "CODEX_LOCAL_MCP_HEADER");
      assert.equal(result.lifecycle, lifecycle);
      assert.equal(result.leaseState, leaseState);
      assert.equal(result.stores.mcpHeader, "PRESENT");
    });
  }
}

for (const now of [undefined, null, "", "not-an-iso-timestamp", 0]) {
  test(`invalid now cannot validate a lease: ${JSON.stringify(now)}`, () => {
    const input = fixture();
    input.now = now;
    input.observation.lease = LEASES[0][1];
    assert.equal(check(input).leaseState, "UNKNOWN");
  });
}

for (const lease of [{}, { expiresAt: "invalid-date", revoked: false }, { revoked: false }]) {
  test(`incomplete lease never means unexpired: ${JSON.stringify(lease)}`, () => {
    const input = fixture();
    input.observation.lease = lease;
    assert.equal(check(input).leaseState, "UNKNOWN");
  });
}

for (const input of [undefined, null, false, 0, "fixture-input", [], {}]) {
  test(`invalid overall input is nonthrowing and non-green: ${JSON.stringify(input)}`, () => {
    const result = check(input);
    assert.ok(["UNKNOWN", "REFUSED"].includes(result.status));
    assert.equal(result.pathClass, "UNKNOWN");
    assert.equal(result.leaseState, "UNKNOWN");
    unknownStores(result);
  });
}

for (const key of ["scope", "binding", "agent", "run"]) {
  for (const value of [undefined, null, [], "fixture-invalid-row"]) {
    test(`malformed ${key} fails closed: ${JSON.stringify(value)}`, () => {
      const input = fixture();
      input[key] = value;
      const result = check(input);
      assert.ok(["UNKNOWN", "REFUSED"].includes(result.status));
      assert.equal(result.pathClass, "UNKNOWN");
      unknownStores(result);
    });
  }
}

for (const [key, value] of [
  ["provider", "fixture-unrecognized-provider"],
  ["target", "fixture-unrecognized-target"],
  ["basis", "fixture-unrecognized-basis"],
  ["stores", { controller: "fixture-not-a-store-enum" }],
]) {
  test(`unrecognized receipt ${key} cannot establish coverage`, () => {
    const input = fixture();
    input.observation[key] = value;
    assert.equal(check(input).pathClass, "UNKNOWN");
  });
}

for (const reject of [false, true]) {
  test(`output drops fake credential/content/path fields${reject ? " on refusal" : " on success"}`, () => {
    const input = fixture();
    const sentinels = {
      bearer: "FAKE_ONLY_BEARER_SENTINEL",
      content: "FAKE_ONLY_TRANSCRIPT_SENTINEL",
      path: "/synthetic/not-a-real-store/FAKE_ONLY_PATH_SENTINEL",
    };
    input.credentials = { bearer: sentinels.bearer };
    input.content = sentinels.content;
    input.agent.credentials = { bearer: sentinels.bearer };
    input.run.transcript = sentinels.content;
    input.observation.rawPath = sentinels.path;
    input.observation.bearer = sentinels.bearer;
    input.observation.content = sentinels.content;
    input.observation.stores.rawPath = sentinels.path;
    if (reject) input.observation.binding.runId = `fixture-wrong-${sentinels.content}`;
    const result = reject ? refused(input) : check(input);
    if (!reject) {
      assert.notEqual(result.status, "REFUSED");
      assert.equal(result.pathClass, "DOT_STARTUP_ONLY");
    }
    const serialized = JSON.stringify(result);
    for (const value of [...Object.values(sentinels), ...Object.values(input.binding)]) {
      assert.equal(serialized.includes(value), false, "output must not echo input data");
    }
  });
}

test("untrusted store values are never echoed into the sanitized result", () => {
  const input = fixture();
  const sentinel = "FAKE_ONLY_UNTRUSTED_STORE_CONTENT";
  input.observation.stores.mcpHeader = { bearer: sentinel, content: sentinel };
  const result = check(input);
  assert.equal(result.stores.mcpHeader, "UNKNOWN");
  assert.equal(JSON.stringify(result).includes(sentinel), false);
});
