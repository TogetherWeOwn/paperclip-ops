#!/usr/bin/env python3
# ===========================================================================
# claude_countdown.py — Claude-lane use-before-expiry countdown readout
# (case_586352f5, parent epic case_9e33a5c2; mirror of the case_862c218c Muse readout and
# the case_59a1ee73 Codex readout, both in review on feat/muse-countdown and
# feat/codex-countdown, NOT on main — no shared code)
#
# SCOPE: read-only. Per each of the 2 Claude weekly lanes, show remaining
# weekly headroom % and hours-to-reset, from a JSON fixture through a pure
# assemble/display function. No roster writes, no pin changes, no enforce
# flip — importing or running this module changes no pacing behaviour.
# Slices around case_3097ed32/case_d5f0e2f5 (blocked on open reviews): no pacing
# change, no pin writes, no stale-pin cleanup here.
#
# DELTAS FROM THE CODEX PATTERN (deliberate, all pinned by tests):
#   * 2 lanes, not 3. The snapshot's Claude rows govern on `seven_day`
#     (seven_day_utilization / seven_day_resets_at); the fixture re-expresses
#     them as weekly countdown rows under the same field names the Muse/Codex
#     fixtures use, so the assemble/display contract is unchanged.
#   * No single shared reset instant: the two committed Claude rows reset
#     ~153 h apart (2026-10-03T09:59:59Z vs 2026-10-09T19:00:00Z), so there
#     is no WEEKLY_RESET_UTC constant and the header names no reset — each
#     row's own resetsAt is that lane's truth, and the per-lane test pins
#     two distinct hours-left values (~6.0 vs ~159.0). At the card clock the
#     thinnest lane (claude-lane-2, 7.0% headroom) is also the most urgent.
#   * The snapshot carries fractional-second resets (.443239Z / .666254Z);
#     they are kept verbatim in the fixture and the parser accepts %f, so
#     the pinned hours-left values keep their sub-second fractions.
#   * No fixture lane sits at utilization 1.0 (both lanes live at 0.55 /
#     0.93). The 1.0-renders-0.0% contract is still pinned by test (§7) so
#     the refuse-the-full-lane mutant dies here too.
#   * The unused EXPECTED_LANE_COUNT constant the Muse module declares is
#     dropped here — nothing reads it there either.
#
# WHAT THIS REFUSES, AND WHY (same contract as the Muse/Codex readouts)
#   * Missing, non-numeric, NaN/Inf, or out-of-[0,1] utilization is a
#     CountdownError naming the lane — never substituted with zero. A null
#     reading scored as zero would print 100% headroom on a lane that may be
#     exhausted (the `[[ "" -eq 0 ]]` bash trap the cold-start detector
#     exists for, in another costume).
#   * A missing, unparseable, or already-past reset is a CountdownError, not
#     "0 hours left". Numbers that describe a window that is over are the
#     case_5b628ce6 failure verbatim.
#   * Lanes are never averaged: rows sharing one weekly window still emit
#     one row per lane. A mean would hide the thinnest lane, which is the
#     one the countdown exists to surface.
#   * Duplicate lane names are refused: two rows for one lane would let a
#     stale row shadow the live one in the display sort.
#
# EXIT CODES (cli)
#   0  readout printed
#   2  usage / validation: bad fixture, bad --now, or a refused lane row
# With --report-json: 0 only for a complete report, otherwise 2;
# the one diagnostic object is printed instead of the readout.
#
# Read-only. Opens one JSON fixture, prints to stdout. No network, no
# credential, no roster read, no file written.
# ===========================================================================
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import math
import os
import sys

UTC = datetime.timezone.utc

_TS_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ")


class CountdownError(ValueError):
    """A lane row (or clock/fixture) that cannot be rendered honestly."""


def parse_ts(value):
    """Strict UTC instant or None. No offsetless or free-text timestamps."""
    if not isinstance(value, str) or not value:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _lane_utilization(row):
    lane = row.get("lane") if isinstance(row, dict) else None
    name = lane if isinstance(lane, str) and lane else "?"
    if not isinstance(row, dict):
        raise CountdownError("lane %s: row is not an object" % name)
    if not isinstance(lane, str) or not lane:
        raise CountdownError("lane ?: missing or empty lane name")
    raw = row.get("weekly_utilization")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
            or not math.isfinite(raw):
        raise CountdownError(
            "lane %s: weekly_utilization is not a finite number" % lane)
    if raw < 0 or raw > 1:
        raise CountdownError(
            "lane %s: weekly_utilization %r outside [0, 1]" % (lane, raw))
    return lane, float(raw)


