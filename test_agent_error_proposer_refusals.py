#!/usr/bin/env python3
# ===========================================================================
# test_agent_error_proposer_refusals.py — refusal acceptance for the benign
# agent-error proposer (immutable; owned by the Test Author).
#
# When the board API returns garbage, the advisory proposer must report a
# stable refusal naming the endpoint and the next action instead of dumping
# a traceback (exit 1) or going quietly green on unmeasured input.
#
# REFUSAL CONTRACT (what the Builder implements in agent_error_proposer.py):
#   * Every refusal exits 2 through the EXISTING error class: RuntimeError
#     (or OSError) caught by the `__main__` guard, which prints
#     `agent_error_proposer.py: <message>` on stderr. No new exit codes,
#     no stdout envelope: stdout stays empty on refusal (measured nothing
#     emits nothing; the driver reads JSONL proposals from stdout).
#   * The stderr message carries stable machine tokens:
#       code=<CODE> endpoint=<PATH> retryable=false
#       next_action=check_board_api_then_retry
#     where CODE is one of:
#       api_response_unparseable    body bytes are not valid JSON
#       api_response_unexpected_shape
#                                   body parses but the top level is not the
#                                   expected list (dict/envelope payloads,
#                                   empty-body None)
#       api_request_failed          HTTP error from either endpoint; the
#                                   message also keeps the existing
#                                   `HTTP <status>` prefix so the status is
#                                   reported
#     retryable is always false: the refusal is a verdict, not a retry
#     policy — re-running the detector is the scheduler's decision, and
#     the human step is always the fixed next_action above.
#   * No traceback text on stderr, no response-body bytes in ANY output
#     (stdout or stderr), no credential bytes anywhere.
#   * Benign/reset semantics, reason strings, the JSONL proposal schema,
#     allowlist behavior and env-only credentials are unchanged: the
#     positive control below pins the happy path end to end.
#
# Hermetic: urllib.request.urlopen is faked per case (no network, no live
# board, no production data); the allowlist test hatch
# (PAPERCLIP_API_ALLOWLIST_TEST=1 with a loopback base) is set explicitly
# per invocation, which is its documented hermetic-suite use. The script
# runs in-process via runpy as __main__ so the real exit-mapping guard is
# exercised, with sys.argv and the environment patched. All fixtures are
# synthetic (loopback URLs, fake company/agent/run IDs, marker garbage).
#
# Builder: implement to this contract, never edit this file, and wire this
# module into CI with your PR (the orphan-suite ratchet fails unwired
# suites; likewise regenerate the offline-ci proof).
#
#   python3 -m unittest -v test_agent_error_proposer_refusals.py
# ===========================================================================

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PROPOSER = HERE / "agent_error_proposer.py"

API_URL = "http://127.0.0.1"
API_KEY = "TEST-KEY-NEVER-SENT"
COMPANY = "company-test"

AGENTS_PATH = f"/api/companies/{COMPANY}/agents"
RUNS_PATH = f"/api/companies/{COMPANY}/heartbeat-runs?agentId=a1&limit=5"

NEXT_ACTION = "check_board_api_then_retry"

# Markers a traceback on stderr would carry. A refusal line must contain
# none of these.
TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    "json.decoder",
    "JSONDecodeError",
    "During handling of the above exception",
    "The above exception was the direct cause",
)


