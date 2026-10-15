# Hung-job monitor source contract

This runbook is reusable source documentation, not an installed-service inventory
or authorization to deploy. Tests run against disposable API and transport stubs.
No live workflow, runner or host is modified by the evaluator.

## Scheduling contract

Operator: select a reviewed immutable bundle and separately authorize its target,
credentials, endpoint and schedule. Merge the external endpoint fragment only
after checking for duplicate keys. Store ordinary shell assignments in a protected
0600 env file; never put token values in cron, argv, logs or this repository.
Required inputs include `GH_REPOS`, `GATUS_URL` and `GATUS_EXTERNAL_TOKEN`.
The GitHub credential needs read access to checks and open PRs. The evaluator
never uses Actions jobs/runs APIs or cancels jobs.

The example exports env-file assignments and refuses to execute when sourcing
fails. A five-minute schedule leaves slack under the ten-minute heartbeat.
It is a source contract, not a command executed during restoration.

```cron
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_e1e23b50/push-ci-hung-job.sh
```

### hung-job-runbook

Owner: devops. Read the exact repository, current head, check name and timestamps
from the bounded diagnostic output. Only started `in_progress` checks older than
five minutes are candidates. Queued, completed and superseded-head checks are not
hung-job evidence. WARN is strictly above 75% of the named timeout; HUNG is
strictly above timeout plus fifteen minutes. An unknown name uses the explicit
360-minute default and is not silently exempted.

Confirm the timeout against the reviewed workflow and threshold table. Inspect
runner liveness and job progress using independently authorized read paths. Do not
cancel work based solely on its age. Distinguish a lost completion event from a
still-progressing job, unavailable API, malformed timestamp or truncated read.
API/read failures push failure, not healthy. A missing heartbeat also fails closed.
WARN-only text is in stdout: Gatus stores error text only on a failing push.

## Rollback

Retain the previous bundle, env-file location and endpoint configuration before
an authorized installation. Revert the bundle/configuration together if the new
monitor is invalid. Stopping an existing critical schedule requires an owned
hold with expiry and a replacement observation path; never mask a failure by
sending a synthetic healthy result. Do not delete historical receipts or alter
running CI jobs as part of monitor rollback.
