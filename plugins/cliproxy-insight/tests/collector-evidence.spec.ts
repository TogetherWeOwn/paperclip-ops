import { Buffer } from "node:buffer";
import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import type { ToolResult } from "@paperclipai/plugin-sdk";
import { afterEach, describe, expect, it, vi } from "vitest";

import manifest from "../src/manifest.js";
import { STATE_KEYS, TOOL_NAMES } from "../src/constants.js";
import { createPlugin } from "../src/worker.js";

const COMPANY = "11111111-1111-4111-8111-111111111111";
const FOREIGN = "22222222-2222-4222-8222-222222222222";
const TOOL = "get_collector_evidence";
const NOW = Date.parse("2026-10-25T12:00:00.000Z");
const iso = (offset = 0) => new Date(NOW + offset).toISOString();
const scope = (stateKey: string) => ({ scopeKind: "company" as const, scopeId: COMPANY, stateKey });
const jsonBytes = (value: unknown) => Buffer.byteLength(JSON.stringify(value), "utf8");
const emptyCounts = { indexed: 0, snapshots: 0, jsonBytes: 0, missing: 0, outsideWindow: 0, invalidTimestamp: 0 };
const missingModel = { ...emptyCounts, indexed: 1, missing: 1 };
const CANARIES = {
  provider: "fixture-provider-private-canary",
  lane: "fixture-lane-private-canary.json",
  account: "fixture-account-private-canary",
  payload: "fixture-nested-payload-private-canary",
  reason: "fixture-reason-private-canary",
  url: "https://fixture-private-canary.example.invalid/internal/telemetry",
  path: "/fixture/private-canary/producer-state.json",
  secret: "fixture-secret-value-private-canary",
  secretRef: "33333333-3333-4333-8333-333333333333",
};
const PRIVATE_CONFIG = {
  pollingEnabled: true,
  baseUrl: CANARIES.url,
  laneApiKeySecretRef: { type: "secret_ref", secretId: CANARIES.secretRef },
  staleAfterSeconds: 321,
  maxCooldownEventsPerProvider: 17,
  laneFiles: [CANARIES.lane],
};

