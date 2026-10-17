#!/usr/bin/env python3
"""Hermetic suite for ops/case_f2010cb7/forward_beszel_discord.py.

Titles below are Beszel's exact alert shapes (henrygd/beszel source):
threshold notices "<system> <alert> above|below threshold"
(internal/alerts/alerts_system.go; alert names lowercased except
CPU/GPU/CPU*, so "disk usage"/"memory" are lowercase and "CPU" is not)
and connection notices "Connection to <system> is down|up ..."
(alerts_status.go).

Pins, each with a positive control:
  1. ROUTE — an allow-listed substring forwards with that route's
     severity / owner / runbook rendered (control: the disk route is
     critical + names the runner-disk-triage runbook; the host-down
     route is critical for "Connection to ovh-1 is down").
  2. LONGEST MATCH wins when two substrings both match.
  3. DEFAULT — a Beszel-shaped title with no specific route (e.g. a
     temperature alert, a recovery "below threshold", a "Connection
     to ... is up" notice) uses default_route; a title outside every
     Beszel shape is DROPPED (exit 0, no payload, "dropped" on stderr
     for counting).
  4. CAPS — title truncated to 200 chars, body to min(body_max_chars, 500)
     even when the routing table asks for more.
  5. MENTIONS — allowed_mentions.parse is always empty; an "@everyone"
     body survives as inert text under that suppression.
  6. BODY KEY — Beszel's generic webhook (template=json) posts the text
     under "message" (shoutrrr generic messagekey default); hand-built
     payloads use "body". "body" wins when both are present.
  7. SEND PATH — with a stub curl on PATH, a live send puts the webhook
     URL in the 0600 curl config only, never in argv (control: the stub
     fails the run if the URL appears in argv), pins proto=https, and
     refuses a non-https webhook URL before any send.
  8. REFUSALS — webhook_env other than DISCORD_WEBHOOK_URL, non-JSON
     alert, empty title, and (live mode) unset webhook env each exit 2.

No network: sends go to a stub curl that records its invocation.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FWD = ROOT / "ops/case_f2010cb7/forward_beszel_discord.py"
ROUTING = ROOT / "ops/case_f2010cb7/beszel-discord-routing.json"
FAKE_WEBHOOK = "https://discord.example.invalid/api/webhooks/000/FAKE-not-a-secret"


class ForwarderCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="beszel_fwd_test."))

    def alert_file(self, title, body="body text"):
        p = self.dir / "alert.json"
        p.write_text(json.dumps({"title": title, "body": body}))
        return str(p)

    def message_file(self, title, message):
        p = self.dir / "alert.json"
        p.write_text(json.dumps({"title": title, "message": message}))
        return str(p)

    def dry_run(self, title, body="body text", routing=None):
        args = [sys.executable, str(FWD), "--dry-run"]
        if routing:
            args += ["--routing", str(routing)]
        args.append(self.alert_file(title, body))
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, payload

    def routing_variant(self, fn):
        data = json.loads(ROUTING.read_text())
        fn(data)
        p = self.dir / "routing.json"
        p.write_text(json.dumps(data))
        return p


class TestRouting(ForwarderCase):
    def test_disk_route_renders_severity_owner_runbook(self):
        proc, payload = self.dry_run("ovh-1 disk usage above threshold")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = payload["content"]
        self.assertTrue(content.startswith("[critical] ovh-1 disk"), content)
        self.assertIn("owner: devops", content)
        self.assertIn("runner-disk-triage.md", content)

    def test_cpu_route_is_warning(self):
        proc, payload = self.dry_run("ovh-1 CPU above threshold")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(payload["content"].startswith("[warning] ovh-1 CPU"),
                        payload["content"])

    def test_memory_route_is_warning(self):
        proc, payload = self.dry_run("db-1 memory above threshold")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(payload["content"].startswith("[warning] db-1 memory"),
                        payload["content"])

    def test_host_down_route_is_critical(self):
        proc, payload = self.dry_run("Connection to ovh-1 is down \U0001F534")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = payload["content"]
        self.assertTrue(content.startswith("[critical] Connection to ovh-1 is down"),
                        content)
        self.assertIn("garm-ci-oncall-alert-to-action.md", content)

    def test_longest_match_wins(self):
        def add_generic(d):
            d["routes"].append({"title_contains": "above threshold", "channel": "ops-alerts",
                                "mention": False, "severity": "info", "owner": "x",
                                "runbook": "generic.md"})
        routing = self.routing_variant(add_generic)
        _proc, payload = self.dry_run("ovh-1 disk usage above threshold", routing=routing)
        self.assertTrue(payload["content"].startswith("[critical]"), payload["content"])

    def test_unknown_threshold_uses_default_route(self):
        _proc, payload = self.dry_run("ovh-1 temperature above threshold")
        self.assertTrue(payload["content"].startswith("[warning]"), payload["content"])
        self.assertIn("garm-ci-oncall-alert-to-action.md", payload["content"])

    def test_recovery_below_threshold_uses_default_route(self):
        _proc, payload = self.dry_run("ovh-1 CPU below threshold")
        self.assertTrue(payload["content"].startswith("[warning]"), payload["content"])

    def test_connection_up_uses_default_route(self):
        _proc, payload = self.dry_run("Connection to ovh-1 is up ✅")
        self.assertTrue(payload["content"].startswith("[warning]"), payload["content"])

    def test_foreign_title_is_dropped(self):
        proc, payload = self.dry_run("Grafana: something unrelated")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(payload)
        self.assertIn("dropped", proc.stderr)


class TestCapsAndMentions(ForwarderCase):
    def test_body_capped_at_500_and_title_at_200(self):
        routing = self.routing_variant(lambda d: d.__setitem__("body_max_chars", 5000))
        title = "ovh-1 memory above threshold " + "x" * 400
        _proc, payload = self.dry_run(title, body="y" * 5000, routing=routing)
        lines = payload["content"].split("\n")
        self.assertEqual(len(lines[0]), len("[warning] ") + 200)
        self.assertEqual(len(lines[1]), 500)

    def test_mentions_always_suppressed(self):
        _proc, payload = self.dry_run("Connection to ovh-1 is down \U0001F534",
                                      body="@everyone look")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("@everyone look", payload["content"])


class TestBodyKey(ForwarderCase):
    def dry_run_payload(self, payload_dict):
        p = self.dir / "alert.json"
        p.write_text(json.dumps(payload_dict))
        proc = subprocess.run([sys.executable, str(FWD), "--dry-run", str(p)],
                              capture_output=True, text=True, timeout=30)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, payload

    def test_message_key_renders_body(self):
        proc, payload = self.dry_run_payload(
            {"title": "ovh-1 disk usage above threshold", "message": "disk text"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("disk text", payload["content"])

    def test_body_wins_over_message(self):
        proc, payload = self.dry_run_payload(
            {"title": "ovh-1 disk usage above threshold",
             "body": "body text", "message": "message text"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("body text", payload["content"])
        self.assertNotIn("message text", payload["content"])

    def test_missing_text_renders_empty_body_line(self):
        proc, payload = self.dry_run_payload({"title": "ovh-1 CPU above threshold"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload["content"].split("\n")[1], "")


class TestSendPath(ForwarderCase):
    def test_webhook_url_only_in_curl_config(self):
        record = self.dir / "record"
        stub = self.dir / "curl"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            # invoked as `curl --config FILE`; record argv + config + mode
            f'printf "%s\\n" "$@" > "{record}.argv"\n'
            f'cat "$2" > "{record}.cfg"\n'
            f'stat -c "%a" "$2" > "{record}.mode" 2>/dev/null || stat -f "%Lp" "$2" > "{record}.mode"\n'
            "exit 0\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH=f"{self.dir}:{os.environ['PATH']}",
                   DISCORD_WEBHOOK_URL=FAKE_WEBHOOK)
        proc = subprocess.run(
            [sys.executable, str(FWD),
             self.alert_file("Connection to ovh-1 is down \U0001F534")],
            capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = Path(f"{record}.argv").read_text()
        self.assertNotIn(FAKE_WEBHOOK, argv)
        cfg = Path(f"{record}.cfg").read_text()
        self.assertIn(FAKE_WEBHOOK, cfg)
        self.assertIn('proto = "https"', cfg)
        self.assertEqual(Path(f"{record}.mode").read_text().strip(), "600")


class TestRefusals(ForwarderCase):
    def assert_refusal(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_other_webhook_env_refuses(self):
        routing = self.routing_variant(lambda d: d.__setitem__("webhook_env", "OTHER_HOOK"))
        proc, _ = self.dry_run("ovh-1 CPU above threshold", routing=routing)
        self.assert_refusal(proc)

    def test_missing_title_contains_refuses(self):
        routing = self.routing_variant(lambda d: d["routes"].append(
            {"channel": "ops-alerts"}))
        proc, _ = self.dry_run("ovh-1 CPU above threshold", routing=routing)
        self.assert_refusal(proc)

    def test_empty_title_refuses(self):
        proc, _ = self.dry_run("")
        self.assert_refusal(proc)

    def test_non_json_alert_refuses(self):
        bad = self.dir / "bad.json"
        bad.write_text("not json{{{")
        proc = subprocess.run(
            [sys.executable, str(FWD), "--dry-run", str(bad)],
            capture_output=True, text=True, timeout=30)
        self.assert_refusal(proc)

    def test_unset_webhook_env_refuses_live_send(self):
        env = {k: v for k, v in os.environ.items() if k != "DISCORD_WEBHOOK_URL"}
        proc = subprocess.run(
            [sys.executable, str(FWD), self.alert_file("ovh-1 CPU above threshold")],
            capture_output=True, text=True, timeout=30, env=env)
        self.assert_refusal(proc)

    def test_http_webhook_url_refuses_live_send(self):
        env = dict(os.environ,
                   DISCORD_WEBHOOK_URL="http://discord.example.invalid/hook/FAKE")
        proc = subprocess.run(
            [sys.executable, str(FWD),
             self.alert_file("Connection to ovh-1 is down \U0001F534")],
            capture_output=True, text=True, timeout=30, env=env)
        self.assert_refusal(proc)
        self.assertIn("HTTPS", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
