#!/usr/bin/env python3
"""Hermetic suite for ops/case_f2010cb7/forward_beszel_discord.py.

Pins, each with a positive control:
  1. ROUTE — an allow-listed prefix forwards with that route's severity /
     owner / runbook rendered (control: disk route is critical + names
     the runner-disk-triage runbook).
  2. LONGEST PREFIX wins when two prefixes both match.
  3. DEFAULT — a "Beszel:" title with no route prefix uses default_route;
     a title outside the Beszel namespace is DROPPED (exit 0, no payload).
  4. CAPS — title truncated to 200 chars, body to min(body_max_chars, 500)
     even when the routing table asks for more.
  5. MENTIONS — allowed_mentions.parse is always empty; an "@everyone"
     body survives as inert text under that suppression.
  6. SEND PATH — with a stub curl on PATH, a live send puts the webhook
     URL in the 0600 curl config only, never in argv (control: the stub
     fails the run if the URL appears in argv).
  7. REFUSALS — webhook_env other than DISCORD_WEBHOOK_URL, non-JSON
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
        proc, payload = self.dry_run("Beszel: disk usage above 90% on node7b1ff338")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = payload["content"]
        self.assertTrue(content.startswith("[critical] Beszel: disk"), content)
        self.assertIn("owner: devops", content)
        self.assertIn("runner-disk-triage.md", content)

    def test_longest_prefix_wins(self):
        def add_generic(d):
            d["routes"].append({"title_prefix": "Beszel:", "channel": "ops-alerts",
                                "mention": False, "severity": "info", "owner": "x",
                                "runbook": "generic.md"})
        routing = self.routing_variant(add_generic)
        _proc, payload = self.dry_run("Beszel: disk almost full", routing=routing)
        self.assertTrue(payload["content"].startswith("[critical]"), payload["content"])

    def test_unknown_beszel_title_uses_default_route(self):
        _proc, payload = self.dry_run("Beszel: network flap on ovh-1")
        self.assertTrue(payload["content"].startswith("[warning]"), payload["content"])
        self.assertIn("garm-ci-oncall-alert-to-action.md", payload["content"])

    def test_foreign_title_is_dropped(self):
        proc, payload = self.dry_run("Grafana: something unrelated")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(payload)
        self.assertIn("dropped", proc.stderr)


class TestCapsAndMentions(ForwarderCase):
    def test_body_capped_at_500_and_title_at_200(self):
        routing = self.routing_variant(lambda d: d.__setitem__("body_max_chars", 5000))
        title = "Beszel: cpu " + "x" * 400
        _proc, payload = self.dry_run(title, body="y" * 5000, routing=routing)
        lines = payload["content"].split("\n")
        self.assertEqual(len(lines[0]), len("[warning] ") + 200)
        self.assertEqual(len(lines[1]), 500)

    def test_mentions_always_suppressed(self):
        _proc, payload = self.dry_run("Beszel: host down ovh-1", body="@everyone look")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("@everyone look", payload["content"])


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
            [sys.executable, str(FWD), self.alert_file("Beszel: host down ovh-1")],
            capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = Path(f"{record}.argv").read_text()
        self.assertNotIn(FAKE_WEBHOOK, argv)
        self.assertIn(FAKE_WEBHOOK, Path(f"{record}.cfg").read_text())
        self.assertEqual(Path(f"{record}.mode").read_text().strip(), "600")


class TestRefusals(ForwarderCase):
    def assert_refusal(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_other_webhook_env_refuses(self):
        routing = self.routing_variant(lambda d: d.__setitem__("webhook_env", "OTHER_HOOK"))
        proc, _ = self.dry_run("Beszel: cpu high", routing=routing)
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
            [sys.executable, str(FWD), self.alert_file("Beszel: cpu high")],
            capture_output=True, text=True, timeout=30, env=env)
        self.assert_refusal(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
