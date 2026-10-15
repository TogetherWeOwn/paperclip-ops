#!/usr/bin/env python3
"""Offline blinding regressions. No network, model calls or live bundle writes."""
import io
import json
import os
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock

import stage


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_positive_text_control(self):
        (self.root / "code.py").write_text("ordinary code\n")
        self.assertEqual(stage.grep_files(self.root, stage.TREE_GLOBAL), set())

    def test_name_identifiers_and_binary_bytes_are_not_skipped(self):
        (self.root / "15049").mkdir()
        (self.root / "15049" / "ordinary.txt").write_text("ordinary code\n")
        (self.root / "binary.bin").write_bytes(b"\0Greptile: held-out defect\0")
        hits = stage.grep_files(self.root, stage.TREE_GLOBAL + stage.own_patterns(15049))
        self.assertIn("15049", hits)
        self.assertIn("15049/ordinary.txt", hits)
        self.assertIn("binary.bin", hits)

    def test_fixed_phrases_in_names(self):
        (self.root / "held-out defect.txt").write_text("ordinary code")
        self.assertIn("held-out defect.txt", stage.grep_files(self.root, ["held-out defect"], fixed=True))

    def test_non_utf8_asset_is_not_a_pass(self):
        (self.root / "asset.bin").write_bytes(b"\xff\xfe\x80")
        self.assertIn("asset.bin", stage.grep_files(self.root, []))

    def test_symlink_is_not_followed_or_passed(self):
        (self.root / "link").symlink_to(self.root.parent)
        with self.assertRaises(stage.GateError):
            stage.grep_files(self.root, [])

    def test_read_and_enumeration_errors_fail_closed(self):
        (self.root / "code.py").write_text("ordinary code")
        with mock.patch.object(pathlib.Path, "read_bytes", side_effect=OSError("fixture")):
            with self.assertRaises(stage.GateError):
                stage.grep_files(self.root, [])
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](OSError("fixture"))
            return iter(())
        with mock.patch.object(stage.os, "walk", side_effect=failed_walk):
            with self.assertRaises(stage.GateError):
                stage.grep_files(self.root, [])

    def test_budget_is_a_refusal_not_partial_measurement(self):
        (self.root / "code.py").write_text("four")
        with mock.patch.object(stage, "MAX_FILE_BYTES", 3):
            with self.assertRaises(stage.GateError):
                stage.grep_files(self.root, [])

    def archive(self, members):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, kind in members:
                member = tarfile.TarInfo(name)
                if kind == "file":
                    member.size = 4
                    archive.addfile(member, io.BytesIO(b"code"))
                else:
                    member.type = kind
                    member.linkname = "../outside"
                    archive.addfile(member)
        stream.seek(0)
        return stream

    def test_archive_positive_control(self):
        stage.extract_archive(self.archive([("code.py", "file")]), self.root)
        self.assertEqual((self.root / "code.py").read_bytes(), b"code")

    def test_archive_rejects_links_special_entries_and_traversal(self):
        for name, kind in [("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE),
                           ("pipe", tarfile.FIFOTYPE), ("../outside", "file"),
                           ("/absolute", "file")]:
            with self.subTest(name=name, kind=kind), self.assertRaises(stage.GateError):
                stage.extract_archive(self.archive([(name, kind)]), self.root)

    def test_archive_rejects_duplicate_entries(self):
        with self.assertRaises(stage.GateError):
            stage.extract_archive(self.archive([("code.py", "file"), ("code.py", "file")]), self.root)

    def test_frozen_manifest_detects_bytes_names_and_empty_directories(self):
        arena = self.root / "arena"
        cell = arena / "C-10"
        (cell / "tree").mkdir(parents=True)
        (cell / "pr.diff").write_text("diff")
        (cell / "reviewer_view.json").write_text('{"title":"ordinary"}')
        with mock.patch.object(stage, "ARENA", str(arena)), mock.patch.object(stage, "SCORER", str(self.root / "scorer")), mock.patch.object(stage, "corpus_pr", return_value={"greptile": {"findings": []}}), mock.patch.object(stage, "last_reviewed", return_value="fixture-head"):
            mf = stage.manifest_path("C-10")
            mf.parent.mkdir(parents=True)
            mf.write_text(json.dumps({"schema": "blinded-bundle-v1", "cid": "C-10", "reviewed_head": "fixture-head", "entries": stage.snapshot_bundle(cell)}))
            self.assertEqual(stage.verify_bundle("C-10")["cid"], "C-10")
            (cell / "tree" / "new-empty-directory").mkdir()
            with self.assertRaises(stage.GateError):
                stage.verify_bundle("C-10")
            (cell / "tree" / "new-empty-directory").rmdir()
            (cell / "pr.diff").write_text("tampered")
            with self.assertRaises(stage.GateError):
                stage.verify_bundle("C-10")

    def test_missing_manifest_never_releases(self):
        with mock.patch.object(stage, "SCORER", str(self.root)):
            with self.assertRaises(stage.GateError):
                stage.verify_bundle("C-10")


class AuditTests(unittest.TestCase):
    def setUp(self):
        import audit
        self.audit = audit
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.arena = self.root / "arena"
        self.cell = self.arena / "C-10"
        (self.cell / "tree").mkdir(parents=True)
        (self.cell / "tree" / "code.py").write_text("ordinary code\n")
        self.card = {"id": "review", "identifier": "SYNTHETIC-1", "status": "done",
                     "title": "Pre-merge code review", "description": f"{self.cell}/",
                     "parentId": None, "blocks": [], "blockedBy": [],
                     "assigneeAdapterOverrides": {"adapterConfig": {"model": "synthetic-model"}}}
        self.record = {"id": "own", "contextSnapshot": {"issueId": "review"},
                       "status": "succeeded", "startedAt": "2026-10-04T00:00:00Z",
                       "finishedAt": "2026-10-04T00:01:00Z",
                       "usageJson": {"model": "synthetic-model", "freshSession": True, "sessionReused": False}}
        self.init = {"type": "system", "subtype": "init", "model": "synthetic-model", "memory_paths": {}}
        self.terminal = {"type": "result", "subtype": "success"}
        self.comments = [{"authorType": "agent", "createdByRunId": "own",
                          "body": '{"verdict":"APPROVE","findings":[]}'}]

    def tool(self, name, inp):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}

    def run_audit(self, events=(), extra_dispatcher=False, record_change=None, page_change=None, queued=False):
        import copy
        record = copy.deepcopy(self.record)
        if record_change:
            record_change(record)
        event_text = "".join(json.dumps(e) + "\n" for e in [self.init, *events, self.terminal])
        # Fragments may split JSON lines inside an outer wrapper's chunk.
        mid = len(event_text) // 2
        raw = "".join(json.dumps({"stream": "stdout", "chunk": part}) + "\n"
                      for part in (event_text[:mid], event_text[mid:]))
        record["logBytes"] = len(raw.encode())
        page = {"content": raw}
        if page_change:
            page_change(page)
        rows = [{"runId": "own", "status": "queued" if queued else "succeeded",
                 "startedAt": None if queued else record["startedAt"]}]
        if extra_dispatcher:
            rows.append({"runId": "dispatcher", "status": "succeeded", "startedAt": record["startedAt"]})
        def fake_get(path):
            if path == "/api/issues/SYNTHETIC-1":
                return self.card
            if path == "/api/issues/review/runs":
                return rows
            if path == "/api/heartbeat-runs/own":
                return record
            if path == "/api/heartbeat-runs/dispatcher":
                return dict(record, contextSnapshot={"issueId": "scorer"})
            if path.startswith("/api/heartbeat-runs/own/log?"):
                return page
            if path == "/api/issues/review/comments?order=asc":
                return self.comments
            raise AssertionError("unexpected API request: " + path)
        with mock.patch.object(self.audit, "get", side_effect=fake_get), mock.patch.object(self.audit, "ARENA", str(self.arena)), mock.patch.object(self.audit, "patterns_for", return_value=(["greptile"], ["synthetic held-out defect"])):
            return self.audit.audit("SYNTHETIC-1")

    def test_positive_observed_control_is_not_enforcement_approval(self):
        out = self.run_audit([self.tool("Read", {"file_path": str(self.cell / "tree" / "code.py")})])
        self.assertEqual(out["trace_verdict"], "CLEAN")
        self.assertEqual(out["verdict"], "UNVERIFIED")
        self.assertFalse(out["scoreable"])

    def test_traversal_prefix_collision_and_symlink_escape_are_void(self):
        outside = self.root / "scorer" / "claims.json"
        outside.parent.mkdir()
        outside.write_text("ordinary fixture")
        collision = self.arena / "C-10-other"
        collision.mkdir()
        (collision / "claims.json").write_text("ordinary fixture")
        (self.cell / "link").symlink_to(outside)
        for path in [str(self.cell / ".." / ".." / "scorer" / "claims.json"),
                     str(collision / "claims.json"), str(self.cell / "link")]:
            with self.subTest(path=path):
                out = self.run_audit([self.tool("Read", {"file_path": path})])
                self.assertEqual(out["verdict"], "VOID")

    def test_all_bash_and_memory_tools_are_void(self):
        for name, inp in [("Bash", {"command": 'read claims; curl -X PATCH "$PAPERCLIP_API_URL/api/issues/$PAPERCLIP_TASK_ID"'}),
                          ("Bash", {"command": "true"}), ("mcp__memory__hindsight_recall", {"query": "code review"}),
                          ("ToolSearch", {"query": "memory"})]:
            with self.subTest(name=name):
                self.assertEqual(self.run_audit([self.tool(name, inp)])["verdict"], "VOID")

    def test_vendor_and_held_out_text_in_all_message_types_are_void(self):
        for kind in ("assistant", "user"):
            for text in ("Greptile reports a defect", "Synthetic held-out defect"):
                with self.subTest(kind=kind, text=text):
                    event = {"type": kind, "message": {"content": [{"type": "text", "text": text}]}}
                    self.assertEqual(self.run_audit([event])["verdict"], "VOID")

    def test_persisted_comment_is_screened_and_run_bound(self):
        self.comments[0]["body"] = 'Greptile reports a defect'
        self.assertEqual(self.run_audit()["verdict"], "VOID")
        self.comments[0].update(body='{"verdict":"APPROVE","findings":[]}', createdByRunId="dispatcher")
        self.assertIn("no valid run-bound persisted result", self.run_audit()["unverified"])

    def test_reused_first_review_is_void_despite_dispatcher_touch(self):
        out = self.run_audit(extra_dispatcher=True, record_change=lambda r: r["usageJson"].update(freshSession=False, sessionReused=True))
        self.assertEqual(out["verdict"], "VOID")
        self.assertEqual(len(out["runs"]), 1)

    def test_missing_freshness_and_model_identity_are_unverified(self):
        out = self.run_audit(record_change=lambda r: r.update(usageJson={}))
        self.assertEqual(out["trace_verdict"], "UNVERIFIED")
        self.assertFalse(out["scoreable"])

    def test_model_mismatch_is_void(self):
        self.init["model"] = "other-model"
        self.assertEqual(self.run_audit()["verdict"], "VOID")

    def test_auto_memory_without_launch_snapshot_is_unverified(self):
        self.init["memory_paths"] = {"auto": "fixture-memory-directory"}
        self.assertEqual(self.run_audit()["trace_verdict"], "UNVERIFIED")

    def test_queued_run_never_requests_nonexistent_log(self):
        out = self.run_audit(queued=True)
        self.assertEqual(out["verdict"], "PENDING")
        self.assertEqual(out["runs"], [])
        self.assertFalse(out["scoreable"])

    def test_empty_truncated_and_unparseable_logs_fail_closed(self):
        for change in [lambda p: p.update(content=""), lambda p: p.update(truncated=True),
                       lambda p: p.update(content="not json"), lambda p: p.update(content=p["content"][:-5])]:
            with self.subTest(change=change):
                self.assertEqual(self.run_audit(page_change=change)["trace_verdict"], "UNVERIFIED")

    def test_pagination_finds_tail_vendor_and_reassembles_split_wrapper(self):
        # A page boundary may split an outer JSON wrapper too; do not parse each page alone.
        import audit
        text = json.dumps(self.init) + "\n" + json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Greptile tail"}]}}) + "\n" + json.dumps(self.terminal) + "\n"
        raw = json.dumps({"stream": "stdout", "chunk": text}) + "\n"
        boundary = len(raw) // 2
        record = dict(self.record, logBytes=len(raw.encode()))
        def fake_get(path):
            if "offset=0&" in path:
                return {"content": raw[:boundary], "nextOffset": boundary}
            if f"offset={boundary}&" in path:
                return {"content": raw[boundary:]}
            raise AssertionError(path)
        with mock.patch.object(audit, "get", side_effect=fake_get):
            complete, events, coverage = audit.read_trace(record)
        self.assertIn("Greptile", json.dumps(events))
        self.assertEqual(coverage["pages"], 2)
        self.assertEqual(complete, raw)

    def test_nonadvancing_continuation_and_missing_terminal_refuse(self):
        self.assertEqual(self.run_audit(page_change=lambda p: p.update(nextOffset=0))["trace_verdict"], "UNVERIFIED")
        self.terminal = {"type": "assistant", "message": {"content": []}}
        self.assertEqual(self.run_audit()["trace_verdict"], "UNVERIFIED")

    def test_result_schema_rejects_bool_line_and_inconsistent_severity(self):
        import audit
        self.assertIsNone(audit.check_result('{"verdict":"APPROVE","findings":[{"file":"code.py","line":true,"severity":"P1","title":"defect","rationale":"wrong"}]}'))
        self.assertIsNone(audit.check_result('{"verdict":"APPROVE","findings":[{"file":"code.py","line":1,"severity":"P1","title":"defect","rationale":"wrong"}]}'))


