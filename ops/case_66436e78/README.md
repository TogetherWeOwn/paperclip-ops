# Backup freshness monitor source contract

This leaf monitors availability of a recent usable backup. It is source-only,
not an installation receipt or authorization to access production backup data.
Offline tests use disposable files and a stub transport. No test creates,
deletes or restores a live database backup.

`push-db-backup-freshness.sh` runs the reviewed `db_backup_stall.sh` detector in
staleness-only mode. It checks for an archive meeting the minimum size within
`MAX_BACKUP_AGE_HOURS` (default two hours), regardless of producer. Freshness is
not proof of successful restore or of health of the in-server scheduler. Those
need their own independently authorized verification.

Stale, absent, truncated and inconclusive measurements push failure. Required
inputs include `GATUS_URL` and `GATUS_EXTERNAL_TOKEN`; use the protected env file,
never token literals in a command or repository. The shared transport keeps its
Bearer value in a 0600 curl config, not argv. Review the backup directory and
threshold configuration for the intended target before installation.

## Cron contract after independent authorization

Select a reviewed immutable bundle and merge the endpoint without duplicate
keys. This source example exports ordinary env-file assignments and stops if
loading them fails. Five-minute pushes leave slack under the ten-minute
heartbeat. It was not executed against a host during source restoration.

```cron
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/ops/paperclip-ops/ops/case_66436e78/push-db-backup-freshness.sh
```

## Triage and rollback

Owner: devops. Verify directory readability, file size and age without printing
backup content. Inspect producer failure evidence and storage capacity; do not
remove archives to suppress an alert. Do not confuse a recent archive with a
verified restore. A missing heartbeat is a failure, not a healthy observation.

Before installation, save the prior bundle and endpoint/environment configuration.
Restore that reviewed state if verification fails. Stopping a critical schedule
requires an owned hold with expiry and continued observation. Rollback must not
delete backup files, disable existing backup producers or fabricate health.
