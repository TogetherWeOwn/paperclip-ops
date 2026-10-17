# Paperclip activation contract (no-chown, exclusive lock)

`activation.sh` is the only supported entry point for a drained,
immutable Paperclip activation. It wraps `drain.sh` in one process that
holds a single service-scoped deployment lock from first validation to
final undrain. It is a source contract: it proves the argv, bindings
and failure behaviour offline; it does **not** attest that any host
has these scripts installed, that a candidate image was built from a
particular recipe, or that a runtime currently satisfies a gate (see
"Remaining host-only attestations").

## Usage

```sh
activation.sh preflight --manifest /abs/activation.conf --state-dir /abs/state
activation.sh run       --manifest /abs/activation.conf --state-dir /abs/state --live
activation.sh resume    --manifest /abs/activation.conf --state-dir /abs/state --live
activation.sh rollback  --manifest /abs/activation.conf --state-dir /abs/state --live
activation.sh abort     --manifest /abs/activation.conf --state-dir /abs/state --live
activation.sh rewake    --manifest /abs/activation.conf --state-dir /abs/state --live [--rewake-filter FILE | --focus-gate-expired]
```

- `preflight` takes the lock briefly, validates everything read-only,
  and starts no drain.
- `run` refuses an existing state dir; a crashed or failed activation
  is continued with `resume`, never restarted over the same state.
- `rollback` runs only for a state this manifest owns, only after the
  operator points the compose file at the pinned pre-change image
  (its hash must then equal `ROLLBACK_COMPOSE_SHA256`), and undrains
  only through `drain.sh`'s verified path.
- `abort` is pre-restart only and only ever lifts its own drain
  (`drain.sh` CAS checks the drain it started and the waker file it
  created; a pre-existing drain or waker-disable is left alone).
- `rewake` is the owned path for wakes deferred at undrain (focus
  gate) or left unsent by a transient failure: it requires this
  manifest's owner record and a completed undrain/abort, and passes
  `--rewake-filter` / `--focus-gate-expired` through to `drain.sh`.
  Without it, deferred wakes would have no lock-carrying caller.
- The wrapper pins every per-helper path environment override
  `drain.sh` honors to the scanned helper set, so a stale override in
  the caller's environment cannot substitute unscanned code.
- Compose identity (file hash, service image/user, mount set) is
  judged at preflight and judged *again* immediately before each
  recreate (restart and undrain), because the natural wait between
  preflight and restart can run for hours.

State ownership is recorded in `<state-dir>.activation-owner`
(manifest SHA-256 + lock path) *before* the drain starts. `resume`,
`rollback` and `abort` refuse a state whose owner record names a
different manifest or lock, so one activation can never undrain or
roll back another activation's drain.

## The lock

- Real `flock(2)` on the manifest's `DEPLOY_LOCK_PATH`, held by the
  `activation.sh` process on fd 8 for the entire invocation — not a
  metadata file, lease timestamp or heartbeat.
- The path is validated before use: absolute; parent directory a
  non-symlink owned by the invoking user with no group/other write;
  an existing lock file a non-symlink, owned, owner-only (`0700`
  mask) regular file. A collision (another holder) refuses; there is
  no waiting, stealing or stale-lock breaking. Because the lock is
  keyed by service identity in the manifest — not by state dir — two
  activations with different state directories are mutually excluded.
- On holder death the kernel releases the lock. `resume` can then
  re-acquire it, but only for the manifest recorded in the owner
  file, and `drain.sh` markers decide which phase runs next. An
  interrupted restart (`restart_started_at` without `restarted_at`)
  is never guessed at: `resume` stops and prints the rollback argv.
- `drain.sh` refuses every mutating subcommand unless the caller
  proves descent from the holder: lock path + token in the
  environment, the token matching the holder's record inside the
  lock file, an inherited open fd on that same file (compared by
  device+inode, so a symlinked path ancestor cannot false-refuse a
  genuine holder), and a failed non-blocking re-lock attempt.
  `drain.sh status` is exempt
  (read-only). Installed legacy scripts that bypass this wrapper are
  a host removal item, not something source can enforce remotely;
  once these sources are installed, the bypass refuses before any
  state is created.

## Parity table

