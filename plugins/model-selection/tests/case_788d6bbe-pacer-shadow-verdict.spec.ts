import { describe, expect, it } from "vitest";

import { snapshotJointCapacity } from "../src/joint-capacity-snapshot.js";
import {
  decidePacerShadowVerdict,
  MAX_SHADOW_JOBS_REQUESTED,
  PACER_SHADOW_VERDICT_SCHEMA,
} from "../src/pacer-shadow-verdict.js";
import { baseInput, NOW_MS, runner } from "./case_5bc69b17-joint-capacity-fixtures.js";

/**
 * case_788d6bbe: pacer shadow verdict consumes joint snapshot pressure (pure).
 *
 * Parent case_7193b2e0 under epic case_9e33a5c2 (pacer dependency for the case_24119081
 * joint controller). Shadow allow/deny only: no enforcement change, no cap
 * change, no live mutation.
 *
 * NON-GOALS (owned elsewhere): case_3b799020 audit log; case_9a475434 hysteresis;
 * case_adc03405 enforce path.
 */

describe("case_788d6bbe pacer shadow verdict", () => {
  it("defers a GARM-eligible admission on a reviewer bottleneck", () => {
    const snap = snapshotJointCapacity(
      baseInput({
        queued: { garmEligible: 1, publicFree: 0 },
        reviewers: { availableReviewers: 1, availableFixers: 2, pendingReviews: 5, pendingFixes: 0 },
      }),
    );
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 });
    expect(verdict.schema).toBe(PACER_SHADOW_VERDICT_SCHEMA);
    expect(verdict.mode).toBe("shadow-only");
    expect(verdict.verdict).toBe("deny");
    expect(verdict.reason).toContain("reviewer-bottleneck-preserved");
    expect(verdict.reasons).toContain("reviewer-bottleneck-preserved");
    expect(verdict.governsHostStarts).toBe(false);
    expect(verdict.mutatesPoolCaps).toBe(false);
    expect(verdict.claimsReservations).toBe(false);
  });

  it("defers a GARM-eligible admission on a fixer bottleneck alone", () => {
    const snap = snapshotJointCapacity(
      baseInput({
        queued: { garmEligible: 1, publicFree: 0 },
        reviewers: { availableReviewers: 5, availableFixers: 1, pendingReviews: 0, pendingFixes: 4 },
      }),
    );
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 });
    expect(verdict.verdict).toBe("deny");
    expect(verdict.reason).toContain("fixer-bottleneck-preserved");
  });

  it("denies when quota-exhausted runners leave no free eligible capacity", () => {
    const snap = snapshotJointCapacity(
      baseInput({
        runners: [
          runner("runner-1", { quotaRemaining: 0 }),
          runner("runner-2", { quotaRemaining: 0 }),
          runner("runner-3", { quotaRemaining: 0 }),
        ],
        queued: { garmEligible: 1, publicFree: 0 },
      }),
    );
    expect(snap.eligibleCapacity.freeEligible).toBe(0);
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 });
    expect(verdict.verdict).toBe("deny");
    expect(verdict.reason).toBe("no-free-eligible-capacity");
    expect(verdict.reasons.join(" ")).toContain("quota-exhausted");
  });

  it("ignores free-public-queue demand: public-free requests never consume GARM pressure", () => {
    const snap = snapshotJointCapacity(
      baseInput({ queued: { garmEligible: 0, publicFree: 100 } }),
    );
    expect(snap.pressure).toBe(0);
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "public-free", jobsRequested: 100 });
    expect(verdict.verdict).toBe("allow");
    expect(verdict.reason).toBe("public-free-excluded-from-garm-pressure");
    expect(verdict.pressureObserved).toBe(0);
  });

  it("allows a GARM-eligible admission when capacity is free and reviewers are healthy", () => {
    const snap = snapshotJointCapacity(
      baseInput({ queued: { garmEligible: 1, publicFree: 50 } }),
    );
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 });
    expect(verdict.verdict).toBe("allow");
    expect(verdict.reason).toBe("garm-capacity-available-shadow-only");
    expect(snap.eligibleCapacity.freeEligible).toBeGreaterThan(0);
    expect(verdict.freeEligibleObserved).toBe(snap.eligibleCapacity.freeEligible);
  });

  it("denies when projected eligible demand exceeds free eligible slots", () => {
    const snap = snapshotJointCapacity(
      baseInput({ queued: { garmEligible: 3, publicFree: 0 } }),
    );
    // 3 free eligible runners, 3 queued + 1 requested = 4 > 3.
    const verdict = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 });
    expect(verdict.verdict).toBe("deny");
    expect(verdict.reason).toBe("projected-garm-demand-exceeds-free-eligible-capacity");
  });

  it("rejects malformed requests without touching enforcement", () => {
    const snap = snapshotJointCapacity(baseInput());
    expect(() =>
      decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 0 }),
    ).toThrow("invalid-shadow-verdict-jobs-requested");
    expect(snap.evaluatedAt).toBe(NOW_MS);
  });
});

