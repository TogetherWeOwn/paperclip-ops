-- gh-event-capture: the durable store.
--
-- Append-only is enforced HERE rather than in application code. The two
-- triggers below refuse UPDATE and DELETE for every caller — including a future
-- version of `store-d1.js`, including `wrangler d1 execute`, including a person
-- pasting SQL into the dashboard console. That makes it a property of the
-- database instead of a discipline someone has to remember.
--
-- Its limit, stated here so it is not learned mid-incident: an actor with
-- Cloudflare account access can `DROP TRIGGER` and then rewrite history, and
-- the drop is not itself recorded anywhere in this database. This raises the
-- bar; it does not make the store tamper-evident. See README § "What this does
-- not cover", which says the same thing about the GitHub side.

CREATE TABLE IF NOT EXISTS deliveries (
  -- X-GitHub-Delivery. PRIMARY KEY *is* the de-duplication: GitHub retries any
  -- delivery it did not see a 2xx for, and a retry reuses this id.
  delivery_id     TEXT    PRIMARY KEY,

  received_at     TEXT    NOT NULL,          -- ISO-8601 UTC, for humans
  received_ms     INTEGER NOT NULL,          -- epoch ms, for ordering and keyset paging

  event           TEXT    NOT NULL,          -- X-GitHub-Event
  action          TEXT,                      -- payload.action, when present
  sender          TEXT,                      -- payload.sender.login
  repository      TEXT,                      -- payload.repository.full_name
  organization    TEXT,                      -- payload.organization.login
  installation_id INTEGER,                   -- payload.installation.id

  hook_id         TEXT,                      -- X-GitHub-Hook-ID
  target_type     TEXT,                      -- X-GitHub-Hook-Installation-Target-Type

  -- The HMAC that verified. Not a secret (it cannot be reversed into the shared
  -- secret) and keeping it is what lets the whole store be re-verified later
  -- against the secret — see README § "Re-verifying the store".
  signature       TEXT    NOT NULL,

  -- Always describe the FULL body, even when `body` below is truncated.
  body_sha256     TEXT    NOT NULL,
  body_bytes      INTEGER NOT NULL,
  body_truncated  INTEGER NOT NULL DEFAULT 0,

  body            TEXT    NOT NULL,          -- raw bytes as received, UTF-8 decoded
  headers_json    TEXT    NOT NULL           -- allowlisted headers only
);

-- The three questions this store gets asked: what happened recently, what
-- happened to this repo, and what happened of this kind.
CREATE INDEX IF NOT EXISTS deliveries_received  ON deliveries (received_ms DESC);
CREATE INDEX IF NOT EXISTS deliveries_repo      ON deliveries (repository, received_ms DESC);
CREATE INDEX IF NOT EXISTS deliveries_event     ON deliveries (event, received_ms DESC);

CREATE TRIGGER IF NOT EXISTS deliveries_no_update
BEFORE UPDATE ON deliveries
BEGIN
  SELECT RAISE(ABORT, 'deliveries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS deliveries_no_delete
BEFORE DELETE ON deliveries
BEGIN
  SELECT RAISE(ABORT, 'deliveries is append-only');
END;

-- Rejected requests, as bounded COUNTERS and never as rows.
--
-- The webhook route is unauthenticated by construction — GitHub has to reach
-- it, so everyone can. Storing a row per rejected request would hand any
-- passer-by an unbounded write channel into the store the control depends on.
-- One row per (UTC day, reason) has fixed cardinality and still answers the
-- question worth asking: is someone probing this endpoint?
--
-- This table is intentionally NOT append-only: the counter has to increment.
CREATE TABLE IF NOT EXISTS rejections (
  day    TEXT    NOT NULL,                   -- YYYY-MM-DD, UTC
  reason TEXT    NOT NULL,                   -- a REJECT.* identifier from verify.js
  count  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, reason)
);