Each behaviour of the previous restart/hardening path, what it
protected, how the contract now proves it, and what happens on
failure. "Pre" runs before the drain; "post" runs under the
scheduling hold before undrain.

| Old operation | Preserved invariant | Pre/post check now | Failure action |
|---|---|---|---|
| Recursive root `chown` of `.local`, `.gitconfig`, plugin root after restart | Agent uid cannot write launcher, gitconfig or plugin roots | Pre + post (inside `drain.sh verify`): `post-restart-reharden.sh` stat-verifies owner `EXPECT_PROTECTED_OWNER` and no group/other write on the roots **and every descendant** (symlinks fail unless their resolved target also passes), then probes write denial as the agent uid with sentinel answers — an inconclusive `docker exec` FAILs, never reads as denied. No ownership is changed | Verify/baseline FAIL; server stays under the hold; resolve ownership out of band under separate authority |
| Non-recursive root `chown`+`chmod` of Cargo pool root + `policy.json` | Pool policy is operator-controlled while slot dirs stay agent-writable | Root and `policy.json` stat-verified non-recursively; slot directories are never tightened or inspected for tightening; pool required iff `REQUIRE_CARGO_POOL=1`, still judged when present | FAIL, hold preserved; slots untouched either way |
| `chmod -R go-w` metadata repair | Protected paths are not group/other writable | Same stat check proves it instead of applying it: a writable mode is a failed gate, byte-for-byte left as found | FAIL, no repair attempted |
| Image entrypoint root branch re-owning the data mount | Nothing in the launch path changes ownership under the mounts | Pre: entrypoint argv pin + entrypoint file hash pin + content scan refusing any ownership-change command; pinned non-root `EXPECT_USER` in image and compose; post: PID 1 uid re-read from `/proc/1/status` | Refuse before drain (pre) or hold + rollback argv (post) |
| Per-state-dir `flock` | One activation at a time per service | The service-scoped lock above, held across every phase; the per-state-dir lock remains as defence in depth inside `drain.sh` | Second activation refused, first is never disturbed |
| Natural drain + stable-zero wait | No work is cancelled; restart only at measured quiescence | Unchanged `drain.sh` machinery: `STABLE_POLLS ≥ 2` consecutive polls with API and SQL hard gates at zero, same container, same drain; `wait` gives up with admission still closed | Phase FAIL; admission stays closed; owned `abort` only |
| Maintenance-window refusal in the legacy wrapper | No restarts inside the blocked UTC window | Manifest-pinned blocked window judged from the live UTC hour before drain and again before restart; no override flag, no `FORCE` (its mere presence refuses) | Refused before any mutation |
| Disk checks in backup/pre-restart | Restart never starts against a full disk | Manifest floor `MIN_DISK_AVAIL_BYTES` pre + pre-restart, plus `drain.sh`'s own backup/pre-restart disk gates (their `df` call was repaired for current coreutils, where `-P` and `--output` are mutually exclusive) | Refused/FAIL before recreate |
| (new) host-pressure admission | No activation onto a loaded or memory-short host | Live `/proc/loadavg` and `MemAvailable` against manifest ceilings/floors; missing or malformed evidence refuses — thresholds have no defaults in code | Refused before drain/restart |
| Image swap by tag in compose | The exact reviewed build is what boots | Candidate ref pinned to `EXPECT_IMAGE_ID`, compose file SHA-256, service image/user, sorted mount-destination hash, effective-build file pins (`EXPECT_FILES` + entrypoint), health `.commit` against `EXPECT_COMMIT`, revision label checked when present but never sufficient alone; recreate is `--no-build --pull never` | Refuse pre-drain for binding mismatches; post-recreate mismatches hold + rollback |
| Rollback by re-pointing compose | Rollback lands on the actual pre-change image/config | Pre-change image id is read live from the running container and must equal `ROLLBACK_IMAGE_ID`; the rollback ref must resolve locally; rollback compose must hash to `ROLLBACK_COMPOSE_SHA256` before `drain.sh rollback` runs | Refused; server stays held |
| Restart hardening side effects | PID 1 umask `0077`, sandbox canary, effective build, migrations, plugin/config/inventory parity | Unchanged `drain.sh verify_held` gates (umask from `/proc/1/status`, review-bot sandbox canary, effective-build proof, migration gate, drift checks) plus the wrapper's PID 1 uid probe before verify | Verify FAIL; hold preserved; nothing admitted |
| Installed pre-restart check run before restart | The restart only proceeds on the reviewed pre-restart gates (drained marker, verified backup, migrations present, orphans, disk) | `drain.sh restart` runs the helper-set `pre-restart-check.sh` (path pinned by the wrapper, helper overrides cannot substitute it) and the wrapper verifies that file's SHA-256 against `PRE_RESTART_CHECK_SHA256` at preflight and again immediately before restart. The check's backup-cover test matches the standard `sha256sum` format `drain.sh backup` actually writes | Digest mismatch refuses before drain/restart; a failing check stops restart with the old server still running under the drain |
| (new) pending-decision safety | No pending decision continuation crosses a restart onto a paused/terminated origin agent | Pre (inside `pre-restart-check.sh`, fresh in the drained transaction): `pending-decisions.sql` counts `wake_origin_agent` decisions with `metadata.continuationPending=true` whose origin agent is `paused` **or** `terminated` — deliberately no age/expiry narrowing. Must be exactly 0; there is no ack flag and rows are never repaired by the check | Restart FAIL before recreate; reconcile through the product, then re-run |
| Fresh backup taken ad hoc before an upgrade | A restorable, validated backup exists before anything can change schema or image | Phase order is drain → wait → **backup** → restart: `drain.sh backup` pg_dumps, TOC-checks and checksums the dump; `drain.sh restart` re-verifies the checksum and refuses a backup older than the manifest's `MAX_BACKUP_AGE_MINS` (no code default) — all before `compose_up`, i.e. before any migration the candidate boot applies | Backup or validation failure stops the run pre-recreate; admission stays closed; owned `abort` only |
| Migrations applied implicitly by a new image's boot | A schema-changing boot happens only under an explicit out-of-band admission for exactly those migrations, with old-image rollback compatibility decided there | Manifest `PENDING_MIGRATIONS` (`none`, or the 4-digit prefixes the candidate will newly apply). Anything but `none` requires `MIGRATION_ADMISSION_RECEIPT` + its SHA-256 pin, verified at preflight and again immediately before restart. The declaration is then **proved, not trusted**: `drain.sh restart` compares the candidate image's migration hashes against the backup-time ledger (`migration_gate.py --expect-pending-exact`) before any recreate, so a stale `none` cannot skip admission for an image carrying a new migration. The wrapper consumes the receipt by hash only — it executes no migration, parses no decision and waives nothing. Rollback stays image-only through `drain.sh`'s known-ahead gate: additive schema and history are retained, never dropped, and no DB restore is exposed | Missing/mismatched receipt refuses before drain/restart; declaration/image mismatch stops restart before recreate; the admission decision itself stays with its issuing authority (host attestation) |
| Legacy 916 redaction overlay refusal | Upgraded server never runs the whole-file override | Unchanged `compose_no_legacy_overlay` + effective-build `--forbid-916-redaction`; private overlays in general are pinned via `EXPECT_HOST_FILES` when `PRIVATE_OVERLAY_REQUIRED=1` | Restart refused before the server is touched |

