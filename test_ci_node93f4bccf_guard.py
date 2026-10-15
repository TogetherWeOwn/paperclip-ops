#!/usr/bin/env python3
"""Tests for ci_node93f4bccf_guard.py (case_0bf02bac).

Every contract carries a positive control: a mutated world in which the
guard MUST refuse or diverge. A gate that has gone vacuous fails here
instead of passing quietly. Offline: the GitHub API is a stubbed
urllib.request.urlopen in-process. No network, no credential on disk, no
host, no systemd.
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import ci_node93f4bccf_guard as guard

TOKEN = "SYNTHETIC_DO_NOT_PRINT_7f3a"
VALUE = '["self-hosted","two-node93f4bccf"]'


def runner(name, status="online", busy=False, labels=("self-hosted", "two-node93f4bccf")):
    return {
        "name": name,
        "status": status,
        "busy": busy,
        "labels": [{"name": l} for l in labels],
    }


class StubTransport:
    """Canned GitHub API. Records every call; raises where told to."""

    def __init__(self):
        self.calls = []
        self.runners_pages = [[
            runner("node93f4bccf-r1", busy=False),
            runner("node93f4bccf-r2", busy=False),
            runner("coolify-vps-1", labels=("self-hosted", "two-isolated")),
        ]]
        self.variable = None  # None = absent (404)
        self.fail_runners_with = None
        self.fail_variable_with = None
        self.fail_delete_with = None
        self.runner_payload = None
        self.variable_payload = None
        self.delete_status = 204

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        self.calls.append((method, url))
        if "/orgs/" in url and "/actions/runners" in url:
            if self.fail_runners_with:
                raise self.fail_runners_with
            page = 1
            for part in url.split("?")[-1].split("&"):
                if part.startswith("page="):
                    page = int(part.split("=", 1)[1])
            batch = self.runners_pages[page - 1] if page <= len(self.runners_pages) else []
            return FakeResponse(200, self.runner_payload if self.runner_payload is not None
                                else {"runners": batch})
        if "/actions/variables/" in url:
            if method == "GET":
                if self.fail_variable_with:
                    raise self.fail_variable_with
                if self.variable_payload is not None:
                    return FakeResponse(200, self.variable_payload)
                if self.variable is None:
                    raise urllib.error.HTTPError(url, 404, "not found", {}, None)
                return FakeResponse(200, {"name": "CI_nodecf677d86_RUNNER", "value": self.variable})
            if method == "DELETE":
                if self.fail_delete_with:
                    raise self.fail_delete_with
                if self.delete_status == 204:
                    self.variable = None
                return FakeResponse(self.delete_status, {})
            if method == "PATCH":
                body = json.loads(req.data.decode())
                self.variable = body["value"]
                return FakeResponse(204, {})
            raise AssertionError(f"unexpected variable method {method}")
        if url.endswith("/actions/variables") and method == "POST":
            body = json.loads(req.data.decode())
            self.variable = body["value"]
            return FakeResponse(201, {"name": body["name"], "value": body["value"]})
        if "/actions/variables/" in url and method == "PATCH":
            body = json.loads(req.data.decode())
            self.variable = body["value"]
            return FakeResponse(200, {"name": body["name"], "value": body["value"]})
        raise AssertionError(f"unstubbed call {method} {url}")


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.stub = StubTransport()
        self._real = urllib.request.urlopen
        urllib.request.urlopen = self.stub
        self.addCleanup(setattr, urllib.request, "urlopen", self._real)
        self._env = dict(os.environ)
        os.environ["GH_TOKEN"] = TOKEN
        self.addCleanup(os.environ.clear)
        self.addCleanup(os.environ.update, self._env)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_guard(self, *argv):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return guard.main(list(argv))

    def wrote(self):
        return [c for c in self.stub.calls if c[0] in ("POST", "PATCH", "DELETE")]

    def all_output(self):
        return self.out.getvalue() + self.err.getvalue()


class Decision(GuardCase):
    def test_idle_runner_var_absent_is_drift(self):
        self.stub.variable = None
        self.assertEqual(self.run_guard("--mode", "check"), 1)

    def test_idle_runner_var_set_is_aligned(self):
        self.stub.variable = VALUE
        self.assertEqual(self.run_guard("--mode", "check"), 0)

    def test_no_idle_var_absent_is_aligned(self):
        self.stub.runners_pages = [[runner("node93f4bccf-r1", busy=True)]]
        self.stub.variable = None
        self.assertEqual(self.run_guard("--mode", "check"), 0)

    def test_no_idle_var_set_is_drift(self):
        self.stub.runners_pages = [[runner("node93f4bccf-r1", busy=True)]]
        self.stub.variable = VALUE
        self.assertEqual(self.run_guard("--mode", "check"), 1)

    def test_busy_runner_is_not_idle(self):
        self.stub.runners_pages = [[
            runner("node93f4bccf-r1", busy=True), runner("node93f4bccf-r2", busy=True),
        ]]
        rc = self.run_guard("--mode", "apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.variable, None)
        self.assertNotIn("node93f4bccf-r1", self.out.getvalue().split("idle_names=")[1].split()[0])

    def test_offline_runner_is_not_idle(self):
        self.stub.runners_pages = [[
            runner("node93f4bccf-r1", status="offline"), runner("node93f4bccf-r2", busy=True),
        ]]
        self.assertEqual(self.run_guard("--mode", "check"), 0)

    def test_foreign_label_is_ignored(self):
        self.stub.runners_pages = [[
            runner("coolify-vps-1", labels=("self-hosted", "two-isolated")),
            runner("other-box", labels=("self-hosted", "other")),
        ]]
        self.assertEqual(self.run_guard("--mode", "check"), 0)

    def test_apply_sets_when_idle_and_absent(self):
        self.stub.variable = None
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertEqual(self.stub.variable, VALUE)

    def test_apply_clears_when_var_set_and_none_idle(self):
        self.stub.runners_pages = [[runner("node93f4bccf-r1", status="offline")]]
        self.stub.variable = VALUE
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertIsNone(self.stub.variable)

    def test_apply_keeps_aligned_without_writing(self):
        self.stub.variable = VALUE
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertEqual(self.wrote(), [])

    def test_positive_control_idle_identity_is_load_bearing(self):
        # The flip side of test_busy_runner_is_not_idle: if the busy flag
        # were ignored, a fully-busy PC would read idle and the var would
        # be set. One idle runner among busy ones must route.
        self.stub.runners_pages = [[
            runner("node93f4bccf-r1", busy=True), runner("node93f4bccf-r2", busy=False),
        ]]
        self.stub.variable = None
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertEqual(self.stub.variable, VALUE)


class FailClosed(GuardCase):
    def test_apply_clears_previously_enabled_route_on_runner_failure(self):
        for error in (
            urllib.error.HTTPError("https://api.github.com/x", 403, "forbidden", {}, None),
            OSError("boom"), TimeoutError("timeout"),
            http.client.IncompleteRead(b"x", 490), http.client.BadStatusLine(""),
        ):
            with self.subTest(error=type(error).__name__):
                self.stub.variable = VALUE
                self.stub.calls.clear()
                self.stub.fail_runners_with = error
                self.assertEqual(self.run_guard("--mode", "apply"), 3)
                self.assertIsNone(self.stub.variable)
                self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])

    def test_apply_attempts_clear_even_when_variable_read_fails(self):
        self.stub.variable = VALUE
        self.stub.fail_variable_with = OSError("read failed")
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])

    def test_malformed_runner_payload_clears_instead_of_crashing_or_routing(self):
        for payload in (
            {}, {"runners": None}, {"runners": [None]},
            {"runners": [{**runner("node93f4bccf-r1"), "labels": None}]},
            {"runners": [{**runner("node93f4bccf-r1"), "labels": [None]}]},
            {"runners": [{**runner("node93f4bccf-r1"), "busy": "false"}]},
            {"runners": [{k: v for k, v in runner("node93f4bccf-r1").items() if k != "busy"}]},
        ):
            with self.subTest(payload=payload):
                self.stub.variable = VALUE
                self.stub.runner_payload = payload
                self.assertEqual(self.run_guard("--mode", "apply"), 3)
                self.assertIsNone(self.stub.variable)

    def test_malformed_variable_payload_is_unmeasured_and_cleared(self):
        self.stub.variable = VALUE
        self.stub.variable_payload = {"name": "CI_nodecf677d86_RUNNER"}
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)

    def test_failed_clear_is_not_reported_as_cleared(self):
        self.stub.variable = VALUE
        self.stub.fail_runners_with = OSError("observation failed")
        self.stub.fail_delete_with = urllib.error.HTTPError(
            "https://api.github.com/x", 403, "forbidden", {}, None)
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertEqual(self.stub.variable, VALUE)
        self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])
        self.assertNotIn("CLEARED", self.all_output())
        self.assertIn("clear failed", self.err.getvalue())
        self.assertNotIn(TOKEN, self.all_output())

    def test_delete_response_must_be_204(self):
        self.stub.variable = VALUE
        self.stub.runners_pages = [[runner("node93f4bccf-r1", status="offline")]]
        self.stub.delete_status = 202
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertEqual(self.stub.variable, VALUE)
        self.assertNotIn("CLEARED", self.all_output())

    def test_clear_of_absent_variable_is_idempotent_on_404(self):
        self.stub.fail_runners_with = OSError("boom")
        self.stub.fail_delete_with = urllib.error.HTTPError(
            "https://api.github.com/x", 404, "not found", {}, None)
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertIn("ALREADY-CLEAR", self.all_output())

    def test_wrong_existing_value_is_repaired(self):
        self.stub.variable = '[]'
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertEqual(self.stub.variable, VALUE)
        self.assertEqual([method for method, _ in self.wrote()], ["PATCH"])

    def test_slow_pagination_preserves_a_separate_delete_budget(self):
        self.stub.variable = VALUE
        self.stub.runners_pages = [[runner(f"other-{n}", labels=("two-isolated",))
                                    for n in range(100)]]
        with mock.patch.object(guard.time, "monotonic", side_effect=[0, 0, 31]):
            self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])

    def test_node93f4bccf_runner_on_second_page_is_observed(self):
        self.stub.runners_pages = [[runner(f"other-{n}", labels=("two-isolated",))
                                    for n in range(100)], [runner("node93f4bccf-r1")]]
        self.assertEqual(self.run_guard("--mode", "apply"), 0)
        self.assertEqual(self.stub.variable, VALUE)

    def test_non_json_observation_clears_and_does_not_print_payload(self):
        self.stub.variable = VALUE
        transport = self.stub

        def non_json(req, timeout=None):
            if "/actions/runners?" in req.full_url:
                response = FakeResponse(200, {})
                response._payload = TOKEN.encode()
                return response
            return transport(req, timeout)

        urllib.request.urlopen = non_json
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertNotIn(TOKEN, self.all_output())

    def test_truncated_body_on_read_clears_the_route(self):
        # The cut-short body surfaces from resp.read(), after urlopen returned.
        self.stub.variable = VALUE
        transport = self.stub

        def truncated(req, timeout=None):
            if "/actions/runners?" in req.full_url:
                response = FakeResponse(200, {})

                def cut():
                    raise http.client.IncompleteRead(b"0123456789", 490)

                response.read = cut
                return response
            return transport(req, timeout)

        urllib.request.urlopen = truncated
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])
        self.assertNotIn("Traceback", self.err.getvalue())
        # Mapped at the transport boundary, not merely caught by main()'s net.
        self.assertIn("github request failed on GET", self.err.getvalue())
        self.assertIn("IncompleteRead", self.err.getvalue())

    def test_unexpected_observation_exception_clears_and_hides_its_message(self):
        self.stub.variable = VALUE
        with mock.patch.object(guard, "observe", side_effect=KeyError(TOKEN)):
            self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIsNone(self.stub.variable)
        self.assertEqual([method for method, _ in self.wrote()], ["DELETE"])
        self.assertIn("KeyError", self.err.getvalue())
        self.assertNotIn(TOKEN, self.all_output())
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_unexpected_delete_exception_is_uncontained_not_a_traceback(self):
        for error in (http.client.IncompleteRead(b"", 5), ZeroDivisionError(TOKEN)):
            with self.subTest(error=type(error).__name__):
                self.out.truncate(0)
                self.err.truncate(0)
                self.stub.variable = VALUE
                self.stub.fail_runners_with = OSError("boom")
                self.stub.fail_delete_with = error
                self.assertEqual(self.run_guard("--mode", "apply"), 3)
                self.assertEqual(self.stub.variable, VALUE)
                self.assertIn("UNCONTAINED", self.err.getvalue())
                self.assertNotIn("CLEARED", self.all_output())
                self.assertNotIn(TOKEN, self.all_output())
                self.assertNotIn("Traceback", self.err.getvalue())

    def test_planned_clear_that_fails_is_uncontained_not_refused(self):
        self.stub.variable = VALUE
        self.stub.runners_pages = [[runner("node93f4bccf-r1", status="offline")]]
        self.stub.fail_delete_with = urllib.error.HTTPError(
            "https://api.github.com/x", 403, "forbidden", {}, None)
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertEqual(self.stub.variable, VALUE)
        self.assertIn("UNCONTAINED", self.err.getvalue())
        self.assertNotIn("CLEARED", self.all_output())

    def test_failed_route_set_stays_refused(self):
        # Positive control: only a failed CLEAR is UNCONTAINED.
        self.stub.fail_delete_with = None
        transport = self.stub

        def set_fails(req, timeout=None):
            if req.get_method() == "POST":
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            return transport(req, timeout)

        urllib.request.urlopen = set_fails
        self.assertEqual(self.run_guard("--mode", "apply"), 3)
        self.assertIn("REFUSED", self.err.getvalue())
        self.assertNotIn("UNCONTAINED", self.err.getvalue())

    def test_check_mode_runner_failure_is_unmeasured_and_never_writes(self):
        self.stub.variable = VALUE
        self.stub.fail_runners_with = OSError("boom")
        self.assertEqual(self.run_guard("--mode", "check"), 3)
        self.assertEqual(self.stub.variable, VALUE)
        self.assertEqual(self.wrote(), [])

    def test_missing_credential_refuses_without_any_call(self):
        del os.environ["GH_TOKEN"]
        self.assertEqual(self.run_guard("--mode", "check"), 2)
        self.assertEqual(self.stub.calls, [])

    def test_credential_never_reaches_output(self):
        self.stub.variable = VALUE
        self.assertEqual(self.run_guard("--mode", "check"), 0)
        self.assertNotIn(TOKEN, self.all_output())

    def test_non_json_value_refuses(self):
        self.assertEqual(self.run_guard("--mode", "check", "--value", "not-json"), 2)

    def test_empty_list_value_refuses(self):
        self.assertEqual(self.run_guard("--mode", "check", "--value", "[]"), 2)


class DrippedBody(unittest.TestCase):
    """A body that drips bytes slower than the whole observation budget.

    No single recv times out, so only the wall-clock bound ends it. The
    runner list is complete and valid when it finally finishes: without the
    bound the guard would read it, find an idle runner and SET the route.
    """

    BODY = json.dumps({"runners": [runner("node93f4bccf-r1")]}).encode()

    def setUp(self):
        test = self
        self.calls = []
        self.variable = VALUE

        class API(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.calls.append("GET")
                if "/actions/runners?" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(test.BODY)))
                    self.end_headers()
                    try:
                        for byte in test.BODY:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError:
                        pass
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                test.calls.append("POST")
                test.variable = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["value"]
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self):
                test.calls.append("DELETE")
                test.variable = None
                self.send_response(204)
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.api_url = f"http://127.0.0.1:{server.server_port}"
        self._env = dict(os.environ)
        os.environ["GH_TOKEN"] = TOKEN
        self.addCleanup(os.environ.clear)
        self.addCleanup(os.environ.update, self._env)

    def run_guard(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = guard.main(list(argv) + ["--api-url", self.api_url])
        return code, out.getvalue() + err.getvalue()

    def test_dripping_body_is_cut_by_the_wall_clock_and_the_route_cleared(self):
        started = time.monotonic()
        with mock.patch.object(guard, "OBSERVATION_BUDGET", 0.5):
            code, output = self.run_guard("--mode", "apply")
        self.assertEqual(code, 3, output)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(self.variable)
        self.assertIn("DELETE", self.calls)
        self.assertIn("observation budget exhausted", output)
        self.assertNotIn(TOKEN, output)

    def test_positive_control_without_the_bound_the_drip_completes_and_sets_the_route(self):
        with mock.patch.object(guard, "observe_bounded", guard.observe):
            code, output = self.run_guard("--mode", "apply")
        self.assertEqual(code, 0, output)
        self.assertIn("idle=1", output)
        self.assertIn("POST", self.calls)
        self.assertNotIn("DELETE", self.calls)
        self.assertEqual(self.variable, VALUE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
