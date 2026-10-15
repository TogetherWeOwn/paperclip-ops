# Fleet run stall: source contract

This runbook describes an offline-tested observation, not a deployment or current
incident. No database address, credential, host identity or operational approval
is included. Tests use PATH stubs and synthetic SQL fixtures only.

### fleet-run-stall

The source key is `paperclip_fleet-run-stall` (`paperclip/fleet-run-stall`).
The SQL detects no run created or started for 20 minutes while at least one
eligible todo/in_progress card has an invokable assigned agent. The Gatus
fragment uses a ten-minute heartbeat and two consecutive failures. Missing probe
delivery is unknown, not proof that the fleet is healthy or stalled.

The source responder role is `devops`. Verify the read-only query and delivery
freshness first. Distinguish no eligible work from work waiting without any run.
Investigate scheduler/control-plane failures through an authorized operational
path. This alert does not authorize unpausing agents, bypassing admission,
changing model assignments, or restarting services.

## Example scheduling contract

The following source example is not an instruction to install a scheduler.
Configuration must be protected and reviewed separately; do not print its values.

```cron
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/ops/paperclip-ops/ops/case_b79f6544/push-fleet-run-stall.sh
```

The legacy manual form is
`( set -a; . /root/.config/paperclip/case_60330d6f.env; set +a; /opt/ops/paperclip-ops/ops/case_b79f6544/push-fleet-run-stall.sh )`.
It is retained here to identify the source contract, not recommended for use:
its semicolons do not stop after a failed configuration load. A separately
authorized acceptance invocation must use a fail-closed `&&` chain instead.

Rollback: delete `/etc/cron.d/paperclip-fleet-run-stall` only for a separately
authorized new installation whose recorded rollback explicitly owns that file.
Do not remove an existing monitor or stop its scheduler without an owned hold
and expiry. Restore the prior configuration when a pre-existing deployment is
being rolled back; never assume the prior state was absence.

## Offline verification

Run `python3 -m unittest test_case_da79e241_fleet_run_stall` from the repository
root. Preserve the SQL threshold, malformed-result, delivery, credential
non-disclosure and environment-boundary controls. Source tests do not establish
host access or deployment readiness.
