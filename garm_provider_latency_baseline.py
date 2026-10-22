#!/usr/bin/env python3
# ===========================================================================
# garm_provider_latency_baseline.py — record and check GARM provider
# operation latency against a checked-in baseline (case_d61d5dad)
#
# SCOPE. This tool baselines PROVIDER OPERATION latency only: how long the
# GARM provider takes to execute an instance create or delete, measured from
# GARM API read metadata (instance request timestamps vs. terminal-state
# timestamps) or operator-supplied samples in the same shape. It does NOT
# cover pool health (case_b24794c2), reaper TTL (case_80716129), queue wait, job
# duration, or runner registration lag. Those are separate instruments; a
# breach here means "the provider is slow", never "the pool is empty".
#
# SOURCE-ONLY AND READ-ONLY. This script never contacts GARM, GitHub, or any
# host. It reads exactly two files it is explicitly given (a samples file and
# a baseline file) and writes only the baseline file on `record`. There is no
# network, no credential, no host command, no discovery. Sample collection
# from live API metadata is an operator step documented in
# docs/garm-provider-latency-baseline.md, not a function in this file.
#
# WHY NEAREST-RANK. p50/p95 here use the nearest-rank method
# (rank = ceil(p/100 * n) on ascending 1-indexed sorted data) because it is
# exact on small samples and needs no interpolation convention. The test
# suite pins 1..100 -> p50=50, p95=95; an off-by-one rank reads 51/96 and
# goes red. Do not "improve" this to linear interpolation without updating
# the baseline arithmetic and the pinned cases together.
#
# WHY DUPLICATES ARE DATA. Real latency samples repeat (quantized provider
# timestamps, identical VM sizes). A validator that rejects duplicate
# durations silently drops the densest part of the distribution and biases
# every percentile. Duplicates are accepted; only shape violations refuse.
#
# WHY AN UPPER BOUND EXISTS. Operator samples are milliseconds. A sample
# above six hours is not a slow VM, it is a unit error (seconds or
# microseconds filed as milliseconds) or a clock-skew artifact from the
# metadata export. Refusing it protects the baseline from one bad export
# moving p95 by an order of magnitude. The bound is deliberately loose: any
# real provider operation that takes over six hours is a pool outage, and an
# outage is case_b24794c2's subject, not a baseline row.
#
# TRI-STATE CHECK. `check` answers three ways, never two:
#   0  OK      — enough samples, all operations within thresholds
#   2  BREACH  — enough samples, an operation exceeds a threshold
#   3  UNKNOWN — baseline missing/provisional, or samples empty/unreadable.
#                UNKNOWN is not permission to page and not permission to
#                relax: it means the instrument has no standing, so the
#                decision must be made on something else and SAID so.
# A baseline with fewer than MIN_SAMPLES_PER_OP samples per operation is
# PROVISIONAL: `record` writes it, `check` refuses to verdict on it (exit 3).
# A provisional threshold that pages is the same false green as a dead feed
# reading quiet — both report confidence the data cannot support.
#
# EVIDENCE INTEGRITY. Stored samples face the same numeric domain as fresh
# ones (number, not bool, finite, >= 0, <= 6h) plus the same bounded window
# (<= MAX_SAMPLES_PER_OP per operation). Stored count/statistics/provisional
# metadata must reconcile exactly with the stored samples: a provisional=false
# flag over thin or empty evidence is forged maturity and refuses, not a
# verdict. An ABSENT baseline path initializes on `init`/`record`; an
# EXISTING but unreadable, malformed or inconsistent baseline is never
# replaced, truncated or auto-repaired — `record`/`init` exit 1 and `check`
# exits 3, leaving every byte untouched. Publication is atomic (temp file in
# the same directory, then rename); a failed rename reports exit 1 and the
# prior valid file survives.
#
# MACHINE OUTPUT. `--json` on any command prints exactly one finite JSON
# object (schema garm-provider-latency-outcome.v1) carrying the operation,
# input paths, validity/maturity/coverage reasons, a stable code, retryable
# and next_action; `check` outcomes also carry the verdict. `--dry-run`
# validates and computes the plan with zero writes. Unknown or conflicting
# flags refuse without writes.
#
# EXIT CODES
#   0  recorded / checked OK (or dry-run plan valid)
#   1  usage or validation failure (bad shape, bad value, invalid existing
#      baseline on record/init, write failed, bad flags)
#   2  threshold breach (`check` only)
#   3  UNKNOWN (`check` only: missing/provisional/invalid baseline, no samples)
#
# Read-only except `record`/`init`, which write only the baseline path given
# on argv. Standard library only. Deterministic under `--now`.
# ===========================================================================
import argparse
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone

