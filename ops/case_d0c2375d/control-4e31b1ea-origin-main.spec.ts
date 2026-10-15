// case_d0c2375d POSITIVE CONTROL — origin/main arm (4e31b1ea). EVIDENCE ONLY.
// Expected RED; do NOT place in plugins/model-selection/tests/ on any branch
// where the suite must stay green. See README.md in this directory.
import { describe, expect, it } from "vitest";

import { modelOverrideForContext } from "../src/engine/context.js";
import type { ModelEntry } from "../src/engine/types.js";

const SOL = "gpt-5.6-sol";
const LUNA = "gpt-5.6-luna";
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
  model({ id: SOL, tier: "T2", laneId: CODEX_LANE }),
  model({ id: LUNA, tier: "T3", laneId: CODEX_LANE }),
  model({ id: "claude-sonnet-5", tier: "T2", laneId: CLAUDE_LANE }),
  model({ id: OPUS, tier: "T1", laneId: CLAUDE_LANE }),
  model({ id: "claude-haiku-4-5-20251001", tier: "T3", laneId: CLAUDE_LANE }),
];

const AGENT_ENV = {
  ANTHROPIC_AUTH_TOKEN: { type: "secret_ref", secretKey: "ANTHROPIC_AUTH_TOKEN" },
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

describe("case_d0c2375d POSITIVE CONTROL on origin/main: the repin write strands all six env surfaces (expected RED)", () => {
  it("THE PREDICATE (must FAIL on this commit): all seven surfaces land on a healthy lane", () => {
    const selected = ROSTER.find((entry) => entry.id === OPUS)!;
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
