#!/usr/bin/env python3
"""Offline contract for the card/run/PR/head/deploy trace correlator.

Every case is hermetic: fixed fixtures, pinned clock, no network, no
credentials, no board. Fixture identifiers are DEMO placeholders.
"""

import datetime as dt
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ops" / "paperclip"))

import trace_correlator
from trace_correlator import InputError, correlate, load_document

AT = "2026-10-25T07:30:00Z"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
INVALID_IGNORED_JSON = (
    ("positive_overflow", b'{"schema_version":1,"ignored":1e999}'),
    ("negative_overflow", b'{"schema_version":1,"ignored":-1e999}'),
    ("high_surrogate_key", b'{"schema_version":1,"\\ud800":"ignored"}'),
    ("low_surrogate_key", b'{"schema_version":1,"\\udfff":"ignored"}'),
    ("high_surrogate_value", b'{"schema_version":1,"ignored":"\\ud800"}'),
    ("low_surrogate_value", b'{"schema_version":1,"ignored":"\\udfff"}'),
    ("nested_surrogate_key",
     b'{"schema_version":1,"ignored":[{"\\ud800":0}]}'),
    ("nested_surrogate_value",
     b'{"schema_version":1,"ignored":[{"value":"\\udfff"}]}'),
)


class BoundedInput(io.BytesIO):
    def __init__(self, raw, limit):
        super().__init__(raw)
        self.limit = limit
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size != self.limit + 1:
            raise AssertionError(f"stdin read must be bounded, got {size}")
        return super().read(size)


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


