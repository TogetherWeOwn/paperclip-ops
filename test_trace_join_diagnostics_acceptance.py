#!/usr/bin/env python3
"""Immutable RED acceptance contract: locate rejected/orphaned trace handoffs.

Run from the repository root:
    python3 -B -m unittest -v test_trace_correlator \
        test_trace_join_diagnostics_acceptance

Hermetic and fixture-only: synthetic DEMO rows, a fixed checked_at for the
in-process controls, subprocess stdin only, no network, no board, no live
reads, and no input mutation. This file is the test author's handoff: a
builder implements the feature to satisfy it and must not edit this file.

CONTRACT PINNED HERE (the builder's spec)
=========================================

Default behaviour is untouched:
- ``correlate(evidence, checked_at)`` (two arguments) and a bare CLI run emit
  exactly the current ``trace-correlation.v1`` report, with no diagnostics
  section, and keep the current valid-input 0/1 exits, text exit-2 input
  refusals, input limits and every trace/SLO/delivery calculation.

Opt-in explanation (CLI only; no new in-process API is required):
- ``--explain-joins`` adds exactly one top-level report key,
  ``joinDiagnostics``, to the otherwise identical report. Its exact keys are
  ``{schema, inputDigest, limit, total, returned, truncated, complete,
  diagnostics}`` where ``schema`` is ``"trace-join-diagnostics.v1"``,
  ``limit`` is the effective diagnostic limit, ``total`` counts every
  diagnostic before limiting, ``returned == len(diagnostics)``,
  ``truncated`` is the boolean ``returned < total`` and ``complete`` is the
  boolean ``returned == total``. A truncated list is therefore never
  labelled complete.
- ``inputDigest`` is the lowercase hex SHA-256 of the exact raw stdin bytes
  supplied, so every row pointer is bound to one input snapshot a consumer
  can re-fingerprint.
- Each entry in ``diagnostics`` has exactly the keys ``{collection, index,
  code, retryable, next_action}``: ``collection`` is ``"runs"``,
  ``"workProducts"`` or ``"deployments"``; ``index`` is the zero-based row
  index in that input collection; ``code`` is a stable snake-case reason;
  ``retryable`` is ``false``; ``next_action`` is a nonempty single-line
  string of at most 400 characters. At most one diagnostic per input row.
  New fields carry only row pointers, controlled codes, that bounded
  guidance and the digest: never raw rows, note bodies, URLs, arbitrary
  identifiers, input paths, or parser exception text. (Validated
  identities already present in the compatible default report -- run/card
  ids, repo, SHAs in traces -- are outside this disclosure rule.)
- Ordering is stable input order regardless of which internal phase
  discovers a row: runs first, then workProducts, then deployments, each by
  ascending index. A limited run returns the exact prefix of that list.
- Normalization/duplicate codes reuse the correlator's existing reasons
  (``run_not_an_object``, ``run_missing_id``, ``run_missing_card``,
  ``run_duplicate_id``, ``work_product_not_an_object``,
  ``link_missing_card``, ``link_missing_run``, ``link_bad_repo``,
  ``link_bad_pr``, ``link_head_not_exact_sha``,
  ``link_merge_not_exact_sha``, ``deploy_not_an_object``,
  ``deploy_bad_repo``, ``deploy_sha_not_exact``, ``deploy_bad_id``),
  derived from the same normalization/join decisions as ``delivery``.
- Valid explicit PR links that cannot join a run are distinguished:
  ``link_orphan_missing_run`` (their run is absent from valid runs) vs
  ``link_orphan_card_mismatch`` (their run exists under a different card).
  Legitimately ignored non-PR work products (no explicit repo/pr) stay
  ignored and produce no diagnostic. Valid rows that merely leave trace
  gaps (missing timestamps, missing headSha) also produce no diagnostic:
  diagnostics locate rejected/orphaned/unselected input rows only.
- Valid deployments selected by no trace are distinguished without
  guessing temporal or production causation: ``deploy_not_selected`` when
  the deployment is an exact repo+head/merge-SHA match for at least one
  joined PR link (a link attached to a trace) but another candidate won
  the existing merge-before-head, earliest-deployment, file-order
  selection; otherwise ``deploy_no_eligible_match`` -- including wrong
  repo/SHA and deployments whose only matching link is invalid or an
  orphan, so no joined link was ever eligible to select them. Exact
  repo/full-SHA matching and every existing selection rule are unchanged.
- ``--diagnostic-limit`` takes an integer 1..1000 (default 100) and is
  valid only with ``--explain-joins``. Changing only the limit never
  changes traces, SLO, delivery, or the exit status, and a diagnostic can
  never repair input, fetch evidence, admit work, or turn the advisory
  SLO exit into a gate.
- ``--dry-run`` is a documented no-op for this already read-only tool: it
  is accepted alone or with ``--explain-joins`` and changes nothing. It
  does not substitute for ``--explain-joins``.
- ``--help``/``-h`` exits 0 without reading stdin and documents
  ``--explain-joins``, ``--diagnostic-limit`` and ``--dry-run``. The same
  flags are documented in ``docs/trace-correlation.md``.
- Requested-mode refusals: when any diagnostics flag (``--explain-joins``,
  ``--diagnostic-limit`` or ``--dry-run``) is present, an input or usage
  refusal exits 2 with a single pure-JSON object on stdout -- exactly
  ``{"schema": "trace-join-diagnostics.v1", "error": {"code": ...,
  "retryable": false, "next_action": ...}}`` -- and nothing on stderr:
  never a fabricated successful explanation. Codes: ``invalid_input`` for
  unreadable/invalid/oversize stdin, ``invalid_diagnostic_limit`` for a
  malformed or out-of-range limit value,
  ``diagnostic_limit_requires_explain_joins`` for a limit without
  ``--explain-joins``, ``invalid_usage`` for other bad arguments. With no
  diagnostics flag, refusals keep today's form (exit 2, empty stdout,
  text on stderr), including for an unknown argument.
"""

