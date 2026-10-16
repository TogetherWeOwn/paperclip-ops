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

Template for a new migration (this exact text passes both the
file-standard gate and squawk 2.70.0 with the canonical
`squawk-migration.toml`):

```sql
-- phase: expand
-- Replaces nothing. Safe to run while the previous release is serving.
-- No IF NOT EXISTS on the concurrent index: a failed CONCURRENTLY build
-- leaves an INVALID index, and IF NOT EXISTS would make the retry report
-- success while indisvalid stays false. The injector lifts
-- statement_timeout for this statement, so the build is never cancelled
-- by the 120s bound; lock_timeout stays on.

CREATE INDEX CONCURRENTLY member_search_idx
  ON members USING gin (to_tsvector('english', display_name));

ALTER TABLE members
  ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
```

And the later contract/validate step:

```sql
-- phase: contract
-- Previous release no longer reads the old column. Validates the
-- constraint added NOT VALID two releases ago, then drops the old column.
-- The DROP COLUMN is a reviewed contract-phase exception: squawk's
-- ban-drop-column still fires by default and this line carries its own
-- ignore with the reason, never a blanket exclusion.

ALTER TABLE members VALIDATE CONSTRAINT members_email_format;
-- squawk-ignore ban-drop-column: contract phase, column unread since the previous release
ALTER TABLE members DROP COLUMN legacy_handle;
```

Timeouts are enforced by the injector, not by pasted lines, so squawk's
`require-lock-timeout` / `require-statement-timeout` are the ONLY rules
excluded in `squawk-migration.toml`. `ADD ... USING INDEX` needs no
`NOT VALID` (valid by construction) and the file-standard gate exempts it.

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
      squawk-version: "2.70.0" # keep in sync with the workflow default
```

`strict: true` refuses when the squawk binary cannot run, so a job that
promises squawk cannot silently run without it. When no squawk is on PATH
the reusable installs the pinned `squawk-version` release binary (no
cargo build); preinstalled runners skip the install. The file-standard
checks always run either way.

## 3. Gate B — prod-clone rehearsal

Before a migration chain may ride a production deploy, the exact pending
chain is applied to a throwaway clone of production — same files, same
order, same timeout preamble — then the clone is deleted. Evidence fresher
than 24 hours is required by the production preflight gate.

```yaml
jobs:
  rehearsal:
    uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-migration-rehearsal.yml@<PINNED_SHA>
    permissions:
      contents: read
      actions: write # evidence artifact upload
    with:
      provider: planetscale # or: pitr
      migrations-dir: packages/db/src/migrations
      apply-cmd: "psql $REHEARSAL_DATABASE_URL -v ON_ERROR_STOP=1 -f"
      # restore-cmd: "..." # pitr only, or the REHEARSAL_RESTORE_CMD variable
    secrets:
      rehearsal-pscale-org: ${{ secrets.REHEARSAL_PSCALE_ORG }}
      rehearsal-pscale-db: ${{ secrets.REHEARSAL_PSCALE_DB }}
      rehearsal-pscale-token: ${{ secrets.REHEARSAL_PSCALE_TOKEN }}
      # rehearsal-pitr-endpoint: ${{ secrets.REHEARSAL_PITR_ENDPOINT }} # pitr
```

Secrets are declared one by one (a reusable workflow cannot receive an
opaque secret bundle). Providers: `planetscale` branches off prod with the
`pscale` CLI — installed automatically from the pinned `pscale-version`
when missing — and deletes the branch afterwards; `pitr` runs the
caller-owned restore command for a point-in-time branch (branches expire
with the restore). Without credentials the job runs `plan` only — it
enumerates and digests the chain but never claims a rehearsal happened,
even when the CLI is installed. The `run` path applies through the
caller-owned apply command (one wrapped file path appended, fail-fast:
the first failure stops the chain and the rest record `not_attempted`)
and writes the evidence document consumed below. Pass `--only` via a
`plan`/`run` wrapper when the chain is a ledger-computed pending subset
rather than the whole directory.

## 4. Gate C — drift detector (preflight + nightly)

**Preflight.** The deploy workflow refuses a candidate whose repo digests
are ahead of the production ledger (unapplied migrations would ship) or
whose ledger is ahead of the repo (out-of-band DDL happened). Zero pending
and zero unknown are required; rollback and live-SHA redeploy paths waive
this gate inside their own scripts so recovery never waits on it.

```yaml
jobs:
  fetch-ledger:
    runs-on: ubuntu-latest
    steps:
      # The caller fetches with its EXISTING read-only principal, then
      # uploads: a reusable workflow is a separate job on a fresh runner,
      # so /tmp files from this job are invisible to the gate below.
      - run: ./scripts/fetch-prod-ledger.sh > prod-ledger.txt
      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: prod-ledger
          path: prod-ledger.txt
  drift:
    needs: [fetch-ledger, rehearsal]
    uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-migration-drift.yml@<PINNED_SHA>
    permissions:
      contents: read
      actions: read # artifact download
      issues: write # auto-filed page cards only (page-on-failure)
    with:
      migrations-dir: packages/db/src/migrations
      ledger-artifact: prod-ledger
      evidence-artifact: rehearsal-evidence
      max-age-hours: 24
```

The evidence is bound to the candidate chain (same tags, same digests):
evidence for an unrelated chain fails, as does a `finished_at` in the
future. The page step creates its `drift` / `automated-page` labels when
missing, so the page is never lost to a missing-label error.

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
