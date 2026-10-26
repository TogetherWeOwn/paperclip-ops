/**
 * PAIRED ACCEPTANCE — optional exact lane selector on `get_provider_usage`. IMMUTABLE.
 *
 * Test-Author artifact: the paired Builder consumes this file and MUST NEVER EDIT IT.
 * A Builder that cannot satisfy a pinned value below has found a spec question, not
 * an editing license: raise it on the build card instead.
 *
 * ## Pinned contract (the Builder implements exactly this)
 *
 * - Selector: optional `lane` parameter on the EXISTING `get_provider_usage` tool.
 *   No new tool, route, registry, or dependency.
 * - Value rule (fail-closed): an omitted/`undefined` selector returns every
 *   persisted lane (today's contract, unchanged). A PRESENT selector — including
 *   `null`, `""`, non-strings, and unknown strings — MUST be an exact member of
 *   the persisted lane index or the call is rejected. Never infer
 *   provider-to-lane equivalence (`"zai"` is not `"zai.json"`), never treat an
 *   account id as a lane id, and never build a state key from raw input: read the
 *   index first, then read the selected lane only when it is a member.
 * - Rejection shape (stable, machine-readable, leak-free):
 *   `error: "unknown_lane"`, and `data` is EXACTLY
 *   `{ ok: false, error: "unknown_lane", retryable: false,
 *       next_action: "use a lane id from the persisted lane index" }`.
 *   No lane data at all — not even the index — so a rejected call can never leak
 *   lane records. The caller learns valid IDs from an unfiltered call, whose
 *   `lanes` array is the machine-readable index.
 * - Ordering: company scope is validated BEFORE any state read (a foreign company
 *   plus an invalid lane reports the company fence, never the lane error, and
 *   reads nothing). Selected success reads the lane index and the selected lane
 *   only — never another lane's state.
 * - Selected success returns ONLY the selected lane in `lanes`/`laneSnapshots`,
 *   counts `accountsCooling` for that subset, and keeps `providers`/`snapshots`/
 *   `modelUsage` at their existing scope. Freshness and read-time cooldown reuse
 *   the existing logic; a missing record for an indexed lane reads conservatively
 *   as `null` (success, not rejection).
 * - Discoverability: `lane` appears in the manifest `parametersSchema` as an
 *   optional string whose description references the lane index; the manifest
 *   tool description names lane selection; the plugin README documents `` `lane` ``,
 *   references the lane index, and states the stored-history (not serving
 *   readiness) scope. Required phrases: `` `lane` ``, "lane index",
 *   "stored history", "not serving readiness".
 * - Calls stay read-only and idempotent: no fetch, no secret resolution, no
 *   state/config write, deterministic finite JSON.
 *
 * ## Baseline expectation
 *
 * On the pre-feature base, the "registered schema", "selected reads", and
 * "rejected selectors" blocks are RED (the selector is ignored or absent) and
 * the "controls" block is GREEN (today's contract, the company fence, and the
 * no-new-surface pins). RED here is the artifact, not a defect: every failing
 * test asserts specified behavior through the real tool boundary with synthetic
 * offline inputs — there are no unconditional failing placeholders.
 *
 * Run from `plugins/cliproxy-insight`:
 *   npm run typecheck && npx vitest run tests/lane-selector.acceptance.spec.ts
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import manifest from "../src/manifest.js";
import { STATE_KEYS, TOOL_NAMES } from "../src/constants.js";
import { createPlugin } from "../src/worker.js";

const here = dirname(fileURLToPath(import.meta.url));

const COMPANY = "00000000-0000-4000-8000-000000000003";
const FOREIGN_COMPANY = "00000000-0000-4000-8000-000000000099";
const FROZEN_NOW = "2026-10-01T12:00:00.000Z";
const LANE_A = "claude.json";
const LANE_B = "zai.json";
/** Frozen-now-relative instants: +5 min (active) and -1 min (expired). */
const COOLING_UNTIL = "2026-10-01T12:05:00.000Z";
const EXPIRED_UNTIL = "2026-10-01T11:59:00.000Z";
/** One hour old against a 300 s budget: stale by construction. */
const STALE_OBSERVED = "2026-10-01T11:00:00.000Z";

interface UsageData {
  version: string;
  providers: string[];
  snapshots: Record<string, unknown>;
  lanes: string[];
  laneSnapshots: Record<string, unknown>;
  accountsCooling: number;
  modelUsage: unknown;
}

