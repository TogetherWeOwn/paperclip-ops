#!/usr/bin/env python3
"""Immutable builder acceptance contract for migration evidence AX (TOG-35161).

IMMUTABLE: builder starts from this pushed branch/commit and MUST NOT edit
this file. Corrections return to the Test Author card. No standalone
test-only PR: this branch is handed to the paired source PR.

Contract under test (migration_drift_gate.py):
- Opt-in --json emits ONE versioned object on stdout. Stdout stays
  json.loads-parseable for every supported success and input-error path.
  No traceback and no raw evidence content in diagnostics.
- Top-level shape: version==1, verdict in {pass,fail,refused},
  pending_count/unknown_count ints, checks list, next_action str.
  verdict maps to exits pass->0, fail->1, refused->2.
- checks[] entries: {id, status, reason} with stable IDs from the
  allowlists below. status in {pass,fail,refused}.
- next_action is actionable (non-empty) whenever verdict != pass.
- Evidence shape validation (root/version, migrations collection/rows,
  required field types, duplicate tags) runs BEFORE evaluation. Invalid
  shapes fail closed with input-error semantics (exit 2, verdict refused
  in JSON mode) instead of crashing.
- Default text output and existing exit meanings are preserved when
  --json is absent. Previously refused evidence never passes.
- Evaluation is deterministic, files-only, idempotent, non-mutating.
- Docs: --help plus docs/migration-standard.md document the JSON
  contract, code-to-next_action examples, one focused offline
  verification command, and why dry-run is inherent for this read-only
  gate. One canonical schema source (JSON_SCHEMA_VERSION), no duplicated
  host policy.

Synthetic fixtures only. Fully offline: temp dirs, no DB, no network,
no credentials, deterministic bounds.

Runnable command (from repo root):
  python3 -B -m unittest -v test_migration_evidence_ax test_migration_drift_gate

Red baseline (current source has no --json, no shape validation, no
docs): JSON/shape/docs tests FAIL meaningfully here. Compatibility and
hygiene controls PASS. Do not weaken asserts to make red pass.
"""
import hashlib
import io
import json
import re
import socket
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

import migration_drift_gate as gate

HERE = Path(__file__).resolve().parent
DOC_PATH = HERE / "docs" / "migration-standard.md"
SRC_PATH = HERE / "migration_drift_gate.py"

EXPECTED_JSON_VERSION = 1
VERDICTS = ("pass", "fail", "refused")
STATUSES = ("pass", "fail", "refused")

ALLOWED_CHECK_IDS = frozenset({
    "candidate_chain",
    "ledger_unknown",
    "evidence_schema",
    "evidence_status",
    "evidence_cleanup",
    "evidence_target",
    "evidence_freshness",
    "evidence_binding",
    "evidence_order",
    "input_files",
})

ALLOWED_REASON_IDS = frozenset({
    "ok",
    "pending_without_evidence",
    "unknown_ledger_rows",
    "evidence_invalid_json",
    "evidence_invalid_root",
    "evidence_invalid_version",
    "evidence_invalid_migrations",
    "evidence_invalid_row",
    "evidence_duplicate_tag",
    "evidence_invalid_field",
    "evidence_status_not_passed",
    "evidence_cleanup_unconfirmed",
    "evidence_target_mismatch",
    "evidence_unparseable_time",
    "evidence_future",
    "evidence_stale",
    "evidence_not_applied",
    "evidence_unbound",
    "evidence_order_mismatch",
    "input_empty_candidate",
    "input_malformed_hashes",
    "input_empty_ledger",
    "input_malformed_ledger",
    "input_invalid_max_age",
})

CANARY = "CANARY-AX-NEVER-ECHO-9f3c2a1e77"

VERIFICATION_COMMAND_FRAGMENTS = (
    "python3 -B -m unittest",
    "test_migration_evidence_ax",
    "test_migration_drift_gate",
)


def sha(b):
    return hashlib.sha256(b).hexdigest()


A = sha(b"-- phase: expand\nselect 1;\n")
B = sha(b"-- phase: expand\nselect 2;\n")
C = sha(b"-- phase: contract\nselect 3;\n")


class AxHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="evidence-ax-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    # -- fixture helpers (synthetic only) -------------------------------
    def write(self, name, text):
        p = self.root / name
        p.write_text(text)
        return str(p)

    def hashes(self, pairs):
        return self.write("candidate.txt",
                          "".join(f"{h}  {t}.sql\n" for t, h in pairs))

    def ledger(self, hashes):
        return self.write("ledger.txt", "".join(h + "\n" for h in hashes))

    def evidence_doc(self, status="passed", age_hours=1,
                     migrations=(("0042_a", A),), **overrides):
        finished = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        ts = finished.strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {"version": 1, "provider": "planetscale", "branch": "rehearsal/x",
               "apply_target": "rehearsal/x", "base": "prod",
               "migrations": [{"tag": t, "sha256": h, "result": "applied"}
                               for t, h in migrations],
               "status": status, "started_at": ts, "finished_at": ts,
               "cleanup": "deleted"}
        doc.update(overrides)
        return doc

    def evidence(self, *args, **kwargs):
        return self.write("evidence.json", json.dumps(self.evidence_doc(*args, **kwargs)))

    def evidence_raw(self, payload):
        """Write arbitrary JSON payload (for shape tests). payload may be
        any JSON-serializable value, or a raw string written verbatim."""
        if isinstance(payload, str):
            return self.write("evidence.json", payload)
        return self.write("evidence.json", json.dumps(payload))

    # -- run helpers (capture, never let gate crash the test) -----------
    def run_gate(self, *args):
        """Returns (rc, stdout, stderr, exc). rc is int exit code on
        normal/SystemExit paths, else 'ExcName: msg' for crashes."""
        buf_out, buf_err = io.StringIO(), io.StringIO()
        rc, exc = None, None
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                rc = gate.main(list(args))
            except SystemExit as e:
                exc = e
                rc = e.code if e.code is not None else 0
            except BaseException as e:  # noqa: BLE001 -- red baseline: crash, not exit
                exc = e
                rc = f"{e.__class__.__name__}: {e}"
        return rc, buf_out.getvalue(), buf_err.getvalue(), exc

    def parse_json_stdout(self, stdout, stderr, rc, context):
        self.assertTrue(
            stdout.strip().startswith("{") and stdout.strip().endswith("}"),
            f"{context}: JSON-mode stdout must be one pure object, "
            f"got rc={rc!r} stdout={stdout[:400]!r} stderr={stderr[:400]!r}")
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError as e:
            self.fail(f"{context}: stdout is not parseable JSON: {e}; "
                      f"stdout={stdout[:600]!r} stderr={stderr[:600]!r}")
        self.assertIsInstance(obj, dict, f"{context}: top-level JSON must be an object")
        return obj

    def assert_contract_shape(self, obj, context):
        for field in ("version", "verdict", "pending_count", "unknown_count",
                      "checks", "next_action"):
            self.assertIn(field, obj, f"{context}: missing top-level field {field!r} in {sorted(obj)}")
        self.assertEqual(obj["version"], EXPECTED_JSON_VERSION,
                         f"{context}: version must be {EXPECTED_JSON_VERSION}")
        self.assertIn(obj["verdict"], VERDICTS, f"{context}: bad verdict {obj['verdict']!r}")
        self.assertIsInstance(obj["pending_count"], int, f"{context}: pending_count must be int")
        self.assertIsInstance(obj["unknown_count"], int, f"{context}: unknown_count must be int")
        self.assertGreaterEqual(obj["pending_count"], 0, f"{context}: pending_count negative")
        self.assertGreaterEqual(obj["unknown_count"], 0, f"{context}: unknown_count negative")
        self.assertIsInstance(obj["checks"], list, f"{context}: checks must be a list")
        self.assertGreater(len(obj["checks"]), 0, f"{context}: checks must be non-empty")
        self.assertIsInstance(obj["next_action"], str, f"{context}: next_action must be str")
        for c in obj["checks"]:
            self.assertIsInstance(c, dict, f"{context}: each check must be an object, got {c!r}")
            for f in ("id", "status", "reason"):
                self.assertIn(f, c, f"{context}: check missing {f!r}: {c!r}")
            self.assertIn(c["id"], ALLOWED_CHECK_IDS,
                          f"{context}: unstable check id {c['id']!r}; allowed={sorted(ALLOWED_CHECK_IDS)}")
            self.assertIn(c["status"], STATUSES, f"{context}: bad status {c['status']!r}")
            self.assertIn(c["reason"], ALLOWED_REASON_IDS,
                          f"{context}: unstable reason {c['reason']!r}; allowed={sorted(ALLOWED_REASON_IDS)}")

    def assert_no_traceback_or_canary(self, stdout, stderr, context):
        for name, blob in (("stdout", stdout), ("stderr", stderr)):
            self.assertNotIn("Traceback", blob, f"{context}: traceback leaked in {name}")
            self.assertNotIn(CANARY, blob, f"{context}: raw evidence content leaked in {name}")

    def assert_refusal_actionable(self, obj, context, keywords=()):
        self.assertNotEqual(obj["verdict"], "pass", f"{context}: expected refusal, got pass")
        na = obj["next_action"]
        self.assertTrue(na.strip(), f"{context}: next_action must be actionable (non-empty) on refusal")
        self.assertGreaterEqual(len(na.strip()), 12,
                                f"{context}: next_action too short to be actionable: {na!r}")
        if keywords:
            hay = na.lower()
            self.assertTrue(any(k in hay for k in keywords),
                            f"{context}: next_action {na!r} should mention one of {keywords}")

    def json_run(self, *args, context=""):
        rc, out, err, exc = self.run_gate(*args)
        self.assertIsInstance(rc, int,
                              f"{context}: JSON mode must exit with int code, not crash: rc={rc!r} "
                              f"exc={exc!r} stderr={err[:300]!r}")
        obj = self.parse_json_stdout(out, err, rc, context)
        self.assert_contract_shape(obj, context)
        return rc, obj, out, err


