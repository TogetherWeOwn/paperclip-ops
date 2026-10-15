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


# Canonical synthetic cursors, including equal timestamps, match the API contract.
def comment(number, reviewer="another-agent"):
    return {"id": "00000000-0000-4000-8000-%012d" % number,
            "createdAt": "2026-10-03T00:00:00Z", "authorAgentId": reviewer,
            "body": "synthetic verdict"}


API_ISSUE = {"identifier": "X-1", "title": "Review fixture", "status": "done"}
REVIEWER_ENV = {"PAPERCLIP_REVIEWER_AGENT_ID": "fixture-reviewer"}


class CollectionFailures(WriteDataset):
    def test_fetch_retries_and_never_exposes_http_or_json_contents(self):
        from urllib.error import HTTPError, URLError

        for mode in ("http", "transport", "json"):
            with self.subTest(mode=mode):
                marker = "sensitive-fixture-do-not-display"
                if mode == "http":
                    replies = [HTTPError("https://example.invalid", 401, marker,
                                         {}, io.BytesIO(marker.encode())) for _ in range(3)]
                elif mode == "transport":
                    replies = [URLError(marker)] * 3
                else:
                    replies = [io.BytesIO(marker.encode()) for _ in range(3)]
                with mock.patch.dict(os.environ, {
                        "PAPERCLIP_API_URL": "https://example.invalid",
                        "PAPERCLIP_API_KEY": "fixture-only"}, clear=True), \
                        mock.patch.object(collect.urllib.request, "urlopen", side_effect=replies) as fetch:
                    with self.assertRaises(collect.CollectionError) as caught:
                        collect.get("/api/issues/i1")
                self.assertEqual(fetch.call_count, 3)
                self.assertNotIn(marker, str(caught.exception))
                self.assertIn("3 attempts", str(caught.exception))

    def test_transient_fetch_failure_then_success(self):
        with mock.patch.dict(os.environ, {
                "PAPERCLIP_API_URL": "https://example.invalid/api",
                "PAPERCLIP_API_KEY": "fixture-only"}, clear=True), \
                mock.patch.object(collect.urllib.request, "urlopen", side_effect=[
                    OSError("transient"), io.BytesIO(b'{"ok": true}')]) as fetch:
            self.assertEqual(collect.get("/api/issues/i1"), {"ok": True})
        self.assertEqual(fetch.call_count, 2)

    def test_failed_fetch_at_any_stage_preserves_previous_dataset(self):
        full = [comment(i) for i in range(1, 101)]
        for replies in ([collect.CollectionError("fetch failed")],
                        [API_ISSUE, collect.CollectionError("fetch failed")],
                        [API_ISSUE, full, collect.CollectionError("fetch failed")],
                        [None], [API_ISSUE, None], [API_ISSUE, full, full]):
            with self.subTest(stage=len(replies)):
                runs_path = self.write_runs()
                self.path.write_bytes(b"previous-bytes\n")
                err = io.StringIO()
                with mock.patch.dict(os.environ, REVIEWER_ENV, clear=True), \
                        mock.patch.object(collect, "get", side_effect=replies), \
                        mock.patch.object(collect, "write_dataset", wraps=collect.write_dataset) as writer, \
                        contextlib.redirect_stderr(err):
                    self.assertEqual(collect.main([str(runs_path), str(self.path)]), 2)
                writer.assert_not_called()
                self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
                self.assertEqual(self.names(), ["dataset.json", "runs.json"])
                self.assertIn("previous dataset preserved", err.getvalue())

    def test_one_successful_issue_does_not_publish_a_partial_dataset(self):
        runs = RUNS + [dict(RUNS[0], id="r2", contextSnapshot={"issueId": "i2"})]
        runs_path = self.dir / "runs.json"
        runs_path.write_text(json.dumps(runs))
        self.path.write_bytes(b"previous-bytes\n")

        def fetch_one(ident):
            if ident == "i1":
                return ISSUE_PAYLOAD
            raise collect.CollectionError("fetch failed")

        with mock.patch.object(collect, "one", side_effect=fetch_one), \
                mock.patch.object(collect, "write_dataset", wraps=collect.write_dataset) as writer:
            self.assertEqual(collect.main([str(runs_path), str(self.path)]), 2)
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")

    def test_http_failure_in_main_is_nonzero_sanitized_and_preserves_bytes(self):
        from urllib.error import HTTPError

        runs_path = self.write_runs()
        self.path.write_bytes(b"previous-bytes\n")
        err = io.StringIO()
        marker = "sensitive-fixture-do-not-display"
        env = dict(REVIEWER_ENV, PAPERCLIP_API_URL="https://example.invalid",
                   PAPERCLIP_API_KEY="fixture-only")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(collect.urllib.request, "urlopen", side_effect=
                    HTTPError("https://example.invalid", 401, marker, {}, None)) as fetch, \
                contextlib.redirect_stderr(err):
            self.assertEqual(collect.main([str(runs_path), str(self.path)]), 2)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
        self.assertNotIn(marker, err.getvalue())


