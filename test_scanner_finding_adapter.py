#!/usr/bin/env python3
"""Offline contract for the scanner finding adapter.

Hermetic: no network, no credentials, no board. Canary secrets are
assembled from fragments at runtime so this suite never trips the
repository secret scan it describes.
"""

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ops" / "paperclip"))

import scanner_finding_adapter as adapter
from scanner_finding_adapter import SCANNER_SCRIPT_BLOB, adapt

HEAD_A = "a" * 40
HEAD_B = "b" * 40
CANARY = "gh" + "p_" + "A" * 20
KEY_CANARY = "BEGIN " + "PRIVATE KEY"


def clean_outcome(**overrides):
    outcome = {
        "schema_version": 1,
        "repo_slug": "example/demo-repo",
        "source_head": HEAD_A,
        "scanner": {"name": "secret-scan", "script_blob": SCANNER_SCRIPT_BLOB},
        "scan": {
            "exit_code": 0,
            "spawn_ok": True,
            "complete": True,
            "shallow": False,
            "truncated": False,
            "stderr_nonempty": False,
            "occurrences": [],
        },
        "responder": "responder-1",
    }
    outcome.update(overrides)
    return outcome


def finding_outcome(occurrences=None, **overrides):
    outcome = clean_outcome(**overrides)
    outcome["scan"] = dict(outcome["scan"])
    outcome["scan"]["exit_code"] = 1
    outcome["scan"]["occurrences"] = occurrences or [
        {"rule_id": "github-token", "path": "src/app.py", "lines": [7]},
    ]
    return outcome


def raw_outcome(raw, exit_code=1, **overrides):
    outcome = clean_outcome(**overrides)
    outcome["scan"] = {
        "exit_code": exit_code,
        "spawn_ok": True,
        "complete": True,
        "shallow": False,
        "truncated": False,
        "stderr_nonempty": False,
        "raw_stdout": raw,
    }
    return outcome


def closure_request(**overrides):
    request = {
        "rule_id": "github-token",
        "path": "src/app.py",
        "existing_issue_ids": ["issue-1"],
        "fix_head": HEAD_A,
        "original_scanner_script_blob": SCANNER_SCRIPT_BLOB,
        "independent_acceptance_ref": "https://example.invalid/acceptance/1",
    }
    request.update(overrides)
    return request


class PinTests(unittest.TestCase):
    def test_scanner_blob_pin_matches_script(self):
        data = (ROOT / "verification" / "secret-scan.sh").read_bytes()
        self.assertIn(b"Exit 0 clean, 1 finding", data)
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        self.assertEqual(blob, SCANNER_SCRIPT_BLOB)


class CreatePayloadTests(unittest.TestCase):
    def test_clean_scan_emits_no_create_or_close(self):
        result = adapt(clean_outcome())
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["findings"], [])
        self.assertIsNone(result["closure"])
        self.assertNotIn("issue_create", json.dumps(result))

    def test_finding_emits_metadata_only_create(self):
        result = adapt(finding_outcome(parent_ref="parent-1", project_ref="project-1"))
        self.assertEqual(result["status"], "payload")
        finding = result["findings"][0]
        self.assertEqual(
            finding["identity"],
            {"repo_slug": "example/demo-repo", "rule_id": "github-token", "path": "src/app.py"},
        )
        self.assertEqual(finding["occurrence_count"], 1)
        self.assertEqual(finding["lines"], [7])
        create = finding["issue_create"]
        self.assertEqual(create["assigneeAgentId"], "responder-1")
        self.assertEqual(create["parentId"], "parent-1")
        self.assertEqual(create["projectId"], "project-1")
        self.assertIn(HEAD_A, create["description"])

    def test_same_inputs_give_identical_identity_and_key(self):
        first = adapt(finding_outcome())
        second = adapt(finding_outcome())
        self.assertEqual(first, second)
        # Revision evidence (head, lines) is not identity.
        revised = finding_outcome(
            [{"rule_id": "github-token", "path": "src/app.py", "lines": [42, 7]}],
            source_head=HEAD_B,
        )
        revised_finding = adapt(revised)["findings"][0]
        first_finding = first["findings"][0]
        self.assertEqual(revised_finding["identity_marker"], first_finding["identity_marker"])
        self.assertEqual(
            revised_finding["issue_create"]["idempotencyKey"],
            first_finding["issue_create"]["idempotencyKey"],
        )

    def test_multiple_lines_collapse_to_one_identity_with_count(self):
        result = adapt(finding_outcome([
            {"rule_id": "github-token", "path": "src/app.py", "lines": [3, 9]},
            {"rule_id": "github-token", "path": "src/app.py", "lines": [9, 27]},
            {"rule_id": "private-key", "path": "src/app.py", "lines": [3]},
        ]))
        identities = {(f["identity"]["rule_id"], f["occurrence_count"]) for f in result["findings"]}
        self.assertEqual(identities, {("github-token", 3), ("private-key", 1)})

    def test_lines_list_is_bounded_but_count_is_full(self):
        result = adapt(finding_outcome([
            {"rule_id": "github-token", "path": "src/app.py", "lines": list(range(1, 61))},
        ]))
        finding = result["findings"][0]
        self.assertEqual(finding["occurrence_count"], 60)
        self.assertEqual(len(finding["lines"]), adapter.MAX_LINES_LISTED)
        self.assertTrue(finding["lines_truncated"])