interface ToolResult {
  error?: string;
  data?: unknown;
}

interface LaneSnapshotView {
  stale: boolean;
  accounts: { account: string; cooldown: unknown }[];
}

async function harnessFor() {
  const harness = createTestHarness({ manifest, config: { pollingEnabled: false } });
  harness.seed({ companies: [{ id: COMPANY, name: "Co" } as never] });
  const { definition } = createPlugin();
  if (!definition.setup) throw new Error("plugin definition has no setup handler");
  await definition.setup(harness.ctx);
  if (!definition.onConfigChanged) throw new Error("missing company config handler");
  await definition.onConfigChanged({ pollingEnabled: false }, { companyId: COMPANY });
  return harness;
}

type Harness = Awaited<ReturnType<typeof harnessFor>>;

let fetchSpy: ReturnType<typeof vi.fn> = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(FROZEN_NOW));
  fetchSpy = vi.fn().mockRejectedValue(new Error("lane-selector acceptance: the tool must not fetch"));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function laneSnapshot(
  laneFile: string,
  records: Record<string, unknown>[],
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    laneFile,
    polledAt: FROZEN_NOW,
    observedAt: FROZEN_NOW,
    staleAfterSeconds: 300,
    records,
    ...overrides,
  };
}

function coolingRecord(account: string, until: string): Record<string, unknown> {
  return {
    lane: account,
    health: "healthy",
    exhausted_until: until,
    exhausted_reason: "acceptance cooldown",
  };
}

function servingRecord(account: string): Record<string, unknown> {
  return { lane: account, health: "healthy", weekly_utilization: 0.4 };
}

async function seedLanes(
  harness: Harness,
  files: string[],
  snapshots: Record<string, Record<string, unknown>>,
): Promise<void> {
  const key = (stateKey: string) => ({ scopeKind: "company" as const, scopeId: COMPANY, stateKey });
  await harness.ctx.state.set(key(STATE_KEYS.laneIndex), files);
  for (const [file, snapshot] of Object.entries(snapshots)) {
    await harness.ctx.state.set(key(STATE_KEYS.lane(file)), snapshot);
  }
}

/** Two indexed lanes, each with one actively cooling account: subset math is exact. */
async function seedTwoCoolingLanes(harness: Harness): Promise<void> {
  await seedLanes(harness, [LANE_A, LANE_B], {
    [LANE_A]: laneSnapshot(LANE_A, [coolingRecord("claude-lane-1", COOLING_UNTIL)]),
    [LANE_B]: laneSnapshot(LANE_B, [coolingRecord("zai-lane-1", COOLING_UNTIL)]),
  });
}

async function seedProviderAndModels(harness: Harness): Promise<void> {
  const key = (stateKey: string) => ({ scopeKind: "company" as const, scopeId: COMPANY, stateKey });
  await harness.ctx.state.set(key(STATE_KEYS.providerIndex), ["claude"]);
  await harness.ctx.state.set(key(STATE_KEYS.provider("claude")), {
    schemaVersion: 1,
    provider: "claude",
    polledAt: FROZEN_NOW,
    observedAt: FROZEN_NOW,
    success: 7,
    failed: 0,
    raw: { success: 7, failed: 0 },
  });
  await harness.ctx.state.set(key(STATE_KEYS.modelUsage), {
    schemaVersion: 1,
    polledAt: FROZEN_NOW,
    observedAt: FROZEN_NOW,
    staleAfterSeconds: 300,
    telemetry: "available",
    reasonCode: null,
    models: { "oc/claude-opus-5": { serviceable: true } },
  });
}