SAMPLES_SCHEMA = "garm-provider-latency-samples.v1"
BASELINE_SCHEMA = "garm-provider-latency-baseline.v1"
OUTCOME_SCHEMA = "garm-provider-latency-outcome.v1"
MAX_BYTES = 1024 * 1024
OPERATIONS = ("create", "delete")
MIN_SAMPLES_PER_OP = 10
MAX_SAMPLES_PER_OP = 2000
# Anything slower than this filed as milliseconds is a unit/clock error, not
# a slow provider. Six hours in milliseconds. See header.
MAX_DURATION_MS = 6 * 60 * 60 * 1000

# Seed thresholds (milliseconds). NO live provider sample stands behind
# these numbers: the GARM fleet has not migrated yet, so there is nothing to
# measure. Each seed is a round number set deliberately ABOVE the physical
# floor of the operation it guards, and each is marked provisional until the
# first recalibration (see docs/garm-provider-latency-baseline.md).
#
# create: provider API call + image boot + cloud-init + runner registration
# is minutes-scale even healthy. p50 alert at 10 min, p95 at 20 min: a median
# create slower than 10 minutes means the typical path — not the tail — is
# sick, and a 5% tail past 20 minutes means one job in twenty waits a full
# CI budget on provisioning alone.
# delete: API call + VM destroy is seconds-to-low-minutes. p50 alert at
# 2 min, p95 at 5 min: deletes have no boot phase, so any median above two
# minutes is provider distress, and a tail past five minutes predicts
# destroy pile-up, which is what the reaper (case_80716129) then has to absorb.
PROPOSED_THRESHOLDS_MS = {
    "create": {
        "p50_alert_ms": 10 * 60 * 1000,
        "p95_alert_ms": 20 * 60 * 1000,
        "rationale": (
            "Seed: create spans provider API, image boot, cloud-init and "
            "runner registration (minutes-scale healthy). Median past "
            "10 min means the typical path is sick; 5% tail past 20 min "
            "spends a full CI budget on provisioning. Recalibrate after "
            "the first 30 live create samples."
        ),
    },
    "delete": {
        "p50_alert_ms": 2 * 60 * 1000,
        "p95_alert_ms": 5 * 60 * 1000,
        "rationale": (
            "Seed: delete is API call plus VM destroy with no boot phase "
            "(seconds-to-low-minutes healthy). Median past 2 min is "
            "provider distress; tail past 5 min predicts destroy "
            "pile-up for the reaper. Recalibrate after the first 30 "
            "live delete samples."
        ),
    },
}


class InvalidInput(ValueError):
    pass


class UnreadableInput(InvalidInput):
    """Input bytes that could not be read (OSError).

    A subclass of InvalidInput so every existing handler keeps catching
    it; _refuse_invalid_baseline tests this type (not message text) to
    choose unreadable_baseline, since refusal messages echo stored values.
    """
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


def _read_json(path, what):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise UnreadableInput("%s unreadable: %s" % (what, exc))
    if len(raw) > MAX_BYTES:
        _reject("%s exceeds %d bytes" % (what, MAX_BYTES))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _reject("%s is not UTF-8 JSON" % what)


def _check_duration(value, where):
    """Validate one duration against the shared numeric domain.

    Fresh and persisted samples share this domain: a real number, not a
    bool, finite, nonnegative, and within six hours. Anything else is a
    shape violation or a unit/clock error, never a slow provider.
    """
    # bool is an int subclass; True == 1 would otherwise pass as 1 ms.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject("%s: number required" % where)
    if not _is_finite(value) or value < 0:
        _reject("%s: finite value >= 0 required" % where)
    if value > MAX_DURATION_MS:
        _reject("%s: exceeds 6h, refusing unit/clock error" % where)
    return value


def _duration_ms(value, index):
    return _check_duration(value, "samples[%d].duration_ms" % index)