async function setup(config: Record<string, unknown> = {}, bind = true) {
  vi.spyOn(Date, "now").mockReturnValue(NOW);
  const harness = createTestHarness({ manifest, config });
  const registration = vi.spyOn(harness.ctx.tools, "register");
  const { definition: plugin } = createPlugin();
  await plugin.setup!(harness.ctx);
  if (bind) await plugin.onConfigChanged!(config, { companyId: COMPANY });

  // Synthetic state only. Reject any scope outside this fixture's company.
  const state = new Map<string, unknown>();
  const stateRead = vi.spyOn(harness.ctx.state, "get").mockImplementation(async (key) => {
    expect(key).toEqual(scope(key.stateKey));
    return state.has(key.stateKey) ? state.get(key.stateKey) : null;
  });
  const configRead = vi.spyOn(harness.ctx.config, "get");
  const fetch = vi.fn().mockRejectedValue(new Error(CANARIES.url));
  vi.stubGlobal("fetch", fetch);
  const effects = [
    vi.spyOn(harness.ctx.http, "fetch"),
    vi.spyOn(harness.ctx.secrets, "resolve"),
    vi.spyOn(harness.ctx.state, "set"),
    vi.spyOn(harness.ctx.state, "delete"),
    vi.spyOn(harness.ctx.metrics, "write"),
    vi.spyOn(harness.ctx.activity, "log"),
    vi.spyOn(harness.ctx.companies, "list"),
    vi.spyOn(harness.ctx.jobs, "register"),
  ];
  const run = (params: unknown = { companyId: COMPANY }) => harness.executeTool(TOOL, params, { companyId: COMPANY });
  const startupLogs = structuredClone(harness.logs);
  const noSideEffects = () => {
    expect(fetch).not.toHaveBeenCalled();
    for (const effect of effects) expect(effect).not.toHaveBeenCalled();
    expect(harness.metrics).toEqual([]);
    expect(harness.activity).toEqual([]);
    expect(harness.logs).toEqual(startupLogs);
  };
  return { harness, plugin, registration, state, stateRead, configRead, run, noSideEffects };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function rejected(result: ToolResult, error: string) {
  expect(result).toEqual({ error, data: { ok: false, error } });
  expect(Object.getPrototypeOf(result.data)).toBe(Object.prototype);
  const encoded = JSON.stringify(result);
  for (const canary of Object.values(CANARIES)) expect(encoded).not.toContain(canary);
  expect(encoded.length).toBeLessThan(256);
}

function receipt(settings = { pollingEnabled: false, staleAfterSeconds: 600, maxCooldownEventsPerProvider: 200 }, windowSeconds = 3600) {
  return {
    schemaVersion: 1,
    companyId: COMPANY,
    pluginId: "togetherweown.cliproxy-insight",
    pluginVersion: "0.3.1",
    generatedAt: iso(),
    qualification: "partial",
    deployedArtifactId: null,
    installedLifecycle: null,
    settingsSource: "resolved_running_worker_config",
    settings: {
      ...settings,
      diagnosticSampleRate: null,
      diagnosticRetentionSeconds: null,
      diagnosticRedactionPolicy: null,
    },
    snapshotWindow: {
      start: iso(-windowSeconds * 1000),
      end: iso(),
      semantics: "current_snapshots_with_polledAt_in_window_not_event_history",
      byteSemantics: "utf8_json_snapshot_values_excluding_state_keys_and_storage_overhead",
      providerSnapshots: null,
      laneSnapshots: null,
      modelSnapshot: missingModel,
    },
    diagnosticEvents: null,
    diagnosticInputBytes: null,
    droppedSelfOriginEvents: null,
    redactedFields: null,
  };
}

describe("collector evidence: registration and request contract", () => {
  it("registers the same exact bounded schema in manifest and worker", async () => {
    const { registration } = await setup();
    const schema = {
      type: "object", additionalProperties: false, required: ["companyId"],
      properties: {
        companyId: { type: "string", minLength: 1 },
        windowSeconds: { type: "integer", minimum: 60, maximum: 86400, default: 3600 },
      },
    };
    expect(TOOL_NAMES.getCollectorEvidence).toBe(TOOL);
    const declarations = manifest.tools!.filter((tool) => tool.name === TOOL);
    expect(declarations).toHaveLength(1);
    expect(declarations[0]!.parametersSchema).toEqual(schema);
    const registrations = registration.mock.calls.filter(([name]) => name === TOOL);
    expect(registrations).toHaveLength(1);
    expect(registrations[0]![1].parametersSchema).toEqual(schema);
  });

  it.each([60, 3600, 86400])("accepts inclusive windowSeconds=%s and emits canonical times", async (windowSeconds) => {
    const { run, configRead, stateRead, noSideEffects } = await setup();
    expect(await run({ companyId: COMPANY, windowSeconds })).toEqual({ data: receipt(undefined, windowSeconds) });
    expect(configRead.mock.calls).toEqual([[COMPANY]]);
    expect(stateRead.mock.calls).toEqual([
      [scope(STATE_KEYS.providerIndex)], [scope(STATE_KEYS.laneIndex)], [scope(STATE_KEYS.modelUsage)],
    ]);
    noSideEffects();
  });

  it("defaults to a one-hour window and reports unavailable instrumentation as null, not zero", async () => {
    const { run, noSideEffects } = await setup();
    const result = await run();
    expect(result).toEqual({ data: receipt() });
    expect(Object.getPrototypeOf(result.data)).toBe(Object.prototype);
    noSideEffects();
  });

  it.each([
    ["null", null], ["undefined", undefined], ["array", []], ["string", COMPANY],
    ["missing company", {}], ["empty company", { companyId: "" }],
    ["null company", { companyId: null }], ["numeric company", { companyId: 123 }],
    ["unexpected field", { companyId: COMPANY, extra: CANARIES.secret }],
  ])("refuses %s before any config or state read", async (_label, params) => {
    const { harness, configRead, stateRead, noSideEffects } = await setup();
    // Call the harness directly: run() has a default argument for the golden path.
    rejected(await harness.executeTool(TOOL, params), "invalid_evidence_request");
    expect(configRead).not.toHaveBeenCalled();
    expect(stateRead).not.toHaveBeenCalled();
    noSideEffects();
  });

  it.each([null, true, false, "3600", "", 0, -1, 59, 86401, 60.5, NaN, Infinity, -Infinity, [], {}])(
    "runtime rejects invalid windowSeconds=%j even without host schema validation", async (windowSeconds) => {
      const { run, configRead, stateRead, noSideEffects } = await setup();
      rejected(await run({ companyId: COMPANY, windowSeconds }), "invalid_evidence_request");
      expect(configRead).not.toHaveBeenCalled();
      expect(stateRead).not.toHaveBeenCalled();
      noSideEffects();
    },
  );
});

describe("collector evidence: company binding", () => {
  it("refuses a foreign invoking company even when params name the configured company", async () => {
    const { harness, state, configRead, stateRead, noSideEffects } = await setup(PRIVATE_CONFIG);
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider]);
    state.set(STATE_KEYS.provider(CANARIES.provider), { polledAt: iso(), raw: CANARIES.payload });
    const result = await harness.executeTool(TOOL, { companyId: COMPANY }, { companyId: FOREIGN });
    expect.soft(configRead).not.toHaveBeenCalled();
    expect.soft(stateRead).not.toHaveBeenCalled();
    noSideEffects();
    rejected(result, "company is not configured");
  });

  it.each(["bootstrap only", "foreign company", "whitespace company", "missing identity", "conflicting identity"])(
    "refuses %s without broad or foreign config/state reads", async (scenario) => {
      const { plugin, run, configRead, stateRead } = await setup({}, scenario !== "bootstrap only");
      if (scenario === "missing identity") await plugin.onConfigChanged!({}, { companyId: null });
      if (scenario === "conflicting identity") {
        await expect(plugin.onConfigChanged!({}, { companyId: FOREIGN })).rejects.toThrow("exactly one");
      }
      const companyId = scenario === "foreign company" ? FOREIGN : scenario === "whitespace company" ? " " : COMPANY;
      rejected(await run({ companyId }), "company is not configured");
      expect(configRead).not.toHaveBeenCalled();
      expect(stateRead).not.toHaveBeenCalled();
    },
  );

  it.each(["missing", "conflicting"])("rechecks %s identity after awaited config, before state", async (identity) => {
    const { plugin, run, configRead, stateRead } = await setup();
    configRead.mockImplementation(async () => {
      if (identity === "missing") await plugin.onConfigChanged!({}, { companyId: null });
      else await expect(plugin.onConfigChanged!({}, { companyId: FOREIGN })).rejects.toThrow("exactly one");
      return {};
    });
    rejected(await run(), "company is not configured");
    expect(configRead.mock.calls).toEqual([[COMPANY]]);
    expect(stateRead).not.toHaveBeenCalled();
  });

  it.each([
    STATE_KEYS.providerIndex, STATE_KEYS.laneIndex,
    STATE_KEYS.provider(CANARIES.provider), STATE_KEYS.lane(CANARIES.lane), STATE_KEYS.modelUsage,
  ])("does not disclose a receipt invalidated while awaiting %s", async (invalidatedKey) => {
    const { plugin, run, state, stateRead } = await setup();
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider]);
    state.set(STATE_KEYS.laneIndex, [CANARIES.lane]);
    for (const key of [STATE_KEYS.provider(CANARIES.provider), STATE_KEYS.lane(CANARIES.lane), STATE_KEYS.modelUsage]) {
      state.set(key, { polledAt: iso(), raw: CANARIES.payload });
    }
    stateRead.mockImplementation(async (key) => {
      expect(key).toEqual(scope(key.stateKey));
      if (key.stateKey === invalidatedKey) {
        await expect(plugin.onConfigChanged!({}, { companyId: FOREIGN })).rejects.toThrow("exactly one");
      }
      return state.get(key.stateKey) ?? null;
    });
    rejected(await run(), "company is not configured");
  });

  it("keeps an invalidated binding refused after replaying the original company", async () => {
    const { plugin, run, configRead, stateRead } = await setup();
    await plugin.onConfigChanged!({}, { companyId: null });
    await plugin.onConfigChanged!({}, { companyId: COMPANY });
    rejected(await run(), "company is not configured");
    expect(configRead).not.toHaveBeenCalled();
    expect(stateRead).not.toHaveBeenCalled();
  });
});

