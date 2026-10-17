#!/usr/bin/env python3
"""forward_beszel_discord.py — Beszel generic-webhook alert -> ops Discord.

Consumer for beszel-discord-routing.json. Beszel's generic webhook (with
template=json) posts {"title": ..., "message": ...}; the forwarder also
accepts {"title": ..., "body": ...} for hand-built payloads. That shape
matches no other adapter we run, so host alerts ride the EXISTING ops
Discord webhook. This forwarder is the only thing that reads the routing
table:

  - only allow-listed title substrings route (longest wins). The
    substrings match Beszel's real alert titles, which always lead with
    the system name: "<system> <alert> above|below threshold"
    (internal/alerts/alerts_system.go, e.g. "ovh-1 disk usage above
    threshold", "ovh-1 CPU above threshold") and "Connection to
    <system> is down" (alerts_status.go). A Beszel-shaped title with no
    specific route takes the default route, so a future Beszel title
    change still forwards instead of vanishing; anything else is
    dropped without a send (exit 0, with a "dropped" line on stderr for
    counting) — alert text is untrusted input and must never become a
    free-form Discord post.
  - title is capped at 200 chars, body at min(body_max_chars, 500).
  - the message renders severity/owner/runbook from the route; Discord
    mentions are always suppressed (allowed_mentions.parse = []), so a
    route's "mention" flag cannot ping anyone from untrusted text —
    there is no mention target in the routing table to ping.
  - the webhook URL comes only from the env var the routing table
    names, and the only accepted name is DISCORD_WEBHOOK_URL (the
    existing secret; no new credential). It must be https and travels
    in a 0600 curl config file, never argv (/proc/<pid>/cmdline is
    world-readable).

Usage: forward_beszel_discord.py [--routing FILE] [--dry-run] [ALERT.json]
  ALERT.json omitted = read the alert from stdin.
  --dry-run prints the exact Discord payload instead of sending.
Exit: 0 sent (or dropped by policy, or dry-run); 1 send failed;
      2 refusal (bad routing table, bad alert, missing webhook env)
      before any send.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

TITLE_CAP = 200
BODY_HARD_CAP = 500
ONLY_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def die(msg):
    print(f"forward_beszel_discord: refusing — {msg}", file=sys.stderr)
    sys.exit(2)


def load_routing(path: Path):
    try:
        routing = json.loads(path.read_text())
    except (OSError, ValueError):
        die(f"routing table {path} is not readable JSON")
    if not isinstance(routing, dict):
        die("routing table is not an object")
    if routing.get("webhook_env") != ONLY_WEBHOOK_ENV:
        die(f"webhook_env must be {ONLY_WEBHOOK_ENV} (the existing secret), "
            f"got {routing.get('webhook_env')!r}")
    cap = routing.get("body_max_chars")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        die("body_max_chars must be a positive integer")
    routes = routing.get("routes")
    if not isinstance(routes, list) or not routes:
        die("routes must be a non-empty list")
    for route in routes:
        if not isinstance(route, dict) or not isinstance(route.get("title_contains"), str) \
                or not route["title_contains"]:
            die("every route needs a non-empty title_contains")
    default = routing.get("default_route")
    if default is not None and not isinstance(default, dict):
        die("default_route must be an object when present")
    return routing, min(cap, BODY_HARD_CAP)


def looks_like_beszel(title):
    """True for Beszel's real title shapes: threshold notices
    ("<system> <alert> above|below threshold") and connection notices
    ("Connection to <system> is ...")."""
    return " threshold" in title or title.startswith("Connection to ")


def pick_route(routing, title):
    matches = [r for r in routing["routes"] if r["title_contains"] in title]
    if matches:
        return max(matches, key=lambda r: len(r["title_contains"]))
    if looks_like_beszel(title):
        return routing.get("default_route")
    return None


def render(route, title, body, cap):
    severity = route.get("severity") if isinstance(route.get("severity"), str) else "warning"
    owner = route.get("owner") if isinstance(route.get("owner"), str) else "devops"
    runbook = route.get("runbook") if isinstance(route.get("runbook"), str) else ""
    lines = [f"[{severity}] {title[:TITLE_CAP]}", body[:cap],
             f"owner: {owner}" + (f" · runbook: {runbook}" if runbook else "")]
    return {"content": "\n".join(lines), "allowed_mentions": {"parse": []}}


def send(webhook_url, payload):
    parts = urlsplit(webhook_url)
    if parts.scheme.lower() != "https" or not parts.netloc:
        die("DISCORD_WEBHOOK_URL must be an HTTPS URL")
    with tempfile.TemporaryDirectory(prefix="beszel_discord.") as tmp:
        os.chmod(tmp, 0o700)
        data = Path(tmp) / "payload.json"
        cfg = Path(tmp) / "curlcfg"
        data.write_text(json.dumps(payload))
        cfg.write_text(
            f'url = "{webhook_url}"\n'
            'header = "Content-Type: application/json"\n'
            f'data-binary = "@{data}"\n'
            'max-time = "15"\nproto = "https"\nsilent\nshow-error\nfail\n')
        os.chmod(cfg, 0o600)
        proc = subprocess.run(["curl", "--config", str(cfg)],
                              capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print(f"forward_beszel_discord: send failed (curl exit {proc.returncode})",
              file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="Beszel alert -> Discord forwarder")
    here = Path(__file__).resolve().parent
    ap.add_argument("--routing", default=str(here / "beszel-discord-routing.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("alert", nargs="?", help="alert JSON file (default: stdin)")
    args = ap.parse_args()
    routing, cap = load_routing(Path(args.routing))
    try:
        raw = Path(args.alert).read_text() if args.alert else sys.stdin.read()
        alert = json.loads(raw)
    except (OSError, ValueError):
        die("alert payload is not readable JSON")
    if not isinstance(alert, dict) or not isinstance(alert.get("title"), str) or not alert["title"]:
        die("alert needs a non-empty string title")
    body = alert.get("body")
    if not isinstance(body, str):
        # Beszel's generic webhook (template=json) posts the text under
        # "message" (the shoutrrr generic messagekey default); hand-built
        # payloads use "body".
        body = alert.get("message") if isinstance(alert.get("message"), str) else ""
    route = pick_route(routing, alert["title"])
    if route is None:
        print("forward_beszel_discord: dropped — no allow-listed title match",
              file=sys.stderr)
        return 0
    payload = render(route, alert["title"], body, cap)
    if args.dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    webhook = os.environ.get(ONLY_WEBHOOK_ENV, "")
    if not webhook:
        die(f"{ONLY_WEBHOOK_ENV} is unset")
    return send(webhook, payload)


if __name__ == "__main__":
    sys.exit(main())