import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ops" / "paperclip"))

from trace_correlator import correlate  # noqa: E402

AT = "2026-10-25T07:30:00Z"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
ENTRYPOINT = ROOT / "ops" / "paperclip" / "trace_correlator.py"
SECTION_KEYS = {
    "schema", "inputDigest", "limit", "total", "returned",
    "truncated", "complete", "diagnostics",
}
ROW_KEYS = {"collection", "index", "code", "retryable", "next_action"}
COLLECTIONS = ("runs", "workProducts", "deployments")
CODE_RE = re.compile(r"^[a-z0-9_]+$")
INVALID_CODES = {
    "run_not_an_object", "run_missing_id", "run_missing_card",
    "run_duplicate_id", "work_product_not_an_object", "link_missing_card",
    "link_missing_run", "link_bad_repo", "link_bad_pr",
    "link_head_not_exact_sha", "link_merge_not_exact_sha",
    "deploy_not_an_object", "deploy_bad_repo", "deploy_sha_not_exact",
    "deploy_bad_id",
}
ORPHAN_LINK_CODES = {"link_orphan_missing_run", "link_orphan_card_mismatch"}
UNSELECTED_DEPLOY_CODES = {"deploy_not_selected", "deploy_no_eligible_match"}


def run_row(run="run-1", card="DEMO-1", created="2026-10-20T10:00:00Z",
            started="2026-10-20T10:02:00Z", finished="2026-10-20T10:07:00Z"):
    return {
        "runId": run,
        "contextIssueId": card,
        "createdAt": created,
        "startedAt": started,
        "finishedAt": finished,
    }


def link_row(card="DEMO-1", run="run-1", repo="demo-org/demo-repo", pr=7,
             head=SHA_A, merge=None):
    row = {
        "issueId": card,
        "createdByRunId": run,
        "type": "pull_request",
        "repo": repo,
        "pr": pr,
        "headSha": head,
    }
    if merge is not None:
        row["mergeSha"] = merge
    return row


def deploy_row(repo="demo-org/demo-repo", sha=SHA_A,
               deploy="2026.10.20.1+aaaaaaa",
               merged="2026-10-20T10:07:00Z", deployed="2026-10-20T10:27:00Z"):
    return {
        "repo": repo,
        "sha": sha,
        "deployId": deploy,
        "mergedAt": merged,
        "deployedAt": deployed,
    }


def doc(runs=None, work_products=None, deployments=None):
    return {
        "schema_version": 1,
        "runs": runs if runs is not None else [],
        "workProducts": work_products if work_products is not None else [],
        "deployments": deployments if deployments is not None else [],
    }


def raw_of(document):
    return json.dumps(document).encode()


def cli(raw, *args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-B", str(ENTRYPOINT), *args],
        input=raw,
        capture_output=True,
        cwd=cwd,
        timeout=30,
    )


def canonical(report):
    """Report stripped of diagnostics and the volatile checked-at stamps."""
    stripped = copy.deepcopy(report)
    stripped.pop("joinDiagnostics", None)
    for key in ("slo", "delivery"):
        if isinstance(stripped.get(key), dict):
            stripped[key].pop("at", None)
    return stripped