def parse_samples(doc):
    """Validate an operator-supplied samples document.

    Returns {operation: [duration_ms, ...]} preserving duplicates and order.
    Raises InvalidInput on any shape violation.
    """
    if type(doc) is not dict:
        _reject("samples: object required")
    if doc.get("schema") != SAMPLES_SCHEMA:
        _reject("samples: schema must be %s" % SAMPLES_SCHEMA)
    rows = doc.get("samples")
    if type(rows) is not list or not rows:
        _reject("samples.samples: non-empty array required")
    if len(rows) > 2 * MAX_SAMPLES_PER_OP:
        _reject("samples.samples: exceeds %d rows" % (2 * MAX_SAMPLES_PER_OP))
    grouped = {op: [] for op in OPERATIONS}
    for index, row in enumerate(rows):
        if type(row) is not dict:
            _reject("samples[%d]: object required" % index)
        if set(row) != {"operation", "duration_ms"}:
            _reject("samples[%d]: exactly {operation, duration_ms} "
                    "required" % index)
        if row["operation"] not in OPERATIONS:
            _reject("samples[%d].operation: must be create or delete"
                    % index)
        grouped[row["operation"]].append(_duration_ms(row["duration_ms"],
                                                      index))
    return grouped


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
        op: {
            "p50_alert_ms": spec["p50_alert_ms"],
            "p95_alert_ms": spec["p95_alert_ms"],
            "rationale": spec["rationale"],
        }
        for op, spec in PROPOSED_THRESHOLDS_MS.items()
    }


def _utcnow(now):
    if now is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        _reject("--now: ISO-8601 timestamp required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
            op: {"count": 0, "min_ms": None, "p50_ms": None,
                 "p95_ms": None, "max_ms": None, "samples": []}
            for op in OPERATIONS
        },
        "provenance": {
            "source": source.strip(),
            "refresh_cadence": ("recompute after every >=30 new samples per "
                                "operation or weekly, whichever comes first"),
            "recorded_at": None,
            "recorded_by": None,
            "total_samples": 0,
        },
    }


def _check_baseline_shape(doc):
    """Validate persisted baseline evidence and reconcile its metadata.

    Stored samples face the same numeric domain and bounded window as fresh
    samples. Stored count/statistics/provisional/provenance metadata must
    reconcile exactly with the stored samples: maturity is established from
    the evidence, never from a caller-set flag. Raises InvalidInput on any
    violation; invalid evidence must be refused, never repaired in place.
    """
    if type(doc) is not dict:
        _reject("baseline: object required")
    if doc.get("schema") != BASELINE_SCHEMA:
        _reject("baseline: schema must be %s" % BASELINE_SCHEMA)
    if type(doc.get("provisional")) is not bool:
        _reject("baseline.provisional: boolean required")
    if type(doc.get("operations")) is not dict:
        _reject("baseline.operations: object required")
    total = 0
    for op in OPERATIONS:
        entry = doc["operations"].get(op)
        if type(entry) is not dict:
            _reject("baseline.operations.%s: object required" % op)
        stored = entry.get("samples")
        if type(stored) is not list:
            _reject("baseline.operations.%s.samples: array required" % op)
        if len(stored) > MAX_SAMPLES_PER_OP:
            _reject("baseline.operations.%s.samples: exceeds %d-sample "
                    "window" % (op, MAX_SAMPLES_PER_OP))
        for index, value in enumerate(stored):
            _check_duration(
                value, "baseline.operations.%s.samples[%d]" % (op, index))
        if type(entry.get("count")) is not int:
            _reject("baseline.operations.%s.count: integer required" % op)
        if stored:
            expected = summarize(stored)
            for key in ("count", "min_ms", "p50_ms", "p95_ms", "max_ms"):
                actual = entry.get(key)
                if actual != expected[key]:
                    _reject("baseline.operations.%s.%s: stored %r disagrees "
                            "with stored samples (%r)"
                            % (op, key, actual, expected[key]))
        else:
            if entry.get("count") != 0:
                _reject("baseline.operations.%s.count: must be 0 with no "
                        "stored samples" % op)
            for key in ("min_ms", "p50_ms", "p95_ms", "max_ms"):
                if entry.get(key) is not None:
                    _reject("baseline.operations.%s.%s: must be null with no "
                            "stored samples" % (op, key))
        total += len(stored)
    thresholds = doc.get("thresholds")
    if type(thresholds) is not dict:
        _reject("baseline.thresholds: object required")
    for op in OPERATIONS:
        spec = thresholds.get(op)
        if type(spec) is not dict:
            _reject("baseline.thresholds.%s: object required" % op)
        for key in ("p50_alert_ms", "p95_alert_ms"):
            value = spec.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not _is_finite(value) or value < 0):
                _reject("baseline.thresholds.%s.%s: finite value >= 0 "
                        "required" % (op, key))
        # Threshold metadata is echoed into check details, so it must be
        # finite-safe too: rationale is text when present, and unknown keys
        # refuse, matching the exact-shape rule for sample rows.
        for key, value in spec.items():
            if key in ("p50_alert_ms", "p95_alert_ms"):
                continue
            if key == "rationale":
                if not isinstance(value, str):
                    _reject("baseline.thresholds.%s.rationale: text "
                            "required" % op)
            else:
                _reject("baseline.thresholds.%s: unknown key %r" % (op, key))
    provenance = doc.get("provenance")
    if type(provenance) is not dict:
        _reject("baseline.provenance: object required")
    if (type(provenance.get("total_samples")) is not int
            or provenance["total_samples"] != total):
        _reject("baseline.provenance.total_samples: must equal stored "
                "sample count (%d)" % total)
    thin = any(len(doc["operations"][op]["samples"]) < MIN_SAMPLES_PER_OP
               for op in OPERATIONS)
    if doc["provisional"] != thin:
        if doc["provisional"]:
            _reject("baseline.provisional: true disagrees with stored "
                    "evidence; refusing inconsistent maturity")
        _reject("baseline.provisional: false disagrees with stored "
                "evidence; refusing forged maturity")
    return doc


