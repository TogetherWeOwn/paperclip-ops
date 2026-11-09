# Disposable synthetic simgit evaluation

This is an experiment, not an installed provider or a recommendation to adopt
simgit. The [Actions workflow](../../../.github/workflows/simgit-evaluation.yml)
runs on `ubuntu-latest` with a 20-minute job timeout. Its lifetime budget is
workflow run numbers 1 and 2 only, attempt 1 only: one initial run and one
defect-justified correction documented in [CORRECTION.md](CORRECTION.md).
Further pushes and reruns cannot execute the matrix under this workflow. Keep the PR draft until its evidence is reviewed. Do not
run this experiment on a shared host. No workflow dispatch, scheduled backstop,
paid/self-hosted runner, live source data or runtime integration is provided.

## Reproduction and identities

Push a matching PR head. The job runs:

```sh
python3 -I -m unittest discover -s ops/eval/simgit -p 'test_*.py' -v
bash ops/eval/simgit/run.sh
```

`run.sh` requires a GitHub-hosted runner and builds only
`abendrothj/simgit@a4721aca27ffcbedfd79c553c78f8969c29bbc63` (workspace version
0.5.0) using `cargo build --release --locked --bin simgit`. The codeload archive
SHA-256 is `2c57d38748159f2ae161d8de349861daa1acd14cde7d138819184f7e6720265d`.
It never executes the candidate's installer or tests, and never substitutes a
published release. Cargo dependency checksums come from that commit's lockfile.
The build gets five minutes; the experiment gets ten; action pins are full SHAs.
No candidate source is executed on the development host.

The runner image is deliberately moving `ubuntu-latest`; the artifact records
its ImageOS/ImageVersion, kernel, backing filesystem, Git/Rust versions,
candidate binary hash, and image-provided FUSE binary hashes/versions. The FUSE
backend is an observed image component, not an independently pinned package.
There is no backend installation or host package change. Missing commands,
`/dev/fuse` permissions or ext4 produce a precise `limitation.txt` with
NO-GO/UNRUN, not an emulated successful optimization. A successful preflight is
not sufficient: every allocation must return `mode=overlay` and have an actual
`fuse.fuse-overlayfs` mount-table entry. All adds force
`SIMGIT_POPULATE=overlay` and `--require-cow`; fallback is never accepted.

## Matrix and measurement boundary

One process runs native Git and simgit, each with cold and warm baseline caches,
and N=1 and N=4 trees. Warm means a seed tree is created and removed before the
measured creation, retaining simgit's baseline cache; it does **not** mean
flushing the kernel page cache. Native Git has no analogous extra baseline
cache. Every scenario gets a separate identical generated repository: 64 tracked
256-KiB deterministic SHAKE-256 payloads (16 MiB total), plus a tiny ignore file.
No production fixture or host inventory is read. No more than four worktrees
are live concurrently. Divergence performs unique in-place writes, renames,
deletes, and generates 512 KiB of ignored output per tree. It then exercises
ordinary Git add/commit/status and records distinct HEAD/index/branch identity.

`result.json` contains timings, commands with exits/stdout/stderr, case verdicts,
verified mounts, and allocation snapshots before cache warming, before creation,
unchanged, divergent, committed, after native removal, and after explicit prune.
For each snapshot it sums `st_blocks * 512` over **physical backing paths** in
that scenario root, deduplicating `(st_dev, st_ino)`. That includes the source
checkout, common Git objects, indexes/refs/locks, baseline cache, overlay
upper/work directories, native worktrees, generated output and residual cache.
Mounted overlay views are excluded: their synthetic inode identities would
otherwise double-count the lower and upper backing files. Symlinks are not
followed. Generated output is separately categorized, not credited as baseline
savings. Never subtract or attribute unchanged shared Git objects to simgit.

Compare absolute `allocated_bytes` totals for matching cache/N/stage rows;
net saving = native total minus simgit total. Report the cold cache construction
cost and residual post-removal cache alongside marginal creation. Do not use
logical payload size, vendor benchmarks or a ratio of tree counts as savings.

