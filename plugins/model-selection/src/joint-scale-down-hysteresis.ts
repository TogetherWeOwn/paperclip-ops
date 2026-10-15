/**
 * case_d58c5c9a: joint controller scale-down hysteresis — source-only decision spec.
 *
 * Parent case_ef1f7b4b (GARM/CI decomposition). Live slice case_c937b34a covers only
 * the desired-target safety guard and fixtures; the scale-down decision path
 * is unwritten. This module writes it without touching live state.
 *
 * WHAT IT IS:
 * - A pure scale-down decision for the joint agent/GARM controller
 *   (case_24119081 research). Caller supplies already-authorized observations;
 *   this module never fetches, never resolves secrets, never writes ledgers,
 *   never changes pool caps, never routes, never kills queued jobs.
 * - Three independent guards must ALL agree before scale-down:
 *   cooldown elapsed since the last controller action, continuous idle for
 *   at least minIdleMs, and point-in-time pressure at/below the low band.
 * - A rollback trigger fires when queued GARM-eligible depth rebounds to
 *   reboundQueueDepth after a scale-down, regardless of cooldown: the
 *   controller must scale back up / cancel the scale-down, never ride it out.
 *
 * WHAT IT PROVES (with tests/case_d58c5c9a-scale-down-hysteresis.spec.ts):
 * - Cooldown windows veto repeat scale-downs (no flapping under sawtooth).
 * - Min-idle thresholds veto fresh idles (a momentary dip never drains).
 * - Rollback triggers on queue rebound even inside cooldown.
 * - A synthetic sawtooth trace scales down exactly once, not repeatedly.
 *
 * NON-GOALS (owned elsewhere, do NOT duplicate):
 * - case_c937b34a desired-target safety guard (live slice; this slice consumes
 *   its pressure/idle vocabulary, never redefines pool caps).
 * - case_5bc69b17 joint capacity snapshot (point-in-time pressure source).
 * - Any live controller wiring, pool mutation, routing, or job kill.
 *
 * Throughput note (case_24119081): the joint controller optimizes completed
 * merges / lead time, not raw run count. This decision reports when idle
 * capacity may drain, not a merge rate; raw run count is never the objective.
 */

const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const clock = (n: unknown): n is number => finite(n) && n >= 0;
const count = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0;

function fail(reason: string): never {
  throw new Error(reason);
}

/** Tuning for the scale-down decision. All thresholds are caller-supplied. */
export interface ScaleDownHysteresisConfig {
  /** Pressure at/above which scale-up may be *considered* (never applied here). */
  scaleUpPressure: number;
  /** Pressure at/below which idle scale-down may be *considered*. Must be < up. */
  scaleDownPressure: number;
  /** Minimum ms between controller actions; the decision honors it, never waits. */
  cooldownMs: number;
  /** Minimum continuous idle ms before scale-down may be considered. */
  minIdleMs: number;
  /** Queued GARM-eligible depth at/above which rollback triggers after a scale-down. */
  reboundQueueDepth: number;
}

/** One point-in-time observation supplied by an authorized caller. */
export interface ScaleDownDecisionInput {
  now: number;
  /** Last controller scale-down epoch ms; null means none observed. */
  lastScaleAt: number | null;
  /**
   * When the current continuous idle started (epoch ms); null when not
   * currently idle or when the idle start is unobserved (fails closed).
   * Idle means queuedGarmEligible === 0 && busyEligible === 0.
   */
  idleSince: number | null;
  /** Jobs that can only run on GARM-eligible runners. */
  queuedGarmEligible: number;
  /** Eligible runners currently executing a job. */
  busyEligible: number;
  /** Eligible runners currently free. Must be > 0 to drain one. */
  freeEligible: number;
}

export interface ScaleDownDecision {
  evaluatedAt: number;
  /** GARM-eligible queued / max(1, freeEligible). */
  pressure: number;
  cooldownActive: boolean;
  idleSatisfied: boolean;
  scaleDownAllowed: boolean;
  /** True when queued depth rebounded after a scale-down: scale back up. */
  rollbackTriggered: boolean;
  reasons: string[];
}

/** One load-trace sample for the offline sawtooth proof. */
export interface ScaleDownTracePoint {
  now: number;
  queuedGarmEligible: number;
  busyEligible: number;
  freeEligible: number;
}

export interface ScaleDownTraceResult {
  decisions: ScaleDownDecision[];
  /** Points where scaleDownAllowed was true (controller would act). */
  scaleDownCount: number;
  scaleDownAt: number[];
  rollbackCount: number;
}

export const MAX_SCALE_DOWN_TRACE_POINTS = 1024;

function validateConfig(config: ScaleDownHysteresisConfig): void {
  if (!finite(config.scaleUpPressure) || config.scaleUpPressure <= 0) fail('invalid-scale-up-pressure');
  if (!finite(config.scaleDownPressure) || config.scaleDownPressure < 0) fail('invalid-scale-down-pressure');
  if (!(config.scaleDownPressure < config.scaleUpPressure)) fail('hysteresis-bands-must-separate');
  if (!count(config.cooldownMs)) fail('invalid-hysteresis-cooldown');
  if (!count(config.minIdleMs)) fail('invalid-min-idle');
  if (!count(config.reboundQueueDepth) || config.reboundQueueDepth < 1) fail('invalid-rebound-queue-depth');
}

