#!/usr/bin/env python3
"""Acceptance: preview closeout proposals without persisting ledger artifacts.

Offline suite for the EXISTING pr_closeout_ledger.py CLI. Synthetic fixtures
only: no network, API, database, credential, timer or host action. Green here
means the PREVIEW path works -- it never claims deployed automation or savings.

Run:
  python3 -B -m unittest -v test_pr_closeout_preview_acceptance
  python3 -B -m unittest -v test_pr_closeout_ledger

Baseline expectation: preview tests RED (missing --dry-run), legacy controls
GREEN. The paired build consumes this file's branch/commit and must never edit
this file.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pr_closeout_ledger as ledger


NOW = dt.datetime(2026, 10, 2, 17, 0, 0, tzinfo=dt.timezone.utc)
NOW_LATER = dt.datetime(2026, 10, 2, 18, 30, 0, tzinfo=dt.timezone.utc)
HEAD_A = "a" * 40
HEAD_B = "b" * 40
REQUIRED = ["check", "gitleaks", "pr-lint"]
REPO = "two-web-next"
PR = 291
PR_KEY = f"{REPO}#{PR}"
AUTHOR = "case_preview_a"
REVIEWER = "case_preview_r"

EXPECTED_PROPOSAL_KEYS = {
    "cardId",
    "identifier",
    "prKey",
    "reason",
    "mutation",
    "recommendedAction",
    "owner",
    "fingerprint",
    "note",
    "nextCheckAt",
}


def checks_at(head, conclusions=("success", "success", "success")):
    return [
        {"name": name, "sha": head, "conclusion": conclusion}
        for name, conclusion in zip(REQUIRED, conclusions)
    ]


def reg_entry(**changes):
    base = {
        "repo": REPO,
        "pr": PR,
        "admission": "admitted",
        "kind": "standard",
        "authorCard": AUTHOR,
        "successorCard": AUTHOR,
        "reviewerCard": REVIEWER,
    }
    base.update(changes)
    return base


def snap_doc(head=HEAD_A, **changes):
    base = {
        "repo": REPO,
        "pr": PR,
        "headSha": head,
        "mergeableState": "clean",
        "merged": False,
        "draft": False,
        "releasePlease": False,
        "checks": checks_at(head),
        "requiredChecks": list(REQUIRED),
        "companyIndependentReviewRequired": True,
        "companySecurityReviewRequired": False,
    }
    base.update(changes)
    return base


def review_approve(head=HEAD_A):
    return {"verdict": "APPROVE", "sha": head}


def card_doc(identifier=AUTHOR, **changes):
    base = {
        "id": "00000000-0000-4000-8000-000000000179",
        "identifier": identifier,
        "status": "in_progress",
        "ownerCard": identifier,
        "runs": [],
    }
    base.update(changes)
    return base


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)
        handle.write("\n")


def stamp_for(moment):
    return moment.isoformat().replace("+00:00", "Z")


def run_cli(argv, now=NOW):
    """Run ledger.main in-process with a controlled clock.

    Returns (returncode_or_None, stdout, stderr, system_exit_code_or_None).
    SystemExit carries argparse usage exits (e.g. missing --dry-run).
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with mock.patch.object(ledger, "utcnow", return_value=now):
        try:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(
                stderr_buf
            ):
                code = ledger.main(list(argv))
            return code, stdout_buf.getvalue(), stderr_buf.getvalue(), None
        except SystemExit as exited:
            return None, stdout_buf.getvalue(), stderr_buf.getvalue(), exited.code


def run_cli_expect_success(testcase, argv, now=NOW):
    code, out, err, exit_code = run_cli(argv, now=now)
    if exit_code is not None:
        testcase.fail(
            f"CLI exited via SystemExit({exit_code}) instead of returning; "
            f"missing --dry-run is the baseline RED. stderr={err!r:.500}"
        )
    return code, out, err


