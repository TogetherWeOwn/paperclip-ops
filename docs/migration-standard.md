# Migration file standard and safety gates

Shared, reusable migration safety for every repo that ships SQL migrations.
One standard, three gates, zero production regressions as the goal. The
runners and workflows below are the canonical copies; app repos consume them
by pinned reference, they do not fork them.

## 1. File standard

Every migration file MUST satisfy all of these; `migration_squawk.sh`
enforces them in CI with no database.

- **Name:** `NNNN_slug.sql` — a 4-digit zero-padded prefix, an underscore,
  a short slug, `.sql`. Example: `0042_add_member_search_index.sql`. The
  prefix order is the apply order, and the ledger gate keys files by content
  hash, so renaming a shipped file is a new migration, never an edit.
- **Phase header:** the first 15 lines carry a comment declaring the
  expand/contract phase, e.g. `-- phase: expand` or `-- phase: contract`.
  Expand migrations only add (nullable columns, new tables, `NOT VALID`
  constraints, concurrent indexes). Contract migrations remove what the
  previous release stopped using.
- **No locking builds:** `CREATE INDEX` MUST be `CREATE INDEX CONCURRENTLY`
  (optionally `UNIQUE ... CONCURRENTLY`). A non-concurrent build holds a
  write lock for the whole build.
- **Two-step constraints:** `ALTER TABLE ... ADD CONSTRAINT` MUST use
  `NOT VALID`. The matching `VALIDATE CONSTRAINT` goes in a LATER migration
  (a later expand phase), never in the same file — validating in the same
  lock window defeats the purpose of `NOT VALID`.
- **Timeouts are injected, not pasted:** authors do NOT paste `SET
  lock_timeout` into files. The deploy runner prepends the canonical
  preamble from `migration_timeout_preamble.sh` (`lock_timeout = '10s'`,
  `statement_timeout = '120s'`) at apply time, and the rehearsal applies
  through the same injector. CI proves presence of the injection, not of
  pasted lines.

Template for a new migration:

```sql
-- phase: expand
-- Replaces nothing. Safe to run while the previous release is serving.

CREATE INDEX CONCURRENTLY IF NOT EXISTS member_search_idx
  ON members USING gin (to_tsvector('english', display_name));

ALTER TABLE members
  ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
```

And the later contract/validate step:

```sql
-- phase: contract
-- Previous release no longer reads the old column. Validates the
-- constraint added NOT VALID two releases ago, then drops the old column.

ALTER TABLE members VALIDATE CONSTRAINT members_email_format;
ALTER TABLE members DROP COLUMN IF EXISTS legacy_handle;
```

## 2. Gate A — squawk in CI

Every migration is linted by [squawk](https://squawk.run/) plus the
file-standard checks above, on every pull request that touches migrations.

Caller (pinned SHA, never a moving tag):

```yaml
jobs:
  migration-squawk:
    uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-migration-squawk.yml@<PINNED_SHA>
    with:
      migrations-dir: packages/db/src/migrations
      strict: true
```

`strict: true` refuses when the squawk binary cannot run, so a job that
promises squawk cannot silently run without it. The reusable installs a
pinned squawk release when `squawk-version` is set; otherwise the
file-standard checks always run and squawk runs when preinstalled.

## 3. Gate B — prod-clone rehearsal

Before a migration chain may ride a production deploy, the exact pending
chain is applied to a throwaway clone of production — same files, same
order, same timeout preamble — then the clone is deleted. Evidence fresher
than 24 hours is required by the production preflight gate.

```yaml
jobs:
  rehearsal:
    uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-migration-rehearsal.yml@<PINNED_SHA>
    with:
      provider: planetscale # or: pitr
      migrations-dir: packages/db/src/migrations
    secrets:
      rehearsal-credentials: ${{ toJSON(secrets) }} # caller maps only its
        # existing read-only principal + provider token; no new principals
```

Providers: `planetscale` branches off prod with the `pscale` CLI and
deletes the branch afterwards; `pitr` runs the caller-owned
`REHEARSAL_RESTORE_CMD` for a point-in-time branch (branches expire with
the restore). Without credentials the job runs `plan` only — it enumerates
and digests the chain but never claims a rehearsal happened. The `run`
path applies through `REHEARSAL_APPLY_CMD` (one wrapped file path
appended) and writes the evidence document consumed below.

## 4. Gate C — drift detector (preflight + nightly)

**Preflight.** The deploy workflow refuses a candidate whose repo digests
are ahead of the production ledger (unapplied migrations would ship) or
whose ledger is ahead of the repo (out-of-band DDL happened). Zero pending
and zero unknown are required; rollback and live-SHA redeploy paths waive
this gate inside their own scripts so recovery never waits on it.

```yaml
jobs:
  drift:
    uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-migration-drift.yml@<PINNED_SHA>
    with:
      migrations-dir: packages/db/src/migrations
      ledger-file: /tmp/prod-ledger.txt # fetched by the caller with its
        # existing read-only principal BEFORE calling this gate
      evidence-file: /tmp/rehearsal-evidence.json
      max-age-hours: 24
```

**Nightly.** A scheduled caller runs the same gate and pages on mismatch:
it auto-files an issue (`gh issue create` with a runbook link) and posts to
Discord via a webhook secret. Both paging steps run only on gate failure
(`if: failure()`), so quiet nights stay quiet.

```yaml
on:
  schedule:
    - cron: "23 4 * * *" # nightly drift sweep, off the :00/:30 marks
```

See `reusable-migration-drift.yml` for the exact paging steps to copy.

## 5. Local proving (no credentials, no database)

```bash
./migration_squawk.sh --migrations-dir path/to/migrations
./test_migration_timeout_preamble.sh && ./test_migration_squawk.sh \
  && ./test_migration_rehearsal.sh && python3 -m unittest test_migration_drift_gate
```

All four suites are offline: fixture files, fake binaries on `PATH`, a
throwaway temp dir. A suite that cannot reach a database, a provider, or
the network refuses or skips loudly — it never scores itself green.