class JsonEnvelopeTest(AxHarness):
    def test_help_mentions_json_flag(self):
        rc, out, err, _ = self.run_gate("--help")
        self.assertEqual(rc, 0, f"--help must exit 0, got {rc!r} out={out[:200]!r} err={err[:200]!r}")
        self.assertIn("--json", out, "--help must document the opt-in --json flag")

    def test_json_green_candidate_parseable_pass(self):
        rc, obj, out, err = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, B]), "--json",
            context="green-candidate")
        self.assertEqual(rc, 0, f"green JSON must exit 0: {out[:300]!r} {err[:300]!r}")
        self.assertEqual(obj["verdict"], "pass")
        self.assertEqual(obj["pending_count"], 0)
        self.assertEqual(obj["unknown_count"], 0)
        self.assert_no_traceback_or_canary(out, err, "green-candidate")

    def test_json_pending_subset_with_bound_rehearsal_parseable(self):
        rc, obj, out, err = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence(migrations=(("0043_b", B),)), "--json",
            context="pending-subset")
        self.assertEqual(rc, 0, f"bound pending subset must exit 0: {out[:300]!r} {err[:300]!r}")
        self.assertEqual(obj["verdict"], "pass")
        self.assertEqual(obj["pending_count"], 1)
        self.assertEqual(obj["unknown_count"], 0)
        self.assert_no_traceback_or_canary(out, err, "pending-subset")

    def test_json_stdout_is_pure_object_no_text_lines(self):
        _, _, out, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence(), "--json",
            context="pure-json")
        stripped = out.strip()
        self.assertFalse(stripped.startswith("ok:") or stripped.startswith("FAIL:"),
                         f"JSON mode must not emit text lines on stdout: {out[:200]!r}")
        # Full stdout must decode as exactly one JSON object; trailing
        # text after the document would prove mixed-mode output.
        decoder = json.JSONDecoder()
        _, end = decoder.raw_decode(out.strip())
        self.assertEqual(out.strip()[end:].strip(), "",
                         f"stdout must be exactly one JSON doc, trailing={out.strip()[end:][:200]!r}")

    def test_json_version_matches_canonical_source(self):
        self.assertTrue(hasattr(gate, "JSON_SCHEMA_VERSION"),
                        "gate must define one canonical JSON_SCHEMA_VERSION constant")
        self.assertEqual(gate.JSON_SCHEMA_VERSION, EXPECTED_JSON_VERSION,
                         "canonical JSON_SCHEMA_VERSION must equal 1")
        _, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--json",
            context="version-source")
        self.assertEqual(obj["version"], gate.JSON_SCHEMA_VERSION,
                         "JSON output version must equal the canonical constant")

    def test_json_counts_pending_and_unknown(self):
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, C]), "--json",
            context="counts")
        self.assertEqual(obj["pending_count"], 1, "B is pending (not in ledger)")
        self.assertEqual(obj["unknown_count"], 1, "C is unknown to the candidate")
        self.assertEqual(rc, 1)
        self.assertEqual(obj["verdict"], "fail")

    def test_json_deterministic_stable_ids_across_runs(self):
        args = ["--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]),
                "--evidence", self.evidence(), "--json"]
        rc1, obj1, _, _ = self.json_run(*args, context="determinism-1")
        rc2, obj2, _, _ = self.json_run(*args, context="determinism-2")
        self.assertEqual(rc1, rc2)
        for field in ("version", "verdict", "pending_count", "unknown_count", "next_action"):
            self.assertEqual(obj1[field], obj2[field], f"field {field} must be deterministic")
        ids1 = [(c["id"], c["status"], c["reason"]) for c in obj1["checks"]]
        ids2 = [(c["id"], c["status"], c["reason"]) for c in obj2["checks"]]
        self.assertEqual(ids1, ids2, "check/reason IDs must be stable across runs")

    def test_json_verdict_maps_to_exit(self):
        # pass -> 0
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--json", context="map-pass")
        self.assertEqual((rc, obj["verdict"]), (0, "pass"))
        # fail -> 1
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]), "--json", context="map-fail")
        self.assertEqual((rc, obj["verdict"]), (1, "fail"))
        # refused -> 2
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence_raw([1, 2]), "--json", context="map-refused")
        self.assertEqual((rc, obj["verdict"]), (2, "refused"))


