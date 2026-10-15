import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { mergeLedgerEntry, type LaneAvoidConfig, type LaneLedger } from "../src/engine/pacing.js";
import { selectModel } from "../src/engine/select.js";
import type { ModelEntry } from "../src/engine/types.js";
import { evaluateLanePace, type LanePaceObservation } from "../src/lane-capacity/pace.js";
import { MODELS, NO_ESCALATION, PROFILES, config } from "./fixtures.js";

/**
 * case_bd95ab6f replay: the same 23 recorded T1 decisions as the case_ec9827b5 replay,
 * with EVERY lane that hosts a candidate in the ledger, not just Meta.
 *
 * The case_ec9827b5 replay ledgers only `cliproxy-meta`. Claude, Codex and Z.ai then
 * have no pace verdict, rank `unknown` and tie, and the engine falls through to
 * cost order, which puts `gpt-6.1-sol` ($1.90) ahead of `claude-sonnet-5-5`
 * ($2.41). That is what `docs/lane-withdrawal.md` first recorded as the residual
 * "selector picks sol, bridge picks sonnet". It is an artifact of the thin
 * ledger: with the recorded lanes in place the engine never reaches cost order
 * between lanes, because pace state and deviation decide first.
 *
 * What these specs pin is therefore the measured structure, not a policy
 * preference: which rule orders the non-MUSE candidates, and where the selector
 * and the bridge part company once MUSE is withdrawn.
 */

interface ReplayAccount {
  accountKey: string;
  health: "healthy" | "degraded" | "exhausted" | "unavailable" | "unknown";
  serviceable: boolean;
  weight: number;
  governingWindow: string;
  governingResetAt: string;
  utilization: number;
}
interface MetaRecord {
  ts: string;
  regime: string;
  bridgeCeiling: number;
  bridge: { model: string; reason: string };
  selectorPick: string;
  ageSeconds: number;
  metaAccounts: ReplayAccount[];
  candidates: Array<{ model: string; lane: string; tier: "T1"; blended: number }>;
}
interface OtherLanesRecord {
  ts: string;
  bridgeClaude5hCombined: number | null;
  recordedPaceOrdering: string[];
  lanes: Record<
    string,
    { ageSeconds: number; recordedState: string; recordedSlotFactor: number; accounts: ReplayAccount[] }
  >;
}

const read = <T>(name: string) =>
  JSON.parse(readFileSync(new URL(`./fixtures/withdrawal-replay/${name}`, import.meta.url), "utf8")) as T;
const metaFixture = read<{ records: MetaRecord[] }>("t1-records.json");
const otherFixture = read<{ records: OtherLanesRecord[] }>("t1-other-lanes.json");

const META = "cliproxy-meta";
const CLAUDE = "cliproxy-claude";
const CODEX = "cliproxy-codex";
const ZAI = "cliproxy-zai";
const SOL = "gpt-6.1-sol";
const SONNET = "claude-sonnet-5-5";
const GLM = "glm-5.3";
const isMuse = (modelId: string) => modelId.startsWith("muse-spark");

const records = metaFixture.records.map((meta) => {
  const others = otherFixture.records.find((entry) => entry.ts === meta.ts);
  if (!others) throw new Error(`no companion lane record for ${meta.ts}`);
  return { meta, others };
});
type Replayed = (typeof records)[number];

const withdrawing = records.filter(({ meta }) => !isMuse(meta.bridge.model));
const hasCandidate = ({ meta }: Replayed, modelId: string) => meta.candidates.some((entry) => entry.model === modelId);

