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
`--no-health`, `--local-only` and its alias `--dry-run` perform the same local
read-only observation: no network, board/state writes or credential config. The
ordinary invocation can optionally GET `/api/health` and create a temporary,
protected credential config. That health view is advisory: it never clears a
stall. Local-only output is not proof of API health. The detector has no
`--no-log` option; `--staleness-only` skips both the log and orphan checks.

## Modes and observation integrity

| Question | Local-only invocation (placeholder paths) | Selected producers |
|---|---|---|
| Is the in-server scheduler stalled? | `./db_backup_stall.sh --local-only --log ./current.log --backup-dir ./backups --json` | In-server; excludes `-hostcron.sql` and `-hostcron.sql.gz` |
| Is any writer producing recent archives? | `./db_backup_stall.sh --dry-run --staleness-only --backup-dir ./backups --json` | All by default; no log or orphan evaluation |
| Include host orphans and archives in combined checks | Add `--include-all-producers` to the first invocation | All |
| Select a different host marker | Add `--exclude-suffix nightly` | Same suffix contract for `.sql` and `.sql.gz`; explicitly narrows staleness-only too |

Thresholds are unchanged: at least 1024 bytes, strictly over two hours for
archive/log staleness, and at least 90 minutes for an orphan. The existing
unsettled-start rule is unchanged. There is no clock-skew grace: even a one-second
future mtime is not freshness evidence. Each file's mtime is checked against a
clock sampled after its stat; that same observation time determines its age and
orphan cutoff. `checkedAt` remains the run's start time. This is a size/mtime
detector, not gzip, SQL or restoration verification.

A measured empty directory is a positive **stall (exit 1)**. An absent/unreadable
directory, failed enumeration, failed stat or unusable/future timestamp is an
**unknown observation**, not a zero-byte archive, epoch-zero orphan or fresh
backup. Without an independent measured stall, required unknown evidence gives
**inconclusive (exit 2)**, in either mode. A partial scan never proves absence or
health; a positively observed orphan or suppression still survives that failure.

A valid independent fresh archive can establish availability despite a different
unusable candidate. An old archive beside an unknown candidate cannot establish
that *all* selected archives are stale. Invalid excluded host observations remain
visible but do not invalidate independent in-server evidence.

## Machine output

Legacy JSON fields, `reasons`, `notes`, verdicts and exit codes remain. The
additive `schemaVersion: 1` contract includes:

- `signals.suppression`, `signals.orphan`, `signals.freshness`: each has `state`
  (`measured`, `unknown`, `not_evaluated`) and a `codes` array. `measured` describes
  evidence, not health: a measured empty directory is still a stall.
- `producers.in_server.selected`, `producers.host.selected`: boolean selection
  under the configured suffix; the `host` ID also represents a custom marker.
- Root `codes`: stable reason IDs across signals; `next_action`: nonempty guidance,
  **not** authorization or an executed remediation.

Observation codes include `directory_unmeasured`, `stat_failed`,
`file_unmeasured`, `mtime_invalid`, `mtime_future`, `clock_unmeasured`, `producer_excluded`,
`log_unmeasured`, `log_read_failed`, `window_unanchored`, `log_stale`. Finding codes
are `backup_suppressed`, `backup_unsettled`, `orphan_dump`, `truncated_sibling`,
`no_usable_backup`, `archive_stale`. Notes identify excluded/unknown files and
producers. A code can coexist with a measured signal when other valid evidence
answers its question. Legacy age `-1` means no usable measured timestamp, never a
negative fresh age. Do not publish raw JSON/log lines containing private data.

Offline verification uses synthetic trees, no inherited credentials, and a
run-owned `TMPDIR`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_backup_observation_contract
bash test_db_backup_stall.sh
```

The immutable acceptance suite denies writes and sockets and injects real scan
failure plus deterministic stat faults. The existing shell suite uses health
stubs, not the real endpoint. Passing these tests establishes source behavior
only: it neither deploys this detector, rearms a monitor nor proves live coverage.

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
