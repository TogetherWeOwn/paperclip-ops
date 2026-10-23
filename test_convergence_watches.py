"""Independent offline acceptance of the pinned convergence digest contract.

Expectations come from docs/convergence-watches.md at
3b80b4d75ab620a18fe8c70f96c347df97a6fa2d, not the implementation or its
builder's probes. Fixtures and literal counts were written before opening
ops/convergence_watches.py. Builder: adopt this file verbatim; test corrections
belong to the Test Author. No live collector/digest/deployment qualification.

Run: python3 -B -m unittest -v test_convergence_watches
Only stdlib, synthetic inputs, local CLI subprocesses with deadlines. No shell,
network, database service, credentials, production data, or harness changes.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "ops" / "convergence_watches.py"
START = 10000
END = 96400
BASE = "bba6a45f0d364cfd962ac59b3bdf2426a8222adb"
SOURCE = "1234567890abcdef1234567890abcdef12345678"
IMAGE = "sha256:" + "ab" * 32
HEAD = "0123456789abcdef0123456789abcdef01234567"
WATCH_SOURCES = {
    "rapid_reblock": ("status_transitions",),
    "over_cap_starts": ("run_intervals", "cap_intervals"),
    "workspace_incoherence": ("workspace_observations",),
    "duplicate_review_heads": ("reviews",),
    "stdin_deadline_failures": ("run_failures",),
}
SOURCES = tuple(s for group in WATCH_SOURCES.values() for s in group)


def snapshot(**rows):
    return {
        "schema": "paperclip.convergence-export.v1",
        "company_id": "00000000-0000-4000-8000-000000000004",
        "artifact": {"base_sha": BASE, "source_sha": SOURCE, "image_digest": IMAGE},
        "start": START,
        "end": END,
        "sources": {
            name: {"complete": True, "boundary_complete": True,
                   "start": START - 300 if name == "status_transitions" else START,
                   "end": END, "rows": rows.get(name, [])}
            for name in SOURCES
        },
    }


def transition(id, at, sequence, status, issue="issue-a"):
    return {"id": id, "issue_id": issue, "at": at,
            "sequence": sequence, "status": status}


def run(id, start, end=None, agent="agent-a"):
    return {"id": id, "agent_id": agent, "start": start, "end": end}


def cap(id="cap-a", start=0, end=END, value=1, agent="agent-a"):
    return {"id": id, "agent_id": agent, "start": start, "end": end, "cap": value}


def review(id, at, assessed=False, endpoint="endpoint-a", repo="12345", pr=7, head=HEAD):
    return {"id": id, "endpoint_id": endpoint, "repository_id": repo,
            "pull_number": pr, "head_sha": head, "at": at, "assessed": assessed}


def failure(id, at=START, failed=True, error="", stderr=""):
    return {"id": id, "at": at, "failed": failed, "error": error, "stderr": stderr}


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def scratch():
    return tempfile.TemporaryDirectory(prefix="convergence-acceptance-",
                                     dir=os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                                     or os.environ.get("PAPERCLIP_SCRATCH_DIR"))


def invoke_path(path, format="json"):
    return subprocess.run(
        [sys.executable, "-B", str(MODULE), "--snapshot", str(path), "--format", format],
        capture_output=True, text=True, timeout=8, cwd=ROOT,
    )


def invoke(value, format="json", raw=False):
    with scratch() as directory:
        path = Path(directory) / "private-synthetic-snapshot.json"
        path.write_bytes(value if raw else encode(value))
        return invoke_path(path, format)


def watch(report, name):
    """Read the public result layout; count expectations remain independent."""
    matches = [row for row in report["results"] if row["watch"] == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name} report, got {matches!r}")
    return matches[0]


class ContractCase(unittest.TestCase):
    def report(self, value, exit=0):
        result = invoke(value)
        self.assertEqual(result.returncode, exit, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def count(self, value, name, expected):
        report = self.report(value, exit=1 if expected else 0)
        self.assertEqual(watch(report, name)["count"], expected)
        return report

    def unknown(self, value, *names):
        report = self.report(value, exit=2)
        for name in WATCH_SOURCES:
            row = watch(report, name)
            if name in names:
                self.assertEqual(row["status"], "unknown", name)
                self.assertIsNone(row["count"], name)
            else:
                self.assertEqual(row["count"], 0, name)
        return report


class DigestContractTests(ContractCase):
    def test_complete_empty_evidence_is_zero_and_not_runtime_qualification(self):
        report = self.report(snapshot())
        self.assertEqual(report["schema"], "paperclip.convergence-report.v1")
        self.assertEqual(len(report["results"]), 5)
        self.assertEqual({r["watch"] for r in report["results"]}, set(WATCH_SOURCES))
        for row in report["results"]:
            self.assertEqual(row["route"], "existing_daily_digest")
        self.assertIs(report["runtime_qualified"], False)
        self.assertEqual(report["artifact"], snapshot()["artifact"])
        for name in WATCH_SOURCES:
            self.assertEqual(watch(report, name)["count"], 0)
        result = invoke(snapshot(), "markdown")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_input_digest_is_of_exact_bytes_not_reserialized_manifest(self):
        value = snapshot()
        first = encode(value)
        second = json.dumps(value, indent=3).encode()
        for data in (first, second):
            with self.subTest(style=len(data)):
                result = invoke(data, raw=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(hashlib.sha256(data).hexdigest(), result.stdout)
                self.assertEqual(json.loads(result.stdout)["artifact"], value["artifact"])

    def test_missing_or_unqualified_source_only_invalidates_dependent_watch(self):
        for source in SOURCES:
            for defect in ("absent", "incomplete", "boundary", "late-start", "early-end"):
                with self.subTest(source=source, defect=defect):
                    value = snapshot()
                    if defect == "absent":
                        del value["sources"][source]
                    elif defect == "incomplete":
                        value["sources"][source]["complete"] = False
                    elif defect == "boundary":
                        value["sources"][source]["boundary_complete"] = False
                    elif defect == "late-start":
                        value["sources"][source]["start"] += 1
                    else:
                        value["sources"][source]["end"] -= 1
                    dependent = [name for name, srcs in WATCH_SOURCES.items() if source in srcs]
                    self.unknown(value, *dependent)

    def test_unknown_does_not_hide_a_measured_finding(self):
        value = snapshot(workspace_observations=[{"id": "obs", "at": START, "coherent": False}])
        del value["sources"]["reviews"]
        report = self.report(value, exit=2)
        self.assertEqual(watch(report, "workspace_incoherence")["count"], 1)
        self.assertEqual(watch(report, "duplicate_review_heads")["status"], "unknown")
        text = invoke(value, "markdown")
        self.assertEqual(text.returncode, 2)
        self.assertIn("workspace_incoherence", text.stdout)
        self.assertIn("duplicate_review_heads", text.stdout)
        self.assertNotIn("rapid_reblock", text.stdout)

    def test_findings_markdown_contains_no_healthy_watches(self):
        value = snapshot(workspace_observations=[{"id": "obs", "at": START, "coherent": False}])
        result = invoke(value, "markdown")
        self.assertEqual(result.returncode, 1)
        self.assertIn("workspace_incoherence", result.stdout)
        for name in WATCH_SOURCES:
            if name != "workspace_incoherence":
                self.assertNotIn(name, result.stdout)

    def test_default_cli_format_is_json(self):
        with scratch() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_bytes(encode(snapshot()))
            result = subprocess.run([sys.executable, "-B", str(MODULE), "--snapshot", str(path)],
                                    capture_output=True, text=True, timeout=8, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema"], "paperclip.convergence-report.v1")


class RapidReblockTests(ContractCase):
    def test_inclusive_lookback_zero_300_and_exclusive_301(self):
        for delay, expected in ((0, 1), (1, 1), (299, 1), (300, 1), (301, 0)):
            with self.subTest(delay=delay):
                at = START + 1
                value = snapshot(status_transitions=[
                    transition("done", at - delay, 1, "done"),
                    transition("blocked", at, 2, "blocked"),
                ])
                self.count(value, "rapid_reblock", expected)
        self.count(snapshot(status_transitions=[
            transition("lookback", START - 300, 1, "done"),
            transition("daily", START, 2, "blocked"),
        ]), "rapid_reblock", 1)

    def test_window_counts_blocked_event_not_done_event(self):
        value = snapshot(status_transitions=[
            transition("old-done", START - 2, 1, "done"),
            transition("old-block", START - 1, 2, "blocked"),
            transition("new-done", END - 1, 3, "done"),
            transition("outside-block", END, 4, "blocked"),
        ])
        self.count(value, "rapid_reblock", 0)

    def test_same_second_sequence_not_input_order_decides_preceding(self):
        cases = [
            ([transition("z-block", START, 2, "blocked"),
              transition("a-done", START, 1, "done")], 1),
            ([transition("b-block", START, 1, "blocked"),
              transition("c-done", START, 2, "done")], 0),
        ]
        # Separate fixtures prevent equal-and-opposite tie-order bugs from
        # cancelling each other in a single aggregate count.
        for rows, expected in cases:
            with self.subTest(blocked_first=rows[0]["sequence"] == 1):
                self.count(snapshot(status_transitions=rows), "rapid_reblock", expected)

    def test_count_events_not_unique_issues_and_do_not_cross_issue(self):
        value = snapshot(status_transitions=[
            transition("d1", START, 1, "done"),
            transition("b1", START + 1, 2, "blocked"),
            transition("d2", START + 2, 3, "done"),
            transition("b2", START + 3, 4, "blocked"),
            transition("other", START + 1, 1, "blocked", "other-issue"),
        ])
        self.count(value, "rapid_reblock", 2)

    def test_invalid_transition_order_or_repeated_status_is_unknown(self):
        cases = [
            [transition("d", START, 1, "done"), transition("b", START, 1, "blocked")],
            [transition("d", START + 1, 1, "done"), transition("b", START, 2, "blocked")],
            [transition("d", START, 1, "done"), transition("d2", START + 1, 2, "done")],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.unknown(snapshot(status_transitions=rows), "rapid_reblock")


class OverCapStartsTests(ContractCase):
    def test_carry_in_half_open_finish_and_tied_starts_are_a_batch(self):
        value = snapshot(run_intervals=[
            run("carry", START - 100, START + 10),
            run("finished", START - 50, START),
            run("start-a", START, START + 1),
            run("start-b", START, START + 2),
            run("safe-at-finish", START + 10, START + 11),
        ], cap_intervals=[cap(value=2)])
        self.count(value, "over_cap_starts", 2)

    def test_finish_at_start_is_not_active_even_at_cap_one(self):
        value = snapshot(run_intervals=[run("finished", START - 1, START),
                                        run("replacement", START, START + 1)],
                         cap_intervals=[cap(value=1)])
        self.count(value, "over_cap_starts", 0)

    def test_exact_cap_is_not_an_exceedance(self):
        self.count(snapshot(run_intervals=[run("a", START), run("b", START)],
                            cap_intervals=[cap(value=2)]), "over_cap_starts", 0)

    def test_tied_starts_all_count_even_when_input_order_is_reversed(self):
        for rows in ([run("z", START), run("a", START), run("m", START)],
                     [run("m", START), run("a", START), run("z", START)]):
            with self.subTest(order=[r["id"] for r in rows]):
                self.count(snapshot(run_intervals=rows, cap_intervals=[cap(value=2)]),
                           "over_cap_starts", 3)

    def test_agents_do_not_share_capacity(self):
        value = snapshot(run_intervals=[run("a", START), run("b", START, agent="agent-b")],
                         cap_intervals=[cap(), cap("cap-b", agent="agent-b")])
        self.count(value, "over_cap_starts", 0)

    def test_effective_historical_cap_changes_at_half_open_boundary(self):
        value = snapshot(run_intervals=[run("carry", START - 5), run("a", START),
                                        run("b", START + 10), run("end", END)],
                         cap_intervals=[cap("old", end=START + 10, value=1),
                                        cap("new", start=START + 10, value=3)])
        self.count(value, "over_cap_starts", 1)

    def test_missing_gap_or_overlap_caps_fail_closed(self):
        cases = [[], [cap(start=START + 1)], [cap(end=START)],
                 [cap("one", end=START + 1), cap("two", start=START)],
                 [cap("one", end=START), cap("two", start=START + 1)]]
        for caps in cases:
            with self.subTest(caps=caps):
                self.unknown(snapshot(run_intervals=[run("daily", START)], cap_intervals=caps),
                             "over_cap_starts")

    def test_nonpositive_or_boolean_cap_and_inverted_intervals_are_unknown(self):
        for invalid in (0, -1, True, 1.0, "1", None):
            with self.subTest(cap=invalid):
                self.unknown(snapshot(run_intervals=[run("a", START)],
                                      cap_intervals=[cap(value=invalid)]), "over_cap_starts")
        self.unknown(snapshot(run_intervals=[run("a", START + 1, START)],
                              cap_intervals=[cap()]), "over_cap_starts")

    def test_random_small_intervals_against_independent_enumeration(self):
        rng = random.Random(729)
        for case in range(16):
            rows = []
            for i in range(12):
                at = START + rng.randrange(-3, 6)
                end = None if i % 4 == 0 else at + rng.randrange(1, 5)
                rows.append(run(str(i), at, end, "agent-" + str(i % 2)))
            caps = [cap("a", value=2, agent="agent-0"), cap("b", value=3, agent="agent-1")]
            expected = 0
            # A hand-specified sweep independent of SQL: membership at each
            # start, including carry-in, excluding half-open finishes.
            for row in rows:
                if not START <= row["start"] < END:
                    continue
                active = sum(other["agent_id"] == row["agent_id"]
                             and other["start"] <= row["start"]
                             and (other["end"] is None or other["end"] > row["start"])
                             for other in rows)
                expected += active > (2 if row["agent_id"] == "agent-0" else 3)
            with self.subTest(case=case):
                self.count(snapshot(run_intervals=rows, cap_intervals=caps),
                           "over_cap_starts", expected)


class WorkspaceTests(ContractCase):
    def test_counts_checks_not_workspaces_and_excludes_daily_boundaries(self):
        rows = [{"id": str(i), "at": at, "coherent": coherent}
                for i, (at, coherent) in enumerate([
                    (START - 1, False), (START, False), (START + 1, False),
                    (START + 2, True), (END - 1, False), (END, False)])]
        self.count(snapshot(workspace_observations=rows), "workspace_incoherence", 3)

    def test_coherence_must_be_boolean_not_truthy_or_missing(self):
        for invalid in (0, 1, "false", "true", None):
            with self.subTest(value=invalid):
                self.unknown(snapshot(workspace_observations=[
                    {"id": "obs", "at": START, "coherent": invalid}]), "workspace_incoherence")


class DuplicateReviewTests(ContractCase):
    def test_historical_siblings_assessed_unassessed_and_group_count(self):
        rows = [review("old-assessed", 1, True), review("old-unassessed", 2),
                review("today", START, True), review("new-a", START, pr=8),
                review("new-b", END - 1, pr=8), review("quiet-a", 1, pr=9),
                review("quiet-b", 2, pr=9)]
        report = self.count(snapshot(reviews=rows), "duplicate_review_heads", 2)
        row = watch(report, "duplicate_review_heads")
        # Across the two touched duplicate groups: 2 assessed + 3 unassessed.
        self.assertEqual(row["assessed_rows"], 2)
        self.assertEqual(row["unassessed_rows"], 3)

    def test_exact_endpoint_repository_pr_and_full_head_form_group_key(self):
        rows = [review("seed", START), review("other-endpoint", START, endpoint="endpoint-b"),
                review("other-repo", START, repo="67890"), review("other-pr", START, pr=8),
                review("other-head", START, head="f" + HEAD[1:]),
                review("end-only-a", END, pr=9), review("end-only-b", END, pr=9)]
        self.count(snapshot(reviews=rows), "duplicate_review_heads", 0)

    def test_all_unassessed_duplicates_are_still_reported(self):
        report = self.count(snapshot(reviews=[review("a", 1), review("b", START)]),
                            "duplicate_review_heads", 1)
        row = watch(report, "duplicate_review_heads")
        self.assertEqual((row["assessed_rows"], row["unassessed_rows"]), (0, 2))

    def test_assessment_presence_is_boolean_not_score_or_state(self):
        for invalid in (None, 0, 5, "completed", {}, []):
            with self.subTest(assessed=invalid):
                self.unknown(snapshot(reviews=[review("a", START, invalid)]), "duplicate_review_heads")

    def test_heads_must_be_exact_full_lowercase_hex(self):
        for invalid in (HEAD[:7], HEAD.upper(), "g" * 40, "", HEAD + "a"):
            with self.subTest(head=invalid):
                self.unknown(snapshot(reviews=[review("a", START, head=invalid)]),
                             "duplicate_review_heads")


class StdinSignatureTests(ContractCase):
    def test_failed_error_or_stderr_case_insensitive_counted_once(self):
        rows = [failure("a", error="prefix NO STDIN DATA RECEIVED IN 3S suffix"),
                failure("b", stderr="no stdin data received in 3s"),
                failure("c", error="no stdin data received in 3s",
                        stderr="no stdin data received in 3s"),
                failure("success", failed=False, error="no stdin data received in 3s"),
                failure("epipe", error="EPIPE"), failure("timeout", error="timeout after 3s"),
                failure("near", error="no stdin data received in 30s"),
                failure("old", START - 1, error="no stdin data received in 3s"),
                failure("end", END, error="no stdin data received in 3s")]
        self.count(snapshot(run_failures=rows), "stdin_deadline_failures", 3)

    def test_exporter_normalized_absent_text_is_empty_not_a_failure_signature(self):
        # The export table requires exact keys. Normalizing absent upstream
        # text is the collector's responsibility, not evidence it can omit
        # those required keys from its qualified export.
        self.count(snapshot(run_failures=[failure("empty", error="", stderr="")]),
                   "stdin_deadline_failures", 0)

    def test_omitted_required_error_keys_are_unknown_not_silently_healthy(self):
        for missing in (("error",), ("stderr",), ("error", "stderr")):
            with self.subTest(missing=missing):
                row = failure("missing-text")
                for field in missing:
                    del row[field]
                self.unknown(snapshot(run_failures=[row]), "stdin_deadline_failures")

    def test_nonboolean_failed_or_nonstring_text_is_unknown(self):
        for field, invalid in (("failed", 1), ("failed", "failed"),
                               ("error", None), ("stderr", [])):
            with self.subTest(field=field, invalid=invalid):
                row = failure("a")
                row[field] = invalid
                self.unknown(snapshot(run_failures=[row]), "stdin_deadline_failures")


class InvalidManifestTests(ContractCase):
    def invalid(self, value, raw=False):
        result = invoke(value, raw=raw)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn("private-synthetic-snapshot", result.stdout + result.stderr)

    def test_unknown_missing_or_malformed_root_fields_fail_closed(self):
        for field in snapshot():
            with self.subTest(missing=field):
                value = snapshot()
                del value[field]
                self.invalid(value)
        for field, bad in (("schema", "paperclip.convergence-export.v2"),
                           ("company_id", "not-a-uuid"), ("artifact", {}),
                           ("start", True), ("end", END + 1), ("start", 10000.0),
                           ("start", -1), ("sources", [])):
            with self.subTest(field=field, bad=bad):
                value = snapshot()
                value[field] = bad
                self.invalid(value)
        value = snapshot()
        value["command"] = "repair"
        self.invalid(value)

    def test_artifact_is_pinned_and_immutable_shape_is_strict(self):
        for field, bad in (("base_sha", "f" * 40), ("source_sha", "latest"),
                           ("image_digest", "latest"), ("source_sha", SOURCE.upper())):
            with self.subTest(field=field, bad=bad):
                value = snapshot()
                value["artifact"][field] = bad
                self.invalid(value)
        value = snapshot()
        value["artifact"]["extra"] = "not-allowed"
        self.invalid(value)

    def test_duplicate_json_keys_nan_invalid_utf8_trailing_and_nonobject_rejected(self):
        valid = encode(snapshot())
        cases = [b'{"schema":"x","schema":"paperclip.convergence-export.v1"}',
                 valid.replace(b'"complete":true', b'"complete":true,"complete":true', 1),
                 valid.replace(b'"start":10000', b'"start":NaN', 1),
                 valid.replace(b'"start":10000', b'"start":Infinity', 1),
                 b'\xff', valid + b'{}', b'[]', b'null', b'{']
        for data in cases:
            with self.subTest(data=data[:80]):
                self.invalid(data, raw=True)

    def test_unknown_source_names_rejected(self):
        value = snapshot()
        value["sources"]["unreviewed_query"] = copy.deepcopy(value["sources"]["reviews"])
        self.invalid(value)

    def test_malformed_source_manifest_only_invalidates_its_watch(self):
        for defect in ("extra", "complete-int", "boundary-int", "rows-object", "missing-end"):
            with self.subTest(defect=defect):
                value = snapshot()
                source = value["sources"]["reviews"]
                if defect == "extra":
                    source["extra"] = 1
                elif defect == "complete-int":
                    source["complete"] = 1
                elif defect == "boundary-int":
                    source["boundary_complete"] = 1
                elif defect == "rows-object":
                    source["rows"] = {}
                else:
                    del source["end"]
                self.unknown(value, "duplicate_review_heads")

    def test_duplicate_ids_and_unknown_row_keys_are_not_silently_dropped(self):
        examples = {
            "status_transitions": transition("dup", START, 1, "done"),
            "run_intervals": run("dup", START),
            "cap_intervals": cap(),
            "workspace_observations": {"id": "dup", "at": START, "coherent": True},
            "reviews": review("dup", START),
            "run_failures": failure("dup"),
        }
        for source, example in examples.items():
            name = next(name for name, srcs in WATCH_SOURCES.items() if source in srcs)
            for kind in ("duplicate", "extra"):
                with self.subTest(source=source, kind=kind):
                    rows = [copy.deepcopy(example), copy.deepcopy(example)]
                    if kind == "extra":
                        rows = [dict(example, unreviewed_field="not-allowed")]
                    self.unknown(snapshot(**{source: rows}), name)

    def test_boolean_numeric_fields_fail_closed(self):
        examples = [
            ("status_transitions", transition("a", True, 1, "done")),
            ("status_transitions", transition("a", START, True, "done")),
            ("run_intervals", run("a", True)),
            ("run_intervals", run("a", START, True)),
            ("cap_intervals", cap(value=True)),
            ("workspace_observations", {"id": "a", "at": True, "coherent": True}),
            ("reviews", review("a", START, pr=True)),
            ("run_failures", failure("a", True)),
        ]
        for source, row in examples:
            with self.subTest(source=source, row=row):
                name = next(name for name, srcs in WATCH_SOURCES.items() if source in srcs)
                self.unknown(snapshot(**{source: [row]}), name)

    def test_row_limit_5000_accepted_5001_unknown_without_partial_zero(self):
        for n, exit in ((5000, 0), (5001, 2)):
            with self.subTest(rows=n):
                rows = [{"id": str(i), "at": START, "coherent": True} for i in range(n)]
                value = snapshot(workspace_observations=rows)
                if exit:
                    self.unknown(value, "workspace_incoherence")
                else:
                    self.count(value, "workspace_incoherence", 0)

    def test_input_8_mib_limit_is_inclusive_not_truncated(self):
        data = encode(snapshot())
        for size, exit in ((8 * 1024 * 1024, 0), (8 * 1024 * 1024 + 1, 2)):
            with self.subTest(size=size):
                padded = data + b' ' * (size - len(data))
                result = invoke(padded, raw=True)
                self.assertEqual(result.returncode, exit, result.stdout + result.stderr)


class PrivacyAndLocalFileTests(ContractCase):
    def test_identifiers_error_text_and_path_never_appear_in_outputs(self):
        marker = "SYNTHETIC-PRIVATE-DO-NOT-EMIT"
        value = snapshot(
            status_transitions=[transition(marker + "-done", START, 1, "done", marker),
                                transition(marker + "-block", START + 1, 2, "blocked", marker)],
            run_intervals=[run(marker + "-a", START, agent=marker),
                           run(marker + "-b", START, agent=marker)],
            cap_intervals=[cap(marker + "-cap", agent=marker)],
            workspace_observations=[{"id": marker + "-obs", "at": START, "coherent": False}],
            reviews=[review(marker + "-r1", 1, endpoint=marker, repo=marker),
                     review(marker + "-r2", START, endpoint=marker, repo=marker)],
            run_failures=[failure(marker, error=marker + " no stdin data received in 3s")],
        )
        for format in ("json", "markdown"):
            with self.subTest(format=format):
                result = invoke(value, format)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(marker, result.stdout + result.stderr)
                self.assertNotIn("no stdin data received", result.stdout + result.stderr)
                self.assertNotIn("private-synthetic-snapshot", result.stdout + result.stderr)
        value["sources"]["reviews"]["rows"][0]["head_sha"] = marker
        result = invoke(value)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_fifo_without_writer_cannot_hang(self):
        with scratch() as directory:
            path = Path(directory) / "private-fifo"
            os.mkfifo(path)
            result = invoke_path(path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("private-fifo", result.stdout + result.stderr)

    def test_symlinks_and_devices_and_sockets_rejected(self):
        with scratch() as directory:
            base = Path(directory)
            regular = base / "data.json"
            regular.write_bytes(encode(snapshot()))
            link = base / "private-link"
            link.symlink_to(regular)
            paths = [link, Path("/dev/null"), base]
            with socket.socket(socket.AF_UNIX) as sock:
                sockpath = base / "private-socket"
                sock.bind(str(sockpath))
                paths.append(sockpath)
                for path in paths:
                    with self.subTest(kind=path.name):
                        result = invoke_path(path)
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertNotIn(str(path), result.stdout + result.stderr)

    def test_missing_file_refuses_without_echoing_input_path(self):
        with scratch() as directory:
            path = Path(directory) / "SYNTHETIC-PRIVATE-MISSING"
            result = invoke_path(path)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(str(path), result.stdout + result.stderr)


class AdditionalFailClosedTests(ContractCase):
    def test_every_source_rejects_5001_rows_without_truncating(self):
        examples = {
            "status_transitions": transition("row", START, 1, "done"),
            "run_intervals": run("row", START),
            "cap_intervals": cap(),
            "workspace_observations": {"id": "row", "at": START, "coherent": True},
            "reviews": review("row", START),
            "run_failures": failure("row"),
        }
        for source, example in examples.items():
            with self.subTest(source=source):
                rows = [dict(example, id=str(i)) for i in range(5001)]
                name = next(name for name, srcs in WATCH_SOURCES.items() if source in srcs)
                self.unknown(snapshot(**{source: rows}), name)

    def test_missing_required_row_fields_and_empty_grouping_keys_are_unknown(self):
        examples = {
            "status_transitions": transition("row", START, 1, "done"),
            "run_intervals": run("row", START),
            "cap_intervals": cap(),
            "workspace_observations": {"id": "row", "at": START, "coherent": True},
            "reviews": review("row", START),
            "run_failures": failure("row"),
        }
        for source, example in examples.items():
            name = next(name for name, srcs in WATCH_SOURCES.items() if source in srcs)
            for field in example:
                with self.subTest(source=source, missing=field):
                    row = dict(example)
                    del row[field]
                    self.unknown(snapshot(**{source: [row]}), name)
            for field in ("id", "issue_id", "agent_id", "endpoint_id", "repository_id"):
                if field not in example:
                    continue
                with self.subTest(source=source, empty=field):
                    row = dict(example, **{field: ""})
                    self.unknown(snapshot(**{source: [row]}), name)

    def test_epoch_window_never_requires_negative_lookback(self):
        value = snapshot(status_transitions=[transition("d", 0, 1, "done"),
                                             transition("b", 0, 2, "blocked")])
        value["start"], value["end"] = 0, 86400
        for source in value["sources"].values():
            source["start"], source["end"] = 0, 86400
        self.count(value, "rapid_reblock", 1)

    def test_cap_overlap_is_unknown_even_if_no_daily_start_happens_inside_overlap(self):
        value = snapshot(run_intervals=[run("r", START + 10, START + 11)],
                         cap_intervals=[cap("a", start=0, end=START + 2),
                                        cap("b", start=START + 1)])
        self.unknown(value, "over_cap_starts")


if __name__ == "__main__":
    unittest.main()