## Manifest

A `0600` regular file of `KEY=VALUE` lines (see
`activation.example.conf`), parsed strictly and never sourced:
unknown or duplicate keys refuse, every required key must be present,
and identity values are shape-checked (full image ids, full source
commit, 64-hex hashes, non-root user, `STABLE_POLLS ≥ 2`). There are
no code defaults for thresholds, window hours or identities: a
missing value is a refusal, not a guess. The manifest itself carries
no secrets — it pins public digests and paths; credentials stay in
`drain.sh`'s existing `0600` curl auth file.

## Failure behaviour

- Before the drain: any refusal exits 2 with the host untouched.
  A `resume` whose next phase would start the drain re-judges
  window, pressure and baseline exactly like `run` — the owner
  record never inherits stale admission.
- `drain.sh` exit 3 (partial/deferred: the phase completed with
  warnings, e.g. undrain opened admission and a rewake warned) maps
  to wrapper `PARTIAL` + exit 3 with the owned `rewake` argv — never
  reported as a held-drain failure. Rerun is safe.
- After the drain, before restart: admission stays closed; the only
  ways out are `resume` (same manifest) or owned `abort`.
- After restart: the server stays under `drain.sh`'s scheduling
  hold; `verify` or the runtime probe failing never undrains. The
  printed next step is the owned `rollback` argv above.
