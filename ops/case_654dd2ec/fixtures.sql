-- ops/case_654dd2ec/fixtures.sql — case_654dd2ec synthetic fixture.
--
-- Minimal stand-ins for the five source tables plus a stub pg_stat_activity,
-- with one positive control (must appear in its view) and at least one
-- negative control (must NOT appear) per invariant. Every timestamp is
-- relative to now() so the fixture never goes stale. Same translation
-- subset as paperclip_invariants.sql (see its header).
--
-- Row naming: P = positive (view must return it), N = negative (must not).

CREATE TABLE agents (
  id uuid PRIMARY KEY NOT NULL,
  company_id uuid NOT NULL,
  name text NOT NULL,
  status text NOT NULL,
  error_reason text,
  last_heartbeat_at timestamp with time zone,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL
);

CREATE TABLE heartbeat_runs (
  id uuid PRIMARY KEY NOT NULL,
  company_id uuid NOT NULL,
  agent_id uuid NOT NULL,
  status text NOT NULL,
  error text,
  exit_code integer,
  signal text,
  context_snapshot jsonb,
  created_at timestamp with time zone NOT NULL
);

CREATE TABLE activity_log (
  id uuid PRIMARY KEY NOT NULL,
  company_id uuid NOT NULL,
  action text NOT NULL,
  entity_type text NOT NULL,
  entity_id text NOT NULL,
  details jsonb,
  created_at timestamp with time zone NOT NULL
);

CREATE TABLE issues (
  id uuid PRIMARY KEY NOT NULL,
  company_id uuid NOT NULL,
  title text NOT NULL,
  status text NOT NULL,
  assignee_actor_id text,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL
);

CREATE TABLE pg_stat_activity (
  pid integer PRIMARY KEY NOT NULL,
  usename text NOT NULL,
  application_name text NOT NULL,
  backend_type text NOT NULL,
  wait_event_type text,
  wait_event text,
  state text NOT NULL,
  xact_start timestamp with time zone
);

-- Invariant 1: agents in error over 10 min.
INSERT INTO agents (id, company_id, name, status, error_reason, last_heartbeat_at, created_at, updated_at) VALUES
  -- P: error, heartbeat 20 min old.
  ('00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000001', 'stuck-error-agent', 'error', 'wake transport refused', now() - INTERVAL '20 minutes', now() - INTERVAL '2 hours', now() - INTERVAL '20 minutes'),
  -- N: error but heartbeat 2 min old (fresh flap, not stuck).
  ('00000000-0000-4000-8000-000000000077', '00000000-0000-4000-8000-000000000001', 'fresh-error-agent', 'error', 'single bad wake', now() - INTERVAL '2 minutes', now() - INTERVAL '2 hours', now() - INTERVAL '2 minutes'),
  -- N: idle with an hour-old heartbeat (wrong status).
  ('00000000-0000-4000-8000-000000000078', '00000000-0000-4000-8000-000000000001', 'idle-agent', 'idle', NULL, now() - INTERVAL '60 minutes', now() - INTERVAL '2 hours', now() - INTERVAL '60 minutes');

