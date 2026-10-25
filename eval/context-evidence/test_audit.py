"""Synthetic acceptance tests for the public context-evidence replay contract.

Run with: python3 -B -m unittest discover -s eval/context-evidence -p test_audit.py -v
No provider, network, credentials or live run receipts are used.
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


AUDIT_PATH = Path(__file__).with_name("audit.py")
SPEC = importlib.util.spec_from_file_location("context_evidence_audit", AUDIT_PATH)
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def replay_bundle():
    """Hand-sized evidence with independently calculable request occupancy."""
    return {
        "schema": "context-evidence.v1",
        "run": {
            "usageJson": {"model": "synthetic-served-model", "inputTokens": 9000000},
            "resultJson": {
                "modelUsage": {"synthetic-served-model": {"contextWindow": 1000}}
            },
        },
        "rawEvidence": [
            {
                "id": "decision-r1",
                "body": "Record α. Keep the gate closed. Error SYNTHETIC-409.",
            },
            {
                "id": "handoff-r7",
                "body": "Original handoff: continue from revision sample-r7.",
            },
        ],
        "anchors": [
            {"id": "stop", "sourceId": "decision-r1", "text": "Keep the gate closed."},
            {"id": "error", "sourceId": "decision-r1", "text": "SYNTHETIC-409"},
            {
                "id": "next",
                "sourceId": "handoff-r7",
                "text": "continue from revision sample-r7",
            },
        ],
        "statePacket": (
            "STOP: Keep the gate closed.\n"
            "Observed error: SYNTHETIC-409\n"
            "Next: continue from revision sample-r7"
        ),
        "request": {
            "model": "synthetic-served-model",
            "inputTokens": 700,
            "outputReserveTokens": 100,
            "source": "synthetic single-request receipt",
        },
    }


class AuditContractTests(unittest.TestCase):
    def run_cli_bytes(self, payload):
        with tempfile.TemporaryDirectory(prefix="context-evidence-test-") as directory:
            path = Path(directory) / "bundle.json"
            path.write_bytes(payload)
            result = subprocess.run(
                [sys.executable, "-B", str(AUDIT_PATH.resolve()), str(path)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.stderr, "", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def assert_verdict(self, bundle, status, *, capacity=None, fidelity=None):
        """Exercise the API and real CLI, not an exit-code mock."""
        api_report = audit_module.audit(bundle)
        code, cli_report = self.run_cli_bytes(json.dumps(bundle).encode("utf-8"))
        self.assertEqual(code, {"pass": 0, "fail": 1, "unknown": 2}[status])
        self.assertEqual(api_report, cli_report)
        self.assertEqual(api_report["schema"], "context-evidence-report.v1")
        self.assertEqual(api_report["status"], status)
        if capacity is not None:
            self.assertEqual(api_report["capacity"]["status"], capacity)
        if fidelity is not None:
            self.assertEqual(api_report["fidelity"]["status"], fidelity)
        return api_report

    def assert_invalid_cli(self, payload):
        code, report = self.run_cli_bytes(payload)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "unknown")

    def test_retained_anchors_and_measured_request_pass(self):
        report = self.assert_verdict(
            replay_bundle(), "pass", capacity="pass", fidelity="pass"
        )
        self.assertEqual(report["fidelity"]["retained"], 3)
        self.assertEqual(report["fidelity"]["total"], 3)
        self.assertEqual(report["fidelity"]["missingAnchorIds"], [])
        self.assertEqual(report["capacity"]["contextWindow"], 1000)
        self.assertEqual(report["capacity"]["requiredTokens"], 800)
        self.assertEqual(report["capacity"]["headroomTokens"], 200)
        self.assertEqual(report["capacity"]["model"], "synthetic-served-model")

    def test_selected_anchor_loss_fails(self):
        bundle = replay_bundle()
        bundle["statePacket"] = bundle["statePacket"].replace("SYNTHETIC-409", "redacted")
        report = self.assert_verdict(bundle, "fail", capacity="pass", fidelity="fail")
        self.assertEqual(report["fidelity"]["missingAnchorIds"], ["error"])
        self.assertEqual(report["fidelity"]["retained"], 2)
        self.assertEqual(report["fidelity"]["total"], 3)

    def test_selected_anchor_loss_overrides_unknown_capacity(self):
        bundle = replay_bundle()
        del bundle["request"]
        bundle["statePacket"] = bundle["statePacket"].replace("Keep the gate closed.", "omitted")
        report = self.assert_verdict(bundle, "fail", capacity="unknown", fidelity="fail")
        self.assertEqual(report["fidelity"]["missingAnchorIds"], ["stop"])

    def test_missing_request_is_unknown_not_cumulative_occupancy(self):
        bundle = replay_bundle()
        del bundle["request"]
        self.assert_verdict(bundle, "unknown", capacity="unknown", fidelity="pass")

    def test_one_token_overflow_fails(self):
        bundle = replay_bundle()
        bundle["request"]["inputTokens"] = 901
        report = self.assert_verdict(bundle, "fail", capacity="fail", fidelity="pass")
        self.assertEqual(report["capacity"]["requiredTokens"], 1001)
        self.assertEqual(report["capacity"]["headroomTokens"], -1)

    def test_required_equal_to_window_passes(self):
        bundle = replay_bundle()
        bundle["request"]["inputTokens"] = 900
        report = self.assert_verdict(bundle, "pass", capacity="pass", fidelity="pass")
        self.assertEqual(report["capacity"]["requiredTokens"], 1000)
        self.assertEqual(report["capacity"]["headroomTokens"], 0)

    def test_zero_request_input_is_a_valid_measurement(self):
        bundle = replay_bundle()
        bundle["request"]["inputTokens"] = 0
        report = self.assert_verdict(bundle, "pass", capacity="pass")
        self.assertEqual(report["capacity"]["requiredTokens"], 100)
        self.assertEqual(report["capacity"]["headroomTokens"], 900)

    def test_cumulative_run_and_cache_totals_do_not_enter_occupancy(self):
        bundle = replay_bundle()
        bundle["run"]["usageJson"].update(
            inputTokens=9000000000,
            cachedInputTokens=8000000000,
            cacheReadInputTokens=7000000000,
            cacheCreationInputTokens=6000000000,
            outputTokens=5000000000,
        )
        bundle["run"]["resultJson"]["modelUsage"]["synthetic-served-model"].update(
            inputTokens=4000000000, outputTokens=3000000000, cacheReadInputTokens=2000000000
        )
        report = self.assert_verdict(bundle, "pass", capacity="pass")
        self.assertEqual(report["capacity"]["requiredTokens"], 800)
        self.assertEqual(report["capacity"]["headroomTokens"], 200)

    def test_empty_anchor_selection_is_unknown(self):
        bundle = replay_bundle()
        bundle["anchors"] = []
        self.assert_verdict(bundle, "unknown")

    def test_duplicate_anchor_ids_are_unknown(self):
        bundle = replay_bundle()
        bundle["anchors"][1]["id"] = "stop"
        self.assert_verdict(bundle, "unknown")

    def test_duplicate_anchor_selection_with_distinct_ids_is_unknown(self):
        bundle = replay_bundle()
        duplicate = copy.deepcopy(bundle["anchors"][0])
        duplicate["id"] = "same-selection-new-id"
        bundle["anchors"].append(duplicate)
        self.assert_verdict(bundle, "unknown")

    def test_empty_or_whitespace_anchor_text_is_unknown(self):
        for text in ("", " \t\n"):
            with self.subTest(text=text):
                bundle = replay_bundle()
                bundle["anchors"][0]["text"] = text
                self.assert_verdict(bundle, "unknown")

    def test_anchor_in_packet_but_not_in_raw_source_is_unknown(self):
        bundle = replay_bundle()
        bundle["anchors"][0]["text"] = "Invented instruction, not source evidence."
        bundle["statePacket"] += "\nInvented instruction, not source evidence."
        self.assert_verdict(bundle, "unknown")

    def test_anchor_in_wrong_raw_source_is_unknown(self):
        bundle = replay_bundle()
        bundle["anchors"][0]["sourceId"] = "handoff-r7"
        self.assert_verdict(bundle, "unknown")

    def test_anchor_with_missing_source_is_unknown(self):
        bundle = replay_bundle()
        bundle["anchors"][0]["sourceId"] = "absent-source"
        self.assert_verdict(bundle, "unknown")

    def test_uncovered_raw_source_is_unknown(self):
        bundle = replay_bundle()
        bundle["rawEvidence"].append({"id": "unselected-r2", "body": "Unselected evidence."})
        self.assert_verdict(bundle, "unknown")

    def test_duplicate_raw_source_ids_are_unknown(self):
        bundle = replay_bundle()
        bundle["rawEvidence"].append(copy.deepcopy(bundle["rawEvidence"][0]))
        self.assert_verdict(bundle, "unknown")

    def test_empty_raw_evidence_is_unknown(self):
        bundle = replay_bundle()
        bundle["rawEvidence"] = []
        self.assert_verdict(bundle, "unknown")

    def test_empty_raw_body_is_unknown(self):
        bundle = replay_bundle()
        bundle["rawEvidence"][0]["body"] = ""
        self.assert_verdict(bundle, "unknown")

    def test_missing_served_model_identity_is_unknown(self):
        bundle = replay_bundle()
        del bundle["run"]["usageJson"]["model"]
        self.assert_verdict(bundle, "unknown", capacity="unknown", fidelity="pass")

    def test_missing_model_usage_metadata_is_unknown(self):
        bundle = replay_bundle()
        del bundle["run"]["resultJson"]["modelUsage"]
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_empty_model_usage_metadata_is_unknown(self):
        bundle = replay_bundle()
        bundle["run"]["resultJson"]["modelUsage"] = {}
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_multiple_model_rows_are_unknown_even_when_windows_match(self):
        bundle = replay_bundle()
        bundle["run"]["resultJson"]["modelUsage"]["another-model"] = {"contextWindow": 1000}
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_run_identity_must_match_model_usage_key(self):
        bundle = replay_bundle()
        bundle["run"]["usageJson"]["model"] = "another-model"
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_request_identity_must_match_served_model(self):
        bundle = replay_bundle()
        bundle["request"]["model"] = "another-model"
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_missing_request_model_is_unknown(self):
        bundle = replay_bundle()
        del bundle["request"]["model"]
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_configured_model_cannot_substitute_for_served_metadata(self):
        bundle = replay_bundle()
        bundle["run"]["configuredModel"] = "synthetic-served-model"
        bundle["run"]["contextWindow"] = 1000
        del bundle["run"]["resultJson"]
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_invalid_or_missing_context_windows_are_unknown(self):
        for window in (None, True, False, 0, -1, 1000.0, "1000"):
            with self.subTest(window=window):
                bundle = replay_bundle()
                row = bundle["run"]["resultJson"]["modelUsage"]["synthetic-served-model"]
                row["contextWindow"] = window
                self.assert_verdict(bundle, "unknown", capacity="unknown")
        bundle = replay_bundle()
        del bundle["run"]["resultJson"]["modelUsage"]["synthetic-served-model"]["contextWindow"]
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_bool_and_invalid_request_counts_are_unknown(self):
        for field in ("inputTokens", "outputReserveTokens"):
            for count in (None, True, False, -1, 1.5, "700"):
                with self.subTest(field=field, count=count):
                    bundle = replay_bundle()
                    bundle["request"][field] = count
                    self.assert_verdict(bundle, "unknown", capacity="unknown")
            with self.subTest(field=field, count="missing"):
                bundle = replay_bundle()
                del bundle["request"][field]
                self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_zero_output_reserve_is_unknown(self):
        bundle = replay_bundle()
        bundle["request"]["outputReserveTokens"] = 0
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_missing_or_empty_measurement_provenance_is_unknown(self):
        for source in (None, "", " \t"):
            with self.subTest(source=source):
                bundle = replay_bundle()
                bundle["request"]["source"] = source
                self.assert_verdict(bundle, "unknown", capacity="unknown")
        bundle = replay_bundle()
        del bundle["request"]["source"]
        self.assert_verdict(bundle, "unknown", capacity="unknown")

    def test_api_does_not_mutate_input_on_pass_fail_or_unknown(self):
        for status in ("pass", "fail", "unknown"):
            with self.subTest(status=status):
                bundle = replay_bundle()
                if status == "fail":
                    bundle["statePacket"] = "Only SYNTHETIC-409 remains."
                elif status == "unknown":
                    del bundle["request"]
                original = copy.deepcopy(bundle)
                report = audit_module.audit(bundle)
                self.assertEqual(report["status"], status)
                self.assertEqual(bundle, original)

    def test_cli_does_not_rewrite_input_file(self):
        payload = json.dumps(replay_bundle(), indent=2, ensure_ascii=False).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="context-evidence-test-") as directory:
            path = Path(directory) / "bundle.json"
            path.write_bytes(payload)
            result = subprocess.run(
                [sys.executable, "-B", str(AUDIT_PATH.resolve()), str(path)],
                cwd=directory,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["bundle.json"])

    def test_report_hashes_utf8_raw_sources_but_omits_their_content(self):
        for status in ("pass", "fail", "unknown"):
            with self.subTest(status=status):
                bundle = replay_bundle()
                if status == "fail":
                    bundle["statePacket"] = "Unrelated continuation without selected evidence."
                elif status == "unknown":
                    del bundle["request"]
                report = self.assert_verdict(bundle, status)
                serialized = json.dumps(report, ensure_ascii=False)
                self.assertEqual(
                    report["fidelity"]["rawEvidenceHashes"],
                    {
                        source["id"]: hashlib.sha256(source["body"].encode("utf-8")).hexdigest()
                        for source in bundle["rawEvidence"]
                    },
                )
                for source in bundle["rawEvidence"]:
                    self.assertNotIn(source["body"], serialized)
                for anchor in bundle["anchors"]:
                    self.assertNotIn(anchor["text"], serialized)
                self.assertNotIn(bundle["statePacket"], serialized)
                self.assertNotIn('"rawEvidence"', serialized)
                self.assertNotIn('"anchors"', serialized)
                self.assertNotIn('"statePacket"', serialized)

    def test_duplicate_top_level_json_keys_are_unknown(self):
        # Plain json.loads would silently keep the last value and pass this bundle.
        payload = json.dumps(replay_bundle()).encode("utf-8")
        self.assert_invalid_cli(b'{"schema": "discarded", ' + payload[1:])

    def test_duplicate_nested_json_keys_are_unknown(self):
        payload = json.dumps(replay_bundle()).replace(
            '"inputTokens": 700', '"inputTokens": 1, "inputTokens": 700'
        )
        self.assert_invalid_cli(payload.encode("utf-8"))

    def test_nonfinite_numbers_in_unused_fields_are_unknown(self):
        # Unused fields ensure downstream count validation cannot hide lost JSON hooks.
        for number in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(number=number):
                payload = json.dumps(replay_bundle())
                self.assert_invalid_cli((payload[:-1] + ', "unused": ' + number + '}').encode())

    def test_input_at_eight_mib_is_accepted(self):
        payload = json.dumps(replay_bundle()).encode("utf-8")
        payload += b" " * (8 * 1024 * 1024 - len(payload))
        code, report = self.run_cli_bytes(payload)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "pass")

    def test_input_one_byte_over_eight_mib_is_unknown(self):
        # Valid JSON plus whitespace must not evade the byte cap.
        payload = json.dumps(replay_bundle()).encode("utf-8")
        payload += b" " * (8 * 1024 * 1024 + 1 - len(payload))
        self.assert_invalid_cli(payload)

    def test_malformed_json_is_unknown(self):
        for payload in (b"", b"{", b'{"schema":', b'{} trailing', b'{"schema":"x",}'):
            with self.subTest(payload=payload):
                self.assert_invalid_cli(payload)

    def test_non_object_or_wrong_schema_input_is_unknown(self):
        for bundle in (None, [], "text", 3, {}, {"schema": "unsupported.v9"}):
            with self.subTest(bundle=bundle):
                self.assert_verdict(bundle, "unknown")

    def test_invalid_utf8_is_unknown_even_in_an_unused_field(self):
        payload = json.dumps(replay_bundle()).encode("utf-8")
        self.assert_invalid_cli(payload[:-1] + b', "unused": "\xff"}')

    def test_nonexistent_file_is_unknown(self):
        with tempfile.TemporaryDirectory(prefix="context-evidence-test-") as directory:
            path = Path(directory) / "missing.json"
            result = subprocess.run(
                [sys.executable, "-B", str(AUDIT_PATH.resolve()), str(path)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "unknown")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
