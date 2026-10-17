#!/usr/bin/env python3
"""Hermetic suite for ops/case_f2010cb7/forward_beszel_discord.py.

Titles below are Beszel's exact alert shapes (henrygd/beszel source,
internal/alerts on main): threshold notices "<system> <alert>
above|below threshold" (alerts_system.go, no trailing emoji; alert names
lowercased except CPU/GPU/CPU*, so "disk usage"/"memory" are lowercase
and "CPU" is not), connection notices "Connection to <system> is
<up|down> <emoji>" (alerts_status.go), "Failed services on <system>
<emoji>" / "Services recovered on <system> <emoji>" (alerts_systemd.go),
"Unhealthy container <c> on <system> 🔴" / "<n> unhealthy containers on
<system> 🔴" / "<system> containers are healthy ✅"
(alerts_container.go), "SMART <state> on <system>: <dev> <emoji>"
(alerts_smart.go), "Storage pool <health> on <system>: <pool>"
(alerts_zfs.go), "Certificate expired|expiring|expiry recovered on
<system>: <name>" and "Network monitor <name> on <system>: <detail>"
(alerts_network_monitors.go).

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
  9. FAMILIES — every other real Beszel family (failed/recovered
     services, unhealthy/healthy containers, SMART, storage pools,
     network monitors, certificates) is Beszel-shaped: failed services
     take the critical route, the rest take default_route. Foreign
     lookalikes ("Database is down", mid-text "… above threshold …"
     prose) are DROPPED even though they embed route substrings: the
     Beszel-shape gate runs before any route, and the host-down route
     additionally requires the "Connection to " head.
 10. RECEIVER — the exact README receiver snippet runs: a POST of
     Beszel's exact {"title", "message"} shape delivers through the
     real forwarder, a dropped title's "dropped" line lands on the
     receiver's stderr (not discarded), and a non-numeric or oversized
     Content-Length is rejected without crashing the server.

No network: sends go to a stub curl that records its invocation, and the
receiver test serves on a loopback ephemeral port.
"""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
import urllib.error
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

    def test_bare_host_down_is_dropped(self):
        # "Database is down" embeds the " is down" route substring but is
        # not Beszel-shaped (no "Connection to " head): the shape gate
        # must drop it before any route matches.
        proc, payload = self.dry_run("Database is down")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(payload)
        self.assertIn("dropped", proc.stderr)

    def test_mid_text_threshold_lookalike_is_dropped(self):
        # Foreign prose embedding route substrings mid-text is not
        # Beszel-shaped: real threshold titles END at "threshold".
        proc, payload = self.dry_run(
            "Grafana note: CPU above threshold and disk usage above "
            "threshold muted, db is down for maintenance")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(payload)
        self.assertIn("dropped", proc.stderr)


class TestAlertFamilies(ForwarderCase):
    """Every other real Beszel family (pins: exact upstream strings)."""

    def assert_default_warning(self, title):
        _proc, payload = self.dry_run(title)
        self.assertIsNotNone(payload, f"dropped real Beszel title: {title}")
        self.assertTrue(payload["content"].startswith("[warning]"),
                        payload["content"])
        self.assertIn("garm-ci-oncall-alert-to-action.md", payload["content"])

    def test_failed_services_route_is_critical(self):
        proc, payload = self.dry_run("Failed services on ovh-1 \U0001F534")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = payload["content"]
        self.assertTrue(content.startswith("[critical] Failed services on ovh-1"),
                        content)
        self.assertIn("garm-ci-oncall-alert-to-action.md", content)

    def test_services_recovered_uses_default_route(self):
        self.assert_default_warning("Services recovered on ovh-1 ✅")

    def test_unhealthy_container_uses_default_route(self):
        self.assert_default_warning("Unhealthy container web on ovh-1 \U0001F534")

    def test_multiple_unhealthy_containers_use_default_route(self):
        self.assert_default_warning("3 unhealthy containers on ovh-1 \U0001F534")

    def test_healthy_containers_use_default_route(self):
        self.assert_default_warning("db-1 containers are healthy ✅")

    def test_smart_uses_default_route(self):
        self.assert_default_warning("SMART failed on ovh-1: sda \U0001F534")

    def test_storage_pool_uses_default_route(self):
        self.assert_default_warning("Storage pool degraded on ovh-1: tank")

    def test_network_monitor_uses_default_route(self):
        self.assert_default_warning(
            "Network monitor ping on ovh-1: host unreachable")

    def test_certificate_uses_default_route(self):
        self.assert_default_warning(
            "Certificate expiring on ovh-1: example.com")


