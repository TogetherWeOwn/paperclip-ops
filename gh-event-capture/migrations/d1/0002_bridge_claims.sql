-- gh-event-capture: the bridge's at-most-once wake ledger (case_22b092f5).
--
-- One wake per (issue, head sha, kind) is a property this table enforces, not
-- a promise the consumer keeps. `claim_key` IS `bridge.claimKey(...)` and the
-- PRIMARY KEY is the whole mechanism: `INSERT OR IGNORE` either claims a fresh
-- key or changes nothing, and the caller (`store.claimBridge`) reports which
-- one happened. A consumer that crashes after claiming and before posting the
-- wake fails safe in the direction of "missed a wake", never "sent two" — see
-- README § "What a missed wake looks like" for the recovery path.
--
-- Append-only: a claim records intent BEFORE the board write, not proof that
-- the write succeeded. Rewriting it would erase the consumer's crash evidence.
-- Work-product claims use PR/action/delivery identity; wake claims remain per
-- issue/head/event. Both encodings live in src/bridge.js.

CREATE TABLE IF NOT EXISTS bridge_claims (
  -- bridge.claimKey({ issueRef, headSha, kind }) — see src/bridge.js.
  claim_key    TEXT    PRIMARY KEY,

  issue_ref    TEXT    NOT NULL,             -- `TOG-n`
  head_sha     TEXT,                         -- null only for events with no head sha
  kind         TEXT    NOT NULL,             -- work_product_create | work_product_update | check_suite_completed
  delivery_id  TEXT    NOT NULL,             -- the `deliveries.delivery_id` that produced this claim

  claimed_at   TEXT    NOT NULL,             -- ISO-8601 UTC
  claimed_ms   INTEGER NOT NULL              -- epoch ms, for ordering
);

CREATE INDEX IF NOT EXISTS bridge_claims_issue    ON bridge_claims (issue_ref, claimed_ms DESC);
CREATE INDEX IF NOT EXISTS bridge_claims_claimed  ON bridge_claims (claimed_ms DESC);

CREATE TRIGGER IF NOT EXISTS bridge_claims_no_update
BEFORE UPDATE ON bridge_claims
BEGIN
  SELECT RAISE(ABORT, 'bridge_claims is append-only');
END;

CREATE TRIGGER IF NOT EXISTS bridge_claims_no_delete
BEFORE DELETE ON bridge_claims
BEGIN
  SELECT RAISE(ABORT, 'bridge_claims is append-only');
END;
