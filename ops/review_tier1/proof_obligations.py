"""Advisory risk-to-evidence mapping over a caller-supplied change snapshot.

Run with one JSON object on stdin. Exit 0 means lexical mappings exist,
1 means mappings are missing, and 2 means input could not be measured.
None of these outcomes proves test execution or authorizes a merge.
"""

import importlib.util
import json
from pathlib import Path
import re
import sys

# Loading the fixed, local helper modules must not create bytecode files.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
FIELDS = {
    "base_sha", "head_sha", "changed_files", "labels", "additions",
    "deletions", "diff", "criteria", "boundary_criteria",
}
LIST_FIELDS = ("changed_files", "labels", "criteria", "boundary_criteria")
LAYER_LIMITS = (
    ("tier0", "Deterministic rules cover only configured patterns and scan scope; "
     "a skipped or unconfigured scanner is not a clean scan."),
    ("tier1", "Lexical symbol and test-name mapping does not prove that a test "
     "ran, passed, or exercised the stated behavior."),
    ("tier2", "Review judgment and runtime correctness remain unverified; "
     "this report cannot replace independent review or staging evidence."),
)


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deterministic = load_helper(
    "proof_deterministic", ROOT / "ops/review_tier1/deterministic.py"
)
review_trigger = load_helper(
    "proof_review_trigger", ROOT / ".github/scripts/review_trigger.py"
)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate-object-key")
        obj[key] = value
    return obj


def reject_constant(_value):
    raise ValueError("invalid-json-constant")


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        return ["invalid-fields"]
    errors = []
    for field in ("base_sha", "head_sha"):
        value = payload[field]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            errors.append("invalid-" + field)
    for field in LIST_FIELDS:
        value = payload[field]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            errors.append("invalid-" + field)
        elif field in ("criteria", "boundary_criteria") and len(set(value)) != len(value):
            errors.append("duplicate-" + field)
    for field in ("additions", "deletions"):
        if type(payload[field]) is not int or payload[field] < 0:
            errors.append("invalid-" + field)
    if not isinstance(payload["diff"], str):
        errors.append("invalid-diff")
    return errors


def empty_report():
    return {
        "schema": "review-proof-obligations.v1",
        "mode": "warn",
        "merge_authority": False,
        "execution_proven": False,
        "base_sha": None,
        "head_sha": None,
        "risk": None,
        "outcome": "unmeasurable",
        "obligations": [],
        "missing": [],
        "layers": [
            {"id": tier, "execution_status": "not_observed", "blind_spots": [limit]}
            for tier, limit in LAYER_LIMITS
        ],
        "errors": [],
    }


def evaluate(payload):
    report = empty_report()
    report["errors"] = validate(payload)
    if report["errors"]:
        return report, 2

    risky, reasons = review_trigger.is_risky(
        payload["changed_files"], payload["labels"],
        payload["additions"], payload["deletions"],
    )
    report["base_sha"] = payload["base_sha"]
    report["head_sha"] = payload["head_sha"]
    report["risk"] = {"level": "elevated" if risky else "ordinary", "reasons": reasons}
    files = deterministic.changed_files(payload["diff"])
    symbols = deterministic.changed_symbols(payload["diff"])
    tests = deterministic.changed_tests(payload["diff"])
    for kind, field, required in (
        ("acceptance", "criteria", True),
        ("boundary", "boundary_criteria", risky),
    ):
        criteria = payload[field]
        mapping = deterministic.ac_coverage(criteria, files, symbols, tests)
        for criterion in criteria:
            symbol, test = mapping[criterion]
            report["obligations"].append({
                "kind": kind, "criterion": criterion, "symbol": symbol,
                "test": test, "mapped": bool(symbol and test),
            })
        if required and not criteria:
            report["missing"].append(kind + "-criteria")
        elif criteria and not deterministic.ac_all_mapped(mapping):
            report["missing"].append(kind + "-mapping")
    report["outcome"] = "missing" if report["missing"] else "mapped"
    return report, int(bool(report["missing"]))


def main():
    try:
        payload = json.loads(
            sys.stdin.buffer.read().decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object, parse_constant=reject_constant,
        )
    except (ValueError, RecursionError, UnicodeError):
        report = empty_report()
        report["errors"] = ["invalid-json"]
        code = 2
    else:
        try:
            report, code = evaluate(payload)
        except (ValueError, RecursionError):
            # The unchanged classifier can exceed the runtime's integer
            # formatting limit even when both input counts parsed safely.
            report = empty_report()
            report["errors"] = ["evaluation-unmeasurable"]
            code = 2
    print(json.dumps(report, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