def _lane_reset(row, lane, now):
    raw = row.get("weekly_reset_utc")
    reset = parse_ts(raw)
    if reset is None:
        raise CountdownError(
            "lane %s: weekly_reset_utc %r is not a UTC "
            "YYYY-MM-DDTHH:MM:SSZ instant" % (lane, raw))
    if reset <= now:
        raise CountdownError(
            "lane %s: weekly_reset_utc %s is not after now %s -- the "
            "window is over, its numbers describe nothing" % (
                lane, raw, now.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return reset, raw if isinstance(raw, str) else reset.strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def assemble_rows(lanes, now):
    """Pure: one countdown row per lane. Input order preserved; no sorting,
    no averaging, no I/O. Raises CountdownError on the first dishonest row."""
    if not isinstance(lanes, list):
        raise CountdownError("lanes is not an array")
    if not isinstance(now, datetime.datetime):
        raise CountdownError("now is not a datetime")
    seen = set()
    rows = []
    for entry in lanes:
        lane, utilization = _lane_utilization(entry)
        if lane in seen:
            raise CountdownError("lane %s: duplicate lane row" % lane)
        seen.add(lane)
        reset, reset_raw = _lane_reset(entry, lane, now)
        headroom_pct = (1.0 - utilization) * 100.0
        hours_to_reset = (reset - now).total_seconds() / 3600.0
        rows.append({
            "lane": lane,
            "weeklyUtilization": utilization,
            "headroomPct": headroom_pct,
            "hoursToReset": hours_to_reset,
            "resetsAt": reset_raw,
        })
    return rows


def format_table(rows):
    """Pure display: fixed-width table, thinnest headroom first (the
    use-before-expiry order). Sorts a copy; the input list is untouched."""
    ordered = sorted(rows, key=lambda r: (r["headroomPct"], r["lane"]))
    lines = ["%-14s %7s %9s %12s %s" % (
        "lane", "used%", "headroom%", "hours-left", "resets-at")]
    for row in ordered:
        lines.append("%-14s %6.1f%% %8.1f%% %12.2f %s" % (
            row["lane"], row["weeklyUtilization"] * 100.0,
            row["headroomPct"], row["hoursToReset"], row["resetsAt"]))
    return "\n".join(lines) + "\n"


def load_fixture(path):
    """Read the fixture file. The only I/O in this module."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), list):
        raise CountdownError(
            "fixture %s has no lanes array" % path)
    return doc


_REPORT_PROVIDER = "claude"
_REPORT_SCHEMA_ID = "countdown-report.v1"
_REPORT_DISCLAIMER = (
    "Diagnostic observations only; not admission or readiness authorization."
)

# code -> (retryable, next_action). The same table is mirrored in the other
# countdown readouts; the acceptance suite pins the correspondence.
_DIAGNOSTIC_ACTIONS = {
    "invalid_lane_record": (False, "provide_lane_object"),
    "invalid_lane_identity": (False, "provide_lane_identity"),
    "duplicate_lane": (False, "disambiguate_lane_identity"),
    "invalid_utilization": (False, "provide_finite_utilization"),
    "invalid_reset": (False, "provide_valid_reset"),
    "expired_reset": (True, "provide_current_window"),
    "empty_input": (False, "provide_lane_records"),
    "invalid_now": (False, "provide_valid_utc_now"),
    "usage_error": (False, "correct_arguments"),
    "fixture_unreadable": (True, "provide_readable_fixture"),
    "fixture_encoding": (False, "provide_utf8_fixture"),
    "fixture_json": (False, "provide_valid_json"),
    "fixture_shape": (False, "provide_lane_list"),
}


class _UsageError(Exception):
    """Argparse refused report-mode arguments. Carries no raw input."""


class _ReportArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that refuses through _UsageError, never stderr."""

    def error(self, message=None):
        raise _UsageError()

    def exit(self, status=0, message=None):
        raise _UsageError()


def _format_as_of(now):
    """UTC instant in the fixture timestamp form, keeping fractions."""
    if now.microsecond:
        return now.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _diagnostic(code, lane, source_index):
    retryable, next_action = _DIAGNOSTIC_ACTIONS[code]
    return {
        "code": code,
        "lane": lane,
        "source_index": source_index,
        "retryable": retryable,
        "next_action": next_action,
    }


def _make_report(as_of, outcome, counts, observations, diagnostics):
    return {
        "schema_id": _REPORT_SCHEMA_ID,
        "provider": _REPORT_PROVIDER,
        "readout": _REPORT_PROVIDER + "_countdown",
        "as_of": _format_as_of(as_of) if as_of is not None else None,
        "outcome": outcome,
        "check_complete": outcome == "complete",
        "counts": counts,
        "observations_partial": outcome == "incomplete",
        "observations": observations,
        "diagnostics": diagnostics,
        "disclaimer": _REPORT_DISCLAIMER,
    }


def _unmeasurable_report(code, as_of):
    """No lane could be evaluated: aggregate counts stay unknown (null)."""
    return _make_report(
        as_of, "unmeasurable",
        {"input": None, "evaluated": None, "invalid": None},
        [], [_diagnostic(code, None, None)])


def _emit_report(report):
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["check_complete"] else 2


def _report_clock_from_argv(raw_argv):
    """Last explicitly valid --now in raw arguments, or None.

    Usage refusals use this so a valid explicit clock is retained as
    as_of and no clock is invented when none was validly supplied.
    """
    candidate = None
    index = 0
    while index < len(raw_argv):
        token = raw_argv[index]
        if token == "--now" and index + 1 < len(raw_argv):
            parsed = parse_ts(raw_argv[index + 1])
            if parsed is not None:
                candidate = parsed
            index += 2
            continue
        if isinstance(token, str) and token.startswith("--now="):
            parsed = parse_ts(token[len("--now="):])
            if parsed is not None:
                candidate = parsed
        index += 1
    return candidate


def _load_report_fixture(path):
    """Report-mode fixture read: (error_code, lanes).

    Exactly one of the pair is None. Error codes carry no path or raw
    input; the legacy load_fixture path below is unchanged.
    """
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return "fixture_unreadable", None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return "fixture_encoding", None
    def _reject_duplicate_keys(pairs):
        seen = set()
        doc = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate key")
            seen.add(key)
            doc[key] = value
        return doc

    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):
        return "fixture_json", None
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), list):
        return "fixture_shape", None
    return None, doc["lanes"]