def record(baseline, grouped, source, recorded_by, now):
    """Append validated samples to the baseline store and recompute stats.

    Returns the updated baseline document. Oldest samples drop once an
    operation exceeds MAX_SAMPLES_PER_OP; the store is a bounded window,
    not an unbounded log.
    """
    _check_baseline_shape(baseline)
    if not isinstance(source, str) or not source.strip():
        _reject("source: non-empty text required")
    if not isinstance(recorded_by, str) or not recorded_by.strip():
        _reject("recorded_by: non-empty text required")
    total = 0
    for op in OPERATIONS:
        stored = baseline["operations"][op]["samples"]
        stored.extend(grouped[op])
        if len(stored) > MAX_SAMPLES_PER_OP:
            del stored[:-MAX_SAMPLES_PER_OP]
        stats = summarize(stored) if stored else None
        if stats is None:
            baseline["operations"][op] = {
                "count": 0, "min_ms": None, "p50_ms": None,
                "p95_ms": None, "max_ms": None, "samples": [],
            }
        else:
            baseline["operations"][op] = dict(stats, samples=list(stored))
        total += len(stored)
    provisional = any(
        baseline["operations"][op]["count"] < MIN_SAMPLES_PER_OP
        for op in OPERATIONS
    )
    baseline["provisional"] = provisional
    baseline["provenance"].update({
        "source": source.strip(),
        "recorded_at": _iso_z(_utcnow(now)),
        "recorded_by": recorded_by.strip(),
        "total_samples": total,
    })
    return baseline


def evaluate(baseline, grouped):
    """Compare fresh samples against baseline thresholds.

    Returns (verdict, details) where verdict is one of
    "ok", "breach", "unknown". Unknown covers: provisional baseline and
    operations with no fresh samples — a verdict on either would be
    confidence the data cannot support.
    """
    _check_baseline_shape(baseline)
    if baseline.get("provisional"):
        return ("unknown", {"reason": "baseline is provisional"})
    details = {}
    worst = "ok"
    for op in OPERATIONS:
        fresh = grouped[op]
        if not fresh:
            return ("unknown", {"reason": "no fresh %s samples" % op})
        stats = summarize(sorted(fresh))
        spec = baseline["thresholds"][op]
        breach = (stats["p50_ms"] > spec["p50_alert_ms"]
                  or stats["p95_ms"] > spec["p95_alert_ms"])
        details[op] = dict(stats, thresholds=dict(spec),
                           breach=bool(breach))
        if breach:
            worst = "breach"
    return (worst, details)


