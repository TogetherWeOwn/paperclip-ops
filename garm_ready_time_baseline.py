#!/usr/bin/env python3
# ===========================================================================
# garm_ready_time_baseline.py — record and check GARM VM create-to-ready
# time against a checked-in baseline (case_026319fe)
#
# SCOPE. This tool baselines RUNNER STARTUP latency only: how long a GARM
# instance takes from VM create to runner ready, measured from a pair of
# operator-supplied UTC timestamps per instance (created_at, ready_at). It
# does NOT cover provider API create/delete latency (case_d61d5dad), pool health
# or capacity (case_b24794c2), reaper TTL and destroy pile-up (case_80716129),
# queue wait, or job duration. Those are separate instruments; a breach here
# means "VMs are slow to become ready", never "the pool is empty".
#
# WHY TIMESTAMP PAIRS, NOT DURATIONS. A bare duration_ms filing is
# unauditable: a reviewer cannot tell a stopwatch reading from a typo.
# Requiring both endpoint timestamps forces provenance (each sample names
# its instance and both clocks), makes clock-skew visible (ready before
# create refuses instead of filing a negative), and lets a later revision
# correlate the same pairs against queue-wait and job-pickup clocks without
# re-collecting. The cost is one strictness rule below; the benefit is that
# every row in the baseline is re-derivable from its two timestamps.
#
# WHY STRICT ZULU SECONDS. GARM read metadata and GitHub timestamps both
# render UTC `Z`. Fractional seconds and offset spellings would silently mix
# precisions across exports (a `.000Z` row and a `+00:00` row compare equal
# but sort differently as text). The clock regex requires exactly
# `YYYY-MM-DDTHH:MM:SSZ` with `[0-9]` in every position, mirroring the
# receipt-clock rule in github-runner/garm: `\d` accepts Unicode decimals,
# `[0-9]` does not.
#
# WHY NEAREST-RANK. p50/p95 use the nearest-rank method
# (rank = ceil(p/100 * n) on ascending 1-indexed sorted data) because it is
# exact on small samples and needs no interpolation convention. The suite
# pins 1..100 -> p50=50, p95=95; an off-by-one rank reads 51/96 and goes
# red. Do not "improve" this to linear interpolation without updating the
# baseline arithmetic and the pinned cases together.
#
# WHY DUPLICATES ARE DATA. Real startup samples repeat (quantized provider
# timestamps, identical image sizes). A validator that rejects duplicate
# durations silently drops the densest part of the distribution and biases
# every percentile. Duplicates are accepted; only shape violations refuse.
#
# WHY AN UPPER BOUND EXISTS. Durations are milliseconds derived from the
# timestamp pair. A duration above six hours is not a slow boot, it is a
# clock-skew artifact or a stale `ready_at` joined to the wrong create.
# Refusing it protects the baseline from one bad export moving p95 by an
# order of magnitude. The bound is deliberately loose: a real startup past
# six hours is a pool outage, and an outage is case_b24794c2's subject, not a
# baseline row.
#
# WHY READY-BEFORE-CREATE REFUSES. A negative duration is never a fast VM;
# it is swapped endpoints or skewed clocks. Filing it as zero would bias
# p50 down and hide the export error. Refuse, do not clamp.
#
# TRI-STATE CHECK. `check` answers three ways, never two:
#   0  OK      — enough samples, startup within thresholds
#   2  BREACH  — enough samples, p50 or p95 exceeds its threshold
#   3  UNKNOWN — baseline missing/provisional, or samples empty/unreadable.
#                UNKNOWN is not permission to page and not permission to
#                relax: it means the instrument has no standing, so the
#                decision must be made on something else and SAID so.
# A baseline with fewer than MIN_SAMPLES startup rows is PROVISIONAL:
# `record` writes it, `check` refuses to verdict on it (exit 3). A
# provisional threshold that pages is the same false green as a dead feed
# reading quiet — both report confidence the data cannot support.
#
# STORED INTEGRITY. A stored baseline is evidence, not a claim: maturity
# is established from its validated stored history, never from a
# caller-set flag. Before `check` verdicts or `record` merges, the stored
# document must reconcile exactly: startup samples are finite numbers
# (never booleans/strings) from 0 through six hours, at most MAX_SAMPLES
# of them, duplicates valid; count equals the number of stored samples;
# min/p50/p95/max equal the nearest-rank recomputation from those samples
# (null exactly when empty); `provisional` is a boolean equal to
# count < MIN_SAMPLES; provenance total_samples equals count. Any
# mismatch is refused, never silently repaired or reset: `check` answers
# UNKNOWN (exit 3) with verdict/detail keys plus the additive machine
# code `invalid_baseline` and a human remediation string; library
# `evaluate` mirrors that as ("unknown", {"code": "invalid_baseline",
# "reason": ...}); `record` raises the existing validation failure
# (exit 1) before mutating anything, leaving the file bytes intact.
# Genuinely empty/provisional baselines and valid mature documents the
# tool itself wrote keep their existing verdicts and carry no code.
#
# EXIT CODES
#   0  recorded / checked OK
#   1  usage or validation failure (bad shape, bad value, write failed)
#   2  threshold breach (`check` only)
#   3  UNKNOWN (`check` only: missing/provisional baseline, no samples)
#
# SOURCE-ONLY AND READ-ONLY. This script never contacts GARM, GitHub, or any
# host. It reads exactly two files it is explicitly given (a samples file
# and a baseline file) and writes only the baseline file on `record`/`init`.
# There is no network, no credential, no host command, no discovery. Sample
# collection from live read metadata is an operator step documented in
# docs/garm-ready-time-baseline.md, not a function in this file.
#
# Read-only except `record`/`init`, which write only the baseline path given
# on argv. Standard library only. Deterministic under `--now`.
# ===========================================================================
import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone

SAMPLES_SCHEMA = "garm-ready-time-samples.v1"
BASELINE_SCHEMA = "garm-ready-time-baseline.v1"
MAX_BYTES = 1024 * 1024
OPERATION = "startup"
MIN_SAMPLES = 10
MAX_SAMPLES = 2000
# Anything slower than this derived from a timestamp pair is a clock/join
# error, not a slow boot. Six hours in milliseconds. See header.
MAX_DURATION_MS = 6 * 60 * 60 * 1000

# Seed thresholds (milliseconds). NO direct VM create-to-ready sample stands
# behind these numbers yet: the GARM fleet has not migrated, so there is
# nothing to measure directly. Each seed is set deliberately ABOVE the
# physical floor of the startup path, and each is marked provisional until
# the first recalibration (see docs/garm-ready-time-baseline.md).
#
# Startup spans LXD create, image boot, cloud-init and runner registration:
# minutes-scale even healthy. The only indirect timing in the repo is three
# canary queue waits (111s/117s/163s, PR #483 run 2). p50 alert at 10 min is
# ~5x that indirect median; p95 alert at 20 min spends a full CI budget on
# provisioning alone. Both mirror the case_d61d5dad provider-create seeds, since
# startup contains the provider create path. Recalibrate after the first 30
# live startup samples.
PROPOSED_P50_ALERT_MS = 10 * 60 * 1000
PROPOSED_P95_ALERT_MS = 20 * 60 * 1000
# The proposed per-VM create-to-ready deadline (case_026319fe): a VM not ready
# within 10 minutes is slow (alert tier). The existing 30-minute reaper
# provisioning-stuck rule stays the outer hard bound; tightening it needs
# the same 30 live samples, in a reviewed PR. See the doc for placement.
PROPOSED_READY_TIMEOUT_MS = 10 * 60 * 1000

# Strict Zulu seconds. No fractional seconds, no offsets. See header.
CLOCK_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class InvalidInput(ValueError):
    pass


def _reject(message):
    raise InvalidInput(message)


def _is_finite(value):
    """math.isfinite without int-to-float overflow.

    math.isfinite(10**1000) raises OverflowError instead of returning
    False, which would escape validation as a traceback. Ints are
    finite by definition, so only floats take the isfinite path.
    Callers exclude bool and non-numbers before calling this.
    """
    return not isinstance(value, float) or math.isfinite(value)


def _is_number(value):
    # bool is an int subclass; True == 1 would otherwise pass as 1 ms.
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _read_json(path, what):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        _reject("%s unreadable: %s" % (what, exc))
    if len(raw) > MAX_BYTES:
        _reject("%s exceeds %d bytes" % (what, MAX_BYTES))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _reject("%s is not UTF-8 JSON" % what)


def _clock(value, where):
    if not isinstance(value, str) or not CLOCK_RE.match(value):
        _reject("%s: UTC Zulu seconds required "
                "(YYYY-MM-DDTHH:MM:SSZ)" % where)
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _reject("%s: not a real calendar time" % where)
    return moment.replace(tzinfo=timezone.utc)


