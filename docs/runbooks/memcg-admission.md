# Container memory admission and test-worker budget

Why: a 40 GiB container cap was exhausted by parallel browser and test trees while the host still had
tens of GiB free, so host PSI never tripped and the kernel killed 16 processes. `memory.current`
cannot be the signal: it includes page cache and sits near `memory.max` on a healthy container
(observed: current about 99% of max with anon about 14.8 GiB of 40 GiB).

Signal (`ops/lib/memcg_pressure.py`): `(anon + shmem + slab_unreclaimable + kernel_stack) / memory.max`
held at 0.60 or more, or cgroup `memory.pressure` full avg60 above 5. Unreadable telemetry fails closed.
`python3 ops/lib/memcg_pressure.py` prints the verdict and how many 3 GiB test trees still fit
(`workersFit`, a memory budget that is 0 whenever the verdict is a hold); size local `--workers` / `PW_WORKERS` from it. Workers must stay strictly below the denial line: at 15 GiB unreclaimable
of a 40 GiB cap, two 3 GiB workers fit, not three. Exit 0 allowed, 1 held, 2 unreadable.

Consumers: `ops/productivity/host_ok.py` (wake-producing reconcilers) reads the gate when
`PAPERCLIP_MEMCG_DIR` is set. Any other pacer that already owns admission can import
`read_memcg` / `evaluate_memcg` instead of adding a second pacer.

## Rollout
1. Merge: inert. With `PAPERCLIP_MEMCG_DIR` unset nothing changes.
2. Shadow: set `PAPERCLIP_MEMCG_DIR=<container cgroup dir>` and `PAPERCLIP_MEMCG_MODE=shadow` in the
   reconciler units. Holds are logged to stderr, never enforced. Compare against kernel OOM events
   for a few days and adjust `DENY_RATIO` (with its pinned test) if needed.
3. Enforce: drop `PAPERCLIP_MEMCG_MODE`.

## Undo
Unset `PAPERCLIP_MEMCG_DIR` (restores previous behaviour instantly) or revert the PR.