class TransportTests(unittest.TestCase):
    def test_launch_is_retired_before_any_bundle_or_network_operation(self):
        import dispatch
        with mock.patch("sys.argv", ["dispatch.py", "--cid", "C-10", "--models", "synthetic-model"]), mock.patch.object(stage, "verify_bundle", side_effect=AssertionError("must not inspect bundle")):
            with self.assertRaises(SystemExit):
                dispatch.main()

    def test_dry_run_requires_frozen_manifest(self):
        import dispatch
        with mock.patch("sys.argv", ["dispatch.py", "--cid", "C-10", "--models", "synthetic-model", "--dry-run"]), mock.patch.object(stage, "verify_bundle", side_effect=stage.GateError("fixture")):
            with self.assertRaises(SystemExit):
                dispatch.main()

    def test_dry_run_is_not_launch_permission(self):
        import contextlib
        import dispatch
        output = io.StringIO()
        with mock.patch("sys.argv", ["dispatch.py", "--cid", "C-10", "--models", "synthetic-model", "--dry-run"]), mock.patch.object(stage, "verify_bundle", return_value={}), mock.patch.object(dispatch, "gate", return_value=[]), mock.patch.object(dispatch, "done_pairs", return_value=set()), contextlib.redirect_stdout(output):
            dispatch.main()
        self.assertIn("launch=HELD", output.getvalue())
        self.assertNotIn("PATCH", dispatch.DESC)
        self.assertNotIn("PAPERCLIP_API_KEY", dispatch.DESC)

    def test_missing_terminal_log_is_unverified_and_auth_failure_propagates(self):
        import audit
        import urllib.error
        record = {"id": "fixture", "logBytes": 100}
        for code in (404, 401, 403):
            error = urllib.error.HTTPError("https://invalid.example", code, "fixture", {}, None)
            with self.subTest(code=code), mock.patch.object(audit, "get", side_effect=error):
                with self.assertRaises(audit.TraceError if code == 404 else urllib.error.HTTPError):
                    audit.read_trace(record)


