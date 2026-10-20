#!/usr/bin/env python3
"""Offline conformance entry for the disabled fixed-purpose staging-preflight carrier.

No network, no credentials, no host writes: every test stages a fixture install
root under mktemp with a synthetic TEST-ONLY adapter stand-in, a fake clock and
a fake authority environment. The suite traverses the same dispatch function the
installed entrypoint uses; a passing suite never establishes installed execution.

Public-safe: all pins below are synthetic fixtures. No live deployment
identifiers, tracker ids, hosts or credentials appear in this file.
"""

import copy
import getpass
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "host-ops" / "staging_preflight_carrier.py"
SPEC = importlib.util.spec_from_file_location("staging_preflight_carrier", SCRIPT)
carrier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = carrier
SPEC.loader.exec_module(carrier)

NOW = 1800000000  # Fixed fake clock; every dispatch takes an explicit now.
USER = getpass.getuser()
APPROVAL = "fixture-approval-0001"
REQUEST_ID = "fixture-request-0001"
TOKEN_CANARY = "FIXTURE-NOT-A-CREDENTIAL-0000"
STDERR_CANARY = "FIXTURE-STDERR-MUST-NOT-ESCAPE-0000"

# Synthetic targets. Shapes mirror the reviewed adapter's pins; values are fake.
TARGETS = {
    "revision": "aa" * 20,
    "build_id": "10000000001-1",
    "worker_version": "11111111-1111-4111-8111-1111111111ab",
    "application_id": "22222222-2222-4222-8222-2222222222cd",
    "namespace_id": "3333333333333333abcdef0123456789",
    "rollout_id": "44444444-4444-4444-8444-4444444444ef",
    "target_version": 11,
    "image": ("registry.cloudflare.com/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
              "two-bot-next-twobotcontainer-staging@sha256:" + "bb" * 32),
    "origin_sha256": "cc" * 32,
}

# Synthetic TEST-ONLY adapter stand-in. It records its invocation (marker, argv,
# interpreter, env keys, stdin state) next to itself and replays a bounded
# test-controlled stdout fixture. It performs no network I/O and is never
# installed; the carrier under test cannot distinguish it from the fixed
# installed path except by the injected fixture root.
FIXTURE_ADAPTER = """#!/usr/bin/env python3
'''TEST-ONLY fixture adapter. Never installed. No network I/O.'''
import json
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
(here / "adapter-invoked.marker").write_text("invoked\\n")
stdin_empty = sys.stdin.buffer.read() == b""
(here / "adapter-argv.json").write_text(json.dumps({
    "argv": sys.argv,
    "executable": sys.executable,
    "stdin_empty": stdin_empty,
    "env_keys": sorted(__import__("os").environ),
    "path_value": __import__("os").environ.get("PATH"),
}))
sleep = here / "adapter-sleep-seconds"
if sleep.exists():
    time.sleep(float(sleep.read_text().strip()))
err = here / "adapter-stderr.txt"
if err.exists():
    sys.stderr.write(err.read_text())
payload = here / "adapter-stdout-fixture.json"
if payload.exists():
    sys.stdout.write(payload.read_text())
    sys.stdout.flush()
exit_code = here / "adapter-exit-code"
sys.exit(int(exit_code.read_text().strip()) if exit_code.exists() else 0)
"""


def valid_manifest(adapter_sha256):
    return {
        "schema": "staging-preflight-manifest/v1",
        "enabled": True,
        "adapter_sha256": adapter_sha256,
        "approval": {"id": APPROVAL, "expires_at": NOW + 600},
        "principal": {"user": USER},
        "targets": copy.deepcopy(TARGETS),
    }


def valid_request():
    return {
        "schema": "staging-preflight-dispatch/v1",
        "action": "staging-preflight",
        "request_id": REQUEST_ID,
        "principal": USER,
        "approval_id": APPROVAL,
        "expires_at": NOW + 300,
        "targets": copy.deepcopy(TARGETS),
    }


def valid_adapter_receipt():
    control = {key: TARGETS[key] for key in TARGETS if key != "origin_sha256"}
    return {
        "started_utc": "2027-01-15T00:00:00Z",
        "source_sha": "dd" * 20,
        "source_blobs": {"fixture.py": "ee" * 20},
        "result": "pass",
        "runtime_observations": [
            {"surface": "health", "status": 200,
             "worker_version": TARGETS["worker_version"]},
            {"surface": "readyz", "status": 200,
             "worker_version": TARGETS["worker_version"]},
        ],
        "runtime": {"checks": [{"check": "health", "verdict": "pass"}]},
        "control_plane": control,
        "identity_reread": "pass",
        "staging_origin_fence": "pass",
        "lag_tolerance_used": False,
    }


