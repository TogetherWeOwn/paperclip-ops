#!/usr/bin/env python3
"""Alarm when fleet-quota-balancer.log goes quiet (case_7edd6ab6).

Residual risk #1 from the case_a3bb3e07 closeout: the host fleet-quota-balancer
logged nothing from 2026-10-04 14:14:31Z to 17:28:14Z (193 min) while the
fleet default sat on a depleted lane, and nothing alarmed on the silence.

Read-only, stdlib only, no credentials. Reads JSON-lines rows shaped like
{"at": "<iso>", "model": "<name>", "reason": "<why>", ...} at about one row
per minute, plus the sibling .state.json for context.

Exit codes:
  0  the last row is newer than --max-age-min (fresh), or the log is stale
     but --retirement-file names an explicit valid retirement (retired)
  1  the last row is older than --max-age-min (stale: the balancer is quiet)
  2  unmeasurable: log missing, empty, or with no parseable row; a bad --now
     or --max-age-min. Unmeasurable is never reported as fresh.

An explicitly retired producer is not a live outage: when the log would
otherwise read stale, a valid retirement file converts the verdict to
RETIRED (exit 0). The file must be JSON naming {"retired": true, the
producer id, a successor, an approver and an approval instant at or before
--now. A missing, unreadable, malformed, future-dated or mismatched file
is ignored with a stderr note and the stale verdict stands: mere
inactivity is never healthy. A live log stays FRESH and an unmeasurable
log stays UNMEASURABLE regardless of the file, which is only consulted on
the stale path. The guard never restarts, re-enables or writes to the
producer, the model roster or any routing state.

Every gap longer than the threshold inside the trailing --window-h hours is
printed with the model and reason on each side, so a replay shows the
14:14:31Z -> 17:28:14Z gap.

With --json, stdout is exactly one JSON document (schema_version 1)
instead of the prose above; exit codes and stderr notes are unchanged,
and text output without the flag is byte-identical. The closed envelope
keys are: schema_version (int), tool, verdict
(fresh|stale|retired|unmeasurable), producer, now (ISO instant, or null
only when --now itself is unparseable), threshold_min, window_h, age_min
(null when unmeasurable), last_row (null, or {at, model, reason}),
state ({summary}, the same text the prose mode prints), gaps (bounded
to GAP_CAP=10 in chronological order; each {start, end, duration_min,
left {model, reason}, right {model, reason}}), gaps_truncated (count
omitted by the cap), retirement ({consulted, accepted, detail};
consulted only on the stale path, detail carries the same refusal note
the prose mode puts on stderr), error (null, or {code, detail} when
unmeasurable; codes: log_missing, log_unreadable,
log_no_parseable_row, log_no_row_before_now, bad_now, bad_max_age_min,
bad_window_h) and skipped_lines. JSON carries instants, models, reasons
and counts only: no other log fields are echoed.

Synthetic example:
{"schema_version": 1, "tool": "balancer_silence_guard.py",
 "verdict": "fresh", "producer": "fleet-quota-balancer",
 "now": "2026-10-05T05:00:00+00:00", "threshold_min": 10.0,
 "window_h": 24.0, "age_min": 0.0,
 "last_row": {"at": "2026-10-05T05:00:00+00:00", "model": "model-a",
 "reason": "reason-x"},
 "state": {"summary": "state: evaluated_at=? model=?"},
 "gaps": [], "gaps_truncated": 0,
 "retirement": {"consulted": false, "accepted": false,
 "detail": "not consulted (only checked when stale)"},
 "error": null, "skipped_lines": 0}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

LOG_DEFAULT = "/paperclip/shared/host-bridge/fleet-quota-balancer.log"
STATE_DEFAULT = "/paperclip/shared/host-bridge/fleet-quota-balancer.state.json"
MAX_AGE_MIN_DEFAULT = 10.0
WINDOW_H_DEFAULT = 24.0
PRODUCER_ID = "fleet-quota-balancer"
REQUIRED_RETIREMENT_FIELDS = ("producer", "successor", "approved_by", "approved_at")
SCHEMA_VERSION = 1
TOOL = "balancer_silence_guard.py"
GAP_CAP = 10


class Unmeasurable(RuntimeError):
    """The log cannot answer whether the balancer is alive."""

    def __init__(self, message, code="log_no_parseable_row", skipped=0):
        super().__init__(message)
        self.code = code
        self.skipped = skipped


def parse_instant(value):
    """Parse one ISO-8601 instant into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("instant is not a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparseable instant {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def load_rows(path):
    """Return [(instant, model, reason)] in file order; warn on skipped lines.

    Raises Unmeasurable when the file is missing or yields no parseable row.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError as exc:
        raise Unmeasurable(f"log missing: {path}",
                           code="log_missing") from exc
    except OSError as exc:
        raise Unmeasurable(f"log unreadable: {path}: {type(exc).__name__}",
                           code="log_unreadable") from exc
    rows = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            moment = parse_instant(entry.get("at"))
        except (ValueError, AttributeError, TypeError, RecursionError):
            skipped += 1
            continue
        model = entry.get("model")
        reason = entry.get("reason")
        rows.append((
            moment,
            model if isinstance(model, str) and model else "?",
            reason if isinstance(reason, str) and reason else "?",
        ))
    if not rows:
        raise Unmeasurable(f"log has no parseable row: {path} (skipped {skipped})",
                           code="log_no_parseable_row", skipped=skipped)
    rows.sort(key=lambda row: row[0])
    return rows, skipped


def read_state(path):
    """Best-effort context from the sibling state file; never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return "state: missing (context only; the log is the signal)"
    except (OSError, ValueError, RecursionError) as exc:
        return f"state: unreadable ({type(exc).__name__}; context only)"
    if not isinstance(state, dict):
        return "state: unreadable (not an object; context only)"
    try:
        moment = parse_instant(state.get("evaluated_at"))
        stamp = moment.isoformat()
    except ValueError:
        stamp = "?"
    model = state.get("model")
    return f"state: evaluated_at={stamp} model={model if model else '?'}"


def read_retirement(path, producer, now):
    """Check explicit retirement evidence; never raises.

    Return (True, detail) only when the file is a JSON object with
    {"retired": true}, the expected producer id, a non-empty successor,
    approver and approval instant, and the approval is at or before now.
    Every other shape returns (False, reason): a bad file is ignored
    evidence, never a healthy verdict and never a fatal error.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except FileNotFoundError:
        return False, "retirement file missing"
    except (OSError, ValueError, RecursionError) as exc:
        return False, f"retirement file unreadable ({type(exc).__name__})"
    if not isinstance(marker, dict):
        return False, "retirement file is not an object"
    if marker.get("retired") is not True:
        return False, "retired is not true"
    for field in REQUIRED_RETIREMENT_FIELDS:
        value = marker.get(field)
        if not isinstance(value, str) or not value.strip():
            return False, f"retirement field {field!r} missing"
    if marker["producer"] != producer:
        return False, "retirement producer mismatch"
    try:
        approved_at = parse_instant(marker["approved_at"])
    except ValueError:
        return False, "retirement approved_at unparseable"
    if approved_at > now:
        return False, "retirement approved_at is in the future"
    return True, (
        f"producer={marker['producer']} successor={marker['successor']} "
        f"approved_by={marker['approved_by']} "
        f"approved_at={approved_at.isoformat()}"
    )


def find_gaps(rows, threshold_s, window_start):
    """Consecutive-row gaps longer than the threshold, ending in-window."""
    gaps = []
    for left, right in zip(rows, rows[1:]):
        delta_s = (right[0] - left[0]).total_seconds()
        if delta_s > threshold_s and right[0] >= window_start:
            gaps.append((left, right, delta_s))
    return gaps


def _retirement_not_consulted():
    return {"consulted": False, "accepted": False,
            "detail": "not consulted (only checked when stale)"}


def _envelope(args, verdict, now, state_summary, age_min=None,
              last_row=None, gaps=(), retirement=None, error=None,
              skipped=0):
    shown = list(gaps)[:GAP_CAP]
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "verdict": verdict,
        "producer": PRODUCER_ID,
        "now": now.isoformat() if now is not None else None,
        "threshold_min": args.max_age_min,
        "window_h": args.window_h,
        "age_min": age_min,
        "last_row": last_row,
        "state": {"summary": state_summary},
        "gaps": [
            {"start": left[0].isoformat(), "end": right[0].isoformat(),
             "duration_min": delta_s / 60.0,
             "left": {"model": left[1], "reason": left[2]},
             "right": {"model": right[1], "reason": right[2]}}
            for left, right, delta_s in shown
        ],
        "gaps_truncated": len(gaps) - len(shown),
        "retirement": (retirement if retirement is not None
                       else _retirement_not_consulted()),
        "error": error,
        "skipped_lines": skipped,
    }


def _emit_json(envelope, code):
    # Stdout is exactly one JSON document plus one trailing newline:
    # sorted keys and compact separators keep bytes deterministic.
    print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    return code


def _verdict_json(args, verdict, now, state_summary, age_min, last, gaps,
                  retirement, skipped, code):
    envelope = _envelope(
        args, verdict, now, state_summary, age_min=age_min,
        last_row={"at": last[0].isoformat(), "model": last[1],
                  "reason": last[2]},
        gaps=gaps, retirement=retirement, skipped=skipped)
    return _emit_json(envelope, code)


def _unmeasurable(args, code, detail, now, skipped=0):
    print(f"UNMEASURABLE: {detail}", file=sys.stderr)
    if not args.json:
        return 2
    envelope = _envelope(
        args, "unmeasurable", now, read_state(args.state),
        error={"code": code, "detail": detail}, skipped=skipped)
    return _emit_json(envelope, 2)


def _envelope_now(args):
    """Effective now for an early-error envelope; None only for bad --now."""
    if args.now is None:
        return datetime.now(timezone.utc)
    try:
        return parse_instant(args.now)
    except ValueError:
        return None


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="exit 1 when the fleet-quota-balancer log goes quiet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="--json stdout (schema_version 1, closed keys): "
               "schema_version, tool, verdict "
               "(fresh|stale|retired|unmeasurable), producer, now, "
               "threshold_min, window_h, age_min, last_row {at, model, "
               "reason}, state {summary}, gaps (at most 10; each {start, "
               "end, duration_min, left {model, reason}, right {model, "
               "reason}}), gaps_truncated, retirement {consulted, "
               "accepted, detail}, error (null or {code, detail}; codes "
               "log_missing, log_unreadable, log_no_parseable_row, "
               "log_no_row_before_now, bad_now, bad_max_age_min, "
               "bad_window_h), skipped_lines.\n"
               "Synthetic example: {\"schema_version\": 1, \"tool\": "
               "\"balancer_silence_guard.py\", \"verdict\": \"fresh\", "
               "\"producer\": \"fleet-quota-balancer\", \"now\": "
               "\"2026-10-05T05:00:00+00:00\", \"threshold_min\": 10.0, "
               "\"window_h\": 24.0, \"age_min\": 0.0, \"last_row\": "
               "{\"at\": \"2026-10-05T05:00:00+00:00\", \"model\": "
               "\"model-a\", \"reason\": \"reason-x\"}, \"state\": "
               "{\"summary\": \"state: evaluated_at=? model=?\"}, "
               "\"gaps\": [], \"gaps_truncated\": 0, \"retirement\": "
               "{\"consulted\": false, \"accepted\": false, \"detail\": "
               "\"not consulted (only checked when stale)\"}, \"error\": "
               "null, \"skipped_lines\": 0}")
    ap.add_argument("--log", default=LOG_DEFAULT)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--max-age-min", type=float, default=MAX_AGE_MIN_DEFAULT)
    ap.add_argument("--window-h", type=float, default=WINDOW_H_DEFAULT)
    ap.add_argument("--now", default=None,
                    help="ISO-8601 instant to measure against (default: now)")
    ap.add_argument("--retirement-file", default=None,
                    help="JSON file with explicit retirement evidence; "
                    "consulted only when the log would otherwise read stale")
    ap.add_argument("--json", action="store_true",
                    help="print one versioned JSON verdict document on "
                    "stdout instead of prose; exit codes unchanged")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not (args.max_age_min > 0):
        return _unmeasurable(
            args, "bad_max_age_min",
            f"--max-age-min must be positive, got {args.max_age_min}",
            _envelope_now(args))
    if not (args.window_h > 0):
        return _unmeasurable(
            args, "bad_window_h",
            f"--window-h must be positive, got {args.window_h}",
            _envelope_now(args))
    if args.now is None:
        now = datetime.now(timezone.utc)
    else:
        try:
            now = parse_instant(args.now)
        except ValueError as exc:
            return _unmeasurable(args, "bad_now", f"{exc}", None)
    try:
        rows, skipped = load_rows(args.log)
    except Unmeasurable as exc:
        return _unmeasurable(args, exc.code, f"{exc}", now,
                             skipped=exc.skipped)
    # A replay measures the log as of --now: rows stamped after it do not
    # exist yet, otherwise the silence under replay would be masked.
    rows = [row for row in rows if row[0] <= now]
    if not rows:
        return _unmeasurable(
            args, "log_no_row_before_now",
            f"no row at or before {now.isoformat()}: {args.log}",
            now, skipped=skipped)
    if skipped:
        print(f"note: skipped {skipped} unparseable line(s)", file=sys.stderr)
    state_summary = read_state(args.state)
    if not args.json:
        print(state_summary)
    threshold_s = args.max_age_min * 60.0
    window_start = now - timedelta(hours=args.window_h)
    gaps = find_gaps(rows, threshold_s, window_start)
    if not args.json:
        for left, right, delta_s in gaps:
            print(
                f"GAP {delta_s / 60.0:.1f} min: {left[0].isoformat()} "
                f"(model={left[1]} reason={left[2]}) -> {right[0].isoformat()} "
                f"(model={right[1]} reason={right[2]})")
    last = rows[-1]
    age_min = (now - last[0]).total_seconds() / 60.0
    if not args.json:
        print(
            f"last row {last[0].isoformat()} (model={last[1]} reason={last[2]}) "
            f"age={age_min:.1f} min threshold={args.max_age_min:g} min")
    if age_min > args.max_age_min:
        retirement = {"consulted": True, "accepted": False,
                      "detail": "no retirement file supplied"}
        if args.retirement_file is not None:
            ok, note = read_retirement(args.retirement_file, PRODUCER_ID, now)
            retirement = {"consulted": True, "accepted": ok, "detail": note}
            if ok:
                if args.json:
                    return _verdict_json(
                        args, "retired", now, state_summary, age_min, last,
                        gaps, retirement, skipped, 0)
                print(f"retirement: {note}")
                print(
                    f"RETIRED: {PRODUCER_ID} explicitly retired; "
                    f"last row {last[0].isoformat()} age={age_min:.1f} min")
                return 0  # retired-approved
            print(f"note: {note} (retirement ignored)", file=sys.stderr)
        if args.json:
            return _verdict_json(
                args, "stale", now, state_summary, age_min, last, gaps,
                retirement, skipped, 1)
        print(f"STALE: balancer quiet for {age_min:.1f} min")
        return 1
    if args.json:
        return _verdict_json(
            args, "fresh", now, state_summary, age_min, last, gaps,
            _retirement_not_consulted(), skipped, 0)
    print("FRESH: balancer log is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
