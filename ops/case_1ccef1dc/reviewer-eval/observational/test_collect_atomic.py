"""Offline atomic-write suite for observational collect.write_dataset.

WHAT THIS PROTECTS. observational/collect.py persisted its evidence dataset with
``json.dump(..., open(path, "w"))``: an ENOSPC mid-write leaves a truncated
dataset.json that reads as data. A truncation silently corrupts observational
evidence. The writer goes through ``ops/lib/atomic_state.write_json_atomic``
(same-dir temp, fsync, atomic rename) and a failing write names the path on
stderr and exits non-zero.

THE TRAP IN TESTING. A suite that only checks the happy path stays green when
the atomic call is reverted to a plain open, so every assertion here is paired
with a failure-injection case: ENOSPC at fsync and at replace must raise
SystemExit(2) with the previous bytes intact and no temp file left behind. A
plain ``open(path, "w")`` never reaches os.replace, so the reverted line fails
the raises assertion (and would already have truncated the file). The happy
path proves the dataset still lands as valid JSON. The main() cases cover the
call site: a revert inside main bypasses os.replace the same way.

Offline and credential-free by construction: tempfile dirs, fabricated runs
rows and issue payloads, ENOSPC injected with mocks. No Paperclip API, no
network, no PAPERCLIP_API_KEY.
"""

import contextlib
import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "lib"))

import atomic_state  # noqa: E402

import collect  # noqa: E402

DATASET = {"runs": {"r1": {"model": "model-a", "issue": "i1"}},
           "issues": {"i1": {"ident": "X-1", "comments": []}}}

RUNS = [{"id": "r1",
         "usageJson": {"model": "model-a (high)", "inputTokens": 100,
                       "outputTokens": 10, "costUsd": 0.01},
         "startedAt": "2026-10-03T00:00:00Z",
         "finishedAt": "2026-10-03T00:02:00Z",
         "status": "succeeded",
         "contextSnapshot": {"issueId": "i1"}}]

ISSUE_PAYLOAD = ("i1", {"ident": "X-1", "title": "Review PR #1",
                        "status": "done", "comments": []})


class WriteDataset(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "dataset.json"

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_happy_path_writes_valid_json_and_leaves_no_temp(self):
        self.path.write_bytes(b'{"old": true}\n')
        collect.write_dataset(str(self.path), DATASET)
        self.assertEqual(json.loads(self.path.read_text()), DATASET)
        self.assertEqual(self.names(), ["dataset.json"])

    def test_enospc_at_fsync_or_replace_exits_nonzero_and_preserves_bytes(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.path.write_bytes(b'previous-bytes\n')
                failure = OSError(errno.ENOSPC, "No space left on device")
                err = io.StringIO()
                with mock.patch.object(atomic_state.os, operation,
                                        side_effect=failure):
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            collect.write_dataset(str(self.path), DATASET)
                self.assertEqual(cm.exception.code, 2)
                self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
                self.assertEqual(self.names(), ["dataset.json"])
                self.assertIn("dataset.json", err.getvalue())

    def test_failing_write_names_path_with_errno(self):
        self.path.write_bytes(b"previous-bytes\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        err = io.StringIO()
        with mock.patch.object(atomic_state.os, "replace",
                                side_effect=failure):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    collect.write_dataset(str(self.path), DATASET)
        self.assertIn(str(self.path), err.getvalue())
        self.assertIn(str(errno.ENOSPC), err.getvalue())

    def write_runs(self):
        runs_path = self.dir / "runs.json"
        runs_path.write_text(json.dumps(RUNS))
        return runs_path

    def test_main_happy_path_writes_valid_json_and_leaves_no_temp(self):
        runs_path = self.write_runs()
        out_path = self.dir / "dataset.json"
        out_path.write_bytes(b'{"old": true}\n')
        with mock.patch.object(collect, "one", return_value=ISSUE_PAYLOAD):
            self.assertEqual(
                collect.main([str(runs_path), str(out_path)]), 0)
        ds = json.loads(out_path.read_text())
        self.assertIn("r1", ds["runs"])
        self.assertIn("i1", ds["issues"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["dataset.json", "runs.json"])

    def test_main_write_failure_preserves_bytes_and_exits_nonzero(self):
        # Covers main()'s call site: a revert to plain open() bypasses
        # os.replace, so the mocked failure never fires, main() returns 0,
        # and this test goes red.
        runs_path = self.write_runs()
        out_path = self.dir / "dataset.json"
        out_path.write_bytes(b"previous-bytes\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        err = io.StringIO()
        with mock.patch.object(collect, "one", return_value=ISSUE_PAYLOAD):
            with mock.patch.object(atomic_state.os, "replace",
                                   side_effect=failure):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        collect.main([str(runs_path), str(out_path)])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(out_path.read_bytes(), b"previous-bytes\n")
        self.assertIn("dataset.json", err.getvalue())


class ReviewerSelection(unittest.TestCase):
    def test_missing_or_empty_identity_refuses_before_network(self):
        for env in ({}, {"PAPERCLIP_REVIEWER_AGENT_ID": "   "}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(collect, "get") as get:
                with self.assertRaises(ValueError):
                    collect.one("i1")
                get.assert_not_called()

    def test_only_explicit_reviewer_comments_are_selected(self):
        comments = [
            {"id": "c1", "createdAt": "2026-10-03T00:00:00Z", "authorAgentId": "fixture-reviewer", "body": "Review 1111111: APPROVE"},
            {"id": "c2", "createdAt": "2026-10-03T00:00:00Z", "authorAgentId": "another-agent", "body": "not this reviewer"},
        ]
        with mock.patch.dict(os.environ, {"PAPERCLIP_REVIEWER_AGENT_ID": "fixture-reviewer"}, clear=True), \
                mock.patch.object(collect, "get", side_effect=[{"identifier": "X-1"}, comments]) as get:
            ident, data = collect.one("i1")
        self.assertEqual(ident, "i1")
        self.assertEqual([c["id"] for c in data["comments"]], ["c1"])
        self.assertEqual(get.call_args_list, [mock.call("/api/issues/i1"), mock.call("/api/issues/i1/comments?order=desc&limit=100")])


if __name__ == "__main__":
    unittest.main()