describe("collector evidence: explicit output allowlist and read-only inventory", () => {
  it("returns only fixed aggregates despite private identities, raw payloads, config, and diagnostic lookalikes", async () => {
    const { run, state, stateRead, configRead, noSideEffects } = await setup(PRIVATE_CONFIG);
    const snapshot = {
      polledAt: iso(), observedAt: iso(-7 * 86400000), provider: CANARIES.provider,
      lane: CANARIES.lane, account: CANARIES.account, success: 987654321, failed: 123456789,
      reason: CANARIES.reason, raw: { nested: [{ secret: CANARIES.secret, path: CANARIES.path, data: CANARIES.payload }] },
      qualification: "verified", deployedArtifactId: CANARIES.path, installedLifecycle: "installed",
      diagnosticEvents: 100, diagnosticInputBytes: 200, droppedSelfOriginEvents: 300, redactedFields: 400,
    };
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider]);
    state.set(STATE_KEYS.laneIndex, [CANARIES.lane]);
    const keys = [STATE_KEYS.provider(CANARIES.provider), STATE_KEYS.lane(CANARIES.lane), STATE_KEYS.modelUsage];
    for (const key of keys) state.set(key, snapshot);
    state.set(STATE_KEYS.cooldownEvents(CANARIES.provider), [{ reason: CANARIES.reason }]);
    const expected = receipt({ pollingEnabled: true, staleAfterSeconds: 321, maxCooldownEventsPerProvider: 17 });
    const counts = { ...emptyCounts, indexed: 1, snapshots: 1, jsonBytes: jsonBytes(snapshot) };
    const result = await run();
    expect(result).toEqual({ data: {
      ...expected,
      snapshotWindow: { ...expected.snapshotWindow, providerSnapshots: counts, laneSnapshots: counts, modelSnapshot: counts },
    } });
    const encoded = JSON.stringify(result);
    for (const canary of Object.values(CANARIES)) expect(encoded).not.toContain(canary);
    expect(encoded).not.toContain("987654321");
    expect(configRead.mock.calls).toEqual([[COMPANY]]);
    expect(stateRead.mock.calls).toEqual([
      [scope(STATE_KEYS.providerIndex)], [scope(STATE_KEYS.laneIndex)], ...keys.map((key) => [scope(key)]),
    ]);
    noSideEffects();
  });

  it("uses scoped effective config on each read, not bootstrap settings or cached receipts", async () => {
    const { run, configRead, noSideEffects } = await setup(PRIVATE_CONFIG);
    configRead.mockResolvedValueOnce({}).mockResolvedValueOnce({ pollingEnabled: true, staleAfterSeconds: 90, maxCooldownEventsPerProvider: 3 });
    expect(await run()).toEqual({ data: receipt() });
    expect(await run()).toEqual({ data: receipt({ pollingEnabled: true, staleAfterSeconds: 90, maxCooldownEventsPerProvider: 3 }) });
    expect(configRead.mock.calls).toEqual([[COMPANY], [COMPANY]]);
    noSideEffects();
  });

  it.each([null, undefined])("missing indexes (%s) remain null rather than measured empty", async (absent) => {
    const { run, state, noSideEffects } = await setup();
    state.set(STATE_KEYS.providerIndex, absent);
    state.set(STATE_KEYS.laneIndex, absent);
    expect(await run()).toEqual({ data: receipt() });
    noSideEffects();
  });

  it("known-empty indexes report zero counts without pretending diagnostic or deployment availability", async () => {
    const { run, state, stateRead, noSideEffects } = await setup();
    state.set(STATE_KEYS.providerIndex, []);
    state.set(STATE_KEYS.laneIndex, []);
    const expected = receipt();
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: { ...expected.snapshotWindow, providerSnapshots: emptyCounts, laneSnapshots: emptyCounts },
    } });
    expect(stateRead).toHaveBeenCalledTimes(3);
    noSideEffects();
  });

  it("counts UTF-8 JSON snapshot values, not character length, keys, storage, or traffic", async () => {
    const { run, state, noSideEffects } = await setup();
    const snapshot = { polledAt: iso(), raw: { text: "é漢🙂\n\"\\", records: [CANARIES.account] } };
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider]);
    state.set(STATE_KEYS.provider(CANARIES.provider), snapshot);
    const expected = receipt();
    const bytes = jsonBytes(snapshot);
    expect(bytes).toBeGreaterThan(JSON.stringify(snapshot).length);
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: {
        ...expected.snapshotWindow,
        providerSnapshots: { ...emptyCounts, indexed: 1, snapshots: 1, jsonBytes: bytes },
      },
    } });
    noSideEffects();
  });
});

