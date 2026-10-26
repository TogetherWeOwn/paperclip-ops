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
  prefix order is the apply order. Rehearsal filters `--only` to the tags
  computed from the production ledger, but always applies them in filename
  order, independent of input tag order. The ledger gate keys files by content
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
-- The DROP COLUMN is a reviewed contract-phase exception. Its reason is
-- separate from the bare, statement-scoped squawk directive.

ALTER TABLE members VALIDATE CONSTRAINT members_email_format;
-- Contract phase: legacy_handle is unread since the previous release.
-- squawk-ignore ban-drop-column
ALTER TABLE members DROP COLUMN legacy_handle;
```

Timeouts are enforced by the injector, not by pasted lines, so squawk's
`require-lock-timeout` and `require-statement-timeout` are excluded. The
`prefer-robust-stmts` rule is also excluded because its generic `IF NOT EXISTS`
advice can mask an invalid concurrent index on retry; the migration standard
intentionally forbids that clause. `ADD ... USING INDEX` needs no `NOT VALID`
(valid by construction) and the file-standard gate exempts it.

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
      script-ref: <PINNED_SHA> # required immutable SHA (no main default)
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
    with:
      provider: planetscale # or: pitr
      migrations-dir: packages/db/src/migrations
      script-ref: <PINNED_SHA> # required immutable SHA for gate scripts
      pending-tags: "0042_add_member_search_index" # exact pending tags from the ledger
      # {branch} is replaced with the created clone name; adapter must target it.
      apply-cmd: "./scripts/apply-to-rehearsal-branch.sh {branch}"
      # restore-cmd: "..." # pitr only, or the REHEARSAL_RESTORE_CMD variable
    secrets:
      rehearsal-pscale-org: ${{ secrets.REHEARSAL_PSCALE_ORG }}
      rehearsal-pscale-db: ${{ secrets.REHEARSAL_PSCALE_DB }}
      rehearsal-pscale-token-id: ${{ secrets.PLANETSCALE_SERVICE_TOKEN_ID }}
      rehearsal-pscale-token: ${{ secrets.PLANETSCALE_SERVICE_TOKEN }}
      # rehearsal-pitr-endpoint: ${{ secrets.REHEARSAL_PITR_ENDPOINT }} # pitr
```

Secrets are declared one by one (a reusable workflow cannot receive an
opaque secret bundle). PlanetScale uses the standard service-token ID/token
environment variables and waits for the created branch to be ready; without
a token pair it requires valid `pscale auth check` credentials. Generated
PlanetScale branch names use lowercase letters, digits, and hyphens (maximum
48 characters). Generated names include a random suffix; caller-supplied names
must be unique per run. The gate refuses a pre-existing branch before
creation (`pscale branch show` must report the verified `branch <B> does
not exist in database <D>` not-found text). Normal completion and unexpected
shell exits use the script's
cleanup trap; the reusable workflow also retries an `always()` cleanup step
up to three times after cancellation or timeout when its unique-run marker
shows clone creation began. A final failure names the exact branch to delete.
PITR parses the caller-owned restore command as arguments
(preserving quoted values) and appends the branch name. The apply command
must contain `{branch}`; the workflow substitutes the created branch name and
passes it as an explicit argument, preventing a passing rehearsal against an
unrelated target. The adapter must connect to that branch and apply each
wrapped file. Failed cleanup fails the rehearsal; PITR expiration is recorded
as `delegated`, not `deleted`. Without usable credentials the job plans only
and never claims rehearsal evidence. The required `pending-tags` input narrows both planning
and application to exactly the ledger-computed pending tags; the first apply
failure stops the chain and later migrations record `not_attempted`.

## 4. Gate C — drift detector (preflight + nightly)

**Preflight.** The deploy workflow refuses a candidate whose ledger contains
unknown digests (possible out-of-band DDL). Candidate migrations already in
the ledger need no rehearsal; candidate migrations absent from it may proceed
only when fresh evidence is bound to exactly that pending subset. Without the
evidence, any pending migration fails the gate. Rollback and live-SHA redeploy
paths waive this gate inside their own scripts so recovery never waits on it.

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
      script-ref: <PINNED_SHA> # required immutable SHA for gate scripts
      ledger-artifact: prod-ledger
      evidence-artifact: rehearsal-evidence
      max-age-hours: 24
```

The evidence artifact must contain exactly one JSON document with
`status: passed` (the `rehearsal-plan.json` planned document is ignored).
Evidence is bound to exactly the pending
candidate subset (same tags and digests); unrelated evidence and a future
`finished_at` fail. PlanetScale evidence must confirm branch deletion; PITR
evidence uses `cleanup: delegated` to represent provider-managed expiration.
The page step creates its `drift` / `automated-page` labels when missing.

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
  && ./test_migration_rehearsal.sh && python3 -m unittest test_migration_drift_gate \
  && python3 -m unittest test_migration_evidence_ax
```