class UnknownScannerTests(unittest.TestCase):
    def assert_refusal(self, outcome, code):
        result = adapt(outcome)
        self.assertEqual(result["status"], "refusal", result)
        self.assertEqual(result["reason_code"], code, result)
        self.assertEqual(result["findings"], [])

    def test_scanner_errors_refuse(self):
        cases = []
        for field, value, code in [
            ("spawn_ok", False, "scanner_spawn_failed"),
            ("complete", False, "scan_incomplete"),
            ("shallow", True, "scan_shallow"),
            ("truncated", True, "scan_truncated"),
            ("stderr_nonempty", True, "scanner_stderr"),
            ("exit_code", 2, "scanner_exit_unknown"),
            ("exit_code", 128, "scanner_exit_unknown"),
        ]:
            outcome = finding_outcome()
            outcome["scan"][field] = value
            cases.append((outcome, code))
        wrong_blob = finding_outcome()
        wrong_blob["scanner"] = {"name": "secret-scan", "script_blob": "0" * 40}
        cases.append((wrong_blob, "scanner_pin_mismatch"))
        wrong_name = finding_outcome()
        wrong_name["scanner"] = {"name": "other", "script_blob": SCANNER_SCRIPT_BLOB}
        cases.append((wrong_name, "scanner_unknown"))
        for outcome, code in cases:
            with self.subTest(code=code):
                self.assert_refusal(outcome, code)

    def test_exit_inconsistency_refuses(self):
        clean_exit_with_finding = clean_outcome()
        clean_exit_with_finding["scan"]["occurrences"] = [
            {"rule_id": "github-token", "path": "src/app.py", "lines": [1]}]
        self.assert_refusal(clean_exit_with_finding, "scan_exit_inconsistent")
        finding_exit_clean = finding_outcome([])
        # finding_outcome replaces [] with a default; build explicitly.
        finding_exit_clean["scan"]["occurrences"] = []
        self.assert_refusal(finding_exit_clean, "scan_exit_inconsistent")

    def test_invalid_identity_and_unknown_fields_refuse_without_echo(self):
        traversal = finding_outcome([
            {"rule_id": "github-token", "path": "../outside.py", "lines": [1]}])
        self.assert_refusal(traversal, "identity_invalid")
        unknown_rule = finding_outcome([
            {"rule_id": "made-up", "path": "src/app.py", "lines": [1]}])
        self.assert_refusal(unknown_rule, "identity_invalid")
        smuggled = finding_outcome()
        smuggled["matched_content"] = CANARY
        result = adapt(smuggled)
        self.assertEqual(result["reason_code"], "unknown_field")
        self.assertNotIn(CANARY, json.dumps(result))


