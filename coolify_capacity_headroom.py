"""Read-only capacity evidence replay. See docs/capacity-evidence.md."""

import datetime as dt
import json
import os
import re
import stat
import sys


VALUE_OPTIONS = {
    "--jobs": None,
    "--node7b1ff338-max": 2,
    "--node575e13db-max": 4,
    "--min-hours": 24,
    "--min-attempts": 10,
}
ARMS = ("coolify", "garm")
CLOCK = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
HELP = """Usage: coolify_capacity_headroom.sh --jobs sample.json [--json] [--dry-run]
  --node7b1ff338-max N   Existing nominal slot cap (default 2)
  --node575e13db-max N   Existing nominal slot cap (default 4)
  --min-hours N         Minimum observed hours per arm (default 24)
  --min-attempts N      Minimum completed attempts per arm (default 10)
  --json               Emit the version 1 report/error object
  --dry-run            Alias for the same read-only offline replay
  --help               Show this help (offline)
All numeric values are nonnegative whole numbers. No network, credential,
write or host operation exists. Bare arrays / {jobs: [...]} are accepted but
do not assert complete comparable coverage; residual headroom stays unknown.
GO/NO-GO need explicit complete cohort evidence and adequate aligned arms.
Reports exit 0 (including NO-GO/INCONCLUSIVE); input refusals exit 2.
Contract and synthetic example: docs/capacity-evidence.md
Offline checks:
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_capacity_evidence_contract
  bash test_coolify_capacity_headroom.sh
"""


class InputError(Exception):
    def __init__(self, code, reason, next_action, counts=None):
        self.code = code
        self.reason = reason
        self.next_action = next_action
        self.counts = counts


def parse_options(argv):
    options = dict(VALUE_OPTIONS)
    index = 0
    seen_options = set()
    while index < len(argv):
        flag = argv[index]
        if flag in ("--json", "--dry-run", "--help"):
            index += 1
            continue
        if flag not in options:
            raise InputError("UNSUPPORTED_OPTION", "unsupported option", "Use --help for supported offline options.")
        if flag in seen_options:
            raise InputError("INVALID_OPTION_VALUE", f"repeated value option {flag}", f"Supply {flag} exactly once.")
        seen_options.add(flag)
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise InputError("MISSING_OPTION_VALUE", f"missing value for {flag}", f"Supply a value after {flag}.")
        value = argv[index + 1]
        if flag == "--jobs":
            if not value or "\x00" in value:
                raise InputError("INVALID_OPTION_VALUE", "invalid jobs path", "Supply a nonempty local JSON file path.")
        else:
            try:
                if not re.fullmatch(r"[0-9]+", value):
                    raise ValueError
                value = int(value)
            except ValueError:
                raise InputError("INVALID_OPTION_VALUE", f"invalid value for {flag}", f"Use a nonnegative whole number for {flag}.") from None
        options[flag] = value
        index += 2
    if "--help" not in argv and options["--jobs"] is None:
        raise InputError("MISSING_OPTION_VALUE", "no --jobs file given", "Supply --jobs with a local JSON file.")
    return options


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError("non-JSON number")