class JsonGateFailureTest(AxHarness):
    """Every legacy FAIL (exit 1) stays FAIL in JSON mode with a stable
    reason and an actionable next_action. None may become pass."""

    def check_fail(self, context, keywords, expected_reason, *args):
        rc, obj, out, err = self.json_run(*args, "--json", context=context)
        self.assertEqual(rc, 1, f"{context}: must stay exit 1: {out[:300]!r} {err[:300]!r}")
        self.assertEqual(obj["verdict"], "fail", f"{context}: verdict must be fail")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertIn(expected_reason, reasons,
                      f"{context}: expected stable reason {expected_reason!r} in {reasons}")
        self.assert_refusal_actionable(obj, context, keywords)
        self.assert_no_traceback_or_canary(out, err, context)
        return obj

    def test_pending_without_rehearsal(self):
        self.check_fail("pending-no-evidence", ("rehears", "evidence", "pending"),
                        "pending_without_evidence",
                        "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                        "--ledger", self.ledger([A]))

    def test_unknown_ledger_rows(self):
        obj = self.check_fail("unknown-ledger", ("ledger", "out-of-band", "inspect"),
                              "unknown_ledger_rows",
                              "--sql-hashes", self.hashes([("0042_a", A)]),
                              "--ledger", self.ledger([A, C]))
        self.assertEqual(obj["unknown_count"], 1)

    def test_stale_evidence(self):
        self.check_fail("stale", ("rehears", "fresh", "max-age", "24"),
                        "evidence_stale",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence(age_hours=30))

    def test_future_evidence(self):
        self.check_fail("future", ("clock", "future", "rehears", "finished_at"),
                        "evidence_future",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence(age_hours=-2))

    def test_failed_status(self):
        self.check_fail("status-failed", ("rehears", "passed", "status"),
                        "evidence_status_not_passed",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence(status="failed"))

    def test_failed_cleanup_planetscale(self):
        doc = self.evidence_doc()
        doc["cleanup"] = "failed"
        self.check_fail("cleanup-failed", ("cleanup", "delet", "branch"),
                        "evidence_cleanup_unconfirmed",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence_raw(doc))

    def test_unbound_evidence(self):
        self.check_fail("unbound", ("bound", "pending", "rehears", "chain"),
                        "evidence_unbound",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence(migrations=(("0099_other", B),)))

    def test_digest_mismatch(self):
        self.check_fail("digest-mismatch", ("bound", "digest", "hash", "chain", "rehears"),
                        "evidence_unbound",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence(migrations=(("0042_a", B),)))

    def test_wrong_migration_order(self):
        self.check_fail("order", ("order", "filename", "sort"),
                        "evidence_order_mismatch",
                        "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                        "--ledger", self.ledger([A, B]),
                        "--evidence", self.evidence(migrations=(("0043_b", B), ("0042_a", A))))

    def test_not_applied_rows_despite_passed(self):
        doc = self.evidence_doc(migrations=(("0042_a", A),))
        doc["migrations"][0]["result"] = "failed"
        self.check_fail("not-applied", ("appl", "rehears"),
                        "evidence_not_applied",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence_raw(doc))

    def test_target_mismatch(self):
        doc = self.evidence_doc()
        doc["apply_target"] = "rehearsal/other"
        self.check_fail("target-mismatch", ("branch", "target", "apply"),
                        "evidence_target_mismatch",
                        "--sql-hashes", self.hashes([("0042_a", A)]),
                        "--ledger", self.ledger([A]),
                        "--evidence", self.evidence_raw(doc))


class JsonInputErrorTest(AxHarness):
    """Invalid shapes fail closed: exit 2, verdict refused, parseable
    JSON, actionable next_action, no traceback, no raw content."""

    def check_refused(self, context, keywords, expected_reasons, *args):
        if isinstance(expected_reasons, str):
            expected_reasons = (expected_reasons,)
        rc, obj, out, err = self.json_run(*args, "--json", context=context)
        self.assertEqual(rc, 2, f"{context}: must fail closed exit 2: {out[:300]!r} {err[:300]!r}")
        self.assertEqual(obj["verdict"], "refused", f"{context}: verdict must be refused")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertTrue(any(r in reasons for r in expected_reasons),
                        f"{context}: expected one of {expected_reasons} in {reasons}")
        self.assert_refusal_actionable(obj, context, keywords)
        self.assert_no_traceback_or_canary(out, err, context)
        return obj

    def base_args(self, evidence_path):
        return ("--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]), "--evidence", evidence_path)

    def test_empty_ledger_refused(self):
        rc, obj, out, err = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([]), "--json", context="empty-ledger")
        self.assertEqual(rc, 2)
        self.assertEqual(obj["verdict"], "refused")
        self.assertIn("input_empty_ledger", [c["reason"] for c in obj["checks"]])
        self.assert_refusal_actionable(obj, "empty-ledger", ("ledger",))
        self.assert_no_traceback_or_canary(out, err, "empty-ledger")

    def test_malformed_hashes_refused(self):
        bad = self.write("bad.txt", "not-a-hash  0042_a.sql\n")
        rc, obj, out, err = self.json_run(
            "--sql-hashes", bad, "--ledger", self.ledger([A]), "--json",
            context="bad-hashes")
        self.assertEqual(rc, 2)
        self.assertEqual(obj["verdict"], "refused")
        self.assertIn("input_malformed_hashes", [c["reason"] for c in obj["checks"]])
        self.assert_refusal_actionable(obj, "bad-hashes", ("hash", "candidate", "sql"))
        self.assert_no_traceback_or_canary(out, err, "bad-hashes")

    def test_negative_max_age_refused(self):
        rc, obj, out, err = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--max-age-hours", "-1", "--json",
            context="negative-max-age")
        self.assertEqual(rc, 2)
        self.assertEqual(obj["verdict"], "refused")
        self.assertIn("input_invalid_max_age", [c["reason"] for c in obj["checks"]])
        self.assert_refusal_actionable(obj, "negative-max-age", ("max-age",))
        self.assert_no_traceback_or_canary(out, err, "negative-max-age")

    def test_root_list_refused(self):
        self.check_refused("root-list", ("shape", "object", "evidence", "json"),
                           "evidence_invalid_root",
                           *self.base_args(self.evidence_raw([{"version": 1}])))

    def test_root_null_refused(self):
        self.check_refused("root-null", ("shape", "object", "evidence", "json"),
                           "evidence_invalid_root",
                           *self.base_args(self.evidence_raw("null")))

    def test_root_string_refused(self):
        self.check_refused("root-string", ("shape", "object", "evidence", "json"),
                           "evidence_invalid_root",
                           *self.base_args(self.evidence_raw('"just-a-string"')))

    def test_missing_version_refused(self):
        doc = self.evidence_doc()
        del doc["version"]
        self.check_refused("missing-version", ("version", "evidence", "shape"),
                           "evidence_invalid_version",
                           *self.base_args(self.evidence_raw(doc)))

    def test_version_wrong_type_refused(self):
        doc = self.evidence_doc()
        doc["version"] = "1"
        self.check_refused("version-type", ("version", "evidence", "shape"),
                           "evidence_invalid_version",
                           *self.base_args(self.evidence_raw(doc)))

    def test_migrations_not_list_refused(self):
        for label, bad in (("dict", {"0042_a": A}), ("string", "0042_a"),
                           ("null", None), ("number", 42)):
            with self.subTest(kind=label):
                doc = self.evidence_doc()
                doc["migrations"] = bad
                self.check_refused(f"migrations-{label}", ("migration", "list", "shape"),
                                   "evidence_invalid_migrations",
                                   *self.base_args(self.evidence_raw(doc)))

    def test_row_not_dict_refused(self):
        for label, bad_row in (("string", "0042_a"), ("null", None),
                               ("number", 7), ("list", ["0042_a"])):
            with self.subTest(kind=label):
                doc = self.evidence_doc()
                doc["migrations"] = [bad_row]
                self.check_refused(f"row-{label}", ("row", "migration", "shape"),
                                   "evidence_invalid_row",
                                   *self.base_args(self.evidence_raw(doc)))

    def test_row_field_types_refused(self):
        cases = [
            ("tag-int", {"tag": 42, "sha256": A, "result": "applied"}),
            ("tag-null", {"tag": None, "sha256": A, "result": "applied"}),
            ("sha-int", {"tag": "0042_a", "sha256": 123, "result": "applied"}),
            ("sha-short", {"tag": "0042_a", "sha256": "abc", "result": "applied"}),
            ("result-int", {"tag": "0042_a", "sha256": A, "result": 1}),
            ("missing-tag", {"sha256": A, "result": "applied"}),
            ("missing-sha", {"tag": "0042_a", "result": "applied"}),
        ]
        for label, row in cases:
            with self.subTest(kind=label):
                doc = self.evidence_doc()
                doc["migrations"] = [row]
                self.check_refused(f"field-{label}", ("field", "type", "migration", "tag", "sha"),
                                   ("evidence_invalid_row", "evidence_invalid_field"),
                                   *self.base_args(self.evidence_raw(doc)))

    def test_required_top_field_types_refused(self):
        for label, patch in (("status-int", {"status": 1}),
                             ("finished_at-int", {"finished_at": 123}),
                             ("finished_at-null", {"finished_at": None}),
                             ("provider-int", {"provider": 7}),
                             ("branch-int", {"branch": 9}),
                             ("cleanup-int", {"cleanup": 0})):
            with self.subTest(kind=label):
                doc = self.evidence_doc()
                doc.update(patch)
                self.check_refused(f"top-{label}", ("field", "type", "evidence", "shape"),
                                   ("evidence_invalid_field", "evidence_invalid_row",
                                    "evidence_unparseable_time"),
                                   *self.base_args(self.evidence_raw(doc)))

    def test_duplicate_tags_refused(self):
        doc = self.evidence_doc()
        doc["migrations"] = [{"tag": "0042_a", "sha256": A, "result": "applied"},
                             {"tag": "0042_a", "sha256": A, "result": "applied"}]
        self.check_refused("dup-tags", ("duplic", "tag"),
                           "evidence_duplicate_tag",
                           *self.base_args(self.evidence_raw(doc)))

    def test_malformed_json_refused(self):
        self.check_refused("bad-json", ("json", "parse", "evidence"),
                           "evidence_invalid_json",
                           *self.base_args(self.evidence_raw("{not json")))