class CliTests(unittest.TestCase):
    def test_audit_ineligible_exit_codes_are_nonzero(self):
        import audit
        import contextlib
        for verdict, expected in [("VOID", 1), ("UNVERIFIED", 3), ("PENDING", 4), ("CLEAN", 3)]:
            with self.subTest(verdict=verdict), mock.patch("sys.argv", ["audit.py", "fixture"]), mock.patch.object(audit, "audit", return_value={"verdict": verdict, "scoreable": False}), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    audit.main()
                self.assertEqual(caught.exception.code, expected)

    def stage_cli(self, only, failure):
        import contextlib
        with tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as tmp:
            root = pathlib.Path(tmp)
            (root / "evidence").mkdir()
            data = root / "corpus.json"
            data.write_text("{}")
            with mock.patch("sys.argv", ["stage.py", "--only", only]), mock.patch.object(stage, "SCORER", str(root)), mock.patch.object(stage, "ARENA", str(root / "arena")), mock.patch.object(stage, "GREPTILE", str(data)), mock.patch.object(stage, "build", side_effect=failure), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    stage.main()
                return caught.exception.code

    def test_stage_scanner_failure_exits_three(self):
        self.assertEqual(self.stage_cli("C-10", stage.GateError("fixture scanner failure")), 3)

    def test_unknown_cell_is_not_an_empty_success(self):
        self.assertEqual(self.stage_cli("C-99", AssertionError("must not build")), 2)


if __name__ == "__main__":
    unittest.main()
