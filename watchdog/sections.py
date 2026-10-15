"""Snapshot section readers for the platform watchdog (case_a8404a2d).

``detectors.py`` promises that a snapshot gap emits ``unknown`` instead of a
silent pass. A MISSING section always did. A section of the wrong type
(``agents: "oops"``, ``redMains: {}``) read as zero rows, a section that is
not iterable (``agents: 5``) crashed the CLI with a traceback, and a row with
an unreadable number (``diskPct: "lots"``) was skipped without a word. This
module is the one place those cases are handled, so every detector reads its
rows the same way and a new detector gets the guarantee by calling it.

Pure functions and one small class: no I/O, no clock, no network. Findings
are built by the caller's ``_finding`` so this module never needs to import
``detectors`` (which imports it).

Aggregate findings carry counts and the section name only. They never echo a
row, a field value or a key from the snapshot: secret-hit rows are among the
sections read here, and a malformed row may be the secret itself.
"""

from __future__ import annotations

import math


def read_float(value):
    """Return ``value`` as a finite float, or None when it is unreadable.

    ``float()`` alone accepts ``True`` (1.0), ``NaN`` and ``Infinity`` (Python's
    JSON decoder emits both). All three then fail every threshold comparison
    and pass silently, so they count as unreadable here.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def read_int(value):
    """Return ``value`` as an int, or None when it is unreadable.

    Same ``int()`` coercion the detectors always used (``3.9`` -> 3, ``"3"`` ->
    3), minus the cases that used to raise or lie: ``True`` is not a count,
    and ``int(float("inf"))`` raised an uncaught ``OverflowError``.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


class SectionRead:
    """The rows of one snapshot section, plus a tally of what could not be read.

    ``value`` is whatever the snapshot holds under ``section``. ``None`` means
    the section is absent; the caller decides whether absence is a gap (each
    detector already emits its own ``... snapshot missing`` finding), so a
    ``None`` reads as no rows and no complaint here. Anything else that is not
    a list is a mistyped section. Iterating yields dict rows only; a row that
    is not an object counts as unreadable instead of vanishing.

    A detector that finds a row it cannot judge (an unreadable number, a
    missing key) calls ``flag()`` while looping over it; flagging the same
    row twice counts once, and two equal rows count twice. ``unknown()``
    turns the tally into at most one finding per section.
    """

    def __init__(self, section, value):
        self.section = section
        self._absent = value is None
        self._is_list = isinstance(value, list)
        self.found = None if self._is_list or self._absent else type(value).__name__
        rows = value if self._is_list else []
        self.rows = [row for row in rows if isinstance(row, dict)]
        self.total = len(rows)
        self._at = None
        self._flagged = set()
        self._non_dict = self.total - len(self.rows)

    def __iter__(self):
        for position, row in enumerate(self.rows):
            self._at = position
            yield row

    def flag(self):
        """Mark the row currently being read as unreadable."""
        if self._at is None:
            raise RuntimeError("flag() is only valid while iterating rows")
        self._flagged.add(self._at)

    @property
    def unreadable(self):
        return self._non_dict + len(self._flagged)

    def unknown(self, finding, detector, owner, route):
        """Return the ``unknown`` finding for this section, as a list of 0 or 1.

        ``finding`` is the caller's ``_finding`` builder. A mistyped section
        and unreadable rows are both reported, but never more than once each.
        """
        if self.found is not None:
            return [finding(
                detector, f"{self.section} snapshot unreadable", "unknown",
                owner, route,
                {"section": self.section, "found": self.found},
                f"supply {self.section} as a list of objects; "
                "no verdict without it")]
        if self.unreadable:
            return [finding(
                detector, f"{self.section} rows unreadable", "unknown",
                owner, route,
                {"section": self.section, "rows": self.total,
                 "unreadableRows": self.unreadable},
                f"fix the {self.section} adapter; the unreadable rows "
                "got no verdict")]
        return []
