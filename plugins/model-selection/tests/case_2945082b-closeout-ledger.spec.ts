import { describe, expect, it } from "vitest";

import {
  CLOSEOUT_DISPOSITIONS,
  CLOSEOUT_LEDGER_SCHEMA,
  MAX_CLOSEOUT_RECORDS,
  appendCloseoutRecord,
  normalizeCloseoutLedger,
  validateCloseoutRecord,
  type CloseoutLedger,
  type CloseoutRecordInput,
} from "../src/closeout-ledger.js";

/**
 * case_2945082b: flag-gated closeout-ledger append verb — pure half.
 *
 * Parent case_f05a9638 (decompose leaf under epic case_cc33e13f). Fixture-driven:
 * flag on appends, flag off no-ops, malformed records rejected. No
 * ledger-schema migration, no backfill (backfill is the Director leaf
 * case_9498ba40).
 *
 * NON-GOALS (owned elsewhere): retry/blocker_attention/decision-log/
 * issue_thread_interaction verbs (case_a2213db5/case_f125613e/case_c554181e/case_54f5e53a);
 * ask_user_questions/request_confirmation/suggest_tasks parity
 * (case_565b8e7c/case_ad5042ce/QA parity).
 */

const NOW_ISO = "2026-10-04T05:00:00.000Z";

function validInput(overrides: Partial<Record<string, unknown>> = {}): CloseoutRecordInput {
  return {
    mergeSha: "be02c31",
    disposition: "merged",
    evidenceLink: "https://github.com/TogetherWeOwn/paperclip-ops/pull/523",
    ...overrides,
  };
}

function emptyLedger(): CloseoutLedger {
  return { schema: CLOSEOUT_LEDGER_SCHEMA, records: [] };
}

describe("case_2945082b closeout ledger: flag on appends", () => {
  it("appends a valid record and stamps the append time", () => {
    const { ledger, outcome } = appendCloseoutRecord(emptyLedger(), validInput(), true, NOW_ISO);
    expect(outcome).toMatchObject({
      appended: true,
      record: {
        mergeSha: "be02c31",
        disposition: "merged",
        evidenceLink: "https://github.com/TogetherWeOwn/paperclip-ops/pull/523",
        appendedAt: NOW_ISO,
      },
    });
    expect(ledger.schema).toBe(CLOSEOUT_LEDGER_SCHEMA);
    expect(ledger.records).toHaveLength(1);
    expect(ledger.records[0]).toMatchObject({ mergeSha: "be02c31", disposition: "merged" });
  });

  it("accepts every documented disposition and a full-length SHA", () => {
    for (const disposition of CLOSEOUT_DISPOSITIONS) {
      const { outcome } = appendCloseoutRecord(
        emptyLedger(),
        validInput({ mergeSha: "8bfc13614a2b9c1d4e5f60718293a4b5c6d7e8f90".slice(0, 40), disposition }),
        true,
        NOW_ISO,
      );
      expect(outcome, `disposition ${disposition}`).toMatchObject({ appended: true });
    }
    expect(CLOSEOUT_DISPOSITIONS).toEqual(["merged", "closed-superseded", "closed-abandoned", "decision-recorded"]);
  });

  it("grows the ledger across successive appends and caps at the bound", () => {
    let ledger = emptyLedger();
    for (let i = 0; i < 3; i++) {
      const next = appendCloseoutRecord(ledger, validInput({ mergeSha: `abc123${i}` }), true, NOW_ISO);
      expect(next.outcome.appended).toBe(true);
      ledger = next.ledger;
    }
    expect(ledger.records).toHaveLength(3);
    expect(ledger.records.map((r) => r.mergeSha)).toEqual(["abc1230", "abc1231", "abc1232"]);

    const full: CloseoutLedger = {
      schema: CLOSEOUT_LEDGER_SCHEMA,
      records: Array.from({ length: MAX_CLOSEOUT_RECORDS }, (_, i) => ({
        mergeSha: `000000${i % 10}`,
        disposition: "merged" as const,
        evidenceLink: "https://example.test/pr/1",
        appendedAt: NOW_ISO,
      })),
    };
    const capped = appendCloseoutRecord(full, validInput({ mergeSha: "ffffff0" }), true, NOW_ISO);
    expect(capped.outcome.appended).toBe(true);
    expect(capped.ledger.records).toHaveLength(MAX_CLOSEOUT_RECORDS);
    expect(capped.ledger.records.at(-1)!.mergeSha).toBe("ffffff0");
  });
});

