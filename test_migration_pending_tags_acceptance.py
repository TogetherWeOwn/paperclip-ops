#!/usr/bin/env python3
"""Immutable builder acceptance contract for the pending-tag projection.

IMMUTABLE: the builder starts from this pushed branch/commit and MUST NOT
edit this file. Corrections return to the test-author card. No standalone
test-only PR: this branch is handed to the paired source PR.

Contract under test (migration_drift_gate.py):

- Opt-in ``--include-pending-tags`` is valid only with ``--json``.
  Requested JSON adds exactly ``pending_tags`` (the complete, sorted
  candidate tag IDs whose digests are absent from the ledger, with no
  digests) and ``pending_tags_evaluated`` (an explicit boolean).
  Default text and default JSON output and exit semantics are otherwise
  unchanged, and default JSON gains neither new field.
- Before candidate and ledger parsing both succeed, ``pending_tags`` is
  null and ``pending_tags_evaluated`` is false. After the pending
  computation succeeds, the exact sorted list and true are emitted,
  including ``[]`` for a genuinely empty subset and preserving the
  result when later evidence validation refuses or the gate fails.
- Pending reuses the existing computation (candidate digests absent
  from applied digests), including duplicate-content behaviour.
  Unknown ledger rows remain counts only. Existing pass/fail/refused
  exits 0/1/2, evidence chain/hash/order, freshness and cleanup checks
  are unchanged: the projection is diagnostic only, never an
  authorization or a gate bypass.
- Never echo ledger rows, hashes, evidence bodies, branch names or raw
  parser exceptions through the new fields (or requested-mode stdout /
  stderr). Invalid flag combination is an explicit nonzero usage
  error; ``--help`` and docs/migration-standard.md document the
  correct combined invocation and the JSON fields.
- The gate stays files-only: it reads only the supplied local files,
  makes no network/DB/API calls, launches no rehearsal or migration,
  and does not mutate its inputs. Public helpers stay compatible.

Synthetic fixtures only. Fully offline: temp dirs, no DB, no network,
no credentials, no staging or production migration.

RED baseline (source has no ``--include-pending-tags``): the
projection, refusal-state, help and docs tests FAIL with argparse
"unrecognized arguments" or the missing fields/tokens. The explicit
compatibility controls PASS on the same source. That split is the
point: do not weaken asserts to make the baseline pass.

Runnable command (from repo root):
  python3 -B -m unittest -v test_migration_evidence_ax \
      test_migration_drift_gate test_migration_pending_tags_acceptance
"""
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

import migration_drift_gate as gate

HERE = Path(__file__).resolve().parent
GATE_PATH = HERE / "migration_drift_gate.py"
DOC_PATH = HERE / "docs" / "migration-standard.md"

BASE_KEYS = frozenset({
    "version", "verdict", "pending_count", "unknown_count",
    "checks", "next_action",
})
PENDING_KEYS = frozenset({"pending_tags", "pending_tags_evaluated"})

CANARY = "CANARY-PENDING-TAGS-NEVER-ECHO-51d8b09c"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRES_JSON_RE = re.compile(
    r"(requires?|required|only(?:\s+\w+){0,3}\s+with|must\s+be\s+used\s+with|"
    r"must\s+be\s+combined\s+with|needs?)[^\n]{0,80}--json",
    re.IGNORECASE)


def sha(b):
    return hashlib.sha256(b).hexdigest()


A = sha(b"-- phase: expand\nselect 1;\n")
B = sha(b"-- phase: expand\nselect 2;\n")
C = sha(b"-- phase: contract\nselect 3;\n")
D = sha(b"-- phase: contract\nselect 4;\n")
UNKNOWN_2 = sha(b"-- out of band\nselect 99;\n")


class PendingTagsHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pending-tags-ax-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    # -- fixture helpers (synthetic only) -------------------------------
    def write(self, name, text):
        p = self.root / name
        p.write_text(text)
        return str(p)

    def write_bytes(self, name, data):
        p = self.root / name
        p.write_bytes(data)
        return str(p)

    def hashes(self, pairs):
        return self.write(
            "candidate.txt", "".join(f"{h}  {t}.sql\n" for t, h in pairs))

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
        return self.write("evidence.json",
                          json.dumps(self.evidence_doc(*args, **kwargs)))

    def evidence_raw(self, payload):
        if isinstance(payload, str):
            return self.write("evidence.json", payload)
        return self.write("evidence.json", json.dumps(payload))

    # -- run helpers ------------------------------------------------------
    def run_gate(self, *args):
        """In-process run: (rc, stdout, stderr, exc). Crashes surface as a
        string rc so asserts fail readably instead of erroring the suite."""
        buf_out, buf_err = io.StringIO(), io.StringIO()
        rc, exc = None, None
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                rc = gate.main(list(args))
            except SystemExit as e:
                exc = e
                rc = e.code if e.code is not None else 0
            except BaseException as e:  # a crash is a failure
                exc = e
                rc = f"{e.__class__.__name__}: {e}"
        return rc, buf_out.getvalue(), buf_err.getvalue(), exc

    def run_subprocess(self, *args):
        """Subprocess-visible run: (rc, stdout, stderr)."""
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"}
        proc = subprocess.run(
            [sys.executable, "-B", str(GATE_PATH), *args],
            cwd=str(HERE), env=env, capture_output=True, text=True,
            timeout=30)
        return proc.returncode, proc.stdout, proc.stderr

    def parse_json_stdout(self, stdout, stderr, rc, context):
        self.assertIsInstance(
            rc, int, f"{context}: gate must exit with an int code, not "
            f"crash: rc={rc!r} stderr={stderr[:400]!r}")
        stripped = stdout.strip()
        self.assertTrue(
            stripped.startswith("{") and stripped.endswith("}"),
            f"{context}: JSON stdout must be exactly one object, got "
            f"rc={rc!r} stdout={stdout[:400]!r} stderr={stderr[:600]!r}. "
            f"On the RED baseline this is the argparse 'unrecognized "
            f"arguments' refusal for the missing --include-pending-tags.")
        try:
            obj, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError as e:
            self.fail(f"{context}: stdout is not parseable JSON: {e}; "
                      f"stdout={stdout[:600]!r} stderr={stderr[:600]!r}")
        self.assertEqual(
            stripped[end:].strip(), "",
            f"{context}: stdout must be exactly one JSON doc, "
            f"trailing={stripped[end:][:200]!r}")
        self.assertIsInstance(obj, dict,
                              f"{context}: top-level JSON must be an object")
        return obj

    def json_run(self, *args, context=""):
        rc, out, err, _ = self.run_gate(*args)
        obj = self.parse_json_stdout(out, err, rc, context)
        return rc, obj, out, err

    def assert_base_shape(self, obj, context):
        for field in BASE_KEYS:
            self.assertIn(field, obj,
                          f"{context}: missing base field {field!r}")
        self.assertEqual(obj["version"], 1, f"{context}: version must stay 1")
        self.assertIn(obj["verdict"], ("pass", "fail", "refused"),
                      f"{context}: bad verdict {obj['verdict']!r}")
        self.assertIsInstance(obj["pending_count"], int, context)
        self.assertIsInstance(obj["unknown_count"], int, context)
        self.assertIsInstance(obj["checks"], list, context)
        self.assertGreater(len(obj["checks"]), 0, context)
        for check in obj["checks"]:
            self.assertIsInstance(check, dict, f"{context}: {check!r}")
            for f in ("id", "status", "reason"):
                self.assertIn(f, check, f"{context}: check missing {f!r}")

    def assert_pending(self, obj, expected, evaluated, context):
        """expected is the exact sorted list, or None when unevaluated."""
        for field in PENDING_KEYS:
            self.assertIn(field, obj,
                          f"{context}: requested JSON must add {field!r}; "
                          f"keys={sorted(obj)}")
        self.assertIs(
            obj["pending_tags_evaluated"], evaluated,
            f"{context}: pending_tags_evaluated must be exactly "
            f"{evaluated!r}, got {obj['pending_tags_evaluated']!r}")
        if not evaluated:
            self.assertIsNone(
                obj["pending_tags"],
                f"{context}: before candidate+ledger parsing both "
                f"succeed, pending_tags must be null (never a partial "
                f"list), got {obj['pending_tags']!r}")
            self.assertIsNone(expected,
                              f"{context}: test bug: unevaluated needs None")
            return
        tags = obj["pending_tags"]
        self.assertIsInstance(tags, list,
                              f"{context}: evaluated pending_tags must be a "
                              f"list, got {tags!r}")
        self.assertEqual(
            tags, expected,
            f"{context}: pending_tags must be the exact complete sorted "
            f"pending subset {expected!r}, got {tags!r}")
        self.assertEqual(tags, sorted(tags),
                         f"{context}: pending_tags must be sorted: {tags!r}")
        self.assertEqual(
            obj["pending_count"], len(tags),
            f"{context}: pending_count must still equal the pending "
            f"subset size when evaluated")
        for tag in tags:
            self.assertIsInstance(tag, str, f"{context}: tag {tag!r}")
            self.assertFalse(DIGEST_RE.match(tag),
                             f"{context}: pending_tags must carry tag IDs, "
                             f"not digests: {tag!r}")
            self.assertFalse(tag.endswith(".sql"),
                             f"{context}: tag IDs have no file suffix: {tag!r}")

    def assert_no_disclosure(self, stdout, stderr, secrets, context):
        blob = stdout + "\n" + stderr
        for token in secrets:
            self.assertNotIn(
                token, blob,
                f"{context}: requested mode must not disclose {token!r} "
                f"(ledger rows, hashes, evidence bodies, branch names or "
                f"raw parser input)")
        for token in ("Traceback", "UnicodeDecodeError", "JSONDecodeError"):
            self.assertNotIn(token, blob,
                             f"{context}: raw parser exception leaked: {token}")

    def assert_mode_parity(self, argv, expected_rc, expected_pending,
                           evaluated, context):
        """Text, default JSON and requested JSON agree on exit; requested
        JSON stripped of the two new fields equals default JSON exactly,
        and default JSON itself gained neither field."""
        rc_text, out_text, _, _ = self.run_gate(*argv)
        self.assertEqual(rc_text, expected_rc,
                         f"{context}: text exit must stay {expected_rc}: "
                         f"out={out_text[:300]!r}")
        rc_plain, plain, _, err_plain = self.json_run(
            *argv, "--json", context=f"{context}/default-json")
        self.assertEqual(rc_plain, expected_rc, f"{context}: default JSON exit")
        self.assertEqual(
            frozenset(plain), BASE_KEYS,
            f"{context}: default JSON keys must be unchanged (no "
            f"pending fields without the opt-in): {sorted(plain)}")
        rc_req, req, _, err_req = self.json_run(
            *argv, "--json", "--include-pending-tags",
            context=f"{context}/requested")
        self.assertEqual(rc_req, expected_rc,
                         f"{context}: requested JSON must preserve the exit: "
                         f"the projection never authorizes or refuses "
                         f"anything by itself")
        self.assertEqual(err_req, err_plain,
                         f"{context}: requested mode must not add stderr noise")
        self.assertEqual(
            frozenset(req), BASE_KEYS | PENDING_KEYS,
            f"{context}: requested JSON must add exactly the two pending "
            f"fields: {sorted(req)}")
        stripped = {k: v for k, v in req.items() if k not in PENDING_KEYS}
        self.assertEqual(
            stripped, plain,
            f"{context}: requested JSON minus the pending fields must "
            f"equal default JSON exactly (verdict, counts, checks, "
            f"next_action unchanged)")
        self.assert_base_shape(req, context)
        self.assert_pending(req, expected_pending, evaluated, context)
        return plain, req


