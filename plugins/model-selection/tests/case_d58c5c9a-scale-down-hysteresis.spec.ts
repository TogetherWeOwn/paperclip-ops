import { describe, expect, it } from "vitest";

import {
  decideScaleDown,
  evaluateScaleDownTrace,
} from "../src/joint-scale-down-hysteresis.js";
import {
  COOLDOWN_MS,
  MIN_IDLE_MS,
  NOW_MS,
  STEP_MS,
  baseConfig,
  baseInput,
  sustainedIdleTrace,
  sawtoothTrace,
} from "./case_d58c5c9a-scale-down-hysteresis-fixtures.js";

/**
 * case_d58c5c9a: joint controller scale-down hysteresis — source-only decision.
 *
 * Parent case_ef1f7b4b; live slice case_c937b34a covers only the desired-target
 * safety guard. Pure decision only: no pool cap change, no routing change,
 * no queued-job kill.
 */

describe("case_d58c5c9a scale-down hysteresis", () => {
  it("allows scale-down for long-idle resources outside cooldown", () => {
    const decision = decideScaleDown(baseInput(), baseConfig());
    expect(decision.pressure).toBe(0);
    expect(decision.cooldownActive).toBe(false);
    expect(decision.idleSatisfied).toBe(true);
    expect(decision.rollbackTriggered).toBe(false);
    expect(decision.scaleDownAllowed).toBe(true);
    expect(decision.reasons).toContain("idle-scale-down-allowed");
  });

  it("cooldown vetoes an otherwise idle scale-down", () => {
    const decision = decideScaleDown(
      baseInput({ lastScaleAt: NOW_MS - 60_000 }),
      baseConfig(),
    );
    expect(decision.cooldownActive).toBe(true);
    expect(decision.idleSatisfied).toBe(true);
    expect(decision.scaleDownAllowed).toBe(false);
    expect(decision.reasons).toContain("hysteresis-cooldown-active");
  });

  it("cooldown ends exactly at cooldownMs", () => {
    const at = (age: number) =>
      decideScaleDown(
        baseInput({ now: NOW_MS, lastScaleAt: NOW_MS - age }),
        baseConfig(),
      );
    expect(at(COOLDOWN_MS - 1).cooldownActive).toBe(true);
    expect(at(COOLDOWN_MS - 1).scaleDownAllowed).toBe(false);
    expect(at(COOLDOWN_MS).cooldownActive).toBe(false);
    expect(at(COOLDOWN_MS).scaleDownAllowed).toBe(true);
  });

  it("min-idle blocks a fresh idle and passes at exactly minIdleMs", () => {
    const fresh = decideScaleDown(
      baseInput({ idleSince: NOW_MS - (MIN_IDLE_MS - 1) }),
      baseConfig(),
    );
    expect(fresh.idleSatisfied).toBe(false);
    expect(fresh.scaleDownAllowed).toBe(false);
    expect(fresh.reasons).toContain("min-idle-not-satisfied");

    const exact = decideScaleDown(
      baseInput({ idleSince: NOW_MS - MIN_IDLE_MS }),
      baseConfig(),
    );
    expect(exact.idleSatisfied).toBe(true);
    expect(exact.scaleDownAllowed).toBe(true);
  });

  it("fails closed when idle start is unobserved", () => {
    const decision = decideScaleDown(baseInput({ idleSince: null }), baseConfig());
    expect(decision.idleSatisfied).toBe(false);
    expect(decision.scaleDownAllowed).toBe(false);
    expect(decision.reasons).toContain("idle-since-unobserved");
  });

  it("busy runners and queued demand each veto scale-down", () => {
    const busy = decideScaleDown(baseInput({ busyEligible: 1 }), baseConfig());
    expect(busy.scaleDownAllowed).toBe(false);
    expect(busy.reasons).toContain("not-idle");

    const queued = decideScaleDown(
      baseInput({ queuedGarmEligible: 1, idleSince: null }),
      baseConfig(),
    );
    expect(queued.pressure).toBeCloseTo(1 / 3, 9);
    expect(queued.scaleDownAllowed).toBe(false);
    expect(queued.reasons).toContain("not-idle");
  });

  it("needs free eligible capacity to drain", () => {
    const decision = decideScaleDown(baseInput({ freeEligible: 0 }), baseConfig());
    expect(decision.scaleDownAllowed).toBe(false);
    expect(decision.reasons).toContain("no-free-eligible-capacity");
  });

  it("rollback triggers on queue rebound even inside cooldown", () => {
    const decision = decideScaleDown(
      baseInput({
        lastScaleAt: NOW_MS - 60_000,
        idleSince: null,
        queuedGarmEligible: 4,
        busyEligible: 0,
      }),
      baseConfig(),
    );
    expect(decision.cooldownActive).toBe(true);
    expect(decision.rollbackTriggered).toBe(true);
    expect(decision.scaleDownAllowed).toBe(false);
    expect(decision.reasons).toContain("scale-down-rollback-queue-rebound");
  });

  it("rollback alone vetoes scale-down outside cooldown", () => {
    const decision = decideScaleDown(
      baseInput({
        lastScaleAt: NOW_MS - COOLDOWN_MS - 1,
        idleSince: null,
        queuedGarmEligible: 3,
        busyEligible: 0,
      }),
      baseConfig(),
    );
    expect(decision.cooldownActive).toBe(false);
    expect(decision.rollbackTriggered).toBe(true);
    expect(decision.scaleDownAllowed).toBe(false);
    expect(decision.reasons).toContain("scale-down-rollback-queue-rebound");
  });

  it("no rollback without a prior scale-down or below the rebound depth", () => {
    const neverScaled = decideScaleDown(
      baseInput({ idleSince: null, queuedGarmEligible: 9, busyEligible: 1 }),
      baseConfig(),
    );
    expect(neverScaled.rollbackTriggered).toBe(false);

    const below = decideScaleDown(
      baseInput({
        lastScaleAt: NOW_MS - COOLDOWN_MS - 1,
        idleSince: null,
        queuedGarmEligible: 2,
        busyEligible: 0,
      }),
      baseConfig(),
    );
    expect(below.cooldownActive).toBe(false);
    expect(below.rollbackTriggered).toBe(false);
    expect(below.scaleDownAllowed).toBe(false);
  });

  it("rejects blended hysteresis bands that would flap", () => {
    expect(() =>
      decideScaleDown(baseInput(), baseConfig({ scaleDownPressure: 1.5, scaleUpPressure: 1.5 })),
    ).toThrow("hysteresis-bands-must-separate");
  });

  it("sawtooth trace scales down exactly once, then rolls back the spike", () => {
    const result = evaluateScaleDownTrace(sawtoothTrace(), baseConfig());
    expect(result.scaleDownCount).toBe(1);
    expect(result.scaleDownAt).toEqual([NOW_MS + 10 * STEP_MS]);
    expect(result.rollbackCount).toBe(1);
  });

  it("sustained idle drains once per cooldown window, never flaps", () => {
    // 20 min window: one drain at t+10; the next would need cooldown (t+25).
    const short = evaluateScaleDownTrace(sustainedIdleTrace(20), baseConfig());
    expect(short.scaleDownCount).toBe(1);
    expect(short.rollbackCount).toBe(0);
    // 40 min window: a second drain only after cooldown + fresh min-idle.
    const long = evaluateScaleDownTrace(sustainedIdleTrace(), baseConfig());
    expect(long.scaleDownCount).toBe(2);
    expect(long.scaleDownAt[1]! - long.scaleDownAt[0]!).toBeGreaterThanOrEqual(COOLDOWN_MS);
    expect(long.rollbackCount).toBe(0);
  });
});
