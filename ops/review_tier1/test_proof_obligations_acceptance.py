"""Acceptance tests for the risk-based review proof-obligations CLI.

Hermetic and black-box: every case drives the public CLI with JSON on
stdin and asserts on its stdout JSON report and exit code. Most use
``python3 -B ops/review_tier1/proof_obligations.py``; the isolated no-write
control omits ``-B`` so the interpreter cannot mask CLI writes. The existing
classifier (``.github/scripts/review_trigger.py:is_risky``) and the existing
lexical mapping helpers (``ops/review_tier1/deterministic.py``) are loaded
only as oracles for the values the adapter must reuse verbatim; no other
implementation detail is exercised.

Run from the repo root:
  python3 -B -m unittest discover -s ops/review_tier1 \\
      -p 'test_proof_obligations_acceptance.py' -v
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REL_SCRIPT = "ops/review_tier1/proof_obligations.py"
ABS_SCRIPT = REPO_ROOT / REL_SCRIPT


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deterministic = _load(
    "proof_obligations_oracle_deterministic",
    REPO_ROOT / "ops" / "review_tier1" / "deterministic.py",
)
review_trigger = _load(
    "proof_obligations_oracle_review_trigger",
    REPO_ROOT / ".github" / "scripts" / "review_trigger.py",
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

PRICING_CRITERION = "pricing_total sums all items"
REFUND_CRITERION = "refund_cap rejects amounts over the cap"
SHIPPING_CRITERION = "shipping_quote rounds up weight"
UNMAPPED_CRITERION = "zebra_quota reconciles lunar invoices"
OTHER_UNMAPPED_CRITERION = "orbital_ledger validates martian taxes"

# A fictitious diff: the named files do not exist in this repository, so a
# passing run also proves the adapter treats the diff as caller-supplied
# data rather than reading named files from disk.
POSITIVE_DIFF = """diff --git a/src/pricing.py b/src/pricing.py
index 111..222 100644
--- a/src/pricing.py
+++ b/src/pricing.py
@@ -1,2 +1,7 @@ def pricing_total(items):
+def pricing_total(items):
+    return sum(items)
+def refund_cap(amount):
+    return min(amount, 100)
+def shipping_quote(weight):
+    return weight
diff --git a/tests/test_pricing.py b/tests/test_pricing.py
index 333..444 100644
--- a/tests/test_pricing.py
+++ b/tests/test_pricing.py
@@ -0,0 +1,6 @@
+def test_pricing_total_sums_items(self):
+    pass
+def test_refund_cap_rejects_over_cap(self):
+    pass
+def test_shipping_quote_rounds_up_weight(self):
+    pass
"""

PARTIAL_CRITERION = "reserve_stock handles shortages"
SYMBOL_ONLY_DIFF = """diff --git a/src/stock.py b/src/stock.py
--- a/src/stock.py
+++ b/src/stock.py
@@ -0,0 +1,2 @@
+def reserve_stock(amount):
+    return amount
"""
# The test name is context, not an added definition or hunk symbol. This
# supplies a test match without also creating a changed-symbol match.
TEST_ONLY_DIFF = """diff --git a/tests/test_stock.py b/tests/test_stock.py
--- a/tests/test_stock.py
+++ b/tests/test_stock.py
@@ -1,3 +1,4 @@
 # Existing stock regression.
 def test_reserve_stock_rejects_shortages():
+    assert reserve_stock(0) == 0
     pass