class TestCapsAndMentions(ForwarderCase):
    def test_body_capped_at_500_and_title_at_200(self):
        routing = self.routing_variant(lambda d: d.__setitem__("body_max_chars", 5000))
        # Padding goes in the system slot: a real threshold title still
        # ENDS at "threshold", so over-long tails must not route.
        title = "ovh-1-" + "x" * 180 + " memory above threshold"
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


class TestReceiverDocumentedSnippet(ForwarderCase):
    """The README receiver is the documented install path: the EXACT
    snippet from the doc must run, deliver Beszel's exact
    {"title", "message"} shape through the real forwarder, keep the
    "dropped" line on stderr, and reject bad Content-Length values
    without dying."""

    README = ROOT / "ops/case_f2010cb7/README.md"

    def receiver_code(self, port):
        blocks = [b for b in re.findall(r"```python\n(.*?)```",
                                        self.README.read_text(), re.DOTALL)
                  if "beszel_discord_receiver.py" in b]
        self.assertEqual(len(blocks), 1,
                         "README must carry exactly one receiver snippet")
        code = textwrap.dedent(blocks[0])
        self.assertIn("ThreadingHTTPServer", code)
        self.assertIn("forward_beszel_discord.py", code)
        code = code.replace("/opt/beszel-bridge/forward_beszel_discord.py",
                            str(FWD))
        self.assertIn('("127.0.0.1", 8099)', code)
        return code.replace('("127.0.0.1", 8099)',
                            f'("127.0.0.1", {port})')

    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def start_receiver(self):
        """Stub curl + fake webhook env; returns (proc, port)."""
        stub = self.dir / "curl"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "sent %s\\n" "$@" >> "{self.dir}/curl.log"\n'
            "exit 0\n")
        stub.chmod(0o755)
        port = self.free_port()
        (self.dir / "receiver.py").write_text(self.receiver_code(port))
        env = dict(os.environ, PATH=f"{self.dir}:{os.environ['PATH']}",
                   DISCORD_WEBHOOK_URL=FAKE_WEBHOOK)
        proc = subprocess.Popen(
            [sys.executable, str(self.dir / "receiver.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        base = f"http://127.0.0.1:{port}/hook"
        deadline = time.time() + 10
        while True:
            try:
                urllib.request.urlopen(base, timeout=2)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 501, "receiver answers GET with 501")
                return proc, base
            except OSError:
                pass
            if time.time() > deadline:
                self.finish(proc)
                self.fail("receiver did not start serving")
            time.sleep(0.1)

    def finish(self, proc):
        """Terminate the receiver; return its stderr bytes."""
        if proc.poll() is None:
            proc.terminate()
        try:
            _, errs = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, errs = proc.communicate(timeout=15)
        return errs or b""

    def post_json(self, base, payload):
        req = urllib.request.Request(
            base, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status

    def test_exact_beszel_shape_delivers(self):
        proc, base = self.start_receiver()
        try:
            status = self.post_json(
                base, {"title": "ovh-1 CPU above threshold",
                       "message": "cpu text"})
            self.assertEqual(status, 200)
            log = (self.dir / "curl.log").read_text() \
                if (self.dir / "curl.log").exists() else ""
            self.assertIn("sent", log, "receiver must drive a forwarder send")
        finally:
            self.finish(proc)

    def test_drop_surfaces_on_receiver_stderr(self):
        proc, base = self.start_receiver()
        try:
            status = self.post_json(
                base, {"title": "Grafana: something unrelated", "message": "x"})
            self.assertEqual(status, 200)
        finally:
            errs = self.finish(proc)
        self.assertIn(b"dropped", errs,
                      "the forwarder's dropped line must reach receiver stderr")

    def test_bad_content_length_rejected_server_survives(self):
        proc, base = self.start_receiver()
        port = int(base.rsplit(":", 1)[1].split("/", 1)[0])
        try:
            for raw_len, want in ((b"many", 400), (b"99999999", 413),
                                  (b"-5", 413)):
                s = socket.create_connection(("127.0.0.1", port), timeout=10)
                try:
                    s.sendall(b"POST /hook HTTP/1.1\r\nHost: x\r\nContent-Length: "
                              + raw_len + b"\r\nConnection: close\r\n\r\n{}")
                    data = s.recv(1024)
                finally:
                    s.close()
                self.assertIn(f"HTTP/1.0 {want}".encode(), data,
                              f"Content-Length {raw_len!r} must yield {want}")
            # Server still alive afterwards: a good POST delivers.
            status = self.post_json(
                base, {"title": "ovh-1 CPU above threshold", "message": "ok"})
            self.assertEqual(status, 200)
        finally:
            self.finish(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
