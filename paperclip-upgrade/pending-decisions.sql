-- ===========================================================================
-- paperclip-upgrade/pending-decisions.sql -- READ-ONLY pending-decision gate
-- for pre-restart-check.sh. One row: "<gate>|<count>".
--
-- Counts wake_origin_agent decisions whose continuation is still pending
-- (metadata.continuationPending = true) while their ORIGIN agent is paused,
-- terminated or pending_approval: after a restart, the startup continuation sweep must not
-- meet a pending continuation it cannot deliver to a live origin. The
-- restart is refused unless this count is exactly 0, measured fresh after
-- drain (this SELECT does not share a transaction with the restart).
--
-- Deliberately NO decision-lifetime narrowing: every pending row counts,
-- fresh or long-lived. A filter that only counted a subset by age would
-- hide live pending rows from the gate. Rows are reported, never repaired
-- here.
--
-- A SELECT only: no DDL, no DML, no temp objects. Callers run it with
-- PGOPTIONS='-c default_transaction_read_only=on'. Counts only, no row data.
-- Company scope is the JOIN condition (agent and decision in the same
-- company); the gate covers every company served by this database.
-- ===========================================================================
SELECT 'pending_wake_origin_decisions|' || count(*)
  FROM decisions AS d
  JOIN agents AS a ON a.id = d.origin_agent_id AND a.company_id = d.company_id
 WHERE d.continuation_policy = 'wake_origin_agent'
   AND d.metadata->>'continuationPending' = 'true'
   AND a.status IN ('paused', 'terminated', 'pending_approval');
