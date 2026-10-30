/**
 * Independent acceptance for persisted-selector boundaries. Removing a company
 * fence, provider membership check, lane-index cleanup or refusal counter must
 * break the corresponding behavior below, without replacing the real plugin.
 */
import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JOB_KEYS, ROUTE_KEYS, STATE_KEYS, TOOL_NAMES } from "../src/constants.js";
import manifest from "../src/manifest.js";
import { createPlugin } from "../src/worker.js";

const COMPANY = "00000000-0000-4000-8000-000000000003";
const FOREIGN_COMPANY = "00000000-0000-4000-8000-000000000099";
const NOW = "2026-10-01T12:00:00.000Z";
const UNTIL = "2026-10-01T12:05:00.000Z";
const CONFIG = { pollingEnabled: false };
const MIXED_INDEX = ["", "   ", "zai.json", "zai.json", 7];
const scope = { scopeKind: "company" as const, scopeId: COMPANY };
const key = (stateKey: string) => ({ ...scope, stateKey });

interface UsageView {
  providers: string[];
  snapshots: Record<string, { success: number }>;
  lanes: string[];
  laneSnapshots: Record<string, unknown>;
  accountsCooling: number;
  modelUsage: { models: Record<string, unknown> };
}

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(NOW));
  fetchSpy = vi.fn().mockRejectedValue(new Error("acceptance: unexpected outbound request"));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function fixture(config: Record<string, unknown> = CONFIG) {
  const harness = createTestHarness({ manifest, config });
  harness.seed({ companies: [{ id: COMPANY, name: "Fixture company" } as never] });
  const { definition: plugin } = createPlugin();
  if (!plugin.setup || !plugin.onConfigChanged || !plugin.onApiRequest) {
    throw new Error("plugin lifecycle and API handlers are required");
  }
  await plugin.setup(harness.ctx);
  await plugin.onConfigChanged(config, { companyId: COMPANY });
  await harness.ctx.state.set(key(STATE_KEYS.laneIndex), ["claude.json", "zai.json"]);
  for (const laneFile of ["claude.json", "zai.json"]) {
    await harness.ctx.state.set(key(STATE_KEYS.lane(laneFile)), {
      schemaVersion: 1,
      laneFile,
      polledAt: NOW,
      observedAt: NOW,
      staleAfterSeconds: 300,
      records: [{
        lane: `${laneFile}-account`,
        health: "healthy",
        weekly_utilization: 0.4,
        ...(laneFile === "zai.json" ? { exhausted_until: UNTIL } : {}),
      }],
    });
  }
  await harness.ctx.state.set(key(STATE_KEYS.providerIndex), ["claude"]);
  await harness.ctx.state.set(key(STATE_KEYS.provider("claude")), {
    schemaVersion: 1,
    provider: "claude",
    polledAt: NOW,
    observedAt: NOW,
    success: 7,
    failed: 0,
    raw: { success: 7, failed: 0 },
  });
  await harness.ctx.state.set(key(STATE_KEYS.modelUsage), {
    schemaVersion: 1,
    polledAt: NOW,
    observedAt: NOW,
    staleAfterSeconds: 300,
    telemetry: "available",
    reasonCode: null,
    models: { "fixture-model": { serviceable: true } },
  });
  return { harness, plugin };
}

function unknownProvider() {
  return {
    error: "unknown_provider",
    data: {
      ok: false,
      error: "unknown_provider",
      retryable: false,
      next_action: "use a provider key from the persisted provider index",
    },
  };
}

function unknownLane() {
  return {
    error: "unknown_lane",
    data: {
      ok: false,
      error: "unknown_lane",
      retryable: false,
      next_action: "use a lane id from the persisted lane index",
    },
  };
}

