# Database backup stall: observation and recovery verification

This is a reusable source runbook, not a record of an installation or an incident.
The detector is read-only. It does not restart a database, remove archives, change
a schedule, or prove that a backup can be restored.

## Read the verdict, not just archive age

Use the detector's `--help` to select an explicitly reviewed backup directory and
current server log. An old archive, repeated suppressed backup attempts, an
orphaned attempt, truncation or stale observation needs investigation. Missing or
unreadable evidence is inconclusive, not healthy. A successful restore exercise
is a separate, isolated acceptance gate.

When an installation logs to container stdout instead of the detector's default
file, supply a fresh, complete server-log capture with `--log`;
exit 0 is unreachable without it. Do not weaken the staleness threshold or fabricate a
current timestamp to make the default invocation appear healthy.

```sh
./db_backup_stall.sh --log /path/to/current-server.log \
  --backup-dir /path/to/reviewed-backups --no-health --json
```

The paths above are placeholders, not host locations. Keep captured logs and
backup payloads in protected local storage, never Git or public issue comments.
If API-health correlation is used, keep its bearer in protected configuration,
not arguments. `--no-health` omits that independent signal; it is not proof of API
health. `--no-log` does not evaluate orphan/truncation signals and must not be
reported as doing so.

## After recovery

Confirm new in-server backup completion and archive freshness against the same
measured observation window. A wrapper that still points to an abandoned log
flips from `stall` straight to `inconclusive`, not to green. Fix the observation
source rather than claiming that this transition proves recovery.

The default producer filter excludes host-cron archives so an independent writer
cannot mask a stalled in-server scheduler. `--include-all-producers` requests the
legacy all-writer view explicitly; it changes the question, not the scheduler's
health. JSON retains the excluded archive's freshness for diagnosis.

## Rollback and safety

This runbook authorizes no production mutation. Preserve evidence and revert only
a reviewed detector/configuration change if necessary. Any critical-timer stop
needs an owned hold and expiry. Use a separate staging environment for restore
validation; never point tests at a live database or consume production archives
as public fixtures.