class _FakeResponse:
    """Minimal urlopen result: context manager with a read() payload."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _ok(payload) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def error_agent(agent_id="a1"):
    return {"id": agent_id, "name": f"Agent {agent_id}",
            "status": "error", "reportsTo": "lead-1"}


def benign_run(run_id="r1", agent_id="a1"):
    # Ancient finishedAt: main() measures age against the real clock, so a
    # fixed 2020 date is past the 10-minute benign window forever.
    return {"id": run_id, "agentId": agent_id, "status": "failed",
            "finishedAt": "2020-05-01T00:00:00Z",
            "resultJson": {
                "subtype": "success",
                "unmanagedBackgroundTask": {"terminalResultSeen": True},
            }}


def _route(request, *, agents, runs):
    """Dispatch a faked urlopen call to the agents or runs responder."""
    url = request.get_full_url()
    if "heartbeat-runs" in url:
        return runs(url)
    if url.rstrip("/").endswith("/agents"):
        return agents(url)
    raise AssertionError(f"unexpected request URL in fixture: {url}")


def run_proposer(agents, runs):
    """Run the proposer as __main__ with faked urlopen.

    `agents`/`runs` are callables (url -> _FakeResponse) or exceptions to
    raise. Returns (exit_code, stdout, stderr). A non-SystemExit escape
    (today: JSONDecodeError) fails the calling test with the escape named:
    the __main__ guard must map every transport failure to exit 2.
    """
    def fake_urlopen(request, timeout=None):
        def agents_resp(url):
            if isinstance(agents, Exception):
                raise agents
            return agents(url)

        def runs_resp(url):
            if isinstance(runs, Exception):
                raise runs
            return runs(url)

        return _route(request, agents=agents_resp, runs=runs_resp)

    env = {"PAPERCLIP_API_URL": API_URL,
           "PAPERCLIP_API_KEY": API_KEY,
           "PAPERCLIP_COMPANY_ID": COMPANY,
           "PAPERCLIP_API_ALLOWLIST_TEST": "1"}
    argv = ["agent_error_proposer.py", "--company-id", COMPANY,
           "--api-url", API_URL]
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            mock.patch.dict(os.environ, env), \
            mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        try:
            runpy.run_path(str(PROPOSER), run_name="__main__")
        except SystemExit as done:
            return done.code, out.getvalue(), err.getvalue()
        except Exception as escaped:  # noqa: BLE001 — the RED signal
            raise AssertionError(
                f"expected SystemExit(2) refusal, "
                f"{type(escaped).__name__} escaped instead: {escaped}"
            ) from escaped
    raise AssertionError("proposer returned without exiting")


class ProposerRefusals(unittest.TestCase):
    maxDiff = 4096

    def assertRefusal(self, result, *, code, endpoint, markers=(),
                      http_status=None):
        exit_code, out, err = result
        self.assertEqual(exit_code, 2, f"stdout={out!r} stderr={err!r}")
        self.assertEqual(out, "",
                         "a refusal must emit no stdout (no JSONL rows "
                         f"on unmeasured input): {out!r}")
        self.assertTrue(err.startswith("agent_error_proposer.py: "),
                        f"refusal must keep the existing stderr prefix: {err!r}")
        for token in (f"code={code}", f"endpoint={endpoint}",
                      "retryable=false",
                      f"next_action={NEXT_ACTION}"):
            self.assertIn(token, err, f"missing stable token {token!r} in {err!r}")
        self.assertNotIn("retryable=true", err,
                         f"exactly one retryable verdict: {err!r}")
        if http_status is not None:
            self.assertIn(f"HTTP {http_status}", err,
                          f"HTTP refusal must report the status: {err!r}")
        for marker in TRACEBACK_MARKERS:
            self.assertNotIn(marker, err,
                             f"traceback text on stderr: {err!r}")
        combined = out + err
        for marker in markers:
            self.assertNotIn(marker, combined,
                             "response-body bytes leaked into output")
        self.assertNotIn(API_KEY, combined, "credential bytes in output")

    # -- malformed JSON bodies -------------------------------------------

    def test_invalid_json_agents_body_refuses(self):
        garbage = b"<html>GARBAGE-MARKER-AGENTS not json at all"
        result = run_proposer(lambda url: _FakeResponse(garbage),
                              lambda url: _ok([]))
        self.assertRefusal(result, code="api_response_unparseable",
                           endpoint=AGENTS_PATH,
                           markers=("GARBAGE-MARKER-AGENTS",))

    def test_invalid_json_runs_body_refuses(self):
        garbage = b"GARBAGE-MARKER-RUNS {\x00 broken bytes"
        result = run_proposer(lambda url: _ok([error_agent()]),
                              lambda url: _FakeResponse(garbage))
        self.assertRefusal(result, code="api_response_unparseable",
                           endpoint=RUNS_PATH,
                           markers=("GARBAGE-MARKER-RUNS",))

    # -- empty bodies (api_get reads b"" as None) -------------------------

    def test_empty_agents_body_refuses(self):
        result = run_proposer(lambda url: _FakeResponse(b""),
                              lambda url: _ok([]))
        self.assertRefusal(result, code="api_response_unexpected_shape",
                           endpoint=AGENTS_PATH)

    def test_empty_runs_body_refuses(self):
        result = run_proposer(lambda url: _ok([error_agent()]),
                              lambda url: _FakeResponse(b""))
        self.assertRefusal(result, code="api_response_unexpected_shape",
                           endpoint=RUNS_PATH)

    # -- unexpected top-level shapes -------------------------------------

    def test_dict_shaped_agents_payload_refuses(self):
        envelope = {"agents": [error_agent()], "total": 1,
                    "marker": "SHAPE-MARKER-AGENTS"}
        result = run_proposer(lambda url: _ok(envelope),
                              lambda url: _ok([]))
        self.assertRefusal(result, code="api_response_unexpected_shape",
                           endpoint=AGENTS_PATH,
                           markers=("SHAPE-MARKER-AGENTS",))

    def test_dict_shaped_runs_payload_refuses(self):
        envelope = {"runs": [benign_run()], "total": 1,
                    "marker": "SHAPE-MARKER-RUNS"}
        result = run_proposer(lambda url: _ok([error_agent()]),
                              lambda url: _ok(envelope))
        self.assertRefusal(result, code="api_response_unexpected_shape",
                           endpoint=RUNS_PATH,
                           markers=("SHAPE-MARKER-RUNS",))

    # -- HTTP errors on each endpoint ------------------------------------

    def _http_error(self, url, status, marker):
        body = ('{"error": "%s status %d boom"}' % (marker, status)).encode()
        return urllib.error.HTTPError(url, status, "boom", {}, io.BytesIO(body))

    def _assert_http_refusal(self, status, endpoint):
        marker = f"HTTP-BODY-MARKER-{endpoint.split('/')[-1].split('?')[0].upper()}-{status}"

        def agents(url):
            if endpoint == AGENTS_PATH:
                raise self._http_error(url, status, marker)
            return _ok([error_agent()])

        def runs(url):
            if endpoint == RUNS_PATH:
                raise self._http_error(url, status, marker)
            return _ok([benign_run()])

        result = run_proposer(agents, runs)
        self.assertRefusal(result, code="api_request_failed",
                           endpoint=endpoint, markers=(marker,),
                           http_status=status)

    def test_http_500_on_agents_refuses_without_body_echo(self):
        self._assert_http_refusal(500, AGENTS_PATH)

    def test_http_403_on_agents_refuses_without_body_echo(self):
        self._assert_http_refusal(403, AGENTS_PATH)

    def test_http_429_on_agents_refuses_without_body_echo(self):
        self._assert_http_refusal(429, AGENTS_PATH)

    def test_http_500_on_runs_refuses_without_body_echo(self):
        self._assert_http_refusal(500, RUNS_PATH)

    def test_http_403_on_runs_refuses_without_body_echo(self):
        self._assert_http_refusal(403, RUNS_PATH)

    def test_http_429_on_runs_refuses_without_body_echo(self):
        self._assert_http_refusal(429, RUNS_PATH)

    # -- positive control: valid fixtures still propose ------------------

    def test_valid_fixtures_emit_benign_proposal_and_exit_zero(self):
        result = run_proposer(lambda url: _ok([error_agent()]),
                              lambda url: _ok([benign_run()]))
        exit_code, out, err = result
        self.assertEqual(exit_code, 0, f"stdout={out!r} stderr={err!r}")
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, f"one JSONL row: {out!r}")
        record = json.loads(lines[0])
        self.assertEqual(record["type"], "proposal")
        self.assertEqual(record["mutation"], "reset_agent_error")
        self.assertEqual(record["agentId"], "a1")
        self.assertEqual(record["runId"], "r1")
        self.assertIn("proposer: 1 agents read, 1 in error", err)
        for token in ("code=api_response_unparseable",
                      "code=api_response_unexpected_shape",
                      "code=api_request_failed", "retryable=false"):
            self.assertNotIn(token, err)
        self.assertNotIn(API_KEY, out + err)


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[__file__])
