/**
 * Independent acceptance: company invalidation while the persisted lane index
 * is awaited must stop the tool before any further state read. Removing only
 * the post-lane-index fence must fail even if a later fence still rejects.
 */
import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { STATE_KEYS, TOOL_NAMES } from "../src/constants.js";
import manifest from "../src/manifest.js";
import { createPlugin } from "../src/worker.js";

const COMPANY = "00000000-0000-4000-8000-000000000003";
const FOREIGN_COMPANY = "00000000-0000-4000-8000-000000000099";
const FROZEN_NOW = "2026-10-01T12:00:00.000Z";
const LANES = ["claude.json", "zai.json"];
const CONFIG = { pollingEnabled: false };

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(FROZEN_NOW));
  fetchSpy = vi.fn().mockRejectedValue(new Error("acceptance: outbound fetch is forbidden"));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("lane-index company fence", () => {
  it.each([
    { name: "selected", params: { lane: "zai.json" } },
    { name: "unfiltered", params: {} },
  ])("stops $name reads when company scope is invalidated during the lane-index await", async ({ params }) => {
    const harness = createTestHarness({ manifest, config: CONFIG });
    harness.seed({ companies: [{ id: COMPANY, name: "Fixture company" } as never] });
    const { definition } = createPlugin();
    if (!definition.setup || !definition.onConfigChanged) {
      throw new Error("plugin lifecycle handlers are required");
    }
    await definition.setup(harness.ctx);
    await definition.onConfigChanged(CONFIG, { companyId: COMPANY });

    const key = (stateKey: string) => ({ scopeKind: "company" as const, scopeId: COMPANY, stateKey });
    await harness.ctx.state.set(key(STATE_KEYS.laneIndex), LANES);
    for (const laneFile of LANES) {
      await harness.ctx.state.set(key(STATE_KEYS.lane(laneFile)), {
        schemaVersion: 1,
        laneFile,
        polledAt: FROZEN_NOW,
        observedAt: FROZEN_NOW,
        staleAfterSeconds: 300,
        records: [{ lane: `${laneFile}-account`, health: "healthy", weekly_utilization: 0.4 }],
      });
    }

    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secretResolve = vi.spyOn(harness.ctx.secrets, "resolve");
    const httpFetch = vi.spyOn(harness.ctx.http, "fetch");
    const readState = harness.ctx.state.get.bind(harness.ctx.state);
    const stateGet = vi.spyOn(harness.ctx.state, "get").mockImplementation(async (request) => {
      const persisted = await readState(request);
      if (request.stateKey === STATE_KEYS.laneIndex) {
        // Deliver real lifecycle invalidation before this pending read resolves.
        await expect(definition.onConfigChanged!(CONFIG, { companyId: FOREIGN_COMPANY }))
          .rejects.toThrow("exactly one configured company");
      }
      return persisted;
    });

    const result = await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      ...params,
    });

    expect(result).toEqual({
      error: "company is not configured",
      data: { ok: false, error: "company is not configured" },
    });
    // Checking only lane snapshots misses a provider-index read before a later
    // fence. This exact sequence forbids every subsequent state read instead.
    expect(stateGet.mock.calls).toEqual([[{
      scopeKind: "company",
      scopeId: COMPANY,
      stateKey: STATE_KEYS.laneIndex,
    }]]);
    expect(stateSet).not.toHaveBeenCalled();
    expect(secretResolve).not.toHaveBeenCalled();
    expect(httpFetch).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