Uncertainty: inode blocks do not include ext4 journal, allocator bookkeeping or
free-space reserves; hidden mountpoint directory allocation is not visible
while mounted (up to one directory block per view for this fixture). `statvfs`
available-byte snapshots supply a noisy cross-check, not an alternative exact
measurement: the image and background activity share the filesystem. ext4 has
no reflink sharing here, so inode deduplication is sufficient for this backing
route; this method is not a general reflink physical-extent measurement. Small
metadata differences within these uncertainties are inconclusive. This small
single trial does not establish population performance or production reclaim.

## Isolation, lifecycle and containment

The negative fixture tests reject wrong identity/version, malformed allocation
shapes, relative/noncanonical/inside-source/symlinked paths, disagreement among
launch/cleanup paths, and wrong mode/branch/base/persistence. Every live allocation
validates its exact expected identity/path/mount before use. The candidate does
not itself promise an outside-source restriction; this test-only caller applies
it. These are caller contract checks, not an installed credential broker.

Cases record PASS/FAIL/UNRUN. The four-tree cold simgit case additionally checks:

- dirty safe removal refuses and preserves work;
- an owned `simgit run` launcher refuses unlock while alive, then is killed
  together with its synthetic waiting child and recovered with native unlock;
- a uniquely identified owned FUSE process is killed, daemon exit and actual
  failed access are observed, then native repair is invoked once; upperdir
  contents and ordinary Git access must survive;
- safe native removal leaves no linked registrations, paths, owned FUSE
  processes or mount entries; explicit prune measures residual storage.

After the matrix, one additional single-tree lifecycle probe injects a dirty
worker exception and checks the native failure finalizer, residual allocation
and prune. It is not another storage matrix. Safe refusal requires exit 1 plus
the expected diagnostic and preserved data, not just any nonzero/crash exit.
The launcher child is explicitly tracked and its exit verified.

Any substantive failure stops the matrix; absent cases stay UNRUN. The finalizer
inspects registrations and attempts bounded cleanup of **only this experiment's
synthetic trees**, with dirty discard recorded as failure containment, not a
successful normal cleanup. No unknown add is blindly replayed. Residual mounts,
processes and bytes are recorded. The ten-minute soft timeout raises a catchable
SIGTERM interruption and allows 90 seconds for bounded native cleanup; command
timeouts retain partial audit output. A hard timeout can prevent finalization;
absence of final evidence then means UNKNOWN/UNRUN. Disposable runner destruction
is final containment, never proof of successful explicit native cleanup.

Git refs, objects, config, hooks, remotes and stashes are intentionally SHARED,
just like ordinary linked worktrees. Processes and credentials are not isolated.
A worker can change repository-wide configuration or refs. Never describe this
as a process/credential/security sandbox.

## Threat model, output and stopping rule

The untrusted inputs are the fixed candidate source/dependency graph and its
JSON/path output. Compilation and execution occur only on a disposable public
GitHub-hosted runner. Checkout does not persist credentials. No secret is
referenced; candidate subprocess environments are allowlisted with a fresh HOME.
The runner itself has powerful OS permissions: this is workload containment,
not a claim that Cargo or FUSE is sandboxed. Artifacts contain generated source
paths/content only, not private host data. Audit evidence is command-level.

The workflow uploads `simgit-evaluation-<exact-head-sha>` even on failure, retained
14 days. Preserve the run/job/artifact links and results in the requesting task
before artifact expiry or PR closure. A green job can mean a **recorded negative
or incomplete experiment**; inspect `limitation.txt` and `result.json`, not the
badge. There is one initial matrix and at most one defect-justified correction;
never rerun flakes or renew the matrix automatically.

Further consideration needs both meaningful positive **net** allocation savings
and compatible native lifecycle behavior. Missing/failing capabilities produce
NO-GO, with the exact owner/action needed for a separately authorized revisit.
No plugin scope, install, production mutation, automatic adoption or upstream
publication is authorized by this fixture. Revert or intentionally close the
source-only PR to remove the harness; no runtime rollback is implied.

REA angle: inspect the pinned project's README, reference, provider contract,
overlay recovery and locking source first; exercise those native mechanisms
instead of inventing an allocator or copying an implementation.