class CommentPagination(unittest.TestCase):
    def collect_with(self, replies):
        with mock.patch.dict(os.environ, REVIEWER_ENV, clear=True), \
                mock.patch.object(collect, "get", side_effect=replies):
            return collect.one("i1")

    def test_valid_empty_comments_are_successful(self):
        self.assertEqual(self.collect_with([API_ISSUE, []])[1]["comments"], [])

    def test_invalid_issue_and_comment_shapes_refuse(self):
        for issue in (None, [], {}, {"error": "unauthorized"},
                      dict(API_ISSUE, title=None), dict(API_ISSUE, status="")):
            with self.subTest(issue=issue), self.assertRaises(collect.CollectionError):
                self.collect_with([issue, []])
        for page in (None, {}, {"error": "unauthorized"}, [None], [{}],
                     [dict(comment(1), id="not-a-uuid")],
                     [dict(comment(1), createdAt=None)],
                     [dict(comment(1), body=None)],
                     [dict(comment(1), authorAgentId=[])],
                     [dict(comment(1), createdByRunId={})]):
            with self.subTest(page=page), self.assertRaises(collect.CollectionError):
                self.collect_with([API_ISSUE, page])

    def test_older_reviewer_verdict_survives_full_nonreviewer_page(self):
        page = [comment(i) for i in range(1, 101)]
        older = comment(101, "fixture-reviewer")
        with mock.patch.dict(os.environ, REVIEWER_ENV, clear=True), \
                mock.patch.object(collect, "get", side_effect=[API_ISSUE, page, [older]]) as get:
            data = collect.one("i1")[1]
        self.assertEqual([c["id"] for c in data["comments"]], [older["id"]])
        self.assertEqual(get.call_args_list[-1], mock.call(
            "/api/issues/i1/comments?order=desc&limit=100&after=" + page[-1]["id"]))

    def test_exact_full_page_requires_a_terminal_request(self):
        page = [comment(i, "fixture-reviewer") for i in range(1, 101)]
        with mock.patch.dict(os.environ, REVIEWER_ENV, clear=True), \
                mock.patch.object(collect, "get", side_effect=[API_ISSUE, page, []]) as get:
            data = collect.one("i1")[1]
        self.assertEqual(len(data["comments"]), 100)
        self.assertEqual(get.call_count, 3)

    def test_duplicate_or_oversized_pages_refuse(self):
        full = [comment(i) for i in range(1, 101)]
        for pages in ([full, full], [[comment(1), comment(1)]],
                      [full, [comment(1)]], [full + [comment(101)]]):
            with self.subTest(lengths=[len(p) for p in pages]), \
                    self.assertRaises(collect.CollectionError):
                self.collect_with([API_ISSUE, *pages])

    def test_safety_bound_exhaustion_is_not_partial_success(self):
        with mock.patch.object(collect, "MAX_COMMENT_PAGES", 1), \
                self.assertRaises(collect.CollectionError):
            self.collect_with([API_ISSUE, [comment(i) for i in range(1, 101)]])


class ReviewerSelection(unittest.TestCase):
    def test_missing_or_empty_identity_refuses_before_network(self):
        for env in ({}, {"PAPERCLIP_REVIEWER_AGENT_ID": "   "}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(collect, "get") as get:
                with self.assertRaises(ValueError):
                    collect.one("i1")
                get.assert_not_called()

    def test_only_explicit_reviewer_comments_are_selected(self):
        comments = [comment(1, "fixture-reviewer"), comment(2)]
        with mock.patch.dict(os.environ, REVIEWER_ENV, clear=True), \
                mock.patch.object(collect, "get", side_effect=[API_ISSUE, comments]) as get:
            ident, data = collect.one("i1")
        self.assertEqual(ident, "i1")
        self.assertEqual([c["id"] for c in data["comments"]], [comments[0]["id"]])
        self.assertEqual(get.call_args_list, [mock.call("/api/issues/i1"), mock.call("/api/issues/i1/comments?order=desc&limit=100")])


if __name__ == "__main__":
    unittest.main()
