-- ops/case_b79f6544/fleet_run_stall.sql — case_b79f6544
--
-- Fleet run-stall probe. One read-only SELECT, no view and no DDL, run by
-- push-fleet-run-stall.sh through a read-only session. It returns ONE row
-- when the fleet has stopped executing and ZERO rows when it has not:
--
--   STALLED  = no heartbeat_runs row was created or started in the last 20
--              minutes AND at least one todo/in_progress card has an agent
--              assignee whose agent can be invoked.
--
-- Why this shape. On 2026-10-08 no heartbeat_runs row was created or started
-- from 18:02Z to 21:11Z while the control plane stayed up. The likely cause,
-- not proven here, is heartbeat.wakeOnDemand=false on 28 agents, set at
-- 18:02Z. Any check that needs an agent to run cannot see that. This one
-- reads the database from the host.
--
-- "Created or started": a queued row that is later started, or a row created
-- at once, both count as the fleet being alive. Finishing a run does NOT
-- count: 16 runs in the 1,000-row dump finished between 18:02Z and 20:53Z
-- while none was created or started.
--
-- "Assigned and invokable": a card held by a paused, terminated or
-- pending_approval agent cannot be woken, so it is not evidence of waiting
-- work. Hidden cards are skipped. An agent in `error` still counts: the
-- server will wake it, and an errored fleet with waiting cards is the very
-- thing to page on.
--
-- Output columns (psql -tA -F '|'):
--   waiting_cards         count of assigned todo/in_progress cards
--   last_run_activity_at  newest created_at/started_at, empty when no run exists
--   stalled_min           whole minutes since then, empty when no run exists
--
-- READ-ONLY CONTRACT (pinned by test_case_da79e241_fleet_run_stall.py): a single
-- SELECT; no other verb; reads only issues, agents and heartbeat_runs; no
-- statement text, config or secret column is selected.
--
-- SQLITE TRANSLATION SUBSET (offline suite only; production runs Postgres):
--   * "now() - INTERVAL 'N minutes'" -> "datetime(<pinned>, '-N minutes')"
--   * "EXTRACT(EPOCH FROM (now() - x))" -> "(strftime('%s', <pinned>) - strftime('%s', x))"
--   * "now()" -> "<pinned>"
-- Nothing else in this file may need translation. Keep it that way.
--
-- The 20 minute window lives here and in gatus-endpoints.yaml (alert text);
-- change them together.

SELECT
  w.waiting_cards,
  r.last_run_activity_at,
  CAST(EXTRACT(EPOCH FROM (now() - r.last_run_activity_at)) AS bigint) / 60 AS stalled_min
FROM (
  SELECT count(*) AS waiting_cards
  FROM issues AS i
  JOIN agents AS a ON a.id = i.assignee_agent_id
  WHERE i.status IN ('todo', 'in_progress')
    AND i.hidden_at IS NULL
    AND a.status NOT IN ('paused', 'terminated', 'pending_approval')
) AS w
CROSS JOIN (
  SELECT max(t.ts) AS last_run_activity_at
  FROM (
    SELECT created_at AS ts FROM heartbeat_runs
    UNION ALL
    SELECT started_at AS ts FROM heartbeat_runs
  ) AS t
  WHERE t.ts IS NOT NULL
) AS r
WHERE w.waiting_cards > 0
  AND (r.last_run_activity_at IS NULL
       OR r.last_run_activity_at < now() - INTERVAL '20 minutes');
