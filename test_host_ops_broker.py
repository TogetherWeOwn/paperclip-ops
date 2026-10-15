#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BROKER = ROOT / "host-ops" / "broker.py"
SPEC = importlib.util.spec_from_file_location("host_ops_broker", BROKER)
assert SPEC and SPEC.loader
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)
# case_5039e77b: installer-owned helper (case_43e41fc3). Loaded read-only so the owned
# grant-pin test can capture its exact sudo argv; its templates stay untouched.
BINDING = ROOT / "host-ops" / "image_binding.py"
BINDING_SPEC = importlib.util.spec_from_file_location("host_ops_image_binding", BINDING)
assert BINDING_SPEC and BINDING_SPEC.loader
binding = importlib.util.module_from_spec(BINDING_SPEC)
BINDING_SPEC.loader.exec_module(binding)
# case_2f1968ad: journaled-upgrade contract sources, loaded read-only like the
# binding above. The owned installer-contract test asserts the fixed FILES
# inventory, publication modes and reader-group set as live values (not
# retyped literals); behavioural publication/rollback stays in the focused
# temporary-root transaction tests. Implementation files stay untouched.
sys.dont_write_bytecode = True
STATE_PATH = ROOT / "host-ops" / "install_state.py"
STATE_SPEC = importlib.util.spec_from_file_location("install_state", STATE_PATH)
assert STATE_SPEC and STATE_SPEC.loader
install_state = importlib.util.module_from_spec(STATE_SPEC)
sys.modules["install_state"] = install_state
STATE_SPEC.loader.exec_module(install_state)
# case_43e41fc3: the transaction's sibling provisioning helper, loaded read-only
# like the state module above so the top-level sibling import resolves.
PROV_PATH = ROOT / "host-ops" / "spool_provisioning.py"
PROV_SPEC = importlib.util.spec_from_file_location("spool_provisioning", PROV_PATH)
assert PROV_SPEC and PROV_SPEC.loader
spool_provisioning = importlib.util.module_from_spec(PROV_SPEC)
sys.modules["spool_provisioning"] = spool_provisioning
PROV_SPEC.loader.exec_module(spool_provisioning)
TRANSACTION_PATH = ROOT / "host-ops" / "install_transaction.py"
TRANSACTION_SPEC = importlib.util.spec_from_file_location("host_ops_install_transaction", TRANSACTION_PATH)
assert TRANSACTION_SPEC and TRANSACTION_SPEC.loader
install_transaction = importlib.util.module_from_spec(TRANSACTION_SPEC)
TRANSACTION_SPEC.loader.exec_module(install_transaction)

