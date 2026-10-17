# Beszel → Gatus/Discord bridge (case_f2010cb7)

Per-host Beszel health into Gatus external endpoints, plus Beszel alert
forwarding into ops Discord. Source-only: no host deploy rides a change
here; an operator installs the cron lines separately.

## Files

- `push-beszel-hosts.sh` — cron (`*/5 * * * *`) consumer. Evaluates every
  enrolled host (disk %, CPU %, memory %, load15-per-cpu, failed systemd
  units, linear disk-growth forecast; inode % and container restarts only
  when a producer supplies them) and pushes one Gatus external endpoint
  per host (`beszel_<host>`) through `../case_658bf6b9/_gatus_push.sh`.
- `beszel_adapter.py` — Beszel-native (PocketBase) → normalized producer.
  Reads `systems.json`, `stats-latest.json`, `stats-history.json` from a
  directory and prints the normalized array the consumer documents.
  Provenance (henrygd/beszel, verified 2026-10-17): `failed_units` ←
  `systems.info.sv[1]`, `disk_history` ← coarse `system_stats` (GiB =
  bytes / 2³⁰), `load15_per_cpu` ← `la[2] / (threads or cores)`.
  `inode_used_percent` and `container_restarts_24h` are never emitted —
  Beszel records neither, so any value would be invented. Down or stale
  (>600 s) hosts are emitted name-only so the consumer fails them closed.
- `fetch-beszel-normalized.sh` — live fetch half: hub
  `auth-with-password`, systems + newest 1m stats per system + 480m
  history, then the adapter. Password and token travel in 0600 curl
  config/data files inside a 0700 temp dir, never argv. The hub must be
  https unless loopback, and a hub token outside the tight
  `[A-Za-z0-9._~+/=-]` charset is refused before any fetch (a quote
  plus a newline would inject curl directives). Wire as
  `BESZEL_SOURCE_CMD` for the consumer.
- `forward_beszel_discord.py` — consumer for
  `beszel-discord-routing.json`. Beszel generic-webhook alerts (with
  `template=json`: `{title, message}`; hand-built `{title, body}`
  payloads also accepted, `body` wins when both are present) forward to
  the existing `${DISCORD_WEBHOOK_URL}` webhook — https only — when an
  allow-listed title substring matches (longest wins; other
  Beszel-shaped titles take the default route; everything else is
  dropped with a `dropped` line on stderr for counting). Title capped
  at 200 chars, body at min(body_max_chars, 500), mentions always
  suppressed, webhook URL only in a 0600 curl config.
- `beszel-discord-routing.json` — the routing table (allow-listed
  title substrings matching Beszel's real `<system> <alert>
  above|below threshold` and `Connection to <system> is down` titles,
  per-route severity/owner/runbook).
- `fixtures/` — normalized payloads for the bridge suite;
  `fixtures/native/` — Beszel-native payloads for the adapter suite.

## Tests

- `test_beszel_gatus_bridge.sh` (repo root) — consumer contract incl.
  optional-field shrink (§13) and adapter→consumer end-to-end.
- `test_beszel_adapter.py`, `test_beszel_discord_forwarder.py` (repo
  root) — producer suites. The forwarder suite uses Beszel's exact
  alert-title shapes, pins the `message`-key fallback, and pins
  https-only webhook delivery.
- `test_fetch_beszel_normalized.sh` (repo root) — fetch-wrapper suite:
  stub-curl fake hub for the happy path, password/token argv hygiene,
  hostile-token and remote-http refusals. All hermetic: fixtures +
  stub curl, no network, no credentials.

## Operator install (separate path, not a code change)

1. Root-only 0600 env file: `BESZEL_HUB_URL`, `BESZEL_IDENTITY`,
   `BESZEL_PASSWORD` (read-only hub user), `GATUS_URL`,
   `GATUS_EXTERNAL_TOKEN`, `BESZEL_HOSTS`, `DISCORD_WEBHOOK_URL`
   (existing secret — do not create a new one).
2. Cron: `*/5 * * * *` run `push-beszel-hosts.sh` with
   `BESZEL_SOURCE_CMD=fetch-beszel-normalized.sh`; append
   `push-beszel-hosts.sh --emit-gatus-yaml` stanzas to the Gatus config.
3. Beszel hub → Discord forwarding. The forwarder is a CLI (file
   argument or stdin) — it starts no listener — so Beszel's generic
   webhook points at a minimal receiver on the same host that pipes
   each POST body to it. Example stdlib receiver (binds loopback only;
   run it under the operator's process supervision with
   `DISCORD_WEBHOOK_URL` in its environment):
   ```python
   # beszel_discord_receiver.py — POST body -> forward_beszel_discord.py stdin
   import subprocess, sys
   from http.server import BaseHTTPRequestHandler, HTTPServer
   FWD = "/opt/beszel-bridge/forward_beszel_discord.py"
   class H(BaseHTTPRequestHandler):
       def do_POST(self):
           n = int(self.headers.get("Content-Length", 0))
           body = self.rfile.read(n) if n else b"{}"
           p = subprocess.run([sys.executable, FWD], input=body,
                              capture_output=True, timeout=60)
           self.send_response(200 if p.returncode == 0 else 500)
           self.end_headers()
       def log_message(self, *a):  # drops go to the forwarder's stderr
           pass
   HTTPServer(("127.0.0.1", 8099), H).serve_forever()
   ```
   Beszel notification URL (shoutrrr `generic://`; `template=json`
   is what produces the `{title, message}` JSON payload — without it
   there is no title key — and `messagekey` defaults to `message`,
   which the forwarder reads):
   `generic://127.0.0.1:8099/hook?template=json`. Keep
   `titlekey=title&messagekey=message` explicit if the hub UI ever
   pre-fills other keys.

## Fail-closed rules

Missing field, unreadable payload, empty host list, silent enrolled
host, down/stale host, or missing credentials all push `success=false`
(or refuse with exit 2 before any health claim). Nothing here reports
healthy on missing data, and nothing invents a metric Beszel does not
record.