function observationFor(
  laneId: string,
  ts: string,
  ageSeconds: number,
  accounts: readonly ReplayAccount[],
): LanePaceObservation {
  return {
    laneId,
    free: false,
    observedAt: new Date(Date.parse(ts) - ageSeconds * 1000).toISOString(),
    staleAfterSeconds: 900,
    error: null,
    accounts: accounts.map((entry) => ({
      accountKey: entry.accountKey,
      health: entry.health,
      weight: entry.weight,
      weightSource: "reported" as const,
      governingWindow: entry.governingWindow,
      windows: [
        {
          name: entry.governingWindow,
          role: "allowance" as const,
          utilization: entry.utilization,
          resetsAt: entry.governingResetAt,
          windowSeconds: 7 * 24 * 60 * 60,
          allowanceWeight: entry.weight,
          allowanceWeightSource: "reported" as const,
          sourcePath: null,
        },
        // The snapshot carries only the governing window. An account it records
        // as unserviceable while its health still reads healthy or degraded was
        // tripped by a window the snapshot does not carry (a five-hour window at
        // the trip ceiling), so one is stood in for the engine to reach the same
        // verdict. The one such account in the cut is a Claude account.
        ...(!entry.serviceable && entry.health !== "exhausted" && entry.health !== "unavailable"
          ? [
              {
                name: "five-hour",
                role: "serviceability" as const,
                utilization: 1,
                resetsAt: new Date(Date.parse(ts) + 60 * 60 * 1000).toISOString(),
                windowSeconds: 5 * 60 * 60,
                sourcePath: null,
              },
            ]
          : []),
      ],
    })),
  };
}

/**
 * The live lane margin is not in the repo. Any margin in [0.121, 0.174)
 * reproduces all 66 recorded lane labels (the engine's 0.1 default mislabels
 * Codex at deviation 0.108-0.121 as `ahead`, and 0.175 would call Claude at
 * 0.174 `on`); 0.15 is used. The lane deviation itself is margin-free and
 * matched the recorded `paceDeviation` on every record at every margin tried.
 * Meta keeps the default, as in the case_ec9827b5 replay.
 */
const NON_META_MARGIN = 0.15;

function verdictFor(laneId: string, ts: string, ageSeconds: number, accounts: readonly ReplayAccount[]) {
  const observation = observationFor(laneId, ts, ageSeconds, accounts);
  const policy = laneId === META ? undefined : { margin: NON_META_MARGIN };
  return { observation, verdict: evaluateLanePace({ observation, asOf: ts, ...(policy ? { policy } : {}) }) };
}

function ledgerFor({ meta, others }: Replayed, lanes: readonly string[] = [META, ...Object.keys(others.lanes)]): LaneLedger {
  let ledger: LaneLedger = {};
  for (const laneId of lanes) {
    const source = laneId === META ? { ageSeconds: meta.ageSeconds, accounts: meta.metaAccounts } : others.lanes[laneId];
    if (!source) continue;
    const { observation, verdict } = verdictFor(laneId, meta.ts, source.ageSeconds, source.accounts);
    ledger = mergeLedgerEntry(ledger, { laneId, fetchedAt: meta.ts, verdict, observation, error: null });
  }
  return ledger;
}

function rosterFor({ meta }: Replayed): ModelEntry[] {
  const template = MODELS.find((entry) => entry.tier === "T1")!;
  return meta.candidates.map((entry) => ({
    ...template,
    id: entry.model,
    tier: entry.tier,
    laneId: entry.lane,
    costPerMTokIn: entry.blended,
    costPerMTokOut: entry.blended,
    costPerMTokCacheRead: entry.blended * 0.1,
    releasedAt: entry.model.startsWith("muse-spark-1.3") ? "2026-09-01" : "2026-01-01",
  }));
}

function decide(
  replayed: Replayed,
  options: { withdraw?: boolean; lanes?: readonly string[]; issueId?: string } = {},
) {
  const { meta } = replayed;
  const avoid: LaneAvoidConfig = {
    defaultThreshold: 0.8,
    perLane: {},
    ...(options.withdraw ? { withdrawAt: { [META]: meta.bridgeCeiling } } : {}),
  };
  return selectModel({
    profiles: PROFILES.map((profile) => ({
      ...profile,
      computedAt: new Date(Date.parse(meta.ts) - 60 * 60 * 1000).toISOString(),
    })),
    signals: NO_ESCALATION,
    now: Date.parse(meta.ts),
    descriptor: { issueId: options.issueId ?? `replay-${meta.ts}`, labelNames: ["tier:T1"] },
    config: config({
      models: rosterFor(replayed),
      pacingMode: "enforce",
      laneLedger: ledgerFor(replayed, options.lanes),
      laneAvoidConfig: avoid,
      stickyWithinIssue: false,
    }),
  });
}