def parse_samples(doc):
    """Validate an operator-supplied samples document.

    Returns [(instance, duration_ms), ...] preserving duplicates and order.
    Raises InvalidInput on any shape violation.
    """
    if type(doc) is not dict:
        _reject("samples: object required")
    if doc.get("schema") != SAMPLES_SCHEMA:
        _reject("samples: schema must be %s" % SAMPLES_SCHEMA)
    rows = doc.get("samples")
    if type(rows) is not list or not rows:
        _reject("samples.samples: non-empty array required")
    if len(rows) > MAX_SAMPLES:
        _reject("samples.samples: exceeds %d rows" % MAX_SAMPLES)
    parsed = []
    for index, row in enumerate(rows):
        if type(row) is not dict:
            _reject("samples[%d]: object required" % index)
        if set(row) != {"instance", "created_at", "ready_at"}:
            _reject("samples[%d]: exactly {instance, created_at, ready_at} "
                    "required" % index)
        name = row["instance"]
        if not isinstance(name, str) or not name.strip():
            _reject("samples[%d].instance: non-empty text required" % index)
        created = _clock(row["created_at"],
                         "samples[%d].created_at" % index)
        ready = _clock(row["ready_at"], "samples[%d].ready_at" % index)
        delta_ms = (ready - created).total_seconds() * 1000
        if delta_ms < 0:
            _reject("samples[%d]: ready_at precedes created_at "
                    "(swapped endpoints or skewed clocks)" % index)
        if delta_ms > MAX_DURATION_MS:
            _reject("samples[%d]: exceeds 6h, refusing clock/join "
                    "error" % index)
        parsed.append((name.strip(), delta_ms))
    return parsed


def percentile(sorted_values, pct):
    """Nearest-rank percentile over ascending sorted values (non-empty)."""
    if not sorted_values:
        _reject("percentile: empty input")
    if not 0 < pct < 100:
        _reject("percentile: pct must be in (0, 100)")
    rank = math.ceil(pct / 100.0 * len(sorted_values))
    return sorted_values[rank - 1]


def summarize(durations):
    ordered = sorted(durations)
    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "p50_ms": percentile(ordered, 50),
        "p95_ms": percentile(ordered, 95),
        "max_ms": ordered[-1],
    }


def default_thresholds():
    return {
        "p50_alert_ms": PROPOSED_P50_ALERT_MS,
        "p95_alert_ms": PROPOSED_P95_ALERT_MS,
        "ready_timeout_ms": PROPOSED_READY_TIMEOUT_MS,
        "rationale": (
            "Seed: startup spans LXD create, image boot, cloud-init and "
            "runner registration (minutes-scale healthy); only indirect "
            "timing exists (three canary queue waits 111s/117s/163s). "
            "p50 alert at 10 min is ~5x that indirect median; p95 at "
            "20 min spends a full CI budget on provisioning; per-VM "
            "ready deadline 10 min, inside the 15 min canary job "
            "ceiling and the 30 min reaper provisioning-stuck bound. "
            "Recalibrate after the first 30 live startup samples."
        ),
    }