- Rollback verify failing likewise leaves the rolled-back server
  held. There is no automatic undrain anywhere in this contract.
- Rollback (one shot): point the compose file at the rollback image →
  `activation.sh rollback ... --live` → it verifies the rollback
  bindings, runs `drain.sh rollback` (image-only; the wrapper does
  not expose `drain.sh`'s `--restore-db` database restore — needing
  one is a separate, explicitly reviewed operator procedure), probes
  PID 1, then undrains.
  Undoing the compose edit afterwards is the operator's recorded
  config change, hash-pinned like the forward edit.

## Hermetic tests

`paperclip-upgrade/test-activation-offline.sh` (wired into
`scripts/verify-fast.sh` and CI) stubs `docker` and `drain.sh`; it
touches no Docker daemon, database or network. It covers: the
verify-only hardener (baseline pass, no repair, missing baseline,
agent-writable path, slot untouched), every binding refusal (image,
commit, compose, mounts, user, entrypoint argv/hash, entrypoint
carrying an ownership-change command, rollback identity, helper
carrying one), pressure/window evidence, lock path safety and
cross-state-dir exclusivity, the full phase order under inherited
lock proof, verify-failure hold preservation, foreign-state refusal,
owned rollback argv and preconditions, and real `drain.sh` refusing
mutating commands without wrapper proof. It also executes the real
`pre-restart-check.sh` against a SQL-answering Docker stub: a fresh
backup in the exact `sha256sum` format `drain.sh backup` writes passes
its checksum gate, a tampered dump fails it, and a nonzero
pending-decision count (paused/terminated origins, no expiry filter in
`pending-decisions.sql`) fails it. The pre-existing
`test-offline.sh` database proofs create isolated `rehearse_sqlproof_*`
databases on the agent test database only; nothing in this contract
adds a database-creating suite.

## Remaining host-only attestations

The authorized host executor must still attest, with live evidence,
before an activation from these sources:

1. The installed scripts on the host are exactly this reviewed tree
   (digests), and legacy wrappers/entrypoints that bypass
   `activation.sh` are removed or refuse.
2. The candidate image's build recipe and provenance (this contract
   pins image id, entrypoint content and effective-build files; it
   does not prove what built the image or what private overlays the
   build consumed).
3. The protected-path ownership baseline itself: that the roots are
   already owned/moded as `EXPECT_PROTECTED_OWNER` requires, and how
   they were established (this path never establishes them).
4. The real manifest values: compose file and rollback-compose
   hashes, mount set, thresholds and window for that host, plugin
   inventory handoff, and any private-overlay file pins.
5. Runtime (a)–(d) evidence for an actual activation — drained
   readback, healthy boot on the pinned image, verify output, and
   post-undrain behaviour — plus the recorded one-shot rollback
   rehearsal/result for that host.
6. That the lock file's parent directory on that host is the intended
   operator-owned location and that no second deployment identity
   (user, timer, legacy wrapper) can write it.
7. The installed pre-restart check's provenance: that the digest bound
   as `PRE_RESTART_CHECK_SHA256` is the reviewed script actually
   installed at the host path the executor records (absolute path,
   digest, exact argv and exit status in the activation receipt), and
   that its database scope covers the service being restarted. This
   source ships and pins the generic `pre-restart-check.sh` +
   `pending-decisions.sql`; it does not prove what a host has
   installed under any product-tree path.
8. For a candidate whose boot applies migrations: the admission
   decision behind `MIGRATION_ADMISSION_RECEIPT` (exact migration
   inventory, backup proof, and old-image compatibility with the
   retained additive schema/history), issued by the authority that
   owns that boundary — plus the frozen product source pin for the
   batch being activated (a full immutable commit, never a moving
   branch) and the protected destination/receipt for the fresh backup
   this contract takes before recreate.
