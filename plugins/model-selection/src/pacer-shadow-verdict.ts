/**
 * case_788d6bbe: pacer shadow verdict consumes joint snapshot pressure (pure).
 *
 * Parent case_7193b2e0 under epic case_9e33a5c2 (pacer dependency for the case_24119081
 * joint controller). Pure function over a case_5bc69b17-shaped joint capacity
 * snapshot plus one admission request, returning a shadow allow/deny with
 * reason. No enforcement change, no cap change, no live mutation, no
 * credentials.
 *
 * WHAT IT IS:
 * - A point-in-time shadow verdict: given the already-computed joint
 *   snapshot (eligible GARM capacity vs queued demand vs reviewer/fixer
 *   capacity) and one admission request, say whether the pacer *would* admit
 *   it, and why. Caller supplies both inputs; this module never fetches,
 *   never resolves secrets, never writes ledgers, never changes pool caps,
 *   never routes, never starts or kills anything.
 *
 * WHAT IT PROVES:
 * - Reviewer/fixer bottleneck defers: either role bottlenecked denies a
 *   GARM-eligible admission even when runner slots are free.
 * - Quota-exhausted runners are ineligible: the snapshot already counts
 *   them out of free capacity, so a verdict over that snapshot denies when
 *   nothing free remains (same for host-budget-exhausted and
 *   reset-crossed runners).
 * - Free-public-queue demand is ignored: a public-free request never
 *   consumes GARM pressure and is always allowed by this verdict; GARM
 *   pressure counts eligible-queued jobs only.
 *
 * NON-GOALS (owned elsewhere, do NOT duplicate):
 * - case_3b799020 admission audit log (in_review)
 * - case_9a475434 hysteresis tests (blocked)
 * - case_adc03405 enforce path (blocked)
 * - case_5bc69b17 snapshot assembly itself (consumed here, never rebuilt)
 *
 * Decision basis: the verdict does not read `snapshot.pressure`. It echoes it
 * as `pressureObserved` and decides on `queued.garmEligible + jobsRequested >
 * eligibleCapacity.freeEligible`, which is the same test as projected
 * pressure > 1 for this request.
 *
 * Throughput note (case_24119081): the joint controller optimizes completed
 * merges / lead time, not raw run count. This verdict reports a shadow
 * admission, never a merge rate; raw run count is never the objective.
 */

import type { JointCapacitySnapshot } from "./joint-capacity-snapshot.js";

export const PACER_SHADOW_VERDICT_SCHEMA = "case_5c3599fd-pacer-shadow-verdict-v1";

export const MAX_SHADOW_JOBS_REQUESTED = 1024;

export type PacerShadowQueueClass = "garm-eligible" | "public-free";

/** One admission request evaluated against the snapshot, nothing more. */
export interface PacerShadowAdmissionRequest {
  queueClass: PacerShadowQueueClass;
  /** Jobs this admission would place; must be a safe integer >= 1. */
  jobsRequested: number;
}

export interface PacerShadowVerdict {
  schema: typeof PACER_SHADOW_VERDICT_SCHEMA;
  mode: "shadow-only";
  governsHostStarts: false;
  claimsReservations: false;
  mutatesPoolCaps: false;
  /** Input provenance is caller-declared; this verdict does not certify it. */
  productionEvidenceCertified: false;
  evaluatedAt: number;
  verdict: "allow" | "deny";
  reason: string;
  reasons: string[];
  /** Snapshot pressure at verdict time (GARM-eligible only; public excluded). */
  pressureObserved: number;
  freeEligibleObserved: number;
  rollbackNotes: string[];
  limitations: string[];
}

const finite = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);
const clock = (n: unknown): n is number => finite(n) && (n as number) >= 0;
const count = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0;

function fail(reason: string): never {
  throw new Error(reason);
}

function validateSnapshot(snapshot: JointCapacitySnapshot): void {
  if (!snapshot || typeof snapshot !== "object") fail("invalid-shadow-verdict-snapshot");
  if (snapshot.mode !== "snapshot-only") fail("verdict-requires-snapshot-only-input");
  if (!clock(snapshot.evaluatedAt)) fail("invalid-shadow-verdict-clock");
  const cap = snapshot.eligibleCapacity;
  const queued = snapshot.queued;
  const rf = snapshot.reviewerFixer;
  if (!cap || !count(cap.freeEligible)) fail("invalid-shadow-verdict-capacity");
  if (!queued || !count(queued.garmEligible) || !count(queued.publicFree)) {
    fail("invalid-shadow-verdict-queued");
  }
  if (!rf || typeof rf.reviewerBottleneck !== "boolean" || typeof rf.fixerBottleneck !== "boolean") {
    fail("invalid-shadow-verdict-reviewer-fixer");
  }
  if (!finite(snapshot.pressure)) fail("invalid-shadow-verdict-pressure");
}