All five suites are offline: fixture files, fake binaries on `PATH`, a
throwaway temp dir. A suite that cannot reach a database, a provider, or
the network refuses or skips loudly — it never scores itself green.

## 6. Machine-readable verdicts (`--json`)

`migration_drift_gate.py` accepts an opt-in `--json` flag for agent
consumers. Without it, output and exits are unchanged (one `ok:`/`FAIL:`
line per check; 0 pass, 1 gate failed, 2 bad input). With it, stdout is
exactly one versioned JSON object — parseable on every success and
input-error path — and diagnostics never carry a traceback or raw
evidence content.

The object shape (the single canonical schema source is
`JSON_SCHEMA_VERSION` in `migration_drift_gate.py`):

```json
{"version": 1, "verdict": "pass", "pending_count": 0, "unknown_count": 0,
 "checks": [{"id": "candidate_chain", "status": "pass", "reason": "ok"}],
 "next_action": ""}
```

- `"version"` is the schema version (currently 1).
- `"verdict"` is `pass`, `fail`, or `refused`, and maps to the exit code
  (pass 0, fail 1, refused 2). JSON mode never changes the exit code.
- `"pending_count"` counts candidate migrations absent from the ledger;
  `"unknown_count"` counts ledger hashes unknown to the candidate.
- `"checks"` lists stable check entries (`"id"`, `"status"`, `"reason"`)
  such as `candidate_chain`, `ledger_unknown`, `evidence_schema`,
  `evidence_freshness`, `evidence_binding`, and `evidence_order`.
- `"next_action"` is empty on pass and names the concrete next step on
  any refusal, so callers can act without parsing prose.

Reason-to-`next_action` examples (each reason below is a stable ID from
the verdict object):

- `evidence_stale` → `next_action` "Re-run the rehearsal for fresh
  evidence within max-age (24h default) and re-submit."
- `evidence_unbound` → `next_action` "Rehearse the exact pending chain
  so evidence tags and digest hashes are bound to this candidate."
- `evidence_duplicate_tag` → `next_action` "Remove duplicate migration
  tags from the evidence and re-run the rehearsal."
- `pending_without_evidence` → `next_action` "Rehearse the pending
  migrations and attach fresh evidence, or narrow the candidate to
  already-ledgered tags."
- `unknown_ledger_rows` → `next_action` "Inspect the production ledger
  for out-of-band DDL and reconcile the candidate repo before retrying."
- `evidence_future` → `next_action` "Check the rehearsal clock:
  finished_at is in the future; re-run the rehearsal with correct time."

Evidence shape (root object, version, migrations list, row field types,
duplicate tags) is validated before evaluation; wrong-shaped evidence
fails closed with exit 2 (`refused`) instead of crashing.

Dry-run is inherent: this gate is read-only. It only reads its input
files, writes nothing, and reaches no network or database, so every
invocation is already a dry-run and no separate preview flag exists.

Verify offline with the focused command (synthetic fixtures only):

```bash
python3 -B -m unittest test_migration_evidence_ax test_migration_drift_gate
```

## 7. Documentation accompaniment for these interfaces

The existing documentation gate (see [docs-drift](docs-drift.md)) carries one
exact-path rule, `migration-contract`, for the three reusable interface
families above. Changing any listed source requires an added or
content-modified change to this standard in the same patch; a deleted or
chmod-only doc change does not satisfy it.

| Family | Interface and helpers (exact tracked paths) | Required documentation |
|---|---|---|
| Gate A — squawk | `.github/workflows/reusable-migration-squawk.yml`, `migration_squawk.sh` | `docs/migration-standard.md` |
| Gate B — rehearsal | `.github/workflows/reusable-migration-rehearsal.yml`, `migration_rehearsal.sh` | `docs/migration-standard.md` |
| Gate C — drift | `.github/workflows/reusable-migration-drift.yml`, `migration_drift_gate.py` | `docs/migration-standard.md` |
| Shared helpers | `migration_timeout_preamble.sh`, `migration_sql.py` | `docs/migration-standard.md` |

The shared helpers cross family boundaries: the timeout preamble injects
timeouts for the rehearsal and apply paths, and `migration_sql.py` serves
both the squawk file-standard checks and the preamble injector. All eight
paths are therefore sources of the same single rule rather than exclusive
per-family ownership. The mapping lives only in `.docs-drift.json`; there is
no second registry or checker, and a new mapping enforces only after merge,
when it is present in the target commit's policy.

This checks accompaniment only. A passing result does not prove that this
document accurately describes the interfaces, that a migration is safe to
deploy, or that rehearsal evidence is fresh; Gates A–C and review own those
claims.
