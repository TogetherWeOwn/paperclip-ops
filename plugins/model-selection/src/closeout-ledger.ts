/**
 * case_2945082b: flag-gated closeout-ledger append verb — pure record validation
 * and ledger fold.
 *
 * Parent case_f05a9638 (decompose leaf under epic case_cc33e13f). Scope (<=4h): the
 * pure half of a new flag-gated plugin verb that appends a closeout record
 * (merge SHA / disposition / evidence link) to the closeout ledger. No
 * ledger-schema migration, no backfill (backfill is the Director leaf
 * case_9498ba40).
 *
 * WHAT IT IS:
 * - A validated closeout record: the merge SHA that landed the work, the
 *   disposition it landed with, and the evidence link proving it. Caller
 *   supplies the record; this module never fetches, never resolves secrets,
 *   never reads plugin state, never touches routing, pacing or enforcement.
 * - A bounded append fold over the stored ledger: flag off appends nothing
 *   (pure no-op returning the ledger unchanged); flag on validates then
 *   appends; a malformed record is rejected with named issues, never stored.
 *
 * WHAT IT PROVES (for the PR-closeout sweep / backfill):
 * - Flag on appends: an enabled ledger grows by exactly the validated record.
 * - Flag off no-ops: a disabled ledger returns byte-identical state and
 *   reports `appended: false` — installing this plugin changes nothing until
 *   an operator flips `closeoutLedger.enabled`.
 * - Malformed records are rejected: a bad SHA, an unknown disposition, or a
 *   non-link evidence value never reaches the ledger.
 *
 * NON-GOALS (owned elsewhere, do NOT duplicate):
 * - retry/blocker_attention/decision-log/issue_thread_interaction verbs stay
 *   on case_a2213db5/case_f125613e/case_c554181e/case_54f5e53a.
 * - ask_user_questions/request_confirmation/suggest_tasks parity stays on
 *   case_565b8e7c/case_ad5042ce/QA parity work.
 * - Ledger-schema migration and backfill (Director leaf case_9498ba40).
 */

export const CLOSEOUT_LEDGER_SCHEMA = "case_1a32cffa-closeout-ledger-v1";

/** Closed disposition vocabulary — the repo's own closeout language (PR_CLOSEOUT). */
export const CLOSEOUT_DISPOSITIONS = [
  "merged",
  "closed-superseded",
  "closed-abandoned",
  "decision-recorded",
] as const;

export type CloseoutDisposition = (typeof CLOSEOUT_DISPOSITIONS)[number];

/** One closeout record: what landed, how, and the evidence proving it. */
export interface CloseoutRecord {
  /** Full or abbreviated git SHA of the merge that landed the work. */
  mergeSha: string;
  /** How the work closed — one of CLOSEOUT_DISPOSITIONS, never inferred. */
  disposition: CloseoutDisposition;
  /** Evidence link (merged PR, decision record, or work product URL). */
  evidenceLink: string;
  /** ISO timestamp of the append; stamped by the verb, not the caller. */
  appendedAt: string;
}

/** Caller-supplied candidate record (pre-stamp, pre-validation). */
export interface CloseoutRecordInput {
  mergeSha: unknown;
  disposition: unknown;
  evidenceLink: unknown;
}

export interface CloseoutLedger {
  schema: typeof CLOSEOUT_LEDGER_SCHEMA;
  records: CloseoutRecord[];
}

export type CloseoutAppendOutcome =
  | { appended: true; record: CloseoutRecord }
  | { appended: false; reason: "closeout-ledger-disabled" }
  | { appended: false; reason: "malformed-closeout-record"; issues: string[] };

/**
 * Bound on stored records. The ledger is an audit trail, not a migration
 * target: oldest records drop past the cap rather than failing the append.
 */
export const MAX_CLOSEOUT_RECORDS = 1000;

const SHA_RE = /^[0-9a-f]{7,40}$/i;
const URL_RE = /^https?:\/\/\S+$/i;
const MAX_FIELD_LEN = 2048;

function fail(reason: string): never {
  throw new Error(reason);
}