describe("collector evidence: timestamp selection and aggregation", () => {
  it("counts missing, invalid, and out-of-window values separately, using inclusive polledAt bounds", async () => {
    const { run, state, noSideEffects } = await setup();
    const snapshots = [
      { polledAt: iso(-3600000), observedAt: iso(-86400000), raw: "lower bound" },
      { polledAt: iso(), observedAt: iso(86400000), raw: "upper bound" },
      { polledAt: iso(-1), observedAt: "not-a-date", raw: "producer time ignored" },
      null, undefined,
      { polledAt: "not-a-date", observedAt: iso() },
      { observedAt: iso() },
      { polledAt: iso(-3600001), observedAt: iso() },
      { polledAt: iso(1), observedAt: iso() },
    ];
    const names = snapshots.map((_, i) => `synthetic-${i}`);
    state.set(STATE_KEYS.providerIndex, names);
    state.set(STATE_KEYS.laneIndex, names);
    snapshots.forEach((value, i) => {
      state.set(STATE_KEYS.provider(names[i]!), value);
      state.set(STATE_KEYS.lane(names[i]!), value);
    });
    state.set(STATE_KEYS.modelUsage, snapshots[0]);
    const expected = receipt();
    const counts = {
      indexed: 9, snapshots: 3, jsonBytes: snapshots.slice(0, 3).reduce((sum, value) => sum + jsonBytes(value), 0),
      missing: 2, outsideWindow: 2, invalidTimestamp: 2,
    };
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: {
        ...expected.snapshotWindow, providerSnapshots: counts, laneSnapshots: counts,
        modelSnapshot: { ...emptyCounts, indexed: 1, snapshots: 1, jsonBytes: jsonBytes(snapshots[0]) },
      },
    } });
    noSideEffects();
  });

  it.each(["invalid", "missing", "before", "after"])("classifies a model snapshot with %s polledAt", async (kind) => {
    const { run, state } = await setup();
    const polledAt = { invalid: "invalid", missing: undefined, before: iso(-3600001), after: iso(1) }[kind];
    state.set(STATE_KEYS.modelUsage, { polledAt, observedAt: iso(), models: { private: CANARIES.payload } });
    const expected = receipt();
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: {
        ...expected.snapshotWindow,
        modelSnapshot: { ...emptyCounts, indexed: 1, invalidTimestamp: kind === "invalid" || kind === "missing" ? 1 : 0, outsideWindow: kind === "before" || kind === "after" ? 1 : 0 },
      },
    } });
  });

  it.each(["raw string", 123, true, [], {}, { polledAt: 123 }, { polledAt: null }])(
    "classifies malformed snapshot %j as invalidTimestamp, not missing or traffic", async (snapshot) => {
      const { run, state } = await setup();
      state.set(STATE_KEYS.modelUsage, snapshot);
      const expected = receipt();
      expect(await run()).toEqual({ data: {
        ...expected, snapshotWindow: { ...expected.snapshotWindow, modelSnapshot: { ...emptyCounts, indexed: 1, invalidTimestamp: 1 } },
      } });
    },
  );
});

