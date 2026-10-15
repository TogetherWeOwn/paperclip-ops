import type {
  ScaleDownDecisionInput,
  ScaleDownHysteresisConfig,
  ScaleDownTracePoint,
} from "../src/joint-scale-down-hysteresis.js";

/**
 * case_d58c5c9a fixtures: scale-down hysteresis decision inputs.
 *
 * Pure builders only; no live state, no secrets. Clocks are fixed so
 * cooldown/min-idle assertions are deterministic.
 */

export const NOW_MS = Date.parse("2026-10-05T00:00:00.000Z");
export const COOLDOWN_MS = 15 * 60 * 1000;
export const MIN_IDLE_MS = 10 * 60 * 1000;
export const STEP_MS = 60 * 1000;

export function baseConfig(overrides: Partial<ScaleDownHysteresisConfig> = {}): ScaleDownHysteresisConfig {
  return {
    scaleUpPressure: 1.5,
    scaleDownPressure: 0.5,
    cooldownMs: COOLDOWN_MS,
    minIdleMs: MIN_IDLE_MS,
    reboundQueueDepth: 3,
    ...overrides,
  };
}

export function baseInput(overrides: Partial<ScaleDownDecisionInput> = {}): ScaleDownDecisionInput {
  return {
    now: NOW_MS,
    lastScaleAt: null,
    idleSince: NOW_MS - MIN_IDLE_MS,
    queuedGarmEligible: 0,
    busyEligible: 0,
    freeEligible: 3,
    ...overrides,
  };
}

function idlePoint(now: number, freeEligible = 3): ScaleDownTracePoint {
  return { now, queuedGarmEligible: 0, busyEligible: 0, freeEligible };
}

function loadPoint(now: number, queuedGarmEligible: number, freeEligible = 3): ScaleDownTracePoint {
  return { now, queuedGarmEligible, busyEligible: 0, freeEligible };
}

/**
 * Synthetic sawtooth: long idle, sharp spike, short idle, sharp spike, long
 * idle — all inside one cooldown after the first drain. The controller must
 * scale down exactly once (the first long idle), never on the short dips or
 * the post-cooldown tail without a fresh full idle window.
 *
 * Layout at 1-minute steps from NOW_MS:
 * - t+0..t+11: idle (12 min >= 10 min min-idle) → scale-down fires at t+10.
 * - t+12: spike of 4 queued (rebound: rollback triggers, cooldown active).
 * - t+13..t+17: idle (5 min < min-idle) → no scale-down.
 * - t+18: spike of 2 queued (below rebound, above scale-down band).
 * - t+19..t+24: idle (6 min < min-idle) → no scale-down.
 */
export function sawtoothTrace(): ScaleDownTracePoint[] {
  const points: ScaleDownTracePoint[] = [];
  for (let m = 0; m <= 11; m += 1) points.push(idlePoint(NOW_MS + m * STEP_MS));
  points.push(loadPoint(NOW_MS + 12 * STEP_MS, 4));
  for (let m = 13; m <= 17; m += 1) points.push(idlePoint(NOW_MS + m * STEP_MS));
  points.push(loadPoint(NOW_MS + 18 * STEP_MS, 2));
  for (let m = 19; m <= 24; m += 1) points.push(idlePoint(NOW_MS + m * STEP_MS));
  return points;
}

/** Sustained idle far beyond cooldown + min-idle: drains once, then holds. */
export function sustainedIdleTrace(minutes = 40): ScaleDownTracePoint[] {
  const points: ScaleDownTracePoint[] = [];
  for (let m = 0; m < minutes; m += 1) points.push(idlePoint(NOW_MS + m * STEP_MS));
  return points;
}