-- Invariant 2: failed runs with subtype=success and the 143 signature.
INSERT INTO heartbeat_runs (id, company_id, agent_id, status, error, exit_code, signal, context_snapshot, created_at) VALUES
  -- P: failed, subtype=success text, exit 143.
  ('00000000-0000-4000-8000-000000000079', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000003', 'failed', 'Claude run failed: subtype=success: container got exit 143 under memory pressure', 143, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000080"}', now() - INTERVAL '10 minutes'),
  -- P: failed, subtype=success text, SIGTERM signal, no exit code recorded.
  ('00000000-0000-4000-8000-000000000020', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000003', 'failed', 'Claude run failed: subtype=success: terminated by host', NULL, 'SIGTERM', '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000080"}', now() - INTERVAL '5 minutes'),
  -- N: failed without the subtype text (honest failure).
  ('00000000-0000-4000-8000-000000000081', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000077', 'failed', 'Error: auth_unavailable at the gateway', 1, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000080"}', now() - INTERVAL '10 minutes'),
  -- N: succeeded run carrying subtype text (status filter excludes it).
  ('00000000-0000-4000-8000-000000000082', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000077', 'succeeded', 'Claude run failed: subtype=success: exit 143', 143, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000080"}', now() - INTERVAL '10 minutes'),
  -- N: failed with subtype text and 143, but 2 hours old (outside the 1h window).
  ('00000000-0000-4000-8000-000000000083', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000077', 'failed', 'Claude run failed: subtype=success: exit 143 yesterday', 143, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000080"}', now() - INTERVAL '2 hours');

-- Invariant 3: cards re-woken >3 times in 6h by dispatch_stalled_issue.
INSERT INTO heartbeat_runs (id, company_id, agent_id, status, error, exit_code, signal, context_snapshot, created_at) VALUES
  -- P: four stall-wakes on one card inside 6h.
  ('00000000-0000-4000-8000-000000000084', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000085"}', now() - INTERVAL '10 minutes'),
  ('00000000-0000-4000-8000-000000000086', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000085"}', now() - INTERVAL '1 hour'),
  ('00000000-0000-4000-8000-000000000087', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000085"}', now() - INTERVAL '2 hours'),
  ('00000000-0000-4000-8000-000000000088', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000085"}', now() - INTERVAL '5 hours'),
  -- N: exactly three stall-wakes (at the line, not over it).
  ('00000000-0000-4000-8000-000000000089', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000090"}', now() - INTERVAL '10 minutes'),
  ('00000000-0000-4000-8000-000000000091', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000090"}', now() - INTERVAL '1 hour'),
  ('00000000-0000-4000-8000-000000000092', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000090"}', now() - INTERVAL '2 hours'),
  -- N: five wakes under a different reason (reason filter excludes them).
  ('00000000-0000-4000-8000-000000000093', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000094"}', now() - INTERVAL '10 minutes'),
  ('00000000-0000-4000-8000-000000000095', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000094"}', now() - INTERVAL '1 hour'),
  ('00000000-0000-4000-8000-000000000096', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000094"}', now() - INTERVAL '2 hours'),
  ('00000000-0000-4000-8000-000000000097', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000094"}', now() - INTERVAL '3 hours'),
  ('00000000-0000-4000-8000-000000000098', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"operator_wake","issueId":"00000000-0000-4000-8000-000000000094"}', now() - INTERVAL '4 hours'),
  -- N: four stall-wakes 7+ hours old (outside the 6h window).
  ('00000000-0000-4000-8000-000000000099', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000100"}', now() - INTERVAL '7 hours'),
  ('00000000-0000-4000-8000-000000000101', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000100"}', now() - INTERVAL '8 hours'),
  ('00000000-0000-4000-8000-000000000102', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000100"}', now() - INTERVAL '9 hours'),
  ('00000000-0000-4000-8000-000000000103', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000078', 'succeeded', NULL, 0, NULL, '{"wakeReason":"dispatch_stalled_issue","issueId":"00000000-0000-4000-8000-000000000100"}', now() - INTERVAL '10 hours');

-- Invariant 4: missing-disposition re-parks in the last hour.
INSERT INTO activity_log (id, company_id, action, entity_type, entity_id, details, created_at) VALUES
  -- P: repark carrying the missing_disposition reason.
  ('00000000-0000-4000-8000-000000000104', '00000000-0000-4000-8000-000000000001', 'issue_reparked', 'issue', '00000000-0000-4000-8000-000000000105', '{"reason":"missing_disposition"}', now() - INTERVAL '10 minutes'),
  -- P: non-repark action carrying the missing_disposition reason.
  ('00000000-0000-4000-8000-000000000106', '00000000-0000-4000-8000-000000000001', 'status_change', 'issue', '00000000-0000-4000-8000-000000000107', '{"reason":"missing_disposition"}', now() - INTERVAL '20 minutes'),
  -- P: repark with no recorded reason (unstated cause needs triage).
  ('00000000-0000-4000-8000-000000000108', '00000000-0000-4000-8000-000000000001', 'card_reparked', 'issue', '00000000-0000-4000-8000-000000000109', NULL, now() - INTERVAL '30 minutes'),
  -- N: repark with an explicit different reason (routine).
  ('00000000-0000-4000-8000-000000000110', '00000000-0000-4000-8000-000000000001', 'issue_reparked', 'issue', '00000000-0000-4000-8000-000000000111', '{"reason":"operator_request"}', now() - INTERVAL '10 minutes'),
  -- N: comment with no details (wrong action, no reason).
  ('00000000-0000-4000-8000-000000000112', '00000000-0000-4000-8000-000000000001', 'comment_created', 'issue', '00000000-0000-4000-8000-000000000113', NULL, now() - INTERVAL '10 minutes'),
  -- N: missing_disposition repark 2 hours old (outside the 1h window).
  ('00000000-0000-4000-8000-000000000114', '00000000-0000-4000-8000-000000000001', 'issue_reparked', 'issue', '00000000-0000-4000-8000-000000000115', '{"reason":"missing_disposition"}', now() - INTERVAL '2 hours');

-- Invariant 5: unassigned in-focus cards.
INSERT INTO issues (id, company_id, title, status, assignee_actor_id, created_at, updated_at) VALUES
  -- P: in_progress, no assignee, untouched 30 min.
  ('00000000-0000-4000-8000-000000000116', '00000000-0000-4000-8000-000000000001', 'orphaned focus card', 'in_progress', NULL, now() - INTERVAL '2 hours', now() - INTERVAL '30 minutes'),
  -- N: in_progress and stale, but assigned.
  ('00000000-0000-4000-8000-000000000117', '00000000-0000-4000-8000-000000000001', 'held focus card', 'in_progress', '00000000-0000-4000-8000-000000000118', now() - INTERVAL '2 hours', now() - INTERVAL '30 minutes'),
  -- N: unassigned and in_progress, but touched 5 min ago.
  ('00000000-0000-4000-8000-000000000119', '00000000-0000-4000-8000-000000000001', 'fresh focus card', 'in_progress', NULL, now() - INTERVAL '1 hour', now() - INTERVAL '5 minutes'),
  -- N: unassigned and stale, but not in focus (todo).
  ('00000000-0000-4000-8000-000000000120', '00000000-0000-4000-8000-000000000001', 'backlog card', 'todo', NULL, now() - INTERVAL '2 hours', now() - INTERVAL '30 minutes');

-- Invariant 6: DB pool waiters.
INSERT INTO pg_stat_activity (pid, usename, application_name, backend_type, wait_event_type, wait_event, state, xact_start) VALUES
  -- P: two active backends waiting on locks.
  (42421, 'paperclip', 'paperclip-api', 'client backend', 'Lock', 'transactionid', 'active', now() - INTERVAL '2 minutes'),
  (42422, 'paperclip', 'paperclip-worker', 'client backend', 'Lock', 'tuple', 'active', now() - INTERVAL '1 minutes'),
  -- N: active but not waiting.
  (42423, 'paperclip', 'paperclip-api', 'client backend', NULL, NULL, 'active', now() - INTERVAL '1 minutes'),
  -- N: waiting but idle (idle-in-transaction seat, not pool pressure).
  (42424, 'paperclip', 'paperclip-worker', 'client backend', 'Client', 'ClientRead', 'idle', now() - INTERVAL '10 minutes');