function validateRequest(request: PacerShadowAdmissionRequest): void {
  if (!request || typeof request !== "object") fail("invalid-shadow-verdict-request");
  if (request.queueClass !== "garm-eligible" && request.queueClass !== "public-free") {
    fail("invalid-shadow-verdict-queue-class");
  }
  if (
    !Number.isSafeInteger(request.jobsRequested) ||
    request.jobsRequested < 1 ||
    request.jobsRequested > MAX_SHADOW_JOBS_REQUESTED
  ) {
    fail("invalid-shadow-verdict-jobs-requested");
  }
}

function base(input: {
  snapshot: JointCapacitySnapshot;
  verdict: "allow" | "deny";
  reason: string;
  reasons: string[];
}): PacerShadowVerdict {
  return {
    schema: PACER_SHADOW_VERDICT_SCHEMA,
    mode: "shadow-only",
    governsHostStarts: false,
    claimsReservations: false,
    mutatesPoolCaps: false,
    productionEvidenceCertified: false,
    evaluatedAt: input.snapshot.evaluatedAt,
    verdict: input.verdict,
    reason: input.reason,
    reasons: input.reasons,
    pressureObserved: input.snapshot.pressure,
    freeEligibleObserved: input.snapshot.eligibleCapacity.freeEligible,
    rollbackNotes: [
      "Verdict mutates nothing: rollback is discarding this object.",
      "No pool cap change, no routing change, no reservation claim, no host start.",
      "A deny here defers nothing live; the enforce path (case_adc03405) moves separately.",
    ],
    limitations: [
      "Caller-declared snapshot and request are not certified production evidence.",
      "Shadow-only: this verdict never admits work and never governs host starts.",
      "Public-free demand is out of scope for GARM pressure by construction.",
    ],
  };
}

/**
 * Pure pacer shadow verdict. Never fetches, never resolves secrets, never
 * writes, never changes pool caps, never admits work, never starts hosts.
 */
export function decidePacerShadowVerdict(
  snapshot: JointCapacitySnapshot,
  request: PacerShadowAdmissionRequest,
): PacerShadowVerdict {
  validateSnapshot(snapshot);
  validateRequest(request);

  // Free public queue demand never consumes GARM pressure: a public-free
  // request is always allowed by this verdict and leaves the snapshot's
  // pressure untouched (the snapshot already excludes it).
  if (request.queueClass === "public-free") {
    return base({
      snapshot,
      verdict: "allow",
      reason: "public-free-excluded-from-garm-pressure",
      reasons: [
        "public-free-demand-ignored",
        "public-free-excluded-from-garm-pressure",
        ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
      ],
    });
  }

  // Reviewer/fixer capacity is preserved: either bottleneck defers the
  // admission even when runner slots are free.
  if (snapshot.reviewerFixer.reviewerBottleneck) {
    return base({
      snapshot,
      verdict: "deny",
      reason: "defer: reviewer-bottleneck-preserved",
      reasons: [
        "reviewer-bottleneck-preserved",
        ...(snapshot.reviewerFixer.fixerBottleneck ? ["fixer-bottleneck-preserved"] : []),
        ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
      ],
    });
  }
  if (snapshot.reviewerFixer.fixerBottleneck) {
    return base({
      snapshot,
      verdict: "deny",
      reason: "defer: fixer-bottleneck-preserved",
      reasons: [
        "fixer-bottleneck-preserved",
        ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
      ],
    });
  }

  // Quota-exhausted, host-budget-exhausted and reset-crossed runners are
  // already ineligible in the snapshot's free count: nothing free means deny.
  const free = snapshot.eligibleCapacity.freeEligible;
  if (free <= 0) {
    return base({
      snapshot,
      verdict: "deny",
      reason: "no-free-eligible-capacity",
      reasons: [
        "no-free-eligible-capacity",
        ...(snapshot.eligibleCapacity.ineligibilityReasons.length > 0
          ? [`ineligible-because:${snapshot.eligibleCapacity.ineligibilityReasons.join(",")}`]
          : []),
        ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
      ],
    });
  }

  // The admission consumes pressure: already-queued eligible jobs plus the
  // request must fit the free eligible slots, otherwise deny.
  const projected = snapshot.queued.garmEligible + request.jobsRequested;
  if (projected > free) {
    return base({
      snapshot,
      verdict: "deny",
      reason: "projected-garm-demand-exceeds-free-eligible-capacity",
      reasons: [
        "projected-garm-demand-exceeds-free-eligible-capacity",
        ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
      ],
    });
  }

  return base({
    snapshot,
    verdict: "allow",
    reason: "garm-capacity-available-shadow-only",
    reasons: [
      "garm-capacity-available-shadow-only",
      ...(snapshot.hysteresis.cooldownActive ? ["hysteresis-cooldown-noted-shadow-only"] : []),
    ],
  });
}
