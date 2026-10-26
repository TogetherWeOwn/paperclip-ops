# Pre-restart continuation gate: install handoff

## Contract

Restart must stop when a pending `wake_origin_agent` decision continuation
has a `paused`, `terminated` or `pending_approval` origin agent.
`pending-decisions.sql` returns exactly one aggregate row:

```text
pending_wake_origin_decisions|0
```

Only zero passes. A positive count, query/transport error, empty response or
malformed response fails closed. There is no acknowledgement override and no
repair, expiry, cancellation or wake operation here. The query reads every
company in the service database, joining origin agent ID **and** company ID;
it does not narrow by decision age or lifecycle status. It counts only
`continuation_policy = 'wake_origin_agent'` with
`metadata->>'continuationPending' = 'true'`. Agent status `error` is still
wakeable and is not one of the three blocked states.

The guard executes SQL through the existing container-local `psql` path with
`PGOPTIONS=-c default_transaction_read_only=on`, `-X`, unaligned tuples-only
output and `ON_ERROR_STOP=1`. Credentials remain inside the database container.
Evidence is aggregate counts only; do not publish decision or agent records.
This is a fresh observation after drain, not an atomic database transaction
spanning the subsequent restart. Preserve the scheduling/admission hold.

## Entry points and existing guards

- `ops/paperclip/pre-restart-check.sh` delegates to the complete
  `paperclip-upgrade/pre-restart-check.sh` with unchanged arguments and exit
  status. Install the repository-relative layout, not that entry point alone.
- `activation.sh` continues to pin the helper-set precheck path;
  `drain.sh restart` runs it before any recreate and propagates failure.
- Both paths retain the drained-marker, checksum-verified/fresh-backup,
  migration, orphan, disk and ownership guards. `--ack-orphans` affects only
  orphan evidence, never decision continuations. This does not change the
  strict activation manifest schema or waive any existing guard.

## Authorized executor checklist (install only, no restart)

1. Wait for exact-head required checks and review to pass and the source PR
   to merge. Record the full reviewed source commit and merge commit. Check
   the current installed layout and helper digests before changing anything.
   Retain the installed helper bundle and manifest at the existing protected
   rollback destination; do not publish those destinations or inventories.
2. Stage the reviewed helper bundle outside the live path. At minimum verify
   these files from the **same** reviewed tree:
   - `ops/paperclip/pre-restart-check.sh`
   - `paperclip-upgrade/pre-restart-check.sh`
   - `paperclip-upgrade/pending-decisions.sql`
   - `paperclip-upgrade/orphans.sql` (unchanged dependency)
   Preserve the established non-agent-writable ownership/modes and helper
   layout. Check SHA-256 of installed bytes against the reviewed tree,
   including both SQL dependencies; do not source a moving branch or fetch
   code at execution time.
3. Update `PRE_RESTART_CHECK_SHA256` in the separately reviewed activation
   manifest to the digest of the **full helper-set**
   `paperclip-upgrade/pre-restart-check.sh`, not the compatibility entry
   point. The existing activation path uses that full helper. Recompute any
   enclosing manifest digest/receipt through its existing contract. Do not
   add unknown keys, weaken pins, substitute the compatibility wrapper into
   an unrelated helper set, or bypass a manifest refusal.
4. Run the independent offline fixtures and the existing activation contract
   suite against the staged tree. Record source commit, dependency digests,
   exact argv, exit statuses and fixture results. The staged gate must refuse
   every disallowed state and unreadable/malformed evidence, and pass eligible
   and no-pending fixtures while preserving all other guards.
5. Install only through the existing authorized host contract. On an isolated
   rehearsal database, capture the query above's aggregate result and the
   complete precheck exit status with a valid drain/backup fixture. Rehearsal
   must report `pending_wake_origin_decisions|0`. Missing query evidence is
   **NOT QUALIFIED**, never zero. A nonzero count is **CHANGES REQUIRED**;
   reconcile through supported product operations under separate authority,
   never an ad hoc database update or mutation-based probe.

**ACT:** install the reviewed external guard and its query dependency only.
**WHY:** fail before restart rather than leave an undeliverable continuation
blocking startup housekeeping. **UNDO:** stop before restart; restore the
previously captured helper bundle and matching manifest only through the
existing rollback contract, keeping restart disabled until the equivalent
safety gate is restored. Never undo by omitting the continuation check.

This handoff is not permission to deploy, restart, restore or migrate
production. The convergence deployment still requires two consecutive passing
rehearsals (the second on the final image) and an explicit release go/no-go
from its authorized decision-maker. No core patch, carried fork migration,
model change, credential change, new authority or spend is introduced.

## Retirement trigger

Retire this temporary external continuation gate at the **first official
stable release containing upstream commit
`cda77edc600e990764596796d1ae8b9e382ac7ac`** (`cda77edc6`,
[upstream change](https://github.com/paperclipai/paperclip/commit/cda77edc600e990764596796d1ae8b9e382ac7ac)).
The change moves startup housekeeping off the readiness path; it is not a
claim that all continuation delivery semantics changed. Before retirement,
record the official release tag, exact image/source digest, ancestry proof
that the commit is included, and equivalent startup/rehearsal evidence.
Until then keep this gate mandatory. Retiring it must preserve all unrelated
restart, manifest, readiness and rollback guards; no implicit ninth patch.