def _utcnow(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, str) or not CLOCK_RE.match(now):
        _reject("--now: ISO-8601 Zulu seconds required")
    return datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def _iso_z(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def init_baseline(source):
    if not isinstance(source, str) or not source.strip():
        _reject("source: non-empty text required")
    return {
        "schema": BASELINE_SCHEMA,
        "provisional": True,
        "thresholds": default_thresholds(),
        "operations": {
            OPERATION: {"count": 0, "min_ms": None, "p50_ms": None,
                        "p95_ms": None, "max_ms": None, "samples": []}
        },
        "provenance": {
            "source": source.strip(),
            "refresh_cadence": ("recompute after every >=30 new samples "
                                "or weekly, whichever comes first"),
            "recorded_at": None,
            "recorded_by": None,
            "total_samples": 0,
        },
    }


def _check_baseline_shape(base):
    """Validate persisted baseline evidence and reconcile its metadata.

    Stored samples face the same numeric domain and bounded window as
    fresh samples (finite numbers, not booleans/strings, 0 through six
    hours, at most MAX_SAMPLES, duplicates valid). Stored
    count/statistics/provisional/provenance metadata must reconcile
    exactly with the stored samples: maturity is established from the
    evidence, never from a caller-set flag. Raises InvalidInput on any
    violation; invalid evidence must be refused, never repaired here.
    This validator reads only; it never mutates ``base``.
    """
    if type(base) is not dict:
        _reject("baseline: object required")
    if base.get("schema") != BASELINE_SCHEMA:
        _reject("baseline: schema must be %s" % BASELINE_SCHEMA)
    thresholds = base.get("thresholds")
    if type(thresholds) is not dict:
        _reject("baseline.thresholds: object required")
    for key in ("p50_alert_ms", "p95_alert_ms", "ready_timeout_ms"):
        value = thresholds.get(key)
        if not _is_number(value):
            _reject("baseline.thresholds.%s: number required" % key)
        if not _is_finite(value) or value <= 0:
            _reject("baseline.thresholds.%s: finite value > 0 required"
                    % key)
    ops = base.get("operations")
    if type(ops) is not dict:
        _reject("baseline.operations.%s: object required" % OPERATION)
    startup = ops.get(OPERATION)
    if type(startup) is not dict:
        _reject("baseline.operations.%s: object required" % OPERATION)
    stored = startup.get("samples")
    if type(stored) is not list:
        _reject("baseline.operations.%s.samples: array required" % OPERATION)
    if len(stored) > MAX_SAMPLES:
        _reject("baseline.operations.%s.samples: exceeds %d-sample window"
                % (OPERATION, MAX_SAMPLES))
    for index, value in enumerate(stored):
        where = "baseline.operations.%s.samples[%d]" % (OPERATION, index)
        if not _is_number(value):
            _reject("%s: number required" % where)
        if not _is_finite(value) or value < 0:
            _reject("%s: finite value >= 0 required" % where)
        if value > MAX_DURATION_MS:
            _reject("%s: exceeds 6h, refusing clock/join error" % where)
    count = startup.get("count")
    if type(count) is not int:
        _reject("baseline.operations.%s.count: integer required" % OPERATION)
    if count != len(stored):
        _reject("baseline.operations.%s.count: disagrees with stored "
                "sample count (%d)" % (OPERATION, len(stored)))
    if stored:
        expected = summarize(stored)
        for key in ("min_ms", "p50_ms", "p95_ms", "max_ms"):
            actual = startup.get(key)
            if not _is_number(actual) or not _is_finite(actual):
                _reject("baseline.operations.%s.%s: finite number required"
                        % (OPERATION, key))
            if actual != expected[key]:
                _reject("baseline.operations.%s.%s: stored value disagrees "
                        "with stored samples" % (OPERATION, key))
    else:
        for key in ("min_ms", "p50_ms", "p95_ms", "max_ms"):
            if key not in startup or startup[key] is not None:
                _reject("baseline.operations.%s.%s: must be null with no "
                        "stored samples" % (OPERATION, key))
    provisional = base.get("provisional")
    if type(provisional) is not bool:
        _reject("baseline.provisional: boolean required")
    if provisional != (len(stored) < MIN_SAMPLES):
        _reject("baseline.provisional: disagrees with stored evidence; "
                "refusing inconsistent maturity")
    provenance = base.get("provenance")
    if type(provenance) is not dict:
        _reject("baseline.provenance: object required")
    total = provenance.get("total_samples")
    if type(total) is not int or total != len(stored):
        _reject("baseline.provenance.total_samples: must equal stored "
                "sample count (%d)" % len(stored))


def record(base, parsed, source, recorded_by, now):
    """Merge parsed samples into the baseline; returns the new baseline."""
    _check_baseline_shape(base)
    if not parsed:
        _reject("record: no samples to record")
    if not isinstance(source, str) or not source.strip():
        _reject("record: non-empty source required")
    if not isinstance(recorded_by, str) or not recorded_by.strip():
        _reject("record: non-empty recorded-by required")
    moment = _utcnow(now)
    stored = base["operations"][OPERATION].get("samples") or []
    if type(stored) is not list:
        _reject("baseline.operations.startup.samples: array required")
    merged = [float(duration) for duration in stored]
    merged.extend(float(duration) for _, duration in parsed)
    # Bounded window: newest samples win, history never grows unbounded.
    merged = merged[-MAX_SAMPLES:]
    summary = summarize(merged)
    base["operations"][OPERATION] = {
        "count": summary["count"],
        "min_ms": summary["min_ms"],
        "p50_ms": summary["p50_ms"],
        "p95_ms": summary["p95_ms"],
        "max_ms": summary["max_ms"],
        "samples": merged,
    }
    base["provisional"] = summary["count"] < MIN_SAMPLES
    provenance = base.get("provenance")
    if type(provenance) is not dict:
        provenance = {}
        base["provenance"] = provenance
    provenance["source"] = source.strip()
    provenance["recorded_at"] = _iso_z(moment)
    provenance["recorded_by"] = recorded_by.strip()
    provenance["total_samples"] = summary["count"]
    return base


def evaluate(base, parsed):
    """Evaluate fresh samples against stored thresholds.

    Returns (verdict, detail) with verdict one of "ok", "breach",
    "unknown". Breach is OR across p50/p95 on the FRESH samples, never on
    the baseline's own history. An internally inconsistent stored
    baseline answers ("unknown", {"code": "invalid_baseline",
    "reason": ...}) instead of raising: `check` is a read-only verdict,
    so bad stored evidence means the instrument has no standing. The
    "code" field is additive; genuinely provisional baselines keep the
    original reason-only detail. `record` raises InvalidInput instead.
    """
    try:
        _check_baseline_shape(base)
    except InvalidInput as exc:
        return ("unknown", {
            "code": "invalid_baseline",
            "reason": ("baseline is internally inconsistent: %s; restore "
                       "a backup or rebuild it with init/record from "
                       "auditable samples" % exc),
        })
    if base.get("provisional"):
        return ("unknown", {"reason": "baseline is provisional"})
    stored_count = base["operations"][OPERATION].get("count") or 0
    if stored_count < MIN_SAMPLES:
        return ("unknown", {"reason": "baseline holds fewer than %d "
                            "samples" % MIN_SAMPLES})
    if not parsed:
        return ("unknown", {"reason": "no fresh samples"})
    fresh = summarize([duration for _, duration in parsed])
    thresholds = base["thresholds"]
    breaches = []
    if fresh["p50_ms"] > thresholds["p50_alert_ms"]:
        breaches.append("p50 %s > alert %s"
                        % (fresh["p50_ms"], thresholds["p50_alert_ms"]))
    if fresh["p95_ms"] > thresholds["p95_alert_ms"]:
        breaches.append("p95 %s > alert %s"
                        % (fresh["p95_ms"], thresholds["p95_alert_ms"]))
    detail = {
        "count": fresh["count"],
        "min_ms": fresh["min_ms"],
        "p50_ms": fresh["p50_ms"],
        "p95_ms": fresh["p95_ms"],
        "max_ms": fresh["max_ms"],
        "breaches": breaches,
    }
    if breaches:
        return ("breach", detail)
    return ("ok", detail)


def _write_json(path, doc):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        _reject("baseline unwritable: %s" % exc)


def cmd_init(args):
    base = init_baseline(args.source)
    _write_json(args.baseline, base)
    print("initialized provisional baseline at %s" % args.baseline)
    return 0


def cmd_record(args):
    samples_doc = _read_json(args.samples, "samples")
    parsed = parse_samples(samples_doc)
    try:
        with open(args.baseline, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        base = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _reject("baseline unreadable or not JSON")
    record(base, parsed, args.source, args.recorded_by, args.now)
    _write_json(args.baseline, base)
    stored = base["operations"][OPERATION]
    print("recorded %d samples (total %d, provisional=%s) p50=%s p95=%s"
          % (len(parsed), stored["count"], base["provisional"],
             stored["p50_ms"], stored["p95_ms"]))
    return 0


def cmd_check(args):
    # check is read-only: it never writes, so its inherited dry-run needs
    # no separate flag. An inconsistent stored baseline answers unknown
    # (exit 3) with code "invalid_baseline"; see evaluate().
    samples_doc = _read_json(args.samples, "samples")
    parsed = parse_samples(samples_doc)
    try:
        with open(args.baseline, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        base = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        print(json.dumps({"verdict": "unknown",
                          "reason": "baseline unreadable"}))
        return 3
    verdict, detail = evaluate(base, parsed)
    print(json.dumps({"verdict": verdict, "detail": detail}, sort_keys=True))
    if verdict == "breach":
        return 2
    if verdict == "unknown":
        return 3
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Record and check GARM VM create-to-ready time.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="write a provisional baseline")
    init_p.add_argument("--baseline", required=True)
    init_p.add_argument("--source", required=True)
    init_p.set_defaults(func=cmd_init)

    record_p = sub.add_parser("record", help="merge samples into baseline")
    record_p.add_argument("--samples", required=True)
    record_p.add_argument("--baseline", required=True)
    record_p.add_argument("--source", required=True)
    record_p.add_argument("--recorded-by", required=True)
    record_p.add_argument("--now", default=None)
    record_p.set_defaults(func=cmd_record)

    check_p = sub.add_parser("check", help="evaluate samples vs baseline")
    check_p.add_argument("--samples", required=True)
    check_p.add_argument("--baseline", required=True)
    check_p.set_defaults(func=cmd_check)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InvalidInput as exc:
        print("invalid input: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
