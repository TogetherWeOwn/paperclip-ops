# host-ops-mcp install runbook (case_16723128)

One human host window, as root on the Paperclip host. This document exists so
the window is complete on the first trip: it names the exact pin, the exact
commands, and the exact post-install checks that answer case_16723128's acceptance
criterion — an agent calling the MCP tool from a normal run gets a verified
signed response for a read verb, with the request visible in the audit chain.

## Why this needs a human, not an agent

Measured and recorded on case_16723128: agent containers share uid 1000, the agent
spawner resets supplementary groups (so the gid-986 spool mount is unreachable
from any agent process), and company policy is that an SSH key or host bearer
token must never be placed in an agent container. Installing a host service,
writing root-owned Caddy and systemd state, and minting the shared bearer are
all host acts. The operator installs and enables; the agent built and tested.

This install spends no money, changes no org structure, publishes no new
external surface beyond one more handle on the established site (no new
hostname, no DNS change — case_96d4d8b5 preflight decision, §4), and —
critically — enables NO new authority: the broker stays
Phase-0 read-only, and this transport allowlists three of its read verbs. It
is not an owner-reserved decision; it only requires hands that can reach the
host.

## 0. Prerequisites (already true — verify, do not rebuild)

- `paperclip-host-ops-broker.service` installed, ACTIVE, and polling
  (`systemctl status paperclip-host-ops-broker`). The broker owns
  `/var/spool/paperclip-host-ops`, `/var/log/paperclip-host-ops/audit.jsonl`
  and the response-signing key.
- The independently recorded SHA-256 of the response-signing PUBLIC key (from
  the broker install window — case_954f2dc1). This runbook never trusts a hash read
  from the same host without that independent record.
- This host's public egress as Caddy sees it after de-proxying
  (192.0.2.239 on this host; re-check with the operator if the host's
  egress ever changes). The old podman-container-IP prerequisite is gone:
  behind Cloudflare Caddy never sees the container peer, so a container-IP
  allowlist would 403 every real call (case_96d4d8b5 preflight).
- A reviewed commit of paperclip-ops containing `mcp/host-ops-mcp.mjs`
  (this runbook: `case_16723128-PIN` below).

## 1. The pin

```
repo:   https://github.com/TogetherWeOwn/paperclip-ops
commit: 988ac6c23948d273889ccd8837f86ec55bd6ac64  (merge of PR #724 on main)
files:  mcp/host-ops-mcp.mjs  mcp/test/host-ops-mcp.test.mjs  mcp/deploy/host-ops-*
review: https://github.com/TogetherWeOwn/paperclip-ops/pull/724
        mcp/ is byte-identical between reviewed head
        07fa132247a8f5df6e7c26b8d339e82ae1ddaf0e and the merge commit
```

Verify before trusting anything else in this document:

```sh
git clone https://github.com/TogetherWeOwn/paperclip-ops.git
cd paperclip-ops
git checkout 988ac6c23948d273889ccd8837f86ec55bd6ac64
git fetch origin pull/724/head   # brings in the reviewed head commit
git diff --quiet 07fa132247a8f5df6e7c26b8d339e82ae1ddaf0e HEAD -- mcp/ \
  && echo "transport bytes = reviewed bytes"
(cd mcp && node --test test/host-ops-mcp.test.mjs)   # 38/38, incl. real-broker e2e
git rev-parse HEAD   # record it in the install log
```

Do not install from an unreviewed tip. The transport is an identity boundary;
the bytes behind Caddy must be the bytes the reviewer approved.

## 2. Lay down the transport bytes

As root. The transport is one file with zero dependencies — no npm tree next
to the operator account:

```sh
install -o OPERATOR_USER -g OPERATOR_GROUP -m 0755 -d /opt/host-ops-mcp
install -o OPERATOR_USER -g OPERATOR_GROUP -m 0644 \
  mcp/host-ops-mcp.mjs /opt/host-ops-mcp/host-ops-mcp.mjs
node --check --input-type=module < /opt/host-ops-mcp/host-ops-mcp.mjs \
  && echo "transport parses on the host node"
```

(`OPERATOR_USER` is the account that owns the broker spool group membership —
the same user org-request-mcp runs as. It needs `paperclip-host-requests`
group membership to write `requests/` and read `responses/`.)

## 3. Mint the bearer and write the transport config

```sh
bearer=$(openssl rand -hex 32)
printf '%s' "$bearer" | sha256sum   # -> bearerSha256, next step
```

Write `/etc/host-ops-mcp/config.json` (root-owned, mode 0600) from
`mcp/deploy/host-ops-config.example.json`, substituting:

- `bearerSha256` with the digest just printed;
- `expectedPublicKeySha256` with the INDEPENDENTLY RECORDED public-key hash
  (prerequisite 0) — then confirm the file on disk hashes to it:
  `sha256sum /var/lib/paperclip-host-ops/keys/response-signing.pub.pem`;
- `brokerSpool` / `brokerConfig` only if this host diverges from the example
  (they must name the LIVE broker paths — `systemctl cat
  paperclip-host-ops-broker` shows what the service actually reads).

```sh
install -o OPERATOR_USER -g OPERATOR_GROUP -m 0600 /path/to/config.json /etc/host-ops-mcp/config.json
```

The bearer itself goes to exactly two places (see the example file's header):
Caddy's unit environment (step 4) and the Paperclip secret referenced by the
tool connection (step 6). `unset bearer` when both are done. Nowhere else.

## 4. Caddy: handle (not a new hostname), bearer, source restriction

DECISION (case_96d4d8b5 preflight, 2026-09-26): a `/…` handle on the existing
site, NOT a new hostname. The live org-request-mcp transport on this host
is a `handle` under the existing `host-48c35084.example.invalid` site (host
`/etc/caddy/Caddyfile:23-29`), guarded by
`not client_ip 192.0.2.239 127.0.0.1`. Host-ops deploys the same way —
a second handle (e.g. `/host-ops/mcp`, stripped to `/mcp` upstream with
`handle_path`) under that site — so there is NO new hostname and NO DNS
record to create. One path per transport keeps the bearers useless at each
other's endpoint exactly as one hostname per transport would: a host-ops
bearer presented at the org-request handle (or vice versa) is a 401 at a
Caddy layer that never reaches either origin.

WHY `client_ip` AND NOT `remote_ip`: this host sets a global Cloudflare
`trusted_proxies` (host `/etc/caddy/Caddyfile:11`) and every public
hostname arrives through Cloudflare, so by the time Caddy evaluates the
matcher `remote_ip` is a Cloudflare edge peer — never the gateway. The
merged snippet's `remote_ip CONTAINER_EGRESS 127.0.0.1` would therefore
403 every real gateway call. `client_ip` is the client address restored
through the already-trusted Cloudflare proxies, which is exactly what the
working org-request transport matches. Keep the trust where it already
is: do NOT add a per-handle `trusted_proxies`, and do NOT reintroduce
`remote_ip`.

1. Inside the existing `host-48c35084.example.invalid` site block, next to the
   org-request `handle`, add the host-ops handle from
   `mcp/deploy/host-ops-Caddyfile.snippet` §§1-5 with `HOST_EGRESS_IP`
   (192.0.2.239 on this host) substituted — e.g.:
   ```caddy
   handle_path /host-ops/* {
       # handle_path strips /host-ops before the inner matchers run, so
       # /host-ops/mcp reaches the origin as exactly /mcp (which is all
       # host-ops-mcp.mjs answers — any other upstream path is its 404).
       # Do NOT use plain `handle` without the strip (unstripped
       # /host-ops/mcp would die as the origin's 404 on every call), and
       # do NOT use `handle_path /host-ops/mcp*` (that strips the full
       # matched prefix and leaves `/`, 404ing every call at §1).
       # §§1-3 of the snippet, paths as written:
       @notmcp not path /mcp
       respond @notmcp 404
       @notlocal not client_ip 192.0.2.239 127.0.0.1
       respond @notlocal 403
       @unauthorized not header Authorization "Bearer {$HOST_OPS_MCP_BEARER}"
       respond @unauthorized 401 {
           close
       }
       # §§4-5: reverse_proxy 127.0.0.1:8392 + log (own log file)
       reverse_proxy 127.0.0.1:8392 {
           header_up -X-Forwarded-For-Original
           header_up X-Real-IP {remote_host}
           transport http {
               dial_timeout 5s
               response_header_timeout 130s
           }
       }
   }
   ```
   (The snippet file itself stays a standalone-site template for
   portability; on THIS host its §§1-5 are what get deployed as the
   handle above. In `mcp/deploy/host-ops-tool-connection.example.json`
   (`"url": "https://MCP_HOSTNAME/mcp"`) substitute MCP_HOSTNAME with
   the existing site name `host-48c35084.example.invalid` AND change the path
   to the handle — `"url": "https://host-48c35084.example.invalid/host-ops/mcp"`
   on this host. No new hostname, no DNS change.)
2. `printf 'HOST_OPS_MCP_BEARER=%s\n' "$bearer" | sudo tee /etc/caddy/host-ops-mcp.env`,
   `chmod 600`, `chown root:root`. A DIFFERENT bearer from org-request-mcp's:
   one credential per transport, so rotating or revoking one never moves
   the other.
3. `systemctl edit caddy` — add `EnvironmentFile=/etc/caddy/host-ops-mcp.env`.
4. `systemctl restart caddy` (RESTART, not reload — a reload does not re-read
   the unit environment), then `caddy validate` / check `systemctl status caddy`.

If a future host is NOT behind Cloudflare (no `trusted_proxies` in front of
Caddy), the handle shape stays but §2 reverts to
`not remote_ip <egress> 127.0.0.1` — `client_ip` without a trusted proxy
would be spoofable via `X-Forwarded-For`. That exception does not apply to
this host.

## 5. Enable and start the transport

```sh
install -m 0644 mcp/deploy/host-ops-mcp.service /etc/systemd/system/host-ops-mcp.service
# edit User=/Group= to OPERATOR_USER/OPERATOR_GROUP if they differ
systemctl daemon-reload
systemctl enable --now host-ops-mcp
journalctl -u host-ops-mcp -n 20   # expect the `listening` line on 127.0.0.1:8392
```

Refusal preflight (do these before step 6 — each is a two-minute check that
saves a misregistration). `MCP_URL` is the full public path from §4
(`https://host-48c35084.example.invalid/host-ops/mcp` on this host):

```sh
# 1. tools/list answers anonymously (gateway health check shape):
curl -sk -H "Authorization: Bearer $bearer" "$MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | grep -o 'inspect_[a-z]*\|locate_tooling'
# expect: inspect_health inspect_service locate_tooling

# 2. tools/call without identity fails CLOSED (the forward-list backstop):
curl -sk -H "Authorization: Bearer $bearer" "$MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"inspect_service","arguments":{}}}' \
  | grep -o 'identity_header_missing'
# expect: identity_header_missing — NOT a broker call, NOT a signed bundle

# 3. The source restriction is real: from any box that is NOT this host
#    (so its client_ip is neither 192.0.2.239 nor loopback), the same
#    call is a 403 at Caddy — never 401, never 200:
curl -sk -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $bearer" "$MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# expect: 403 (run off-host; on-host the call egresses as 192.0.2.239 and passes)
```

## 6. Register the tool connection

Create the Paperclip secret holding the bearer, then register
`mcp/deploy/host-ops-tool-connection.example.json` with the full public
path — `"url": "https://host-48c35084.example.invalid/host-ops/mcp"` on this
host (MCP_HOSTNAME = existing site name, path = handle; no new hostname)
— and the
secret id substituted. Confirm the registered connection carries
`config.headerPolicy.metadata.forward` =
`["agent_id", "company_id", "run_id", "issue_id", "correlation_id"]` —
`"issue_id"` is the one this transport adds over the org-request template, and
without it every call fails closed.

## 7. Acceptance (answers case_16723128's Done)

From a NORMAL agent run (not from this host window), call `inspect_service`.
Expect:

1. A `succeeded` response with `verb: service.inspect`, a `result` with
   `Status`/`Running`/`Pid` (the `docker inspect --format '{{json .State}}'`
   shape on node7b1ff338: `Status`, `Running`, `Pid`, `StartedAt`, `FinishedAt`,
   `ExitCode`, `OOMKilled`, `Restarting`), and an `audit` block with
   `sequence` and `entryHash`;
2. The request visible in the host audit chain:
   `grep <requestId> /var/log/paperclip-host-ops/audit.jsonl` shows
   `request.accepted` with the calling agent/run/issue, then
   `execution.succeeded`;
3. The response bundle verifiable with the pinned key:
   `python3 host-ops/broker.py verify --public-key <pinned> \
     --expected-key-sha256 <recorded> --response <bundle>.json \
     --signature <bundle>.sig`.

Then close the loop on the card: the agent run that performed the acceptance
posts its evidence, and case_16723128 moves to `done` (= merged transport +
operator-confirmed live call, evidence link registered as a work product).

## Rollback

```sh
systemctl disable --now host-ops-mcp
rm /etc/systemd/system/host-ops-mcp.service /etc/host-ops-mcp/config.json
# Caddy: remove the `handle_path /host-ops/*` block from the host-48c35084.example.invalid
# site block (NOT a standalone site block), restart caddy
# Paperclip: quarantine or delete the tool connection
```

The broker is untouched by this install and keeps polling either way; rolling
back the transport returns the board to exactly the pre-case_16723128 state (spool
unreachable, zero requests) with nothing half-installed behind it.