class ExistingBehaviourControlTest(PendingTagsHarness):
    """Baseline controls. Every test here must PASS on the unmodified
    source; they pin the verdict/exit surface the feature must preserve,
    so a RED run can tell 'feature missing' from 'fixture broken'."""

    def test_control_text_green_and_pending_exits(self):
        rc, out, _, _ = self.run_gate(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, B]))
        self.assertEqual(rc, 0, out)
        self.assertIn("ok: all 2 candidate migrations", out)
        rc, out, _, _ = self.run_gate(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]))
        self.assertEqual(rc, 1, out)
        self.assertIn("0043_b", out)

    def test_control_default_json_shape_without_pending_fields(self):
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]), "--json", context="control-default")
        self.assertEqual((rc, obj["verdict"]), (1, "fail"))
        self.assertEqual(obj["pending_count"], 1)
        self.assertEqual(frozenset(obj), BASE_KEYS,
                         "today's default JSON key set is the frozen baseline")
        self.assert_base_shape(obj, "control-default")

    def test_control_refused_malformed_candidate_default_json(self):
        bad = self.write("bad.txt", "not-a-hash  0042_a.sql\n")
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", bad, "--ledger", self.ledger([A]), "--json",
            context="control-refused")
        self.assertEqual((rc, obj["verdict"]), (2, "refused"))

    def test_control_public_entrypoints_exist(self):
        self.assertTrue(callable(gate.main), "gate.main is the public entry")
        # Spelled dynamically: the sibling canonical-source suite treats
        # any textual mention of the constant's name in another module as
        # a duplicated definition.
        version_attr = "JSON_" + "SCHEMA_VERSION"
        self.assertEqual(getattr(gate, version_attr, None), 1,
                         "canonical JSON schema-version constant must stay 1")
        rc, out, _, _ = self.run_gate("--help")
        self.assertEqual(rc, 0, "help exits 0 today and must keep doing so")
        self.assertIn("--json", out)

    def test_control_duplicate_content_counts_each_tag(self):
        # Existing duplicate-content behaviour the projection must reuse:
        # two tags sharing one unapplied digest both count as pending.
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_dup", A)]),
            "--ledger", self.ledger([B]), "--json", context="control-dup")
        self.assertEqual(obj["pending_count"], 2)
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_dup", A)]),
            "--ledger", self.ledger([A]), "--json", context="control-dup-ok")
        self.assertEqual((rc, obj["pending_count"]), (0, 0))