def mixed_doc():
    """One fixture producing one diagnostic of each broad class (7 total)."""
    return doc(
        runs=[
            run_row(),                                              # 0 valid
            "not-an-object",                                        # 1
            run_row(run="run-2", card="DEMO-2"),                    # 2 valid
            run_row(),                                              # 3 dup id
        ],
        work_products=[
            link_row(),                                             # 0 joined
            {"issueId": "DEMO-1", "type": "note"},                  # 1 ignored
            link_row(head="main"),                                  # 2 invalid
            link_row(run="run-ghost", head=SHA_B),                  # 3 orphan
        ],
        deployments=[
            deploy_row(),                                           # 0 selected
            deploy_row(deploy="2026.10.20.2+aaaaaaa",
                       deployed="2026-10-20T11:00:00Z"),           # 1 late dup
            deploy_row(sha=SHA_C),                                  # 2 no match
            deploy_row(sha="short"),                                # 3 invalid
        ],
    )


MIXED_EXPECTED = [
    ("runs", 1, "run_not_an_object"),
    ("runs", 3, "run_duplicate_id"),
    ("workProducts", 2, "link_head_not_exact_sha"),
    ("workProducts", 3, "link_orphan_missing_run"),
    ("deployments", 1, "deploy_not_selected"),
    ("deployments", 2, "deploy_no_eligible_match"),
    ("deployments", 3, "deploy_sha_not_exact"),
]


class DiagnosticsTestCase(unittest.TestCase):
    maxDiff = None

    def explain(self, raw, *extra_args):
        done = cli(raw, "--explain-joins", *extra_args)
        self.assertIn(done.returncode, (0, 1), done.stderr)
        self.assertEqual(done.stderr, b"")
        return done, json.loads(done.stdout)

    def diagnostics(self, report):
        self.assertIn("joinDiagnostics", report)
        section = report["joinDiagnostics"]
        self.assertEqual(set(section), SECTION_KEYS)
        return section

    def assert_well_formed_section(self, section):
        self.assertEqual(section["schema"], "trace-join-diagnostics.v1")
        self.assertRegex(section["inputDigest"], r"^[0-9a-f]{64}$")
        for key in ("limit", "total", "returned"):
            self.assertIs(type(section[key]), int, key)
            self.assertGreaterEqual(section[key], 0, key)
        rows = section["diagnostics"]
        self.assertIsInstance(rows, list)
        self.assertEqual(section["returned"], len(rows))
        self.assertEqual(section["truncated"], section["returned"] < section["total"])
        self.assertEqual(section["complete"], section["returned"] == section["total"])
        self.assertIs(type(section["truncated"]), bool)
        self.assertIs(type(section["complete"]), bool)
        pointers = []
        for row in rows:
            self.assertEqual(set(row), ROW_KEYS)
            self.assertIn(row["collection"], COLLECTIONS)
            self.assertIs(type(row["index"]), int)
            self.assertGreaterEqual(row["index"], 0)
            self.assertRegex(row["code"], CODE_RE)
            self.assertIs(row["retryable"], False)
            self.assertIsInstance(row["next_action"], str)
            self.assertTrue(row["next_action"].strip())
            self.assertLessEqual(len(row["next_action"]), 400)
            self.assertNotIn("\n", row["next_action"])
            pointers.append((COLLECTIONS.index(row["collection"]), row["index"]))
        self.assertEqual(len(pointers), len(set(pointers)),
                         "at most one diagnostic per input row")
        self.assertEqual(pointers, sorted(pointers),
                         "diagnostics must be in stable collection/index order")

    def triples(self, report):
        section = self.diagnostics(report)
        self.assert_well_formed_section(section)
        return [(row["collection"], row["index"], row["code"])
                for row in section["diagnostics"]]

    def assert_json_refusal(self, done, expected_code):
        self.assertEqual(done.returncode, 2,
                         (done.stdout, done.stderr))
        self.assertEqual(done.stderr, b"",
                         "requested-mode refusal must be a single JSON emission")
        payload = json.loads(done.stdout)  # pure JSON: nothing else on stdout
        self.assertEqual(set(payload), {"schema", "error"})
        self.assertEqual(payload["schema"], "trace-join-diagnostics.v1")
        self.assertNotIn("traces", payload)
        self.assertNotIn("joinDiagnostics", payload)
        error = payload["error"]
        self.assertEqual(set(error), {"code", "retryable", "next_action"})
        if isinstance(expected_code, str):
            self.assertEqual(error["code"], expected_code)
        else:
            self.assertIn(error["code"], expected_code)
        self.assertIs(error["retryable"], False)
        self.assertTrue(error["next_action"].strip())
        self.assertLessEqual(len(error["next_action"]), 400)
        return payload


