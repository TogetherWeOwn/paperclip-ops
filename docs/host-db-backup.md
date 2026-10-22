# Host backup: publication receipts

`host_db_backup.sh` is the existing independent host-side pg_dump fallback.
This source contract **does not authorize execution, installation, scheduling,
restore, or a database/host operation**. Existing security and deployment fences
still apply. Discover it without touching live state:

```bash
./host_db_backup.sh --help
./host_db_backup.sh --json --dry-run
```

`--dry-run` parses/validates arguments, then returns a plan before checking live
state, opening a lock, creating an artifact, calling podman, or enumerating files.
It does not prove that tools, directories, or the container are available.
Unknown flags, missing values, invalid container names and non-positive retention
refuse with exit 2; adding `--help` or `--dry-run` cannot activate a bad invocation.

## Cycle contract

- Keep the existing flock and courtesy COPY query. The query is not a lock against
  the server scheduler; an unmeasurable count still follows the existing behavior.
- Create an exclusive, run-owned hidden `.partial` in the backup directory. Never
  reuse or delete an older invocation's partial. Check pipeline status, the
  compressed 1024-byte floor, gzip integrity, at least one `CREATE TABLE` outside
  COPY data, and a terminal completion footer. No SQL is executed or restored.
- Footer verification ignores marker-like COPY rows. Accept the standard
  `--` / `-- PostgreSQL database dump complete` / `--` block and legacy bare
  terminal marker. Only whitespace and a matching modern `\unrestrict` may follow;
  a `\restrict` prefix requires its matching terminal `\unrestrict`. This is a
  structural stream check, not a complete SQL parser or restore validation.
- Publish with same-directory GNU `mv -nT`; require both its success and a consumed
  temporary source. A no-clobber skip is an error, even on versions returning 0.
  Existing files, directories and symlinks are never publication targets to replace.
  Failed publication cannot report PASS or start retention; cleanup touches only
  this invocation's owned temporaries, never a final archive.
- After publication, enumerate the full retention candidate list successfully
  **before** deleting anything. Preserve `-maxdepth 1`, regular files,
  `paperclip-*-hostcron.sql.gz`, and `-mtime +N` (default N=2); never delete the new
  final archive. Server-produced, young and nested archives remain outside policy.
  Enumeration failure means an unknown count, not zero. Deletion failure counts
  successful removals but returns a partial cycle; the valid new archive survives.

The default target, naming (`paperclip-YYYYMMDD-HHMMSS-hostcron.sql.gz`), retention
policy, environment variables, and courtesy semantics are unchanged; see `--help`.
The implementation needs Bash 5 and GNU/Linux utilities, including `awk`, `mktemp`
and GNU `mv` with no-clobber/no-target-directory support. No retry engine is added.

## Machine output v1

`--json` emits exactly one terminal JSON object on stdout (except `--help`, which
always prints help). Text mode retains `host_db_backup=PASS file=… size_bytes=…
tables=…` for a landed archive and `host_db_backup_retention: pruned=…
retention_days=…` for completed retention. **PASS describes publication only**:
consume the final receipt/exit status to determine the complete cycle result.

| Field | Contract |
|---|---|
| `schema_version` | Integer `1` |
| `cycle_id` | Opaque per-invocation identifier, including for refusals/plans/skips |
| `archive_id` | Published basename, or `null` before publication |
| `outcome` | `success`, `error`, `partial`, `planned`, `skipped` |
| `stage` | Stable token in the table below |
| `error_code` | Stable token below; `null` for success/plan/skip |
| `retryable` | Boolean; advisory, never permission or an automatic retry |
| `next_action` | Stable remediation token; `none` on complete success |
| `backup_landed` | Boolean; true only after verified publication |
| `retention.status` | `complete`, `failed`, `not_started` |
| `retention.pruned` | Successful removal count; `null` until enumeration succeeds |

Identifiers contain no database/user identities, paths, or dump content. Archive
IDs retain the existing filename semantics: a same-second collision is refused,
not overwritten. Cycle IDs distinguish invocations even within the same second.
Raw podman/database diagnostics are not emitted; errors carry remediation tokens.

| Stage | Error codes → next_action |
|---|---|
| `arguments` | `invalid_arguments` → `correct_arguments` |
| `precondition` | `missing_tool` → `install_required_tools`; `invalid_backup_directory` → `correct_backup_directory`; `container_unavailable` → `inspect_container`; `database_identity_unavailable` → `inspect_container_configuration`; `clock_failed` → `inspect_clock` |
| `lock` | `lock_open_failed` → `correct_lock_directory` |
| `dump` | `temporary_create_failed` → `inspect_backup_directory`; `dump_pipeline_failed` → `inspect_dump_failure` |
| `verification` | `archive_stat_failed` → `inspect_archive_verification`; `archive_too_small`, `gzip_integrity_failed`, `missing_create_table`, `invalid_terminal_trailer` → `inspect_dump_failure` |
| `publication` | `destination_exists` → `inspect_existing_archive`; `atomic_publication_failed` → `inspect_destination_before_retry` |
| `retention` | `retention_list_failed`, `retention_enumeration_failed`, `retention_deletion_failed` → `inspect_retention_without_redump` |
| `cleanup` | `temporary_cleanup_failed` → `inspect_owned_temporary_artifact` |
| `interrupted` | `interrupted` → `inspect_cycle_before_retry` |

Non-error stages: `plan` → `obtain_execution_authorization`, `lock` →
`wait_for_active_cycle`, `courtesy` → `wait_for_active_dump`, `complete` → `none`.
Only dump-pipeline errors and deliberate concurrency skips advertise retryable;
all other paths require inspection/correction first.

Exit 0 means complete, planned, or skipped; 1 means failed/partial cycle; 2 means
refused/precondition failure. A retention error is `partial`, `backup_landed=true`,
with the immutable archive ID: **do not blindly run a new dump to fix retention**.
A failed temporary cleanup also returns nonzero without invalidating a published
archive. Uncatchable termination cannot produce a receipt or guarantee cleanup.

## Offline verification

Use a run-owned `TMPDIR` and clear runtime overrides. Both suites create their own
synthetic podman adapters and fixture-only filesystem trees; neither contacts a
real database/container or prunes the configured live directory:

```bash
env -i PATH="$PATH" HOME="$HOME" TMPDIR="$PAPERCLIP_RUN_SCRATCH_DIR" \
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_host_backup_publication
env -i PATH="$PATH" HOME="$HOME" TMPDIR="$PAPERCLIP_RUN_SCRATCH_DIR" \
  bash test_host_db_backup.sh
bash -n host_db_backup.sh
shellcheck host_db_backup.sh
```

The immutable acceptance suite owns publication-fault, collision, COPY/footer,
interrupted/corrupt/undersized stream, traversal/deletion failure, JSON, dry-run
and concurrency controls. Its command spies fail closed on unexpected host calls.
