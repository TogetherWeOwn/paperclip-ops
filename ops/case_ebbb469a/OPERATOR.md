# Runner census source contract

This public runbook describes the source, not a current runner inventory or
permission to install it. CI uses disposable GitHub/transport stubs. No live
runner, pool, timer or production service is changed by these tests.

## Inputs and scheduling

A separately authorized operator selects a reviewed immutable bundle and a
protected credential/environment wrapper. GitHub access needs organization runner
read permission. `GH_TOKEN`, `GATUS_URL` and `GATUS_EXTERNAL_TOKEN` are inherited
from protected configuration; never embed values in argv, cron or the repository.
The queued-job snapshot must be fresh, complete and readable; an absent or stale
snapshot cannot establish zero demand. Review the configured organization,
labels and grace period rather than inheriting an unrelated installation's pool.

The following is the source scheduling contract. The scheduling environment must
already supply the protected variables; this is not a self-contained installer.
Five-minute pushes leave slack under the ten-minute endpoint heartbeat.

```cron
*/5 * * * * /opt/ops/paperclip-ops/ops/case_ebbb469a/push-runner-census.py --queued-json /var/lib/status-page/queued-jobs.json
```

### runner-census

Owner: devops. Compare queued demand with the per-label registered, online, busy,
idle and offline counts. Confirm snapshot age, pagination completeness and label
scope before making a capacity decision. Read-only `--no-push` classification can
be used under separate authorization; it does not activate or repair runners.

- `starved`: old demand with no online runner for the label. Inspect availability
  and labels; do not raise capacity blindly.
- `draining`: online runners are all busy. Preserve active work and inspect its
  progress before changing the pool.
- `dual`: a runner has both mutually exclusive pool labels. Verify the intended
  isolation contract before a separately reviewed label repair.
- `idle-with-queue`: idle capacity exists but old demand remains. Inspect runner
  groups, routing and labels rather than assuming more machines are needed.
- `waiting`: demand is still within the configured grace period.

API denial, malformed or truncated runner data, inconsistent pagination, stale
queued input and future timestamps are UNKNOWN and push failure. Missing data
must never produce a healthy census. The script only reads GitHub and optionally
pushes the endpoint; it does not cancel jobs, delete runners or resize pools.

## Rollback

Keep the prior reviewed bundle and endpoint/environment configuration. Restore
those together if a separately authorized installation fails verification.
Stopping a critical schedule requires an owned hold with expiry and continued
observation. Never claim recovery by deleting evidence or pushing fake health.