"""

TOP_LEVEL_KEYS = {
    "schema",
    "mode",
    "merge_authority",
    "execution_proven",
    "base_sha",
    "head_sha",
    "risk",
    "outcome",
    "obligations",
    "missing",
    "layers",
    "errors",
}
OBLIGATION_KEYS = {"kind", "criterion", "symbol", "test", "mapped"}
LAYER_IDS = ["tier0", "tier1", "tier2"]
LAYER_HINTS = {
    "tier0": (
        "deterministic", "config", "rule", "scan", "lint", "pattern",
        "parser", "static",
    ),
    "tier1": (
        "lexical", "symbol", "test name", "test-name", "name", "mapping",
        "mapped", "execution",
    ),
    "tier2": (
        "judgment", "judgement", "runtime", "human", "review", "execution",
        "semantic", "correctness",
    ),
}
MISSING_ORDER = [
    "acceptance-criteria",
    "acceptance-mapping",
    "boundary-criteria",
    "boundary-mapping",
]


def valid_payload(**overrides):
    payload = {
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "changed_files": ["src/pricing.py", "tests/test_pricing.py"],
        "labels": ["enhancement"],
        "additions": 12,
        "deletions": 3,
        "diff": POSITIVE_DIFF,
        "criteria": [PRICING_CRITERION],
        "boundary_criteria": [],
    }
    payload.update(overrides)
    return payload


def unicode_payload():
    return valid_payload(
        changed_files=["src/café_雪.py", "tests/test_pricing.py"],
        diff=POSITIVE_DIFF + "+# ENCODEDSNAPSHOT café 雪 🌱\n",
    )


def expected_mapping(criteria, diff):
    return deterministic.ac_coverage(
        list(criteria),
        deterministic.changed_files(diff),
        deterministic.changed_symbols(diff),
        deterministic.changed_tests(diff),
    )


class ProofObligationsAcceptanceTest(unittest.TestCase):
    maxDiff = None

    # -- CLI harness ---------------------------------------------------

    def run_cli(self, input_text, *, cwd=None, script=None):
        return subprocess.run(
            [sys.executable, "-B", script or REL_SCRIPT],
            input=input_text,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def run_payload(self, payload, **kwargs):
        return self.run_cli(json.dumps(payload), **kwargs)

    def run_cli_bytes(self, input_bytes):
        proc = subprocess.run(
            [sys.executable, "-B", REL_SCRIPT],
            input=input_bytes,
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=30,
        )
        try:
            proc.stdout = proc.stdout.decode("utf-8")
            proc.stderr = proc.stderr.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("CLI must emit UTF-8 output without raw invalid bytes")
        return proc

    def report_from(self, proc):
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        try:
            report = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            self.fail(
                "stdout is not one JSON object: %r (stdout=%r stderr=%r)"
                % (exc, proc.stdout, proc.stderr)
            )
        self.assertIsInstance(report, dict)
        return report

    # -- Shared report assertions ---------------------------------------

    def layers_by_id(self, report):
        layers = report["layers"]
        by_id = {}
        if isinstance(layers, dict):
            self.assertEqual(list(layers), LAYER_IDS)
            for layer_id in LAYER_IDS:
                by_id[layer_id] = layers[layer_id]
            return by_id
        self.assertIsInstance(layers, list)
        ordered_ids = []
        for record in layers:
            self.assertIsInstance(record, dict)
            layer_id = None
            for key in ("id", "tier", "name", "layer"):
                if record.get(key) in LAYER_IDS:
                    layer_id = record[key]
                    break
            self.assertIsNotNone(
                layer_id, "layer record names no tier: %r" % (record,)
            )
            ordered_ids.append(layer_id)
            by_id[layer_id] = record
        self.assertEqual(ordered_ids, LAYER_IDS)
        return by_id

    def blind_spot_text(self, layer_id, record):
        self.assertIsInstance(record, dict)
        self.assertEqual(record.get("execution_status"), "not_observed")
        blind_spots = record.get("blind_spots")
        if isinstance(blind_spots, str):
            text = blind_spots
        else:
            self.assertIsInstance(blind_spots, list)
            self.assertTrue(blind_spots, "blind_spots must be nonempty")
            for item in blind_spots:
                self.assertIsInstance(item, str)
                self.assertTrue(item)
            text = " ".join(blind_spots)
        self.assertTrue(text.strip(), "blind_spots must be nonempty")
        lowered = text.lower()
        self.assertTrue(
            any(hint in lowered for hint in LAYER_HINTS[layer_id]),
            "%s blind spots do not describe its limits: %r"
            % (layer_id, text),
        )
        return lowered

    def assert_common(self, report):
        self.assertEqual(set(report), TOP_LEVEL_KEYS)
        self.assertEqual(report["schema"], "review-proof-obligations.v1")
        self.assertEqual(report["mode"], "warn")
        self.assertIs(report["merge_authority"], False)
        self.assertIs(report["execution_proven"], False)
        self.assertIn(report["outcome"], ("mapped", "missing", "unmeasurable"))
        self.assertIsInstance(report["obligations"], list)
        self.assertIsInstance(report["missing"], list)
        for missing_id in report["missing"]:
            self.assertIn(missing_id, MISSING_ORDER)
        self.assertEqual(
            report["missing"],
            [m for m in MISSING_ORDER if m in report["missing"]],
            "missing ids must be unique and in the stable order",
        )
        self.assertIsInstance(report["errors"], list)
        for error in report["errors"]:
            self.assertIsInstance(error, str)
            self.assertTrue(error)
            self.assertNotIn("Traceback", error)
        by_id = self.layers_by_id(report)
        for layer_id in LAYER_IDS:
            self.blind_spot_text(layer_id, by_id[layer_id])
        return by_id

    def assert_valid(self, proc, payload, expected_exit, expected_outcome):
        report = self.report_from(proc)
        self.assert_common(report)
        self.assertEqual(
            proc.returncode, expected_exit,
            "stdout=%r stderr=%r" % (proc.stdout, proc.stderr),
        )
        self.assertEqual(report["outcome"], expected_outcome)
        self.assertEqual(report["base_sha"], payload["base_sha"])
        self.assertEqual(report["head_sha"], payload["head_sha"])
        self.assertEqual(report["errors"], [])
        risk = report["risk"]
        self.assertIsInstance(risk, dict)
        self.assertEqual(set(risk), {"level", "reasons"})
        risky, reasons = review_trigger.is_risky(
            payload["changed_files"],
            payload["labels"],
            payload["additions"],
            payload["deletions"],
        )
        self.assertEqual(risk["level"], "elevated" if risky else "ordinary")
        self.assertEqual(risk["reasons"], reasons)
        return report

    def assert_refused(self, proc):
        report = self.report_from(proc)
        self.assert_common(report)
        self.assertEqual(
            proc.returncode, 2,
            "stdout=%r stderr=%r" % (proc.stdout, proc.stderr),
        )
        self.assertEqual(report["outcome"], "unmeasurable")
        self.assertIsNone(report["base_sha"])
        self.assertIsNone(report["head_sha"])
        self.assertIsNone(report["risk"])
        self.assertEqual(report["obligations"], [])
        self.assertEqual(report["missing"], [])
        self.assertTrue(report["errors"], "refusal must carry diagnostics")
        return report

    def assert_obligations(self, report, payload):
        expected = []
        for kind, criteria in (
            ("acceptance", payload["criteria"]),
            ("boundary", payload["boundary_criteria"]),
        ):
            mapping = expected_mapping(criteria, payload["diff"])
            for criterion in criteria:
                symbol, test = mapping[criterion]
                expected.append(
                    {
                        "kind": kind,
                        "criterion": criterion,
                        "symbol": symbol,
                        "test": test,
                        "mapped": bool(symbol and test),
                    }
                )
        actual = report["obligations"]
        self.assertEqual(len(actual), len(expected))
        for record, want in zip(actual, expected):
            self.assertIsInstance(record, dict)
            self.assertEqual(set(record), OBLIGATION_KEYS)
            self.assertIsInstance(record["symbol"], str)
            self.assertIsInstance(record["test"], str)
            self.assertIsInstance(record["mapped"], bool)
            self.assertEqual(
                record["mapped"], bool(record["symbol"] and record["test"])
            )
            self.assertEqual(record, want)
        return expected

    # -- Ordinary / mapped behaviour ------------------------------------

    def test_ordinary_mapped_acceptance_exits_zero(self):
        payload = valid_payload()
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assert_obligations(report, payload)
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["risk"], {"level": "ordinary", "reasons": []})

    def test_mapped_records_never_prove_execution_or_merge_authority(self):
        payload = valid_payload(
            labels=["security"],
            criteria=[PRICING_CRITERION],
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assert_obligations(report, payload)
        self.assertTrue(all(r["mapped"] for r in report["obligations"]))
        self.assertIs(report["execution_proven"], False)
        self.assertIs(report["merge_authority"], False)
        self.assertEqual(report["mode"], "warn")
        for record in self.layers_by_id(report).values():
            self.assertEqual(record["execution_status"], "not_observed")

    def test_layers_declare_distinct_blind_spots_without_observation(self):
        payload = valid_payload()
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        by_id = self.layers_by_id(report)
        texts = {
            self.blind_spot_text(layer_id, by_id[layer_id])
            for layer_id in LAYER_IDS
        }
        self.assertEqual(
            len(texts), 3, "each tier must declare its own limits"
        )

    # -- Risk classification parity --------------------------------------

    def test_risk_parity_with_canonical_classifier(self):
        # Boundary criteria are supplied and mapped in every case so the
        # exit code isolates classification, not obligation gaps.
        cases = [
            ("ordinary", {}),
            ("workflow path", {"changed_files": [".github/workflows/ci.yml"]}),
            ("auth path", {"changed_files": ["src/auth/login.py"]}),
            ("security label", {"labels": ["security"]}),
            ("tier label", {"labels": ["tier-1"]}),
            ("risk label", {"labels": ["risk: high"]}),
            ("exactly 600 lines", {"additions": 400, "deletions": 200}),
            ("601 lines", {"additions": 601, "deletions": 0}),
            ("zero lines", {"additions": 0, "deletions": 0}),
            (
                "all reasons, unsorted and duplicated inputs",
                {
                    "changed_files": [
                        "f/billing.py", "SRC/AUTH/Login.py", "a/secret.py",
                        "SRC/AUTH/Login.py", "b/token.py", "c/migrate.py",
                        "d/deploy.py", "e/policy.py", "g/quota.py",
                    ],
                    "labels": ["enhancement", "SECURITY", "SECURITY"],
                    "additions": 400,
                    "deletions": 201,
                },
            ),
            (
                "uppercase risky path and label",
                {
                    "changed_files": ["SRC/AUTH/Login.py"],
                    "labels": ["SECURITY"],
                },
            ),
        ]
        for name, overrides in cases:
            with self.subTest(case=name):
                payload = valid_payload(
                    boundary_criteria=[REFUND_CRITERION], **overrides
                )
                report = self.assert_valid(
                    self.run_payload(payload), payload, 0, "mapped"
                )
                self.assert_obligations(report, payload)

    def test_large_diff_threshold_is_strictly_above_600(self):
        at_limit = valid_payload(
            additions=400, deletions=200,
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(
            self.run_payload(at_limit), at_limit, 0, "mapped"
        )
        self.assertEqual(report["risk"], {"level": "ordinary", "reasons": []})

        over_limit = valid_payload(
            additions=400, deletions=201,
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(
            self.run_payload(over_limit), over_limit, 0, "mapped"
        )
        self.assertEqual(
            report["risk"],
            {
                "level": "elevated",
                "reasons": ["large diff: 601 lines (> 600)"],
            },
        )

    def test_elevated_reason_strings_are_the_canonical_ones(self):
        payload = valid_payload(
            changed_files=["src/auth/login.py"],
            labels=["security"],
            additions=1,
            deletions=1,
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assertEqual(
            report["risk"]["reasons"],
            [
                "risky paths: src/auth/login.py",
                "risk labels: security",
            ],
        )

    def test_audit_sample_head_does_not_elevate_risk(self):
        sample_sha = None
        for i in range(1000):
            candidate = "%040x" % i
            if review_trigger.in_audit_sample(candidate):
                sample_sha = candidate
                break
        self.assertIsNotNone(sample_sha, "oracle found no audit-sample SHA")
        payload = valid_payload(head_sha=sample_sha)
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assertEqual(report["risk"], {"level": "ordinary", "reasons": []})

    def test_classification_uses_supplied_paths_not_diff_paths(self):
        # A supplied risky path whose name contains quotes, a space and
        # non-ASCII text must classify from the supplied list even though
        # the diff itself names only ordinary files.
        supplied = valid_payload(
            changed_files=['src/"üser auth module".py'],
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(
            self.run_payload(supplied), supplied, 0, "mapped"
        )
        self.assertEqual(report["risk"]["level"], "elevated")

        # Conversely, a risky path named only inside the diff text must not
        # elevate a change whose supplied paths are ordinary.
        risky_diff = POSITIVE_DIFF.replace(
            "src/pricing.py", "src/auth/pricing.py"
        )
        diff_only = valid_payload(diff=risky_diff)
        report = self.assert_valid(
            self.run_payload(diff_only), diff_only, 0, "mapped"
        )
        self.assertEqual(report["risk"], {"level": "ordinary", "reasons": []})
        self.assert_obligations(report, diff_only)

    # -- Obligations and missing ids --------------------------------------

    def test_obligations_preserve_order_with_acceptance_first(self):
        payload = valid_payload(
            labels=["security"],
            criteria=[SHIPPING_CRITERION, PRICING_CRITERION],
            boundary_criteria=[REFUND_CRITERION],
        )
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assert_obligations(report, payload)
        self.assertEqual(
            [(r["kind"], r["criterion"]) for r in report["obligations"]],
            [
                ("acceptance", SHIPPING_CRITERION),
                ("acceptance", PRICING_CRITERION),
                ("boundary", REFUND_CRITERION),
            ],
        )

        payload = valid_payload(
            labels=["security"],
            criteria=[PRICING_CRITERION],
            boundary_criteria=[SHIPPING_CRITERION, REFUND_CRITERION],
        )
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assert_obligations(report, payload)
        self.assertEqual(
            [(r["kind"], r["criterion"]) for r in report["obligations"]],
            [
                ("acceptance", PRICING_CRITERION),
                ("boundary", SHIPPING_CRITERION),
                ("boundary", REFUND_CRITERION),
            ],
        )

    def test_missing_obligations_matrix(self):
        elevated = {"labels": ["security"]}
        cases = [
            (
                "ordinary without acceptance criteria cannot pass vacuously",
                {"criteria": [], "boundary_criteria": []},
                1, ["acceptance-criteria"],
            ),
            (
                "ordinary with unmapped acceptance criterion",
                {"criteria": [UNMAPPED_CRITERION]},
                1, ["acceptance-mapping"],
            ),
            (
                "ordinary with partially mapped acceptance criteria",
                {"criteria": [PRICING_CRITERION, UNMAPPED_CRITERION]},
                1, ["acceptance-mapping"],
            ),
            (
                "empty diff supplies no mapping evidence",
                {"diff": "", "criteria": [PRICING_CRITERION]},
                1, ["acceptance-mapping"],
            ),
            (
                "ordinary supplied boundary gap is reported, not hidden",
                {"boundary_criteria": [UNMAPPED_CRITERION]},
                1, ["boundary-mapping"],
            ),
            (
                "ordinary supplied mapped boundary passes",
                {"boundary_criteria": [REFUND_CRITERION]},
                0, [],
            ),
            (
                "ordinary empty acceptance plus supplied boundary gap",
                {
                    "criteria": [],
                    "boundary_criteria": [UNMAPPED_CRITERION],
                },
                1, ["acceptance-criteria", "boundary-mapping"],
            ),
            (
                "elevated without boundary criteria",
                dict(elevated),
                1, ["boundary-criteria"],
            ),
            (
                "elevated with unmapped boundary criterion",
                dict(elevated, boundary_criteria=[UNMAPPED_CRITERION]),
                1, ["boundary-mapping"],
            ),
            (
                "elevated with neither criteria list",
                dict(elevated, criteria=[], boundary_criteria=[]),
                1, ["acceptance-criteria", "boundary-criteria"],
            ),
            (
                "elevated with both lists unmapped",
                dict(
                    elevated,
                    criteria=[UNMAPPED_CRITERION],
                    boundary_criteria=[OTHER_UNMAPPED_CRITERION],
                ),
                1, ["acceptance-mapping", "boundary-mapping"],
            ),
            (
                "elevated empty acceptance and unmapped boundary",
                dict(
                    elevated, criteria=[],
                    boundary_criteria=[UNMAPPED_CRITERION],
                ),
                1, ["acceptance-criteria", "boundary-mapping"],
            ),
            (
                "elevated unmapped acceptance and empty boundary",
                dict(elevated, criteria=[UNMAPPED_CRITERION]),
                1, ["acceptance-mapping", "boundary-criteria"],
            ),
            (
                "elevated fully mapped",
                dict(elevated, boundary_criteria=[REFUND_CRITERION]),
                0, [],
            ),
        ]
        for name, overrides, expected_exit, expected_missing in cases:
            with self.subTest(case=name):
                payload = valid_payload(**overrides)
                expected_outcome = "mapped" if expected_exit == 0 else "missing"
                report = self.assert_valid(
                    self.run_payload(payload),
                    payload,
                    expected_exit,
                    expected_outcome,
                )
                self.assert_obligations(report, payload)
                self.assertEqual(report["missing"], expected_missing)

    def assert_one_sided_mapping_missing(self, kind, diff, symbol, test):
        if kind == "acceptance":
            payload = valid_payload(diff=diff, criteria=[PARTIAL_CRITERION])
        else:
            payload = valid_payload(
                diff=POSITIVE_DIFF + diff,
                labels=["security"],
                boundary_criteria=[PARTIAL_CRITERION],
            )
        report = self.assert_valid(
            self.run_payload(payload), payload, 1, "missing"
        )
        self.assertEqual(report["missing"], [kind + "-mapping"])
        self.assertEqual(
            len(report["obligations"]), 1 if kind == "acceptance" else 2
        )
        record = report["obligations"][-1]
        self.assertEqual(
            record,
            {
                "kind": kind,
                "criterion": PARTIAL_CRITERION,
                "symbol": symbol,
                "test": test,
                "mapped": False,
            },
        )
        self.assertIs(record["mapped"], False)
        if kind == "boundary":
            self.assertIs(report["obligations"][0]["mapped"], True)

    def test_acceptance_symbol_without_test_is_missing(self):
        self.assert_one_sided_mapping_missing(
            "acceptance", SYMBOL_ONLY_DIFF, "reserve_stock", ""
        )

    def test_acceptance_test_without_symbol_is_missing(self):
        self.assert_one_sided_mapping_missing(
            "acceptance", TEST_ONLY_DIFF, "", "test_reserve_stock_rejects_shortages"
        )

    def test_boundary_symbol_without_test_is_missing(self):
        self.assert_one_sided_mapping_missing(
            "boundary", SYMBOL_ONLY_DIFF, "reserve_stock", ""
        )

    def test_boundary_test_without_symbol_is_missing(self):
        self.assert_one_sided_mapping_missing(
            "boundary", TEST_ONLY_DIFF, "", "test_reserve_stock_rejects_shortages"
        )

    def test_same_criterion_may_appear_in_both_lists(self):
        payload = valid_payload(
            labels=["security"],
            criteria=[PRICING_CRITERION],
            boundary_criteria=[PRICING_CRITERION],
        )
        report = self.assert_valid(self.run_payload(payload), payload, 0, "mapped")
        self.assert_obligations(report, payload)
        self.assertEqual(
            [r["kind"] for r in report["obligations"]],
            ["acceptance", "boundary"],
        )

    def test_empty_lists_and_empty_diff_are_valid_input_but_missing(self):
        payload = valid_payload(
            changed_files=[], labels=[], diff="",
            criteria=[], boundary_criteria=[],
            additions=0, deletions=0,
        )
        report = self.assert_valid(self.run_payload(payload), payload, 1, "missing")
        self.assertEqual(report["risk"], {"level": "ordinary", "reasons": []})
        self.assertEqual(report["obligations"], [])
        self.assertEqual(report["missing"], ["acceptance-criteria"])

    # -- Malformed input refusal -------------------------------------------

    def test_malformed_json_and_non_object_inputs_refuse(self):
        valid_text = json.dumps(valid_payload())
        cases = [
            ("empty stdin", ""),
            ("whitespace only", "  \n"),
            ("not json", "not json"),
            ("truncated object", "{"),
            ("trailing second object", valid_text + " {}"),
            ("top level list", "[]"),
            ("top level null", "null"),
            ("top level string", '"text"'),
            ("top level number", "42"),
            ("empty object", "{}"),
        ]
        for name, raw in cases:
            with self.subTest(case=name):
                self.assert_refused(self.run_cli(raw))

    def test_malformed_json_does_not_echo_raw_input(self):
        sentinel = "RAWINPUTSENTINEL12345"
        raw = '{"base_sha": "%s", ' % sentinel
        proc = self.run_cli(raw)
        report = self.assert_refused(proc)
        self.assertNotIn(sentinel, proc.stdout)
        self.assertNotIn(sentinel, " ".join(report["errors"]))

    def test_utf8_unicode_snapshot_bytes_map(self):
        payload = unicode_payload()
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        report = self.assert_valid(
            self.run_cli_bytes(raw), payload, 0, "mapped"
        )
        self.assert_obligations(report, payload)

    def test_utf16_bom_snapshot_bytes_refuse_safely(self):
        raw = json.dumps(unicode_payload(), ensure_ascii=False).encode("utf-16")
        self.assertTrue(raw.startswith((b"\xff\xfe", b"\xfe\xff")))
        proc = self.run_cli_bytes(raw)
        self.assert_refused(proc)
        self.assertNotIn("ENCODEDSNAPSHOT", proc.stdout + proc.stderr)

    def test_utf32_bom_snapshot_bytes_refuse_safely(self):
        raw = json.dumps(unicode_payload(), ensure_ascii=False).encode("utf-32")
        self.assertTrue(
            raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        )
        proc = self.run_cli_bytes(raw)
        self.assert_refused(proc)
        self.assertNotIn("ENCODEDSNAPSHOT", proc.stdout + proc.stderr)

    def test_invalid_utf8_bytes_refuse_safely(self):
        sentinel = "INVALIDUTF8SENTINEL12345"
        echo_marker = "RAWUTF8DIAGNOSTICS12345"
        payload = valid_payload(
            diff=POSITIVE_DIFF + "+# %s %s\n" % (echo_marker, sentinel)
        )
        raw = json.dumps(payload).encode("utf-8")
        encoded_sentinel = sentinel.encode("ascii")
        self.assertEqual(raw.count(encoded_sentinel), 1)
        self.assert_valid(self.run_cli_bytes(raw), payload, 0, "mapped")

        # Pass bytes directly to stdin; decoding here would hide the defect.
        proc = self.run_cli_bytes(raw.replace(encoded_sentinel, b"\xff", 1))
        self.assert_refused(proc)
        for output in (proc.stdout, proc.stderr):
            self.assertNotIn(echo_marker, output)
            self.assertNotIn("\udcff", output)
            self.assertNotIn("\\udcff", output.lower())

    def test_count_reason_formatting_limit_refuses_safely(self):
        get_limit = getattr(sys, "get_int_max_str_digits", None)
        if get_limit is None:
            self.skipTest("runtime has no integer-string conversion limit")
        limit = get_limit()
        if limit == 0:
            self.skipTest("runtime integer-string conversion limit is disabled")

        count = int("9" * limit)
        payload = valid_payload(
            additions=count,
            deletions=count,
            boundary_criteria=[REFUND_CRITERION],
        )
        raw = json.dumps(payload)
        parsed = json.loads(raw)
        self.assertEqual(parsed["additions"], count)
        self.assertEqual(parsed["deletions"], count)
        # Each count is legal; only formatting their sum crosses the limit.
        with self.assertRaises(ValueError):
            str(count + count)

        proc = self.run_cli(raw)
        self.assert_refused(proc)
        for output in (proc.stdout, proc.stderr):
            self.assertNotIn("9" * 64, output)

    def test_duplicate_object_keys_refuse(self):
        payload = valid_payload()
        for dup_key, dup_value in (
            ("diff", ""),
            ("base_sha", "c" * 40),
            ("criteria", [REFUND_CRITERION]),
        ):
            with self.subTest(duplicate=dup_key):
                pairs = list(payload.items())
                pairs.append((dup_key, dup_value))
                raw = "{" + ", ".join(
                    "%s: %s" % (json.dumps(k), json.dumps(v)) for k, v in pairs
                ) + "}"
                self.assert_refused(self.run_cli(raw))

    def test_missing_and_unknown_fields_refuse(self):
        for field in list(valid_payload()):
            with self.subTest(missing=field):
                payload = valid_payload()
                del payload[field]
                self.assert_refused(self.run_payload(payload))
        for extra in ("extra", "is_draft", "approve"):
            with self.subTest(unknown=extra):
                payload = valid_payload()
                payload[extra] = True
                self.assert_refused(self.run_payload(payload))

    def test_wrong_types_and_values_refuse(self):
        cases = [
            ("short base_sha", {"base_sha": "abc123"}),
            ("uppercase base_sha", {"base_sha": "A" * 40}),
            ("non-hex base_sha", {"base_sha": "g" * 40}),
            ("41-char head_sha", {"head_sha": "a" * 41}),
            ("numeric head_sha", {"head_sha": 123}),
            ("null head_sha", {"head_sha": None}),
            ("changed_files as string", {"changed_files": "src/pricing.py"}),
            ("changed_files empty string", {"changed_files": [""]}),
            ("changed_files numeric element", {"changed_files": [123]}),
            ("changed_files null", {"changed_files": None}),
            ("labels as object", {"labels": {}}),
            ("labels empty string", {"labels": [""]}),
            ("labels null element", {"labels": [None]}),
            ("boolean additions", {"additions": True}),
            ("boolean deletions", {"deletions": False}),
            ("negative additions", {"additions": -1}),
            ("negative deletions", {"deletions": -1}),
            ("fractional deletions", {"deletions": 1.5}),
            ("float additions", {"additions": 1.0}),
            ("string additions", {"additions": "10"}),
            ("null diff", {"diff": None}),
            ("numeric diff", {"diff": 123}),
            ("list diff", {"diff": [POSITIVE_DIFF]}),
            ("criteria as string", {"criteria": PRICING_CRITERION}),
            ("criteria empty string", {"criteria": [""]}),
            ("criteria numeric element", {"criteria": [42]}),
            (
                "duplicate criteria",
                {"criteria": [PRICING_CRITERION, PRICING_CRITERION]},
            ),
            (
                "duplicate boundary criteria",
                {"boundary_criteria": [REFUND_CRITERION, REFUND_CRITERION]},
            ),
            ("boundary empty string", {"boundary_criteria": [""]}),
            ("boundary null element", {"boundary_criteria": [None]}),
        ]
        for name, overrides in cases:
            with self.subTest(case=name):
                self.assert_refused(
                    self.run_payload(valid_payload(**overrides))
                )

    # -- Determinism and side-effect boundaries ------------------------------

    def test_json_only_stdout_for_every_outcome(self):
        mapped = self.run_payload(valid_payload())
        missing = self.run_payload(valid_payload(criteria=[]))
        refused = self.run_cli("{")
        for name, proc, expected_exit in (
            ("mapped", mapped, 0),
            ("missing", missing, 1),
            ("unmeasurable", refused, 2),
        ):
            with self.subTest(outcome=name):
                self.assertEqual(proc.returncode, expected_exit)
                report = self.report_from(proc)
                self.assertEqual(report["outcome"], name)
                stripped = proc.stdout.strip()
                self.assertTrue(stripped.startswith("{"))
                self.assertTrue(stripped.endswith("}"))

    def test_repeated_and_key_reordered_inputs_are_byte_identical(self):
        payload = valid_payload(
            labels=["security"], boundary_criteria=[REFUND_CRITERION]
        )
        first = self.run_cli(json.dumps(payload))
        second = self.run_cli(json.dumps(payload))
        reordered = self.run_cli(json.dumps(payload, sort_keys=True))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout, reordered.stdout)

        refused_first = self.run_cli("{")
        refused_second = self.run_cli("{")
        self.assert_refused(refused_first)
        self.assertEqual(refused_first.stdout, refused_second.stdout)

    def test_input_collection_order_does_not_change_report(self):
        files = ["b/token.py", "a/secret.py", "c/migrate.py", "a/secret.py"]
        labels = ["enhancement", "security"]
        one = valid_payload(
            changed_files=files, labels=labels,
            boundary_criteria=[REFUND_CRITERION],
        )
        two = valid_payload(
            changed_files=list(reversed(files)),
            labels=list(reversed(labels)),
            boundary_criteria=[REFUND_CRITERION],
        )
        proc_one = self.run_payload(one)
        proc_two = self.run_payload(two)
        self.assert_valid(proc_one, one, 0, "mapped")
        self.assertEqual(proc_one.stdout, proc_two.stdout)

    def test_runs_from_arbitrary_working_directory(self):
        payload = valid_payload()
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_payload(
                payload, cwd=Path(tmp), script=str(ABS_SCRIPT)
            )
            report = self.assert_valid(proc, payload, 0, "mapped")
            self.assert_obligations(report, payload)

    def test_cli_writes_no_files(self):
        watched_roots = [
            REPO_ROOT / "ops" / "review_tier1",
            REPO_ROOT / ".github" / "scripts",
        ]

        def snapshot():
            return {
                str(path.relative_to(REPO_ROOT))
                for root in watched_roots
                for path in root.rglob("*")
            }

        payload = valid_payload()
        with tempfile.TemporaryDirectory() as tmp:
            before = snapshot()
            proc = self.run_payload(
                payload, cwd=Path(tmp), script=str(ABS_SCRIPT)
            )
            self.assert_valid(proc, payload, 0, "mapped")
            self.assert_refused(
                self.run_cli("{", cwd=Path(tmp), script=str(ABS_SCRIPT))
            )
            self.assertEqual(
                [], [str(p) for p in Path(tmp).rglob("*")],
                "CLI must not write into its working directory",
            )
            self.assertEqual(before, snapshot())

    def test_cli_without_interpreter_guard_writes_no_files(self):
        with tempfile.TemporaryDirectory(prefix="proof-obligations-no-write-") as tmp:
            root = Path(tmp)
            for relative in (
                REL_SCRIPT,
                "ops/review_tier1/deterministic.py",
                ".github/scripts/review_trigger.py",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / relative, target)

            def snapshot():
                return sorted(str(p.relative_to(root)) for p in root.rglob("*"))

            before = snapshot()
            env = dict(os.environ)
            # Neither an inherited -B equivalent nor cache redirection may
            # hide bytecode writes outside this fresh, writable helper tree.
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            payload = valid_payload()
            proc = subprocess.run(
                [sys.executable, str(root / REL_SCRIPT)],
                input=json.dumps(payload),
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            report = self.assert_valid(proc, payload, 0, "mapped")
            self.assert_obligations(report, payload)
            self.assertEqual(
                before, snapshot(),
                "CLI must not create files without interpreter bytecode guards",
            )

    def test_supplied_strings_are_data_not_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "executed-sentinel"
            hostile_diff = POSITIVE_DIFF + (
                '+__import__("pathlib").Path(r"%s").touch()\n' % sentinel
            )
            payload = valid_payload(
                diff=hostile_diff,
                changed_files=[
                    "src/pricing.py",
                    "tests/test_pricing.py",
                    "src/$(touch %s).py" % sentinel,
                ],
                labels=["enhancement", "; touch %s" % sentinel],
            )
            proc = self.run_payload(
                payload, cwd=Path(tmp), script=str(ABS_SCRIPT)
            )
            report = self.assert_valid(proc, payload, 0, "mapped")
            self.assert_obligations(report, payload)
            self.assertFalse(
                sentinel.exists(), "supplied strings must never execute"
            )


if __name__ == "__main__":
    unittest.main()