class RawParsingTests(unittest.TestCase):
    def test_raw_output_parses_metadata_and_never_echoes_canary(self):
        raw = (
            f"src/app.py:7:TOKEN = '{CANARY}'\n"
            "::error::a GitHub-token-shaped string is committed\n"
            f"keys/prod.pem:1:{KEY_CANARY}\n"
            "::error::private key material is committed\n"
            "::error::.gitignore no longer covers *.pem\n"
        )
        result = adapt(raw_outcome(raw))
        self.assertEqual(result["status"], "payload")
        by_rule = {f["identity"]["rule_id"]: f for f in result["findings"]}
        self.assertEqual(by_rule["github-token"]["lines"], [7])
        self.assertEqual(by_rule["private-key"]["identity"]["path"], "keys/prod.pem")
        self.assertEqual(by_rule["gitignore-coverage"]["occurrence_count"], 1)
        blob = json.dumps(result)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn(KEY_CANARY, blob)

    def test_raw_clean_line(self):
        result = adapt(raw_outcome("secret scan: clean\n", exit_code=0))
        self.assertEqual(result["status"], "none")

    def test_raw_unaccounted_output_refuses(self):
        for raw in [
            "src/app.py:7:unflushed\n",
            "src/app.py:7:x\n::error::some new scanner message\n",
            "total nonsense\n",
            "secret scan: clean\nsrc/app.py:1:x\n"
            "::error::private key material is committed\n",
        ]:
            with self.subTest(raw=raw.splitlines()[0]):
                result = adapt(raw_outcome(raw))
                self.assertEqual(result["status"], "refusal")


class ReplayTests(unittest.TestCase):
    def delivery(self, age_days, ids=(), **overrides):
        entry = {
            "rule_id": "github-token",
            "path": "src/app.py",
            "age_days": age_days,
            "existing_issue_ids": list(ids),
        }
        entry.update(overrides)
        return {"deliveries": [entry]}

    def test_in_window_without_binding_creates(self):
        result = adapt(finding_outcome(**self.delivery(3)))
        self.assertEqual(result["findings"][0]["action"], "create")

    def test_post_window_without_binding_refuses(self):
        result = adapt(finding_outcome(**self.delivery(30)))
        self.assertEqual(result["reason_code"], "post_window_unbound")
        boundary = adapt(finding_outcome(**self.delivery(7)))
        self.assertEqual(boundary["reason_code"], "post_window_unbound")

    def test_post_window_with_one_binding_updates(self):
        result = adapt(finding_outcome(**self.delivery(30, ["issue-9"])))
        finding = result["findings"][0]
        self.assertEqual(finding["action"], "update")
        self.assertEqual(finding["issue_update"]["issueId"], "issue-9")
        self.assertNotIn("issue_create", finding)

    def test_ambiguous_and_unknown_age_refuse(self):
        self.assertEqual(
            adapt(finding_outcome(**self.delivery(1, ["a", "b"])))["reason_code"],
            "ambiguous_binding",
        )
        self.assertEqual(
            adapt(finding_outcome(**self.delivery(None)))["reason_code"],
            "unknown_delivery_age",
        )

    def test_cross_repo_and_unmatched_delivery_refuse(self):
        cross = self.delivery(1, repo_slug="example/other")
        self.assertEqual(adapt(finding_outcome(**cross))["reason_code"], "delivery_invalid")
        unmatched = self.delivery(1, path="src/other.py")
        self.assertEqual(
            adapt(finding_outcome(**unmatched))["reason_code"], "unmatched_delivery")

    def test_adverse_after_pass_and_no_auto_close(self):
        self.assertEqual(adapt(clean_outcome())["status"], "none")
        self.assertEqual(adapt(finding_outcome())["status"], "payload")
        after = adapt(clean_outcome())
        self.assertEqual(after["status"], "none")
        self.assertNotIn('"status": "done"', json.dumps(after))


class ClosureTests(unittest.TestCase):
    def test_exact_fix_head_zero_occurrences_emits_closure(self):
        result = adapt(clean_outcome(closure_request=closure_request()))
        self.assertEqual(result["status"], "payload")
        update = result["closure"]["issue_update"]
        self.assertEqual(update["issueId"], "issue-1")
        self.assertEqual(update["status"], "done")
        self.assertIn(HEAD_A, update["comment"])

    def test_closure_refusals(self):
        cases = [
            (clean_outcome(closure_request=closure_request(fix_head=HEAD_B)),
             "closure_head_mismatch"),
            (clean_outcome(closure_request=closure_request(
                original_scanner_script_blob="0" * 40)), "closure_scanner_version"),
            (clean_outcome(closure_request=closure_request(
                independent_acceptance_ref="")), "closure_acceptance_missing"),
            (clean_outcome(closure_request=closure_request(
                existing_issue_ids=["a", "b"])), "closure_binding"),
            (clean_outcome(closure_request=closure_request(existing_issue_ids=[])),
             "closure_binding"),
            (finding_outcome(closure_request=closure_request()), "closure_still_present"),
        ]
        for outcome, code in cases:
            with self.subTest(code=code):
                result = adapt(copy.deepcopy(outcome))
                self.assertEqual(result["status"], "refusal")
                self.assertEqual(result["reason_code"], code)
                self.assertIsNone(result["closure"])