def deploy_row(repo="demo-org/demo-repo", sha=SHA_A, deploy="2026.10.20.1+aaaaaaa",
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


def cli(raw):
    return subprocess.run(
        [sys.executable, "ops/paperclip/trace_correlator.py"],
        input=raw,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )


class NormalizeTest(unittest.TestCase):
    def test_rest_and_sdk_run_shapes_join_identically(self):
        rest = run_row()
        sdk = {
            "id": "run-1",
            "issueId": "DEMO-1",
            "createdAt": rest["createdAt"],
            "startedAt": rest["startedAt"],
            "finishedAt": rest["finishedAt"],
        }
        first = correlate(doc(runs=[rest]), AT)
        second = correlate(doc(runs=[sdk]), AT)
        self.assertEqual(first["traces"], second["traces"])
        self.assertEqual(first["traces"][0]["run"], "run-1")
        self.assertEqual(first["traces"][0]["card"], "DEMO-1")

    def test_duplicate_run_id_is_invalid_not_double_traced(self):
        report = correlate(doc(runs=[run_row(), run_row()]), AT)
        self.assertEqual(len(report["traces"]), 1)
        self.assertIn("run_duplicate_id", report["delivery"]["unknowns"])
        self.assertEqual(report["delivery"]["runsValid"], 1)


class JoinTest(unittest.TestCase):
    def test_full_chain_carries_all_five_ids_and_stages(self):
        report = correlate(
            doc(runs=[run_row()], work_products=[link_row()],
                deployments=[deploy_row()]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertEqual(trace["card"], "DEMO-1")
        self.assertEqual(trace["run"], "run-1")
        self.assertEqual(trace["repo"], "demo-org/demo-repo")
        self.assertEqual(trace["pr"], 7)
        self.assertEqual(trace["headSha"], SHA_A)
        self.assertEqual(trace["deployId"], "2026.10.20.1+aaaaaaa")
        self.assertEqual(trace["stages"]["admissionToStartS"], 120.0)
        self.assertEqual(trace["stages"]["executionS"], 300.0)
        self.assertEqual(trace["stages"]["mergeToProdS"], 1200.0)
        self.assertEqual(trace["missing"], [])
        self.assertEqual(report["slo"]["verdict"], "met")
        self.assertEqual(report["delivery"]["coverageDeployJoin"], 1.0)

    def test_mutable_head_ref_is_rejected_never_resolved(self):
        bad = link_row(head="main")
        report = correlate(
            doc(runs=[run_row()], work_products=[bad],
                deployments=[deploy_row()]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertIsNone(trace["headSha"])
        self.assertIsNone(trace["deployId"])
        self.assertIn("pr_link", trace["missing"])
        self.assertIn("link_head_not_exact_sha", report["delivery"]["unknowns"])
        self.assertEqual(report["delivery"]["prLinksJoined"], 0)
        self.assertEqual(report["delivery"]["orphanDeployments"], 1)

    def test_no_guessing_orphans_are_counted_not_joined(self):
        unknown_run = link_row(run="run-ghost")
        card_mismatch = link_row(card="DEMO-other", head=SHA_B)
        wrong_sha = deploy_row(sha=SHA_C)
        report = correlate(
            doc(runs=[run_row()], work_products=[unknown_run, card_mismatch],
                deployments=[wrong_sha]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertIn("pr_link", trace["missing"])
        self.assertEqual(report["delivery"]["orphanWorkProducts"], 2)
        self.assertEqual(report["delivery"]["orphanDeployments"], 1)
        self.assertEqual(report["delivery"]["coveragePrJoin"], 0.0)

    def test_valid_pr_link_never_joins_wrong_sha_or_repository(self):
        wrong_sha = deploy_row(sha=SHA_B, deploy="wrong-sha")
        wrong_repo = deploy_row(repo="other-org/other-repo", deploy="wrong-repo")
        for deployments in ([wrong_sha], [wrong_repo], [wrong_sha, wrong_repo]):
            with self.subTest(deployments=deployments):
                report = correlate(
                    doc(runs=[run_row()], work_products=[link_row()],
                        deployments=deployments), AT)
                (trace,) = report["traces"]
                self.assertEqual(trace["repo"], "demo-org/demo-repo")
                self.assertEqual(trace["pr"], 7)
                self.assertEqual(trace["headSha"], SHA_A)
                self.assertIsNone(trace["deployId"])
                self.assertIsNone(trace["shaMatch"])
                self.assertIsNone(trace["stages"]["mergeToProdS"])
                self.assertEqual(trace["missing"], ["deploy"])
                self.assertEqual(report["slo"]["verdict"], "unknown")
                self.assertEqual(report["slo"]["samples"], 0)
                delivery = report["delivery"]
                self.assertEqual(delivery["prLinksJoined"], 1)
                self.assertEqual(delivery["coveragePrJoin"], 1.0)
                self.assertEqual(delivery["orphanWorkProducts"], 0)
                self.assertEqual(delivery["deploysValid"], len(deployments))
                self.assertEqual(delivery["orphanDeployments"], len(deployments))
                self.assertEqual(delivery["deploysJoined"], 0)
                self.assertEqual(delivery["coverageDeployJoin"], 0.0)

    def test_pr_identifiers_reject_trailing_newline_without_trimming(self):
        for field, reason in (
            ("headSha", "link_head_not_exact_sha"),
            ("mergeSha", "link_merge_not_exact_sha"),
            ("repo", "link_bad_repo"),
        ):
            for suffix in ("", "\n"):
                with self.subTest(field=field, suffix=repr(suffix)):
                    link = link_row(merge=SHA_B)
                    link[field] += suffix
                    report = correlate(
                        doc(runs=[run_row()], work_products=[link],
                            deployments=[deploy_row(sha=SHA_B)]), AT)
                    (trace,) = report["traces"]
                    delivery = report["delivery"]
                    if not suffix:
                        self.assertEqual(trace["headSha"], SHA_A)
                        self.assertEqual(trace["mergeSha"], SHA_B)
                        self.assertEqual(trace["repo"], "demo-org/demo-repo")
                        self.assertEqual(trace["deployId"], "2026.10.20.1+aaaaaaa")
                        self.assertEqual(trace["missing"], [])
                        self.assertEqual(delivery["invalidRows"], 0)
                        self.assertEqual(delivery["prLinksJoined"], 1)
                    else:
                        self.assertEqual(delivery["unknowns"], [reason])
                        self.assertEqual(delivery["invalidRows"], 1)
                        self.assertEqual(delivery["prLinksExplicit"], 0)
                        self.assertEqual(delivery["prLinksJoined"], 0)
                        self.assertEqual(trace["missing"], ["pr_link"])
                        self.assertIsNone(trace["headSha"])
                        self.assertIsNone(trace["mergeSha"])
                        self.assertIsNone(trace["repo"])
                        self.assertIsNone(trace["deployId"])

    def test_deploy_identifiers_reject_trailing_newline_without_trimming(self):
        for field, reason in (
            ("sha", "deploy_sha_not_exact"),
            ("repo", "deploy_bad_repo"),
        ):
            for suffix in ("", "\n"):
                with self.subTest(field=field, suffix=repr(suffix)):
                    deploy = deploy_row()
                    deploy[field] += suffix
                    report = correlate(
                        doc(runs=[run_row()], work_products=[link_row()],
                            deployments=[deploy]), AT)
                    (trace,) = report["traces"]
                    delivery = report["delivery"]
                    self.assertEqual(delivery["prLinksJoined"], 1)
                    if not suffix:
                        self.assertEqual(trace["deployId"], "2026.10.20.1+aaaaaaa")
                        self.assertEqual(trace["missing"], [])
                        self.assertEqual(delivery["invalidRows"], 0)
                        self.assertEqual(delivery["deploysValid"], 1)
                        self.assertEqual(delivery["deploysJoined"], 1)
                    else:
                        self.assertEqual(delivery["unknowns"], [reason])
                        self.assertEqual(delivery["invalidRows"], 1)
                        self.assertEqual(delivery["deploysValid"], 0)
                        self.assertEqual(delivery["deploysJoined"], 0)
                        self.assertEqual(trace["missing"], ["deploy"])
                        self.assertIsNone(trace["deployId"])
                        self.assertIsNone(trace["stages"]["mergeToProdS"])

    def test_earliest_deploy_wins_tie_keeps_file_order(self):
        late = deploy_row(deploy="2026.10.20.2+aaaaaaa",
                          deployed="2026-10-20T11:00:00Z")
        early = deploy_row(deploy="2026.10.20.1+aaaaaaa",
                           deployed="2026-10-20T10:27:00Z")
        report = correlate(
            doc(runs=[run_row()], work_products=[link_row()],
                deployments=[late, early]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertEqual(trace["deployId"], "2026.10.20.1+aaaaaaa")
        self.assertEqual(trace["stages"]["mergeToProdS"], 1200.0)

    def test_merge_sha_joins_post_merge_deploys(self):
        link = link_row(head=SHA_A, merge=SHA_B)
        preview = deploy_row(sha=SHA_A, deploy="preview-1",
                             deployed="2026-10-20T10:12:00Z")
        prod = deploy_row(sha=SHA_B, deploy="2026.10.20.1+bbbbbbb",
                          merged="2026-10-20T10:07:00Z",
                          deployed="2026-10-20T10:57:00Z")
        report = correlate(
            doc(runs=[run_row()], work_products=[link],
                deployments=[preview, prod]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertEqual(trace["mergeSha"], SHA_B)
        self.assertEqual(trace["shaMatch"], "merge")
        self.assertEqual(trace["deployId"], "2026.10.20.1+bbbbbbb")
        self.assertEqual(trace["stages"]["mergeToProdS"], 3000.0)

    def test_head_sha_joins_preview_deploys(self):
        report = correlate(
            doc(runs=[run_row()], work_products=[link_row()],
                deployments=[deploy_row()]),
            AT,
        )
        (trace,) = report["traces"]
        self.assertEqual(trace["shaMatch"], "head")

    def test_mutable_merge_ref_is_rejected(self):
        bad = link_row(merge="v1.2.3")
        report = correlate(doc(runs=[run_row()], work_products=[bad]), AT)
        (trace,) = report["traces"]
        self.assertIn("pr_link", trace["missing"])
        self.assertIn("link_merge_not_exact_sha",
                      report["delivery"]["unknowns"])

    def test_determinism_shuffled_input_same_report(self):
        rows = [run_row(run=f"run-{n}", card=f"DEMO-{n}") for n in range(5)]
        links = [link_row(run=f"run-{n}", card=f"DEMO-{n}", pr=n + 1)
                 for n in range(5)]
        forward = correlate(
            doc(runs=rows, work_products=links), AT)
        backward = correlate(
            doc(runs=list(reversed(rows)), work_products=list(reversed(links))),
            AT,
        )
        self.assertEqual(forward, backward)


class UnknownStateTest(unittest.TestCase):
    def test_missing_spans_are_null_and_named_never_zero(self):
        bare = {"runId": "run-1", "contextIssueId": "DEMO-1"}
        report = correlate(doc(runs=[bare]), AT)
        (trace,) = report["traces"]
        self.assertEqual(
            trace["stages"],
            {"admissionToStartS": None, "executionS": None,
             "mergeToProdS": None},
        )
        for name in ("run.createdAt", "run.startedAt", "run.finishedAt",
                     "pr_link"):
            self.assertIn(name, trace["missing"])

    def test_partial_chain_names_each_gap(self):
        link = link_row(head=None)
        report = correlate(doc(runs=[run_row()], work_products=[link]), AT)
        (trace,) = report["traces"]
        self.assertIn("pr.headSha", trace["missing"])
        self.assertIsNone(trace["stages"]["mergeToProdS"])

        report = correlate(
            doc(runs=[run_row()], work_products=[link_row()]), AT)
        (trace,) = report["traces"]
        self.assertIn("deploy", trace["missing"])

    def test_negative_span_is_skew_not_latency(self):
        skewed = run_row(started="2026-10-20T09:59:00Z")
        report = correlate(doc(runs=[skewed]), AT)
        (trace,) = report["traces"]
        self.assertIsNone(trace["stages"]["admissionToStartS"])
        self.assertIn("run.admission_negative", trace["missing"])

    def test_invalid_rows_are_counted_never_fatal(self):
        rows = [
            "not-an-object",
            {"runId": "", "contextIssueId": "DEMO-1"},
            {"runId": "run-9"},
        ]
        links = [
            ["not-an-object"],
            {"issueId": "DEMO-1", "createdByRunId": "run-1", "repo": "bad",
             "pr": 1},
            {"issueId": "DEMO-1", "createdByRunId": "run-1",
             "repo": "demo-org/demo-repo", "pr": True},
        ]
        deploys = [
            {"repo": "demo-org/demo-repo", "sha": "short"},
            {"repo": "demo-org/demo-repo", "sha": SHA_A, "deployId": ""},
        ]
        report = correlate(
            doc(runs=rows + [run_row()], work_products=links,
                deployments=deploys),
            AT,
        )
        self.assertEqual(len(report["traces"]), 1)
        self.assertEqual(report["delivery"]["invalidRows"], 8)
        self.assertIn("deploy_sha_not_exact", report["delivery"]["unknowns"])

    def test_zero_eligible_is_unknown_never_met(self):
        report = correlate(doc(runs=[run_row()]), AT)
        slo = report["slo"]
        self.assertEqual(slo["verdict"], "unknown")
        self.assertEqual(slo["unknownReason"], "zero_eligible_samples")
        self.assertIsNone(slo["p50S"])
        self.assertIsNone(slo["p90S"])
        self.assertEqual(slo["samples"], 0)


class SloMathTest(unittest.TestCase):
    def _chain(self, n, merge_s):
        sha = f"{n:040x}"
        merged_dt = dt.datetime(2026, 10, 20, 10, 0, 0,
                                tzinfo=dt.timezone.utc)
        merged = "2026-10-20T10:00:00Z"
        deployed = (merged_dt + dt.timedelta(seconds=merge_s)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return (
            run_row(run=f"run-{n}", card=f"DEMO-{n}"),
            link_row(run=f"run-{n}", card=f"DEMO-{n}", pr=n, head=sha),
            deploy_row(sha=sha, merged=merged, deployed=deployed),
        )

    def test_met_pins_interpolated_percentiles(self):
        parts = [self._chain(n, s) for n, s in
                 enumerate((600, 1200, 1800, 2400), start=1)]
        report = correlate(
            doc(runs=[p[0] for p in parts],
                work_products=[p[1] for p in parts],
                deployments=[p[2] for p in parts]),
            AT,
        )
        slo = report["slo"]
        self.assertEqual(slo["verdict"], "met")
        self.assertEqual(slo["p50S"], 1500.0)
        self.assertEqual(slo["p90S"], 2220.0)

    def test_p90_breach_is_missed(self):
        parts = [self._chain(n, s) for n, s in
                 enumerate((600, 1200, 1800, 20000), start=1)]
        report = correlate(
            doc(runs=[p[0] for p in parts],
                work_products=[p[1] for p in parts],
                deployments=[p[2] for p in parts]),
            AT,
        )
        slo = report["slo"]
        self.assertEqual(slo["verdict"], "missed")
        self.assertEqual(slo["p50S"], 1500.0)
        # rank 2.7: 1800 + 0.7 * (20000 - 1800)
        self.assertEqual(slo["p90S"], 14540.0)

    def test_p50_only_breach_is_missed(self):
        parts = [self._chain(n, 7200) for n in range(1, 5)]
        report = correlate(
            doc(runs=[p[0] for p in parts],
                work_products=[p[1] for p in parts],
                deployments=[p[2] for p in parts]), AT)
        slo = report["slo"]
        self.assertEqual(slo["samples"], 4)
        self.assertEqual(slo["p50S"], 7200.0)
        self.assertEqual(slo["p90S"], 7200.0)
        self.assertEqual(slo["verdict"], "missed")

    def test_thresholds_are_inclusive_and_independently_enforced(self):
        for samples, p50, p90, verdict in (
            ((3600, 3600), 3600.0, 3600.0, "met"),
            ((0, 0, 3600, 10800, 10800), 3600.0, 10800.0, "met"),
            ((3601, 3601), 3601.0, 3601.0, "missed"),
            ((0, 0, 3600, 10801, 10801), 3600.0, 10801.0, "missed"),
        ):
            with self.subTest(samples=samples):
                parts = [self._chain(n, s) for n, s in
                         enumerate(samples, start=1)]
                report = correlate(
                    doc(runs=[p[0] for p in parts],
                        work_products=[p[1] for p in parts],
                        deployments=[p[2] for p in parts]), AT)
                slo = report["slo"]
                self.assertEqual(slo["samples"], len(samples))
                self.assertEqual(slo["p50S"], p50)
                self.assertEqual(slo["p90S"], p90)
                self.assertEqual(slo["verdict"], verdict)

    def test_single_sample_percentile_is_the_sample(self):
        run, link, deploy = self._chain(1, 900)
        report = correlate(
            doc(runs=[run], work_products=[link], deployments=[deploy]), AT)
        slo = report["slo"]
        self.assertEqual(slo["verdict"], "met")
        self.assertEqual(slo["p50S"], 900.0)
        self.assertEqual(slo["p90S"], 900.0)


class DocumentTest(unittest.TestCase):
    def test_rejects_malformed_documents(self):
        with self.assertRaises(InputError):
            load_document(b"{not json")
        with self.assertRaises(InputError):
            load_document(b'{"schema_version": 1, "schema_version": 1}')
        with self.assertRaises(InputError):
            load_document(b'{"schema_version": 1, "runs": [NaN]}')
        with self.assertRaises(InputError):
            load_document(b"[]")
        with self.assertRaises(InputError):
            load_document(b'{"schema_version": "1"}')
        with self.assertRaises(InputError):
            load_document(b'{"schema_version": 1, "runs": {}}')

    def test_rejects_nonfinite_and_surrogate_ignored_keys_and_values(self):
        for name, raw in INVALID_IGNORED_JSON:
            with self.subTest(name=name):
                with self.assertRaises(InputError):
                    load_document(raw)

    def test_accepts_finite_and_unicode_ignored_values(self):
        raw = ('{"schema_version":1,"ignored":{"snowman":"☃",'
               '"value":1e99,"pair":"\\ud83d\\ude00"}}').encode("utf-8")
        self.assertEqual(load_document(raw),
                         {"runs": [], "workProducts": [], "deployments": []})

    def test_input_size_boundary_accepts_limit_and_rejects_one_byte_over(self):
        limit = 256
        raw = b'{"schema_version":1}'.ljust(limit, b" ")
        with patch.object(trace_correlator, "MAX_INPUT_BYTES", limit):
            self.assertEqual(load_document(raw),
                             {"runs": [], "workProducts": [], "deployments": []})
            with self.assertRaises(InputError):
                load_document(raw + b" ")

    def test_rejects_oversize_documents(self):
        with self.assertRaises(InputError):
            load_document(b"x" * (1_048_576 + 1))


class CliTest(unittest.TestCase):
    def test_met_exits_zero_with_report(self):
        raw = json.dumps(doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[deploy_row()])).encode()
        done = cli(raw)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["schema"], "trace-correlation.v1")
        self.assertEqual(report["slo"]["verdict"], "met")
        self.assertEqual(len(report["traces"]), 1)

    def test_unknown_exits_one(self):
        done = cli(json.dumps(doc(runs=[run_row()])).encode())
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(json.loads(done.stdout)["slo"]["verdict"], "unknown")

    def test_missed_exits_one_with_report(self):
        raw = json.dumps(doc(
            runs=[run_row()], work_products=[link_row()],
            deployments=[deploy_row(deployed="2026-10-20T12:07:00Z")])).encode()
        done = cli(raw)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stderr, b"")
        report = json.loads(done.stdout)
        self.assertEqual(report["slo"]["verdict"], "missed")
        self.assertEqual(report["slo"]["samples"], 1)
        self.assertEqual(report["slo"]["p50S"], 7200.0)
        self.assertEqual(report["slo"]["p90S"], 7200.0)

    def test_utc_overflow_is_named_without_losing_valid_sibling_traces(self):
        stages = {"admissionToStartS": 120.0, "executionS": 300.0,
                  "mergeToProdS": 1200.0}
        for overflow in ("0001-01-01T00:00:00+01:00",
                         "9999-12-31T23:59:59-01:00"):
            for source, field, affected in (
                ("run", "createdAt", ("admissionToStartS",)),
                ("run", "startedAt", ("admissionToStartS", "executionS")),
                ("run", "finishedAt", ("executionS",)),
                ("deploy", "mergedAt", ("mergeToProdS",)),
                ("deploy", "deployedAt", ("mergeToProdS",)),
            ):
                with self.subTest(overflow=overflow, source=source, field=field):
                    bad_run = run_row(run="run-2", card="DEMO-2")
                    bad_deploy = deploy_row(sha=SHA_B, deploy="sibling-deploy")
                    row = bad_run if source == "run" else bad_deploy
                    row[field] = overflow
                    raw = json.dumps(doc(
                        runs=[run_row(), bad_run],
                        work_products=[link_row(),
                                       link_row(run="run-2", card="DEMO-2",
                                                head=SHA_B, pr=8)],
                        deployments=[deploy_row(), bad_deploy])).encode()
                    done = cli(raw)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(done.stderr, b"")
                    report = json.loads(done.stdout)
                    self.assertEqual(len(report["traces"]), 2)
                    traces = {trace["run"]: trace for trace in report["traces"]}
                    good = traces["run-1"]
                    self.assertEqual(good["stages"], stages)
                    self.assertEqual(good["missing"], [])
                    self.assertEqual(good["headSha"], SHA_A)
                    self.assertEqual(good["deployId"], "2026.10.20.1+aaaaaaa")
                    bad = traces["run-2"]
                    self.assertEqual(bad["missing"], [f"{source}.{field}"])
                    for stage, measured in stages.items():
                        if stage in affected:
                            self.assertIsNone(bad["stages"][stage])
                        else:
                            self.assertEqual(bad["stages"][stage], measured)
                    self.assertEqual(bad["headSha"], SHA_B)
                    self.assertEqual(bad["deployId"], "sibling-deploy")
                    self.assertEqual(report["slo"]["verdict"], "met")
                    self.assertEqual(report["slo"]["samples"],
                                     2 if source == "run" else 1)
                    self.assertEqual(report["delivery"]["runsValid"], 2)
                    self.assertEqual(report["delivery"]["deploysJoined"], 2)
                    self.assertEqual(report["delivery"]["invalidRows"], 0)

    def test_invalid_ignored_json_exits_two_with_empty_stdout(self):
        for name, raw in INVALID_IGNORED_JSON:
            with self.subTest(name=name):
                done = cli(raw)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, b"")
                self.assertIn(b"trace_correlator", done.stderr)
                self.assertNotIn(b"Traceback", done.stderr)

    def test_bad_input_exits_two_with_no_report(self):
        done = cli(b"{not json")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")
        self.assertIn(b"trace_correlator", done.stderr)

        done = cli(b"x" * (1_048_576 + 1))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")


class MainInputTest(unittest.TestCase):
    def _invoke(self, raw, limit):
        source = BoundedInput(raw, limit)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(trace_correlator, "MAX_INPUT_BYTES", limit), \
                patch.object(sys, "stdin", SimpleNamespace(buffer=source)), \
                patch.object(sys, "stdout", stdout), \
                patch.object(sys, "stderr", stderr):
            status = trace_correlator.main([])
        self.assertEqual(source.read_sizes, [limit + 1])
        self.assertLessEqual(source.tell(), limit + 1)
        return status, stdout.getvalue(), stderr.getvalue(), source.tell()

    def test_exact_input_limit_is_accepted_with_one_bounded_read(self):
        limit = 256
        raw = b'{"schema_version":1}'.ljust(limit, b" ")
        status, stdout, stderr, consumed = self._invoke(raw, limit)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout)["slo"]["verdict"], "unknown")
        self.assertEqual(stderr, "")
        self.assertEqual(consumed, limit)

    def test_oversize_refusal_does_not_drain_stdin(self):
        limit = 256
        for size in (limit + 1, limit * 3):
            with self.subTest(size=size):
                raw = b'{"schema_version":1}'.ljust(size, b" ")
                status, stdout, stderr, consumed = self._invoke(raw, limit)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("trace_correlator", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(consumed, limit + 1)


if __name__ == "__main__":
    unittest.main()