def parse_single_json(testcase, stdout):
    stripped = stdout.strip()
    testcase.assertTrue(stripped, "stdout must hold exactly one JSON plan")
    testcase.assertNotIn("NaN", stripped, "plan must be finite JSON")
    testcase.assertNotIn("Infinity", stripped, "plan must be finite JSON")
    try:
        plan = json.loads(stripped)
    except ValueError as error:
        testcase.fail(f"stdout is not a single JSON document: {error}; out={stripped!r:.800}")
    testcase.assertIsInstance(plan, dict, "top-level JSON plan must be an object")
    return plan


def mk_proposals_inputs(work):
    """UNKNOWN_CHECKS record with no live cards -> one owner_unmapped proposal."""
    registry_path = os.path.join(work, "registry.json")
    snapshot_path = os.path.join(work, "snapshot.json")
    write_json(registry_path, {"entries": [reg_entry()]})
    write_json(
        snapshot_path,
        {
            "snapshots": {PR_KEY: snap_doc(checks=None)},
            "reviews": {PR_KEY: review_approve()},
            "cards": {},
        },
    )
    return registry_path, snapshot_path


def mk_silent_inputs(work):
    """APPROVED_WAIT_CI record with a healthy reviewer monitor -> no proposals."""
    registry_path = os.path.join(work, "registry.json")
    snapshot_path = os.path.join(work, "snapshot.json")
    write_json(registry_path, {"entries": [reg_entry()]})
    write_json(
        snapshot_path,
        {
            "snapshots": {PR_KEY: snap_doc()},
            "reviews": {PR_KEY: review_approve()},
            "cards": {
                REVIEWER: card_doc(
                    REVIEWER,
                    monitor={
                        "nextCheckAt": "2026-10-03T00:00:00Z",
                        "attemptsLeft": 2,
                        "maxAttempts": 3,
                    },
                )
            },
        },
    )
    return registry_path, snapshot_path


def mk_prior_path(work):
    prior_path = os.path.join(work, "prior.json")
    write_json(
        prior_path,
        {
            "records": {
                PR_KEY: {
                    "headSha": HEAD_B,
                    "checkState": "GREEN",
                    "verdict": "APPROVE",
                    "verdictSha": HEAD_B,
                    "lastProgressAt": "2026-10-01T00:00:00Z",
                    "history": [],
                }
            }
        },
    )
    return prior_path


def mk_extra_cards_path(work):
    cards_path = os.path.join(work, "extra_cards.json")
    write_json(cards_path, {"cards": {AUTHOR: card_doc(AUTHOR)}})
    return cards_path


class WriteRecorder:
    """Snapshot directory entries + fixture bytes; record write-mode opens."""

    def __init__(self, root):
        self.root = root
        self.write_opens = []
        self._orig_open = open
        self._patcher = None
        self.before_entries = set()
        self.before_bytes = {}
        self.after_entries = set()
        self.after_bytes = {}

    def _snapshot(self):
        entries = set()
        for base, dirs, files in os.walk(self.root):
            for name in list(dirs) + list(files):
                entries.add(os.path.relpath(os.path.join(base, name), self.root))
        payloads = {}
        for rel in entries:
            full = os.path.join(self.root, rel)
            if os.path.isfile(full):
                with self._orig_open(full, "rb") as handle:
                    payloads[rel] = handle.read()
        return entries, payloads

    def __enter__(self):
        self.before_entries, self.before_bytes = self._snapshot()
        orig = self._orig_open
        recorder = self

        def wrapped(*args, **kwargs):
            mode = "r"
            if len(args) >= 2 and isinstance(args[1], str):
                mode = args[1]
            elif isinstance(kwargs.get("mode"), str):
                mode = kwargs["mode"]
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                try:
                    shown = str(args[0]) if args else "?"
                except Exception:
                    shown = "?"
                recorder.write_opens.append((shown, mode))
            return orig(*args, **kwargs)

        self._patcher = mock.patch("builtins.open", side_effect=wrapped)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        if self._patcher is not None:
            self._patcher.stop()
        self.after_entries, self.after_bytes = self._snapshot()
        return False