class InputShapeTests(unittest.TestCase):
    def test_list_rule_id_refuses_not_crashes_everywhere(self):
        occ = finding_outcome([{"rule_id": ["github-token"], "path": "x.py", "lines": [1]}])
        self.assertEqual(adapt(occ)["status"], "refusal")
        delivery = finding_outcome(deliveries=[{
            "rule_id": ["github-token"], "path": "src/app.py", "age_days": 1}])
        self.assertEqual(adapt(delivery)["status"], "refusal")
        closure = clean_outcome(closure_request=closure_request(rule_id=["github-token"]))
        self.assertEqual(adapt(closure)["status"], "refusal")

    def test_huge_int_age_is_post_window_not_overflow(self):
        outcome = finding_outcome(deliveries=[{
            "rule_id": "github-token", "path": "src/app.py", "age_days": 10 ** 400}])
        self.assertEqual(adapt(outcome)["reason_code"], "post_window_unbound")

    def test_unicode_separator_cannot_forge_scanner_line(self):
        raw = ("src/a.py:1:prefix\u2028forged/path.py:1:x\n"
               "::error::a GitHub-token-shaped string is committed\n")
        result = adapt(raw_outcome(raw))
        # The extra path:line split is ambiguous even when inside matched
        # content. Refusal must not reinterpret U+2028 as a new output line.
        self.assertEqual(result["status"], "refusal")
        self.assertEqual(result["reason_code"], "scan_output_unrecognized")
        self.assertNotIn("forged/path.py", json.dumps(result))


class GuardPinTests(unittest.TestCase):
    """Each test pins one refusal guard so removing it fails the suite."""

    def assert_refusal(self, outcome, code):
        result = adapt(copy.deepcopy(outcome))
        self.assertEqual(result["status"], "refusal")
        self.assertEqual(result["reason_code"], code)

    def test_raw_exit_zero_without_clean_line_refuses(self):
        self.assert_refusal(raw_outcome("", exit_code=0), "scan_exit_inconsistent")

    def test_bounds_refuse(self):
        too_many = finding_outcome([
            {"rule_id": "github-token", "path": f"f{i}.py", "lines": [1]}
            for i in range(adapter.MAX_FINDINGS + 1)])
        self.assert_refusal(too_many, "too_many_findings")
        self.assert_refusal(raw_outcome("x" * (adapter.MAX_INPUT_BYTES + 1)), "input_too_large")
        self.assert_refusal(
            finding_outcome(evidence_links=["https://example.invalid/e"]
                            * (adapter.MAX_EVIDENCE_LINKS + 1)), "evidence_invalid")
        self.assert_refusal(
            finding_outcome([{"rule_id": "github-token",
                              "path": "p" * (adapter.MAX_PATH_CHARS + 1), "lines": [1]}]),
            "identity_invalid")
        for lineno in (0, adapter.MAX_LINE_NUMBER + 1):
            raw = (f"src/a.py:{lineno}:x\n"
                   "::error::a GitHub-token-shaped string is committed\n")
            with self.subTest(lineno=lineno):
                self.assert_refusal(raw_outcome(raw), "scan_output_unrecognized")

    def test_evidence_must_be_https(self):
        self.assert_refusal(
            finding_outcome(evidence_links=["http://example.invalid/e"]), "evidence_invalid")

    def test_bare_https_scheme_is_not_a_reference(self):
        self.assert_refusal(finding_outcome(evidence_links=["https://"]), "evidence_invalid")
        self.assert_refusal(
            clean_outcome(closure_request=closure_request(independent_acceptance_ref="https://")),
            "closure_acceptance_missing")

    def test_strict_json_rejects_duplicate_keys_and_nonfinite(self):
        with self.assertRaises(ValueError):
            adapter._strict_loads('{"scan": 1, "scan": 2}')
        with self.assertRaises(ValueError):
            adapter._strict_loads('{"age_days": NaN}')
        with self.assertRaises(ValueError):
            adapter._strict_loads('{"age_days": Infinity}')

    def test_replay_guards(self):
        entry = {"rule_id": "github-token", "path": "src/app.py", "age_days": 30,
                 "existing_issue_ids": ["issue-1"]}
        second = dict(entry, age_days=1, existing_issue_ids=[])
        self.assert_refusal(finding_outcome(deliveries=[entry, second]), "ambiguous_delivery")
        self.assert_refusal(
            finding_outcome(deliveries=[dict(entry, age_days=-30, existing_issue_ids=[])]),
            "delivery_invalid")
        self.assert_refusal(finding_outcome(responder=None), "responder_missing")

    def test_closure_cross_repo_and_colon_path_refuse(self):
        self.assert_refusal(
            clean_outcome(closure_request=closure_request(repo_slug="other/repo")),
            "identity_invalid")
        self.assert_refusal(
            clean_outcome(closure_request=closure_request(path="a:5:b.txt")),
            "closure_path_ambiguous")

    def test_colon_path_cannot_yield_false_closure(self):
        # git grep -n prints "a:5:b.txt:2:<content>" for a file named
        # "a:5:b.txt"; refuse at parse time before the wrong path can
        # create a finding or a closure can report zero occurrences.
        raw = (f"a:5:b.txt:2:x = '{CANARY}'\n"
               "::error::a GitHub-token-shaped string is committed\n")
        self.assert_refusal(
            raw_outcome(raw, closure_request=closure_request(path="a:5:b.txt")),
            "scan_output_unrecognized")