class TextCompatibilityTest(AxHarness):
    """Text mode: invalid shapes exit 2 without crashing; legacy text
    and exits are preserved; JSON never changes the exit code."""

    def assert_text_refused(self, context, *args):
        rc, out, err, exc = self.run_gate(*args)
        self.assertEqual(rc, 2, f"{context}: text mode must fail closed exit 2, "
                                f"got rc={rc!r} exc={exc!r} out={out[:300]!r} err={err[:300]!r}")
        self.assertIsInstance(exc, SystemExit,
                              f"{context}: must refuse via SystemExit(2), not crash: {exc!r}")
        self.assertIn("REFUSED", err, f"{context}: stderr must carry REFUSED, got {err[:300]!r}")
        self.assertNotIn("Traceback", out + err, f"{context}: no traceback in text refusal")
        self.assertNotIn(CANARY, out + err, f"{context}: no raw evidence leak")
        return rc, out, err

    def test_text_root_list_refused(self):
        self.assert_text_refused(
            "text-root-list", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw([1]))

    def test_text_root_null_refused(self):
        self.assert_text_refused(
            "text-root-null", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw("null"))

    def test_text_migrations_not_list_refused(self):
        doc = self.evidence_doc()
        doc["migrations"] = {"0042_a": A}
        self.assert_text_refused(
            "text-migrations-dict", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw(doc))

    def test_text_row_not_dict_refused(self):
        doc = self.evidence_doc()
        doc["migrations"] = ["0042_a"]
        self.assert_text_refused(
            "text-row-string", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw(doc))

    def test_text_duplicate_tags_refused(self):
        doc = self.evidence_doc()
        doc["migrations"] = [{"tag": "0042_a", "sha256": A, "result": "applied"},
                             {"tag": "0042_a", "sha256": A, "result": "applied"}]
        self.assert_text_refused(
            "text-dup-tags", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw(doc))

    def test_text_field_type_refused(self):
        doc = self.evidence_doc()
        doc["migrations"] = [{"tag": 42, "sha256": A, "result": "applied"}]
        self.assert_text_refused(
            "text-field-type", "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence_raw(doc))

    def test_text_default_output_preserved(self):
        rc, out, err, _ = self.run_gate(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, B]))
        self.assertEqual(rc, 0, out)
        self.assertIn("ok: all 2 candidate migrations", out)
        self.assertIn("ok: no out-of-band", out)
        self.assertNotIn('"verdict"', out, "text mode must not emit JSON")

        rc, out, _, _ = self.run_gate(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]))
        self.assertEqual(rc, 1, out)
        self.assertIn("0043_b", out)

        rc, out, _, _ = self.run_gate(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--evidence", self.evidence(age_hours=30))
        self.assertEqual(rc, 1, out)
        self.assertIn("older than 24h", out)

    def test_exit_codes_match_text_and_json(self):
        # Fixture helpers write fixed filenames (candidate.txt, ledger.txt,
        # evidence.json) into the shared tmp dir, so each case must build
        # its fixtures immediately before running. Pre-building all cases
        # eagerly lets later cases clobber earlier ones (e.g. empty-ledger
        # empties ledger.txt for green/pending/stale).
        def build_green():
            return ["--sql-hashes", self.hashes([("0042_a", A)]),
                    "--ledger", self.ledger([A])]

        def build_pending():
            return ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                    "--ledger", self.ledger([A])]

        def build_stale():
            return ["--sql-hashes", self.hashes([("0042_a", A)]),
                    "--ledger", self.ledger([A]),
                    "--evidence", self.evidence(age_hours=30)]

        def build_empty_ledger():
            return ["--sql-hashes", self.hashes([("0042_a", A)]),
                    "--ledger", self.ledger([])]

        cases = [
            ("green", 0, build_green),
            ("pending", 1, build_pending),
            ("stale", 1, build_stale),
            ("empty-ledger", 2, build_empty_ledger),
        ]
        for label, expected, build in cases:
            with self.subTest(kind=label):
                argv = build()
                rc_text, _, _, _ = self.run_gate(*argv)
                rc_json, _, _, _ = self.run_gate(*argv, "--json")
                self.assertEqual(rc_text, expected, f"{label}: text exit")
                self.assertEqual(rc_json, expected, f"{label}: JSON must preserve exit {expected}")