async function callTool(params: Record<string, unknown>): Promise<ToolResult> {
  const harness = await harnessFor();
  return (await harness.executeTool(TOOL_NAMES.getProviderUsage, params)) as ToolResult;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function laneView(value: unknown): LaneSnapshotView | null {
  if (value === null) return null;
  if (!isPlainObject(value)) throw new Error("expected a lane snapshot object or null");
  return value as unknown as LaneSnapshotView;
}

describe("lane selector: registered schema (RED until implemented)", () => {
  it("declares an optional `lane` string whose description references the lane index", () => {
    const tool = manifest.tools?.find((t) => t.name === TOOL_NAMES.getProviderUsage);
    expect(tool, "get_provider_usage is registered").toBeDefined();
    const schema = tool?.parametersSchema as unknown as {
      properties: Record<string, { type?: unknown; description?: unknown }>;
      required?: unknown;
    };
    expect(schema.properties["lane"], "parametersSchema.properties.lane").toBeDefined();
    expect(schema.properties["lane"]?.type).toBe("string");
    expect(schema.properties["lane"]?.description).toMatch(/lane index/i);
    // Optional: the absent-selector contract keeps working with companyId alone.
    expect(schema.required).toEqual(["companyId"]);
  });

  it("names lane selection in the registered tool description", () => {
    const tool = manifest.tools?.find((t) => t.name === TOOL_NAMES.getProviderUsage);
    expect(tool?.description).toMatch(/lane/i);
  });

  it("documents `lane`, the lane index, and the stored-history (not serving readiness) scope in the README", () => {
    const readme = readFileSync(join(here, "..", "README.md"), "utf8");
    expect(readme).toContain("`lane`");
    expect(readme).toMatch(/lane index/);
    expect(readme).toMatch(/stored history/i);
    expect(readme).toMatch(/not serving readiness/i);
  });
});

describe("lane selector: selected reads (RED until implemented)", () => {
  it("returns only the selected lane in lanes/laneSnapshots, with stable account ids", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;

    expect(result.error).toBeUndefined();
    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_B]);
    expect(Object.keys(data.laneSnapshots).sort()).toEqual([LANE_B]);
    expect(laneView(data.laneSnapshots[LANE_B])?.accounts[0]?.account).toBe("zai-lane-1");
  });

  it("computes accountsCooling for the selected subset (1 of 2 cooling lanes)", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);

    const selected = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_A,
    })) as ToolResult;
    const unfiltered = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    expect((unfiltered.data as UsageData).accountsCooling).toBe(2);
    expect((selected.data as UsageData).accountsCooling).toBe(1);
  });

  it("keeps providers/snapshots/modelUsage at their existing scope on a selected call", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    await seedProviderAndModels(harness);

    const selected = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;
    const unfiltered = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    const selectedData = selected.data as UsageData;
    const unfilteredData = unfiltered.data as UsageData;
    // Selection took effect on the lane half…
    expect(selectedData.lanes).toEqual([LANE_B]);
    // …while the provider half is untouched.
    expect(selectedData.providers).toEqual(unfilteredData.providers);
    expect(selectedData.providers).toEqual(["claude"]);
    expect(selectedData.snapshots).toEqual(unfilteredData.snapshots);
    expect(selectedData.modelUsage).toEqual(unfilteredData.modelUsage);
  });

  it("reads the lane index and the selected lane only — never other lane state", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    await seedProviderAndModels(harness);
    const stateGet = vi.spyOn(harness.ctx.state, "get");

    await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, lane: LANE_B });

    const keys = stateGet.mock.calls.map((call) => {
      const key = call[0] as { stateKey?: unknown };
      return typeof key.stateKey === "string" ? key.stateKey : "";
    });
    expect(keys).toContain(STATE_KEYS.laneIndex);
    expect(keys).toContain(STATE_KEYS.lane(LANE_B));
    expect(
      keys.filter((k) => k.startsWith("cliproxy-insight:lane:") && k !== STATE_KEYS.laneIndex),
    ).toEqual([STATE_KEYS.lane(LANE_B)]);
  });

  it("returns a smaller payload than the unfiltered call", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);

    const selected = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;
    const unfiltered = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    const selectedBytes = JSON.stringify(selected.data)?.length ?? 0;
    const unfilteredBytes = JSON.stringify(unfiltered.data)?.length ?? 0;
    expect(selectedBytes).toBeGreaterThan(0);
    expect(selectedBytes).toBeLessThan(unfilteredBytes);
  });

  it("is deterministic across repeated selected calls and differs from unfiltered", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    const params = { companyId: COMPANY, lane: LANE_B };

    const first = (await harness.executeTool(TOOL_NAMES.getProviderUsage, params)) as ToolResult;
    const second = (await harness.executeTool(TOOL_NAMES.getProviderUsage, params)) as ToolResult;
    const unfiltered = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    expect(first.data).toEqual(second.data);
    // Finite JSON: a stringify round-trip is lossless (no undefined/functions/NaN).
    expect(JSON.parse(JSON.stringify(first.data) ?? "null")).toEqual(first.data);
    expect((first.data as UsageData).lanes).not.toEqual((unfiltered.data as UsageData).lanes);
  });

  it("applies provider and lane filters together", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    await seedProviderAndModels(harness);

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      provider: "claude",
      lane: LANE_B,
    })) as ToolResult;

    const data = result.data as UsageData;
    expect(data.providers).toEqual(["claude"]);
    expect(data.lanes).toEqual([LANE_B]);
  });

  it("reads a missing record for an indexed lane conservatively as null (success, not rejection)", async () => {
    const harness = await harnessFor();
    await seedLanes(harness, [LANE_A, LANE_B], {
      [LANE_A]: laneSnapshot(LANE_A, [coolingRecord("claude-lane-1", COOLING_UNTIL)]),
      // LANE_B is indexed but has no persisted snapshot.
    });

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;

    expect(result.error).toBeUndefined();
    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_B]);
    expect(data.laneSnapshots[LANE_B] ?? "present").toBeNull();
    expect(data.accountsCooling).toBe(0);
  });

  it("flags a fresh selected lane as not stale", async () => {
    const harness = await harnessFor();
    await seedLanes(harness, [LANE_A, LANE_B], {
      [LANE_A]: laneSnapshot(LANE_A, [servingRecord("claude-lane-1")]),
      [LANE_B]: laneSnapshot(LANE_B, [servingRecord("zai-lane-1")]),
    });

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_A,
    })) as ToolResult;

    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_A]);
    expect(laneView(data.laneSnapshots[LANE_A])?.stale).toBe(false);
  });

  it("flags a selected lane older than the budget as stale", async () => {
    const harness = await harnessFor();
    await seedLanes(harness, [LANE_A, LANE_B], {
      [LANE_A]: laneSnapshot(LANE_A, [servingRecord("claude-lane-1")]),
      [LANE_B]: laneSnapshot(LANE_B, [servingRecord("zai-lane-1")], { observedAt: STALE_OBSERVED }),
    });

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;

    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_B]);
    expect(laneView(data.laneSnapshots[LANE_B])?.stale).toBe(true);
  });

  it("stops counting a selected lane whose cooldown instant has passed (read-time expiry)", async () => {
    const harness = await harnessFor();
    await seedLanes(harness, [LANE_A, LANE_B], {
      [LANE_A]: laneSnapshot(LANE_A, [coolingRecord("claude-lane-1", COOLING_UNTIL)]),
      [LANE_B]: laneSnapshot(LANE_B, [coolingRecord("zai-lane-1", EXPIRED_UNTIL)]),
    });

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane: LANE_B,
    })) as ToolResult;

    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_B]);
    expect(data.accountsCooling).toBe(0);
    expect(laneView(data.laneSnapshots[LANE_B])?.accounts[0]?.cooldown).toBeNull();
  });
});