function validateInput(input: ScaleDownDecisionInput): void {
  if (!clock(input.now)) fail('invalid-decision-clock');
  if (input.lastScaleAt !== null) {
    if (!clock(input.lastScaleAt)) fail('invalid-last-scale-at');
    if (input.lastScaleAt > input.now) fail('last-scale-at-in-future');
  }
  if (input.idleSince !== null) {
    if (!clock(input.idleSince)) fail('invalid-idle-since');
    if (input.idleSince > input.now) fail('idle-since-in-future');
  }
  if (!count(input.queuedGarmEligible)) fail('invalid-queued-demand');
  if (!count(input.busyEligible)) fail('invalid-busy-eligible');
  if (!count(input.freeEligible)) fail('invalid-free-eligible');
}

/**
 * Pure scale-down decision. Never fetches, never resolves secrets, never
 * writes, never changes pool caps, never routes, never kills queued jobs.
 */
export function decideScaleDown(
  input: ScaleDownDecisionInput,
  config: ScaleDownHysteresisConfig,
): ScaleDownDecision {
  validateConfig(config);
  validateInput(input);

  const pressure = input.queuedGarmEligible / Math.max(1, input.freeEligible);
  const cooldownActive =
    input.lastScaleAt !== null && input.now - input.lastScaleAt < config.cooldownMs;

  // Idle is a property of THIS point; idleSince proves it has lasted.
  const idleNow = input.queuedGarmEligible === 0 && input.busyEligible === 0;
  const idleSatisfied =
    idleNow && input.idleSince !== null && input.now - input.idleSince >= config.minIdleMs;

  // Rollback outranks everything except validation: after a scale-down, a
  // queue rebound means the drain was wrong — scale back up immediately,
  // even inside cooldown.
  const rollbackTriggered =
    input.lastScaleAt !== null && input.queuedGarmEligible >= config.reboundQueueDepth;

  const reasons: string[] = [];
  if (cooldownActive) reasons.push('hysteresis-cooldown-active');
  if (rollbackTriggered) reasons.push('scale-down-rollback-queue-rebound');
  if (!idleNow) reasons.push('not-idle');
  else if (input.idleSince === null) reasons.push('idle-since-unobserved');
  else if (!idleSatisfied) reasons.push('min-idle-not-satisfied');
  if (!idleNow && pressure > config.scaleDownPressure) reasons.push('above-scale-down-band');
  if (input.freeEligible === 0) reasons.push('no-free-eligible-capacity');

  // Note: idle implies zero queued, hence zero pressure, so the band
  // conjunct below is defense-in-depth (it can only fail if the idle
  // definition ever widens to allow low-but-nonzero queues). The live vetoes
  // are cooldown, rollback, min-idle, and free capacity.
  const scaleDownAllowed =
    !cooldownActive &&
    !rollbackTriggered &&
    idleSatisfied &&
    pressure <= config.scaleDownPressure &&
    input.freeEligible > 0;
  if (scaleDownAllowed) reasons.push('idle-scale-down-allowed');

  return {
    evaluatedAt: input.now,
    pressure,
    cooldownActive,
    idleSatisfied,
    scaleDownAllowed,
    rollbackTriggered,
    reasons,
  };
}

/**
 * Fold an offline load trace through the decision, tracking lastScaleAt and
 * continuous idle the way a controller would. Pure and bounded: at most
 * MAX_SCALE_DOWN_TRACE_POINTS points, non-decreasing clocks.
 *
 * When a point allows scale-down, the fold records the action (lastScaleAt =
 * point.now) so later points see its cooldown — this is what proves a
 * sawtooth trace drains once instead of flapping.
 */
export function evaluateScaleDownTrace(
  points: ScaleDownTracePoint[],
  config: ScaleDownHysteresisConfig,
  seedLastScaleAt: number | null = null,
): ScaleDownTraceResult {
  validateConfig(config);
  if (points.length > MAX_SCALE_DOWN_TRACE_POINTS) fail('too-many-trace-points');
  if (seedLastScaleAt !== null && !clock(seedLastScaleAt)) fail('invalid-last-scale-at');

  const decisions: ScaleDownDecision[] = [];
  const scaleDownAt: number[] = [];
  let rollbackCount = 0;
  let lastScaleAt: number | null = seedLastScaleAt;
  let idleSince: number | null = null;
  let lastT: number | null = null;

  for (const p of points) {
    if (!clock(p.now)) fail('invalid-decision-clock');
    if (lastT !== null && p.now < lastT) fail('trace-clock-went-backwards');
    lastT = p.now;
    if (!count(p.queuedGarmEligible)) fail('invalid-queued-demand');
    if (!count(p.busyEligible)) fail('invalid-busy-eligible');
    if (!count(p.freeEligible)) fail('invalid-free-eligible');

    const idleNow = p.queuedGarmEligible === 0 && p.busyEligible === 0;
    if (idleNow) {
      // A scale-down breaks the streak: idle observed before it is stale.
      if (idleSince === null || (lastScaleAt !== null && idleSince < lastScaleAt)) idleSince = p.now;
    } else {
      idleSince = null;
    }

    const decision = decideScaleDown(
      {
        now: p.now,
        lastScaleAt,
        idleSince,
        queuedGarmEligible: p.queuedGarmEligible,
        busyEligible: p.busyEligible,
        freeEligible: p.freeEligible,
      },
      config,
    );
    decisions.push(decision);
    if (decision.rollbackTriggered) rollbackCount += 1;
    if (decision.scaleDownAllowed) {
      lastScaleAt = p.now;
      scaleDownAt.push(p.now);
      idleSince = null;
    }
  }

  return { decisions, scaleDownCount: scaleDownAt.length, scaleDownAt, rollbackCount };
}