def _publish_json(path, doc):
    """Publish the baseline atomically.

    The document is written to a temp file in the target directory, then
    renamed over the target. A failed rename leaves the prior valid file
    untouched; callers report the OSError instead of retrying in place.
    The temp file inherits the existing baseline's permission bits so the
    first record does not lock out other readers; a fresh baseline keeps
    mkstemp's 0600 default. Contents are fsynced before the rename and the
    directory entry after it, so a crash cannot leave a torn baseline.
    The directory is opened before the rename, so an unopenable directory
    fails the publication instead of reporting success without durability.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".baseline-", suffix=".tmp",
                               dir=directory)
    dir_fd = None
    try:
        try:
            os.fchmod(fd, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass  # Absent target keeps mkstemp's 0600; publish unaffected.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The fd refers to the directory itself, so opening it before the
        # rename still lets the later fsync cover the new entry.
        dir_fd = os.open(directory, os.O_RDONLY)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass
        raise
    try:
        os.fsync(dir_fd)
    except OSError:
        pass  # Durability hint only: the rename already committed.
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass  # Cleanup only: the rename already committed, so a failed
            # close must not masquerade as a failed publication.


def _outcome(operation, baseline_path, samples_path, *, code, retryable,
             next_action, validity_ok, validity_reason,
             maturity_mature, maturity_reason,
             coverage_ok, coverage_reason,
             verdict=None, dry_run=False, extra=None):
    """Build one versioned --json outcome object.

    Every field is finite by construction: counts and reconciled statistics
    are validated integers, reasons are strings, and persisted values are
    never echoed raw, so a poisoned baseline cannot poison the outcome.
    """
    doc = {
        "schema": OUTCOME_SCHEMA,
        "operation": operation,
        "baseline_schema": BASELINE_SCHEMA,
        "samples_schema": SAMPLES_SCHEMA,
        "source": {"baseline": baseline_path, "samples": samples_path},
        "dry_run": bool(dry_run),
        "validity": {"ok": bool(validity_ok), "reason": validity_reason},
        "maturity": {"mature": bool(maturity_mature),
                     "reason": maturity_reason},
        "coverage": {"adequate": bool(coverage_ok), "reason": coverage_reason},
        "code": code,
        "retryable": bool(retryable),
        "next_action": next_action,
    }
    if verdict is not None:
        doc["verdict"] = verdict
    if extra:
        doc.update(extra)
    return doc


def _print_outcome(doc):
    print(json.dumps(doc, indent=2, sort_keys=True))


def _stored_counts(baseline):
    return {op: len(baseline["operations"][op]["samples"])
            for op in OPERATIONS}


def _maturity_summary(counts):
    parts = ", ".join("%s=%d" % (op, counts[op]) for op in OPERATIONS)
    if any(counts[op] < MIN_SAMPLES_PER_OP for op in OPERATIONS):
        return (False, "provisional: stored samples below %d per operation "
                       "(%s)" % (MIN_SAMPLES_PER_OP, parts))
    return (True, "mature: stored samples meet %d per operation (%s)"
                  % (MIN_SAMPLES_PER_OP, parts))


def _coverage_summary(counts):
    parts = ", ".join("%s=%d" % (op, counts[op]) for op in OPERATIONS)
    if all(counts[op] >= MIN_SAMPLES_PER_OP for op in OPERATIONS):
        return (True, "stored evidence meets %d per operation (%s)"
                       % (MIN_SAMPLES_PER_OP, parts))
    return (False, "stored evidence below %d per operation (%s)"
                   % (MIN_SAMPLES_PER_OP, parts))


def _baseline_extra(baseline):
    counts = _stored_counts(baseline)
    return {"baseline": {
        "provisional": bool(baseline["provisional"]),
        "total_samples": sum(counts.values()),
        "create_samples": counts["create"],
        "delete_samples": counts["delete"],
    }}


def _fail_samples(args, exc, as_json, dry_run):
    """Refuse invalid samples for record/check: exit 1, zero writes."""
    if as_json:
        _print_outcome(_outcome(
            args.command, args.baseline, args.samples,
            code="invalid_samples", retryable=False,
            next_action="fix the samples document and rerun; the baseline "
                        "was left untouched",
            validity_ok=False, validity_reason="invalid samples: %s" % exc,
            maturity_mature=False,
            maturity_reason="maturity not established: samples invalid",
            coverage_ok=False,
            coverage_reason="coverage not established: samples invalid",
            verdict="unknown" if args.command == "check" else None,
            dry_run=dry_run))
    else:
        print("error: %s" % exc, file=sys.stderr)
    return 1


def _refuse_invalid_baseline(args, exc, as_json, check_mode, dry_run):
    """Refuse an existing invalid baseline without touching its bytes.

    `record`/`init` report exit 1; `check` reports exit 3 (unknown), since
    a damaged baseline is no standing for a measured verdict either way.
    """
    message = str(exc)
    unreadable = isinstance(exc, UnreadableInput)
    code = "unreadable_baseline" if unreadable else "invalid_baseline"
    if as_json:
        _print_outcome(_outcome(
            args.command, args.baseline,
            getattr(args, "samples", None),
            code=code, retryable=unreadable,
            next_action="inspect or restore the baseline file from a known "
                        "valid copy; this tool never auto-repairs damaged "
                        "evidence",
            validity_ok=False,
            validity_reason="invalid existing baseline: %s" % message,
            maturity_mature=False,
            maturity_reason="maturity not established: stored evidence "
                            "invalid",
            coverage_ok=False,
            coverage_reason="coverage not established: stored evidence "
                            "invalid",
            verdict="unknown" if check_mode else None,
            dry_run=dry_run))
    else:
        print("%s: refusing invalid baseline: %s"
              % ("unknown" if check_mode else "error", message),
              file=sys.stderr if not check_mode else sys.stdout)
    return 3 if check_mode else 1


def _report_publish_failure(args, exc, as_json, computed):
    """Report a failed atomic publication: exit 1, prior bytes preserved."""
    counts = _stored_counts(computed)
    _, maturity_reason = _maturity_summary(counts)
    covered, coverage_reason = _coverage_summary(counts)
    if as_json:
        _print_outcome(_outcome(
            args.command, args.baseline,
            getattr(args, "samples", None),
            code="publish_failed", retryable=True,
            next_action="retry the command; the prior baseline file was "
                        "preserved unchanged",
            validity_ok=True,
            validity_reason="samples and stored evidence valid; atomic "
                            "publication failed: %s" % exc,
            maturity_mature=not computed["provisional"],
            maturity_reason="computed but unpublished: " + maturity_reason,
            coverage_ok=covered,
            coverage_reason="computed but unpublished: " + coverage_reason,
            extra=_baseline_extra(computed)))
    else:
        print("error: baseline write failed: %s" % exc, file=sys.stderr)
    return 1


def _cmd_init(args, as_json, dry_run):
    try:
        doc = init_baseline(args.source)
    except InvalidInput as exc:
        if as_json:
            _print_outcome(_outcome(
                "init", args.baseline, None,
                code="invalid_arguments", retryable=False,
                next_action="rerun init with a non-empty --source; nothing "
                            "was written",
                validity_ok=False, validity_reason=str(exc),
                maturity_mature=False,
                maturity_reason="maturity not established: invalid arguments",
                coverage_ok=False,
                coverage_reason="coverage not established: invalid "
                                "arguments",
                dry_run=dry_run))
        else:
            print("error: %s" % exc, file=sys.stderr)
        return 1
    if os.path.exists(args.baseline):
        if as_json:
            _print_outcome(_outcome(
                "init", args.baseline, None,
                code="baseline_exists", retryable=False,
                next_action="record samples into the existing baseline, or "
                            "back it up and remove it explicitly before init",
                validity_ok=False,
                validity_reason="baseline already exists at %s; refusing to "
                                "overwrite evidence" % args.baseline,
                maturity_mature=False,
                maturity_reason="maturity not established: existing baseline "
                                "left untouched",
                coverage_ok=False,
                coverage_reason="coverage not established: existing baseline "
                                "left untouched",
                dry_run=dry_run))
        else:
            print("error: baseline already exists at %s; refusing to "
                  "overwrite evidence" % args.baseline, file=sys.stderr)
        return 1
    if dry_run:
        if as_json:
            _print_outcome(_outcome(
                "init", args.baseline, None,
                code="ok", retryable=False,
                next_action="rerun without --dry-run to write the "
                            "provisional baseline",
                validity_ok=True,
                validity_reason="arguments valid; baseline absent, "
                                "initialization planned",
                maturity_mature=False,
                maturity_reason="new baseline would be provisional: 0 "
                                "stored samples",
                coverage_ok=False,
                coverage_reason="no stored samples yet",
                dry_run=True, extra=_baseline_extra(doc)))
        else:
            print("dry-run: would initialize provisional baseline at %s"
                  % args.baseline)
        return 0
    try:
        _publish_json(args.baseline, doc)
    except OSError as exc:
        return _report_publish_failure(args, exc, as_json, doc)
    if as_json:
        _print_outcome(_outcome(
            "init", args.baseline, None,
            code="ok", retryable=False,
            next_action="record at least %d samples per operation, then "
                        "check" % MIN_SAMPLES_PER_OP,
            validity_ok=True,
            validity_reason="provisional baseline initialized",
            maturity_mature=False,
            maturity_reason="new baseline is provisional: 0 stored samples",
            coverage_ok=False,
            coverage_reason="no stored samples yet",
            extra=_baseline_extra(doc)))
    else:
        print("initialized provisional baseline at %s" % args.baseline)
    return 0


def _cmd_record(args, grouped, as_json, dry_run):
    if os.path.exists(args.baseline):
        try:
            baseline = _check_baseline_shape(
                _read_json(args.baseline, "baseline"))
        except InvalidInput as exc:
            return _refuse_invalid_baseline(args, exc, as_json, False,
                                            dry_run)
    else:
        try:
            baseline = init_baseline(args.source)
        except InvalidInput as exc:
            if as_json:
                _print_outcome(_outcome(
                    "record", args.baseline, args.samples,
                    code="invalid_arguments", retryable=False,
                    next_action="rerun record with a non-empty --source; "
                                "nothing was written",
                    validity_ok=False, validity_reason=str(exc),
                    maturity_mature=False,
                    maturity_reason="maturity not established: invalid "
                                    "arguments",
                    coverage_ok=False,
                    coverage_reason="coverage not established: invalid "
                                    "arguments",
                    dry_run=dry_run))
            else:
                print("error: %s" % exc, file=sys.stderr)
            return 1
    try:
        record(baseline, grouped, args.source, args.recorded_by, args.now)
    except InvalidInput as exc:
        if as_json:
            _print_outcome(_outcome(
                "record", args.baseline, args.samples,
                code="invalid_arguments", retryable=False,
                next_action="fix the arguments and rerun; the baseline was "
                            "left untouched",
                validity_ok=False, validity_reason=str(exc),
                maturity_mature=False,
                maturity_reason="maturity not established: invalid arguments",
                coverage_ok=False,
                coverage_reason="coverage not established: invalid "
                                "arguments",
                dry_run=dry_run))
        else:
            print("error: %s" % exc, file=sys.stderr)
        return 1
    counts = _stored_counts(baseline)
    mature, maturity_reason = _maturity_summary(counts)
    covered, coverage_reason = _coverage_summary(counts)
    total = baseline["provenance"]["total_samples"]
    if dry_run:
        if as_json:
            _print_outcome(_outcome(
                "record", args.baseline, args.samples,
                code="ok", retryable=False,
                next_action="rerun without --dry-run to publish the update",
                validity_ok=True,
                validity_reason="samples and stored evidence valid; update "
                                "computed, not published",
                maturity_mature=mature, maturity_reason=maturity_reason,
                coverage_ok=covered, coverage_reason=coverage_reason,
                dry_run=True, extra=_baseline_extra(baseline)))
        else:
            print("dry-run: would record: provisional=%s total_samples=%d"
                  % (baseline["provisional"], total))
        return 0
    try:
        _publish_json(args.baseline, baseline)
    except OSError as exc:
        return _report_publish_failure(args, exc, as_json, baseline)
    if as_json:
        _print_outcome(_outcome(
            "record", args.baseline, args.samples,
            code="ok", retryable=False,
            next_action="run check to verdict fresh samples" if mature else
                        "record more samples until both operations hold %d"
                        % MIN_SAMPLES_PER_OP,
            validity_ok=True,
            validity_reason="samples recorded and statistics recomputed",
            maturity_mature=mature, maturity_reason=maturity_reason,
            coverage_ok=covered, coverage_reason=coverage_reason,
            extra=_baseline_extra(baseline)))
    else:
        print("recorded: provisional=%s total_samples=%d"
              % (baseline["provisional"], total))
    return 0


def _cmd_check(args, grouped, as_json, dry_run):
    # `check` never writes: dry-run and live runs share one read-only path.
    if not os.path.exists(args.baseline):
        if as_json:
            _print_outcome(_outcome(
                "check", args.baseline, args.samples,
                code="unknown_missing_baseline", retryable=False,
                next_action="initialize with init or record at least %d "
                            "samples per operation, then check"
                            % MIN_SAMPLES_PER_OP,
                validity_ok=False,
                validity_reason="baseline file absent at %s" % args.baseline,
                maturity_mature=False,
                maturity_reason="maturity not established: no stored "
                                "evidence",
                coverage_ok=False,
                coverage_reason="no stored coverage",
                verdict="unknown", dry_run=dry_run))
        else:
            print("unknown: baseline not found at %s; run init or record "
                  "first" % args.baseline)
        return 3
    try:
        baseline = _check_baseline_shape(
            _read_json(args.baseline, "baseline"))
    except InvalidInput as exc:
        return _refuse_invalid_baseline(args, exc, as_json, True, dry_run)
    verdict, details = evaluate(baseline, grouped)
    mature, maturity_reason = _maturity_summary(_stored_counts(baseline))
    if as_json:
        if verdict == "unknown":
            reason = details.get("reason", "unknown")
            provisional = "provisional" in reason
            _print_outcome(_outcome(
                "check", args.baseline, args.samples,
                code=("unknown_provisional" if provisional else
                      "unknown_missing_samples"),
                retryable=False,
                next_action=("record more samples until both operations hold "
                             "%d" % MIN_SAMPLES_PER_OP if provisional else
                             "supply fresh samples for every operation, then "
                             "check"),
                validity_ok=True,
                validity_reason="stored evidence valid; no measured verdict",
                maturity_mature=mature, maturity_reason=maturity_reason,
                coverage_ok=False, coverage_reason=reason,
                verdict="unknown", dry_run=dry_run,
                extra={"details": details}))
        else:
            _print_outcome(_outcome(
                "check", args.baseline, args.samples,
                code="ok" if verdict == "ok" else "breach",
                retryable=False,
                next_action=("no action; provider latency within thresholds"
                             if verdict == "ok" else
                             "investigate provider operation latency; "
                             "thresholds unchanged"),
                validity_ok=True,
                validity_reason="fresh samples checked against mature "
                                "baseline thresholds",
                maturity_mature=True, maturity_reason=maturity_reason,
                coverage_ok=True,
                coverage_reason="fresh samples cover every operation",
                verdict=verdict, dry_run=dry_run,
                extra={"details": details}))
    else:
        print(json.dumps({"verdict": verdict, "details": details},
                         indent=2, sort_keys=True))
    return {"ok": 0, "breach": 2, "unknown": 3}[verdict]


def _usage_outcome(argv, message):
    """Build a coded outcome for flag/usage errors mentioning --json.

    argparse fails before parsed values exist, so the operation and input
    paths are recovered lexically; anything absent stays empty, never a
    guess, and nothing is written.
    """
    operation = "unknown"
    for token in argv:
        if token in ("init", "record", "check"):
            operation = token
            break
    baseline_path = ""
    samples_path = None
    for index, token in enumerate(argv):
        if token == "--baseline" and index + 1 < len(argv):
            baseline_path = argv[index + 1]
        elif token == "--samples" and index + 1 < len(argv):
            samples_path = argv[index + 1]
    if operation == "init":
        samples_path = None
    reason = "usage error: %s" % message
    return _outcome(
        operation, baseline_path, samples_path,
        code="usage_error", retryable=False,
        next_action="rerun with --help and valid flags; nothing was written",
        validity_ok=False, validity_reason=reason,
        maturity_mature=False,
        maturity_reason="maturity not established: usage error, no evidence "
                        "validated",
        coverage_ok=False,
        coverage_reason="coverage not established: usage error, no evidence "
                        "validated",
        verdict="unknown" if operation == "check" else None,
        dry_run="--dry-run" in argv)


class _ArgumentParser(argparse.ArgumentParser):
    # Effective argv for usage-error reporting, set by main() before parsing
    # so explicit argv (not the host process's sys.argv) controls --json.
    # A class attribute because add_subparsers builds subparsers from it.
    active_argv = None

    def error(self, message):
        argv = _ArgumentParser.active_argv
        if argv is None:
            argv = sys.argv[1:]
        if "--json" in argv:
            _print_outcome(_usage_outcome(argv, message))
        else:
            self.print_usage(sys.stderr)
            sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        self.exit(1)


def main(argv=None):
    parser = _ArgumentParser(
        description="Record GARM provider operation latency samples into "
                    "a baseline file, or check fresh samples against it. "
                    "Read-only except for the baseline path on record/init. "
                    "No network, no credentials, no host access.")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(subparser):
        subparser.add_argument("--baseline", required=True)
        subparser.add_argument(
            "--json", action="store_true",
            help="emit one versioned JSON outcome object (schema %s) "
                 "instead of text" % OUTCOME_SCHEMA)
        subparser.add_argument(
            "--dry-run", action="store_true",
            help="validate and compute without creating or changing the "
                 "baseline file")

    p_init = sub.add_parser("init", help="write a provisional baseline")
    _common(p_init)
    p_init.add_argument("--source", required=True,
                        help="where seed provenance comes from, e.g. "
                             "operator-supplied seed pending live samples")

    p_record = sub.add_parser("record", help="append samples, recompute")
    _common(p_record)
    p_record.add_argument("--samples", required=True)
    p_record.add_argument("--source", required=True,
                          help="sample origin, e.g. garm-api-read-metadata "
                               "export 2026-10-03 or operator-supplied")
    p_record.add_argument("--recorded-by", default="operator")
    p_record.add_argument("--now", default=None,
                          help="ISO-8601 clock override for determinism")

    p_check = sub.add_parser("check", help="verdict fresh samples vs "
                                           "thresholds")
    _common(p_check)
    p_check.add_argument("--samples", required=True)

    _ArgumentParser.active_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    as_json = args.json
    dry_run = args.dry_run
    if args.command == "init":
        return _cmd_init(args, as_json, dry_run)
    try:
        grouped = parse_samples(_read_json(args.samples, "samples"))
    except InvalidInput as exc:
        return _fail_samples(args, exc, as_json, dry_run)
    if args.command == "record":
        return _cmd_record(args, grouped, as_json, dry_run)
    return _cmd_check(args, grouped, as_json, dry_run)


if __name__ == "__main__":
    sys.exit(main())