def _classify_lanes(lanes, now):
    """Report-mode evaluation: every record is checked, nothing aborts.

    Mirrors the strict validators above field by field (same parse_ts,
    same utilization range, same row math as assemble_rows), but each
    defect becomes a diagnostic instead of the first one refusing the
    whole readout. Duplicate identities invalidate every member.
    Returns (observations, diagnostics, invalid_record_count).
    """
    identity_counts = {}
    for entry in lanes:
        if isinstance(entry, dict):
            name = entry.get("lane")
            if isinstance(name, str) and name:
                identity_counts[name] = identity_counts.get(name, 0) + 1
    duplicate_names = {name for name, count in identity_counts.items()
                       if count > 1}
    observations = []
    diagnostics = []
    invalid_indices = set()
    for index, entry in enumerate(lanes):
        if not isinstance(entry, dict):
            diagnostics.append(
                _diagnostic("invalid_lane_record", None, index))
            invalid_indices.add(index)
            continue
        name = entry.get("lane")
        identity_valid = isinstance(name, str) and bool(name)
        lane = name if identity_valid else None
        record_diagnostics = []
        if not identity_valid:
            record_diagnostics.append(
                _diagnostic("invalid_lane_identity", None, index))
        elif name in duplicate_names:
            record_diagnostics.append(
                _diagnostic("duplicate_lane", name, index))
        utilization = None
        raw_utilization = entry.get("weekly_utilization")
        if isinstance(raw_utilization, bool) \
                or not isinstance(raw_utilization, (int, float)) \
                or (isinstance(raw_utilization, float)
                    and not math.isfinite(raw_utilization)) \
                or raw_utilization < 0 or raw_utilization > 1:
            record_diagnostics.append(
                _diagnostic("invalid_utilization", lane, index))
        else:
            utilization = float(raw_utilization)
        reset = None
        raw_reset = entry.get("weekly_reset_utc")
        parsed_reset = parse_ts(raw_reset)
        if parsed_reset is None:
            record_diagnostics.append(
                _diagnostic("invalid_reset", lane, index))
        elif parsed_reset <= now:
            record_diagnostics.append(
                _diagnostic("expired_reset", lane, index))
        else:
            reset = parsed_reset
        diagnostics.extend(record_diagnostics)
        if record_diagnostics:
            invalid_indices.add(index)
            continue
        observations.append({
            "lane": name,
            "weeklyUtilization": utilization,
            "headroomPct": (1.0 - utilization) * 100.0,
            "hoursToReset": (reset - now).total_seconds() / 3600.0,
            "resetsAt": raw_reset,
        })
    return observations, diagnostics, len(invalid_indices)


