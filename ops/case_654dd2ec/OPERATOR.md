# Paperclip invariant monitor source runbook

This is a reusable read-only monitoring contract, not live installation evidence
or permission to access production. The offline suite applies the SQL to a
disposable fixture database. Private board records and credentials are excluded.
An independently authorized installation must review schema compatibility,
read-only credentials, endpoint ownership and the exact immutable bundle first.

The six views diagnose; none authorizes an automatic board mutation. The pusher
reports unreadable input as failure, not zero rows. Env-file assignments must be
exported from protected configuration, with no credential values in argv/logs.
Keep the configured schedule shorter than the ten-minute endpoint heartbeat.

### agents-error-10m

Owner: director. Rows identify agents remaining in error beyond the view's time
window. Inspect the terminal run and actual failure cause before routing a
supported recovery. Do not resume paused agents or reset unknown failures.

### failed-subtype-success

Owner: director. Rows identify failed runs whose result claims success in the
one-hour window. Reconcile status, exit code and result subtype from the same
run; success-shaped text is not proof of a successful completion. Preserve the
contradictory evidence rather than rewriting it to make the signal green.

### cards-rewoken-6h

Owner: director. More than three stall-wakes for a card in six hours indicates a
loop. Inspect wake causes, dependencies and the latest valid liveness path.
Coalesce duplicate work through supported APIs; do not reopen terminal cards or
assign the owner just to clear the count.

### missing-disposition-reparks

Owner: director. More than two qualifying re-parks in the one-hour window need
inspection of disposition and continuation evidence. Repair the actual missing
path; comments alone are not a valid waiting or execution path.

### unassigned-infocus-cards

Owner: director. Inspect rows for actionable unassigned cards under the source's
configured scope. Verify current routing and project ownership before assigning
through the supported workflow. Historical focus terminology in a view does not
authorize cancelling unrelated work or changing project policy.

### db-pool-waiters

Owner: devops. Inspect waiter count and age, connection usage and long-running
queries using read-only diagnostics. Distinguish saturation from an unavailable
measurement. Do not terminate queries, restart the database or raise connection
limits solely to silence the alert.

## Rollback

Save the prior view definitions, bundle and endpoint configuration before any
separately authorized installation. Restore reviewed definitions/configuration
without deleting board history. A critical schedule may be stopped only with an
owned hold, expiry and replacement observation. Never publish artificial healthy
results to compensate for a broken monitor.