class HygieneDeterminismTest(AxHarness):
    def test_no_raw_evidence_leak_either_mode(self):
        doc = self.evidence_doc()
        doc["secret_note"] = CANARY
        doc["migrations"][0]["secret_note"] = CANARY
        ev = self.evidence_raw(doc)
        argv = ["--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]), "--evidence", ev]
        for label, extra in (("text", []), ("json", ["--json"])):
            with self.subTest(mode=label):
                _, out, err, _ = self.run_gate(*argv, *extra)
                self.assertNotIn(CANARY, out, f"{label}: canary leaked in stdout")
                self.assertNotIn(CANARY, err, f"{label}: canary leaked in stderr")

    def test_inputs_not_mutated_idempotent(self):
        h_path = self.hashes([("0042_a", A)])
        l_path = self.ledger([A])
        e_path = self.evidence()
        before = {p: Path(p).read_bytes() for p in (h_path, l_path, e_path)}
        argv = ["--sql-hashes", h_path, "--ledger", l_path, "--evidence", e_path]
        # Text mode is the non-mutation control (works red and green).
        rc1, out1, _, _ = self.run_gate(*argv)
        rc2, out2, _, _ = self.run_gate(*argv)
        self.assertEqual(rc1, rc2, "second run must be idempotent")
        self.assertEqual(out1, out2, "text output must be deterministic across runs")
        after = {p: Path(p).read_bytes() for p in (h_path, l_path, e_path)}
        self.assertEqual(before, after, "gate must not mutate its inputs")
        leftovers = [p.name for p in self.root.iterdir()
                     if p.name not in ("candidate.txt", "ledger.txt", "evidence.json")]
        self.assertEqual(leftovers, [], f"gate must not create files: {leftovers}")

    def test_zero_network_calls_runtime(self):
        orig_socket = socket.socket

        def blocked(*a, **k):
            raise AssertionError("network is forbidden for this offline gate")

        socket.socket = blocked
        try:
            rc, _, _, _ = self.run_gate(
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]), "--evidence", self.evidence())
            self.assertEqual(rc, 0)
            rc, _, _, _ = self.run_gate(
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]), "--evidence", self.evidence(), "--json")
            # In red baseline --json is unknown (exit 2); either way no network.
            self.assertIn(rc, (0, 2))
        finally:
            socket.socket = orig_socket

    def test_zero_network_db_imports_static(self):
        src = SRC_PATH.read_text()
        forbidden = ["import socket", "import urllib", "import requests",
                     "import httpx", "import psycopg", "import sqlite3",
                     "from urllib", "from socket", "from requests"]
        for stmt in forbidden:
            self.assertNotIn(stmt, src, f"gate must stay files-only; found {stmt!r}")