describe("lane selector: rejected selectors (RED until implemented)", () => {
  const cases: { name: string; lane: unknown; echo: string | null }[] = [
    { name: "unknown lane id", lane: "kemono.json", echo: "kemono.json" },
    { name: "empty string", lane: "", echo: null },
    { name: "whitespace-only string", lane: "   ", echo: null },
    { name: "number", lane: 123, echo: null },
    { name: "boolean", lane: true, echo: null },
    { name: "array", lane: ["zai.json"], echo: "zai.json" },
    { name: "object", lane: { lane: "zai.json" }, echo: "zai.json" },
    { name: "null", lane: null, echo: null },
    { name: "path traversal", lane: "../secret", echo: "../secret" },
    { name: "nested traversal", lane: "zai.json/../claude.json", echo: "zai.json/../claude.json" },
    {
      name: "state-key injection",
      lane: "cliproxy-insight:lane:zai.json",
      echo: "cliproxy-insight:lane:zai.json",
    },
    // No provider-to-lane inference: "zai" is not "zai.json".
    { name: "provider name without lane suffix", lane: "zai", echo: "zai" },
    { name: "account id instead of lane id", lane: "zai-lane-1", echo: "zai-lane-1" },
  ];

  it.each(cases)("rejects $name with the stable unknown_lane contract", async ({ lane, echo }) => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    const stateGet = vi.spyOn(harness.ctx.state, "get");

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
      lane,
    })) as ToolResult;

    expect(result.error).toBe("unknown_lane");
    expect(isPlainObject(result.data)).toBe(true);
    const data = result.data as Record<string, unknown>;
    expect(data).toEqual({
      ok: false,
      error: "unknown_lane",
      retryable: false,
      next_action: "use a lane id from the persisted lane index",
    });
    // No lane data of any kind on the rejection path.
    expect(data).not.toHaveProperty("lanes");
    expect(data).not.toHaveProperty("laneSnapshots");
    if (echo !== null) {
      expect(JSON.stringify(result)).not.toContain(echo);
    }
    // The index may be read to validate membership; no lane snapshot ever is —
    // in particular, no state key is ever derived from the rejected input.
    const keys = stateGet.mock.calls.map((call) => {
      const key = call[0] as { stateKey?: unknown };
      return typeof key.stateKey === "string" ? key.stateKey : "";
    });
    expect(
      keys.filter((k) => k.startsWith("cliproxy-insight:lane:") && k !== STATE_KEYS.laneIndex),
    ).toEqual([]);
  });
});

