"""Container-aware (cgroup v2) memory admission and test-worker budget.

Host PSI (host_pressure.py) cannot see a container that hits its own memory cap while the host has
tens of GiB free. memory.current is also the wrong signal: it includes reclaimable page cache and
sits near memory.max on a healthy busy container. What the OOM killer cannot reclaim is anonymous
memory, shmem (tmpfs), unreclaimable slab and kernel stacks, so the guard is
(anon + shmem + slab_unreclaimable + kernel_stack) / memory.max, plus the cgroup's own memory
PSI. Missing or malformed telemetry fails closed. An uncapped cgroup (no memory.max) is passed without
looking at PSI: the host-wide gate in host_pressure.py covers it.

CLI: python3 memcg_pressure.py [--dir DIR] [--per-worker-gib N]
  prints one JSON verdict (with the number of test workers that still fit); exit 0 allowed,
  1 denied, 2 telemetry unreadable.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

DENY_RATIO = 0.60          # unreclaimable share of memory.max at which new local work is held
PSI_FULL_MAX = 5.0         # cgroup memory.pressure full avg60, same bound as host_pressure
PER_WORKER_BYTES = 3 * 1024 ** 3   # largest observed browser/test tree (~3.3 GiB RSS, shared pages included)
DEFAULT_DIR = "/sys/fs/cgroup"
_STAT_FIELDS = ("anon", "shmem", "slab_unreclaimable", "kernel_stack")


class MemcgError(ValueError):
    """A required cgroup measurement is missing or malformed."""


def _number(text: str, what: str) -> int:
    try:
        value = int(text.strip())
    except (ValueError, AttributeError) as error:
        raise MemcgError(f"{what} is malformed") from error
    if value < 0:
        raise MemcgError(f"{what} is negative")
    return value


def _stat(text: str) -> dict[str, int]:
    rows: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in _STAT_FIELDS:
            if key in rows:
                raise MemcgError(f"memory.stat {key} is duplicated")
            rows[key] = _number(value, f"memory.stat {key}")
    missing = [key for key in _STAT_FIELDS if key not in rows]
    if missing:
        raise MemcgError(f"memory.stat is missing {', '.join(missing)}")
    return rows


def _psi_full_avg60(text: str) -> float:
    rows = [line.split() for line in text.splitlines() if line.startswith("full ")]
    if len(rows) != 1:
        raise MemcgError("memory.pressure full row is missing or duplicated")
    fields: dict[str, str] = {}
    for item in rows[0][1:]:
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in fields:
            raise MemcgError("memory.pressure row is malformed")
        fields[key] = value
    try:
        value = float(fields["avg60"])
    except (KeyError, ValueError) as error:
        raise MemcgError("memory.pressure avg60 is missing or malformed") from error
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise MemcgError("memory.pressure avg60 is outside 0..100")
    return value


def read_memcg(cgroup_dir: str = DEFAULT_DIR,
               read_text: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Read one fresh sample; `limit` is None when the cgroup has no memory.max."""
    reader = read_text or (lambda path: Path(path).read_text(encoding="ascii"))
    base = cgroup_dir.rstrip("/")
    try:
        raw_limit = reader(f"{base}/memory.max").strip()
        limit = None if raw_limit == "max" else _number(raw_limit, "memory.max")
        if limit == 0:
            raise MemcgError("memory.max is zero")
        sample: dict[str, Any] = {
            "limit": limit,
            "current": _number(reader(f"{base}/memory.current"), "memory.current"),
            "psiFullAvg60": _psi_full_avg60(reader(f"{base}/memory.pressure")),
            **_stat(reader(f"{base}/memory.stat")),
        }
    except MemcgError:
        raise
    except (OSError, UnicodeError, TypeError) as error:
        raise MemcgError("cgroup memory telemetry is unreadable") from error
    return sample


def evaluate_memcg(sample: Any, per_worker_bytes: int = PER_WORKER_BYTES) -> dict[str, Any]:
    """Return admission, reason and how many more `per_worker_bytes` test trees fit below the deny line.

    `workersFit` is a memory budget only: it is 0 whenever the verdict is a hold, and None when uncapped.
    """
    if isinstance(per_worker_bytes, bool) or not isinstance(per_worker_bytes, int) or per_worker_bytes <= 0:
        raise MemcgError("per_worker_bytes must be a positive integer")
    if not isinstance(sample, dict):
        raise MemcgError("memcg sample is not an object")
    limit = sample.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise MemcgError("limit is invalid")
    psi = sample.get("psiFullAvg60")
    if isinstance(psi, bool) or not isinstance(psi, (int, float)) or not 0 <= psi <= 100:
        raise MemcgError("psiFullAvg60 is invalid")
    parts = []
    for field in _STAT_FIELDS:
        value = sample.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MemcgError(f"{field} is invalid")
        parts.append(value)
    unreclaimable = sum(parts)
    if limit is None:
        return {"allowed": True, "reason": "uncapped", "unreclaimable": unreclaimable,
                "ratio": None, "workersFit": None}
    ratio = unreclaimable / limit
    headroom = int(DENY_RATIO * limit) - unreclaimable
    reason = ("memcg_psi" if psi > PSI_FULL_MAX else
              "memcg_unreclaimable" if ratio >= DENY_RATIO else "headroom")
    fit = max(0, headroom - 1) // per_worker_bytes if reason == "headroom" else 0
    return {"allowed": reason == "headroom", "reason": reason, "unreclaimable": unreclaimable,
            "ratio": round(ratio, 4), "workersFit": fit}


def main(argv: list[str]) -> int:
    args = list(argv)
    cgroup_dir, per_worker = DEFAULT_DIR, PER_WORKER_BYTES
    try:
        while args:
            flag = args.pop(0)
            if flag == "--dir":
                cgroup_dir = args.pop(0)
            elif flag == "--per-worker-gib":
                per_worker = int(float(args.pop(0)) * 1024 ** 3)
                if per_worker <= 0:
                    raise ValueError
            else:
                raise ValueError
        verdict = evaluate_memcg(read_memcg(cgroup_dir), per_worker)
    except MemcgError as error:
        print(json.dumps({"allowed": False, "reason": "unreadable", "error": str(error)}))
        return 2
    except (IndexError, ValueError, OverflowError):
        print("usage: memcg_pressure.py [--dir DIR] [--per-worker-gib N]", file=sys.stderr)
        return 2
    print(json.dumps(verdict))
    return 0 if verdict["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