describe("case_788d6bbe pacer shadow verdict: fail-closed inputs and boundaries", () => {
  const healthy = () => snapshotJointCapacity(baseInput({ queued: { garmEligible: 1, publicFree: 0 } }));
  const garm = { queueClass: "garm-eligible", jobsRequested: 1 } as const;

  it.each([
    ["null snapshot", () => null, "invalid-shadow-verdict-snapshot"],
    ["non snapshot-only mode", () => ({ ...healthy(), mode: "live" }), "verdict-requires-snapshot-only-input"],
    ["NaN clock", () => ({ ...healthy(), evaluatedAt: Number.NaN }), "invalid-shadow-verdict-clock"],
    [
      "negative free capacity",
      () => ({ ...healthy(), eligibleCapacity: { ...healthy().eligibleCapacity, freeEligible: -1 } }),
      "invalid-shadow-verdict-capacity",
    ],
    ["missing queued", () => ({ ...healthy(), queued: undefined }), "invalid-shadow-verdict-queued"],
    [
      "non-boolean bottleneck flag",
      () => ({ ...healthy(), reviewerFixer: { reviewerBottleneck: "false", fixerBottleneck: false, preserved: true } }),
      "invalid-shadow-verdict-reviewer-fixer",
    ],
    ["NaN pressure", () => ({ ...healthy(), pressure: Number.NaN }), "invalid-shadow-verdict-pressure"],
    ["negative clock", () => ({ ...healthy(), evaluatedAt: -1 }), "invalid-shadow-verdict-clock"],
    [
      "fractional free capacity",
      () => ({ ...healthy(), eligibleCapacity: { ...healthy().eligibleCapacity, freeEligible: 1.5 } }),
      "invalid-shadow-verdict-capacity",
    ],
    [
      "NaN garmEligible (would project NaN and allow)",
      () => ({ ...healthy(), queued: { ...healthy().queued, garmEligible: Number.NaN } }),
      "invalid-shadow-verdict-queued",
    ],
    [
      "negative publicFree",
      () => ({ ...healthy(), queued: { ...healthy().queued, publicFree: -1 } }),
      "invalid-shadow-verdict-queued",
    ],
    [
      "missing fixer flag (would read as not bottlenecked and allow)",
      () => ({ ...healthy(), reviewerFixer: { reviewerBottleneck: false, preserved: true } }),
      "invalid-shadow-verdict-reviewer-fixer",
    ],
  ])("throws on a malformed snapshot: %s", (_name, build, code) => {
    for (const request of [garm, { queueClass: "public-free", jobsRequested: 1 } as const]) {
      expect(() => decidePacerShadowVerdict(build() as never, request)).toThrow(code);
    }
  });

  it.each([
    ["unknown queue class", { queueClass: "other", jobsRequested: 1 }, "invalid-shadow-verdict-queue-class"],
    ["fractional jobs", { queueClass: "garm-eligible", jobsRequested: 1.5 }, "invalid-shadow-verdict-jobs-requested"],
    ["string jobs", { queueClass: "garm-eligible", jobsRequested: "1" }, "invalid-shadow-verdict-jobs-requested"],
    [
      "jobs above the cap",
      { queueClass: "garm-eligible", jobsRequested: MAX_SHADOW_JOBS_REQUESTED + 1 },
      "invalid-shadow-verdict-jobs-requested",
    ],
    ["null request", null, "invalid-shadow-verdict-request"],
  ])("throws on a malformed request: %s", (_name, request, code) => {
    expect(() => decidePacerShadowVerdict(healthy(), request as never)).toThrow(code);
  });

  it("allows a request exactly at the free-slot boundary and denies one past it", () => {
    const snap = snapshotJointCapacity(baseInput({ queued: { garmEligible: 2, publicFree: 0 } }));
    expect(snap.eligibleCapacity.freeEligible).toBe(3);
    expect(decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 1 }).verdict).toBe("allow");
    const over = decidePacerShadowVerdict(snap, { queueClass: "garm-eligible", jobsRequested: 2 });
    expect(over.verdict).toBe("deny");
    expect(over.reason).toBe("projected-garm-demand-exceeds-free-eligible-capacity");
  });

  it("always allows public-free, even on the worst snapshot (no free capacity, both roles bottlenecked)", () => {
    const worst = snapshotJointCapacity(
      baseInput({
        runners: [],
        queued: { garmEligible: 9, publicFree: 50 },
        reviewers: { availableReviewers: 0, availableFixers: 0, pendingReviews: 3, pendingFixes: 3 },
      }),
    );
    expect(worst.reviewerFixer.reviewerBottleneck && worst.reviewerFixer.fixerBottleneck).toBe(true);
    expect(worst.eligibleCapacity.freeEligible).toBe(0);
    const verdict = decidePacerShadowVerdict(worst, { queueClass: "public-free", jobsRequested: 50 });
    expect(verdict.verdict).toBe("allow");
    expect(verdict.pressureObserved).toBe(worst.pressure);
    expect(verdict.freeEligibleObserved).toBe(worst.eligibleCapacity.freeEligible);
    expect(verdict.evaluatedAt).toBe(worst.evaluatedAt);
    expect(verdict.productionEvidenceCertified).toBe(false);
  });

  it("reports both bottlenecks when reviewer and fixer are both short", () => {
    const snap = snapshotJointCapacity(
      baseInput({
        queued: { garmEligible: 1, publicFree: 0 },
        reviewers: { availableReviewers: 1, availableFixers: 1, pendingReviews: 5, pendingFixes: 5 },
      }),
    );
    const verdict = decidePacerShadowVerdict(snap, garm);
    expect(verdict.reasons).toEqual(
      expect.arrayContaining(["reviewer-bottleneck-preserved", "fixer-bottleneck-preserved"]),
    );
  });

  it("notes an active cooldown on every verdict without changing the decision", () => {
    const snap = snapshotJointCapacity(
      baseInput({
        queued: { garmEligible: 1, publicFree: 0 },
        hysteresis: { scaleUpPressure: 1.5, scaleDownPressure: 0.5, cooldownMs: 900_000, lastScaleAt: NOW_MS - 1 },
      }),
    );
    expect(snap.hysteresis.cooldownActive).toBe(true);
    const verdict = decidePacerShadowVerdict(snap, garm);
    expect(verdict.verdict).toBe("allow");
    expect(verdict.reasons).toContain("hysteresis-cooldown-noted-shadow-only");
  });

  const COOLDOWN = { scaleUpPressure: 1.5, scaleDownPressure: 0.5, cooldownMs: 900_000, lastScaleAt: NOW_MS - 1 };
  const cooling = (over: Parameters<typeof baseInput>[0]) =>
    snapshotJointCapacity(baseInput({ hysteresis: COOLDOWN, ...over }));

  it.each([
    [
      "public-free allow",
      cooling({ queued: { garmEligible: 1, publicFree: 4 } }),
      { queueClass: "public-free", jobsRequested: 1 } as const,
      "allow",
    ],
    [
      "reviewer bottleneck deny",
      cooling({
        queued: { garmEligible: 1, publicFree: 0 },
        reviewers: { availableReviewers: 0, availableFixers: 5, pendingReviews: 3, pendingFixes: 0 },
      }),
      garm,
      "deny",
    ],
    [
      "fixer bottleneck deny",
      cooling({
        queued: { garmEligible: 1, publicFree: 0 },
        reviewers: { availableReviewers: 5, availableFixers: 0, pendingReviews: 0, pendingFixes: 3 },
      }),
      garm,
      "deny",
    ],
    [
      "no free capacity deny",
      cooling({ runners: [], queued: { garmEligible: 1, publicFree: 0 } }),
      garm,
      "deny",
    ],
    [
      "projected demand deny",
      cooling({ queued: { garmEligible: 3, publicFree: 0 } }),
      garm,
      "deny",
    ],
  ])("notes an active cooldown on the %s path", (_name, snap, request, expected) => {
    expect(snap.hysteresis.cooldownActive).toBe(true);
    const verdict = decidePacerShadowVerdict(snap, request);
    expect(verdict.verdict).toBe(expected);
    expect(verdict.reasons).toContain("hysteresis-cooldown-noted-shadow-only");
  });
});