/** Validate one candidate record, naming every defect. Empty issues = valid. */
export function validateCloseoutRecord(input: CloseoutRecordInput): string[] {
  if (!input || typeof input !== "object" || Array.isArray(input)) return ["malformed-closeout-record-not-an-object"];
  const issues: string[] = [];
  const { mergeSha, disposition, evidenceLink } = input;
  // SHA_RE caps at 40 chars, so no separate length branch is needed.
  if (typeof mergeSha !== "string" || !SHA_RE.test(mergeSha)) {
    issues.push("invalid-closeout-merge-sha");
  }
  if (typeof disposition !== "string" || !(CLOSEOUT_DISPOSITIONS as readonly string[]).includes(disposition)) {
    issues.push("invalid-closeout-disposition");
  }
  if (typeof evidenceLink !== "string" || evidenceLink.length === 0 || evidenceLink.length > MAX_FIELD_LEN) {
    issues.push("invalid-closeout-evidence-link");
  } else if (!URL_RE.test(evidenceLink)) {
    issues.push("invalid-closeout-evidence-link");
  }
  return issues;
}

/**
 * Normalize untyped plugin-state input. Stored state is unversioned JSON a
 * previous build (or a hand edit) may have shaped differently; a malformed
 * value normalizes to an empty ledger rather than crashing the verb. A
 * schema mismatch is also an empty ledger: rows written under a superseded
 * schema describe a rule this build no longer implements. Malformed RECORDS
 * inside an otherwise well-formed ledger are dropped, never repaired.
 */
export function normalizeCloseoutLedger(stored: unknown): CloseoutLedger {
  const empty: CloseoutLedger = { schema: CLOSEOUT_LEDGER_SCHEMA, records: [] };
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) return empty;
  const record = stored as Record<string, unknown>;
  if (record.schema !== CLOSEOUT_LEDGER_SCHEMA) return empty;
  if (!Array.isArray(record.records)) return empty;
  const records: CloseoutRecord[] = [];
  for (const entry of record.records) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const candidate = entry as Record<string, unknown>;
    const issues = validateCloseoutRecord({
      mergeSha: candidate.mergeSha,
      disposition: candidate.disposition,
      evidenceLink: candidate.evidenceLink,
    });
    if (issues.length > 0) continue;
    if (typeof candidate.appendedAt !== "string" || !Number.isFinite(Date.parse(candidate.appendedAt))) continue;
    records.push({
      mergeSha: candidate.mergeSha as string,
      disposition: candidate.disposition as CloseoutDisposition,
      evidenceLink: candidate.evidenceLink as string,
      appendedAt: candidate.appendedAt as string,
    });
  }
  return { schema: CLOSEOUT_LEDGER_SCHEMA, records: records.slice(-MAX_CLOSEOUT_RECORDS) };
}

/**
 * Pure append fold. Flag off returns the ledger unchanged (`appended:
 * false`): the verb writes nothing. Flag on validates first — a malformed
 * record changes nothing and names its defects. A valid record is stamped
 * and appended, oldest records dropping past the cap.
 */
export function appendCloseoutRecord(
  ledger: CloseoutLedger,
  input: CloseoutRecordInput,
  enabled: boolean,
  nowIso: string,
): { ledger: CloseoutLedger; outcome: CloseoutAppendOutcome } {
  if (!enabled) {
    return { ledger, outcome: { appended: false, reason: "closeout-ledger-disabled" } };
  }
  const issues = validateCloseoutRecord(input);
  if (issues.length > 0) {
    return { ledger, outcome: { appended: false, reason: "malformed-closeout-record", issues } };
  }
  if (typeof nowIso !== "string" || !Number.isFinite(Date.parse(nowIso))) fail("invalid-closeout-clock");
  const record: CloseoutRecord = {
    mergeSha: input.mergeSha as string,
    disposition: input.disposition as CloseoutDisposition,
    evidenceLink: input.evidenceLink as string,
    appendedAt: nowIso,
  };
  const records = [...ledger.records, record].slice(-MAX_CLOSEOUT_RECORDS);
  return { ledger: { schema: CLOSEOUT_LEDGER_SCHEMA, records }, outcome: { appended: true, record } };
}