class DryRunHelp(unittest.TestCase):
    def test_help_names_dry_run_and_nonpersistence(self):
        code, out, err, exit_code = run_cli(["--help"])
        self.assertEqual(exit_code, 0, f"--help must exit 0; stderr={err!r:.400}")
        text = out.lower()
        self.assertIn("--dry-run", text, "help must document the explicit --dry-run flag")
        for token in ("history", "deadline", "diagnostic"):
            self.assertIn(token, text, f"help must say preview {token} wording")
        self.assertIn("not persist", text, "help must say preview was not persisted")


class DryRunJsonProposals(unittest.TestCase):
    def test_json_preview_without_outputs_exits_action_due(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            with WriteRecorder(work) as recorder:
                code, out, err = run_cli_expect_success(
                    self,
                    [
                        "--registry", registry_path,
                        "--snapshot", snapshot_path,
                        "--dry-run", "--json",
                    ],
                )
            self.assertEqual(code, 1, f"proposals preview exits 1; out={out!r:.600} err={err!r:.400}")
            plan = parse_single_json(self, out)
            self.assertEqual(plan.get("generatedAt"), stamp_for(NOW))
            self.assertIsInstance(plan.get("proposals"), list)
            self.assertEqual(len(plan["proposals"]), 1)
            proposal = plan["proposals"][0]
            self.assertEqual(proposal.get("prKey"), PR_KEY)
            self.assertEqual(proposal.get("reason"), "owner_unmapped")
            self.assertEqual(proposal.get("mutation"), "none")
            self.assertTrue(proposal.get("recommendedAction"))
            self.assertEqual(plan.get("dry_run"), True)
            self.assertEqual(plan.get("persisted"), False)
            self.assertEqual(recorder.write_opens, [])
            self.assertEqual(recorder.after_entries, recorder.before_entries)
            self.assertEqual(recorder.after_bytes, recorder.before_bytes)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_json_preview_is_deterministic(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            argv = [
                "--registry", registry_path,
                "--snapshot", snapshot_path,
                "--dry-run", "--json",
            ]
            code_a, out_a, _ = run_cli_expect_success(self, argv)
            code_b, out_b, _ = run_cli_expect_success(self, argv)
            self.assertEqual(code_a, code_b)
            self.assertEqual(out_a, out_b, "same evidence/time must render byte-identical JSON")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunTextProposals(unittest.TestCase):
    def test_text_preview_says_not_persisted_and_diagnostic(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            with WriteRecorder(work) as recorder:
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path, "--dry-run"],
                )
            self.assertEqual(code, 1)
            text = f"{out}\n{err}".lower()
            for token in ("history", "deadline", "diagnostic"):
                self.assertIn(token, text, f"text preview must name {token}")
            self.assertIn("not persist", text, "text preview must say not persisted")
            self.assertIn(PR_KEY, out, "text preview keeps the stable repo#pr identifier")
            self.assertIn("owner_unmapped", out, "text preview keeps the proposal reason")
            self.assertEqual(recorder.write_opens, [])
            self.assertEqual(recorder.after_entries, recorder.before_entries)
            self.assertEqual(recorder.after_bytes, recorder.before_bytes)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunNoProposals(unittest.TestCase):
    def test_json_preview_without_proposals_exits_zero(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_silent_inputs(work)
            code, out, _ = run_cli_expect_success(
                self,
                ["--registry", registry_path, "--snapshot", snapshot_path,
                 "--dry-run", "--json"],
            )
            self.assertEqual(code, 0, f"silent preview exits 0; out={out!r:.600}")
            plan = parse_single_json(self, out)
            self.assertEqual(plan.get("proposals"), [])
            self.assertEqual(plan.get("dry_run"), True)
            self.assertEqual(plan.get("persisted"), False)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_text_preview_without_proposals_exits_zero(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_silent_inputs(work)
            code, out, err = run_cli_expect_success(
                self,
                ["--registry", registry_path, "--snapshot", snapshot_path, "--dry-run"],
            )
            self.assertEqual(code, 0)
            text = f"{out}\n{err}".lower()
            self.assertIn("not persist", text)
            self.assertIn("diagnostic", text)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunUsageConflict(unittest.TestCase):
    def test_dry_run_with_output_paths_is_rejected_without_writes(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            with WriteRecorder(work) as recorder:
                code, out, err, exit_code = run_cli(
                    [
                        "--registry", registry_path,
                        "--snapshot", snapshot_path,
                        "--dry-run",
                        "--ledger-out", ledger_out,
                        "--plan-out", plan_out,
                    ]
                )
            effective = exit_code if exit_code is not None else code
            self.assertEqual(effective, 2, f"dry-run plus outputs is usage error 2; out={out!r:.400} err={err!r:.400}")
            text = f"{out}\n{err}".lower()
            self.assertIn("dry-run", text, "conflict message must name --dry-run")
            self.assertTrue(
                ("conflict" in text) or ("output" in text),
                f"conflict message must name outputs/conflict; got {text!r:.500}",
            )
            self.assertEqual(recorder.write_opens, [])
            self.assertFalse(os.path.exists(ledger_out))
            self.assertFalse(os.path.exists(plan_out))
            self.assertEqual(recorder.after_entries, recorder.before_entries)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_dry_run_with_single_output_path_is_rejected(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            with WriteRecorder(work):
                code, out, err, exit_code = run_cli(
                    [
                        "--registry", registry_path,
                        "--snapshot", snapshot_path,
                        "--dry-run", "--json",
                        "--ledger-out", ledger_out,
                    ]
                )
            effective = exit_code if exit_code is not None else code
            self.assertEqual(effective, 2)
            text = f"{out}\n{err}".lower()
            self.assertIn("dry-run", text)
            self.assertTrue(
                ("conflict" in text) or ("output" in text),
                f"single-output conflict must name outputs/conflict; got {text!r:.500}",
            )
            self.assertFalse(os.path.exists(ledger_out))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunParity(unittest.TestCase):
    def test_preview_matches_persisted_proposals_same_clock(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            preview_code, preview_out, _ = run_cli_expect_success(
                self,
                ["--registry", registry_path, "--snapshot", snapshot_path,
                 "--dry-run", "--json"],
            )
            persisted_code, persisted_out, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(preview_code, persisted_code)
            self.assertEqual(preview_code, 1)
            preview = parse_single_json(self, preview_out)
            persisted_stdout = parse_single_json(self, persisted_out)
            with open(plan_out, encoding="utf-8") as handle:
                persisted_file = json.load(handle)
            self.assertEqual(persisted_stdout["proposals"], persisted_file["proposals"])
            self.assertEqual(preview["generatedAt"], persisted_file["generatedAt"])
            self.assertEqual(preview["proposals"], persisted_file["proposals"])
            cleaned = dict(preview)
            cleaned.pop("dry_run", None)
            cleaned.pop("persisted", None)
            self.assertEqual(cleaned, persisted_file)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_preview_preserves_identifiers_reasons_actions(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            _, preview_out, _ = run_cli_expect_success(
                self,
                ["--registry", registry_path, "--snapshot", snapshot_path,
                 "--dry-run", "--json"],
            )
            plan = parse_single_json(self, preview_out)
            self.assertEqual(len(plan["proposals"]), 1)
            proposal = plan["proposals"][0]
            self.assertEqual(set(proposal.keys()), EXPECTED_PROPOSAL_KEYS)
            self.assertEqual(proposal["prKey"], PR_KEY)
            self.assertEqual(proposal["reason"], "owner_unmapped")
            self.assertEqual(proposal["mutation"], "none")
            self.assertTrue(proposal["recommendedAction"])
            self.assertTrue(proposal["owner"])
            lowered = json.dumps(proposal).lower()
            for forbidden in ("executed", "applied", "authority", "actiontaken"):
                self.assertNotIn(forbidden, lowered)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_preview_matches_persisted_with_prior_and_cards(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            prior_path = mk_prior_path(work)
            extra_cards = mk_extra_cards_path(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            preview_code, preview_out, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--prior-ledger", prior_path, "--cards", extra_cards,
                    "--dry-run", "--json",
                ],
            )
            persisted_code, _, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--prior-ledger", prior_path, "--cards", extra_cards,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(preview_code, persisted_code)
            preview = parse_single_json(self, preview_out)
            with open(plan_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(preview["proposals"], persisted["proposals"])
            self.assertEqual(preview["generatedAt"], persisted["generatedAt"])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunSamePathAndClock(unittest.TestCase):
    def test_preview_calls_same_evaluate_and_plan(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            calls = {"evaluate": 0, "plan": 0}
            orig_evaluate = ledger.evaluate
            orig_plan = ledger.plan_proposals

            def counting_evaluate(*args, **kwargs):
                calls["evaluate"] += 1
                return orig_evaluate(*args, **kwargs)

            def counting_plan(*args, **kwargs):
                calls["plan"] += 1
                return orig_plan(*args, **kwargs)

            with mock.patch.object(ledger, "evaluate", side_effect=counting_evaluate), mock.patch.object(
                ledger, "plan_proposals", side_effect=counting_plan
            ):
                _, out, _ = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self.assertGreaterEqual(calls["evaluate"], 1, "preview must use evaluate()")
            self.assertGreaterEqual(calls["plan"], 1, "preview must use plan_proposals()")
            plan = parse_single_json(self, out)
            self.assertEqual(len(plan["proposals"]), 1)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_preview_uses_caller_clock(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            argv = ["--registry", registry_path, "--snapshot", snapshot_path,
                   "--dry-run", "--json"]
            _, out_a, _ = run_cli_expect_success(self, argv, now=NOW)
            _, out_b, _ = run_cli_expect_success(self, argv, now=NOW_LATER)
            plan_a = parse_single_json(self, out_a)
            plan_b = parse_single_json(self, out_b)
            self.assertEqual(plan_a["generatedAt"], stamp_for(NOW))
            self.assertEqual(plan_b["generatedAt"], stamp_for(NOW_LATER))
            self.assertNotEqual(plan_a["generatedAt"], plan_b["generatedAt"])
            self.assertEqual(plan_a["proposals"], plan_b["proposals"])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunNoWrites(unittest.TestCase):
    def test_preview_creates_modifies_nothing(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            prior_path = mk_prior_path(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            nested_out = os.path.join(work, "nested", "plan.json")
            with WriteRecorder(work) as recorder:
                code, out, _ = run_cli_expect_success(
                    self,
                    [
                        "--registry", registry_path,
                        "--snapshot", snapshot_path,
                        "--prior-ledger", prior_path,
                        "--dry-run", "--json",
                    ],
                )
            self.assertEqual(code, 1)
            parse_single_json(self, out)
            self.assertEqual(recorder.write_opens, [], f"preview opened for write: {recorder.write_opens!r}")
            self.assertEqual(recorder.after_entries, recorder.before_entries)
            self.assertEqual(recorder.after_bytes, recorder.before_bytes)
            self.assertFalse(os.path.exists(ledger_out))
            self.assertFalse(os.path.exists(plan_out))
            self.assertFalse(os.path.exists(nested_out))
            self.assertFalse(os.path.exists(os.path.join(work, "nested")))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_preview_leaves_normal_artifacts_untouched(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            persisted_code, _, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(persisted_code, 1)
            with open(ledger_out, "rb") as handle:
                ledger_before = handle.read()
            with open(plan_out, "rb") as handle:
                plan_before = handle.read()
            with WriteRecorder(work) as recorder:
                preview_code, _, _ = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self.assertEqual(preview_code, 1)
            with open(ledger_out, "rb") as handle:
                self.assertEqual(handle.read(), ledger_before)
            with open(plan_out, "rb") as handle:
                self.assertEqual(handle.read(), plan_before)
            self.assertEqual(recorder.write_opens, [])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunOptionalInputs(unittest.TestCase):
    def test_preview_without_optionals_matches_normal_without_optionals(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            preview_code, preview_out, _ = run_cli_expect_success(
                self,
                ["--registry", registry_path, "--snapshot", snapshot_path,
                 "--dry-run", "--json"],
            )
            persisted_code, _, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(preview_code, persisted_code)
            preview = parse_single_json(self, preview_out)
            with open(plan_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(preview["proposals"], persisted["proposals"])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_preview_accepts_prior_and_cards(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_silent_inputs(work)
            prior_path = mk_prior_path(work)
            extra_cards = mk_extra_cards_path(work)
            with WriteRecorder(work) as recorder:
                code, out, _ = run_cli_expect_success(
                    self,
                    [
                        "--registry", registry_path,
                        "--snapshot", snapshot_path,
                        "--prior-ledger", prior_path,
                        "--cards", extra_cards,
                        "--dry-run", "--json",
                    ],
                )
            plan = parse_single_json(self, out)
            self.assertIn(code, (0, 1))
            self.assertEqual(plan.get("dry_run"), True)
            self.assertEqual(plan.get("persisted"), False)
            self.assertEqual(recorder.write_opens, [])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunErrors(unittest.TestCase):
    def _assert_safe_json_error(self, code, out, err, secret=""):
        self.assertEqual(code, 5, f"unmeasurable preview exits 5; out={out!r:.600} err={err!r:.400}")
        plan = parse_single_json(self, out)
        self.assertEqual(plan.get("dry_run"), True)
        self.assertEqual(plan.get("persisted"), False)
        error = plan.get("error")
        self.assertIsInstance(error, dict, "JSON failure must carry an error object")
        self.assertTrue(error.get("code"), "error.code must be a stable non-empty string")
        self.assertIsInstance(error.get("retryable"), bool, "error.retryable must be boolean")
        next_action = error.get("next_action", error.get("nextAction"))
        self.assertTrue(next_action, "error must carry next_action")
        combined = f"{out}\n{err}"
        self.assertNotIn("Traceback", combined)
        if secret:
            self.assertNotIn(secret, combined, "safe error must not echo raw input")
        self.assertFalse(
            plan.get("proposals") == [] and code == 0,
            "missing/malformed evidence must not become a clean empty plan",
        )

    def test_missing_registry_is_safe_error_not_clean(self):
        work = tempfile.mkdtemp()
        try:
            _, snapshot_path = mk_proposals_inputs(work)
            missing = os.path.join(work, "absent-registry.json")
            with WriteRecorder(work) as recorder:
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", missing, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self._assert_safe_json_error(code, out, err)
            self.assertEqual(recorder.write_opens, [])
            self.assertEqual(recorder.after_entries, recorder.before_entries)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_malformed_registry_is_safe_error_without_raw_echo(self):
        work = tempfile.mkdtemp()
        secret = "SECRET_MARKER_9f3a7c_preview"
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            with open(registry_path, "w", encoding="utf-8") as handle:
                handle.write("{not json " + secret)
            with WriteRecorder(work) as recorder:
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self._assert_safe_json_error(code, out, err, secret=secret)
            self.assertEqual(recorder.write_opens, [])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_empty_snapshot_refuses_clean_in_preview(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, _ = mk_proposals_inputs(work)
            empty_snapshot = os.path.join(work, "empty_snapshot.json")
            write_json(empty_snapshot, {"snapshots": {}, "reviews": {}, "cards": {}})
            with WriteRecorder(work):
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", empty_snapshot,
                     "--dry-run", "--json"],
                )
            self._assert_safe_json_error(code, out, err)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_unknown_admission_refuses_in_preview(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            write_json(registry_path, {"entries": [reg_entry(admission="auto")]})
            with WriteRecorder(work):
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self._assert_safe_json_error(code, out, err)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_text_preview_error_stays_unmeasurable(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, _ = mk_proposals_inputs(work)
            empty_snapshot = os.path.join(work, "empty_snapshot.json")
            write_json(empty_snapshot, {"snapshots": {}, "reviews": {}, "cards": {}})
            with WriteRecorder(work) as recorder:
                code, out, err = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", empty_snapshot, "--dry-run"],
                )
            self.assertEqual(code, 5)
            self.assertIn("refusing to report a clean board", f"{out}\n{err}")
            self.assertEqual(recorder.write_opens, [])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunOffline(unittest.TestCase):
    def test_preview_runs_without_network_or_subprocess(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")), mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess forbidden")
            ):
                code, out, _ = run_cli_expect_success(
                    self,
                    ["--registry", registry_path, "--snapshot", snapshot_path,
                     "--dry-run", "--json"],
                )
            self.assertEqual(code, 1)
            parse_single_json(self, out)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_tool_imports_stay_offline_only(self):
        import ast

        with open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_closeout_ledger.py"),
            encoding="utf-8",
        ) as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("socket", "urllib", "requests", "httpx", "subprocess"):
            self.assertNotIn(forbidden, imported, f"ledger must not import {forbidden}")


class NormalModePreserved(unittest.TestCase):
    """Positive controls: these pass on the baseline and must keep passing."""

    def test_normal_mode_still_requires_output_paths(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            code, _, err, exit_code = run_cli(
                ["--registry", registry_path, "--snapshot", snapshot_path]
            )
            self.assertIsNone(code)
            self.assertEqual(exit_code, 2)
            self.assertIn("ledger-out", err)
            self.assertIn("plan-out", err)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_normal_mode_persists_proposals_exit_one(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            code, out, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(code, 1)
            for path in (ledger_out, plan_out):
                self.assertTrue(os.path.isfile(path), f"normal mode must write {path}")
            with open(plan_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(persisted["generatedAt"], stamp_for(NOW))
            self.assertEqual(len(persisted["proposals"]), 1)
            self.assertEqual(persisted["proposals"][0]["prKey"], PR_KEY)
            self.assertEqual(persisted["proposals"][0]["reason"], "owner_unmapped")
            self.assertNotIn("dry_run", persisted)
            self.assertNotIn("persisted", persisted)
            stdout_plan = parse_single_json(self, out)
            self.assertEqual(stdout_plan, persisted)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_normal_mode_silent_exit_zero(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_silent_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            code, _, _ = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", snapshot_path,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                    "--json",
                ],
            )
            self.assertEqual(code, 0)
            with open(plan_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(persisted["proposals"], [])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_normal_mode_unmeasurable_stays_exit_five(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, _ = mk_proposals_inputs(work)
            empty_snapshot = os.path.join(work, "empty_snapshot.json")
            write_json(empty_snapshot, {"snapshots": {}, "reviews": {}, "cards": {}})
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            code, _, err = run_cli_expect_success(
                self,
                [
                    "--registry", registry_path, "--snapshot", empty_snapshot,
                    "--ledger-out", ledger_out, "--plan-out", plan_out,
                ],
            )
            self.assertEqual(code, 5)
            self.assertIn("refusing to report a clean board", err)
            self.assertFalse(os.path.exists(ledger_out))
            self.assertFalse(os.path.exists(plan_out))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_normal_mode_offline_without_network(self):
        work = tempfile.mkdtemp()
        try:
            registry_path, snapshot_path = mk_proposals_inputs(work)
            ledger_out = os.path.join(work, "ledger.json")
            plan_out = os.path.join(work, "plan.json")
            with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")), mock.patch(
                "subprocess.run", side_effect=AssertionError("subprocess forbidden")
            ):
                code, _, _ = run_cli_expect_success(
                    self,
                    [
                        "--registry", registry_path, "--snapshot", snapshot_path,
                        "--ledger-out", ledger_out, "--plan-out", plan_out,
                    ],
                )
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