class DocsCanonicalTest(AxHarness):
    def test_help_documents_json_contract(self):
        rc, out, _, _ = self.run_gate("--help")
        self.assertEqual(rc, 0)
        for token in ("--json", "verdict", "next_action", "JSON_SCHEMA_VERSION"):
            self.assertIn(token, out, f"--help must document {token!r} for the JSON contract")

    def test_docs_contain_json_contract(self):
        self.assertTrue(DOC_PATH.exists(), f"missing {DOC_PATH}")
        text = DOC_PATH.read_text()
        for token in ("--json", "JSON_SCHEMA_VERSION", '"version"', '"verdict"',
                      '"pending_count"', '"unknown_count"', '"checks"', '"next_action"',
                      "evidence_stale", "evidence_unbound"):
            self.assertIn(token, text, f"docs must document JSON contract token {token!r}")

    def test_docs_contain_code_to_next_action_examples(self):
        text = DOC_PATH.read_text()
        # At least three reason -> action mappings, each naming next_action.
        hits = sum(1 for rid in ("evidence_stale", "evidence_unbound",
                                 "evidence_duplicate_tag", "pending_without_evidence",
                                 "unknown_ledger_rows", "evidence_future")
                   if rid in text and "next_action" in text)
        self.assertGreaterEqual(hits, 3,
                                "docs must show code-to-next_action examples for >=3 reasons")
        self.assertIn("next_action", text)

    def test_docs_contain_offline_verification_command(self):
        text = DOC_PATH.read_text()
        for frag in VERIFICATION_COMMAND_FRAGMENTS:
            self.assertIn(frag, text,
                          f"docs must include the focused offline command fragment {frag!r}")

    def test_docs_explain_dry_run_inherent(self):
        text = DOC_PATH.read_text().lower()
        self.assertIn("dry-run", text, "docs must explain why dry-run is inherent")
        self.assertIn("read-only", text, "docs must name this read-only gate")
        self.assertTrue("inherent" in text or "inherently" in text,
                        "docs must say dry-run is inherent (read-only, non-mutating)")

    def test_canonical_schema_source_single_definition(self):
        src = SRC_PATH.read_text()
        defs = re.findall(r"^\s*JSON_SCHEMA_VERSION\s*=\s*1\s*$", src, flags=re.M)
        self.assertEqual(len(defs), 1,
                         f"exactly one canonical JSON_SCHEMA_VERSION = 1, found {len(defs)}")
        # No second definition elsewhere in repo-root python sources.
        dupes = []
        for py in HERE.glob("*.py"):
            if py.name in ("test_migration_evidence_ax.py",):
                continue
            if py == SRC_PATH:
                continue
            if "JSON_SCHEMA_VERSION" in py.read_text():
                dupes.append(py.name)
        self.assertEqual(dupes, [], f"canonical source must not be duplicated: {dupes}")
        self.assertIn("JSON_SCHEMA_VERSION", DOC_PATH.read_text(),
                      "docs must reference the canonical constant, not a copy")

    def test_no_duplicated_host_policy(self):
        _, obj, out, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--json", context="host-policy") \
            if "--json" in self.run_gate("--help")[1] else (None, {}, "", "")
        if obj:
            blob = json.dumps(obj).lower()
            for token in ("self-hosted", "ubuntu-latest", "webhook", "discord",
                          "gh issue", "pscale_token", "secret"):
                self.assertNotIn(token, blob, f"JSON verdict must not duplicate host policy {token!r}")
            self.assertNotIn("host", list(obj.keys()), "no host keys in the verdict object")
        text = DOC_PATH.read_text()
        # The JSON contract section must not re-state paging/host policy.
        start = text.find("--json")
        section = text[start:start + 6000].lower() if start >= 0 else ""
        for token in ("self-hosted", "ubuntu-latest", "webhook", "discord", "gh issue create"):
            self.assertNotIn(token, section,
                             f"JSON docs section must not duplicate host policy {token!r}")


if __name__ == "__main__":
    unittest.main()