const paceOrderOf = (trace: readonly string[]): string[] | null => {
  const line = trace.find((entry) => entry.startsWith("pace ordering"));
  return line?.includes(" reorders to ") ? line.split(" reorders to ")[1]!.split(" > ") : null;
};

const label = ({ meta }: Replayed) => `${meta.ts} ${meta.regime}`;

describe("case_bd95ab6f full-lane replay of recorded T1 decisions", () => {
  it("pairs every case_ec9827b5 record with its companion lanes, and spans both withdrawal regimes", () => {
    expect(records.length).toBe(23);
    const regimes = new Set(withdrawing.map(({ meta }) => meta.regime));
    expect([...regimes].sort()).toEqual(["claude_5h_below_98", "muse_collective_95"]);
    for (const { others } of records) expect(Object.keys(others.lanes)).toContain(CODEX);
  });

  it("rebuilds each lane's recorded read through the real pace engine: serviceability, lane label and slot factor", () => {
    // The shadow stream labels a lane `exhausted`, `unavailable`, `degraded` (ahead) or `available`.
    const labelOf = (verdict: { state: string; serviceable: boolean | null }) =>
      verdict.state === "exhausted"
        ? "exhausted"
        : verdict.serviceable === false
          ? "unavailable"
          : verdict.state === "ahead"
            ? "degraded"
            : verdict.serviceable === true
              ? "available"
              : "unavailable";
    for (const replayed of records) {
      for (const [laneId, lane] of Object.entries(replayed.others.lanes)) {
        const { verdict } = verdictFor(laneId, replayed.meta.ts, lane.ageSeconds, lane.accounts);
        const where = `${label(replayed)} ${laneId}`;
        expect(
          verdict.accounts.map((entry) => [entry.accountKey, entry.serviceable]),
          where,
        ).toEqual(lane.accounts.map((entry) => [entry.accountKey, entry.serviceable]));
        expect(labelOf(verdict), where).toBe(lane.recordedState);
        expect(verdict.state === "ahead" ? 0.25 : 1, where).toBe(lane.recordedSlotFactor);
      }
    }
  });

  it("baseline: reproduces the pace ordering and the pick the selector recorded when it decided, on 23/23", () => {
    for (const replayed of records) {
      const decision = decide(replayed);
      expect(paceOrderOf(decision.trace), label(replayed)).toEqual(replayed.others.recordedPaceOrdering);
      expect(decision.modelId, label(replayed)).toBe(replayed.meta.selectorPick);
    }
  });

  it("with only the Meta lane ledgered the engine falls to cost order and picks sol: the thin-ledger artifact", () => {
    // This is the case_ec9827b5 replay's own configuration. Reproduced here so the
    // difference between the two readings is a measured one.
    const claude5h = withdrawing.filter(({ meta }) => meta.regime === "claude_5h_below_98");
    expect(claude5h.length).toBeGreaterThan(0);
    for (const replayed of claude5h) {
      const thin = decide(replayed, { withdraw: true, lanes: [META] });
      expect(thin.modelId, label(replayed)).toBe(SOL);
    }
  });

  it("with every lane ledgered and MUSE withdrawn, the pick is the head of the pace order (in this cut never sol while Claude or Z.ai can serve)", () => {
    expect(withdrawing.length).toBeGreaterThan(0);
    for (const replayed of withdrawing) {
      const decision = decide(replayed, { withdraw: true });
      // No reorder line means pace agreed with cost order: a single survivor.
      const head =
        paceOrderOf(decision.trace)?.[0] ??
        [...replayed.meta.candidates].filter((entry) => !isMuse(entry.model)).sort((a, b) => a.blended - b.blended)[0]!.model;
      expect(decision.modelId, label(replayed)).toBe(head);
      if (hasCandidate(replayed, SONNET) || hasCandidate(replayed, GLM)) {
        expect(decision.modelId, label(replayed)).not.toBe(SOL);
      }
    }
  });

  it("what decides is the pace rule: Z.ai (behind pace) first, then Claude over Codex on deviation, sol last", () => {
    for (const replayed of withdrawing) {
      const expected = hasCandidate(replayed, GLM) ? GLM : hasCandidate(replayed, SONNET) ? SONNET : SOL;
      expect(decide(replayed, { withdraw: true }).modelId, label(replayed)).toBe(expected);
    }
  });

  it("claude_5h_below_98: every disagreement with the bridge is a Z.ai pick, and the records without a Z.ai row agree", () => {
    const claude5h = withdrawing.filter(({ meta }) => meta.regime === "claude_5h_below_98");
    const withGlm = claude5h.filter((replayed) => hasCandidate(replayed, GLM));
    const withoutGlm = claude5h.filter((replayed) => !hasCandidate(replayed, GLM));
    expect(withGlm.length).toBeGreaterThan(0);
    expect(withoutGlm.length).toBeGreaterThan(0);
    for (const replayed of withGlm) expect(decide(replayed, { withdraw: true }).modelId, label(replayed)).toBe(GLM);
    for (const replayed of withoutGlm) {
      expect(decide(replayed, { withdraw: true }).modelId, label(replayed)).toBe(replayed.meta.bridge.model);
    }
  });

  it("the Z.ai pick is a gas-pedal pick: its lane trails pace, and that is the only lane that does", () => {
    for (const replayed of withdrawing.filter((entry) => hasCandidate(entry, GLM))) {
      const states = Object.fromEntries(
        Object.entries(replayed.others.lanes).map(([laneId, lane]) => [
          laneId,
          verdictFor(laneId, replayed.meta.ts, lane.ageSeconds, lane.accounts).verdict.state,
        ]),
      );
      expect(states[ZAI], label(replayed)).toBe("behind");
      for (const laneId of [CLAUDE, CODEX]) {
        if (states[laneId]) expect(states[laneId], `${label(replayed)} ${laneId}`).not.toBe("behind");
      }
    }
  });

  it("sonnet over sol is a deviation outcome, not a Claude preference: it flips when Claude is the more-ahead lane", () => {
    // 22:01Z, a long-run engineer (no Z.ai row): Claude lane deviation about
    // 0.38 against Codex's 0.40 (0.382 and 0.399 at 21:56Z), so sonnet leads. Raise Claude's utilization until its lane
    // deviation passes Codex's, as the live stream did from 22:27Z (0.428
    // against 0.401), and pace puts sol first. The bridge would still say sonnet.
    const replayed = withdrawing.find(({ meta }) => meta.ts.startsWith("2026-10-04T22:01:59"))!;
    expect(hasCandidate(replayed, GLM)).toBe(false);
    expect(decide(replayed, { withdraw: true }).modelId).toBe(SONNET);
    const claudeLane = replayed.others.lanes[CLAUDE]!;
    const hotter: Replayed = {
      ...replayed,
      others: {
        ...replayed.others,
        lanes: {
          ...replayed.others.lanes,
          [CLAUDE]: {
            ...claudeLane,
            accounts: claudeLane.accounts.map((entry) => ({ ...entry, utilization: Math.min(0.85, entry.utilization + 0.1) })),
          },
        },
      },
    };
    expect(decide(hotter, { withdraw: true }).modelId).toBe(SOL);
  });

  it("two lanes that are both ahead share one throttle coin: the slot throttle cannot split load between them", () => {
    // Claude and Codex are both `ahead` (slot factor 0.25) in the 14:44Z record.
    // The throttle compares the same per-issue hash with the same factor for
    // both, so an issue either clears both or neither; when neither clears the
    // walk takes index 0. The first lane in pace order therefore wins every
    // issue, whatever its id.
    const replayed = withdrawing.find(({ meta }) => meta.ts.startsWith("2026-10-04T14:44:19"))!;
    expect(replayed).toBeDefined();
    const picks = new Set<string | null>();
    for (let index = 0; index < 200; index += 1) {
      picks.add(decide(replayed, { withdraw: true, issueId: `throttle-coin-${index}` }).modelId);
    }
    expect([...picks]).toEqual([SONNET]);
  });
});