class BoundaryRegressionTests(unittest.TestCase):
    def assert_refusal(self, outcome, code):
        result = adapt(outcome)
        self.assertEqual(result["status"], "refusal")
        self.assertEqual(result["reason_code"], code)
        self.assertEqual(result["findings"], [])
        self.assertIsNone(result["closure"])
        self.assertNotIn(CANARY, json.dumps(result))

    def test_ambiguous_raw_path_refuses_create_and_replay(self):
        annotation = "::error::a GitHub-token-shaped string is committed\n"
        for prefix in ("", f"a:9:{CANARY}\n"):
            for path in ("a:5:b.txt:2", "a:5:2"):
                raw = f"{prefix}{path}:{CANARY}\n{annotation}"
                with self.subTest(prefix=bool(prefix), path=path):
                    self.assert_refusal(raw_outcome(raw), "scan_output_unrecognized")
                    self.assert_refusal(raw_outcome(raw, deliveries=[{
                        "rule_id": "github-token", "path": "a:5:b.txt", "age_days": 8,
                        "existing_issue_ids": ["issue-1"]}]), "scan_output_unrecognized")

    def test_huge_raw_line_number_refuses_without_conversion_crash(self):
        raw = ("src/a.py:" + "9" * 5000 + f":{CANARY}\n"
               "::error::a GitHub-token-shaped string is committed\n")
        self.assert_refusal(raw_outcome(raw), "scan_output_unrecognized")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ops" / "paperclip" / "scanner_finding_adapter.py")],
            input=json.dumps(raw_outcome(raw)).encode(), capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(json.loads(proc.stdout)["reason_code"], "scan_output_unrecognized")
        self.assertNotIn(CANARY.encode(), proc.stdout)

    def test_structured_line_range_refuses(self):
        for line in (0, -1, adapter.MAX_LINE_NUMBER + 1, True, "1", 1.0):
            with self.subTest(line=line):
                self.assert_refusal(finding_outcome([
                    {"rule_id": "github-token", "path": "src/app.py", "lines": [line]}]),
                    "invalid_input")

    def test_library_nonfinite_delivery_age_refuses(self):
        for age in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(age=age):
                self.assert_refusal(finding_outcome(deliveries=[{
                    "rule_id": "github-token", "path": "src/app.py", "age_days": age}]),
                    "delivery_invalid")

    def test_at_limit_findings_evidence_and_path_succeed(self):
        links = [f"https://example.invalid/e/{i}" for i in range(adapter.MAX_EVIDENCE_LINKS)]
        occurrences = [{"rule_id": "github-token", "path": f"f{i}.py", "lines": [1]}
                       for i in range(adapter.MAX_FINDINGS)]
        occurrences[0]["path"] = "p" * adapter.MAX_PATH_CHARS
        result = adapt(finding_outcome(occurrences, evidence_links=links))
        self.assertEqual(result["status"], "payload")
        self.assertEqual(len(result["findings"]), adapter.MAX_FINDINGS)
        self.assertIn("p" * adapter.MAX_PATH_CHARS,
                      [f["identity"]["path"] for f in result["findings"]])
        self.assertTrue(all(f["evidence_links"] == links for f in result["findings"]))

    def test_at_limit_line_number_succeeds_in_both_modes(self):
        for line in (1, adapter.MAX_LINE_NUMBER):
            raw = (f"src/app.py:{line}:{CANARY}\n"
                   "::error::a GitHub-token-shaped string is committed\n")
            for outcome in (raw_outcome(raw), finding_outcome([
                    {"rule_id": "github-token", "path": "src/app.py", "lines": [line]}])):
                with self.subTest(line=line, raw="raw_stdout" in outcome["scan"]):
                    result = adapt(outcome)
                    self.assertEqual(result["status"], "payload")
                    self.assertEqual(result["findings"][0]["lines"], [line])

    def test_at_limit_raw_and_cli_input_succeed(self):
        prefix = "src/app.py:1:"
        suffix = "\n::error::a GitHub-token-shaped string is committed\n"
        raw = prefix + "x" * (adapter.MAX_INPUT_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(raw.encode()), adapter.MAX_INPUT_BYTES)
        self.assertEqual(adapt(raw_outcome(raw))["status"], "payload")
        text = json.dumps(finding_outcome())
        text += " " * (adapter.MAX_INPUT_BYTES - len(text.encode()))
        for extra, code in (("", 0), (" ", 2)):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "ops" / "paperclip" / "scanner_finding_adapter.py")],
                input=(text + extra).encode(), capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, code)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "payload" if code == 0 else "refusal")
            if code:
                self.assertEqual(result["reason_code"], "input_too_large")

    def test_valid_https_hosts_succeed_in_evidence_and_acceptance(self):
        for url in ("https://example.invalid/e", "https://host:443/e", "https://[::1]/e",
                    "https://example.invalid./e", "https://xn--bcher-kva.invalid/e"):
            with self.subTest(url=url):
                finding = adapt(finding_outcome(evidence_links=[url]))
                self.assertEqual(finding["status"], "payload")
                self.assertEqual(finding["findings"][0]["evidence_links"], [url])
                closure = adapt(clean_outcome(closure_request=closure_request(
                    independent_acceptance_ref=url)))
                self.assertEqual(closure["closure"]["issue_update"]["status"], "done")

    def test_invalid_https_hosts_refuse_evidence_and_acceptance(self):
        for url in ("https://:", "https://:443/", "https://host:bad/", "https://[broken/",
                    "https://a..b/", "https://-host/"):
            with self.subTest(url=url):
                self.assert_refusal(finding_outcome(evidence_links=[url]), "evidence_invalid")
                self.assert_refusal(clean_outcome(closure_request=closure_request(
                    independent_acceptance_ref=url)), "closure_acceptance_missing")