describe("provider-index company fence", () => {
  it.each([
    { name: "selected", params: { lane: "zai.json" } },
    { name: "unfiltered", params: {} },
  ])("stops $name reads when scope is invalidated during the provider-index await", async ({ params }) => {
    const { harness, plugin } = await fixture();
    const readState = harness.ctx.state.get.bind(harness.ctx.state);
    const stateGet = vi.spyOn(harness.ctx.state, "get").mockImplementation(async (request) => {
      const persisted = await readState(request);
      if (request.stateKey === STATE_KEYS.providerIndex) {
        await expect(plugin.onConfigChanged!(CONFIG, { companyId: FOREIGN_COMPANY }))
          .rejects.toThrow("exactly one configured company");
      }
      return persisted;
    });
    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secret = vi.spyOn(harness.ctx.secrets, "resolve");
    const http = vi.spyOn(harness.ctx.http, "fetch");

    expect(await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, ...params }))
      .toEqual({ error: "company is not configured", data: { ok: false, error: "company is not configured" } });
    expect(stateGet.mock.calls).toEqual([[key(STATE_KEYS.laneIndex)], [key(STATE_KEYS.providerIndex)]]);
    expect(stateSet).not.toHaveBeenCalled();
    expect(secret).not.toHaveBeenCalled();
    expect(http).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("provider rejection", () => {
  it.each([
    { name: "empty string", provider: "" },
    { name: "whitespace", provider: "   " },
    { name: "number", provider: 123 },
    { name: "boolean", provider: true },
    { name: "array", provider: ["provider-input-marker"] },
    { name: "object", provider: { input: "provider-input-marker" } },
    { name: "null", provider: null },
  ])("rejects a present $name provider before any state read", async ({ provider }) => {
    const { harness } = await fixture();
    const stateGet = vi.spyOn(harness.ctx.state, "get");
    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secret = vi.spyOn(harness.ctx.secrets, "resolve");
    const http = vi.spyOn(harness.ctx.http, "fetch");
    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, provider });

    expect(result).toEqual(unknownProvider());
    expect(JSON.stringify(result)).not.toContain("provider-input-marker");
    expect(stateGet).not.toHaveBeenCalled();
    expect(stateSet).not.toHaveBeenCalled();
    expect(secret).not.toHaveBeenCalled();
    expect(http).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects an unindexed provider after only the authorized indexes", async () => {
    const { harness } = await fixture();
    const stateGet = vi.spyOn(harness.ctx.state, "get");
    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secret = vi.spyOn(harness.ctx.secrets, "resolve");
    const http = vi.spyOn(harness.ctx.http, "fetch");
    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      provider: "unindexed-provider-input-marker",
    });

    expect(result).toEqual(unknownProvider());
    expect(JSON.stringify(result)).not.toContain("unindexed-provider-input-marker");
    expect(stateGet.mock.calls).toEqual([[key(STATE_KEYS.laneIndex)], [key(STATE_KEYS.providerIndex)]]);
    expect(stateSet).not.toHaveBeenCalled();
    expect(secret).not.toHaveBeenCalled();
    expect(http).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([
    { name: "omitted", params: {} },
    { name: "undefined", params: { provider: undefined } },
    { name: "indexed", params: { provider: "claude" } },
  ])("keeps the $name provider control successful", async ({ params }) => {
    const { harness } = await fixture();
    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, ...params });
    expect(result.error).toBeUndefined();
    const data = result.data as unknown as UsageView;
    expect(data.providers).toEqual(["claude"]);
    expect(data.snapshots.claude?.success).toBe(7);
    expect(data.modelUsage.models).toEqual({ "fixture-model": { serviceable: true } });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("lane-index cleanup", () => {
  it.each(["tool", "route"])("normalizes the mixed persisted index on the %s surface", async (surface) => {
    const { harness, plugin } = await fixture();
    await harness.ctx.state.set(key(STATE_KEYS.laneIndex), MIXED_INDEX);
    const stateGet = vi.spyOn(harness.ctx.state, "get");
    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secret = vi.spyOn(harness.ctx.secrets, "resolve");
    const http = vi.spyOn(harness.ctx.http, "fetch");
    let data: UsageView;
    if (surface === "tool") {
      const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY });
      expect(result.error).toBeUndefined();
      data = result.data as unknown as UsageView;
    } else {
      const result = await plugin.onApiRequest!({
        routeKey: ROUTE_KEYS.usageSummary,
        method: "GET",
        path: "/usage-summary",
        companyId: COMPANY,
        query: {}, params: {}, headers: {}, body: null,
        actor: { actorType: "agent", actorId: "fixture" },
      });
      expect(result.status).toBe(200);
      data = result.body as UsageView;
    }

    expect(data.lanes).toEqual(["zai.json"]);
    expect(Object.keys(data.laneSnapshots)).toEqual(["zai.json"]);
    expect(data.accountsCooling).toBe(1);
    const laneReads = stateGet.mock.calls.map(([request]) => request.stateKey)
      .filter((stateKey) => stateKey.startsWith("cliproxy-insight:lane:"));
    expect(laneReads).toEqual(["cliproxy-insight:lane:zai.json"]);
    expect(stateSet).not.toHaveBeenCalled();
    expect(secret).not.toHaveBeenCalled();
    expect(http).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("unions a newly polled lane with normalized stored history without blank keys or double counts", async () => {
    const { harness } = await fixture({
      pollingEnabled: true,
      baseUrl: "https://telemetry.example.invalid/telemetry/cliproxy",
      laneFiles: ["claude.json"],
      legacyAggregateFiles: false,
      laneApiKeySecretRef: { type: "secret_ref", secretId: "11111111-1111-4111-8111-111111111111" },
    });
    await harness.ctx.state.set(key(STATE_KEYS.laneIndex), MIXED_INDEX);
    const secret = vi.spyOn(harness.ctx.secrets, "resolve").mockResolvedValue("fixture-poll-only");
    fetchSpy.mockImplementation(async () => new Response(JSON.stringify({
      schemaVersion: 1,
      observedAt: NOW,
      staleAfterSeconds: 300,
      records: [{ lane: "claude.json-account", health: "healthy", weekly_utilization: 0.4 }],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const stateGet = vi.spyOn(harness.ctx.state, "get");
    const stateSet = vi.spyOn(harness.ctx.state, "set");

    await harness.runJob(JOB_KEYS.poll);

    expect(secret).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0]?.[0]).toBe("https://telemetry.example.invalid/telemetry/cliproxy/claude.json");
    expect(harness.metrics.filter((metric) => metric.name === "cliproxy_insight.poll_ok"))
      .toEqual([expect.objectContaining({ value: 1 })]);
    expect((await harness.ctx.state.get(key(STATE_KEYS.laneIndex)) as string[]).sort())
      .toEqual(["claude.json", "zai.json"]);
    const indexWrites = stateSet.mock.calls.filter(([request]) => request.stateKey === STATE_KEYS.laneIndex);
    expect(indexWrites).toHaveLength(1);
    expect([...(indexWrites[0]?.[1] as string[])].sort()).toEqual(["claude.json", "zai.json"]);
    const readsAndWrites = [...stateGet.mock.calls, ...stateSet.mock.calls].map(([request]) => request.stateKey);
    expect(readsAndWrites.filter((stateKey) => stateKey === STATE_KEYS.lane("") || stateKey === STATE_KEYS.lane("   ")))
      .toEqual([]);

    const writesBeforeRead = stateSet.mock.calls.length;
    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY });
    expect(result.error).toBeUndefined();
    const data = result.data as unknown as UsageView;
    expect([...data.lanes].sort()).toEqual(["claude.json", "zai.json"]);
    expect(Object.keys(data.laneSnapshots).sort()).toEqual(["claude.json", "zai.json"]);
    expect(data.accountsCooling).toBe(1);
    expect(stateSet.mock.calls).toHaveLength(writesBeforeRead);
    expect(secret).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});

describe("lane refusal telemetry", () => {
  const refusals = [
    { reason: "invalid_shape", lane: { input: "selector-input-marker" } },
    { reason: "unknown_id", lane: "unindexed-selector-input-marker.json" },
  ];

  it.each(refusals)("records one bounded $reason refusal without echoing the selector", async ({ reason, lane }) => {
    const { harness } = await fixture();
    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, lane });
    expect(result).toEqual(unknownLane());
    expect(harness.metrics.filter((metric) => metric.name === "cliproxy_insight.lane_selection_refused"))
      .toEqual([{ name: "cliproxy_insight.lane_selection_refused", value: 1, tags: { companyId: COMPANY, reason } }]);
    expect(JSON.stringify({ result, metrics: harness.metrics, logs: harness.logs }))
      .not.toContain("selector-input-marker");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each(refusals)("keeps the exact $reason rejection when the metric sink rejects", async ({ lane }) => {
    const { harness } = await fixture();
    vi.spyOn(harness.ctx.metrics, "write").mockRejectedValue(new Error("raw-metric-exception-marker"));
    const result = harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, lane });
    await expect(result).resolves.toEqual(unknownLane());
    expect(JSON.stringify({ result: await result, logs: harness.logs })).not.toContain("raw-metric-exception-marker");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
