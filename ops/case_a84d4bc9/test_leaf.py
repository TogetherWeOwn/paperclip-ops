#!/usr/bin/env python3
"""case_672a252f offline leaf-executor regressions. No network, model calls or live writes.

Every control uses synthetic fixtures with a stubbed transport: the real
`cliproxy_transport` is exercised only for its redirect/route gates via
local mocks, never against a URL. Fixtures live under PAPERCLIP_SCRATCH_DIR.
"""
import copy
import hashlib
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import leaf


def binding_doc(**over):
    doc = {"company_id": "company", "child_id": "child", "assignee_agent_id": "agent",
           "native_run_id": "run", "cell_id": "C-10", "tier_label": "t2",
           "permitted_tiers": ["t2"], "issue_pin": "synthetic-model",
           "pin_revision": "rev-1", "run_model": "synthetic-model"}
    doc.update(over)
    return doc


def admission_doc(**over):
    doc = {"observed_at": time.time(), "source_revision": "src-1",
           "weekly_ok": True, "short_window_ok": True, "cooldowns_ok": True,
           "kill_rules_ok": True, "context_ok": True, "effort_ok": True,
           "account_ok": True, "pool_ok": True}
    doc.update(over)
    return doc


def answer(text='{"verdict":"APPROVE","findings":[]}', tools=(), model="synthetic-model"):
    return {"model_identity": {"served": model, "route": "cliproxy"},
            "segments": [text], "complete": True,
            "tool_calls": list(tools), "usage": {"in": 1, "out": 1}}


class LeafTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cell = self.root / "C-10"
        (self.cell / "tree").mkdir(parents=True)
        (self.cell / "tree" / "code.py").write_text("ordinary code\n")
        (self.cell / "pr.diff").write_text("ordinary diff\n")
        (self.cell / "reviewer_view.json").write_text('{"title":"ordinary"}')
        self.out = self.root / "record.json"
        self.trace = self.root / "trace.json"
        self.ledger = self.root / "ledger"
        self.manifest = {"schema": "blinded-bundle-v1", "cid": "C-10",
                         "reviewed_head": "head",
                         "entries": {"pr.diff": {"kind": "file", "sha256": "x"},
                                     "reviewer_view.json": {"kind": "file", "sha256": "y"},
                                     "tree": {"kind": "directory"}}}

    def drive(self, binding=None, admission=None, transport=None, heldout=None,
            dry_check=False, ledger=None):
        try:
            binding = leaf.Binding.from_json(
                binding if binding is not None else binding_doc())
        except leaf.Hold as e:
            return 2, {"status": "HOLD", "terminal_note": str(e)}, []
        admiss = admission if admission is not None else admission_doc()
        calls = []

        def admission_fn():
            if callable(admiss):
                return admiss(calls)
            return leaf.Admission.from_json(copy.deepcopy(admiss))

        def fake_transport(payload):
            calls.append(payload)
            return transport(payload) if callable(transport) else transport

        heldout = heldout or {}
        with mock.patch.object(leaf.stage, "verify_bundle", return_value=self.manifest), \
             mock.patch.object(leaf.stage, "manifest_path") as mf:
            mf.return_value.read_bytes.return_value = b"manifest"
            code, record = leaf.run_cell(
                binding, admission_fn, fake_transport, str(self.cell),
                str(self.out), str(self.trace), str(ledger or self.ledger),
                heldout.get("regex", ()), heldout.get("phrases", ()), dry_check=dry_check)
        return code, record, calls

    # -- positive control ----------------------------------------------------
    def test_admitted_mock_cell_seals_and_persists(self):
        code, record, calls = self.drive(transport=lambda p: answer())
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "OK")
        self.assertTrue(record["sealed"])
        self.assertFalse(record["scoreable"])
        self.assertEqual(record["result"], {"verdict": "APPROVE", "findings": []})
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.out.exists() and self.trace.exists())
        persisted = json.loads(self.out.read_text())
        self.assertEqual(persisted["record_id"], record["record_id"])
        trace = json.loads(self.trace.read_text())
        self.assertGreaterEqual(len(trace), 3)  # system + request + response
        self.assertFalse((self.ledger / "C-10.claim").exists())  # claim released

    # -- binding gates: HOLD, no inference ------------------------------------
    def test_missing_pin_holds_without_transport(self):
        code, record, calls = self.drive(binding=binding_doc(issue_pin=""))
        self.assertEqual(code, 2)
        self.assertEqual(record["status"], "HOLD")
        self.assertEqual(calls, [])

    def test_model_pin_mismatch_holds_without_transport(self):
        code, record, calls = self.drive(binding=binding_doc(run_model="other-model"))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_aliased_binding_holds_without_inference(self):
        for run_model, pin in (("other-model", "synthetic-model"),
                               ("synthetic-model", "other-model")):
            with self.subTest(run_model=run_model):
                code, record, calls = self.drive(binding=binding_doc(
                    run_model=run_model, issue_pin=pin,
                    canonical_aliases={run_model: pin}))
                self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_excluded_model_holds(self):
        code, record, calls = self.drive(binding=binding_doc(
            excluded_models=["synthetic-model"]))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_wrong_tier_holds(self):
        code, record, calls = self.drive(binding=binding_doc(tier_label="t9"))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    # -- admission gates: HOLD, no inference -----------------------------------
    def test_stale_admission_holds(self):
        old = admission_doc(observed_at=time.time() - 3600)
        code, record, calls = self.drive(admission=old)
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_denied_gate_holds(self):
        code, record, calls = self.drive(admission=admission_doc(short_window_ok=False))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))
        self.assertIn("short_window", record["terminal_note"])

    def test_shadow_report_shape_is_not_a_grant(self):
        # A report-shaped object without per-gate booleans defaults every gate
        # to False: HOLD, not free quota.
        report = {"observed_at": time.time(), "source_revision": "shadow-1",
                  "cohort": "synthetic", "provenance": "fresh-observations"}
        code, record, calls = self.drive(admission=report)
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_exhaustion_mid_cell_stops_before_retry(self):
        seen = []

        def fading(calls):
            seen.append(len(calls))
            ok = all(g for g in (len(calls) < 1,))
            return leaf.Admission.from_json(admission_doc(short_window_ok=ok))

        def flaky(payload):
            raise leaf.TransientError("HTTP 429")

        code, record, calls = self.drive(admission=fading, transport=flaky)
        self.assertEqual(record["status"], "HOLD")
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)  # one attempt, then the retry boundary held

    # -- identity gates ---------------------------------------------------------
    def test_substituted_model_is_void(self):
        code, record, _ = self.drive(transport=lambda p: answer(model="other-model"))
        self.assertEqual((code, record["status"]), (1, "VOID"))

    def test_missing_identity_is_unverified(self):
        resp = answer()
        del resp["model_identity"]
        code, record, _ = self.drive(transport=lambda p: resp)
        self.assertEqual((code, record["status"]), (3, "UNVERIFIED"))

    def test_wrong_route_is_void(self):
        resp = answer()
        resp["model_identity"]["route"] = "direct"
        code, record, _ = self.drive(transport=lambda p: resp)
        self.assertEqual((code, record["status"]), (1, "VOID"))

    def test_truncated_response_is_unverified(self):
        resp = answer()
        resp["complete"] = False
        code, record, _ = self.drive(transport=lambda p: resp)
        self.assertEqual((code, record["status"]), (3, "UNVERIFIED"))

    def test_retry_exhaustion_is_unverified(self):
        def flaky(payload):
            raise leaf.TransientError("HTTP 503")

        code, record, calls = self.drive(transport=flaky)
        self.assertEqual(record["status"], "UNVERIFIED")
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), leaf.MAX_REQUEST_ATTEMPTS)
        self.assertEqual(len(record["admissions"]), 1 + leaf.MAX_REQUEST_ATTEMPTS)

    # -- tool boundary gates -------------------------------------------------------
    def test_glob_is_not_offered(self):
        self.assertNotIn("Glob", leaf.TOOLSET)
        self.assertNotIn("Glob", json.dumps(leaf.TOOL_DEFINITIONS))
        worker = leaf.ToolWorker(self.cell / "tree")
        with self.assertRaises(leaf.Void):
            worker.dispatch("Glob", {"pattern": "*.py"})

    def test_shell_and_memory_tools_are_void(self):
        worker = leaf.ToolWorker(self.cell / "tree")
        for name in ("Bash", "mcp__memory__hindsight_recall", "ToolSearch", "WebFetch"):
            with self.subTest(name=name), self.assertRaises(leaf.Void):
                worker.dispatch(name, {})

    def test_escaping_read_is_void(self):
        worker = leaf.ToolWorker(self.cell / "tree")
        outside = self.root / "claims.json"
        outside.write_text("ordinary")
        (self.cell / "tree" / "link").symlink_to(outside)
        for path in (str(self.cell / "tree" / ".." / ".." / "claims.json"),
                     str(self.cell / "tree" / "link"),
                     "relative/path.py"):
            with self.subTest(path=path), self.assertRaises(leaf.Void):
                worker.dispatch("Read", {"file_path": path})

    def test_escaping_grep_include_is_void(self):
        worker = leaf.ToolWorker(self.cell / "tree")
        with self.assertRaises(leaf.Void):
            worker.dispatch("Grep", {"pattern": "x", "include": "../*.json"})

    def test_model_tool_escape_voids_cell(self):
        def evil(payload):
            return answer(tools=[{"id": "1", "name": "Bash",
                                  "input": {"command": "true"}}])

        code, record, _ = self.drive(transport=evil)
        self.assertEqual((code, record["status"]), (1, "VOID"))

    def test_grep_files_only_discovers_names(self):
        worker = leaf.ToolWorker(self.cell / "tree")
        out = worker.dispatch("Grep", {"pattern": "ordinary", "files_only": True})
        self.assertEqual(out["files"], ["code.py"])
        self.assertNotIn("matches", out)

    # -- transport / leakage gates --------------------------------------------------
    def test_transport_refuses_redirect(self):
        with self.assertRaises(leaf.Void):
            leaf.NoRedirectHandler().redirect_request(
                None, None, 302, "moved", {}, "https://evil.invalid/")
        with mock.patch.object(leaf.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
            leaf._default_post("https://cliproxy.invalid/v1", {"route": "cliproxy"})
            self.assertIsInstance(opener.call_args[0][0], leaf.NoRedirectHandler)

    def test_transport_refuses_foreign_route(self):
        send = leaf.cliproxy_transport("https://cliproxy.invalid/v1",
                                       post_fn=lambda e, p: self.fail("must not post"))
        with self.assertRaises(leaf.Void):
            send({"route": "direct"})

    def test_trace_carries_no_credential_or_posting(self):
        code, record, calls = self.drive(transport=lambda p: answer(
            tools=[{"id": "1", "name": "Read",
                    "input": {"file_path": str(self.cell / "tree" / "code.py")}}]))
        trace_raw = self.trace.read_text()
        self.assertNotIn("PAPERCLIP_API_KEY", trace_raw)
        self.assertNotIn("PATCH", calls[0].get("route", "") + json.dumps(calls))
        self.assertNotIn("Authorization", trace_raw)
        self.assertEqual(code, 3)  # turn budget: tool answer never finalizes here
        self.assertIn("turn budget", record["terminal_note"])

    # -- screening gates ---------------------------------------------------------------
    def test_heldout_hit_in_prompt_voids(self):
        code, record, calls = self.drive(
            heldout={"regex": ["ordinary"], "phrases": []},
            transport=lambda p: self.fail("must not infer after a held-out hit"))
        self.assertEqual((code, record["status"], calls), (1, "VOID", []))

    def test_heldout_hit_in_answer_voids(self):
        code, record, _ = self.drive(
            heldout={"regex": [], "phrases": ["ordinary code"]},
            transport=lambda p: answer(tools=[{"id": "1", "name": "Read",
                                               "input": {"file_path": str(
                                                   self.cell / "tree" / "code.py")}}]))
        # The Read result itself carries the held-out phrase: the tool trace
        # screens first, so the cell is VOID regardless of the later verdict.
        self.assertEqual((code, record["status"]), (1, "VOID"))

    # -- single flight ---------------------------------------------------------------------
    def test_second_cell_in_flight_holds(self):
        (self.ledger).mkdir(parents=True, exist_ok=True)
        (self.ledger / "C-10.claim").write_text('{"cell_id":"C-10"}\n')
        code, record, calls = self.drive(transport=lambda p: self.fail("held"))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))

    def test_dry_check_never_calls_transport(self):
        code, record, calls = self.drive(dry_check=True,
                                       transport=lambda p: self.fail("must not infer"))
        self.assertEqual((code, record["status"], calls), (2, "HOLD", []))
        self.assertIn("dry-check", record["terminal_note"])

    # -- CLI gates ------------------------------------------------------------------------------
    def test_cli_missing_endpoint_is_hold(self):
        import contextlib
        import io
        binding = self.root / "binding.json"
        binding.write_text(json.dumps(binding_doc()))
        admission = self.root / "admission.json"
        admission.write_text(json.dumps(admission_doc()))
        out = io.StringIO()
        with mock.patch("sys.argv", ["leaf.py", "--binding", str(binding),
                                     "--admission", str(admission),
                                     "--bundle-dir", str(self.cell),
                                     "--out", str(self.out), "--trace", str(self.trace),
                                     "--ledger", str(self.ledger)]), \
                contextlib.redirect_stderr(out):
            self.assertEqual(leaf.main(), 2)


if __name__ == "__main__":
    unittest.main()