def _build_report(lanes, now):
    if not lanes:
        # Known-empty input is unmeasurable, with known zero counts.
        return _make_report(
            now, "unmeasurable",
            {"input": 0, "evaluated": 0, "invalid": 0},
            [], [_diagnostic("empty_input", None, None)])
    observations, diagnostics, invalid = _classify_lanes(lanes, now)
    outcome = "incomplete" if invalid else "complete"
    counts = {"input": len(lanes), "evaluated": len(lanes),
              "invalid": invalid}
    return _make_report(now, outcome, counts, observations, diagnostics)


def _build_parser(parser_cls=argparse.ArgumentParser, add_help=True):
    default_fixture = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "tests", "fixtures", "case_3b3ee3d8", "claude-weekly-lanes.json")
    p = parser_cls(
        description="Claude-lane use-before-expiry countdown readout "
                    "(read-only).",
        add_help=add_help)
    p.add_argument("--fixture", default=default_fixture)
    p.add_argument("--now",
                   help="override current UTC time (testing), e.g. "
                        "2026-10-03T04:00:00Z")
    p.add_argument("--json", action="store_true",
                   help="emit the assembled rows as JSON instead of the "
                        "human table")
    p.add_argument("--report-json", action="store_true",
                   help="emit one countdown-report.v1 diagnostic object "
                        "covering every lane record instead of the "
                        "strict readout; mutually exclusive with --json")
    return p


def _report_main(raw_argv):
    """Opt-in diagnostic report: one safe JSON object, exit 0 iff the
    evaluation is complete, otherwise 2. Errors live in the report,
    never on stderr, and raw input values are never echoed."""
    p = _build_parser(parser_cls=_ReportArgumentParser, add_help=False)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            a = p.parse_args(raw_argv)
    except _UsageError:
        return _emit_report(_unmeasurable_report(
            "usage_error", _report_clock_from_argv(raw_argv)))
    if a.json or not a.report_json:
        clock = parse_ts(a.now) if a.now is not None else None
        return _emit_report(_unmeasurable_report("usage_error", clock))
    if a.now is not None:
        now = parse_ts(a.now)
        if now is None:
            return _emit_report(_unmeasurable_report("invalid_now", None))
    else:
        now = datetime.datetime.now(UTC).replace(microsecond=0)
    code, lanes = _load_report_fixture(a.fixture)
    if code is not None:
        return _emit_report(_unmeasurable_report(code, now))
    return _emit_report(_build_report(lanes, now))


def main(argv=None):
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    if any(token == "--report-json"
           or (isinstance(token, str)
               and token.startswith("--report-json="))
           for token in raw_argv):
        return _report_main(raw_argv)
    p = _build_parser()
    a = p.parse_args(raw_argv)
    if a.report_json:
        # Reached only through an argparse long-option abbreviation.
        return _report_main(raw_argv)

    if a.now is not None:
        now = parse_ts(a.now)
        if now is None:
            p.error("--now must be UTC in YYYY-MM-DDTHH:MM:SSZ form")
    else:
        now = datetime.datetime.now(UTC).replace(microsecond=0)

    try:
        doc = load_fixture(a.fixture)
        rows = assemble_rows(doc["lanes"], now)
    except (OSError, json.JSONDecodeError) as exc:
        print("claude_countdown: cannot read fixture: %s" % exc,
              file=sys.stderr)
        return 2
    except CountdownError as exc:
        print("claude_countdown: refused: %s" % exc, file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        # No single shared reset to name here (see module header): the
        # per-lane resets-at column below is each lane's truth.
        print("Claude weekly countdown (now %s, %d lanes)" % (
            now.strftime("%Y-%m-%dT%H:%M:%SZ"), len(rows)))
        print(format_table(rows), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