def dispatch_child(args):
    """Top-level for forked concurrency controls; returns (kind, payload)."""
    request_path, adapter_path, manifest_path, state_dir, now, uid = args
    install = carrier.Install(adapter_path=adapter_path,
                              manifest_path=manifest_path,
                              state_dir=state_dir)
    try:
        receipt = carrier.dispatch(request_path, install, now=now,
                                   expected_uid=uid)
    except carrier.Refusal as refused:
        return ("refused", refused.code)
    return ("receipt", receipt["outcome"])


class CarrierTests(unittest.TestCase):
    maxDiff = 4000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="staging-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "adapter").mkdir()
        self.adapter_path = self.root / "adapter" / "staging-preflight-adapter.py"
        self.adapter_path.write_text(FIXTURE_ADAPTER)
        self.adapter_path.chmod(0o644)
        self.manifest_path = self.root / "manifest.json"
        self.request_path = self.root / "request.json"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.install = carrier.Install(adapter_path=str(self.adapter_path),
                                       manifest_path=str(self.manifest_path),
                                       state_dir=str(self.state_dir))
        self.uid = os.geteuid()
        self.request_path.write_text(json.dumps(valid_request()))
        self.request_path.chmod(0o644)
        self.write_manifest(valid_manifest(self.adapter_sha256()))
        self.write_stdout(valid_adapter_receipt())
        self.old_env = dict(os.environ)
        os.environ.pop("https_proxy", None)
        os.environ.pop("http_proxy", None)
        os.environ.pop("all_proxy", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("ALL_PROXY", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def adapter_sha256(self):
        return hashlib.sha256(self.adapter_path.read_bytes()).hexdigest()

    def write_manifest(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest))
        self.manifest_path.chmod(0o644)

    def write_request(self, request):
        self.request_path.write_text(json.dumps(request))

    def write_stdout(self, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        (self.root / "adapter" / "adapter-stdout-fixture.json").write_text(payload)

    def marker(self):
        return self.root / "adapter" / "adapter-invoked.marker"

    def claims(self):
        try:
            names = [p.name for p in self.state_dir.iterdir()]
        except OSError:
            return []
        return sorted(name for name in names if name.endswith(".claim"))

    def receipts(self):
        try:
            names = [p.name for p in self.state_dir.iterdir()]
        except OSError:
            return []
        return sorted(name for name in names if name.endswith(".receipt.json"))

    def dispatch(self, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("expected_uid", self.uid)
        return carrier.dispatch(str(self.request_path), self.install, **kwargs)

    def assertRefused(self, code, **kwargs):
        with self.assertRaises(carrier.Refusal) as caught:
            self.dispatch(**kwargs)
        self.assertEqual(caught.exception.code, code)
        # Refusal means zero I/O: nothing ran, nothing was claimed or stored.
        self.assertFalse(self.marker().exists(), code)
        self.assertEqual(self.claims(), [], code)
        self.assertEqual(self.receipts(), [], code)

    def assertReplayRefused(self, code):
        before = (self.marker().stat().st_mtime_ns if self.marker().exists()
                  else None, self.claims(), self.receipts())
        with self.assertRaises(carrier.Refusal) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, code)
        after = (self.marker().stat().st_mtime_ns if self.marker().exists()
                 else None, self.claims(), self.receipts())
        self.assertEqual(after, before, code)

    # Positive control: the one fixed path that may dispatch.

    def test_positive_fixed_path_dispatch(self):
        receipt = self.dispatch()
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.claims(), [REQUEST_ID + ".claim"])
        self.assertEqual(self.receipts(), [REQUEST_ID + ".receipt.json"])
        stored = json.loads((self.state_dir / self.receipts()[0]).read_text())
        self.assertEqual(stored, receipt)
        self.assertEqual(receipt["kind"], "staging-preflight-dispatch-receipt")
        self.assertEqual(receipt["schema"], "staging-preflight-dispatch-receipt/v1")
        self.assertEqual(receipt["action"], "staging-preflight")
        self.assertEqual(receipt["request_id"], REQUEST_ID)
        self.assertEqual(receipt["approval_id"], APPROVAL)
        self.assertEqual(receipt["principal"], USER)
        self.assertEqual(receipt["adapter_sha256"], self.adapter_sha256())
        self.assertEqual(receipt["targets"], TARGETS)
        self.assertEqual(receipt["outcome"], "pass")
        self.assertIsNone(receipt["failure"])
        self.assertEqual(receipt["adapter_exit"], 0)
        self.assertEqual(receipt["observations"], [
            {"surface": "health", "status": 200, "worker_version_matched": True},
            {"surface": "readyz", "status": 200, "worker_version_matched": True},
        ])
        self.assertEqual(receipt["interval"],
                         {"claimed_at": NOW, "finished_at": NOW,
                          "expires_at": NOW + 300})
        # Minimized: exactly the allowlisted receipt keys, nothing forwarded.
        self.assertEqual(sorted(receipt), [
            "action", "adapter_exit", "adapter_sha256", "approval_id",
            "failure", "interval", "kind", "observations", "outcome",
            "principal", "request_id", "schema", "targets",
        ])

    def test_dispatch_argv_env_and_stdin_are_fixed(self):
        self.dispatch()
        probe = json.loads((self.root / "adapter" / "adapter-argv.json").read_text())
        self.assertEqual(probe["argv"], [str(self.adapter_path)])
        self.assertEqual(probe["executable"], sys.executable)
        self.assertTrue(probe["stdin_empty"])
        self.assertEqual(probe["path_value"], carrier.CHILD_PATH)
        expected = ({key for key in carrier.CHILD_ENV_KEYS if key in os.environ}
                    | set(carrier.FIXED_CHILD_ENV))
        self.assertEqual(set(probe["env_keys"]), expected)
        for proxy in ("https_proxy", "http_proxy", "all_proxy",
                      "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            self.assertNotIn(proxy, probe["env_keys"])

    def test_child_env_copies_only_the_fixed_allowlist(self):
        os.environ["CLOUDFLARE_ACCOUNT_ID"] = "ffffixture-account"
        os.environ["CLOUDFLARE_API_TOKEN"] = TOKEN_CANARY
        os.environ["PAPERCLIP_RUN_SCRATCH_DIR"] = str(self.root)
        os.environ["HOME"] = str(self.root)
        os.environ["CARRIER_ONLY_CANARY"] = "must-not-cross"
        receipt = self.dispatch()
        probe = json.loads((self.root / "adapter" / "adapter-argv.json").read_text())
        self.assertEqual(set(probe["env_keys"]), {
            "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
            "PAPERCLIP_RUN_SCRATCH_DIR", "HOME", "PATH", "LC_ALL", "LANG",
        })
        blob = json.dumps(receipt)
        stored = "".join((self.state_dir / name).read_text()
                         for name in self.claims() + self.receipts())
        self.assertNotIn(TOKEN_CANARY, blob + stored)
        self.assertNotIn("must-not-cross", blob + stored)

    def test_stderr_is_drained_and_never_forwarded(self):
        (self.root / "adapter" / "adapter-stderr.txt").write_text(
            STDERR_CANARY + ": remote exception text\n")
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "pass")
        blob = json.dumps(receipt)
        stored = "".join((self.state_dir / name).read_text()
                         for name in self.claims() + self.receipts())
        self.assertNotIn(STDERR_CANARY, blob + stored)

    def test_production_paths_are_pinned_constants(self):
        self.assertEqual(carrier.ADAPTER_PATH,
                         "/usr/local/libexec/paperclip-host-ops/staging-preflight-adapter.py")
        self.assertEqual(carrier.MANIFEST_PATH,
                         "/etc/paperclip-host-ops/staging-preflight-manifest.json")
        self.assertEqual(carrier.STATE_DIR,
                         "/var/lib/paperclip-host-ops/staging-preflight")
        install = carrier.production_install()
        self.assertEqual(install.adapter_path, carrier.ADAPTER_PATH)
        self.assertEqual(install.manifest_path, carrier.MANIFEST_PATH)
        self.assertEqual(install.state_dir, carrier.STATE_DIR)

    # Request-shape refusals: generic command/argv and undeclared input.

    def test_request_file_must_exist_as_regular_file(self):
        self.request_path.unlink()
        self.assertRefused("request_unreadable")

    def test_request_symlink_refused(self):
        real = self.root / "real-request.json"
        self.request_path.rename(real)
        self.request_path.symlink_to(real)
        self.assertRefused("request_unreadable")

    def test_request_world_writable_refused(self):
        self.request_path.chmod(0o646)
        try:
            self.assertRefused("request_unreadable")
        finally:
            self.request_path.chmod(0o644)

    def test_request_over_cap_refused(self):
        self.request_path.write_bytes(b"#" + b"x" * carrier.MAX_REQUEST_BYTES)
        self.assertRefused("request_too_large")

    def test_request_must_be_a_json_object(self):
        for payload in ("[1,2]", '"text"', "42", "null", "{oops"):
            self.request_path.write_text(payload)
            with self.subTest(payload=payload):
                self.assertRefused("request_schema_refused"
                                   if payload != "{oops" else "request_not_json")

    def test_undeclared_request_fields_refused(self):
        base = valid_request()
        for extra in ("command", "argv", "shell", "endpoints", "metadata_endpoints",
                      "urls", "script", "adapter", "manifest", "state_dir", "env",
                      "proxy", "credentials", "token", "enabled", "debug"):
            request = copy.deepcopy(base)
            request[extra] = "smuggled"
            self.write_request(request)
            with self.subTest(extra=extra):
                self.assertRefused("request_schema_refused")

    def test_fixed_schema_and_action_refused(self):
        for field, value in (("schema", "staging-preflight-dispatch/v2"),
                             ("schema", None), ("schema", 1),
                             ("action", "staging-probe"), ("action", "run-command"),
                             ("action", ""), ("action", None), ("action", ["x"])):
            request = valid_request()
            request[field] = value
            self.write_request(request)
            with self.subTest(field=field, value=value):
                self.assertRefused("request_schema_refused")

    def test_request_id_shape_refused(self):
        for bad in (None, "", 1, "a" * 65, "../escape", "a/b", "..", ".",
                    "has space", "semi;colon", "quote\"q", "back\\slash",
                    "UPPER-ok?"[:0] + "has\ttab", "line\nbreak", "star*"):
            request = valid_request()
            request["request_id"] = bad
            self.write_request(request)
            with self.subTest(bad=bad):
                self.assertRefused("request_schema_refused")

    def test_request_targets_must_match_manifest_exactly(self):
        for key in TARGETS:
            request = valid_request()
            if isinstance(TARGETS[key], int):
                request["targets"][key] = TARGETS[key] + 1
            else:
                request["targets"][key] = request["targets"][key][:-1] + (
                    "0" if request["targets"][key][-1:] != "0" else "1")
            self.write_request(request)
            with self.subTest(key=key):
                self.assertRefused("targets_refused")

    def test_request_targets_shape_refused(self):
        cases = [None, [], "targets", {"revision": "short"},
                 {**TARGETS, "extra": "undeclared"},
                 {k: v for k, v in TARGETS.items() if k != "image"},
                 {**TARGETS, "revision": "AA" * 20},
                 {**TARGETS, "revision": "aa" * 19},
                 {**TARGETS, "build_id": "not-a-build"},
                 {**TARGETS, "build_id": "1-2-3"},
                 {**TARGETS, "worker_version": "not-a-uuid"},
                 {**TARGETS, "worker_version": TARGETS["worker_version"].upper()},
                 {**TARGETS, "namespace_id": "zz" * 16},
                 {**TARGETS, "target_version": 0},
                 {**TARGETS, "target_version": -1},
                 {**TARGETS, "target_version": True},
                 {**TARGETS, "target_version": "11"},
                 {**TARGETS, "image": TARGETS["image"].replace("@sha256:", ":latest")},
                 {**TARGETS, "image": TARGETS["image"].replace(
                     "two-bot-next-twobotcontainer-staging", "other-container")},
                 {**TARGETS, "origin_sha256": "cc" * 31 + "zz"},
                 {**TARGETS, "revision": None}]
        for index, targets in enumerate(cases):
            request = valid_request()
            request["targets"] = targets
            self.write_request(request)
            with self.subTest(index=index):
                self.assertRefused("request_schema_refused")

    # Authorization: absent/expired/over-broad approval and wrong principal.

    def test_unknown_or_absent_approval_refused(self):
        for approval_id in (None, "", 1, "other-approval", "A" * 129,
                            "../escape", "with space"):
            request = valid_request()
            request["approval_id"] = approval_id
            self.write_request(request)
            with self.subTest(approval_id=approval_id):
                self.assertRefused("authorization_refused")

    def test_expired_request_refused(self):
        for expires_at in (NOW, NOW - 1, 0, -5, NOW + 0):
            request = valid_request()
            request["expires_at"] = expires_at
            self.write_request(request)
            with self.subTest(expires_at=expires_at):
                self.assertRefused("request_expired")

    def test_request_expiry_beyond_approval_refused(self):
        request = valid_request()
        request["expires_at"] = NOW + 601
        self.write_request(request)
        self.assertRefused("request_expiry_beyond_approval")

    def test_request_expiry_shape_refused(self):
        for expires_at in (None, True, "1800000300", 1800000300.0, 2 ** 40):
            request = valid_request()
            request["expires_at"] = expires_at
            self.write_request(request)
            with self.subTest(expires_at=expires_at):
                self.assertRefused("request_schema_refused")

    def test_wrong_principal_refused(self):
        request = valid_request()
        request["principal"] = "someone-else"
        self.write_request(request)
        self.assertRefused("principal_mismatch")

    def test_manifest_principal_must_match_invoker(self):
        manifest = valid_manifest(self.adapter_sha256())
        manifest["principal"] = {"user": "someone-else"}
        self.write_manifest(manifest)
        request = valid_request()
        request["principal"] = "someone-else"
        self.write_request(request)
        self.assertRefused("principal_mismatch")

    def test_proxy_environment_refuses_before_io(self):
        for variable in ("https_proxy", "http_proxy", "all_proxy",
                         "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            os.environ[variable] = "http://fixture-proxy.invalid:8080"
            try:
                with self.subTest(variable=variable):
                    self.assertRefused("proxy_environment_refused")
            finally:
                del os.environ[variable]
        self.assertNotIn("fixture-proxy.invalid", json.dumps(dict(os.environ)))

    # Manifest and artifact refusals: disabled default, stale hashes/identity.

    def test_missing_manifest_refused(self):
        self.manifest_path.unlink()
        self.assertRefused("manifest_unreadable")

    def test_manifest_symlink_refused(self):
        real = self.root / "real-manifest.json"
        self.manifest_path.rename(real)
        self.manifest_path.symlink_to(real)
        self.assertRefused("manifest_unreadable")

    def test_shipped_default_is_disabled(self):
        for manifest in (lambda m: {**m, "enabled": False},
                         lambda m: {k: v for k, v in m.items()
                                    if k != "enabled"},
                         lambda m: {**m, "enabled": "true"},
                         lambda m: {**m, "enabled": 1}):
            self.write_manifest(manifest(valid_manifest(self.adapter_sha256())))
            with self.subTest():
                self.assertRefused("carrier_disabled")

    def test_manifest_schema_refused(self):
        base = valid_manifest(self.adapter_sha256())
        cases = [{**base, "extra": 1},
                 {k: v for k, v in base.items() if k != "adapter_sha256"},
                 {**base, "schema": "other/v1"},
                 {**base, "adapter_sha256": "zz" * 32},
                 {**base, "adapter_sha256": "aa" * 31},
                 {**base, "approval": {"id": APPROVAL}},
                 {**base, "approval": {"id": APPROVAL, "expires_at": True}},
                 {**base, "approval": {"id": APPROVAL, "expires_at": 2 ** 40}},
                 {**base, "principal": {"user": "Has Upper"}},
                 {**base, "principal": {}},
                 {**base, "targets": {"revision": "aa" * 20}}]
        for index, manifest in enumerate(cases):
            self.write_manifest(manifest)
            with self.subTest(index=index):
                self.assertRefused("manifest_refused")

    def test_manifest_over_cap_refused(self):
        self.manifest_path.write_bytes(b"#" + b"x" * carrier.MAX_MANIFEST_BYTES)
        self.assertRefused("manifest_too_large")

    def test_stale_approval_refused(self):
        manifest = valid_manifest(self.adapter_sha256())
        manifest["approval"]["expires_at"] = NOW - 1
        self.write_manifest(manifest)
        self.assertRefused("approval_expired")

    def test_tampered_adapter_refused(self):
        with open(self.adapter_path, "ab") as handle:
            handle.write(b"\n# tampered\n")
        self.assertRefused("adapter_digest_mismatch")

    def test_wrong_manifest_digest_refused(self):
        manifest = valid_manifest("ab" * 32)
        self.write_manifest(manifest)
        self.assertRefused("adapter_digest_mismatch")

    def test_adapter_symlink_traversal_or_substitution_refused(self):
        real = self.root / "real-adapter.py"
        self.adapter_path.rename(real)
        self.adapter_path.symlink_to(real)
        self.assertRefused("adapter_unreadable")

    def test_missing_adapter_refused_without_fallback(self):
        self.adapter_path.unlink()
        self.assertRefused("adapter_unreadable")

    def test_group_writable_adapter_refused(self):
        self.adapter_path.chmod(0o664)
        try:
            self.assertRefused("adapter_ownership_refused")
        finally:
            self.adapter_path.chmod(0o644)

    def test_adapter_over_cap_refused(self):
        blob = b"#" + b"x" * carrier.MAX_ADAPTER_BYTES
        self.adapter_path.write_bytes(blob)
        manifest = valid_manifest(hashlib.sha256(blob).hexdigest())
        self.write_manifest(manifest)
        self.assertRefused("adapter_too_large")

    @unittest.skipIf(os.geteuid() == 0, "strict ownership needs a non-root test process")
    def test_non_root_install_refused_in_strict_mode(self):
        with self.assertRaises(carrier.Refusal) as caught:
            self.dispatch(expected_uid=0)
        self.assertIn(caught.exception.code,
                      ("manifest_ownership_refused", "adapter_ownership_refused"))
        self.assertFalse(self.marker().exists())

    def test_world_writable_manifest_refused(self):
        self.manifest_path.chmod(0o646)
        try:
            self.assertRefused("manifest_ownership_refused")
        finally:
            self.manifest_path.chmod(0o644)

    # One-shot state: replay, concurrency, bounds.

    def test_replay_of_consumed_request_refused(self):
        first = self.dispatch()
        self.assertEqual(first["outcome"], "pass")
        self.assertReplayRefused("request_replayed")
        # The first receipt still stands; no second dispatch ran.
        self.assertEqual(self.receipts(), [REQUEST_ID + ".receipt.json"])

    def test_failed_dispatch_still_consumes_one_shot(self):
        self.write_stdout({"queued": True, "request_id": REQUEST_ID})
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "fail")
        self.assertReplayRefused("request_replayed")

    def test_concurrent_replay_dispatches_exactly_once(self):
        context = multiprocessing.get_context("fork")
        args = [(str(self.request_path), str(self.adapter_path),
                 str(self.manifest_path), str(self.state_dir), NOW, self.uid)] * 4
        with context.Pool(4) as pool:
            outcomes = pool.map(dispatch_child, args)
        kinds = sorted(kind for kind, _ in outcomes)
        self.assertEqual(kinds, ["receipt", "refused", "refused", "refused"])
        codes = sorted(payload for kind, payload in outcomes if kind == "refused")
        self.assertEqual(codes, ["request_replayed"] * 3)
        passed = [payload for kind, payload in outcomes if kind == "receipt"]
        self.assertEqual(passed, ["pass"])
        self.assertEqual(self.claims(), [REQUEST_ID + ".claim"])
        self.assertEqual(self.receipts(), [REQUEST_ID + ".receipt.json"])

    def test_missing_state_dir_refused(self):
        self.state_dir.rmdir()
        self.assertRefused("state_refused")

    def test_state_path_must_be_a_directory(self):
        self.state_dir.rmdir()
        self.state_dir.write_text("not a directory")
        self.addCleanup(self.state_dir.unlink)
        self.assertRefused("state_refused")

    def test_world_writable_state_dir_refused(self):
        self.state_dir.chmod(0o707)
        try:
            self.assertRefused("state_refused")
        finally:
            self.state_dir.chmod(0o700)

    def test_exhausted_claim_store_refused(self):
        for index in range(carrier.MAX_CLAIMS):
            (self.state_dir / f"old-{index:04d}.claim").write_text(json.dumps({
                "request_sha256": "ff" * 32, "approval_id": APPROVAL,
                "expires_at": NOW + 3600, "claimed_at": NOW, "pid": os.getpid()}))
        with self.assertRaises(carrier.Refusal) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, "state_exhausted")
        self.assertFalse(self.marker().exists())
        self.assertNotIn(REQUEST_ID + ".claim", self.claims())
        self.assertEqual(self.receipts(), [])

    def test_long_expired_claims_are_pruned(self):
        (self.state_dir / "ancient.claim").write_text(json.dumps({
            "request_sha256": "ff" * 32, "approval_id": APPROVAL,
            "expires_at": NOW - 90000, "claimed_at": NOW - 90000,
            "pid": os.getpid()}))
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "pass")
        self.assertFalse((self.state_dir / "ancient.claim").exists())

    # Dispatch outcome: adapter ran; queue ack is never dispatch proof.

    def test_queue_acknowledgement_is_not_dispatch_proof(self):
        self.write_stdout({"status": "queued", "request_id": REQUEST_ID,
                           "approval_id": APPROVAL})
        receipt = self.dispatch()
        self.assertTrue(self.marker().exists())
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")
        self.assertEqual(receipt["observations"], [])
        self.assertEqual(receipt["kind"], "staging-preflight-dispatch-receipt")

    def test_adapter_crash_yields_failed_dispatch_not_refusal(self):
        (self.root / "adapter" / "adapter-exit-code").write_text("3")
        self.write_stdout("not json at all {{{")
        receipt = self.dispatch()
        self.assertTrue(self.marker().exists())
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["adapter_exit"], 3)
        self.assertEqual(self.receipts(), [REQUEST_ID + ".receipt.json"])

    def test_adapter_failure_code_is_forwarded_shape_checked(self):
        payload = valid_adapter_receipt()
        payload["result"] = "fail"
        payload["failure"] = "health_contract_failed"
        del payload["control_plane"]
        payload["runtime_observations"] = [payload["runtime_observations"][0]]
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "health_contract_failed")
        self.assertEqual(len(receipt["observations"]), 1)

    def test_free_text_failure_is_not_a_fixed_code(self):
        payload = valid_adapter_receipt()
        payload["result"] = "fail"
        payload["failure"] = "boom: remote exception text with secrets"
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")

    def test_dispatch_timeout_consumes_one_shot(self):
        (self.root / "adapter" / "adapter-sleep-seconds").write_text("5")
        receipt = self.dispatch(dispatch_timeout=1)
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "dispatch_timeout")
        self.assertReplayRefused("request_replayed")

    def test_adapter_stdout_over_cap_refused(self):
        self.write_stdout(" " + "x" * (carrier.MAX_STDOUT_BYTES + 1))
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "adapter_output_over_cap")

    def test_unstorable_receipt_is_an_error_not_a_refusal(self):
        # A planted receipt blocks the attested write after the adapter ran:
        # the adapter executed, the claim is consumed, and the caller gets a
        # distinct error (never exit 2, which means the adapter never ran).
        (self.state_dir / (REQUEST_ID + ".receipt.json")).write_text("planted")
        with self.assertRaises(carrier.DispatchError) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, "receipt_unwritable")
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.claims(), [REQUEST_ID + ".claim"])
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = carrier.main(["--request", str(self.request_path)],
                                _install=self.install, _now=NOW, _uid=self.uid)
        # Second attempt replays (the claim stands); a fresh id is required.
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "REFUSED: request_replayed\n")

    def test_unstorable_receipt_cli_exit_code(self):
        (self.state_dir / (REQUEST_ID + ".receipt.json")).write_text("planted")
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = carrier.main(["--request", str(self.request_path)],
                                _install=self.install, _now=NOW, _uid=self.uid)
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "ERROR: receipt_unwritable\n")

    # Two-GET ceiling: order, early stop, identity recheck on observations.

    def test_three_observations_violate_the_ceiling(self):
        payload = valid_adapter_receipt()
        payload["runtime_observations"].append(payload["runtime_observations"][0])
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")

    def test_observation_order_is_fixed(self):
        payload = valid_adapter_receipt()
        payload["runtime_observations"].reverse()
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")

    def test_readyz_after_failed_health_violates_early_stop(self):
        payload = valid_adapter_receipt()
        payload["runtime_observations"][0]["status"] = 500
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")

    def test_single_failed_health_observation_is_allowed(self):
        payload = valid_adapter_receipt()
        payload["result"] = "fail"
        payload["failure"] = "runtime_http_refused"
        payload["runtime_observations"] = [
            {"surface": "health", "status": 500,
             "worker_version": TARGETS["worker_version"]}]
        del payload["control_plane"]
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["outcome"], "fail")
        self.assertEqual(receipt["failure"], "runtime_http_refused")
        self.assertEqual(receipt["observations"], [
            {"surface": "health", "status": 500, "worker_version_matched": True}])

    def test_observation_worker_identity_mismatch_refused(self):
        payload = valid_adapter_receipt()
        payload["runtime_observations"][1]["worker_version"] = (
            "99999999-9999-4999-8999-999999999999")
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "observed_worker_mismatch")

    def test_observation_shapes_refused(self):
        base = valid_adapter_receipt()["runtime_observations"]
        cases = [None, {}, "observations",
                 [{**base[0], "surface": "health", "extra": 1}],
                 [{**base[0], "surface": "status"}],
                 [{**base[0], "status": 99}],
                 [{**base[0], "status": 600}],
                 [{**base[0], "status": "200"}],
                 [{**base[0], "worker_version": "not-a-uuid"}],
                 [{**base[0], "worker_version": None}],
                 [{k: v for k, v in base[0].items() if k != "status"}]]
        for index, observations in enumerate(cases):
            payload = valid_adapter_receipt()
            payload["runtime_observations"] = observations
            self.write_stdout(payload)
            with self.subTest(index=index):
                receipt = self.dispatch()
                self.assertEqual(receipt["outcome"], "fail")
                self.assertEqual(receipt["failure"], "adapter_receipt_refused")
            # Each attempt consumes its one-shot; rotate the request id.
            request = valid_request()
            request["request_id"] = f"fixture-request-shape-{index:02d}"
            self.write_request(request)

    def test_control_plane_must_match_manifest(self):
        payload = valid_adapter_receipt()
        payload["control_plane"]["revision"] = "bb" * 20
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "control_plane_mismatch")

    def test_pass_requires_control_plane_binding(self):
        payload = valid_adapter_receipt()
        del payload["control_plane"]
        self.write_stdout(payload)
        receipt = self.dispatch()
        self.assertEqual(receipt["failure"], "adapter_receipt_refused")

    def test_unknown_receipt_keys_refused(self):
        for extra in ("metadata", "raw", "config", "headers", "body",
                      "exception", "token", "payload"):
            payload = valid_adapter_receipt()
            payload[extra] = {"smuggled": True}
            self.write_stdout(payload)
            with self.subTest(extra=extra):
                receipt = self.dispatch()
                self.assertEqual(receipt["failure"], "adapter_receipt_refused")
            request = valid_request()
            request["request_id"] = f"fixture-request-{extra}"
            self.write_request(request)

    def test_receipt_result_shape_refused(self):
        for result in (None, True, 0, "PASS", "error", ["pass"]):
            payload = valid_adapter_receipt()
            payload["result"] = result
            self.write_stdout(payload)
            with self.subTest(result=result):
                receipt = self.dispatch()
                self.assertEqual(receipt["failure"], "adapter_receipt_refused")
            request = valid_request()
            request["request_id"] = f"fixture-request-result-{id(result) % 100000}"
            self.write_request(request)

    # CLI layer: one input flag, fixed paths, exit-code mapping.

    def test_cli_positive_mapping_returns_zero(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = carrier.main(["--request", str(self.request_path)],
                                _install=self.install, _now=NOW, _uid=self.uid)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["outcome"], "pass")
        self.assertEqual(err.getvalue(), "")

    def test_cli_exit_codes_and_streams(self):
        def run(args, **seams):
            import io
            from contextlib import redirect_stdout, redirect_stderr
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = carrier.main(args, **seams)
            return code, out.getvalue(), err.getvalue()

        code, out, err = run(["--request", str(self.request_path)],
                             _install=self.install, _now=NOW, _uid=self.uid)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "pass")
        self.assertEqual(err, "")

        self.write_stdout({"queued": True})
        request = valid_request()
        request["request_id"] = "fixture-request-queued"
        self.write_request(request)
        code, out, err = run(["--request", str(self.request_path)],
                             _install=self.install, _now=NOW, _uid=self.uid)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["outcome"], "fail")
        self.assertEqual(err, "")

        request["request_id"] = "fixture-request-refused-claimed"
        self.write_request(request)
        self.dispatch()  # consume the one-shot
        code, out, err = run(["--request", str(self.request_path)],
                             _install=self.install, _now=NOW, _uid=self.uid)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "REFUSED: request_replayed\n")

    def test_cli_has_no_path_activation_or_execution_flags(self):
        for arg in ("--install", "--activate", "--enable", "--adapter",
                    "--manifest", "--state-dir", "--install-root",
                    "--exec", "--command", "--argv", "--shell", "--force",
                    "--conformance", "--fixture", "--principal", "--uid",
                    "--timeout", "--env", "--find-adapter"):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--request", str(self.request_path),
                 arg], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, arg)
            self.assertEqual(result.stdout, b"", arg)

    def test_cli_missing_request_flag_refused(self):
        result = subprocess.run([sys.executable, str(SCRIPT)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")

    def test_cli_production_paths_refuse_without_install(self):
        missing = self.root / "no-request.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--request", str(missing)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "REFUSED: request_unreadable\n")


if __name__ == "__main__":
    unittest.main()