describe("lane selector: controls (GREEN on baseline — today's contract, unchanged)", () => {
  it("adds no tool: the registry is exactly get_provider_usage", () => {
    expect(Object.values(TOOL_NAMES)).toEqual(["get_provider_usage"]);
    expect(manifest.tools?.map((t) => t.name)).toEqual(["get_provider_usage"]);
  });

  it("adds no route: the API surface is exactly usage-summary", () => {
    expect(manifest.apiRoutes?.map((r) => r.routeKey)).toEqual(["usage-summary"]);
  });

  it("absent selector returns every persisted lane", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    const data = result.data as UsageData;
    expect(data.lanes).toEqual([LANE_A, LANE_B]);
    expect(Object.keys(data.laneSnapshots).sort()).toEqual([LANE_A, LANE_B]);
  });

  it("absent selector counts cooling across all lanes", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);

    const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
      companyId: COMPANY,
    })) as ToolResult;

    expect((result.data as UsageData).accountsCooling).toBe(2);
  });

  it("keeps the missing-companyId rejection with plain-object data", async () => {
    const result = await callTool({});
    expect(result.error).toBe("companyId is required");
    expect(isPlainObject(result.data)).toBe(true);
  });

  it("keeps the unconfigured-company rejection with plain-object data", async () => {
    const result = await callTool({ companyId: FOREIGN_COMPANY });
    expect(result.error).toBe("company is not configured");
    expect(isPlainObject(result.data)).toBe(true);
  });

  it.each([{ lane: LANE_B }, { lane: "../secret" }])(
    "validates company scope before any state read (lane: $lane)",
    async ({ lane }) => {
      const harness = await harnessFor();
      await seedTwoCoolingLanes(harness);
      const stateGet = vi.spyOn(harness.ctx.state, "get");
      const configGet = vi.spyOn(harness.ctx.config, "get");

      const result = (await harness.executeTool(TOOL_NAMES.getProviderUsage, {
        companyId: FOREIGN_COMPANY,
        lane,
      })) as ToolResult;

      // The company fence wins over lane validation, and nothing is read.
      expect(result.error).toBe("company is not configured");
      expect(stateGet).not.toHaveBeenCalled();
      expect(configGet).not.toHaveBeenCalled();
    },
  );

  it("selected, rejected, and unfiltered calls write nothing and reach neither secrets nor the network", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    const stateSet = vi.spyOn(harness.ctx.state, "set");
    const secretsResolve = vi.spyOn(harness.ctx.secrets, "resolve");

    await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY });
    await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, lane: LANE_B });
    await harness.executeTool(TOOL_NAMES.getProviderUsage, { companyId: COMPANY, lane: "nope.json" });

    expect(stateSet).not.toHaveBeenCalled();
    expect(secretsResolve).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("unfiltered calls stay deterministic with finite JSON", async () => {
    const harness = await harnessFor();
    await seedTwoCoolingLanes(harness);
    const params = { companyId: COMPANY };

    const first = (await harness.executeTool(TOOL_NAMES.getProviderUsage, params)) as ToolResult;
    const second = (await harness.executeTool(TOOL_NAMES.getProviderUsage, params)) as ToolResult;

    expect(first.data).toEqual(second.data);
    expect(JSON.parse(JSON.stringify(first.data) ?? "null")).toEqual(first.data);
  });
});
