#!/usr/bin/env python3
# ===========================================================================
# test_paperclip_api_base_validation_py.py -- hermetic suite for BASE host
# validation on the Python PAPERCLIP_API_KEY senders.
#
# Every sender below derives its base from PAPERCLIP_API_URL/--api-url and
# attaches the run/board bearer to it. A base that is not an http(s) URL
# with a non-empty bare host -- wrong scheme, userinfo, whitespace,
# query/fragment, curl-glob hosts, empty or non-numeric ports -- must be
# refused BEFORE any request is built, so the bearer is never sent to a
# base we refused to trust. A well-formed hostile host still passes by
# design (that needs an operator-owned host allowlist); this gate pins the
# malformed-env layer, mirroring test_paperclip_api_base_validation.sh.
#
# Fully offline: urllib.request.urlopen is mocked, no network, no
# credentials (canary values only).
#
#   python3 -B -m unittest -v test_paperclip_api_base_validation_py
# ===========================================================================

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))
sys.path.insert(0, os.path.join(HERE, "ops", "case_b5184419"))
sys.path.insert(0, os.path.join(HERE, "ops", "case_b1d141af"))
sys.path.insert(0, os.path.join(HERE, "ops", "case_60e07cc2"))
sys.path.insert(0, os.path.join(HERE, "ops", "case_34c2c99f"))
sys.path.insert(0, os.path.join(HERE, "ops", "case_af881f62"))

import agent_error_proposer as proposer  # noqa: E402
import model_pin_cleanup as model_pin  # noqa: E402
import operator_workspace_reconciler as reconciler  # noqa: E402
import operator_workspace_sweep as sweep  # noqa: E402
import process_lost_probe as lost_probe  # noqa: E402
import prune_operator_run_dirs as run_dirs  # noqa: E402
import project_worktree_regen_prune as regen  # noqa: E402
import recovery_writer as rw  # noqa: E402
import terminal_worktree_cache_reclaim as reclaim  # noqa: E402
import terminal_worktree_prune as prune  # noqa: E402
import websearch_lane_probe as probe  # noqa: E402

CANARY = "pc_CANARY_base_validation_key"
COMPANY = "co-stub"
GOOD = "https://paperclip.invalid/api"

# Kept identical to MALFORMED in test_paperclip_api_base_validation.sh:
# both gates refuse the same set.
MALFORMED = [
    "ftp://board.invalid/api",
    "file:///etc/passwd",
    "notaurl",
    "https://board@evil.invalid/api",
    "https://board.invalid/api with space",
    "http:///no-host",
    "https://paperclip.invalid/api\nheader = \"X: injected\"",
    "https://:8080/api",
    "http://?q",
    "http://#frag",
    "https://host.invalid/api?x=1",
    "https://host.invalid/api#frag",
    "http://{127.0.0.1,127.0.0.2}/api",
    "http://127.0.0.[1-2]/api",
    "https://host:badport/api",
    "http://ho,st.invalid/api",
    "https://host:/x",
    "http://host:80:90/",
    "https://ho\x01st.invalid/api",
    "http://ho%09st.invalid/api",
]


def canned_json(payload: bytes):
    """A urlopen mock whose context manager yields a canned JSON body."""
    response = mock.MagicMock()
    response.read.return_value = payload
    opener = mock.MagicMock()
    opener.__enter__.return_value = response
    return opener


def request_url(call):
    """Full URL of a urlopen(Request) call."""
    return call.args[0].full_url


class ProposerBase(unittest.TestCase):
    def test_malformed_bases_refuse_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]), \
                    mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(RuntimeError):
                    proposer.api_get(base, CANARY, "/api/companies/co/agents")
                self.assertEqual(urlopen.call_count, 0)

    def test_well_formed_base_still_reads(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b"[]")
            rows = proposer.api_get(GOOD, CANARY, "/api/companies/co/agents")
        self.assertEqual(rows, [])
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class RecoveryWriterBase(unittest.TestCase):
    def test_malformed_bases_refuse_at_construction(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]):
                with self.assertRaises(rw.WriterError):
                    rw.PaperclipClient(base, CANARY)

    def test_well_formed_base_still_reads(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b'{"id":"issue-stub"}')
            client = rw.PaperclipClient(GOOD, CANARY)
            issue = client.get("/api/issues/issue-stub")
        self.assertEqual(issue, {"id": "issue-stub"})
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class WebsearchProbeBase(unittest.TestCase):
    def env(self, base, **extra):
        environ = {"PAPERCLIP_API_URL": base, "PAPERCLIP_API_KEY": CANARY}
        environ.update(extra)
        return environ

    def test_malformed_bases_refuse_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]):
                with self.assertRaises(ValueError):
                    probe._paperclip_api_base(self.env(base))

    def test_secret_fetch_skips_malformed_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]), \
                    mock.patch("urllib.request.urlopen") as urlopen:
                self.assertIsNone(
                    probe.fetch_run_secret("k", self.env(base)))
                self.assertEqual(urlopen.call_count, 0)

    def test_alarm_refuses_malformed_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]), \
                    mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    probe.post_board_alarm(
                        [], self.env(base, PAPERCLIP_TASK_ID="issue-stub",
                                     PAPERCLIP_RUN_ID="run-stub"))
                self.assertEqual(urlopen.call_count, 0)

    def test_well_formed_base_still_fetches(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b'{"value":"fetched-key"}')
            value = probe.fetch_run_secret("k", self.env(GOOD))
        self.assertEqual(value, "fetched-key")
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class ModelPinCleanupBase(unittest.TestCase):
    def test_malformed_bases_refuse_at_construction(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]):
                with self.assertRaises(model_pin.CleanupError):
                    model_pin.PaperclipClient(base, CANARY, COMPANY)

    def test_well_formed_base_still_reads(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b"{}")
            client = model_pin.PaperclipClient(GOOD, CANARY, COMPANY)
            self.assertEqual(client.get("/api/issues/i"), {})
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class ProcessLostProbeBase(unittest.TestCase):
    def test_malformed_bases_refuse_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]), \
                    mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    lost_probe.fetch_runs(base, CANARY, COMPANY, 10)
                self.assertEqual(urlopen.call_count, 0)

    def test_well_formed_base_still_reads(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b"[]")
            self.assertEqual(
                lost_probe.fetch_runs(GOOD, CANARY, COMPANY, 10), [])
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class ReclaimClientBase(unittest.TestCase):
    def test_malformed_bases_refuse_at_construction(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]):
                with self.assertRaises(reclaim.ReclaimError):
                    reclaim.ApiClient(base, COMPANY, CANARY)

    def test_well_formed_base_still_reads(self):
        # ApiClient._get feeds the urlopen return straight to json.load
        # (no context manager), so the mock answers read() directly.
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.read.return_value = "[]"
            client = reclaim.ApiClient(GOOD, COMPANY, CANARY)
            self.assertEqual(client.heartbeat_runs(), [])
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))