class DefaultCompatibilityControl(DiagnosticsTestCase):
    """Positive controls: these describe current behaviour and pass today."""

    def test_two_argument_correlate_has_no_diagnostics_section(self):
        report = correlate(mixed_doc(), AT)
        self.assertEqual(report["schema"], "trace-correlation.v1")
        self.assertEqual(set(report), {"schema", "traces", "slo", "delivery"})

    def test_correlate_does_not_mutate_evidence(self):
        evidence = {
            "runs": [run_row(), run_row()],
            "workProducts": [link_row(head="main")],
            "deployments": [deploy_row(sha="short")],
        }
        before = copy.deepcopy(evidence)
        correlate(evidence, AT)
        self.assertEqual(evidence, before)

    def test_default_cli_report_shape_and_exits_unchanged(self):
        met = cli(raw_of(doc(runs=[run_row()], work_products=[link_row()],
                             deployments=[deploy_row()])))
        self.assertEqual(met.returncode, 0, met.stderr)
        report = json.loads(met.stdout)
        self.assertEqual(set(report), {"schema", "traces", "slo", "delivery"})
        self.assertEqual(report["slo"]["verdict"], "met")

        unknown = cli(raw_of(doc(runs=[run_row()])))
        self.assertEqual(unknown.returncode, 1, unknown.stderr)
        self.assertNotIn("joinDiagnostics", json.loads(unknown.stdout))

    def test_default_cli_bad_input_keeps_text_refusal(self):
        done = cli(b"{not json")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")
        self.assertTrue(done.stderr.strip())


class ExplainModeShape(DiagnosticsTestCase):
    def test_explain_adds_one_versioned_section_and_preserves_report(self):
        raw = raw_of(mixed_doc())
        default_done = cli(raw)
        explained_done, explained = self.explain(raw)
        self.assertEqual(explained_done.returncode, default_done.returncode)
        default = json.loads(default_done.stdout)
        self.assertEqual(set(explained),
                         {"schema", "traces", "slo", "delivery", "joinDiagnostics"})
        self.assertEqual(explained["schema"], "trace-correlation.v1")
        self.assertEqual(canonical(explained), canonical(default))
        section = self.diagnostics(explained)
        self.assert_well_formed_section(section)
        self.assertEqual(section["limit"], 100)
        self.assertEqual(section["total"], len(MIXED_EXPECTED))

    def test_clean_full_chain_explains_to_an_empty_complete_list(self):
        done, report = self.explain(raw_of(doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[deploy_row()])))
        self.assertEqual(done.returncode, 0)
        section = self.diagnostics(report)
        self.assert_well_formed_section(section)
        self.assertEqual(section["diagnostics"], [])
        self.assertEqual(section["total"], 0)
        self.assertFalse(section["truncated"])
        self.assertTrue(section["complete"])

    def test_input_digest_is_sha256_of_exact_stdin_bytes(self):
        raw = raw_of(mixed_doc())
        _, report = self.explain(raw)
        section = self.diagnostics(report)
        self.assertEqual(section["inputDigest"],
                         hashlib.sha256(raw).hexdigest())

        # Same evidence re-serialised is a different snapshot → new digest,
        # identical row pointers.
        respaced = json.dumps(mixed_doc(), indent=2).encode()
        _, report2 = self.explain(respaced)
        section2 = self.diagnostics(report2)
        self.assertEqual(section2["inputDigest"],
                         hashlib.sha256(respaced).hexdigest())
        self.assertNotEqual(section2["inputDigest"], section["inputDigest"])
        self.assertEqual(section2["diagnostics"], section["diagnostics"])

    def test_digest_changes_when_a_row_changes_and_is_stable_otherwise(self):
        raw = raw_of(doc(runs=[run_row()]))
        _, first = self.explain(raw)
        _, again = self.explain(raw)
        changed_raw = raw_of(doc(runs=[run_row(run="run-9")]))
        _, changed = self.explain(changed_raw)
        self.assertEqual(self.diagnostics(first), self.diagnostics(again))
        self.assertNotEqual(self.diagnostics(first)["inputDigest"],
                            self.diagnostics(changed)["inputDigest"])

    def test_explain_preserves_met_missed_unknown_exits(self):
        cases = [
            (doc(runs=[run_row()], work_products=[link_row()],
                 deployments=[deploy_row()]), 0, "met"),
            (doc(runs=[run_row()], work_products=[link_row()],
                 deployments=[deploy_row(deployed="2026-10-20T12:07:00Z")]),
             1, "missed"),
            (doc(runs=[run_row()]), 1, "unknown"),
        ]
        for document, exit_code, verdict in cases:
            with self.subTest(verdict=verdict):
                raw = raw_of(document)
                done, report = self.explain(raw)
                self.assertEqual(done.returncode, exit_code)
                self.assert_well_formed_section(self.diagnostics(report))
                self.assertEqual(report["slo"]["verdict"], verdict)
                self.assertEqual(canonical(report),
                                 canonical(json.loads(cli(raw).stdout)))

    def test_explanation_is_idempotent_across_runs(self):
        raw = raw_of(mixed_doc())
        _, first = self.explain(raw)
        _, second = self.explain(raw)
        self.assertEqual(self.diagnostics(first), self.diagnostics(second))
        self.assertEqual(first["traces"], second["traces"])

    def test_empty_document_explains_without_fabricated_rows(self):
        done, report = self.explain(raw_of(doc()))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(report["slo"]["verdict"], "unknown")
        self.assertEqual(report["slo"]["unknownReason"],
                         "zero_eligible_samples")
        section = self.diagnostics(report)
        self.assert_well_formed_section(section)
        self.assertEqual(section["total"], 0)
        self.assertTrue(section["complete"])