describe("collector evidence: bounded indexes and generic failures", () => {
  it("accepts exactly 64 providers, 32 lanes and one model with at most 99 current-state reads", async () => {
    const { run, state, stateRead, noSideEffects } = await setup();
    const providers = Array.from({ length: 64 }, (_, i) => `synthetic-provider-${i}`);
    const lanes = Array.from({ length: 32 }, (_, i) => `synthetic-lane-${i}`);
    state.set(STATE_KEYS.providerIndex, providers);
    state.set(STATE_KEYS.laneIndex, lanes);
    const snapshot = { polledAt: iso(), raw: "合成🙂" };
    for (const key of [...providers.map(STATE_KEYS.provider), ...lanes.map(STATE_KEYS.lane), STATE_KEYS.modelUsage]) state.set(key, snapshot);
    const expected = receipt();
    const counts = (n: number) => ({ ...emptyCounts, indexed: n, snapshots: n, jsonBytes: n * jsonBytes(snapshot) });
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: { ...expected.snapshotWindow, providerSnapshots: counts(64), laneSnapshots: counts(32), modelSnapshot: counts(1) },
    } });
    expect(stateRead).toHaveBeenCalledTimes(99);
    noSideEffects();
  });

  it("deduplicates valid indexes without double-counting or rereading a snapshot", async () => {
    const { run, state, stateRead } = await setup();
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider, CANARIES.provider]);
    state.set(STATE_KEYS.laneIndex, [CANARIES.lane, CANARIES.lane]);
    const snapshot = { polledAt: iso() };
    state.set(STATE_KEYS.provider(CANARIES.provider), snapshot);
    state.set(STATE_KEYS.lane(CANARIES.lane), snapshot);
    const expected = receipt();
    const counts = { ...emptyCounts, indexed: 1, snapshots: 1, jsonBytes: jsonBytes(snapshot) };
    expect(await run()).toEqual({ data: {
      ...expected, snapshotWindow: { ...expected.snapshotWindow, providerSnapshots: counts, laneSnapshots: counts },
    } });
    expect(stateRead).toHaveBeenCalledTimes(5);
  });

  for (const [indexKey, cap] of [[STATE_KEYS.providerIndex, 64], [STATE_KEYS.laneIndex, 32]] as const) {
    it.each([
      ["oversize", Array.from({ length: cap + 1 }, (_, i) => `private-${i}`)],
      ["oversize duplicates", Array(cap + 1).fill(CANARIES.provider)],
      ["object", { providers: [CANARIES.provider] }], ["string", CANARIES.payload],
      ["number", 1], ["boolean", false], ["empty name", [""]],
      ["nonstring name", [CANARIES.provider, 42]], ["null name", [null]],
      ["nested array", [[CANARIES.provider]]], ["overlong name", ["x".repeat(129)]],
    ])(`fails closed on ${indexKey}: %s, without truncation or snapshot reads`, async (_label, value) => {
      const { run, state, stateRead, noSideEffects } = await setup();
      state.set(indexKey, value);
      rejected(await run(), "evidence_unavailable");
      expect(stateRead.mock.calls.every(([key]) => [STATE_KEYS.providerIndex, STATE_KEYS.laneIndex].includes(key.stateKey as typeof STATE_KEYS.providerIndex))).toBe(true);
      expect(stateRead.mock.calls.length).toBeLessThanOrEqual(2);
      noSideEffects();
    });
  }

  it("accepts the 128-character index-name boundary without echoing the name", async () => {
    const { run, state } = await setup();
    const name = "x".repeat(128);
    state.set(STATE_KEYS.providerIndex, [name]);
    const expected = receipt();
    const result = await run();
    expect(result).toEqual({ data: {
      ...expected, snapshotWindow: { ...expected.snapshotWindow, providerSnapshots: { ...missingModel } },
    } });
    expect(JSON.stringify(result)).not.toContain(name);
  });

  it("sanitizes arbitrary config-read failures and never starts state reads", async () => {
    const { run, configRead, stateRead, noSideEffects } = await setup();
    configRead.mockRejectedValue(new Error(Object.values(CANARIES).join(" ")));
    rejected(await run(), "evidence_unavailable");
    expect(stateRead).not.toHaveBeenCalled();
    noSideEffects();
  });

  it.each([
    STATE_KEYS.providerIndex, STATE_KEYS.laneIndex,
    STATE_KEYS.provider(CANARIES.provider), STATE_KEYS.lane(CANARIES.lane), STATE_KEYS.modelUsage,
  ])("sanitizes a rejected state read at %s without returning partial data", async (failedKey) => {
    const { run, state, stateRead, noSideEffects } = await setup();
    state.set(STATE_KEYS.providerIndex, [CANARIES.provider]);
    state.set(STATE_KEYS.laneIndex, [CANARIES.lane]);
    stateRead.mockImplementation(async (key) => {
      expect(key).toEqual(scope(key.stateKey));
      if (key.stateKey === failedKey) throw new Error(Object.values(CANARIES).join(" "));
      return state.get(key.stateKey) ?? null;
    });
    rejected(await run(), "evidence_unavailable");
    noSideEffects();
  });

  it("fails closed if a snapshot value cannot be JSON serialized", async () => {
    const { run, state, noSideEffects } = await setup();
    state.set(STATE_KEYS.modelUsage, { polledAt: iso(), raw: BigInt(1) });
    rejected(await run(), "evidence_unavailable");
    noSideEffects();
  });
});
