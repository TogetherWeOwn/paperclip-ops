"""Fail-closed Linux PSI and load-average admission for host recovery writes."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

CPU_SOME_MAX = 30.0
IO_SOME_MAX = 30.0
MEM_FULL_MAX = 5.0
LOAD5_HARD_MAX = 96.0


class PressureError(ValueError):
    """A required pressure measurement is missing or malformed."""


def _parse_psi(kind: str, line_name: str, text: str) -> float:
    if not isinstance(text, str):
        raise PressureError(f"{kind} PSI is unreadable")
    rows = [line.split() for line in text.splitlines()
            if line.startswith(f"{line_name} ")]
    if len(rows) != 1:
        raise PressureError(f"{kind} PSI {line_name} row is missing or duplicated")

    fields: dict[str, str] = {}
    for item in rows[0][1:]:
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in fields:
            raise PressureError(f"{kind} PSI row is malformed")
        fields[key] = value
    if "avg60" not in fields:
        raise PressureError(f"{kind} PSI avg60 is missing")
    try:
        value = float(fields["avg60"])
    except ValueError as error:
        raise PressureError(f"{kind} PSI avg60 is malformed") from error
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise PressureError(f"{kind} PSI avg60 is outside 0..100")
    return value


def _parse_loadavg(text: str) -> tuple[float, float, float]:
    if not isinstance(text, str):
        raise PressureError("load averages are unreadable")
    fields = text.split()
    if len(fields) < 3:
        raise PressureError("load averages are incomplete")
    try:
        loads = tuple(float(value) for value in fields[:3])
    except ValueError as error:
        raise PressureError("load averages are malformed") from error
    if any(not math.isfinite(value) or value < 0 for value in loads):
        raise PressureError("load averages are outside the valid range")
    return loads


def read_pressure(read_text: Callable[[str], str] | None = None) -> dict[str, float]:
    """Read a fresh PSI/load sample; missing or corrupt evidence never means zero."""
    reader = read_text or (lambda path: Path(path).read_text(encoding="ascii"))
    try:
        cpu = _parse_psi("cpu", "some", reader("/proc/pressure/cpu"))
        io = _parse_psi("io", "some", reader("/proc/pressure/io"))
        memory = _parse_psi("memory", "full", reader("/proc/pressure/memory"))
        load1, load5, load15 = _parse_loadavg(reader("/proc/loadavg"))
    except PressureError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, IndexError, KeyError) as error:
        raise PressureError("host pressure telemetry is unreadable") from error
    return {
        "cpuSomeAvg60": cpu,
        "ioSomeAvg60": io,
        "memoryFullAvg60": memory,
        "load1": load1,
        "load5": load5,
        "load15": load15,
    }


def evaluate_pressure(sample: Any) -> dict[str, Any]:
    """Return admission and reason using PSI stall time plus the load5 backstop."""
    fields = ("cpuSomeAvg60", "ioSomeAvg60", "memoryFullAvg60", "load1", "load5", "load15")
    if not isinstance(sample, dict):
        raise PressureError("pressure sample is not an object")
    values: dict[str, float] = {}
    for field in fields:
        value = sample.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PressureError(f"{field} is missing or invalid")
        try:
            numeric = float(value)
        except (OverflowError, ValueError) as error:
            raise PressureError(f"{field} is missing or invalid") from error
        if not math.isfinite(numeric) or numeric < 0:
            raise PressureError(f"{field} is missing or invalid")
        values[field] = numeric
    for field in fields[:3]:
        if values[field] > 100:
            raise PressureError(f"{field} is outside 0..100")

    reason = ("cpu" if values["cpuSomeAvg60"] > CPU_SOME_MAX else
              "io" if values["ioSomeAvg60"] > IO_SOME_MAX else
              "memory_pressure" if values["memoryFullAvg60"] > MEM_FULL_MAX else
              "load5" if values["load5"] > LOAD5_HARD_MAX else "headroom")
    return {**values, "allowed": reason == "headroom", "reason": reason}
