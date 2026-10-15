import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import type { Issue } from "@paperclipai/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import manifest from "../src/manifest.js";
import { CLOSEOUT_LEDGER_SCHEMA } from "../src/closeout-ledger.js";
import { PLUGIN_STATE_KEYS, TOOL_NAMES } from "../src/constants.js";
import { createPlugin } from "../src/worker.js";
import { MODELS, NO_ESCALATION, NOW, PROFILES } from "./fixtures.js";

/**
 * case_2945082b: flag-gated closeout-ledger append verb — worker half.
 *
 * Flag on appends (and the append persists under its own state key, read
 * back across calls); flag off no-ops and writes nothing; malformed records
 * and missing scope fail closed with stable codes; garbage input never
 * throws (case_8b9297d6 envelope).
 */

const COMPANY = "co-1";
const ISSUE = "issue-1";
const TIER_LABEL_ID = "lbl-t1";
const OTHER_LABEL_ID = "lbl-other";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function issue(overrides: Partial<Issue> = {}): Issue {
  return {
    id: ISSUE,
    companyId: COMPANY,
    title: "Rename a constant",
    status: "done",
    assigneeAgentId: null,
    assigneeAdapterOverrides: null,
    labels: [
      { id: TIER_LABEL_ID, companyId: COMPANY, name: "tier:T1" },
      { id: OTHER_LABEL_ID, companyId: COMPANY, name: "area:platform" },
    ],
    labelIds: [TIER_LABEL_ID, OTHER_LABEL_ID],
    ...overrides,
  } as unknown as Issue;
}

function baseConfig(overrides: Record<string, unknown> = {}) {
  return {
    selection: { enabled: true, mode: "advise", holdOnUntrustedProfile: true },
    models: MODELS,
    tierLabelIds: { T1: TIER_LABEL_ID },
    ...overrides,
  };
}

function onConfig(overrides: Record<string, unknown> = {}) {
  return baseConfig({ closeoutLedger: { enabled: true }, ...overrides });
}

async function boot(config: Record<string, unknown>, seedIssue = issue()) {
  const harness = createTestHarness({ manifest, config });
  harness.seed({ issues: [seedIssue] });
  const plugin = createPlugin();
  const setup = plugin.definition.setup;
  if (!setup) throw new Error("plugin definition has no setup handler");
  await setup(harness.ctx);
  const onConfigChanged = plugin.definition.onConfigChanged;
  if (!onConfigChanged) throw new Error("plugin definition has no onConfigChanged handler");
  await onConfigChanged(config, { companyId: COMPANY });
  await harness.ctx.state.set(
    { scopeKind: "company", scopeId: COMPANY, stateKey: PLUGIN_STATE_KEYS.volumeProfiles },
    { profiles: PROFILES, signals: NO_ESCALATION },
  );
  return harness;
}

const runCtx = { companyId: COMPANY, agentId: "agent-1", runId: "run-1" };

const RECORD = {
  mergeSha: "be02c31",
  disposition: "merged",
  evidenceLink: "https://github.com/TogetherWeOwn/paperclip-ops/pull/523",
};

async function readLedger(harness: Awaited<ReturnType<typeof boot>>) {
  return (await harness.ctx.state.get({
    scopeKind: "company",
    scopeId: COMPANY,
    stateKey: PLUGIN_STATE_KEYS.closeoutLedger,
  })) as { schema?: unknown; records?: Array<Record<string, unknown>> } | null | undefined;
}

describe("closeoutLedgerAppend: flag-gated append verb", () => {
  it("writes nothing when disabled — the ledger key stays empty", async () => {
    const harness = await boot(baseConfig());
    const result = (await harness.executeTool(TOOL_NAMES.closeoutLedgerAppend, RECORD, runCtx)) as {
      content: unknown;
      data: unknown;
    };
    expect(typeof result.content).toBe("string");
    expect(result.data).toMatchObject({ ok: false, error: "closeout-ledger-disabled" });
    // The harness returns null for a missing key: absent, never a ledger.
    expect(await readLedger(harness)).toBeNull();
  });

  it("appends a valid record when enabled and reads it back across calls", async () => {
    const harness = await boot(onConfig());
    const first = (await harness.executeTool(TOOL_NAMES.closeoutLedgerAppend, RECORD, runCtx)) as {
      content: unknown;
      data: { ok: unknown; record: Record<string, unknown>; recordCount: unknown };
    };
    expect(first.data).toMatchObject({
      ok: true,
      record: { mergeSha: "be02c31", disposition: "merged" },
      recordCount: 1,
    });
    expect(typeof first.data.record.appendedAt).toBe("string");

    const second = (await harness.executeTool(
      TOOL_NAMES.closeoutLedgerAppend,
      { ...RECORD, mergeSha: "a1b2c3d", disposition: "decision-recorded" },
      runCtx,
    )) as { data: { recordCount: unknown } };
    expect(second.data.recordCount).toBe(2);

    const stored = await readLedger(harness);
    expect(stored?.schema).toBe(CLOSEOUT_LEDGER_SCHEMA);
    expect(stored?.records?.map((r) => r.mergeSha)).toEqual(["be02c31", "a1b2c3d"]);
  });

  it("fails closed on a malformed record and changes nothing", async () => {
    const harness = await boot(onConfig());
    const cases: Array<Record<string, unknown>> = [
      {},
      { mergeSha: "xyz", disposition: "merged", evidenceLink: "https://example.test/pr/1" },
      { mergeSha: "be02c31", disposition: "shipped-it", evidenceLink: "https://example.test/pr/1" },
      { mergeSha: "be02c31", disposition: "merged", evidenceLink: "not a link" },
      { mergeSha: "be02c31", disposition: "merged" },
    ];
    for (const params of cases) {
      const result = (await harness.executeTool(TOOL_NAMES.closeoutLedgerAppend, params, runCtx)) as {
        content: unknown;
        data: unknown;
      };
      expect(result.data, JSON.stringify(params)).toMatchObject({ ok: false, error: "malformed-closeout-record" });
      expect((result.data as { issues: unknown }).issues).toBeDefined();
    }
    expect(await readLedger(harness)).toBeNull();
  });

  it("fails closed without company scope, like every other stateful tool", async () => {
    const harness = await boot(onConfig());
    const result = (await harness.executeTool(
      TOOL_NAMES.closeoutLedgerAppend,
      RECORD,
      { ...runCtx, companyId: "" as never },
    )) as { content: unknown; data: unknown };
    expect(typeof result.content).toBe("string");
    expect(result.data).toMatchObject({ ok: false, error: "missing-company-scope" });
  });

  it("normalizes corrupt stored state instead of throwing", async () => {
    const harness = await boot(onConfig());
    await harness.ctx.state.set(
      { scopeKind: "company", scopeId: COMPANY, stateKey: PLUGIN_STATE_KEYS.closeoutLedger },
      { schema: "something-else-v9", records: "junk" },
    );
    const result = (await harness.executeTool(TOOL_NAMES.closeoutLedgerAppend, RECORD, runCtx)) as {
      data: { ok: unknown; recordCount: unknown };
    };
    // Corrupt state normalizes to empty, then the valid append lands as record 1.
    expect(result.data).toMatchObject({ ok: true, recordCount: 1 });
  });
});