describe("case_2945082b closeout ledger: flag off no-ops", () => {
  it("returns the ledger unchanged and reports disabled, writing nothing", () => {
    const before = emptyLedger();
    const { ledger, outcome } = appendCloseoutRecord(before, validInput(), false, NOW_ISO);
    expect(outcome).toEqual({ appended: false, reason: "closeout-ledger-disabled" });
    expect(ledger).toBe(before);
    expect(ledger.records).toHaveLength(0);
  });

  it("no-ops even for a malformed record — the flag is checked first", () => {
    const seeded: CloseoutLedger = {
      schema: CLOSEOUT_LEDGER_SCHEMA,
      records: [{ mergeSha: "be02c31", disposition: "merged", evidenceLink: "https://example.test/pr/1", appendedAt: NOW_ISO }],
    };
    const { ledger, outcome } = appendCloseoutRecord(
      seeded,
      validInput({ mergeSha: "not-a-sha!!!", disposition: "bogus", evidenceLink: "nope" }),
      false,
      NOW_ISO,
    );
    expect(outcome).toEqual({ appended: false, reason: "closeout-ledger-disabled" });
    expect(ledger).toBe(seeded);
  });
});

describe("case_2945082b closeout ledger: malformed records rejected", () => {
  it("rejects a bad SHA, an unknown disposition, and a non-link evidence value", () => {
    const cases: Array<[CloseoutRecordInput, string]> = [
      [validInput({ mergeSha: "xyz" }), "invalid-closeout-merge-sha"],
      [validInput({ mergeSha: "not a sha!!!" }), "invalid-closeout-merge-sha"],
      [validInput({ mergeSha: 42 }), "invalid-closeout-merge-sha"],
      [validInput({ disposition: "shipped-it" }), "invalid-closeout-disposition"],
      [validInput({ disposition: null }), "invalid-closeout-disposition"],
      [validInput({ evidenceLink: "just some words" }), "invalid-closeout-evidence-link"],
      [validInput({ evidenceLink: "" }), "invalid-closeout-evidence-link"],
      [validInput({ evidenceLink: 7 }), "invalid-closeout-evidence-link"],
    ];
    for (const [input, code] of cases) {
      expect(validateCloseoutRecord(input), JSON.stringify(input)).toContain(code);
      const before = emptyLedger();
      const { ledger, outcome } = appendCloseoutRecord(before, input, true, NOW_ISO);
      expect(outcome).toMatchObject({ appended: false, reason: "malformed-closeout-record" });
      expect(ledger).toBe(before);
    }
  });

  it("names every defect at once and never stores a partial record", () => {
    const issues = validateCloseoutRecord({ mergeSha: "bad", disposition: "bad", evidenceLink: "bad" });
    expect(issues).toContain("invalid-closeout-merge-sha");
    expect(issues).toContain("invalid-closeout-disposition");
    expect(issues).toContain("invalid-closeout-evidence-link");
    expect(validateCloseoutRecord(null as unknown as CloseoutRecordInput)).toContain(
      "malformed-closeout-record-not-an-object",
    );
    expect(validateCloseoutRecord([] as unknown as CloseoutRecordInput)).toContain(
      "malformed-closeout-record-not-an-object",
    );
  });

  it("refuses an unparseable clock instead of stamping a dishonest time", () => {
    expect(() => appendCloseoutRecord(emptyLedger(), validInput(), true, "not-a-time")).toThrow(
      "invalid-closeout-clock",
    );
  });
});

describe("case_2945082b closeout ledger: stored state normalizes, never throws", () => {
  it("normalizes null, non-objects, schema mismatches, and drops bad rows", () => {
    expect(normalizeCloseoutLedger(null)).toEqual({ schema: CLOSEOUT_LEDGER_SCHEMA, records: [] });
    expect(normalizeCloseoutLedger([])).toEqual({ schema: CLOSEOUT_LEDGER_SCHEMA, records: [] });
    expect(normalizeCloseoutLedger({ schema: "something-else-v9", records: [] })).toEqual({
      schema: CLOSEOUT_LEDGER_SCHEMA,
      records: [],
    });
    const mixed = normalizeCloseoutLedger({
      schema: CLOSEOUT_LEDGER_SCHEMA,
      records: [
        { mergeSha: "be02c31", disposition: "merged", evidenceLink: "https://example.test/pr/1", appendedAt: NOW_ISO },
        { mergeSha: "bad", disposition: "merged", evidenceLink: "https://example.test/pr/1", appendedAt: NOW_ISO },
        "junk",
        { mergeSha: "a1b2c3d", disposition: "merged", evidenceLink: "https://example.test/pr/2", appendedAt: "whenever" },
      ],
    });
    expect(mixed.records.map((r) => r.mergeSha)).toEqual(["be02c31"]);
  });
});