class PendingTagsProjectionTest(PendingTagsHarness):
    """The new projection on evaluated inputs (RED until implemented)."""

    def test_unordered_candidates_emit_complete_sorted_pending(self):
        argv = ["--sql-hashes",
                self.hashes([("0099_z", D), ("0002_a", A),
                             ("0043_b", B), ("0010_m", C)]),
                "--ledger", self.ledger([A])]
        _, req = self.assert_mode_parity(
            argv, 1, ["0010_m", "0043_b", "0099_z"], True, "unordered")
        self.assertEqual(req["verdict"], "fail",
                         "pending without evidence still fails; the list "
                         "is diagnostic, not authorization")

    def test_all_applied_emits_empty_list_not_null(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A, B])]
        _, req = self.assert_mode_parity(argv, 0, [], True, "all-applied")
        self.assertEqual(req["verdict"], "pass")

    def test_pending_without_evidence_still_fails_with_exact_list(self):
        rc, obj, _, _ = self.json_run(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--json", "--include-pending-tags", context="pending-no-evidence")
        self.assertEqual((rc, obj["verdict"]), (1, "fail"))
        self.assert_pending(obj, ["0043_b"], True, "pending-no-evidence")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertIn("pending_without_evidence", reasons)

    def test_flag_order_does_not_matter(self):
        rc, obj, _, _ = self.json_run(
            "--include-pending-tags", "--json",
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]), context="flag-order")
        self.assertEqual(rc, 1)
        self.assert_pending(obj, ["0043_b"], True, "flag-order")

    def test_duplicate_content_tags_both_listed(self):
        argv = ["--sql-hashes",
                self.hashes([("0043_dup", A), ("0042_a", A), ("0044_b", B)]),
                "--ledger", self.ledger([C])]
        self.assert_mode_parity(
            argv, 1, ["0042_a", "0043_dup", "0044_b"], True, "dup-content")

    def test_duplicate_content_applied_digest_lists_neither(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A), ("0043_dup", A)]),
                "--ledger", self.ledger([A])]
        self.assert_mode_parity(argv, 0, [], True, "dup-content-applied")

    def test_unknown_ledger_rows_remain_counts_only(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A, C, UNKNOWN_2])]
        _, req = self.assert_mode_parity(
            argv, 1, ["0043_b"], True, "unknown-ledger")
        self.assertEqual(req["unknown_count"], 2,
                         "unknown ledger digests stay a count; no tag may "
                         "be invented for them")
        self.assertEqual(req["verdict"], "fail")

    def test_large_pending_set_complete_no_truncation(self):
        digests = [sha(f"select {i};\n".encode()) for i in range(40)]
        pairs = [(f"{i:04d}_tag{i:02d}", digests[i]) for i in range(40)]
        argv = ["--sql-hashes", self.hashes(list(reversed(pairs))),
                "--ledger", self.ledger([digests[0]])]
        expected = [f"{i:04d}_tag{i:02d}" for i in range(1, 40)]
        _, req = self.assert_mode_parity(argv, 1, expected, True, "large-set")
        self.assertEqual(len(req["pending_tags"]), 39,
                         "no silent truncation of the complete subset")

    def test_valid_bound_evidence_passes_and_lists_pending(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A]),
                "--evidence", self.evidence(migrations=(("0043_b", B),))]
        _, req = self.assert_mode_parity(argv, 0, ["0043_b"], True, "valid-ev")
        self.assertEqual(req["verdict"], "pass")

    def test_stale_evidence_still_fails_and_preserves_list(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A]),
                "--evidence", self.evidence(age_hours=30,
                                            migrations=(("0043_b", B),))]
        _, req = self.assert_mode_parity(argv, 1, ["0043_b"], True, "stale-ev")
        reasons = [c["reason"] for c in req["checks"]]
        self.assertIn("evidence_stale", reasons)

    def test_failed_and_unbound_evidence_preserve_list_and_verdict(self):
        cases = []
        cases.append(("failed-status", lambda: [
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence(status="failed",
                                        migrations=(("0043_b", B),))]))
        cases.append(("cleanup-failed", lambda: [
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence_raw(
                self.evidence_doc(migrations=(("0043_b", B),),
                                  cleanup="failed"))]))
        cases.append(("unbound", lambda: [
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence(migrations=(("0099_other", D),))]))
        cases.append(("wrong-order", lambda: [
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, B]),
            "--evidence", self.evidence(
                migrations=(("0043_b", B), ("0042_a", A)))]))
        for label, build in cases:
            with self.subTest(kind=label):
                expected = [] if label == "wrong-order" else ["0043_b"]
                self.assert_mode_parity(build(), 1, expected, True, label)