AGENT = "00000000-0000-4000-8000-000000000150"
RUN = "00000000-0000-4000-8000-000000000165"
ISSUE = "00000000-0000-4000-8000-000000000166"
CEO = "00000000-0000-4000-8000-000000000167"
OTHER_AGENT = "00000000-0000-4000-8000-000000000168"
EXPECTED_READ_EXECUTORS = {
    "health.inspect": "health",
    "logs.read": "logs",
    "service.inspect": "service",
    "image.inspect": "image",
    "host.inspect": "host",
    "tooling.locate": "locate",
    "tooling.fingerprint": "fingerprint",
    "paperclip.stat-chain": "stat_chain",
}
EXPECTED_WRITE_VERBS = {
    "paperclip.deploy", "paperclip.restart",
    "plugin.install", "plugin.restart", "unit.install",
    "cliproxy.apply", "coolify.env.set", "script.run",
    "coolify.app.deploy", "coolify.app.restart",
}
# case_b1f57205 Phase-1 write verbs take names and SHAs only -- never host paths,
# commands, URLs, or secrets. Each entry here is the valid argument set used by
# the dry-run and refusal tests. case_802335d7 staging-app verbs follow the same
# rule: deploy takes only {app, sha}, restart only {app}; the Coolify UUID,
# environment, release set and deploy path resolve host-side from the
# versioned stagingApps catalogue.
WRITE_ARGUMENTS = {
    "paperclip.deploy": {"commit": "1" * 40},
    "paperclip.restart": {},
    "plugin.install": {"package": "dispatch-src", "sha": "1" * 40},
    "plugin.restart": {"package": "dispatch-src"},
    "unit.install": {"name": "paperclip-host-ops-broker", "sha": "2" * 40},
    "cliproxy.apply": {"sha": "3" * 40},
    "coolify.env.set": {"app": "paperclip", "keys": "FOO,BAR"},
    "script.run": {"script": "quota_brake", "sha": "4" * 40},
    "coolify.app.deploy": {"app": "two-web-staging", "sha": "a" * 40},
    "coolify.app.restart": {"app": "two-web-staging"},
}
PHASE1_WRITE_VERBS = {
    "plugin.install", "plugin.restart", "unit.install",
    "cliproxy.apply", "coolify.env.set", "script.run",
    "coolify.app.deploy", "coolify.app.restart",
}
# case_802335d7 synthetic catalogue shared by the staging-app tests: two staging
# apps with disjoint pinned releases, never production, never real UUIDs.
STAGING_APPS = {
    "two-web-staging": {
        "coolifyAppUuid": "00000000-0000-4000-8000-000000000003",
        "environment": "staging",
        "allowedReleases": ["a" * 40, "e" * 40],
        "deployPath": "/var/lib/coolify/staging/two-web-staging",
    },
    "two-bot-staging": {
        "coolifyAppUuid": "00000000-0000-4000-8000-000000000020",
        "environment": "staging",
        "allowedReleases": ["b" * 40],
        "deployPath": "/var/lib/coolify/staging/two-bot-staging",
    },
}
EXPECTED_RESULT_KEYS = {
    "health.inspect": {"status", "commit"},
    "logs.read": {"unit", "windowSeconds", "lineLimit", "redaction", "entries"},
    "service.inspect": {"Status", "Running", "Pid", "StartedAt", "FinishedAt", "ExitCode", "OOMKilled", "Restarting"},
    "image.inspect": {"Id", "Digest", "RepoTags", "Created", "Size"},
    "host.inspect": {"hostname", "kernel", "uptimeSeconds", "loadAverage", "paperclipFilesystem"},
    "tooling.locate": {"candidates", "truncated"},
    "tooling.fingerprint": {"root", "files"},
    "paperclip.stat-chain": {"chain"},
}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="host-ops-test."))
        self.spool = self.temp / "spool"
        self.state = self.temp / "state"
        self.audit = self.temp / "audit" / "audit.jsonl"
        self.facts = self.temp / "facts"
        self.facts.mkdir()
        self.tooling = self.temp / "tooling"
        self.tooling.mkdir()
        tool = self.tooling / "probe.sh"
        tool.write_text("#!/bin/sh\nprintf tested\\n")
        tool.chmod(0o755)
        self.private_key = self.temp / "response-signing.pem"
        self.public_key = self.temp / "response-signing.pub.pem"
        subprocess.run([
            "openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
            "-out", str(self.private_key),
        ], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.private_key), "-pubout", "-out", str(self.public_key)], check=True, capture_output=True)
        self.public_key_hash = hashlib.sha256(self.public_key.read_bytes()).hexdigest()
        self.config = {
            "version": 1,
            "spoolPath": str(self.spool),
            "statePath": str(self.state),
            "auditPath": str(self.audit),
            "privateKeyPath": str(self.private_key),
            "healthUrl": "http://127.0.0.1:3100/api/health",
            "paperclipDataPath": str(self.temp),
            "paperclipImageRef": "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b",
            "opsToolingPath": str(self.tooling),
            "toolingSearchRoots": [str(self.temp)],
            "toolingMarkers": ["probe.sh", "tool_drift.sh"],
            "pollSeconds": 2,
            "testMode": True,
            "writesEnabled": False,
            "ceoAgentId": CEO,
            "writeAllowList": {
                "version": 1, "revision": "a" * 40,
                "apps": {"paperclip": {"keys": ["FOO", "BAR"], "mapping": {"name": "test-env", "sha": "b" * 40}}},
                "scripts": {"quota_brake": {
                    "sha": "4" * 40, "writeScope": ["/var/lib/example/state.json"],
                    "inverse": {"script": "quota_restore", "sha": "4" * 40, "testEvidenceSha256": "c" * 64},
                    "compositionReviewSha256": "d" * 64,
                }},
                "stagingApps": {
                    "two-web-staging": {
                        "coolifyAppUuid": "00000000-0000-4000-8000-000000000003",
                        "environment": "staging",
                        "allowedReleases": ["a" * 40, "e" * 40],
                        "deployPath": "/var/lib/coolify/staging/two-web-staging",
                    },
                    "two-bot-staging": {
                        "coolifyAppUuid": "00000000-0000-4000-8000-000000000020",
                        "environment": "staging",
                        "allowedReleases": ["b" * 40],
                        "deployPath": "/var/lib/coolify/staging/two-bot-staging",
                    },
                },
            },
        }
        self.config_path = self.temp / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        # The logs seam carries RAW `docker logs --timestamps` lines so the
        # fixture exercises the production parse-and-redact path; message text
        # (here "must-not-escape") is redacted by the executor, like production.
        fixtures = {
            "health": {"status": "ok", "commit": "1" * 40},
            "logs": ["2026-10-04T23:00:00.123456789Z must-not-escape"],
            "service": {"Status": "running", "Running": True, "Pid": 123, "StartedAt": "2026-10-04T22:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z", "ExitCode": 0, "OOMKilled": False, "Restarting": False},
            "image": {"Id": "sha256:" + "2" * 64, "Digest": None, "RepoTags": ["localhost/paperclip-local:latest"], "Created": None, "Size": None},
            "host": {"hostname": "host.example", "kernel": "6.8.0", "uptimeSeconds": 1234, "loadAverage": [0.1, 0.2, 0.3], "paperclipFilesystem": {"total": 1000, "used": 400, "free": 600}},
            "locate": {"candidates": [{"path": "/opt/paperclip-ops", "markers": ["probe.sh", "tool_drift.sh"]}], "truncated": False},
            "fingerprint": {"root": "/opt/paperclip-ops", "files": [{"path": "probe.sh", "sha256": "3" * 64, "size": 20}]},
            "stat_chain": {"chain": [{"path": "/home/ubuntu/stacks/paperclip/data", "uid": 1000, "gid": 1000, "mode": "0755", "type": "directory"}]},
        }
        for name, payload in fixtures.items():
            (self.facts / f"{name}.json").write_text(json.dumps(payload))
        self.old_env = os.environ.copy()
        os.environ.update({"HOST_OPS_TEST_MODE": "1", "HOST_OPS_FAKE_FACTS_DIR": str(self.facts)})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.temp)

    def make_request(self, verb="health.inspect", request_id="HRQ-TEST001", ttl=300, age=0, arguments=None,
                      reason=None, approval=None, drain_receipt=None):
        requested = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=age)
        request = {
            "version": 1,
            "requestId": request_id,
            "requestedAt": broker.iso(requested),
            "expiresAt": broker.iso(requested + dt.timedelta(seconds=ttl)),
            "verb": verb,
            "arguments": {} if arguments is None else arguments,
            "requester": {"agentId": AGENT, "runId": RUN, "issueId": ISSUE},
        }
        if reason is not None:
            request["reason"] = reason
        if approval is not None:
            request["approval"] = approval
        if drain_receipt is not None:
            request["drainReceipt"] = drain_receipt
        request["canonicalHash"] = hashlib.sha256(canonical(request)).hexdigest()
        return request

    def valid_approval(self, approver=CEO, age=0, decision_ref="case_5248bcfa"):
        decided = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=age)
        return {"approver": approver, "decisionRef": decision_ref, "decidedAt": broker.iso(decided)}

    def valid_drain_receipt(self, running_runs=0, new_dispatch_stopped=True, age=0):
        checked = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=age)
        return {"checkedAt": broker.iso(checked), "runningRuns": running_runs, "newDispatchStopped": new_dispatch_stopped}

    def make_write_request(self, verb, request_id, arguments=None, reason="reviewed and approved for this card",
                            approval=None, drain_receipt=None, **kwargs):
        tier = broker.verb_tier(verb)
        approval = self.valid_approval() if approval is None else approval
        if tier == "paperclip-restart" and drain_receipt is None:
            drain_receipt = self.valid_drain_receipt()
        return self.make_request(verb, request_id, arguments=arguments, reason=reason, approval=approval,
                                  drain_receipt=drain_receipt, **kwargs)

    def enqueue(self, request, suffix="request"):
        directory = self.spool / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{request['requestId']}.{suffix}.json"
        path.write_bytes(canonical(request))
        return path

    def run_once(self):
        self.private_key.chmod(0o600)
        completed = subprocess.run(
            [sys.executable, str(BROKER), "run-once", "--config", str(self.config_path)],
            env=os.environ, text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def outputs(self, kind):
        return sorted((self.spool / kind).glob("*.bundle/*.json"))

    def read_json(self, path):
        return json.loads(path.read_text())

    def verify_signed(self, path):
        sig = path.with_suffix(".sig")
        completed = subprocess.run([
            sys.executable, str(BROKER), "verify", "--public-key", str(self.public_key),
            "--expected-key-sha256", self.public_key_hash, "--response", str(path), "--signature", str(sig),
        ], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_literal_authority_surface_is_pinned(self):
        self.assertEqual(set(broker.READ_VERBS), set(EXPECTED_READ_EXECUTORS))
        self.assertEqual(set(broker.WRITE_VERB_SPECS), EXPECTED_WRITE_VERBS)
        self.assertTrue(set(broker.READ_VERBS).isdisjoint(broker.WRITE_VERB_SPECS))
        self.assertEqual(
            {verb: spec["executor"] for verb, spec in broker.READ_VERBS.items()},
            EXPECTED_READ_EXECUTORS,
        )
        self.assertEqual(set(broker.EXECUTORS), set(EXPECTED_READ_EXECUTORS.values()))

    def test_every_literal_read_verb_requests_previews_executes_and_verifies(self):
        for index, verb in enumerate(EXPECTED_READ_EXECUTORS, 1):
            with self.subTest(verb=verb):
                self.enqueue(self.make_request(verb, f"HRQ-READ{index:03d}"), suffix=str(index))
        self.run_once()
        self.assertEqual(len(self.outputs("previews")), len(EXPECTED_READ_EXECUTORS))
        responses = self.outputs("responses")
        self.assertEqual(len(responses), len(EXPECTED_READ_EXECUTORS))
        by_verb = {}
        for response_path in responses:
            response = self.verify_signed(response_path)
            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["code"], "ok")
            self.assertEqual(response["audit"]["entryHash"].__len__(), 64)
            by_verb[response["verb"]] = response
        self.assertEqual(set(by_verb), set(EXPECTED_READ_EXECUTORS))
        for verb, expected_keys in EXPECTED_RESULT_KEYS.items():
            with self.subTest(result=verb):
                self.assertEqual(set(by_verb[verb]["result"]), expected_keys)
        previews = [self.verify_signed(path) for path in self.outputs("previews")]
        self.assertEqual({item["verb"] for item in previews}, set(EXPECTED_READ_EXECUTORS))
        self.assertTrue(all(item["executorRendered"] for item in previews))
        self.assertTrue(all(item["writes"] == [] for item in previews))
        self.assertNotIn("must-not-escape", json.dumps([self.read_json(p) for p in responses]))

    def test_unknown_and_argument_bearing_requests_are_refused(self):
        self.enqueue(self.make_request("shell.run", "HRQ-UNKNOWN1"), "unknown")
        self.enqueue(self.make_request("health.inspect", "HRQ-ARGS001", arguments={"url": "http://evil"}), "args")
        self.run_once()
        codes = {self.read_json(path)["code"] for path in self.outputs("responses")}
        self.assertEqual(codes, {"unknown_verb", "invalid_arguments"})
        self.assertEqual(self.outputs("previews"), [])

    def test_logs_redact_all_text_before_signing_and_persisting(self):
        # Assembled from fragments so the literal canary never appears in this
        # file as a whole string: CI's Secret scan greps tracked files for the
        # PEM-header shape, and a suite that trips the repo's own scan fails
        # the Offline job (the same convention other offline suites use).
        pem = ("-----BEGIN " + "PRIVATE KEY-----\nfixture-key\n"
               + "-----END " + "PRIVATE KEY-----")
        secrets = ["Bearer fixture-token", "password=fixture-password", "unlabelled-fixture-secret",
                   pem]
        # Docker timestamps each container line separately, so a multiline
        # message arrives as one timestamped line per fragment. Flatten the
        # same way: every fragment must be redacted, none may escape.
        fragments = [fragment for secret in secrets for fragment in secret.split("\n")]
        lines = [f"2026-10-04T23:00:{i:02d}.123456789Z {fragment}" for i, fragment in enumerate(fragments)]
        (self.facts / "logs.json").write_text(json.dumps(lines))
        self.enqueue(self.make_request("logs.read", "HRQ-LOGREDACT"))
        self.run_once()
        path = self.outputs("responses")[0]
        response = self.verify_signed(path)
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(len(response["result"]["entries"]), len(lines))
        self.assertTrue(all(e["message"] == "[REDACTED]" for e in response["result"]["entries"]))
        self.assertLessEqual(path.stat().st_size, 65536)
        # Include staging/preview/state/audit, not just the returned response.
        for root in [self.spool, self.state, self.audit.parent]:
            for stored in root.rglob("*"):
                if stored.is_file():
                    payload = stored.read_bytes()
                    for secret in secrets:
                        self.assertNotIn(secret.encode(), payload, str(stored))
                        self.assertNotIn(json.dumps(secret)[1:-1].encode(), payload, str(stored))

    def test_logs_accept_no_scope_or_limit_arguments(self):
        for index, args in enumerate([{"container": "other"}, {"since": "-1day"},
                                      {"lines": 201}, {"path": "/etc/shadow"}]):
            self.enqueue(self.make_request("logs.read", f"HRQ-LOGARGS{index}", arguments=args))
        self.run_once()
        self.assertEqual(len(self.outputs("responses")), 4)
        self.assertTrue(all(self.verify_signed(p)["code"] == "invalid_arguments" for p in self.outputs("responses")))
        self.assertEqual(self.outputs("previews"), [])

    def test_logs_line_limit_empty_tail_and_result_schema(self):
        # limit-1 timestamped lines plus one undated framing line: the undated
        # line is dropped, so 200 raw lines still yield 199 entries.
        line = "2026-10-04T23:00:00.123456789Z fixture-secret"
        for count in [0, 200]:
            (self.facts / "logs.json").write_text(json.dumps([line] * (count - 1) + ["partial write without timestamp"] if count else []))
            result = broker.validate_result("logs.read", broker.execute_logs(self.config))
            self.assertEqual(len(result["entries"]), 0 if count == 0 else count - 1)
            self.assertTrue(all(entry["priority"] == broker.LOG_PRIORITY for entry in result["entries"]))
            self.assertLess(len(canonical(result)), 65536)
        (self.facts / "logs.json").write_text(json.dumps([line] * 201))
        with self.assertRaises(broker.Refusal) as raised:
            broker.execute_logs(self.config)
        self.assertEqual(raised.exception.code, "invalid_logs")
        result["entries"][0]["message"] = "fixture-secret"
        with self.assertRaises(broker.Refusal) as raised:
            broker.validate_result("logs.read", result)
        self.assertEqual(raised.exception.code, "invalid_logs")
        for invalid in [["not a timestamped line"], "not-a-list", [123]]:
            (self.facts / "logs.json").write_text(json.dumps(invalid))
            with self.assertRaises(broker.Refusal):
                broker.execute_logs(self.config)

    def test_logs_subprocess_raw_byte_cap_timeout_and_stderr_suppression(self):
        from unittest.mock import patch
        cases = [
            ("import sys; sys.stdout.write('x' * 65537)", "logs_too_large", 5),
            ("import time; time.sleep(10)", "logs_timeout", 0.05),
            ("import sys; sys.stderr.write('fixture-secret'); sys.exit(1)", "host_read_failed", 5),
        ]
        for code, expected, timeout in cases:
            with self.subTest(expected=expected), patch.object(broker, "LOG_COMMAND", (sys.executable, "-c", code)):
                with self.assertRaises(broker.Refusal) as raised:
                    broker.read_log_bytes(timeout=timeout)
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn("fixture-secret", str(raised.exception))
        # Positive controls at the byte limit and on the parse path.
        with patch.object(broker, "LOG_COMMAND", (sys.executable, "-c", "print('x' * 65535)")):
            self.assertEqual(len(broker.read_log_bytes()), 65536)
        os.environ.pop("HOST_OPS_FAKE_FACTS_DIR")
        record = b"2026-10-04T23:00:00.123456789Z fixture-secret"
        with patch.object(broker, "read_log_bytes", return_value=record):
            entries = broker.execute_logs(self.config)["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["message"], "[REDACTED]")
            self.assertNotIn("fixture-secret", json.dumps(entries))
        with patch.object(broker, "read_log_bytes", return_value=b'fixture-secret-not-timestamped'):
            with self.assertRaises(broker.Refusal) as raised:
                broker.execute_logs(self.config)
            self.assertNotIn("fixture-secret", str(raised.exception))

    def test_logs_total_response_cap_applies_before_bundle_publication(self):
        from unittest.mock import patch
        self.enqueue(self.make_request("logs.read", "HRQ-LOGCAP01"))
        # Leave one byte beyond the result; the signed envelope cannot fit.
        limit = len(canonical(broker.execute_logs(self.config))) + 1
        with patch.object(broker, "LOG_RESPONSE_BYTE_LIMIT", limit):
            broker.Broker(self.config).run_once()
        response = self.verify_signed(self.outputs("responses")[0])
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["code"], "logs_too_large")
        self.assertIsNone(response["result"])

    def test_expiry_and_long_ttl_are_refused(self):
        self.enqueue(self.make_request("health.inspect", "HRQ-EXPIRED", ttl=60, age=120), "expired")
        self.enqueue(self.make_request("health.inspect", "HRQ-LONGTTL", ttl=601), "long")
        self.run_once()
        codes = {self.read_json(path)["code"] for path in self.outputs("responses")}
        self.assertEqual(codes, {"expired", "ttl_too_long"})
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            self.assertEqual(db.execute("select count(*) from used_requests where request_id='HRQ-EXPIRED'").fetchone()[0], 0)

    def test_replay_is_refused_even_after_success(self):
        request = self.make_request("health.inspect", "HRQ-REPLAY1")
        self.enqueue(request, "first")
        self.run_once()
        self.enqueue(request, "second")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual([item["status"] for item in responses].count("succeeded"), 1)
        self.assertEqual([item["code"] for item in responses].count("replay"), 1)
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            self.assertEqual(db.execute("select count(*) from used_requests where request_id='HRQ-REPLAY1'").fetchone()[0], 1)

    def test_retained_claim_resumes_instead_of_becoming_replay(self):
        request = self.make_request("health.inspect", "HRQ-RESUME1")
        claimed = self.spool / "processing" / "retained-claim.json"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(canonical(request))
        replay = broker.ReplayStore(self.state / "state.sqlite3")
        self.assertEqual(replay.reserve(request["requestId"], request["canonicalHash"], claimed.name), "new")
        replay.db.close()

        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["status"], "succeeded")
        self.assertNotEqual(responses[0]["code"], "replay")
        self.assertFalse(claimed.exists())
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            self.assertEqual(db.execute("select final_status from used_requests where request_id='HRQ-RESUME1'").fetchone()[0], "succeeded")

    def test_legacy_pending_reservation_is_adopted_by_retained_claim(self):
        request = self.make_request("health.inspect", "HRQ-LEGACY1")
        claimed = self.spool / "processing" / "legacy-claim.json"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(canonical(request))
        replay = broker.ReplayStore(self.state / "state.sqlite3")
        with replay.db:
            replay.db.execute(
                "INSERT INTO used_requests(request_id, canonical_hash, claim_id, accepted_at) VALUES(?,?,NULL,?)",
                (request["requestId"], request["canonicalHash"], broker.iso(broker.now_utc())),
            )
        replay.db.close()

        self.run_once()
        response = self.read_json(self.outputs("responses")[0])
        self.assertEqual(response["status"], "succeeded")
        self.assertNotEqual(response["code"], "replay")
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            claim_id, status = db.execute(
                "select claim_id, final_status from used_requests where request_id='HRQ-LEGACY1'"
            ).fetchone()
        self.assertEqual((claim_id, status), (claimed.name, "succeeded"))

    def test_completed_retained_claim_is_cleaned_without_second_terminal_response(self):
        request = self.make_request("health.inspect", "HRQ-COMPLETE1")
        claimed = self.spool / "processing" / "completed-claim.json"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(canonical(request))
        replay = broker.ReplayStore(self.state / "state.sqlite3")
        self.assertEqual(replay.reserve(request["requestId"], request["canonicalHash"], claimed.name), "new")
        replay.finish(request["requestId"], "succeeded")
        replay.db.close()

        self.run_once()
        self.assertEqual(self.outputs("responses"), [])
        self.assertFalse(claimed.exists())

    def test_corrupt_existing_terminal_bundle_preserves_claim_and_fails_closed(self):
        request = self.make_request("health.inspect", "HRQ-CORRUPT1")
        claimed = self.spool / "processing" / "corrupt-terminal-claim.json"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(canonical(request))
        replay = broker.ReplayStore(self.state / "state.sqlite3")
        self.assertEqual(replay.reserve(request["requestId"], request["canonicalHash"], claimed.name), "new")
        replay.db.close()
        basename = f"{request['requestId']}.{request['canonicalHash'][:12]}"
        response, _ = broker.persist_signed(self.spool / "responses", basename, {"corrupt": True}, self.private_key)
        response.write_bytes(b"tampered\n")

        with self.assertRaises(broker.FatalStateError):
            broker.Broker(self.config).run_once()
        self.assertTrue(claimed.exists())
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            self.assertIsNone(db.execute("select final_status from used_requests where request_id='HRQ-CORRUPT1'").fetchone()[0])

    def test_post_response_crash_finishes_from_existing_signed_terminal_bundle(self):
        request = self.make_request("health.inspect", "HRQ-POSTRESP")
        claimed = self.spool / "processing" / "post-response-claim.json"
        claimed.parent.mkdir(parents=True)
        claimed.write_bytes(canonical(request))
        replay = broker.ReplayStore(self.state / "state.sqlite3")
        self.assertEqual(replay.reserve(request["requestId"], request["canonicalHash"], claimed.name), "new")
        replay.db.close()
        audit = broker.AuditLog(self.audit, self.state / "audit-head.json")
        result = {"status": "ok", "commit": "1" * 40}
        execution_audit = audit.append("execution.succeeded", {
            "requestId": request["requestId"], "canonicalHash": request["canonicalHash"],
            "verb": request["verb"], "resultHash": broker.sha256_bytes(canonical(result)),
            "writesPerformed": 0, "credentialEnteredRequesterProcess": False,
        })
        response = broker.Broker(self.config)._final_response(
            request, "succeeded", "ok", "fixed read-only verb completed", result, execution_audit,
        )
        basename = f"{request['requestId']}.{request['canonicalHash'][:12]}"
        broker.persist_signed(self.spool / "responses", basename, response, self.private_key)

        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["status"], "succeeded")
        self.assertFalse(claimed.exists())
        with sqlite3.connect(self.state / "state.sqlite3") as db:
            self.assertEqual(db.execute("select final_status from used_requests where request_id='HRQ-POSTRESP'").fetchone()[0], "succeeded")

    def test_multiple_same_batch_replays_each_receive_signed_refusal(self):
        request = self.make_request("health.inspect", "HRQ-MULTIREPLAY")
        for index in range(4):
            self.enqueue(request, f"duplicate-{index}")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), 4)
        self.assertEqual([item["status"] for item in responses].count("succeeded"), 1)
        self.assertEqual([item["code"] for item in responses].count("replay"), 3)
        for path in self.outputs("responses"):
            self.verify_signed(path)

    def test_oversized_request_refuses_before_reading_payload(self):
        directory = self.spool / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "HRQ-HUGE001.json"
        with path.open("wb") as handle:
            handle.truncate(512 * 1024 * 1024)
        self.run_once()
        response = self.read_json(self.outputs("responses")[0])
        self.assertEqual(response["code"], "request_too_large")

    def test_bounded_reader_refuses_fifo_without_blocking(self):
        path = self.temp / "request.fifo"
        os.mkfifo(path)
        with self.assertRaises(broker.Refusal) as raised:
            broker.read_bounded_regular(path)
        self.assertEqual(raised.exception.code, "invalid_spool_entry")

    def test_bounded_reader_refuses_symlink_to_regular_file(self):
        target = self.temp / "request.json"
        target.write_text("{}")
        path = self.temp / "request-link.json"
        path.symlink_to(target)
        with self.assertRaises(broker.Refusal) as raised:
            broker.read_bounded_regular(path)
        self.assertEqual(raised.exception.code, "invalid_spool_entry")

    def test_bounded_reader_uses_open_descriptor_after_path_replacement(self):
        path = self.temp / "request.json"
        path.write_bytes(b"original")
        real_open = broker.os.open
        replacement = self.temp / "replacement.json"

        def swap_after_open(open_path, flags, *args):
            fd = real_open(open_path, flags, *args)
            if Path(open_path) == path:
                replacement.write_bytes(b"replacement")
                os.replace(replacement, path)
            return fd

        broker.os.open = swap_after_open
        try:
            self.assertEqual(broker.read_bounded_regular(path), b"original")
        finally:
            broker.os.open = real_open
        self.assertEqual(path.read_bytes(), b"replacement")

    def test_process_file_claims_before_read_and_preserves_replacement(self):
        first = self.make_request("health.inspect", "HRQ-CLAIM01")
        second = self.make_request("host.inspect", "HRQ-CLAIM02")
        path = self.enqueue(first, "claim")
        second_bytes = canonical(second)
        host_broker = broker.Broker(self.config)
        real_open = broker.os.open

        def publish_replacement_after_open(open_path, flags, *args):
            fd = real_open(open_path, flags, *args)
            if Path(open_path).parent == host_broker.processing:
                path.write_bytes(second_bytes)
            return fd

        broker.os.open = publish_replacement_after_open
        try:
            host_broker.process_file(path)
        finally:
            broker.os.open = real_open

        self.assertEqual(path.read_bytes(), second_bytes)
        self.assertEqual(list(host_broker.processing.glob("*.json")), [])
        self.assertEqual(len(self.outputs("responses")), 1)
        host_broker.run_once()
        responses = [self.read_json(output) for output in self.outputs("responses")]
        self.assertEqual({response["requestId"] for response in responses}, {"HRQ-CLAIM01", "HRQ-CLAIM02"})
        self.assertFalse(path.exists())

    def test_canonical_hash_tamper_is_refused(self):
        request = self.make_request("health.inspect", "HRQ-TAMPER1")
        request["verb"] = "host.inspect"
        self.enqueue(request, "tamper")
        self.run_once()
        response = self.read_json(self.outputs("responses")[0])
        self.assertEqual(response["code"], "canonical_hash_mismatch")

    def test_write_specs_are_typed_inverse_complete_and_not_executable(self):
        described = subprocess.run([sys.executable, str(BROKER), "describe-verbs"], check=True, text=True, capture_output=True)
        payload = json.loads(described.stdout)
        self.assertNotEqual(set(payload["readVerbs"]), set())
        for name, spec in payload["writeVerbSpecifications"].items():
            with self.subTest(name=name):
                self.assertFalse(spec["enabled"])
                self.assertFalse(spec["authorization"]["agentApprovalAllowed"])
                self.assertEqual(spec["authorization"]["technicalKey"], "board_or_human")
                self.assertTrue(spec["authorization"]["singleUse"])
                self.assertTrue(spec["rollback"]["automatic"])
                self.assertTrue(spec["rollback"]["operation"])
                self.assertTrue(spec["preImage"])
                self.assertTrue(spec["postconditions"])
                self.assertGreater(spec["timeoutSeconds"], 0)
        for name in PHASE1_WRITE_VERBS:
            with self.subTest(verb=name):
                spec = payload["writeVerbSpecifications"][name]
                self.assertIn("O4", spec["authorization"]["blockedBy"])
                self.assertTrue(spec["plannedWrites"])
                self.assertTrue(spec["allowList"])
                self.assertIn("summary", spec)
        # Phase-1 writes render a signed dry-run preview, then the O4 gate
        # refuses: no executor runs, no host write happens. Every request here
        # carries a fully valid case_5248bcfa reason + CEO approval (and a fresh
        # drain receipt for the paperclip-restart pair), proving the O4 gate
        # still refuses execution even once authorization fully passes.
        for index, verb in enumerate(sorted(EXPECTED_WRITE_VERBS), 1):
            request = self.make_write_request(verb, f"HRQ-WRITE{index:02d}", arguments=WRITE_ARGUMENTS[verb])
            self.enqueue(request, f"write-{index}")
        self.run_once()
        responses = [self.verify_signed(path) for path in self.outputs("responses")]
        self.assertEqual({response["verb"] for response in responses}, EXPECTED_WRITE_VERBS)
        self.assertTrue(all(response["status"] == "refused" for response in responses))
        by_verb = {response["verb"]: response for response in responses}
        # PHASE1_WRITE_VERBS still hit the unconditional O4 gate. The
        # paperclip-restart pair is refused one step earlier -- authorization
        # passes, but verify_zero_running_runs_host_side has no host-side
        # wiring in this build, so a restart can never execute regardless of
        # how fresh or well-formed the caller's drain receipt is.
        # case_802335d7: coolify.app.deploy joins the O4 gate; coolify.app.restart
        # is refused one step earlier by the deploy-ledger precondition --
        # no ledger wiring exists, so a restart can never bypass row 1.
        for verb in PHASE1_WRITE_VERBS - {"coolify.app.restart"}:
            self.assertEqual(by_verb[verb]["code"], "write_execution_disabled")
        self.assertEqual(by_verb["coolify.app.restart"]["code"], "deploy_verification_unavailable")
        for verb in broker.PAPERCLIP_RESTART_VERBS:
            self.assertEqual(by_verb[verb]["code"], "drain_verification_unavailable")
        previews = [self.verify_signed(path) for path in self.outputs("previews")]
        self.assertEqual({item["verb"] for item in previews}, PHASE1_WRITE_VERBS)
        for item in previews:
            with self.subTest(preview=item["verb"]):
                self.assertEqual(item["effectClass"], "write_dry_run")
                self.assertTrue(item["executorRendered"])
                self.assertTrue(item["dryRun"])
                self.assertFalse(item["executed"])
                self.assertTrue(item["writes"])
                self.assertFalse(item["credentialsExposedToRequester"])

    def test_write_requests_with_bad_arguments_are_refused_before_preview(self):
        bad_shapes = [
            ("plugin.install", {}),
            ("plugin.install", {"package": "dispatch-src", "sha": "not-a-sha"}),
            ("plugin.install", {"package": "../escape", "sha": "1" * 40}),
            # CISO case_00a30085 change 2: dot-only/leading-dot names match the
            # shared name pattern but must still refuse (proven by direct
            # match: "." and ".." both fullmatch ^[A-Za-z0-9._-]{1,64}$).
            ("plugin.install", {"package": ".", "sha": "1" * 40}),
            ("plugin.install", {"package": "..", "sha": "1" * 40}),
            ("plugin.install", {"package": ".hidden", "sha": "1" * 40}),
            ("plugin.restart", {"package": ".."}),
            ("unit.install", {"name": ".", "sha": "2" * 40}),
            ("coolify.env.set", {"app": ".", "keys": "FOO"}),
            ("coolify.env.set", {"app": "paperclip", "keys": "FOO,.."}),
            ("coolify.env.set", {"app": "paperclip", "keys": ".hidden"}),
            ("script.run", {"script": "..", "sha": "4" * 40}),
            ("coolify.env.set", {"app": "paperclip", "keys": "FOO", "secret": "must-not-transit"}),
            ("script.run", {"script": "quota_brake", "sha": "1" * 40, "command": "rm -rf /"}),
            # case_802335d7: deploy takes only {app, sha}, restart only {app}.
            # Floating refs fail the 40-hex pattern; endpoint URLs, tokens,
            # argv and secret values have no declared field and refuse here,
            # before any catalogue lookup.
            ("coolify.app.deploy", {}),
            ("coolify.app.deploy", {"app": "two-web-staging", "sha": "latest"}),
            ("coolify.app.deploy", {"app": "two-web-staging", "sha": "a" * 39}),
            ("coolify.app.deploy", {"app": ".", "sha": "a" * 40}),
            ("coolify.app.deploy", {"app": "two-web-staging", "sha": "a" * 40, "endpointUrl": "https://coolify.example"}),
            ("coolify.app.deploy", {"app": "two-web-staging", "sha": "a" * 40, "token": "must-not-transit"}),
            ("coolify.app.deploy", {"app": "two-web-staging", "sha": "a" * 40, "argv": "--force"}),
            ("coolify.app.restart", {}),
            ("coolify.app.restart", {"app": ".."}),
            ("coolify.app.restart", {"app": "two-web-staging", "sha": "a" * 40}),
        ]
        for index, (verb, arguments) in enumerate(bad_shapes, 1):
            self.enqueue(self.make_write_request(verb, f"HRQ-BADW{index:02d}", arguments=arguments), f"bad-{index}")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual({response["code"] for response in responses}, {"invalid_arguments"})
        self.assertEqual(self.outputs("previews"), [])

    def test_owner_reserved_verbs_are_refused_unconditionally(self):
        # case_5248bcfa: owner-reserved verbs are refused before any other check --
        # no reason, approval, or config is even consulted -- so a bare request
        # (no reason/approval attached) is enough to prove the refusal fires
        # first.
        for index, verb in enumerate(sorted(broker.OWNER_RESERVED_VERBS), 1):
            self.enqueue(self.make_request(verb, f"HRQ-RESV{index:02d}"), f"reserved-{index}")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), len(broker.OWNER_RESERVED_VERBS))
        self.assertTrue(all(response["code"] == "owner_reserved" for response in responses))
        self.assertEqual(self.outputs("previews"), [])

    def test_owner_reserved_namespace_prefix_catches_verbs_outside_the_named_set(self):
        self.enqueue(self.make_request("credential.export", "HRQ-RESVNS01"), "reserved-ns")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(responses[0]["code"], "owner_reserved")

    def test_routine_write_without_ceo_configured_is_refused(self):
        config = dict(self.config)
        del config["ceoAgentId"]
        alt_config = self.temp / "no-ceo-config.json"
        alt_config.write_text(json.dumps(config))
        request = self.make_write_request("plugin.restart", "HRQ-NOCEO01", arguments=WRITE_ARGUMENTS["plugin.restart"])
        self.enqueue(request, "no-ceo")
        self.private_key.chmod(0o600)
        completed = subprocess.run(
            [sys.executable, str(BROKER), "run-once", "--config", str(alt_config)],
            env=os.environ, text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(responses[0]["code"], "approval_unconfigured")

    def test_routine_write_self_or_wrong_approver_is_refused(self):
        self_approval = self.valid_approval(approver=AGENT)
        wrong_approval = self.valid_approval(approver=OTHER_AGENT)
        self.enqueue(self.make_write_request(
            "unit.install", "HRQ-SELFAPR01", arguments=WRITE_ARGUMENTS["unit.install"], approval=self_approval,
        ), "self-approval")
        self.enqueue(self.make_write_request(
            "unit.install", "HRQ-WRONGAPR01", arguments=WRITE_ARGUMENTS["unit.install"], approval=wrong_approval,
        ), "wrong-approval")
        self.run_once()
        by_id = {response["requestId"]: response for response in (self.read_json(p) for p in self.outputs("responses"))}
        self.assertEqual(by_id["HRQ-SELFAPR01"]["code"], "approval_invalid")
        self.assertEqual(by_id["HRQ-WRONGAPR01"]["code"], "approval_invalid")

    def test_routine_write_stale_approval_is_refused(self):
        stale = self.valid_approval(age=broker.APPROVAL_MAX_AGE_SECONDS + 60)
        self.enqueue(self.make_write_request(
            "cliproxy.apply", "HRQ-STALEAPR01", arguments=WRITE_ARGUMENTS["cliproxy.apply"], approval=stale,
        ), "stale-approval")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(responses[0]["code"], "approval_stale")

    def test_paperclip_restart_without_fresh_drain_receipt_is_refused(self):
        missing = self.make_request(
            "paperclip.restart", "HRQ-NODRAIN01",
            reason="reviewed and approved for this card", approval=self.valid_approval(),
        )
        stale = self.make_write_request(
            "paperclip.restart", "HRQ-STALEDRAIN01",
            drain_receipt=self.valid_drain_receipt(age=broker.DRAIN_RECEIPT_MAX_AGE_SECONDS + 60),
        )
        dirty = self.make_write_request(
            "paperclip.restart", "HRQ-DIRTYDRAIN01",
            drain_receipt=self.valid_drain_receipt(running_runs=3),
        )
        self.enqueue(missing, "no-drain")
        self.enqueue(stale, "stale-drain")
        self.enqueue(dirty, "dirty-drain")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        # A missing drainReceipt fails schema validation before requestId
        # survives into the response (see the placeholder assigned at the top
        # of _process_file), so it is identified by code alone.
        codes = [response["code"] for response in responses]
        self.assertEqual(codes.count("invalid_shape"), 1)
        by_id = {response["requestId"]: response for response in responses if response["requestId"] in {"HRQ-STALEDRAIN01", "HRQ-DIRTYDRAIN01"}}
        self.assertEqual(by_id["HRQ-STALEDRAIN01"]["code"], "drain_required")
        self.assertEqual(by_id["HRQ-DIRTYDRAIN01"]["code"], "drain_required")

    def test_paperclip_restart_with_a_fresh_zero_run_drain_receipt_still_cannot_execute(self):
        # Proves the acceptance criterion directly: even a perfectly-formed,
        # fresh, zero-running-runs attestation from the caller is not enough on
        # its own -- verify_zero_running_runs_host_side has no host-side wiring
        # in this build, so the broker refuses rather than trusting the caller.
        self.enqueue(self.make_write_request("paperclip.restart", "HRQ-GOODDRAIN01"), "good-drain")
        self.run_once()
        responses = [self.read_json(path) for path in self.outputs("responses")]
        self.assertEqual(responses[0]["code"], "drain_verification_unavailable")

    def test_audit_entries_embed_requester_reason_and_approver(self):
        request = self.make_write_request("plugin.restart", "HRQ-AUDIT01", arguments=WRITE_ARGUMENTS["plugin.restart"])
        self.enqueue(request, "audit")
        self.run_once()
        rows = [json.loads(line) for line in self.audit.read_text().splitlines()]
        accepted = next(row for row in rows if row["event"] == "request.accepted")
        self.assertEqual(accepted["details"]["requester"], request["requester"])
        self.assertEqual(accepted["details"]["requestReason"], request["reason"])
        self.assertEqual(accepted["details"]["approver"], request["approval"]["approver"])
        refused = next(row for row in rows if row["event"] == "execution.refused")
        self.assertEqual(refused["details"]["requester"], request["requester"])
        self.assertEqual(refused["details"]["requestReason"], request["reason"])
        self.assertEqual(refused["details"]["approver"], request["approval"]["approver"])
        self.assertEqual(refused["details"]["reason"], "write_execution_disabled")

    def test_env_manifest_refuses_cross_app_keys_and_duplicate_keys_before_preview(self):
        cases = [
            ({"app": "other-app", "keys": "FOO"}, "env_app_not_allowlisted"),
            ({"app": "paperclip", "keys": "DATABASE_URL"}, "env_key_not_allowlisted"),
            ({"app": "paperclip", "keys": "FOO,FOO"}, "env_key_not_allowlisted"),
        ]
        for index, (args, _) in enumerate(cases):
            self.enqueue(self.make_write_request("coolify.env.set", f"HRQ-ENVDENY{index}", arguments=args))
        self.run_once()
        responses = [self.verify_signed(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), len(cases))
        self.assertEqual({r["code"] for r in responses}, {code for _, code in cases})
        self.assertEqual(self.outputs("previews"), [])

    def test_manifest_required_and_mapping_must_be_pinned_server_side(self):
        request = self.make_request("coolify.env.set", arguments=WRITE_ARGUMENTS["coolify.env.set"])
        for manifest in [None, {}, {"version": True, "revision": "a" * 40}, {"version": 1, "revision": "latest"}]:
            with self.subTest(manifest=manifest), self.assertRaises(broker.Refusal) as raised:
                broker.validate_write_manifest(request, manifest)
            self.assertEqual(raised.exception.code, "write_manifest_required")
        manifest = self.config["writeAllowList"]
        for mapping in [None, {}, {"name": "test-env", "sha": "latest"},
                        {"name": "test-env", "sha": "b" * 40, "value": "must-not-transit"}]:
            with self.subTest(mapping=mapping), self.assertRaises(broker.Refusal) as raised:
                manifest["apps"]["paperclip"]["mapping"] = mapping
                broker.validate_write_manifest(request, manifest)
            self.assertEqual(raised.exception.code, "env_mapping_required")

    def test_script_manifest_refuses_missing_scope_inverse_and_composition_evidence(self):
        import copy
        request = self.make_write_request("script.run", "HRQ-TEST001", arguments=WRITE_ARGUMENTS["script.run"])
        baseline = self.config["writeAllowList"]
        cases = [
            ("sha", "5" * 40, "script_not_allowlisted"),
            ("writeScope", None, "script_scope_required"),
            ("writeScope", [], "script_scope_required"),
            ("writeScope", ["relative/path"], "script_scope_required"),
            ("writeScope", ["/"], "script_scope_required"),
            ("writeScope", ["/var/lib/../escape"], "script_scope_required"),
            ("writeScope", ["/var//state"], "script_scope_required"),
            ("inverse", None, "script_inverse_required"),
            ("inverse", {"script": "restore", "sha": "5" * 40, "testEvidenceSha256": "c" * 64}, "script_inverse_required"),
            ("inverse", {"script": "restore", "sha": "4" * 40, "testEvidenceSha256": ""}, "script_inverse_required"),
            ("compositionReviewSha256", None, "script_composition_review_required"),
        ]
        for field, value, code in cases:
            manifest = copy.deepcopy(baseline)
            manifest["scripts"]["quota_brake"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(broker.Refusal) as raised:
                broker.validate_write_manifest(request, manifest)
            self.assertEqual(raised.exception.code, code)
        # Exercise the production dispatch seam, not just the helper.
        del self.config["writeAllowList"]["scripts"]["quota_brake"]["writeScope"]
        self.config_path.write_text(json.dumps(self.config))
        self.enqueue(request)
        self.run_once()
        self.assertEqual(self.verify_signed(self.outputs("responses")[0])["code"], "script_scope_required")
        self.assertEqual(self.outputs("previews"), [])

    def test_valid_manifests_pin_previews_but_never_enable_writes(self):
        self.config["writesEnabled"] = True
        self.config_path.write_text(json.dumps(self.config))
        for index, verb in enumerate(["coolify.env.set", "script.run"]):
            self.enqueue(self.make_write_request(verb, f"HRQ-MANIFEST{index}", arguments=WRITE_ARGUMENTS[verb]))
        self.run_once()
        previews = [self.verify_signed(path) for path in self.outputs("previews")]
        self.assertEqual(len(previews), 2)
        self.assertTrue(all(p["allowListRevision"] == "a" * 40 for p in previews))
        self.assertTrue(all(p["executed"] is False for p in previews))
        for path in self.outputs("responses"):
            self.assertEqual(self.verify_signed(path)["code"], "write_execution_disabled")
        for spec in broker.WRITE_VERB_SPECS.values():
            self.assertIs(spec["enabled"], False)

    def test_missing_host_manifest_fails_closed_in_dispatch(self):
        del self.config["writeAllowList"]
        self.config_path.write_text(json.dumps(self.config))
        for index, verb in enumerate(["coolify.env.set", "script.run"]):
            self.enqueue(self.make_write_request(verb, f"HRQ-NOMANIFEST{index}", arguments=WRITE_ARGUMENTS[verb]))
        self.run_once()
        self.assertEqual(len(self.outputs("responses")), 2)
        for path in self.outputs("responses"):
            self.assertEqual(self.verify_signed(path)["code"], "write_manifest_required")
        self.assertEqual(self.outputs("previews"), [])

    def test_staging_catalogue_valid_control_previews_but_never_enables_writes(self):
        # case_802335d7 valid control: even with writesEnabled True and a fully
        # valid CEO approval, the disabled specs never execute. Deploy ends
        # at the O4 gate; restart ends one step earlier at the deploy-ledger
        # precondition, so it can never bypass row 1.
        self.config["writesEnabled"] = True
        self.config_path.write_text(json.dumps(self.config))
        self.enqueue(self.make_write_request("coolify.app.deploy", "HRQ-STAGEDEPLOY01", arguments=WRITE_ARGUMENTS["coolify.app.deploy"]))
        self.enqueue(self.make_write_request("coolify.app.restart", "HRQ-STAGERESTART01", arguments=WRITE_ARGUMENTS["coolify.app.restart"]))
        self.run_once()
        previews = [self.verify_signed(path) for path in self.outputs("previews")]
        self.assertEqual(len(previews), 2)
        self.assertTrue(all(p["allowListRevision"] == "a" * 40 for p in previews))
        self.assertTrue(all(p["executed"] is False and p["dryRun"] is True for p in previews))
        self.assertTrue(all(p["effectClass"] == "write_dry_run" for p in previews))
        by_verb = {p["verb"]: p for p in previews}
        self.assertEqual(by_verb["coolify.app.deploy"]["arguments"], WRITE_ARGUMENTS["coolify.app.deploy"])
        self.assertEqual(by_verb["coolify.app.restart"]["arguments"], WRITE_ARGUMENTS["coolify.app.restart"])
        by_response = {r["verb"]: r for r in (self.verify_signed(p) for p in self.outputs("responses"))}
        self.assertEqual(by_response["coolify.app.deploy"]["code"], "write_execution_disabled")
        self.assertEqual(by_response["coolify.app.restart"]["code"], "deploy_verification_unavailable")
        for verb in ("coolify.app.deploy", "coolify.app.restart"):
            spec = broker.WRITE_VERB_SPECS[verb]
            self.assertIs(spec["enabled"], False)
            self.assertEqual(spec["tier"], "routine-write")
            self.assertFalse(spec["authorization"]["agentApprovalAllowed"])
            self.assertTrue(spec["rollback"]["automatic"])
            self.assertTrue(spec["preImage"] and spec["postconditions"])

    def test_staging_deploy_refuses_unknown_prod_and_wrong_release(self):
        cases = [
            ({"app": "no-such-app", "sha": "a" * 40}, "app_not_allowlisted"),
            ({"app": "two-web-prod", "sha": "a" * 40}, "app_not_allowlisted"),
            # Valid 40-hex but pinned to the other app: cross-app binding.
            ({"app": "two-web-staging", "sha": "b" * 40}, "release_not_allowlisted"),
            # Valid 40-hex but pinned nowhere: unpinned release.
            ({"app": "two-web-staging", "sha": "c" * 40}, "release_not_allowlisted"),
            ({"app": "two-bot-staging", "sha": "a" * 40}, "release_not_allowlisted"),
        ]
        for index, (args, _) in enumerate(cases):
            self.enqueue(self.make_write_request("coolify.app.deploy", f"HRQ-DEPLOYREF{index}", arguments=args))
        self.run_once()
        responses = [self.verify_signed(path) for path in self.outputs("responses")]
        self.assertEqual(len(responses), len(cases))
        self.assertEqual({r["code"] for r in responses}, {code for _, code in cases})
        self.assertEqual(self.outputs("previews"), [])

    def test_staging_restart_refuses_unknown_and_prod_apps(self):
        cases = [
            ({"app": "no-such-app"}, "app_not_allowlisted"),
            ({"app": "two-bot-prod"}, "app_not_allowlisted"),
        ]
        for index, (args, _) in enumerate(cases):
            self.enqueue(self.make_write_request("coolify.app.restart", f"HRQ-RESTARTREF{index}", arguments=args))
        self.run_once()
        responses = [self.verify_signed(path) for path in self.outputs("responses")]
        self.assertEqual({r["code"] for r in responses}, {code for _, code in cases})
        self.assertEqual(self.outputs("previews"), [])

    def test_staging_catalogue_malformed_entries_fail_closed(self):
        import copy
        deploy = self.make_write_request("coolify.app.deploy", "HRQ-CAT001", arguments=WRITE_ARGUMENTS["coolify.app.deploy"])
        restart = self.make_write_request("coolify.app.restart", "HRQ-CAT002", arguments=WRITE_ARGUMENTS["coolify.app.restart"])
        # Missing envelope or unpinned revision refuses before any lookup.
        for manifest in [None, {}, {"version": 1, "revision": "latest", "stagingApps": {}}]:
            with self.subTest(manifest=manifest):
                for request in (deploy, restart):
                    with self.assertRaises(broker.Refusal) as raised:
                        broker.validate_write_manifest(request, manifest)
                    self.assertEqual(raised.exception.code, "write_manifest_required")
        # A production entry refuses as a prod target, never as an allow-list.
        prod = copy.deepcopy(self.config["writeAllowList"])
        prod["stagingApps"]["two-web-prod"] = dict(STAGING_APPS["two-web-staging"], environment="production")
        prod_deploy = self.make_write_request("coolify.app.deploy", "HRQ-CATPROD01",
                                              arguments={"app": "two-web-prod", "sha": "a" * 40})
        prod_restart = self.make_write_request("coolify.app.restart", "HRQ-CATPROD02",
                                               arguments={"app": "two-web-prod"})
        for request in (prod_deploy, prod_restart):
            with self.subTest(request=request["requestId"]), self.assertRaises(broker.Refusal) as raised:
                broker.validate_write_manifest(request, prod)
            self.assertEqual(raised.exception.code, "prod_target_refused")
        # Malformed entries refuse as catalogue-invalid: bad UUID, empty or
        # duplicated release set, non-absolute or URL deploy path.
        malformed = [
            ("coolifyAppUuid", "not-a-uuid"),
            ("allowedReleases", []),
            ("allowedReleases", ["latest"]),
            ("allowedReleases", ["a" * 40, "a" * 40]),
            ("deployPath", "relative/path"),
            ("deployPath", "/"),
            ("deployPath", "https://coolify.example/apps/1"),
            ("deployPath", "/var/lib/../escape"),
        ]
        for field, value in malformed:
            manifest = copy.deepcopy(self.config["writeAllowList"])
            manifest["stagingApps"]["two-web-staging"][field] = value
            with self.subTest(field=field, value=value):
                for request in (deploy, restart):
                    with self.assertRaises(broker.Refusal) as raised:
                        broker.validate_write_manifest(request, manifest)
                    self.assertEqual(raised.exception.code, "staging_catalogue_invalid")

    def test_staging_approval_missing_stale_and_replayed_are_refused(self):
        # Missing approval fails request shape before any catalogue lookup.
        bare = self.make_request("coolify.app.deploy", "HRQ-DEPLOYNOAPR",
                                 arguments=WRITE_ARGUMENTS["coolify.app.deploy"],
                                 reason="reviewed and approved for this card")
        self.enqueue(bare, "no-approval")
        # Stale CEO approval refuses even with a valid catalogue entry.
        stale = self.make_write_request("coolify.app.deploy", "HRQ-DEPLOYSTALE",
                                        arguments=WRITE_ARGUMENTS["coolify.app.deploy"],
                                        approval=self.valid_approval(age=broker.APPROVAL_MAX_AGE_SECONDS + 60))
        self.enqueue(stale, "stale-approval")
        self.run_once()
        by_id = {r["requestId"]: r for r in (self.read_json(p) for p in self.outputs("responses"))}
        self.assertEqual(by_id["HRQ-DEPLOYSTALE"]["code"], "approval_stale")
        stale_codes = {r["code"] for r in (self.read_json(p) for p in self.outputs("responses"))}
        self.assertIn("approval_stale", stale_codes)
        self.assertIn("invalid_shape", stale_codes)
        # The shape-failed request renders no preview; the stale-approval
        # request renders its dry-run preview before authorization refuses.
        previews = [self.read_json(p) for p in self.outputs("previews")]
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["verb"], "coolify.app.deploy")
        # Replay: the same deploy requestId consumed once refuses on resend.
        for leftover in list((self.spool / "requests").glob("*.json")):
            leftover.unlink()
        for leftover in list((self.spool / "responses").glob("*.bundle")):
            shutil.rmtree(leftover)
        first = self.make_write_request("coolify.app.deploy", "HRQ-DEPLOYREPLAY01", arguments=WRITE_ARGUMENTS["coolify.app.deploy"])
        self.enqueue(first, "replay-first")
        self.run_once()
        second = self.make_write_request("coolify.app.deploy", "HRQ-DEPLOYREPLAY01", arguments=WRITE_ARGUMENTS["coolify.app.deploy"])
        self.enqueue(second, "replay-second")
        self.run_once()
        codes = [self.read_json(p)["code"] for p in self.outputs("responses")]
        self.assertIn("replay", codes)

    def test_staging_restart_binds_a_successful_deploy_only(self):
        # Synthetic ledger (tests only): succeeded binds, failed or missing
        # records refuse so a restart can never bypass a failed deploy.
        restart = self.make_write_request("coolify.app.restart", "HRQ-RESTARTLEDGER01", arguments=WRITE_ARGUMENTS["coolify.app.restart"])
        good_ledger = {"two-web-staging": {"releaseSha": "a" * 40, "status": "succeeded"}}
        broker.validate_coolify_restart_precondition(restart, self.config, good_ledger)
        for ledger, label in [
            ({"two-web-staging": {"releaseSha": "a" * 40, "status": "failed"}}, "failed"),
            ({}, "missing"),
            ({"two-web-staging": {"releaseSha": "latest", "status": "succeeded"}}, "unpinned"),
            ("not-a-ledger", "malformed"),
        ]:
            with self.subTest(ledger=label), self.assertRaises(broker.Refusal) as raised:
                broker.validate_coolify_restart_precondition(restart, self.config, ledger)
            self.assertEqual(raised.exception.code, "deploy_required")
        # This build wires no ledger: dispatch refuses before the O4 gate.
        with self.assertRaises(broker.Refusal) as raised:
            broker.validate_coolify_restart_precondition(restart, self.config)
        self.assertEqual(raised.exception.code, "deploy_verification_unavailable")
        self.enqueue(restart, "restart-no-ledger")
        self.run_once()
        self.assertEqual(self.verify_signed(self.outputs("responses")[0])["code"],
                         "deploy_verification_unavailable")

    def test_staging_specs_carry_no_spend_side_effect(self):
        # No spend verb, flag, or amount exists anywhere on the staging-app
        # path: specs name no spend write, and the owner-reserved spend
        # namespace still refuses unconditionally.
        for verb in ("coolify.app.deploy", "coolify.app.restart"):
            spec = broker.WRITE_VERB_SPECS[verb]
            # The executable surface (writes + rollback) names no spend,
            # cost, URL, or token effect; the postconditions record the
            # absence as a bounded guarantee instead.
            executable = json.dumps({"writes": spec["plannedWrites"], "rollback": spec["rollback"]}).lower()
            self.assertNotIn("spend", executable)
            self.assertNotIn("cost", executable)
            self.assertNotIn("http", executable)
            self.assertNotIn("token", executable)
            self.assertTrue(any("no spend" in post.lower() for post in spec["postconditions"]))
        self.assertIn("spend.approve", broker.OWNER_RESERVED_VERBS)
        self.assertTrue(all(namespace in broker.OWNER_RESERVED_VERB_NAMESPACES for namespace in ("spend.",)))
        self.enqueue(self.make_request("spend.approve", "HRQ-SPEND01"), "spend")
        self.run_once()
        self.assertEqual(self.read_json(self.outputs("responses")[0])["code"], "owner_reserved")
        self.assertEqual(self.outputs("previews"), [])

    def test_writes_enabled_defaults_false_and_must_be_boolean(self):
        config = dict(self.config)
        del config["writesEnabled"]
        minimal = self.temp / "minimal-config.json"
        minimal.write_text(json.dumps(config))
        loaded = broker.load_config(minimal)
        self.assertFalse(loaded["writesEnabled"])
        config["writesEnabled"] = "yes"
        minimal.write_text(json.dumps(config))
        with self.assertRaises(broker.Refusal) as raised:
            broker.load_config(minimal)
        self.assertEqual(raised.exception.code, "unsafe_config")

    def test_concurrent_run_once_processes_serialize_audit_and_process_each_request_once(self):
        total = 80
        for index in range(total):
            verb = tuple(EXPECTED_READ_EXECUTORS)[index % len(EXPECTED_READ_EXECUTORS)]
            self.enqueue(self.make_request(verb, f"HRQ-CONCURRENT{index:03d}"), str(index))

        processes = [
            subprocess.Popen(
                [sys.executable, str(BROKER), "run-once", "--config", str(self.config_path)],
                env=os.environ,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        results = [process.communicate(timeout=60) for process in processes]
        processed_counts = []
        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stderr)
            processed_counts.append(json.loads(stdout)["processed"])
        self.assertEqual(sum(processed_counts), total)

        responses = [self.read_json(path) for path in self.outputs("responses")]
        previews = [self.read_json(path) for path in self.outputs("previews")]
        self.assertEqual(len(responses), total)
        self.assertEqual(len(previews), total)
        self.assertEqual({response["requestId"] for response in responses}, {f"HRQ-CONCURRENT{index:03d}" for index in range(total)})
        self.assertTrue(all(response["status"] == "succeeded" for response in responses))
        restarted = broker.Broker(self.config)
        with restarted._exclusive_run():
            audit, replay = restarted._require_state()
            self.assertEqual(audit.sequence, total * 3)
            self.assertEqual(replay.db.execute("select count(*) from used_requests").fetchone()[0], total)

    def test_audit_chain_detects_mutation(self):
        self.enqueue(self.make_request("health.inspect", "HRQ-AUDIT01"), "audit")
        self.run_once()
        log = broker.AuditLog(self.audit, self.state / "audit-head.json")
        self.assertGreaterEqual(log.sequence, 3)
        lines = self.audit.read_text().splitlines()
        row = json.loads(lines[0])
        row["details"]["verb"] = "tampered"
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.audit.write_text("\n".join(lines) + "\n")
        with self.assertRaises(broker.Refusal) as raised:
            broker.AuditLog(self.audit, self.state / "audit-head.json")
        self.assertEqual(raised.exception.code, "audit_corrupt")

    def test_audit_chain_head_detects_complete_tail_truncation(self):
        self.enqueue(self.make_request("health.inspect", "HRQ-AUDIT02"), "audit-tail")
        self.run_once()
        checkpoint = self.state / "audit-head.json"
        chain_head = self.read_json(checkpoint)
        lines = self.audit.read_bytes().splitlines(keepends=True)
        self.assertEqual(chain_head["sequence"], len(lines))
        self.assertGreater(chain_head["sequence"], 1)
        self.audit.write_bytes(b"".join(lines[:-1]))

        with self.assertRaises(broker.Refusal) as raised:
            broker.Broker(self.config).run_once()
        self.assertEqual(raised.exception.code, "audit_corrupt")
        self.assertEqual(self.read_json(checkpoint), chain_head)

    def test_audit_pending_checkpoint_recovers_before_or_after_durable_append(self):
        checkpoint = self.state / "audit-head.json"
        log = broker.AuditLog(self.audit, checkpoint)
        record = {
            "version": 1,
            "sequence": 1,
            "at": broker.iso(broker.now_utc()),
            "event": "test.first",
            "previousHash": "0" * 64,
            "details": {},
        }
        record["entryHash"] = broker.sha256_bytes(broker.canonical_bytes(record))
        log._write_checkpoint(0, "0" * 64, record)

        restarted = broker.AuditLog(self.audit, checkpoint)
        self.assertEqual((restarted.sequence, restarted.previous_hash), (1, record["entryHash"]))
        self.assertEqual(self.audit.read_bytes(), broker.canonical_bytes(record))
        self.assertEqual(self.read_json(checkpoint)["state"], "committed")

        log._write_checkpoint(1, record["entryHash"], {
            **record,
            "sequence": 2,
            "previousHash": record["entryHash"],
            "event": "test.second",
        })
        pending = self.read_json(checkpoint)["record"]
        pending["entryHash"] = broker.sha256_bytes(broker.canonical_bytes({key: value for key, value in pending.items() if key != "entryHash"}))
        log._write_checkpoint(1, record["entryHash"], pending)
        with self.audit.open("ab") as handle:
            handle.write(broker.canonical_bytes(pending))
            handle.flush()
            os.fsync(handle.fileno())
        restarted = broker.AuditLog(self.audit, checkpoint)
        self.assertEqual((restarted.sequence, restarted.previous_hash), (2, pending["entryHash"]))

    def test_missing_or_reset_audit_checkpoint_refuses_nonempty_chain(self):
        checkpoint = self.state / "audit-head.json"
        log = broker.AuditLog(self.audit, checkpoint)
        first = log.append("test.first", {})
        checkpoint.unlink()
        with self.assertRaises(broker.Refusal) as raised:
            broker.AuditLog(self.audit, checkpoint)
        self.assertEqual(raised.exception.code, "audit_corrupt")

        broker.atomic_write(
            checkpoint,
            broker.canonical_bytes({"version": 1, "state": "committed", "sequence": 0, "entryHash": "0" * 64}),
            mode=0o600,
        )
        with self.assertRaises(broker.Refusal) as raised:
            broker.AuditLog(self.audit, checkpoint)
        self.assertEqual(raised.exception.code, "audit_corrupt")
        self.assertEqual(first["sequence"], 1)

    def test_checkpoint_publication_failure_does_not_reuse_sequence(self):
        checkpoint = self.state / "audit-head.json"
        log = broker.AuditLog(self.audit, checkpoint)
        real_write = log._write_checkpoint
        calls = 0

        def fail_commit(sequence, entry_hash, pending_record=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected committed-checkpoint failure")
            return real_write(sequence, entry_hash, pending_record)

        log._write_checkpoint = fail_commit
        with self.assertRaises(OSError):
            log.append("test.first", {})
        second = log.append("test.second", {})
        rows = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([row["sequence"] for row in rows], [1, 2])
        self.assertEqual(second["previousHash"], rows[0]["entryHash"])
        broker.AuditLog(self.audit, checkpoint)

    def test_malformed_audit_checkpoint_refuses_startup(self):
        checkpoint = self.state / "audit-head.json"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text('{"version":1,"sequence":true,"entryHash":"' + "0" * 64 + '"}\n')

        with self.assertRaises(broker.Refusal) as raised:
            broker.AuditLog(self.audit, checkpoint)
        self.assertEqual(raised.exception.code, "audit_corrupt")

    def test_signature_requires_independent_public_key_pin(self):
        self.enqueue(self.make_request("health.inspect", "HRQ-SIGNED01"), "signed")
        self.run_once()
        path = self.outputs("responses")[0]
        bad = subprocess.run([
            sys.executable, str(BROKER), "verify", "--public-key", str(self.public_key),
            "--expected-key-sha256", "0" * 64, "--response", str(path), "--signature", str(path.with_suffix('.sig')),
        ], text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("independently pinned", bad.stderr)

    def test_verify_uses_captured_response_bytes_after_path_replacement(self):
        payload = {"signed": True}
        path, signature = broker.persist_signed(self.spool / "responses", "replacement", payload, self.private_key)
        real_read = broker.read_bounded_regular
        replacement = self.temp / "attacker.json"
        replacement.write_bytes(canonical({"attackerControlled": True}))

        def swap_after_response_read(read_path, maximum=65536, code="invalid_spool_entry"):
            captured = real_read(read_path, maximum, code)
            if Path(read_path) == path:
                os.replace(replacement, path)
            return captured

        broker.read_bounded_regular = swap_after_response_read
        try:
            args = type("Args", (), {
                "public_key": str(self.public_key), "expected_key_sha256": self.public_key_hash,
                "response": str(path), "signature": str(signature),
            })()
            output = tempfile.TemporaryFile(mode="w+")
            old_stdout = sys.stdout
            sys.stdout = output
            try:
                self.assertEqual(broker.command_verify(args), 0)
            finally:
                sys.stdout = old_stdout
            output.seek(0)
            self.assertEqual(json.load(output), payload)
            output.close()
        finally:
            broker.read_bounded_regular = real_read
        self.assertEqual(self.read_json(path), {"attackerControlled": True})

    def test_verify_refuses_fifo_and_symlink_inputs_without_blocking(self):
        fifo = self.temp / "response.fifo"
        os.mkfifo(fifo)
        symlink = self.temp / "public-link.pem"
        symlink.symlink_to(self.public_key)
        signature = self.temp / "response.sig"
        signature.write_bytes(b"not-a-signature")
        for public_key, response, expected in (
            (symlink, self.temp / "missing.json", "invalid_public_key"),
            (self.public_key, fifo, "invalid_response"),
        ):
            with self.subTest(expected=expected):
                args = type("Args", (), {
                    "public_key": str(public_key), "expected_key_sha256": self.public_key_hash,
                    "response": str(response), "signature": str(signature),
                })()
                with self.assertRaises(broker.Refusal) as raised:
                    broker.command_verify(args)
                self.assertEqual(raised.exception.code, expected)

    def test_signed_publication_keeps_staging_private_until_rename(self):
        directory = self.spool / "responses"
        directory.mkdir(parents=True)
        directory.chmod(0o2750)
        observed = {}
        real_publish = broker.publish_directory_noreplace

        def inspect_before_publish(staging, bundle, published_mode):
            observed["before"] = staging.stat().st_mode & 0o777
            observed["stagingParentMode"] = staging.parent.stat().st_mode & 0o777
            observed["separateParent"] = staging.parent != bundle.parent
            real_rename = broker.rename_noreplace

            def inspect_at_publication(source, target):
                observed["atPublication"] = Path(source).stat().st_mode & 0o777
                real_rename(source, target)

            broker.rename_noreplace = inspect_at_publication
            try:
                real_publish(staging, bundle, published_mode)
            finally:
                broker.rename_noreplace = real_rename
            observed["after"] = bundle.stat().st_mode & 0o777

        broker.publish_directory_noreplace = inspect_before_publish
        try:
            path, signature = broker.persist_signed(directory, "private-staging", {"payload": True}, self.private_key)
        finally:
            broker.publish_directory_noreplace = real_publish
        self.assertEqual(observed, {
            "before": 0o700, "stagingParentMode": 0o700, "separateParent": True,
            "atPublication": 0o750, "after": 0o750,
        })
        self.assertTrue(path.exists())
        self.assertTrue(signature.exists())

    def test_signed_publication_never_exposes_json_without_signature(self):
        directory = self.spool / "responses"
        real_sign = broker.sign_file

        def fail_signing(private_key, path, output):
            raise broker.Refusal("signing_failed", "injected")

        broker.sign_file = fail_signing
        try:
            with self.assertRaises(broker.Refusal):
                broker.persist_signed(directory, "sign-failure", {"payload": True}, self.private_key)
        finally:
            broker.sign_file = real_sign
        self.assertFalse((directory / "sign-failure.bundle").exists())
        self.assertEqual(list(directory.glob(".sign-failure.*")), [])

    def test_health_accepts_supported_optional_fields_and_rejects_unknowns(self):
        supported = {"status": "ok", "commit": "1" * 40, "version": "1.2.3", "uptime": 123.5}
        self.assertEqual(broker.validate_result("health.inspect", supported), supported)
        with self.assertRaises(broker.Refusal) as raised:
            broker.validate_result("health.inspect", {**supported, "secret": "must-not-escape"})
        self.assertEqual(raised.exception.code, "invalid_health")
        for field, value in (("version", 42), ("uptime", "123"), ("uptime", True), ("uptime", -1)):
            with self.subTest(field=field, value=value):
                malformed = {**supported, field: value}
                with self.assertRaises(broker.Refusal) as raised:
                    broker.validate_result("health.inspect", malformed)
                self.assertEqual(raised.exception.code, "invalid_health")

    def test_health_executor_preserves_malformed_known_optional_for_validation(self):
        (self.facts / "health.json").write_text(json.dumps({
            "status": "ok", "commit": "1" * 40, "version": 42, "secret": "must-not-escape",
        }))
        result = broker.execute_health(self.config)
        self.assertEqual(result, {"status": "ok", "commit": "1" * 40, "version": 42})
        with self.assertRaises(broker.Refusal) as raised:
            broker.validate_result("health.inspect", result)
        self.assertEqual(raised.exception.code, "invalid_health")

    def test_incomplete_executor_facts_are_signed_refusals_not_success(self):
        invalid = {
            "health": {},
            "service": {},
            "image": {"Id": None, "Digest": None, "RepoTags": None, "Created": None, "Size": None},
        }
        for index, (fact, payload) in enumerate(invalid.items(), 1):
            with self.subTest(fact=fact):
                (self.facts / f"{fact}.json").write_text(json.dumps(payload))
                verb = {"health": "health.inspect", "service": "service.inspect", "image": "image.inspect"}[fact]
                self.enqueue(self.make_request(verb, f"HRQ-INVALID{index:02d}"), fact)
        self.run_once()
        responses = [self.verify_signed(path) for path in self.outputs("responses")]
        self.assertTrue(all(response["status"] == "refused" for response in responses))
        self.assertEqual({response["code"] for response in responses}, {"invalid_health", "invalid_service", "invalid_image"})

    def test_claim_fsyncs_source_and_processing_directories(self):
        request = self.make_request("health.inspect", "HRQ-FSYNC01")
        path = self.enqueue(request, "fsync")
        synced = []
        real_fsync_directory = broker.fsync_directory

        def capture(directory):
            synced.append(Path(directory))
            real_fsync_directory(directory)

        broker.fsync_directory = capture
        try:
            broker.Broker(self.config).process_file(path)
        finally:
            broker.fsync_directory = real_fsync_directory
        self.assertIn(self.spool / "requests", synced)
        self.assertIn(self.spool / "processing", synced)

    def test_acceptance_validates_configured_signer_and_actual_signature_contract(self):
        args = type("Args", (), {"config": str(self.config_path)})()
        output = tempfile.TemporaryFile(mode="w+")
        old_stdout = sys.stdout
        sys.stdout = output
        try:
            self.assertEqual(broker.command_acceptance(args), 0)
        finally:
            sys.stdout = old_stdout
        output.seek(0)
        self.assertEqual(json.load(output)["signing"], "ok")
        output.close()

        unsupported = self.temp / "ed25519.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(unsupported)], check=True, capture_output=True)
        unsupported.chmod(0o600)
        self.config["privateKeyPath"] = str(unsupported)
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(broker.Refusal) as raised:
            broker.command_acceptance(args)
        self.assertEqual(raised.exception.code, "invalid_signing_key")

        unsupported_ec = self.temp / "ec.pem"
        subprocess.run([
            "openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(unsupported_ec),
        ], check=True, capture_output=True)
        unsupported_ec.chmod(0o600)
        self.config["privateKeyPath"] = str(unsupported_ec)
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(broker.Refusal) as raised:
            broker.command_acceptance(args)
        self.assertEqual(raised.exception.code, "invalid_signing_key")

        self.config["privateKeyPath"] = str(self.temp / "missing.pem")
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(broker.Refusal) as raised:
            broker.command_acceptance(args)
        self.assertEqual(raised.exception.code, "invalid_signing_key")

    def test_signing_key_rejects_undersized_rsa(self):
        for bits in (512, 1024):
            with self.subTest(bits=bits):
                key = self.temp / f"rsa-{bits}.pem"
                subprocess.run([
                    "openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{bits}",
                    "-out", str(key),
                ], check=True, capture_output=True)
                key.chmod(0o600)
                with self.assertRaises(broker.Refusal) as raised:
                    broker.validate_signing_key(key)
                self.assertEqual(raised.exception.code, "invalid_signing_key")
                self.assertIn("at least 2048 bits", str(raised.exception))

    def test_image_ref_is_config_driven_and_pinned_to_the_deployed_sudoers(self):
        # case_954f2dc1: the host's image reference is host state, not source state, so
        # image.inspect reads it from root-owned config. That alone would let a
        # config edit change which image the broker attests, so a non-test-mode
        # start re-derives the reference from the readable sudoers mirror and
        # refuses on disagreement. The unit separately binds that mirror to the
        # protected deployed grant before the unprivileged check runs.
        executed = []

        def capture_run_fixed(command, timeout=20):
            executed.append(command)
            return json.dumps({"Id": "sha256:" + "4" * 64, "Digest": None, "RepoTags": [], "Created": None, "Size": None})

        real_run_fixed = broker.run_fixed
        old_fake_dir = os.environ.pop("HOST_OPS_FAKE_FACTS_DIR", None)
        broker.run_fixed = capture_run_fixed
        try:
            config = dict(self.config, paperclipImageRef="ghcr.io/paperclipai/paperclip@sha256:" + "f" * 64)
            broker.execute_image(config)
            self.assertEqual(executed, [[
                "/usr/bin/sudo", "-n", "-u", "root", "/usr/bin/docker", "image", "inspect",
                config["paperclipImageRef"], "--format", "{{json .}}",
            ]])
            self.assertNotIn("paperclip-local", " ".join(executed[0]))
            broker.execute_image(config)
            self.assertEqual(executed[1], executed[0])
            # A config that still names the retired Ubuntu pin is refused at load.
            retired_path = self.temp / "retired-pin-config.json"
            retired_path.write_text(json.dumps({**self.config, "paperclipHostUser": "ubuntu"}))
            with self.assertRaises(broker.Refusal) as retired:
                broker.load_config(retired_path)
            self.assertEqual(retired.exception.code, "unsafe_config")

            for bad in ("paperclip-local --format {{json .}}, /bin/sh", "paperclip local", "", "-local"):
                with self.subTest(bad=bad):
                    with self.assertRaises(broker.Refusal) as raised:
                        broker.execute_image(dict(self.config, paperclipImageRef=bad))
                    self.assertEqual(raised.exception.code, "unsafe_config")
        finally:
            broker.run_fixed = real_run_fixed
            if old_fake_dir is not None:
                os.environ["HOST_OPS_FAKE_FACTS_DIR"] = old_fake_dir

        # case_4f3123eb: read the grant back the way the INSTALLER writes it. sudoers
        # reads a bare colon as the run-as separator, so install.sh escapes each
        # one as "\:" and sudo strips the escape before matching argv. Rendering
        # the placeholder raw here would assert a file the installer can never
        # produce -- visudo rejects it -- and would hide a broker that compared
        # the escaped bytes to the plain config value and refused every tag and
        # digest reference. Both forms are checked; both carry a colon.
        for granted in (
            "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b",
            "ghcr.io/paperclipai/paperclip@sha256:" + "f" * 64,
            "paperclip-local",
        ):
            with self.subTest(granted=granted):
                installed = (ROOT / "host-ops" / "host-ops-broker.sudoers").read_text().replace(
                    "@@PAPERCLIP_IMAGE_REF@@", granted.replace(":", "\\:")
                )
                self.assertEqual(broker.sudoers_granted_image_ref(installed), granted)

        granted = "localhost/paperclip-local:case_2e2581b7v2-51ee6c01b"
        rendered = (ROOT / "host-ops" / "host-ops-broker.sudoers").read_text().replace(
            "@@PAPERCLIP_IMAGE_REF@@", granted.replace(":", "\\:")
        )
        self.assertEqual(broker.sudoers_granted_image_ref(rendered), granted)
        # Exercise the deployed mirror check, not a locally invented refusal.
        mirror = self.temp / "image-sudoers.rendered"
        mirror.write_text(rendered)
        old_mirror = broker.DEPLOYED_SUDOERS_MIRROR_PATH
        broker.DEPLOYED_SUDOERS_MIRROR_PATH = str(mirror)
        try:
            broker.require_image_ref_matches_sudoers(granted)
            with self.assertRaises(broker.Refusal) as raised:
                broker.require_image_ref_matches_sudoers("ghcr.io/paperclipai/paperclip:v1")
            self.assertEqual(raised.exception.code, "unsafe_config")
        finally:
            broker.DEPLOYED_SUDOERS_MIRROR_PATH = old_mirror
        # Zero, duplicate, wrong-engine and widened grants all refuse.
        for text in (
            rendered.replace("docker image inspect", "docker image list"),
            rendered.replace("/usr/bin/docker", "/usr/bin/podman"),
            rendered.replace("--format {{json .}}", "--format {{json .}}, /bin/sh"),
            rendered.replace("ALL=(root)", "ALL=(ubuntu)"),
            rendered.replace("Cmnd_Alias PAPERCLIP_IMAGE_READ", "Cmnd_Alias OTHER_IMAGE_READ"),
            rendered + rendered,
        ):
            with self.subTest(text=text[:40]):
                with self.assertRaises(broker.Refusal):
                    broker.sudoers_granted_image_ref(text)

    def test_docker_image_payload_is_filtered_without_inventing_a_digest(self):
        executed = []
        real_run_fixed = broker.run_fixed
        old_fake_dir = os.environ.pop("HOST_OPS_FAKE_FACTS_DIR", None)

        def capture_run_fixed(command, timeout=20):
            executed.append(command)
            return json.dumps({
                "Id": "sha256:" + "4" * 64,
                "RepoTags": ["paperclip-local:reviewed"],
                "Created": "2026-10-04T00:00:00Z",
                "Size": 123,
                "Config": {"Env": ["PRIVATE_VALUE=must-not-escape"]},
            })

        broker.run_fixed = capture_run_fixed
        try:
            result = broker.execute_image(self.config)
        finally:
            broker.run_fixed = real_run_fixed
            if old_fake_dir is not None:
                os.environ["HOST_OPS_FAKE_FACTS_DIR"] = old_fake_dir
        self.assertEqual(executed, [[
            "/usr/bin/sudo", "-n", "-u", "root", "/usr/bin/docker", "image", "inspect",
            self.config["paperclipImageRef"], "--format", "{{json .}}",
        ]])
        self.assertEqual(set(result), EXPECTED_RESULT_KEYS["image.inspect"])
        self.assertIsNone(result["Digest"])
        self.assertNotIn("must-not-escape", json.dumps(result))

    def test_unit_deliberately_omits_incompatible_namespace_filter(self):
        # case_81816394: the rootful Docker client needs full namespace access, and
        # systemd's RestrictNamespaces seccomp filter rejects namespace entry
        # unless every type is allowed, which is no restriction.
        service_source = (ROOT / "host-ops" / "host-ops-broker.service").read_text()
        active_directives = [
            line for line in service_source.splitlines()
            if re.match(r"^\s*RestrictNamespaces\s*=", line)
        ]
        self.assertEqual(active_directives, [])
        self.assertIn("RestrictNamespaces is intentionally omitted", service_source)
        self.assertNotIn("Podman", service_source)
        self.assertNotIn("setns", service_source)

    def test_acceptance_requires_each_service_and_image_contract(self):
        args = type("Args", (), {"config": str(self.config_path)})()
        self.private_key.chmod(0o600)
        cases = (
            ("service", {}, "invalid_service"),
            ("image", {"Id": None, "Digest": None, "RepoTags": None, "Created": None, "Size": None}, "invalid_image"),
        )
        for fact, payload, code in cases:
            with self.subTest(fact=fact):
                original = (self.facts / f"{fact}.json").read_text()
                (self.facts / f"{fact}.json").write_text(json.dumps(payload))
                try:
                    with self.assertRaises(broker.Refusal) as raised:
                        broker.command_acceptance(args)
                    self.assertEqual(raised.exception.code, code)
                finally:
                    (self.facts / f"{fact}.json").write_text(original)

    def test_production_config_rejects_alternate_signing_path(self):
        self.config["testMode"] = False
        self.config["privateKeyPath"] = "/root/alternate-signing.pem"
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(broker.Refusal) as raised:
            broker.load_config(self.config_path)
        self.assertEqual(raised.exception.code, "unsafe_config")

    def test_fake_fact_seam_fails_closed_outside_test_mode(self):
        self.config["testMode"] = False
        self.config["privateKeyPath"] = "/var/lib/paperclip-host-ops/keys/response-signing.pem"
        self.config_path.write_text(json.dumps(self.config))
        old_fake_dir = os.environ.get("HOST_OPS_FAKE_FACTS_DIR")
        try:
            with self.assertRaises(broker.Refusal) as raised:
                broker.fake_fact(self.config, "health")
        finally:
            if old_fake_dir is not None:
                os.environ["HOST_OPS_FAKE_FACTS_DIR"] = old_fake_dir
        self.assertEqual(raised.exception.code, "test_seam_refused")

    def test_spool_directory_poison_is_refused_without_crashing(self):
        poison = self.spool / "requests" / "HRQ-POISON1.json"
        poison.mkdir(parents=True)
        completed = self.run_once()
        self.assertEqual(json.loads(completed.stdout)["processed"], 1)
        response = self.read_json(self.outputs("responses")[0])
        self.assertEqual(response["code"], "invalid_spool_entry")
        self.assertFalse(poison.exists())

    def test_request_cli_publishes_atomically_with_group_readable_mode(self):
        marker = self.temp / "request-ready-before-publish"
        environment = os.environ.copy()
        environment["HOST_OPS_TEST_BEFORE_PUBLISH_MARKER"] = str(marker)
        previous = os.umask(0o077)
        try:
            requester = subprocess.Popen([
                sys.executable, str(BROKER), "request", "--spool", str(self.spool),
                "--verb", "health.inspect", "--request-id", "HRQ-ATOMIC1",
                "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
            ], env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.umask(previous)

        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)
        while not marker.exists() and dt.datetime.now(dt.timezone.utc) < deadline:
            import time
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "request CLI did not reach the pre-publication pause")
        request_dir = self.spool / "requests"
        self.assertEqual(list(request_dir.glob("*.json")), [])
        temporary_paths = list(request_dir.glob(".*"))
        self.assertEqual(len(temporary_paths), 1)
        self.assertEqual(temporary_paths[0].stat().st_mode & 0o777, 0o640)

        concurrent = self.run_once()
        self.assertEqual(json.loads(concurrent.stdout), {"processed": 0})
        self.assertEqual(self.outputs("responses"), [])

        marker.with_name(f"{marker.name}.release").touch()
        stdout, stderr = requester.communicate(timeout=10)
        self.assertEqual(requester.returncode, 0, stderr)
        request_path = Path(json.loads(stdout)["requestPath"])
        self.assertEqual(request_path.stat().st_mode & 0o777, 0o640)

        completed = self.run_once()
        self.assertEqual(json.loads(completed.stdout), {"processed": 1})
        responses = self.outputs("responses")
        self.assertEqual(len(responses), 1)
        self.assertEqual(self.verify_signed(responses[0])["status"], "succeeded")
        self.assertEqual(list(request_dir.glob("*.json")), [])

        duplicate = subprocess.run([
            sys.executable, str(BROKER), "request", "--spool", str(self.spool),
            "--verb", "health.inspect", "--request-id", "HRQ-DUPLICATE",
            "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
        ], text=True, capture_output=True)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        duplicate_path = Path(json.loads(duplicate.stdout)["requestPath"])
        original = duplicate_path.read_bytes()
        with self.assertRaises(FileExistsError):
            broker.atomic_write(duplicate_path, b"replacement\n", mode=0o640, exclusive=True)
        self.assertEqual(duplicate_path.read_bytes(), original)

    def test_request_cli_accepts_no_path_shell_or_auth_arguments(self):
        help_text = subprocess.run([sys.executable, str(BROKER), "request", "--help"], check=True, text=True, capture_output=True).stdout
        for forbidden in ("--path", "--command", "--shell", "--token", "--approval", "--url", "--package", "--sha", "--script", "--unit", "--app", "--keys"):
            self.assertNotIn(forbidden, help_text)
        request_id = "HRQ-CLI0001"
        completed = subprocess.run([
            sys.executable, str(BROKER), "request", "--spool", str(self.spool), "--verb", "health.inspect",
            "--request-id", request_id, "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
        ], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        request_path = Path(json.loads(completed.stdout)["requestPath"])
        request = self.read_json(request_path)
        self.assertEqual(request["arguments"], {})
        self.assertEqual(broker.validate_request(request)[0]["requestId"], request_id)

    def test_request_cli_write_arguments_are_one_json_object(self):
        # case_5248bcfa: reason/approval can never exist on this CLI (see the
        # --arguments-json comment in parser()), so a write-verb request built
        # here is always refused for missing shape now, even with perfectly
        # well-formed arguments. agentApprovalAllowed stays False in practice:
        # approval has to be constructed by a non-agent-controlled path.
        well_formed = subprocess.run([
            sys.executable, str(BROKER), "request", "--spool", str(self.spool), "--verb", "plugin.install",
            "--arguments-json", json.dumps(WRITE_ARGUMENTS["plugin.install"]),
            "--request-id", "HRQ-CLIW001", "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
        ], text=True, capture_output=True)
        self.assertEqual(well_formed.returncode, 2)
        self.assertIn("reason", well_formed.stderr)
        self.assertEqual(list((self.spool / "requests").glob("*.json")), [])
        refused = subprocess.run([
            sys.executable, str(BROKER), "request", "--spool", str(self.spool), "--verb", "plugin.install",
            "--arguments-json", json.dumps({"package": "dispatch-src"}),
            "--request-id", "HRQ-CLIW002", "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
        ], text=True, capture_output=True)
        self.assertEqual(refused.returncode, 2)
        extra = subprocess.run([
            sys.executable, str(BROKER), "request", "--spool", str(self.spool), "--verb", "health.inspect",
            "--arguments-json", json.dumps({"url": "http://evil"}),
            "--request-id", "HRQ-CLIW003", "--agent-id", AGENT, "--run-id", RUN, "--issue-id", ISSUE,
        ], text=True, capture_output=True)
        self.assertEqual(extra.returncode, 2)

    def test_runbook_documents_atomic_bundle_paths(self):
        runbook = (ROOT / "host-ops" / "RUNBOOK.md").read_text()
        self.assertIn("bundle=/path/to/previews/HRQ-....bundle", runbook)
        self.assertIn('base=${base%.bundle}', runbook)
        self.assertIn('--response "$bundle/$base.json"', runbook)
        self.assertIn('--signature "$bundle/$base.sig"', runbook)
        self.assertIn("atomic publication unit", runbook)
        for stale in ("previews/*.json", "responses/*.json", "/previews/HRQ-....json"):
            self.assertNotIn(stale, runbook)

    def test_runbook_documents_phase1_dry_run_gate(self):
        runbook = (ROOT / "host-ops" / "RUNBOOK.md").read_text()
        self.assertIn("--arguments-json", runbook)
        self.assertIn("write_dry_run", runbook)
        self.assertIn("writesEnabled: false", runbook)
        self.assertIn("OWNER decision O4", runbook)
        self.assertIn("no executor runs and no host write happens", runbook)

    def test_runbook_requires_live_host_evidence_for_node7b1ff338_docker_compatibility(self):
        runbook = (ROOT / "host-ops" / "RUNBOOK.md").read_text()
        self.assertIn("`RestrictNamespaces=` is deliberately absent", runbook)
        self.assertIn("A green CI", runbook)
        self.assertIn("not installation evidence", runbook)
        self.assertIn("unprivileged acceptance check passed", runbook)
        self.assertIn("service remained active", runbook)
        self.assertNotIn("rootless Podman", runbook)
        self.assertNotIn("paperclip.service", runbook)

    def test_installer_export_ignores_git_replacement_objects(self):
        repository = self.temp / "replacement-repo"
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Host Ops Test"], cwd=repository, check=True)
        host_ops = repository / "host-ops"
        host_ops.mkdir()
        marker = host_ops / "broker.py"
        marker.write_text("reviewed\n")
        subprocess.run(["git", "add", "host-ops/broker.py"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "reviewed"], cwd=repository, check=True)
        reviewed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, capture_output=True).stdout.strip()
        marker.write_text("attacker\n")
        subprocess.run(["git", "commit", "-qam", "attacker"], cwd=repository, check=True)
        attacker = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, capture_output=True).stdout.strip()
        subprocess.run(["git", "replace", reviewed, attacker], cwd=repository, check=True)

        ordinary = subprocess.run(["git", "archive", reviewed, "host-ops"], cwd=repository, check=True, capture_output=True).stdout
        protected = subprocess.run(["git", "--no-replace-objects", "archive", reviewed, "host-ops"], cwd=repository, check=True, capture_output=True).stdout
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(ordinary)) as archive:
            self.assertEqual(archive.extractfile("host-ops/broker.py").read(), b"attacker\n")
        with tarfile.open(fileobj=io.BytesIO(protected)) as archive:
            self.assertEqual(archive.extractfile("host-ops/broker.py").read(), b"reviewed\n")

    def test_installer_requires_source_ref_to_name_commit_object_directly(self):
        repository = self.temp / "tag-object-repo"
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Host Ops Test"], cwd=repository, check=True)
        marker = repository / "marker"
        marker.write_text("reviewed\n")
        subprocess.run(["git", "add", "marker"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "reviewed"], cwd=repository, check=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, capture_output=True).stdout.strip()
        subprocess.run(["git", "tag", "-a", "reviewed-tag", "-m", "reviewed"], cwd=repository, check=True)
        tag_object = subprocess.run(["git", "rev-parse", "reviewed-tag"], cwd=repository, check=True, text=True, capture_output=True).stdout.strip()
        self.assertEqual(len(commit), 40)
        self.assertEqual(len(tag_object), 40)
        self.assertEqual(subprocess.run(["git", "--no-replace-objects", "cat-file", "-t", commit], cwd=repository, check=True, text=True, capture_output=True).stdout.strip(), "commit")
        self.assertEqual(subprocess.run(["git", "--no-replace-objects", "cat-file", "-t", tag_object], cwd=repository, check=True, text=True, capture_output=True).stdout.strip(), "tag")

    def test_install_service_tmpfiles_and_sudoers_contracts_are_consistent(self):
        install_source = (ROOT / "host-ops" / "install.sh").read_text()
        service_source = (ROOT / "host-ops" / "host-ops-broker.service").read_text()
        tmpfiles_source = (ROOT / "host-ops" / "host-ops-broker.tmpfiles").read_text()
        sudoers_source = (ROOT / "host-ops" / "host-ops-broker.sudoers").read_text()
        example_config_path = ROOT / "host-ops" / "config.example.json"
        # The shipped example carries the same placeholder as the sudoers
        # template; install.sh renders both from one --paperclip-image-ref. Render
        # it here the way the installer does so the rest of this contract check
        # reads a config the broker would actually accept.
        rendered_example = json.loads(
            example_config_path.read_text().replace("@@PAPERCLIP_IMAGE_REF@@", self.config["paperclipImageRef"])
        )
        self.assertEqual(rendered_example["paperclipImageRef"], self.config["paperclipImageRef"])
        rendered_example_path = self.temp / "rendered-example-config.json"
        rendered_example["testMode"] = True
        rendered_example_path.write_text(json.dumps(rendered_example))
        example_config = broker.load_config(rendered_example_path)

        exec_start = re.search(r"^ExecStart=(\S+) run --config (\S+)$", service_source, re.MULTILINE)
        self.assertIsNotNone(exec_start)
        executable, config_path = exec_start.groups()
        unit_path = "/etc/systemd/system/paperclip-host-ops-broker.service"
        # case_2f1968ad: the shell no longer direct-copies managed bytes or drives
        # systemd itself. It prevalidates the reviewed export, stages config /
        # sudoers / unit temporaries, then delegates publication to the
        # journaled transaction with the fixed inventory. Assert that real
        # shell -> transaction -> fixed-inventory contract as live values.
        transaction_source = (ROOT / "host-ops" / "install_transaction.py").read_text()
        # No direct-copy publication remains: the shell stages temporaries
        # ($SOURCE_DIR references below are prevalidation only: py_compile,
        # image-binding reads, unit-rewrite heredoc), and the single GNU
        # install-era publication path is gone -- the transaction owns it.
        self.assertNotRegex(install_source, r"(?m)^\s*install\s+(-o\s+root|CONFIG_TMP|SUDOERS_TMP)")
        self.assertNotIn("install -o root", install_source)
        self.assertNotIn("install -o paperclip", install_source)
        self.assertIn('python3 -B "$SOURCE_DIR/install_transaction.py" apply', install_source)
        self.assertIn('--source-dir "$SOURCE_DIR" --source-ref "$SOURCE_REF"', install_source)
        self.assertIn('--config "$CONFIG_TMP" --sudoers "$SUDOERS_TMP"', install_source)
        # Fixed runtime inventory: every managed byte has one reviewed source
        # name and one absolute destination. Read the values, never retypes.
        self.assertEqual(install_state.FILES["broker.py"], executable)
        self.assertEqual(install_state.FILES["host-ops-broker.service"], unit_path)
        self.assertEqual(install_state.FILES["config"], config_path)
        self.assertEqual(set(install_state.FILES),
                         {"broker.py", "image_binding.py", "RUNBOOK.md",
                          "host-ops-broker.service", "host-ops-broker.tmpfiles",
                          "sudoers", "sudoers-mirror", "config", "public-key"})
        # The private signing key is never published: it lives in KEYS metadata
        # only, and FILES carries just the fixed public-key output destination.
        self.assertNotIn(install_state.KEYS[0], install_state.FILES.values())
        self.assertEqual(install_state.FILES["public-key"], install_state.PUBLIC_OUT)
        self.assertEqual(install_state.PUBLIC_OUT,
                         "/etc/paperclip-host-ops/response-signing.pub.pem")
        self.assertIn('[[ "$PUBLIC_KEY_OUT" == /etc/paperclip-host-ops/response-signing.pub.pem ]]', install_source)
        # Staged unit is the reviewed export validated before handoff.
        self.assertIn('UNIT_TMP="$WORK_DIR/paperclip-host-ops-broker.service"', install_source)
        self.assertIn('cp "$SOURCE_DIR/host-ops-broker.service" "$UNIT_TMP"', install_source)
        self.assertIn('systemd-analyze verify "$UNIT_TMP"', install_source)
        # config.json is rendered from --paperclip-image-ref (preserving an
        # existing operator config), staged disabled, and published by the
        # transaction at reader-group 0640. The shell seeds the stage from the
        # reviewed example only when no operator config exists.
        self.assertIn('cp "$SOURCE_DIR/config.example.json" "$CONFIG_TMP"', install_source)
        self.assertIn('render_disabled_config "$CONFIG_TMP" "$CONFIG_TMP" "$PAPERCLIP_IMAGE_REF"', install_source)
        self.assertEqual(install_transaction.MODES["config"], 0o640)
        self.assertIn("config", install_transaction.READER_GROUP_FILES)
        self.assertIn('"writesEnabled") is not False', transaction_source)
        self.assertIn('"testMode") is not False', transaction_source)
        self.assertIn('git --no-replace-objects archive --format=tar "$SOURCE_REF" host-ops', install_source)
        self.assertIn('source_type=$(git --no-replace-objects cat-file -t "$SOURCE_REF"', install_source)
        self.assertIn('[[ "$source_type" == commit ]]', install_source)
        self.assertIn('git --no-replace-objects cat-file -e "$SOURCE_REF:host-ops/broker.py"', install_source)
        self.assertNotIn("git diff --quiet -- host-ops", install_source)
        # Reviewed export covers the journaled runtime: all transaction
        # modules must be required regular files of the reviewed commit.
        # spool_provisioning.py rides along because the transaction imports
        # it and the rollback entrypoint re-executes from the journal.
        for reviewed in ("install_state.py", "install_transaction.py",
                         "spool_provisioning.py"):
            self.assertIn(reviewed, install_source)
        self.assertIn('[[ -f "$SOURCE_DIR/$source" && ! -L "$SOURCE_DIR/$source" ]]', install_source)
        # Service lifecycle runs through the journaled helper only: the shell
        # stages and hands off; start/enable/stop/daemon-reload are the
        # transaction's sequenced, rollback-guarded calls. No direct shell
        # systemctl mutation and no shell restart/is-active shortcut remain.
        self.assertNotIn("systemctl enable", install_source)
        self.assertNotIn("systemctl restart", install_source)
        self.assertNotIn("systemctl is-active", install_source)
        self.assertNotIn("systemctl stop", install_source)
        self.assertNotIn("systemctl daemon-reload", install_source)
        for call in ('state.systemctl("stop", state.UNIT)',
                     'state.systemctl("daemon-reload")',
                     'state.systemctl("enable", state.UNIT)',
                     'state.systemctl("start", state.UNIT)'):
            self.assertIn(call, transaction_source)

        service_user = re.search(r"^User=(\S+)$", service_source, re.MULTILINE).group(1)
        service_group = re.search(r"^Group=(\S+)$", service_source, re.MULTILINE).group(1)
        install_lib_source = (ROOT / "host-ops" / "install-lib.sh").read_text()
        # Upgrade-only identity: no account provisioning anywhere in the
        # installer. The shell classifies and refuses without a valid existing
        # identity; the transaction requires the pre-provisioned system
        # identity and unchanged runtime directories before snapshotting.
        self.assertNotIn("useradd", install_source)
        self.assertNotIn("groupadd", install_source)
        self.assertNotIn("useradd", transaction_source)
        self.assertNotIn("groupadd", transaction_source)
        self.assertNotIn(f"--home-dir {example_config['statePath']}", install_source)
        self.assertIn('identity_state=$(classify_service_identity)', install_source)
        self.assertIn('"transactional upgrade requires the existing broker identity', install_source)
        self.assertIn('validate_service_identity', install_source)
        self.assertIn('"pre-provisioned broker identity is required"', transaction_source)
        self.assertIn('"upgrade requires unchanged pre-provisioned runtime directories"', transaction_source)
        for validation in (
            "must be a system account", "credentials must be locked", "primary group is incompatible",
            "home is incompatible", "shell is incompatible", "has unintended supplemental groups",
            "must contain only paperclip-host-reader", "private and public keys do not match",
            "RSA key must be at least 2048 bits",
        ):
            self.assertIn(validation, install_lib_source)
        self.assertIn('openssl pkey -in "$private_key" -check -noout', install_lib_source)
        self.assertIn('openssl pkey -in "$private_key" -pubout', install_lib_source)
        self.assertIn('openssl pkey -pubin -in "$public_key" -outform DER', install_lib_source)
        # case_a2b959d4: single source of truth — the host-ops parse loop lives
        # in scripts/verify-fast.sh, which CI's Syntax step calls via
        # --parse-only. Pin the wiring (ci.yml calls the script) and the
        # loop (the script still covers host-ops/*.sh with bash -n).
        syntax_source = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/verify-fast.sh --parse-only", syntax_source)
        verify_fast = (ROOT / "scripts" / "verify-fast.sh").read_text()
        self.assertRegex(verify_fast, r'host-ops/\*\.sh; do\n\s+bash -n ')

        exec_start_pre = re.search(r"^ExecStartPre=(\S+) acceptance-check --config (\S+)$", service_source, re.MULTILINE)
        self.assertIsNotNone(exec_start_pre)
        self.assertEqual(exec_start_pre.groups(), (executable, config_path))
        # case_4f3123eb: sudo ignores a grant unless it remains root:root 0440, so the
        # service user cannot read that authority file directly. Bind a readable
        # mirror to it with one root-only cmp, but keep acceptance-check and the
        # long-running daemon unprefixed so both exercise the service user's real
        # exact-command sudo grant rather than root bypassing it.
        protected_sudoers = "/etc/sudoers.d/paperclip-host-ops-broker"
        sudoers_mirror = "/etc/paperclip-host-ops/sudoers.rendered"
        self.assertIn(
            f"ExecStartPre=+/usr/bin/cmp --silent {protected_sudoers} {sudoers_mirror}",
            service_source,
        )
        # Both destinations are the fixed inventory, read as live values.
        self.assertEqual(install_state.FILES["sudoers"], protected_sudoers)
        self.assertEqual(install_state.FILES["sudoers-mirror"], sudoers_mirror)
        # case_2f1968ad: the shell stages ONE sudoers temp (rendered from the
        # reviewed template, 0440, visudo-checked) and hands it off. The
        # transaction publishes those identical staged bytes to BOTH
        # destinations: root-owned 0440 authority plus the reader-group 0640
        # mirror. Behavioural byte-identity and rollback already proved in the
        # focused temporary-root transaction tests; this pins the publication
        # modes and group split as live values, never retyped literals.
        self.assertIn('render_sudoers_image_ref "$SOURCE_DIR/host-ops-broker.sudoers" "$SUDOERS_TMP"', install_source)
        self.assertIn('chmod 0440 "$SUDOERS_TMP"', install_source)
        self.assertIn('visudo -cf "$SUDOERS_TMP"', install_source)
        self.assertEqual(install_transaction.MODES["sudoers"], 0o440)
        self.assertEqual(install_transaction.MODES["sudoers-mirror"], 0o640)
        self.assertIn("sudoers-mirror", install_transaction.READER_GROUP_FILES)
        self.assertNotIn("sudoers", install_transaction.READER_GROUP_FILES)
        self.assertIn('"sudoers-mirror": args.sudoers', transaction_source)
        self.assertIn('gid = reader_gid if name in READER_GROUP_FILES else os.getgid()', transaction_source)
        # Production ownership is root:root for the authority file: the
        # operator CLI requires root:root and publishes as journal.owner
        # (UID 0) with the non-reader gid (root group in production).
        self.assertIn('if os.geteuid() != 0 or os.getgid() != 0', transaction_source)
        self.assertIn('os.fchown(stream.fileno(), journal.owner, gid)', transaction_source)
        self.assertEqual(broker.DEPLOYED_SUDOERS_MIRROR_PATH, sudoers_mirror)
        # No key rotation, no tmpfiles/account mutation anywhere in the
        # installer: the signing pair is validated fixed targets only, runtime
        # directories must already match, and a directory-changing tmpfiles
        # upgrade refuses for separate preparation.
        for secret_gen in ("genpkey", "genrsa", "openssl req", "openssl ecparam"):
            with self.subTest(no_key_rotation=secret_gen):
                self.assertNotIn(secret_gen, install_source)
                self.assertNotIn(secret_gen, transaction_source)
        self.assertNotIn("systemd-tmpfiles", install_source)
        self.assertNotIn("systemd-tmpfiles", transaction_source)
        self.assertIn('"directory-changing tmpfiles upgrade requires separate preparation"', transaction_source)
        self.assertIn('"upgrade requires existing broker-owned keys with unchanged modes"', transaction_source)
        self.assertNotIn(f"ExecStartPre=+{executable} acceptance-check", service_source)
        self.assertNotIn(f"ExecStart=+{executable} run", service_source)
        tmpfiles_rows = {}
        for line in tmpfiles_source.splitlines():
            if line.startswith("d "):
                _, path, mode, owner, group, _ = line.split()
                tmpfiles_rows[path] = (mode, owner, group)
        for path in (
            example_config["statePath"],
            str(Path(example_config["statePath"]) / "keys"),
            str(Path(example_config["spoolPath"]) / ".staging"),
            str(Path(example_config["spoolPath"]) / "processing"),
            str(Path(example_config["spoolPath"]) / "quarantine"),
            str(Path(example_config["auditPath"]).parent),
        ):
            with self.subTest(tmpfiles_identity=path):
                self.assertEqual(tmpfiles_rows[path][1:], (service_user, service_group))
        self.assertIn(example_config["spoolPath"], tmpfiles_rows)
        self.assertIn(str(Path(example_config["spoolPath"]) / "requests"), tmpfiles_rows)

        # Rootless Podman mutates runtime and storage lock state even for image
        # inspect. Sudo changes identity but not this unit's mount namespace, so
        # pin the complete writable authority set while preserving both strict
        # filesystem protections. Iterating every directive is important: a
        # first-line-only check would miss an appended broad writable path.
        expected_hardening = {
            "NoNewPrivileges=no",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectKernelLogs=yes",
            "ProtectControlGroups=yes",
            "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes",
            "MemoryMax=256M",
            "TasksMax=32",
            "RestrictSUIDSGID=yes",
            "RestrictRealtime=yes",
            "SystemCallArchitectures=native",
            "UMask=0027",
        }
        self.assertTrue(expected_hardening.issubset(set(service_source.splitlines())))
        self.assertNotRegex(service_source, r"(?m)^\s*RestrictNamespaces\s*=")
        self.assertNotRegex(service_source, r"(?m)^\s*(ReadWriteDirectories|BindPaths)\s*=")
        writable_paths = {
            path
            for line in service_source.splitlines()
            if line.startswith("ReadWritePaths=")
            for path in line.removeprefix("ReadWritePaths=").split()
        }
        # node7b1ff338 is rootful Docker: no per-user runtime or container-storage
        # path needs to stay writable. The only writable set is the broker's
        # own host-owned state/spool/audit directories.
        self.assertEqual(
            writable_paths,
            {
                example_config["statePath"],
                str(Path(example_config["auditPath"]).parent),
                example_config["spoolPath"],
            },
        )
        for broad_path in ("/", "/home", "/home/ubuntu", "/run/user"):
            self.assertNotIn(broad_path, writable_paths)
        self.assertNotIn("/run/user/1000", service_source)
        self.assertNotIn(".local/share/containers", service_source)
        readonly_paths = set(re.search(r"^ReadOnlyPaths=(.*)$", service_source, re.MULTILINE).group(1).split())
        self.assertIn(str(Path(config_path).parent), readonly_paths)
        self.assertIn(str(Path(executable).parent), readonly_paths)
        self.assertIn(example_config["paperclipDataPath"], readonly_paths)
        self.assertTrue(Path(example_config["privateKeyPath"]).is_relative_to(example_config["statePath"]))

        # Run-as bindings: the three Docker executor grants plus the container
        # binding read are root-bound; only the E3 CLIProxy unit read is
        # ubuntu-bound (--user units). Any other identity would hand the
        # broker another account's state.
        expected_bindings = {
            "PAPERCLIP_LOGS_READ": "root",
            "PAPERCLIP_SERVICE_READ": "root",
            "PAPERCLIP_IMAGE_READ": "root",
            "PAPERCLIP_CONTAINER_READ": "root",
            "PAPERCLIP_CLIPROXY_READ": "ubuntu",
        }
        for alias, run_as in expected_bindings.items():
            sudoers_match = re.search(
                rf"^(\S+) ALL=\((\S+)\) NOPASSWD: {alias}$",
                sudoers_source,
                re.MULTILINE,
            )
            self.assertIsNotNone(sudoers_match, alias)
            sudoers_grantee, sudoers_run_as = sudoers_match.groups()
            self.assertEqual(sudoers_grantee, service_user)
            self.assertEqual(sudoers_run_as, run_as, alias)
        self.assertEqual(sudoers_source.count("(ubuntu)"), 1)
        self.assertEqual(sudoers_source.count("XDG_RUNTIME_DIR"), 1)

        # The image reference is host state rendered by install.sh, so the shipped
        # template carries the placeholder rather than any one host's image. The
        # rendered-vs-config agreement is asserted separately below and in
        # test_host_ops_install.sh.
        # case_5039e77b: the container-inspect read is installer-owned (case_43e41fc3 observed-
        # running-image read), exercised through host-ops/image_binding.py's
        # root-bound Docker argv. Fixed container name and pipe format only:
        # full container JSON can expose environment secrets, so it stays
        # ungranted. The binding's exact sudo argv is pinned below.
        container_grant = (
            "/usr/bin/docker container inspect paperclip "
            "--format {{.Image}}|{{.ImageDigest}}|{{.State.Running}}"
        )
        # case_f121fc6d (E3, case_477bab93 rev 8a515f6a): the CLIProxy grant is the
        # installer-owned CLIProxy unit read. Placeholder form: install.sh
        # renders @@PAPERCLIP_CLIPROXY_UNIT@@ from its required
        # --cliproxy-unit argument (no default, wildcard or invented name).
        # Read-only --user show of exactly ActiveState,SubState,MainPID -- no
        # ExecMainStartTimestampMonotonic, no extra property, no second unit
        # grant, and no lifecycle verb (E2).
        cliproxy_grant = (
            "/usr/bin/env XDG_RUNTIME_DIR=/run/user/1000 "
            "/usr/bin/systemctl --user show @@PAPERCLIP_CLIPROXY_UNIT@@ "
            "--no-pager --property=ActiveState\\,SubState\\,MainPID"
        )
        expected_sudoers_commands = {
            "/usr/bin/docker logs paperclip --tail 200 --since 15m --timestamps",
            "/usr/bin/docker inspect paperclip --format {{json .State}}",
            "/usr/bin/docker image inspect @@PAPERCLIP_IMAGE_REF@@ --format {{json .}}",
            container_grant,
            cliproxy_grant,
        }
        self.assertEqual(broker.LOG_COMMAND[:4], ("/usr/bin/sudo", "-n", "-u", "root"))
        self.assertIn(" ".join(broker.LOG_COMMAND[4:]), expected_sudoers_commands)
        self.assertEqual(list(broker.SERVICE_COMMAND[:4]), ["/usr/bin/sudo", "-n", "-u", "root"])
        self.assertIn(" ".join(broker.SERVICE_COMMAND[4:]), expected_sudoers_commands)
        actual_sudoers_commands = set()
        for alias in expected_bindings:
            command_lines = re.search(rf"Cmnd_Alias {alias} = \\\n(.*?)\n\n", sudoers_source, re.DOTALL).group(1)
            actual_sudoers_commands.update(
                line.strip().removesuffix(" \\").removesuffix(",")
                for line in command_lines.splitlines()
                if line.strip()
            )
        self.assertEqual(actual_sudoers_commands, expected_sudoers_commands)
        self.assertNotIn("journalctl", sudoers_source)
        # No grant targets the retired user unit (rbs1 runs none); the header
        # comment may name it, so check grant lines only, not the whole file.
        grant_lines = [line for line in sudoers_source.splitlines()
                       if line.startswith("  /usr/bin")]
        self.assertFalse(any("paperclip.service" in line for line in grant_lines))
        self.assertNotIn("podman", sudoers_source)
        # E2 + exact-argv shape: no lifecycle verb may ever appear as an
        # argv token in any grant; the CLIProxy read carries exactly the
        # three E3 properties and appears exactly once.
        for pinned_command in expected_sudoers_commands:
            argv = pinned_command.split()
            for lifecycle_verb in ("restart", "start", "stop", "enable",
                                   "reload", "daemon-reload", "is-active"):
                self.assertNotIn(lifecycle_verb, argv)
        self.assertNotIn("ExecMainStartTimestampMonotonic", cliproxy_grant)
        self.assertEqual(
            sum(1 for pinned in expected_sudoers_commands
                if "@@PAPERCLIP_CLIPROXY_UNIT@@" in pinned),
            1,
        )
        executed_commands = []
        real_run_fixed = broker.run_fixed

        # The container read is installer-owned, so the stub answers it with the
        # observable half of the helper contract: same Image ID and manifest
        # digest the pinned image inspection returns, container running.
        pinned_image_id = "sha256:" + "2" * 64
        pinned_digest = "sha256:" + "3" * 64
        observed_container = f"{pinned_image_id}|{pinned_digest}|true"

        def capture_run_fixed(command, timeout=20):
            executed_commands.append(command)
            if "inspect" in command and "image" not in command:
                return json.dumps({
                    "Status": "running", "Running": True, "Pid": 123,
                    "StartedAt": "2026-10-04T22:00:00Z", "Secret": "must-not-escape",
                })
            return json.dumps({
                "Id": pinned_image_id,
                "Digest": pinned_digest,
                "RepoTags": ["localhost/paperclip-local:latest"],
                "Created": "2026-08-27T00:00:00Z",
                "Size": 123,
                "Secret": "must-not-escape",
            })

        broker.run_fixed = capture_run_fixed
        old_fake_dir = os.environ.pop("HOST_OPS_FAKE_FACTS_DIR", None)
        try:
            service_result = broker.execute_service(self.config)
            image_result = broker.execute_image(self.config)
        finally:
            broker.run_fixed = real_run_fixed
            if old_fake_dir is not None:
                os.environ["HOST_OPS_FAKE_FACTS_DIR"] = old_fake_dir
        self.assertEqual(service_result["Status"], "running")
        self.assertEqual(service_result["Pid"], 123)
        self.assertEqual(set(image_result), EXPECTED_RESULT_KEYS["image.inspect"])
        self.assertEqual(executed_commands[0][:4], ["/usr/bin/sudo", "-n", "-u", "root"])
        self.assertEqual(executed_commands[1][:4], ["/usr/bin/sudo", "-n", "-u", "root"])
        actual_executor_commands = {" ".join(command[4:]) for command in executed_commands}
        # logs.read uses a separately bounded subprocess path, not run_fixed.
        actual_executor_commands.add(" ".join(broker.LOG_COMMAND[4:]))
        self.assertEqual(
            actual_executor_commands,
            {
                command.replace("@@PAPERCLIP_IMAGE_REF@@", self.config["paperclipImageRef"])
                for command in expected_sudoers_commands
                if "container inspect" not in command
                and "@@PAPERCLIP_CLIPROXY_UNIT@@" not in command
            },
        )
        # Positive control: the pinned grant set is exercisable end to end.
        # The helper issues the container read at its fixed argv and the two
        # halves agree (running ID == pinned image ID, both digests == pin);
        # a swapped digest refuses, so deletion of the digest check is caught.
        real_capture = binding.capture
        captured_argv = []

        def capture_binding(args, **kwargs):
            captured_argv.append(list(args))
            if args[:2] == ["container", "inspect"]:
                return observed_container + "\n"
            return json.dumps({"Id": pinned_image_id, "Digest": pinned_digest})

        digest_pin = "registry.example/paperclip@sha256:" + pinned_digest.removeprefix("sha256:")
        binding.assert_running_image(
            digest_pin,
            observed_container,
            json.dumps({"Id": pinned_image_id, "Digest": pinned_digest}),
        )
        binding.capture = capture_binding
        try:
            binding.check_running(digest_pin)
        finally:
            binding.capture = real_capture
        self.assertEqual(
            [call[:5] for call in captured_argv],
            [
                ["container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT],
                ["image", "inspect", digest_pin, "--format", "{{json .}}"],
                ["container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT],
            ],
        )
        with self.assertRaises(binding.Refusal):
            binding.assert_running_image(
                digest_pin,
                observed_container.replace(pinned_digest, "sha256:" + "d" * 64),
                json.dumps({"Id": pinned_image_id, "Digest": pinned_digest}),
            )
        expected_binding_argv = ["container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT]
        self.assertEqual(
            binding.ENGINE_PREFIX + expected_binding_argv,
            ["/usr/bin/sudo", "-n", "-u", "root", "/usr/bin/docker",
             "container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT],
        )
        # The binding's container read is the sudo-wrapped Docker form of the
        # pinned container_grant above (root-bound, no user-runtime env prefix).
        self.assertEqual(
            "/usr/bin/docker " + " ".join(expected_binding_argv),
            container_grant,
        )

        systemd_analyze = shutil.which("systemd-analyze")
        if systemd_analyze:
            # The committed unit names the production install path, which does not
            # exist on a CI runner. Verify the same unit grammar with only that
            # already-pinned executable path replaced by a real inert binary.
            # Otherwise systemd-analyze correctly fails on the missing install,
            # before it can certify the unit directives this test owns.
            self.assertEqual(service_source.count(executable), 2)
            validation_unit = self.temp / "host-ops-broker.validation.service"
            validation_source = service_source.replace(executable, shutil.which("true") or "/bin/true")
            validation_source = validation_source.replace("/usr/bin/cmp", shutil.which("true") or "/bin/true")
            validation_unit.write_text(validation_source)
            completed = subprocess.run(
                [systemd_analyze, "verify", str(validation_unit)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        visudo = shutil.which("visudo")
        if visudo:
            completed = subprocess.run(
                [visudo, "-cf", str(ROOT / "host-ops" / "host-ops-broker.sudoers")],
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