class ImporterMainsBase(unittest.TestCase):
    IMPORTERS = (prune, regen, run_dirs)

    def test_malformed_bases_refuse_dry_run_with_zero_requests(self):
        for module in self.IMPORTERS:
            for base in MALFORMED:
                env = {"PAPERCLIP_API_URL": base,
                       "PAPERCLIP_COMPANY_ID": COMPANY,
                       "PAPERCLIP_API_KEY": CANARY}
                with self.subTest(module=module.__name__, base=base[:48]), \
                        mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaises(module.PruneError):
                        module.main(argv=[], api=None, env=env)
                    self.assertEqual(urlopen.call_count, 0)

    def test_malformed_bases_refuse_apply_as_safety(self):
        for module in self.IMPORTERS:
            for base in MALFORMED:
                env = {"PAPERCLIP_API_URL": base,
                       "PAPERCLIP_COMPANY_ID": COMPANY,
                       "PAPERCLIP_API_KEY": CANARY}
                with self.subTest(module=module.__name__, base=base[:48]), \
                        mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaises(module.SafetyRefusal):
                        module.main(argv=["--apply"], api=None, env=env)
                    self.assertEqual(urlopen.call_count, 0)


class ReconcilerBase(unittest.TestCase):
    def test_malformed_bases_refuse_at_construction(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]):
                with self.assertRaises(reconciler.ReconcileError):
                    reconciler.PaperclipClient(base, CANARY, COMPANY)

    def test_well_formed_base_still_reads(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = canned_json(b"{}")
            client = reconciler.PaperclipClient(GOOD, CANARY, COMPANY)
            self.assertEqual(client.get("/api/issues/i"), {})
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(
            request_url(urlopen.call_args).startswith("https://paperclip.invalid/"))

    def test_sweep_entry_refuses_malformed_with_zero_requests(self):
        for base in MALFORMED:
            with self.subTest(base=base[:48]), \
                    mock.patch("urllib.request.urlopen") as urlopen, \
                    mock.patch.dict(os.environ,
                                    {"PAPERCLIP_API_URL": base,
                                     "PAPERCLIP_API_KEY": CANARY,
                                     "PAPERCLIP_COMPANY_ID": COMPANY,
                                     "PAPERCLIP_RUN_ID": "run-stub"}), \
                    tempfile.TemporaryDirectory() as state_dir:
                self.assertEqual(
                    sweep.main(argv=["--state-dir", state_dir]), 1)
                self.assertEqual(urlopen.call_count, 0)


class ValidatorParity(unittest.TestCase):
    """Every copy of the gate refuses the same malformed set."""

    MODULES = (
        proposer,
        rw,
        probe,
        model_pin,
        lost_probe,
        reclaim,
        prune,
        regen,
        run_dirs,
        reconciler,
    )

    def test_all_gates_agree(self):
        cases = MALFORMED + ["", "https://", "http://",
                             "https://host with space/x"]
        for module in self.MODULES:
            gate = getattr(module, "_valid_api_base", None)
            self.assertIsNotNone(
                gate, f"{module.__name__} has no _valid_api_base")
            with self.subTest(gate=module.__name__):
                for base in cases:
                    self.assertFalse(gate(base), base[:48])
                for base in (GOOD, "https://paperclip.invalid",
                             "http://127.0.0.1:9", "https://host:8443/base",
                             "http://[::1]:9", "https://evil.invalid/api"):
                    self.assertTrue(gate(base), base[:48])


if __name__ == "__main__":
    unittest.main()