class InvalidAndDuplicateRows(DiagnosticsTestCase):
    def test_each_run_rejection_is_located(self):
        _, report = self.explain(raw_of(doc(runs=[
            "not-an-object",
            {"contextIssueId": "DEMO-1"},
            {"runId": "run-x"},
            run_row(),
            run_row(),
        ])))
        self.assertEqual(self.triples(report), [
            ("runs", 0, "run_not_an_object"),
            ("runs", 1, "run_missing_id"),
            ("runs", 2, "run_missing_card"),
            ("runs", 4, "run_duplicate_id"),
        ])
        self.assertEqual(report["delivery"]["invalidRows"], 4)
        self.assertEqual(len(report["traces"]), 1)

    def test_each_pr_link_rejection_is_located(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()],
            work_products=[
                "not-an-object",
                {"createdByRunId": "run-1",
                 "repo": "demo-org/demo-repo", "pr": 1},
                {"issueId": "DEMO-1",
                 "repo": "demo-org/demo-repo", "pr": 1},
                link_row(repo="bad"),
                link_row(pr=0),
                link_row(head="main"),
                link_row(merge="v1.2.3"),
            ],
        )))
        self.assertEqual(self.triples(report), [
            ("workProducts", 0, "work_product_not_an_object"),
            ("workProducts", 1, "link_missing_card"),
            ("workProducts", 2, "link_missing_run"),
            ("workProducts", 3, "link_bad_repo"),
            ("workProducts", 4, "link_bad_pr"),
            ("workProducts", 5, "link_head_not_exact_sha"),
            ("workProducts", 6, "link_merge_not_exact_sha"),
        ])
        self.assertEqual(report["delivery"]["invalidRows"], 7)

    def test_each_deployment_rejection_is_located(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[
                "not-an-object",
                {"repo": "bad", "sha": SHA_A},
                {"repo": "demo-org/demo-repo", "sha": "short"},
                deploy_row(deploy=""),
            ],
        )))
        self.assertEqual(self.triples(report), [
            ("deployments", 0, "deploy_not_an_object"),
            ("deployments", 1, "deploy_bad_repo"),
            ("deployments", 2, "deploy_sha_not_exact"),
            ("deployments", 3, "deploy_bad_id"),
        ])
        self.assertEqual(report["delivery"]["invalidRows"], 4)

    def test_diagnostics_agree_with_delivery_self_test_counts(self):
        """Same decisions, not a second algorithm: invalid diagnostics must
        reconcile with invalidRows/unknowns, orphan and unselected rows with
        the delivery orphan counters."""
        _, report = self.explain(raw_of(mixed_doc()))
        codes = [code for _, _, code in self.triples(report)]
        delivery = report["delivery"]
        invalid = [c for c in codes if c in INVALID_CODES]
        self.assertEqual(len(invalid), delivery["invalidRows"])
        self.assertEqual(set(invalid), set(delivery["unknowns"]))
        self.assertEqual(
            sum(1 for c in codes if c in ORPHAN_LINK_CODES),
            delivery["orphanWorkProducts"])
        self.assertEqual(
            sum(1 for c in codes if c in UNSELECTED_DEPLOY_CODES),
            delivery["orphanDeployments"])
        self.assertEqual(self.triples(report), MIXED_EXPECTED)