class CliTests(unittest.TestCase):
    def run_cli(self, payload_text):
        return subprocess.run(
            [sys.executable, str(ROOT / "ops" / "paperclip" / "scanner_finding_adapter.py")],
            input=payload_text.encode(),
            capture_output=True,
            timeout=30,
        )

    def test_cli_payload_exit_zero_and_canary_absent(self):
        raw = f"src/app.py:7:'{CANARY}'\n::error::a GitHub-token-shaped string is committed\n"
        proc = self.run_cli(json.dumps(raw_outcome(raw)))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertNotIn(CANARY, proc.stdout.decode())
        self.assertEqual(json.loads(proc.stdout)["status"], "payload")

    def test_cli_refusal_exit_nonzero(self):
        proc = self.run_cli(json.dumps(raw_outcome("nonsense\n")))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "refusal")

    def test_cli_invalid_json_refuses_without_echo(self):
        proc = self.run_cli("{broken " + CANARY)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(CANARY, proc.stdout.decode())

    def test_cli_duplicate_keys_refuse(self):
        text = json.dumps(finding_outcome()).replace(
            '"scan":', '"scan": {"exit_code": 0}, "scan":', 1)
        proc = self.run_cli(text)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "refusal")

    def test_cli_deeply_nested_input_refuses_not_crashes(self):
        proc = self.run_cli("[" * 200000)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "refusal")


if __name__ == "__main__":
    unittest.main()
