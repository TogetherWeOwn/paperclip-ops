// case_d0c2375d POSITIVE CONTROL — d808d668 arm (PR #319 tip). EVIDENCE ONLY.
// Expected RED; do NOT place in plugins/model-selection/tests/ on any branch
// where the suite must stay green. See README.md in this directory.
import { describe, expect, it } from "vitest";

import { modelOverrideForContext } from "../src/engine/context.js";
import type { LaneOutageOverride } from "../src/engine/pacing.js";
import { selectModel } from "../src/engine/select.js";
import type { ModelEntry } from "../src/engine/types.js";
import {
  autoQuarantineFor,
  laneExhaustionFromRunFailure,
  mergeLaneOutage,
} from "../src/lane-capacity/run-failure.js";
import { NO_ESCALATION, PROFILES, config } from "./fixtures.js";

const SOL = "gpt-5.6-sol";
const LUNA = "gpt-5.6-luna";
const HAIKU = "claude-haiku-4-5-20251001";
const OPUS = "claude-opus-5";
const CODEX_LANE = "cliproxy-codex";
const CLAUDE_LANE = "cliproxy-claude";

function model(entry: Partial<ModelEntry> & Pick<ModelEntry, "id" | "tier">): ModelEntry {
  return {
    enabled: true,
    costPerMTokIn: 0,
    costPerMTokOut: 0,
    costPerMTokCacheRead: 0,
    capabilities: ["tools"],
    contextWindow: 1_000_000,
    aaIndex: null,
    releasedAt: "1970-01-01",
    fallbackOnly: false,
    note: "",
    earnIn: null,
    ...entry,
  };
}

const ROSTER: ModelEntry[] = [
  model({ id: SOL, tier: "T2", laneId: CODEX_LANE, costPerMTokIn: 0.4, costPerMTokOut: 1.6, costPerMTokCacheRead: 0.04 }),
  model({ id: LUNA, tier: "T3", laneId: CODEX_LANE, costPerMTokIn: 0.1, costPerMTokOut: 0.4, costPerMTokCacheRead: 0.01 }),
  model({ id: "claude-sonnet-5", tier: "T2", laneId: CLAUDE_LANE, costPerMTokIn: 3, costPerMTokOut: 15, costPerMTokCacheRead: 0.3 }),
  model({ id: OPUS, tier: "T1", laneId: CLAUDE_LANE, costPerMTokIn: 15, costPerMTokOut: 75, costPerMTokCacheRead: 1.5 }),
  model({ id: HAIKU, tier: "T3", laneId: CLAUDE_LANE, costPerMTokIn: 1, costPerMTokOut: 5, costPerMTokCacheRead: 0.1 }),
];

const LIVE_429 =
  "API Error: Request rejected (429) · All credentials for model gpt-5.6-sol are cooling down "
  + "(last error: usage_limit_reached: The usage limit has been reached)";

const INCIDENT_MS = Date.parse("2026-09-16T16:40:00.000Z");

const AGENT_ENV = {
  ANTHROPIC_AUTH_TOKEN: { type: "secret_ref", secretKey: "ANTHROPIC_AUTH_TOKEN" },
  CLIPROXY_USAGE_LANE_KEY: { type: "secret_ref", secretKey: "CLIPROXY_USAGE_LANE_KEY" },
  GH_APP_REPOS: { type: "plain", value: "paperclip-ops" },
};

const FROZEN_OVERRIDE_ENV = {
  PAPERCLIP_ASSIGNED_MODEL: { type: "plain", value: SOL },
  CLAUDE_CODE_SUBAGENT_MODEL: { type: "plain", value: SOL },
  ANTHROPIC_DEFAULT_OPUS_MODEL: { type: "plain", value: SOL },
  ANTHROPIC_DEFAULT_SONNET_MODEL: { type: "plain", value: SOL },
  ANTHROPIC_DEFAULT_HAIKU_MODEL: { type: "plain", value: LUNA },
  ANTHROPIC_SMALL_FAST_MODEL: { type: "plain", value: LUNA },
};

const SEVEN_MODEL_KEYS = [
  "PAPERCLIP_ASSIGNED_MODEL",
  "CLAUDE_CODE_SUBAGENT_MODEL",
  "ANTHROPIC_DEFAULT_OPUS_MODEL",
  "ANTHROPIC_DEFAULT_SONNET_MODEL",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
  "ANTHROPIC_SMALL_FAST_MODEL",
] as const;

function laneOf(modelId: string | null): string | null {
  return ROSTER.find((entry) => entry.id === modelId)?.laneId ?? null;
}

describe("case_d0c2375d POSITIVE CONTROL: on the base commit, the repin write strands six surfaces (expected RED)", () => {
  const verdict = laneExhaustionFromRunFailure({ error: LIVE_429, models: ROSTER });
  const quarantine = verdict
    ? mergeLaneOutage(null, autoQuarantineFor(verdict, INCIDENT_MS), new Date(INCIDENT_MS).toISOString())
    : null;

  it("setup control: the quarantine is in force and the router leaves Codex", () => {
    expect(quarantine?.lanes).toContain(CODEX_LANE);
    const decision = selectModel({
      profiles: PROFILES,
      signals: NO_ESCALATION,
      now: INCIDENT_MS,
      descriptor: { issueId: "case_59a265cf", labelNames: ["tier:T1"] },
      config: config({
        models: ROSTER,
        pacingMode: "enforce",
        holdOnUntrustedProfile: false,
        laneOutageOverride: quarantine,
      }),
    });
    expect(decision.modelId).toBe(OPUS);
  });

  it("THE PREDICATE (must FAIL on this commit): all seven surfaces land on a healthy lane", () => {
    const decision = selectModel({
      profiles: PROFILES,
      signals: NO_ESCALATION,
      now: INCIDENT_MS,
      descriptor: { issueId: "case_59a265cf", labelNames: ["tier:T1"] },
      config: config({
        models: ROSTER,
        pacingMode: "enforce",
        holdOnUntrustedProfile: false,
        laneOutageOverride: quarantine,
      }),
    });
    const selected = ROSTER.find((entry) => entry.id === decision.modelId)!;
    const patch = modelOverrideForContext({
      model: selected,
      fleetCeilingTokens: 1_000_000,
      compactionRatio: 0.75,
      agentEnv: AGENT_ENV,
      existingOverrideEnv: FROZEN_OVERRIDE_ENV,
    });
    const env = patch.assigneeAdapterOverrides.adapterConfig.env ?? {};
    const surfaces: Record<string, string | null> = {
      model: laneOf(patch.assigneeAdapterOverrides.adapterConfig.model),
    };
    for (const key of SEVEN_MODEL_KEYS) {
      const value = env[key] as { type?: string; value?: string } | undefined;
      surfaces[key] = value?.type === "plain" ? laneOf(String(value.value)) : "(not a plain pin)";
    }
    for (const [surface, lane] of Object.entries(surfaces)) {
      expect(lane, `surface ${surface}`).toBe(CLAUDE_LANE);
    }
  });
});