class OrphansAndUnselected(DiagnosticsTestCase):
    def test_orphan_missing_run_is_distinguished_from_card_mismatch(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()],
            work_products=[
                link_row(run="run-ghost"),
                link_row(card="DEMO-other", head=SHA_B),
            ],
        )))
        self.assertEqual(self.triples(report), [
            ("workProducts", 0, "link_orphan_missing_run"),
            ("workProducts", 1, "link_orphan_card_mismatch"),
        ])
        self.assertEqual(report["delivery"]["orphanWorkProducts"], 2)
        for code in ORPHAN_LINK_CODES:
            self.assertNotIn(code, report["delivery"]["unknowns"],
                             "orphans are not invalid rows")

    def test_ignored_non_pr_work_products_produce_no_diagnostic(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()],
            work_products=[
                {},
                {"issueId": "DEMO-1", "type": "comment"},
                {"issueId": "DEMO-1", "createdByRunId": "run-1",
                 "type": "pull_request"},  # type alone is never linkage
            ],
        )))
        self.assertEqual(self.triples(report), [])
        self.assertEqual(report["delivery"]["invalidRows"], 0)
        self.assertEqual(report["delivery"]["prLinksExplicit"], 0)

    def test_valid_partial_rows_produce_no_diagnostic(self):
        bare_run = {"runId": "run-1", "contextIssueId": "DEMO-1"}
        _, report = self.explain(raw_of(doc(
            runs=[bare_run], work_products=[link_row(head=None)])))
        self.assertEqual(self.triples(report), [])
        (trace,) = report["traces"]
        self.assertIn("pr.headSha", trace["missing"])

    def test_deployment_with_no_eligible_exact_match_is_located(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[
                deploy_row(sha=SHA_C),
                deploy_row(repo="other-org/other-repo"),
            ],
        )))
        self.assertEqual(self.triples(report), [
            ("deployments", 0, "deploy_no_eligible_match"),
            ("deployments", 1, "deploy_no_eligible_match"),
        ])
        (trace,) = report["traces"]
        self.assertIsNone(trace["deployId"])
        self.assertEqual(report["delivery"]["orphanDeployments"], 2)

    def test_eligible_duplicate_deployment_not_selected(self):
        late_first = doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[
                deploy_row(deploy="late", deployed="2026-10-20T11:00:00Z"),
                deploy_row(deploy="early", deployed="2026-10-20T10:27:00Z"),
            ])
        _, report = self.explain(raw_of(late_first))
        self.assertEqual(report["traces"][0]["deployId"], "early")
        self.assertEqual(self.triples(report),
                         [("deployments", 0, "deploy_not_selected")])

    def test_equal_time_duplicate_keeps_file_order_and_locates_loser(self):
        same_time = doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[
                deploy_row(deploy="first"),
                deploy_row(deploy="second"),
            ])
        _, report = self.explain(raw_of(same_time))
        self.assertEqual(report["traces"][0]["deployId"], "first")
        self.assertEqual(self.triples(report),
                         [("deployments", 1, "deploy_not_selected")])

    def test_merge_precedence_leaves_head_candidate_eligible_unselected(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()],
            work_products=[link_row(head=SHA_A, merge=SHA_B)],
            deployments=[
                deploy_row(sha=SHA_A, deploy="preview",
                           deployed="2026-10-20T10:12:00Z"),
                deploy_row(sha=SHA_B, deploy="prod",
                           deployed="2026-10-20T10:57:00Z"),
            ],
        )))
        (trace,) = report["traces"]
        self.assertEqual(trace["shaMatch"], "merge")
        self.assertEqual(trace["deployId"], "prod")
        self.assertEqual(self.triples(report),
                         [("deployments", 0, "deploy_not_selected")])

    def test_deployment_whose_only_link_is_invalid_has_no_eligible_match(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()], work_products=[link_row(head="main")],
            deployments=[deploy_row()],
        )))
        self.assertEqual(self.triples(report), [
            ("workProducts", 0, "link_head_not_exact_sha"),
            ("deployments", 0, "deploy_no_eligible_match"),
        ])

    def test_deployment_whose_only_link_is_orphaned_has_no_eligible_match(self):
        _, report = self.explain(raw_of(doc(
            runs=[run_row()],
            work_products=[link_row(run="run-ghost", head=SHA_B)],
            deployments=[deploy_row(sha=SHA_B)],
        )))
        self.assertEqual(self.triples(report), [
            ("workProducts", 0, "link_orphan_missing_run"),
            ("deployments", 0, "deploy_no_eligible_match"),
        ])

    def test_lone_deployment_with_no_traces_has_no_eligible_match(self):
        done, report = self.explain(raw_of(doc(deployments=[deploy_row()])))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(report["slo"]["unknownReason"],
                         "zero_eligible_samples")
        self.assertEqual(self.triples(report),
                         [("deployments", 0, "deploy_no_eligible_match")])