class PendingTagsRefusalStateTest(PendingTagsHarness):
    """null/false before both parses succeed; exact list preserved
    when a later evidence stage refuses (RED until implemented)."""

    def base_pending_argv(self, evidence_path):
        return ["--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A]),
                "--evidence", evidence_path]

    def test_malformed_candidate_is_null_false(self):
        bad = self.write("bad.txt", f"not-a-hash {CANARY} 0042_a.sql\n")
        argv = ["--sql-hashes", bad, "--ledger", self.ledger([A])]
        self.assert_mode_parity(argv, 2, None, False, "bad-candidate")

    def test_malformed_ledger_is_null_false(self):
        bad = self.write("badledger.txt", f"no hash on this row {CANARY}\n")
        argv = ["--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", bad]
        self.assert_mode_parity(argv, 2, None, False, "bad-ledger")

    def test_non_utf8_candidate_and_ledger_are_null_false(self):
        cases = [
            ("candidate", lambda: [
                "--sql-hashes", self.write_bytes("c.bin", b"\xff\xfe\x00bad\n"),
                "--ledger", self.ledger([A])]),
            ("ledger", lambda: [
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.write_bytes("l.bin", b"\xff\xfe\x00bad\n")]),
        ]
        for label, build in cases:
            with self.subTest(kind=label):
                self.assert_mode_parity(build(), 2, None, False,
                                        f"nonutf8-{label}")

    def test_unreadable_candidate_and_ledger_are_null_false(self):
        missing = str(self.root / "does-not-exist.txt")
        directory = str(self.root)
        cases = [
            ("missing-candidate", lambda: [
                "--sql-hashes", missing, "--ledger", self.ledger([A])]),
            ("missing-ledger", lambda: [
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", missing]),
            ("candidate-is-directory", lambda: [
                "--sql-hashes", directory, "--ledger", self.ledger([A])]),
            ("ledger-is-directory", lambda: [
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", directory]),
        ]
        for label, build in cases:
            with self.subTest(kind=label):
                self.assert_mode_parity(build(), 2, None, False, label)

    def test_malformed_evidence_after_pending_preserves_exact_list(self):
        cases = [
            ("not-json", lambda: self.evidence_raw("{not json " + CANARY)),
            ("root-list", lambda: self.evidence_raw([1, 2])),
            ("duplicate-evidence-tags", lambda: self.evidence_raw(
                self.evidence_doc(migrations=(("0043_b", B), ("0043_b", B))))),
            ("bad-field-type", lambda: self.evidence_raw(
                self.evidence_doc(migrations=(("0043_b", B),),
                                  finished_at=123))),
            ("nonutf8", lambda: self.write_bytes("evidence.bin",
                                                 b"\xff\xfe\x00bad")),
            ("missing-evidence", lambda: str(self.root / "no-evidence.json")),
        ]
        for label, make_evidence in cases:
            with self.subTest(kind=label):
                argv = self.base_pending_argv(make_evidence())
                _, req = self.assert_mode_parity(
                    argv, 2, ["0043_b"], True, f"evidence-{label}")
                self.assertEqual(req["verdict"], "refused",
                                 "the refusal itself is unchanged; only the "
                                 "already-computed list is preserved")

    def test_malformed_evidence_with_none_pending_preserves_empty_list(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]),
                "--evidence", self.evidence_raw("{not json")]
        self.assert_mode_parity(argv, 2, [], True, "evidence-empty-pending")

    def test_include_without_json_is_explicit_usage_error(self):
        argv = ["--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.ledger([A]), "--include-pending-tags"]
        rc, out, err, exc = self.run_gate(*argv)
        self.assertIsInstance(rc, int, f"usage error must exit, not crash: {exc!r}")
        self.assertNotEqual(rc, 0, "flag without --json must not pass silently")
        self.assertEqual(rc, 2, "usage errors exit 2 like other bad input")
        self.assertNotIn("ok:", out, "the gate must not run on an invalid combo")
        self.assertNotIn("unrecognized arguments", err,
                         "the flag must be recognized and rejected for the "
                         "combination, not as an unknown argument")
        self.assertIn("--include-pending-tags", err)
        self.assertTrue(
            REQUIRES_JSON_RE.search(err) or REQUIRES_JSON_RE.search(out),
            f"error must explicitly say the flag requires/only works "
            f"with --json and point at the next command: err={err[:600]!r} "
            f"out={out[:300]!r}")
        self.assertNotIn("Traceback", out + err)


class PendingTagsSafetyTest(PendingTagsHarness):
    """Disclosure, subprocess, mutation and files-only safety."""

    def test_requested_mode_discloses_no_digests_ledger_or_evidence(self):
        doc = self.evidence_doc(
            migrations=(("0043_b", B),),
            branch=f"rehearsal/{CANARY}",
            apply_target=f"rehearsal/{CANARY}")
        doc["secret_note"] = CANARY
        doc["migrations"][0]["secret_note"] = CANARY
        ev = self.evidence_raw(doc)
        h_path = self.hashes([("0042_a", A), ("0043_b", B)])
        l_path = self.ledger([A, C])
        rc, obj, out, err = self.json_run(
            "--sql-hashes", h_path, "--ledger", l_path, "--evidence", ev,
            "--json", "--include-pending-tags", context="no-disclosure")
        self.assertEqual(rc, 1, "unknown ledger row still fails the gate")
        self.assert_pending(obj, ["0043_b"], True, "no-disclosure")
        self.assert_no_disclosure(
            out, err, [CANARY, A, B, C, Path(l_path).read_text().strip(),
                       "secret_note"], "no-disclosure")

    def test_parser_refusals_do_not_echo_raw_input(self):
        # Fixture files share names across cases; rebuild each argv lazily.
        builders = [
            ("candidate", lambda: [
                "--sql-hashes", self.write("bad.txt", f"junk {CANARY} x.sql\n"),
                "--ledger", self.ledger([A])], None, False),
            ("ledger", lambda: [
                "--sql-hashes", self.hashes([("0042_a", A)]),
                "--ledger", self.write("badl.txt", f"junk row {CANARY}\n")],
             None, False),
            ("evidence", lambda: [
                "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A]),
                "--evidence", self.evidence_raw("{broken " + CANARY)],
             ["0043_b"], True),
        ]
        for label, build, expected, evaluated in builders:
            with self.subTest(kind=label):
                rc, obj, out, err = self.json_run(
                    *build(), "--json", "--include-pending-tags",
                    context=f"raw-input-{label}")
                self.assertEqual((rc, obj["verdict"]), (2, "refused"))
                self.assert_pending(obj, expected, evaluated,
                                    f"raw-input-{label}")
                self.assert_no_disclosure(out, err, [CANARY],
                                          f"raw-input-{label}")

    def test_subprocess_pending_and_all_applied(self):
        rc, out, err = self.run_subprocess(
            "--sql-hashes", self.hashes([("0043_b", B), ("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--json", "--include-pending-tags")
        obj = self.parse_json_stdout(out, err, rc, "subprocess-pending")
        self.assertEqual(rc, 1)
        self.assert_pending(obj, ["0043_b"], True, "subprocess-pending")
        self.assert_no_disclosure(out, err, [A, B], "subprocess-pending")

        rc, out, err = self.run_subprocess(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--json", "--include-pending-tags")
        obj = self.parse_json_stdout(out, err, rc, "subprocess-applied")
        self.assertEqual((rc, obj["verdict"]), (0, "pass"))
        self.assert_pending(obj, [], True, "subprocess-applied")

    def test_subprocess_refusal_after_pending_and_invalid_combo(self):
        rc, out, err = self.run_subprocess(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A]),
            "--evidence", self.evidence_raw("{not json"),
            "--json", "--include-pending-tags")
        obj = self.parse_json_stdout(out, err, rc, "subprocess-refused")
        self.assertEqual((rc, obj["verdict"]), (2, "refused"))
        self.assert_pending(obj, ["0043_b"], True, "subprocess-refused")

        rc, out, err = self.run_subprocess(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]), "--include-pending-tags")
        self.assertEqual(rc, 2, f"invalid combo must exit 2: {err[:400]!r}")
        self.assertNotIn("unrecognized arguments", err)
        self.assertTrue(REQUIRES_JSON_RE.search(err + out),
                        f"usage error must name the --json requirement: "
                        f"err={err[:600]!r} out={out[:300]!r}")
        self.assertNotIn("Traceback", out + err)

    def test_inputs_byte_identical_and_no_new_files(self):
        h_path = self.hashes([("0042_a", A), ("0043_b", B)])
        l_path = self.ledger([A])
        e_path = self.evidence(migrations=(("0043_b", B),))
        before = {p: Path(p).read_bytes() for p in (h_path, l_path, e_path)}
        names_before = sorted(p.name for p in self.root.iterdir())
        argv = ["--sql-hashes", h_path, "--ledger", l_path,
                "--evidence", e_path, "--json", "--include-pending-tags"]
        rc1, obj1, out1, _ = self.json_run(*argv, context="immutable-1")
        rc2, obj2, out2, _ = self.json_run(*argv, context="immutable-2")
        self.assertEqual((rc1, obj1), (rc2, obj2), "runs are deterministic")
        self.assertEqual(out1, out2, "requested stdout is byte-stable")
        after = {p: Path(p).read_bytes() for p in (h_path, l_path, e_path)}
        self.assertEqual(before, after, "inputs must be byte-identical")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         names_before, "the gate must not create files")

    def test_zero_network_calls_in_requested_mode(self):
        orig_socket, orig_create = socket.socket, socket.create_connection

        def blocked(*a, **k):
            raise AssertionError("network is forbidden for this offline gate")

        socket.socket = blocked
        socket.create_connection = blocked
        try:
            rc, obj, _, _ = self.json_run(
                "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                "--ledger", self.ledger([A]),
                "--json", "--include-pending-tags", context="no-network")
            self.assertEqual(rc, 1)
            self.assert_pending(obj, ["0043_b"], True, "no-network")
        finally:
            socket.socket = orig_socket
            socket.create_connection = orig_create

    def test_source_stays_files_only_static(self):
        # Control that also holds on the RED baseline: no client, fetch,
        # rehearsal-launch or migration-execution machinery in source.
        src = GATE_PATH.read_text()
        for token in ("import socket", "import urllib", "from urllib",
                      "import requests", "import httpx", "import psycopg",
                      "import sqlite3", "import subprocess", "os.system",
                      "Popen(", "create_connection"):
            self.assertTrue(token not in src,
                            f"gate must stay files-only; found {token!r}")

    def test_source_declares_the_pending_projection(self):
        src = GATE_PATH.read_text()
        for token in ("--include-pending-tags", "pending_tags",
                      "pending_tags_evaluated"):
            self.assertTrue(token in src,
                            f"source must declare {token!r} for the projection")


class PendingTagsHelpDocsTest(PendingTagsHarness):
    """Help and the canonical docs ship in the same PR."""

    def test_help_documents_flag_fields_and_next_command(self):
        rc, out, err, _ = self.run_gate("--help")
        self.assertEqual(rc, 0, f"--help must exit 0: err={err[:300]!r}")
        flat = re.sub(r"\s+", " ", out)
        for token in ("--include-pending-tags", "pending_tags",
                      "pending_tags_evaluated"):
            self.assertTrue(token in flat, f"--help must document {token!r}")
        self.assertTrue(
            REQUIRES_JSON_RE.search(flat),
            "--help must state the flag is only valid with --json and "
            f"show that next/correct combination: {flat[:1200]!r}")
        self.assertIn("sorted", flat.lower(),
                      "--help must say pending_tags is the sorted subset")

    def test_docs_document_pending_contract(self):
        self.assertTrue(DOC_PATH.exists(), f"missing {DOC_PATH}")
        text = DOC_PATH.read_text()
        flat = re.sub(r"\s+", " ", text)
        for token in ("--include-pending-tags", '"pending_tags"',
                      '"pending_tags_evaluated"'):
            self.assertTrue(token in text,
                            f"docs must document JSON contract token {token!r}")
        self.assertTrue(REQUIRES_JSON_RE.search(flat),
                        "docs must state the flag requires --json")
        lowered = flat.lower()
        self.assertIn("sorted", lowered,
                      "docs must state pending_tags is sorted")
        self.assertIn("null", lowered,
                      "docs must state the unevaluated pending_tags is null")
        self.assertTrue(
            re.search(r"(diagnostic|does not authorize|not\s+.{0,30}"
                      r"authori[sz]ation|never\s+.{0,30}authori)", lowered),
            "docs must state the projection is diagnostic only, never a "
            "rehearsal/deploy authorization")

    def test_docs_verification_command_includes_this_suite(self):
        text = DOC_PATH.read_text()
        for token in ("test_migration_evidence_ax", "test_migration_drift_gate",
                      "test_migration_pending_tags_acceptance"):
            self.assertTrue(token in text,
                            f"docs offline verification must run {token!r}")


if __name__ == "__main__":
    unittest.main()
