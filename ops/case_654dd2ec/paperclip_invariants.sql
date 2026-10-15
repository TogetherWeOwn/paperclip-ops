-- ops/case_654dd2ec/paperclip_invariants.sql — case_654dd2ec
--
-- Read-only Paperclip health invariants for Gatus. Each object here is a
-- plain SELECT view over platform tables; there is no remediation path in
-- this file. Remediation stays on the existing allowlisted verbs elsewhere.
--
-- READ-ONLY CONTRACT (pinned by test_case_1372613a_invariants.sh):
--   * Only CREATE OR REPLACE VIEW statements. No other verb.
--   * Every view reads now() (or a now() - INTERVAL window) and exposes it
--     as checked_at, so a stale push is distinguishable from a healthy zero.
--   * The pool view never selects wait statement text: only waiter identity
--     and wait state. Statement text can carry credentials.
--
-- SOURCE TABLES (all live in the platform database):
--   agents, heartbeat_runs, activity_log — same names and columns the
--   operator suites already read (see test/fixtures/orgdb/schema.sql).
--   issues — platform cards table. Column contract the views rely on:
--   id, company_id, title, status, assignee_actor_id, created_at,
--   updated_at. The push script fails closed when a column is absent, so a
--     drifted name pages instead of reading as healthy.
--   pg_stat_activity — stock Postgres statistics view, present on every
--   supported server version. No fixture needed for it; the offline suite
--   stands in a same-shaped stub table.
--
-- SQLITE TRANSLATION SUBSET (offline suite only; production runs Postgres):
--   * "CREATE OR REPLACE VIEW n AS" -> "remove view n if present, then CREATE VIEW n AS"
--   * "now()" -> "datetime('now')"
--   * "datetime('now') - INTERVAL 'N minutes|hours'" -> "datetime('now','-N minutes|hours')"
--   * "col ->> 'k'" -> "json_extract(col, '$.k')"
--   * "ILIKE" -> "LIKE"
-- Nothing else in this file may need translation. Keep it that way.
--
-- Thresholds live with the Gatus definitions (gatus-endpoints.yaml) and the
-- push script (push-paperclip-invariants.sh); this file only exposes the
-- violating rows.

-- 1. Agents stuck in error: status error with no heartbeat for 10 minutes.
-- Fresh errors (deploy flaps, single bad wake) stay out; only stuck ones page.
CREATE OR REPLACE VIEW paperclip_inv_agents_error_10m AS
SELECT
  a.id AS agent_id,
  a.company_id,
  a.name AS agent_name,
  a.status,
  a.error_reason,
  a.last_heartbeat_at,
  now() AS checked_at
FROM agents AS a
WHERE a.status = 'error'
  AND COALESCE(a.last_heartbeat_at, a.updated_at, a.created_at) < now() - INTERVAL '10 minutes';

-- 2. Runs marked failed whose error text claims subtype=success, with the
-- exit-143 (SIGTERM / infra kill) signature. A success subtype on a failed
-- run means the outcome label lied; 143 says who did it.
CREATE OR REPLACE VIEW paperclip_inv_failed_subtype_success AS
SELECT
  r.id AS run_id,
  r.company_id,
  r.agent_id,
  r.status,
  r.exit_code,
  r.signal,
  r.error AS error_text,
  r.created_at AS run_created_at,
  now() AS checked_at
FROM heartbeat_runs AS r
WHERE r.status = 'failed'
  AND r.error LIKE '%subtype=success%'
  AND (r.exit_code = 143 OR r.signal = 'SIGTERM' OR r.error LIKE '%143%')
  AND r.created_at > now() - INTERVAL '1 hour';

-- 3. Cards re-woken more than 3 times in 6 hours by dispatch_stalled_issue.
-- One row per card, so the push script counts cards, not wakes.
CREATE OR REPLACE VIEW paperclip_inv_cards_rewoken_6h AS
SELECT
  r.context_snapshot ->> 'issueId' AS issue_id,
  r.company_id,
  count(*) AS wake_count,
  max(r.created_at) AS last_wake_at,
  now() AS checked_at
FROM heartbeat_runs AS r
WHERE r.context_snapshot ->> 'wakeReason' = 'dispatch_stalled_issue'
  AND r.created_at > now() - INTERVAL '6 hours'
GROUP BY r.context_snapshot ->> 'issueId', r.company_id
HAVING count(*) > 3;

-- 4. Missing-disposition re-parks in the last hour: issue activity carrying
-- the missing_disposition reason, plus reparks with no recorded reason
-- (unstated cause needs triage, not silence). A repark with an explicit
-- different reason is routine and stays out.
CREATE OR REPLACE VIEW paperclip_inv_missing_disposition_reparks AS
SELECT
  l.id AS activity_id,
  l.company_id,
  l.entity_id AS issue_id,
  l.action,
  l.details ->> 'reason' AS park_reason,
  l.created_at AS parked_at,
  now() AS checked_at
FROM activity_log AS l
WHERE l.entity_type = 'issue'
  AND (
    l.details ->> 'reason' = 'missing_disposition'
    OR (l.action ILIKE '%repark%' AND l.details ->> 'reason' IS NULL)
  )
  AND l.created_at > now() - INTERVAL '1 hour';

-- 5. In-focus cards with nobody holding them: in_progress, no assignee,
-- untouched for 15 minutes. Fresh or assigned cards stay out.
CREATE OR REPLACE VIEW paperclip_inv_unassigned_infocus_cards AS
SELECT
  i.id AS issue_id,
  i.company_id,
  i.title,
  i.status,
  i.updated_at AS last_touched_at,
  now() AS checked_at
FROM issues AS i
WHERE i.status = 'in_progress'
  AND i.assignee_actor_id IS NULL
  AND i.updated_at < now() - INTERVAL '15 minutes';

-- 6. Database pool waiters: backends that are active yet waiting on an
-- event. Identity and wait state only; never the statement text.
CREATE OR REPLACE VIEW paperclip_inv_db_pool_waiters AS
SELECT
  s.pid,
  s.usename,
  s.application_name,
  s.backend_type,
  s.wait_event_type,
  s.wait_event,
  s.state,
  s.xact_start,
  now() AS checked_at
FROM pg_stat_activity AS s
WHERE s.wait_event IS NOT NULL
  AND s.state = 'active';