def read_jobs(path):
    try:
        # Refuse FIFOs/devices before reading: malformed input must not hang.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("not a regular file")
            try:
                data = json.load(source, object_pairs_hook=unique_object, parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise InputError("INVALID_JOBS", "invalid JSON evidence", "Supply strict UTF-8 JSON with unique object keys.") from None
    except OSError:
        raise InputError("UNREADABLE_INPUT", "jobs file is not readable", "Supply a readable regular local JSON file.") from None
    rows = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(rows, list) or (rows and not any(isinstance(row, dict) for row in rows)):
        raise InputError("INVALID_JOBS", "expected a job array or {jobs: [...]}", "Supply an array containing job objects.")
    return rows, data.get("evidence") if isinstance(data, dict) else None


def clock(value):
    if not isinstance(value, str) or not CLOCK.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def timestamp(value):
    return value.isoformat(timespec="seconds") + "Z" if value is not None else None


def identity(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and not any(ord(c) < 32 for c in value)


def runner_class(runner):
    if runner.startswith("coolify-vps-"):
        return "coolify"
    if runner.startswith("garm-"):
        return "garm"
    if runner.startswith("ci-node7b1ff338-"):
        return "node7b1ff338"
    return "other"


def peak(rows):
    events = [(row["start"], 1) for row in rows] + [(row["end"], -1) for row in rows]
    current = maximum = 0
    # Ends sort before starts: execution intervals are [start, end).
    for _, delta in sorted(events):
        current += delta
        maximum = max(maximum, current)
    return maximum


def window(rows):
    return (min(row["start"] for row in rows), max(row["end"] for row in rows)) if rows else (None, None)


def hours(start, end):
    return (end - start).total_seconds() / 3600 if start is not None and end is not None else 0


def analyze(rows, evidence, options):
    counts = dict.fromkeys(("input_rows", "accepted_rows", "excluded_no_runner", "excluded_clock", "malformed_rows", "duplicate_rows", "unknown_rows"), 0)
    counts["input_rows"] = len(rows)
    accepted = []
    seen_ids = set()
    seen_intervals = set()
    for row in rows:
        if not isinstance(row, dict):
            counts["malformed_rows"] += 1
            continue
        job_id = row.get("id")
        runner = row.get("runner_name")
        if (("id" in row and (type(job_id) is not int or job_id <= 0))
                or (runner is not None and not isinstance(runner, str))
                or any(key in row and row[key] is not None and not isinstance(row[key], str)
                       for key in ("started_at", "completed_at", "name", "conclusion", "status", "cohort_id"))
                or ("labels" in row and (not isinstance(row["labels"], list)
                                        or not all(isinstance(label, str) for label in row["labels"])))):
            counts["malformed_rows"] += 1
            continue
        # Missing/null/empty runner assignment is never compute, regardless of
        # the queued/cancelled timestamp span or absent clocks.
        if not runner:
            counts["excluded_no_runner"] += 1
            continue
        if not identity(runner):
            counts["malformed_rows"] += 1
            continue
        start, end = clock(row.get("started_at")), clock(row.get("completed_at"))
        if (start is None or end is None or end <= start
                or ("conclusion" in row and not row["conclusion"])
                or ("status" in row and row["status"] != "completed")):
            counts["excluded_clock"] += 1
            continue
        # Check both identities even when an ID exists: mixed collector
        # projections must not count one execution with and without its ID.
        interval = (runner, start, end)
        duplicate = job_id in seen_ids or interval in seen_intervals
        if job_id is not None:
            seen_ids.add(job_id)
        seen_intervals.add(interval)
        if duplicate:
            counts["duplicate_rows"] += 1
            continue
        arm = runner_class(runner)
        if arm == "other":
            counts["unknown_rows"] += 1
        accepted.append(dict(id=job_id, start=start, end=end, arm=arm, cohort_id=row.get("cohort_id")))
    counts["accepted_rows"] = len(accepted)
    if not accepted:
        raise InputError("NO_COMPUTE", "zero runner-assigned jobs with valid completed clocks",
                         "Supply completed runner-assigned attempts; queue/cancel spans are not compute.", counts)

    cohort_id = evidence.get("cohort_id") if isinstance(evidence, dict) else None
    cohort_id = cohort_id if identity(cohort_id) else None
    claimed = evidence.get("window") if isinstance(evidence, dict) else None
    claimed_start = clock(claimed.get("start")) if isinstance(claimed, dict) else None
    claimed_end = clock(claimed.get("end")) if isinstance(claimed, dict) else None
    provenance = (isinstance(evidence, dict) and evidence.get("complete") is True
                  and cohort_id is not None
                  and isinstance(evidence.get("arms"), list)
                  and sorted(arm for arm in evidence["arms"] if isinstance(arm, str)) == sorted(ARMS)
                  and len(evidence["arms"]) == len(ARMS)
                  and claimed_start is not None and claimed_end is not None and claimed_end > claimed_start)
    observed_start, observed_end = window(accepted)
    complete = bool(provenance and not any(counts[key] for key in ("malformed_rows", "excluded_clock", "duplicate_rows", "unknown_rows"))
                    and all(row["cohort_id"] == cohort_id and claimed_start <= row["start"] < row["end"] <= claimed_end for row in accepted))
    arms = {}
    arm_rows = {}
    for arm in ARMS:
        arm_rows[arm] = [row for row in accepted if row["arm"] == arm]
        start, end = window(arm_rows[arm])
        observed_hours = hours(start, end)
        arms[arm] = dict(matched_attempts=len(arm_rows[arm]), observed_hours=round(observed_hours, 6),
                         adequate=bool(arm_rows[arm]) and len(arm_rows[arm]) >= options["--min-attempts"] and observed_hours >= options["--min-hours"],
                         complete=complete and bool(arm_rows[arm]), window=dict(start=timestamp(start), end=timestamp(end)))
    complete = complete and all(arms[arm]["complete"] for arm in ARMS)
    common_start = max((window(arm_rows[arm])[0] for arm in ARMS), default=None) if all(arm_rows.values()) else None
    common_end = min((window(arm_rows[arm])[1] for arm in ARMS), default=None) if all(arm_rows.values()) else None
    aligned_hours = max(0, hours(common_start, common_end))
    aligned = common_start is not None and common_end > common_start and aligned_hours >= options["--min-hours"]
    adequate = all(arms[arm]["adequate"] for arm in ARMS)
    known = bool(complete and adequate and aligned)
    nominal = options["--node7b1ff338-max"] + options["--node575e13db-max"]
    candidate_peak, incumbent_peak = (peak(arm_rows[arm]) for arm in ARMS)
    combined_peak = peak(arm_rows["coolify"] + arm_rows["garm"]) if known else None
    if not known:
        verdict, code = "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE"
        thin = [arm for arm in ARMS if not arms[arm]["adequate"]]
        reason = ("per-arm adequacy bound not met: " + ", ".join(thin)) if thin else "complete comparable execution coverage not established"
        next_action = ("Supply adequate completed attempts and observed hours for " + ", ".join(thin) + ". ") if thin else ""
        next_action += "Supply complete same-cohort coolify and garm coverage in an enclosing aligned window; resolve exclusions/unknowns. Do not infer free slots from nominal caps."
    elif combined_peak > nominal:
        verdict, code = "NO-GO", "CAPACITY_EXCEEDED"
        reason = f"aligned coolify + incumbent peak {combined_peak} exceeds {nominal} GARM slots"
        next_action = "Observed shared-slot demand exceeds nominal slots; retain the candidate. This report authorizes no fleet or cap changes."
    else:
        verdict, code = "GO", "MEASURED_FIT"
        reason = f"aligned coolify + incumbent peak {combined_peak} fits {nominal} GARM slots under supplied coverage assumptions"
        next_action = "Use this offline sample result only within its stated assumptions; live capacity and any host action require separate authorized evidence and gates."
    minutes = {arm: sum((row["end"] - row["start"]).total_seconds() for row in accepted if row["arm"] == arm) / 60
               for arm in (*ARMS, "node7b1ff338", "other")}
    return dict(schema_version=1, verdict=verdict, code=code, retryable=False, next_action=next_action, reason=reason,
                counts=counts, cohort_id=cohort_id, job_ids=sorted(row["id"] for row in accepted if row["id"] is not None), arms=arms,
                window=dict(start=timestamp(claimed_start if provenance else observed_start), end=timestamp(claimed_end if provenance else observed_end)),
                evidence_complete=complete, aligned_observed_hours=round(aligned_hours, 6),
                thresholds=dict(min_hours=options["--min-hours"], min_attempts=options["--min-attempts"]),
                capacity=dict(nominal_slots=nominal, candidate_peak=candidate_peak, incumbent_peak=incumbent_peak,
                              combined_peak=combined_peak, residual_headroom_known=known),
                assumptions=["Completeness, cohort identity and nominal caps are caller assertions, not verified live inventory.",
                             "Both arms share the asserted window; absent executions within complete coverage mean zero observed occupancy.",
                             "Candidate and incumbent GARM attempts each consume one logical shared slot during [start,end), not a dedicated CPU core.",
                             "Missing job IDs use distinct runner/start/end intervals as attempt identities; no-runner spans are never compute.",
                             "Replay measures this supplied sample only; no live headroom, decommission or pool-change authority is granted."],
                runner_minutes=dict(total=sum(minutes.values()), **minutes), span_h=hours(observed_start, observed_end), peak_all=peak(accepted))


def text_report(report):
    minutes, counts, capacity = report["runner_minutes"], report["counts"], report["capacity"]
    parts = [f"runner_minutes_total={minutes['total']:.2f}"]
    parts += [f"{arm}={minutes[arm]:.2f}" for arm in ("coolify", "node7b1ff338", "garm", "other")]
    parts += [f"{key}={counts[key]}" for key in ("excluded_no_runner", "excluded_clock", "malformed_rows", "duplicate_rows", "unknown_rows")]
    parts += [f"span_h={report['span_h']:.2f}", f"matched={counts['accepted_rows']}", f"peak_all={report['peak_all']}",
              f"coolify_peak={capacity['candidate_peak']}", f"garm_slots={capacity['nominal_slots']}",
              f"incumbent_peak={capacity['incumbent_peak']}", f"combined_peak={capacity['combined_peak'] if capacity['combined_peak'] is not None else 'unknown'}"]
    parts += [f"{arm}_matched={report['arms'][arm]['matched_attempts']} {arm}_hours={report['arms'][arm]['observed_hours']:.2f}" for arm in ARMS]
    parts += [f"verdict={report['verdict']}", f"code={report['code']}", f"reason={report['reason']}", f"next_action={report['next_action']}"]
    return "coolify_capacity_headroom: " + " ".join(parts)


def main(argv):
    machine = "--json" in argv
    try:
        options = parse_options(argv)
        if "--help" in argv:
            print(HELP, end="")
            return 0
        rows, evidence = read_jobs(options["--jobs"])
        report = analyze(rows, evidence, options)
    except InputError as error:
        report = dict(schema_version=1, verdict="ERROR", code=error.code, retryable=False,
                      reason=error.reason, next_action=error.next_action)
        if error.counts is not None:
            report["counts"] = error.counts
        if machine:
            print(json.dumps(report, sort_keys=True, allow_nan=False))
        else:
            detail = " ".join(f"{key}={value}" for key, value in (error.counts or {}).items())
            print(f"coolify_capacity_headroom: refusing — {error.code}: {error.reason}; {error.next_action} {detail}".rstrip(), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False) if machine else text_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