class OrderingLimitAndTruncation(DiagnosticsTestCase):
    def test_stable_input_order_across_collections(self):
        _, report = self.explain(raw_of(mixed_doc()))
        self.assertEqual(self.triples(report), MIXED_EXPECTED)

    def test_limit_returns_exact_prefix_with_exact_counts(self):
        raw = raw_of(mixed_doc())
        _, full = self.explain(raw, "--diagnostic-limit", "1000")
        _, limited = self.explain(raw, "--diagnostic-limit=2")
        full_rows = self.diagnostics(full)["diagnostics"]
        section = self.diagnostics(limited)
        self.assert_well_formed_section(section)
        self.assertEqual(section["limit"], 2)
        self.assertEqual(section["total"], len(MIXED_EXPECTED))
        self.assertEqual(section["returned"], 2)
        self.assertTrue(section["truncated"])
        self.assertFalse(section["complete"])
        self.assertEqual(section["diagnostics"], full_rows[:2])
        # The limit bounds explanation only; the join itself is untouched.
        self.assertEqual(canonical(limited), canonical(full))

    def test_limit_equal_to_total_is_complete(self):
        _, report = self.explain(raw_of(mixed_doc()),
                                 "--diagnostic-limit",
                                 str(len(MIXED_EXPECTED)))
        section = self.diagnostics(report)
        self.assertEqual(section["returned"], len(MIXED_EXPECTED))
        self.assertFalse(section["truncated"])
        self.assertTrue(section["complete"])

    def test_limit_boundaries_one_and_thousand_are_accepted(self):
        raw = raw_of(doc(runs=[run_row()], work_products=[link_row()],
                         deployments=[deploy_row()]))
        for limit in ("1", "1000"):
            with self.subTest(limit=limit):
                done, report = self.explain(raw, "--diagnostic-limit", limit)
                self.assertEqual(done.returncode, 0)
                self.assertEqual(self.diagnostics(report)["limit"], int(limit))

    def test_default_limit_is_one_hundred(self):
        raw = raw_of(doc(runs=[{} for _ in range(150)]))
        done, report = self.explain(raw)
        self.assertEqual(done.returncode, 1)
        section = self.diagnostics(report)
        self.assert_well_formed_section(section)
        self.assertEqual(section["limit"], 100)
        self.assertEqual(section["total"], 150)
        self.assertEqual(section["returned"], 100)
        self.assertTrue(section["truncated"])
        self.assertFalse(section["complete"])
        self.assertEqual(section["diagnostics"][0]["index"], 0)
        self.assertEqual(section["diagnostics"][-1]["index"], 99)
        self.assertEqual(report["delivery"]["invalidRows"], 150)

        _, widened = self.explain(raw, "--diagnostic-limit", "1000")
        section = self.diagnostics(widened)
        self.assertEqual(section["returned"], 150)
        self.assertTrue(section["complete"])


class RequestedModeRefusals(DiagnosticsTestCase):
    def test_invalid_input_in_explain_mode_is_pure_json_refusal(self):
        done = cli(b'{"schema_version":1,"note":"CANARY-SECRET-BODY"',
                   "--explain-joins")
        payload = self.assert_json_refusal(done, "invalid_input")
        text = json.dumps(payload)
        self.assertNotIn("CANARY-SECRET-BODY", text)
        self.assertNotIn("Expecting", text)  # no parser exception text

    def test_wrong_schema_and_oversize_in_explain_mode_refuse_as_json(self):
        for raw in (b'{"schema_version":"1"}', b"x" * (1_048_576 + 1)):
            with self.subTest(prefix=raw[:24]):
                self.assert_json_refusal(cli(raw, "--explain-joins"),
                                         "invalid_input")

    def test_limit_without_explain_is_a_json_refusal(self):
        raw = raw_of(doc(runs=[run_row()]))
        for args in (("--diagnostic-limit", "5"),
                     ("--dry-run", "--diagnostic-limit", "5")):
            with self.subTest(args=args):
                self.assert_json_refusal(
                    cli(raw, *args),
                    "diagnostic_limit_requires_explain_joins")

    def test_out_of_range_and_malformed_limits_refuse_as_json(self):
        raw = raw_of(doc(runs=[run_row()]))
        for limit_args in (("--diagnostic-limit", "0"),
                           ("--diagnostic-limit", "1001"),
                           ("--diagnostic-limit=-1",),
                           ("--diagnostic-limit", "abc"),
                           ("--diagnostic-limit", "1.5")):
            with self.subTest(limit_args=limit_args):
                self.assert_json_refusal(
                    cli(raw, "--explain-joins", *limit_args),
                    "invalid_diagnostic_limit")

    def test_missing_limit_value_refuses_as_json(self):
        done = cli(raw_of(doc(runs=[run_row()])),
                   "--explain-joins", "--diagnostic-limit")
        self.assert_json_refusal(
            done, {"invalid_diagnostic_limit", "invalid_usage"})

    def test_unknown_argument_in_explain_mode_refuses_without_echoing_it(self):
        raw = raw_of(doc(runs=[run_row()]))
        done = cli(raw, "--explain-joins", "/private-canary/evidence.json")
        payload = self.assert_json_refusal(done, "invalid_usage")
        self.assertNotIn("private-canary", json.dumps(payload))
        self.assert_json_refusal(cli(raw, "--explain-joins", "--bogus"),
                                 "invalid_usage")

    def test_unknown_argument_without_diagnostics_flags_keeps_text_form(self):
        done = cli(raw_of(doc(runs=[run_row()])), "--bogus")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")


class DisclosureAndSafety(DiagnosticsTestCase):
    def test_new_fields_never_disclose_row_bodies_or_identifiers(self):
        run = run_row(run="run-canary-xyz", card="DEMO-CANARY-99")
        run["note"] = "CANARY-NOTE-BODY"
        bad_link = link_row(card="DEMO-CANARY-99", run="run-canary-xyz",
                            repo="canary-org/canary-repo",
                            head="canary-branch-ref")
        bad_link["url"] = "https://example.invalid/CANARY-URL-TAIL"
        bad_link["title"] = "CANARY-TITLE"
        bad_deploy = deploy_row(repo="canary-org/canary-repo",
                                sha="canary-short-sha",
                                deploy="deploy-canary-xyz")
        _, report = self.explain(raw_of(doc(
            runs=[run], work_products=[bad_link], deployments=[bad_deploy])))
        # The rows were located...
        self.assertEqual(self.triples(report), [
            ("workProducts", 0, "link_head_not_exact_sha"),
            ("deployments", 0, "deploy_sha_not_exact"),
        ])
        # ...but nothing about them leaks through the additive fields. The
        # default report's own validated identities are out of scope here.
        blob = json.dumps(report["joinDiagnostics"], sort_keys=True)
        for canary in ("run-canary-xyz", "DEMO-CANARY-99",
                       "canary-org/canary-repo", "canary-branch-ref",
                       "canary-short-sha", "deploy-canary-xyz",
                       "CANARY-NOTE-BODY", "CANARY-URL-TAIL", "CANARY-TITLE",
                       "://", SHA_A):
            self.assertNotIn(canary, blob)

    def test_explain_run_writes_no_files_in_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            done = cli(raw_of(mixed_doc()), "--explain-joins", cwd=work)
            self.assertIn(done.returncode, (0, 1), done.stderr)
            self.assertIn("joinDiagnostics", json.loads(done.stdout))
            self.assertEqual(list(work.iterdir()), [])


class HelpDryRunAndDocs(DiagnosticsTestCase):
    def test_help_documents_flags_without_reading_stdin(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                done = cli(b"{not json", flag)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stderr, b"")
                text = done.stdout.decode()
                for token in ("--explain-joins", "--diagnostic-limit",
                              "--dry-run"):
                    self.assertIn(token, text)
                self.assertIn("stdin", text.lower())

    def test_dry_run_is_a_noop_with_explain(self):
        raw = raw_of(mixed_doc())
        plain_done, plain = self.explain(raw)
        dry_done, dry = self.explain(raw, "--dry-run")
        self.assertEqual(dry_done.returncode, plain_done.returncode)
        self.assertEqual(self.diagnostics(dry), self.diagnostics(plain))
        self.assertEqual(canonical(dry), canonical(plain))

    def test_dry_run_alone_matches_default_mode(self):
        raw = raw_of(doc(runs=[run_row()], work_products=[link_row()],
                         deployments=[deploy_row()]))
        default_done = cli(raw)
        dry_done = cli(raw, "--dry-run")
        self.assertEqual(dry_done.returncode, default_done.returncode)
        dry_report = json.loads(dry_done.stdout)
        self.assertNotIn("joinDiagnostics", dry_report)
        self.assertEqual(canonical(dry_report),
                         canonical(json.loads(default_done.stdout)))

    def test_canonical_docs_describe_the_explanation_path(self):
        text = (ROOT / "docs" / "trace-correlation.md").read_text(
            encoding="utf-8")
        for token in ("--explain-joins", "--diagnostic-limit", "--dry-run",
                      "trace-join-diagnostics.v1"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
