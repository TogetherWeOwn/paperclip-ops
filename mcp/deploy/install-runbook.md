# case_d5cbfb18 — install the host-side MCP transport

**From:** CISO · **Date:** 2026-08-24 · **Epic:** case_f70516a1
**Canonical path:** `mcp/deploy/install-runbook.md` in `paperclip-ops`, on `main`
**Approval:** Chief of Staff, 2026-08-24, under `board_or_agents` — Caddy on a public hostname, six conditions. All six are implemented; step 9 verifies them.

> **Check that you are reading the real one.** (case_5bda0b5a)
>
> ```sh
> git -C <checkout> fetch origin
> git -C <checkout> diff --exit-code origin/main -- mcp/deploy/install-runbook.md   # expect: no output
> ```
>
> This document is the *instructions* for a root install, so it is worth as much
> to an attacker as the scripts it tells you to run — it names which files to
> trust, which assertions count as a pass, and when to stop. Until 2026-08-25 it
> existed **only** in `/paperclip/operator-handoff/`, which is mode `1777` and
> where every file is owned by the shared agent uid `node`: any agent on this box
> could have rewritten a step between staging and execution, and there was
> nothing to diff it against. It had already been silently wrong once (the
> `/etc/caddy/org-request-mcp.bearer` row in step 2, corrected under case_0409633c).
>
> So the same rule the rest of this repo applies to staged scripts now applies to
> this runbook: **git is the source, the handoff channel is a pointer.** The file
> left at `/paperclip/operator-handoff/case_d5cbfb18-mcp-install-runbook.md` is a
> signpost to this path and nothing else; `./channel_drift.sh check --ref
> origin/main` fails if its bytes stop matching the committed pointer. If you are
> holding a copy of this runbook that did *not* come out of `git`, the diff above
> is the only thing that makes it trustworthy — run it before step 1.

This runbook needs the operator. Every step below is on the **host**, as the
operator user, and nothing in it can be done from inside an agent container.

> **Before you start, please delete the earlier identity probe.**
> `case_d5cbfb18-identity-probe.mjs` and `case_d5cbfb18-identity-probe-runbook.md` in
> `/paperclip/operator-handoff/` should not be run. Registering the probe requires *publishing* it,
> and the question it was written to answer has since been answered from the
> other direction — in-process against the real gateway code, where the result
> is a regression test rather than a one-off observation. Its own teardown step
> says to remove it and the `whoami_spoof` tool. Nothing depends on it.

---

## 0. What you are installing

A small zero-dependency Node HTTP server that binds `127.0.0.1` and forwards MCP
tool calls into two pinned scripts: three provisioning tools into
`org_request_queue.sh`, and three capability tools into `capability_gate.sh`
(§3 — do not skip the `capabilityScript` step; skipping it is what caused
case_b22b58a0). Caddy fronts it on a public hostname and terminates TLS and a bearer.
It is a transport: it decides *who is calling*, and nothing about what they may
do.

Full design and rationale: `mcp/README.md` in the repo.

---

## 1. Place the code

```sh
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/org-request-mcp
sudo install -o "$USER" -g "$USER" -m 0755 \
     /path/to/paperclip-ops/mcp/org-request-mcp.mjs \
     /opt/org-request-mcp/org-request-mcp.mjs
node --version      # must be 18+; the suite is pinned against 24
```

Confirm `org_request_queue.sh` **and `capability_gate.sh`** are where the config
will point (below). The server **refuses to start** if `queueScript` resolves to
anything not named `org_request_queue.sh`, or if a configured `capabilityScript`
resolves to anything not named `capability_gate.sh` — these are the only entry
points, and both are checked before it will accept a single request.

---

## 2. Generate the bearer, and put the three pieces in three places

Run this once. Note that the *server* never receives the bearer — only its
digest — so a read of the server's config does not yield a usable credential.

```sh
umask 077
bearer="$(openssl rand -hex 32)"

# (a) Caddy gets the bearer itself, as an ENV FILE that systemd reads. The
#     site block expands {$ORG_MCP_BEARER} from Caddy's process environment,
#     so this is the form Caddy can actually consume. Step 4 wires it up.
printf 'ORG_MCP_BEARER=%s\n' "$bearer" | sudo tee /etc/caddy/org-request-mcp.env >/dev/null
sudo chmod 600 /etc/caddy/org-request-mcp.env
sudo chown root:root /etc/caddy/org-request-mcp.env

# (b) The server gets only the SHA-256.
printf '%s' "$bearer" | sha256sum | cut -d' ' -f1

# (c) Store $bearer as a Paperclip company secret (UI or API), name it
#     something like org_request_mcp_bearer, and note the secretId for step 6.
#     Do NOT put it in transport_config.url — see the note at the end.

unset bearer
history -d "$((HISTCMD-1))" 2>/dev/null || true
```

**Do not** pass the bearer on any command line at any point. `/proc/*/cmdline`
is world-readable and every company on this box shares the host.

**Corrected 2026-08-24 (case_0409633c).** An earlier revision of this runbook had (a)
write the bearer to `/etc/caddy/org-request-mcp.bearer` and then asked you, in
step 4, to somehow get it from there into `Environment=ORG_MCP_BEARER=…` by
hand. Nothing read that file, and the hand-copy left the credential in a second
plaintext place. If you already ran the old step 2, remove the stale file once
step 4 is working:

```sh
sudo shred -u /etc/caddy/org-request-mcp.bearer   # only after step 4 verifies
```

---

## 3. Server config

```sh
sudo install -d -m 0755 /etc/org-request-mcp
sudo cp /path/to/paperclip-ops/mcp/deploy/config.example.json \
        /etc/org-request-mcp/config.json
sudo chown "$USER":"$USER" /etc/org-request-mcp/config.json
sudo chmod 600 /etc/org-request-mcp/config.json
sudoedit /etc/org-request-mcp/config.json   # fill bearerSha256, queueScript, capabilityScript
```

`chmod 600` is not advisory: the server refuses to start on any mode with group
or other bits set.

### `capabilityScript` — set it, or knowingly decide not to (case_b22b58a0)

**This step did not exist until 2026-09-04, and its absence was a nine-day
outage of the entire capability-request pathway.** case_662081e3 added the three
capability tools and `capabilityScript` to `config.example.json`, but never
added a line here. The install was performed from this runbook, so the key was
never set, and `mcp__Org_provisioning_request_queue__request_capability` failed
for *every* agent in the company with:

```
this transport is not configured to front that script (capabilityScript is unset in its config)
```

`TOOLS.md` instructs every agent to route capability asks through that tool, so
each ask died in an error the agent could not act on — reproduced from three
different agent identities on three different days (2026-08-31, 09-02, 09-04)
before anyone traced it to config rather than to a missing grant.

Set it to the gate, alongside the queue:

```json
"queueScript":      "/opt/paperclip-ops/org_request_queue.sh",
"capabilityScript": "/opt/paperclip-ops/capability_gate.sh",
```

Both scripts must exist at those paths. The server pins each basename at
startup and **refuses to launch** if `capabilityScript` resolves to anything not
named `capability_gate.sh` — the same guard `queueScript` gets.

**If you deliberately want a queue-only deployment,** leave it unset. Since
case_b22b58a0 that is now a safe, visible choice: the three capability tools are
withheld from `tools/list` entirely, so agents never see a tool this transport
cannot run. Confirm which you got — the startup line says so directly:

```sh
systemctl status org-request-mcp   # or: journalctl -u org-request-mcp -n 5
# tools includes submit/review/countersign_capability_request -> all six tools live
# no capability tools in the array                            -> queue-only
# capabilityToolsAdvertised mirrors that verdict on newer servers
```

A deployment that *meant* to front the gate and omits the capability tools is
this bug recurring. Fix the config and restart; no other change is needed.

---

## 4. systemd unit

```sh
sudo cp /path/to/paperclip-ops/mcp/deploy/org-request-mcp.service \
        /etc/systemd/system/org-request-mcp.service
sudo sed -i "s/OPERATOR_USER/$USER/;s/OPERATOR_GROUP/$USER/" \
        /etc/systemd/system/org-request-mcp.service
sudoedit /etc/systemd/system/org-request-mcp.service   # check ReadWritePaths
sudo systemctl daemon-reload
sudo systemctl enable --now org-request-mcp
systemctl status org-request-mcp --no-pager
```

Expect a single JSON line on start naming the address, the company, and the two
tool names.

**Then confirm the bind really is loopback-only:**

```sh
ss -lntp | grep 8391          # expect 127.0.0.1:8391 and NOT 0.0.0.0:8391
```

---

## 5. Caddy

```sh
sudo cp /path/to/paperclip-ops/mcp/deploy/Caddyfile.snippet \
        /etc/caddy/sites/org-request-mcp.caddy
```

Substitute in that file:

- `MCP_HOSTNAME` — the public hostname you are using.
- `CONTAINER_EGRESS` — the Paperclip container's egress address as the host
  sees it. Find it with the engine this host answers to (case_d8f38556: node7b1ff338 is
  Docker-only, so `docker` there):
  ```sh
  ENGINE="$(command -v docker || command -v podman)"
  "$ENGINE" inspect -f '{{.NetworkSettings.IPAddress}}' <paperclip-container>
  ```
  On a podman box the network is `192.0.2.49/23`; measure it on this host
  rather than copying that prefix.
- `{$ORG_MCP_BEARER}` — Caddy expands this from its own process environment at
  config-load time. Point systemd at the env file written in step 2; systemd
  reads the file, so the credential stays in one place and you never paste it:
  ```sh
  sudo systemctl edit caddy    # add:
  # [Service]
  # EnvironmentFile=/etc/caddy/org-request-mcp.env

  sudo systemctl daemon-reload
  sudo systemctl restart caddy   # RESTART, not reload: `systemctl reload caddy`
                                 # re-reads the Caddyfile but NOT the unit
                                 # environment, so a reload here leaves
                                 # {$ORG_MCP_BEARER} expanding to the empty
                                 # string and every request answering 401.
  ```
  Verify the substitution took, without printing the credential:
  ```sh
  sudo systemctl show caddy -p Environment | grep -c ORG_MCP_BEARER   # EXPECT 1
  ```
  Do **not** put the literal value in the site file instead. That file is the
  one that gets copied into a ticket.

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**Two things in that snippet are load-bearing and easy to undo by accident:**

- The `remote_ip` source restriction is condition 5 of the approval. It is what
  keeps a leaked bearer from being usable from the open internet. Do **not** add
  `trusted_proxies` or a `client_ip` matcher unless something genuinely fronts
  Caddy, or the control becomes spoofable via `X-Forwarded-For`.
- The 401 carries **no** `WWW-Authenticate`. The Paperclip gateway reads a 401
  with a bearer/oauth `WWW-Authenticate` as "go discover OAuth endpoints", which
  would bury a wrong-bearer error under a protocol error.

---

## 6. Register the tool_connection

Use `mcp/deploy/tool-connection.example.json`. Substitute `MCP_HOSTNAME` and the
`secretId` from step 2c.

**The single field that silently voids the whole epic** is
`config.headerPolicy.metadata.forward`. The gateway forwards **no** metadata
headers by default — a connection registered with just a `url` sends no
`x-paperclip-agent-id`, with no error and no health warning, and the transport
becomes silently anonymous. It must be:

```json
"config": {
  "url": "https://MCP_HOSTNAME/mcp",
  "headerPolicy": { "metadata": { "forward": ["agent_id", "company_id", "run_id", "correlation_id"] } }
}
```

Under `config`, **not** `transport_config`. And `quarantineNewEntries: true`.

Leave `versionSelector` unset so it resolves to `latest`: a pinned selector
silently keeps serving the old value through a rotation, so a bearer rotated
*because it leaked* would go on working.

---

## 7. Verify the container engine still works under the unit's sandbox

The unit sets `IPAddressDeny=any` / `IPAddressAllow=localhost`. Those filters
apply to AF_INET/AF_INET6 only, so a container exec over the engine UNIX
socket should be unaffected — but verify it on this box rather than assuming
(use the engine this host answers to; node7b1ff338 is Docker-only):

```sh
sudo systemctl show org-request-mcp -p IPAddressDeny
ENGINE="$(command -v docker || command -v podman)"
sudo -u "$USER" "$ENGINE" exec paperclip-db true && echo "engine reachable"
journalctl -u org-request-mcp -n 50 --no-pager | grep -c run_check_unavailable
```

If you see `run_check_unavailable` in the journal after a real call, the engine
is being blocked. That is the *correct* fail-closed behaviour for the wrong
reason, and it will look like a database outage. Comment out those two
`IPAddress*` lines rather than weakening anything else — the bind address is
still hard-coded in source.

---

## 8. Health

The catalog refresh POSTs `tools/list` with credential headers and **no**
session, so it is anonymous by necessity and the server allows it. Expect the
connection to go healthy and show exactly two tools:

- `submit_provisioning_request`
- `review_provisioning_request`

If it shows more, or shows anything that mentions the provisioner, stop and
tell me — that is not the build in this branch.

---

## 9. Acceptance — run the script and send me its output

```sh
sudo -v
./mcp/deploy/acceptance_live.sh --url https://MCP_HOSTNAME/mcp
```

Run it **from the git checkout**, not from a copy staged in the handoff channel,
and confirm the checkout is clean against `origin/main` first — the same check
the box at the top of this file asks for, for the same reason:

```sh
git -C <checkout> fetch origin
git -C <checkout> diff --exit-code origin/main -- mcp/deploy/acceptance_live.sh   # expect: no output
```

Send me the whole run, including the summary line. Exit status is the verdict;
the prose is for me.

### Why this is a script and not the four `curl`s

Step 9 was originally the four commands in the appendix below, pasted by hand.
Three things were wrong with that, and they are the reasons not to fall back to
it if the script is inconvenient (case_961e6ec2):

1. **Appendix assertion (1) cannot catch the bug it is aimed at.** It sends a
   call with *no* identity headers and checks for `403` /
   `identity_header_missing`. The missing-agent, missing-company and missing-run
   arms all raise that one code, so it stays green against a server that
   resolves a missing agent id to *some* principal — the exact hole mutation
   testing found in the container suite. That was measured, not argued: the
   self-test applies that mutation and replays assertion (1) verbatim, and it
   still reports `403 identity_header_missing`. The script's §3 omits **exactly
   one** header and asserts on the message, which is the only thing that
   discriminates.
2. **Every refusal needs a positive control.** With `requireLiveRun` ON, the
   control call needs an `(agent, run, company)` triple `heartbeat_runs` will
   corroborate, so §1 asks the database for a RUNNING one. If no agent in this
   company is awake the script stops and says so, rather than reporting refusals
   that would have happened anyway.
3. **Ordering.** The `requester`-argument refusal runs *after* identity and
   *after* corroboration, so the "nothing was appended" check needs the call to
   carry full live identity or it holds for an unrelated reason.

### What it does not cover

The script covers appendix assertions (1), (2) and (4), the end-to-end
`requester` case, and the run-corroboration check that had no command here at
all. Two conditions it reports as `SKIP` — never as a pass — and names in its
summary, because no shell on the VPS can produce them:

- **Assertion (3), Caddy's source restriction.** Has to be run from a different
  host. Still owed separately.
- **The gateway's half of the identity contract.** Needs an agent calling the
  tool through its own session. §9 of the script prints the exact hand-off.

### Appendix to step 9 — the assertions, as commands

Kept as the specification of what the script asserts, and as the record of the
approval conditions. **Do not run these instead of the script** — see assertion
(1) above.

These are the conditions of the approval, as commands.

```sh
# (1) Anonymous execution is refused, and the 403 names the fix.
#
# `-K -` reads the Authorization header from a curl config on STDIN. An earlier
# revision used -H "Bearer $(sudo cat ...)", which put the credential straight
# into curl's argv — contradicting this runbook's own rule two sections up,
# since /proc/*/cmdline is world-readable and this box is shared. (case_0409633c)
sudo sed -n 's/^ORG_MCP_BEARER=\(.*\)/header = "Authorization: Bearer \1"/p' \
  /etc/caddy/org-request-mcp.env \
| curl -s -K - -o /dev/stdout -w '\n%{http_code}\n' https://MCP_HOSTNAME/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"submit_provisioning_request","arguments":{"template":"E4_AUDIT_ANALYST","title":"t"}}}'
# EXPECT 403, code identity_header_missing, message naming headerPolicy.metadata.forward

# (2) No bearer, no service.
curl -s -o /dev/null -w '%{http_code}\n' https://MCP_HOSTNAME/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# EXPECT 401

# (3) Not reachable from anywhere but the container. Run this from a DIFFERENT host.
curl -s -o /dev/null -w '%{http_code}\n' https://MCP_HOSTNAME/mcp \
  -H "Authorization: Bearer <the bearer>" -d '{}'
# EXPECT 403 (Caddy source restriction), NOT 401 and NOT 200

# (4) The bearer is nowhere it should not be.
# `grep -f -` takes the pattern from stdin, so the bearer never becomes an
# argument of the very command that is checking where the bearer has leaked to.
sudo sh -c 'sed -n "s/^ORG_MCP_BEARER=//p" /etc/caddy/org-request-mcp.env \
  | grep -rlF -f - /etc /opt /var/log 2>/dev/null'
# EXPECT exactly one line: /etc/caddy/org-request-mcp.env
# In particular NOT the Caddyfile (the literal must never be pasted in) and NOT
# a systemd drop-in (use EnvironmentFile=, not Environment=).
tr '\0' ' ' < /proc/$(pgrep -f org-request-mcp.mjs)/cmdline; echo
# EXPECT the config PATH only — no token
```

Then the real end-to-end one, which is the epic's definition of done. Have one
agent call `submit_provisioning_request` with a `requester` argument naming a
*different* agent:

```json
{"template":"E4_AUDIT_ANALYST","title":"audit analyst","requester":"<some other agent uuid>"}
```

**Expected:** an MCP tool error reading `REFUSED (identity_argument_refused)`,
and **nothing appended to the queue**. If a request is created at all, or if it
is created against the named agent rather than the caller, stop and page me —
that is the failure the whole epic exists to prevent.

---

## 10. One thing I could not check from inside the container, and you can

`docs/transport-identity.md` §4 flags it: everything here rests on agent X not
holding agent Y's gateway session token. Both minting paths derive the agent
from the database, tokens are hashed, expiring, revocable and re-checked against
a live run — but **where the token is delivered to an agent** could not be
traced, because no `tool_connection` was granted to us and so no MCP config was
ever materialised.

Registering this connection is the moment that becomes observable. Right after
step 6, please check:

```sh
ls -la ~/.claude/session-env/ 2>/dev/null | head
sudo find / -name '.mcp.json' -newermt '-10 minutes' 2>/dev/null
```

and tell me whether a gateway session token appears anywhere an agent process
can read. If it does, that overturns the conclusion in §1 of that document and I
want to know the same day.

---

## Rollback

```sh
sudo systemctl disable --now org-request-mcp
sudo rm /etc/caddy/sites/org-request-mcp.caddy && sudo systemctl reload caddy
# then delete the tool_connection and revoke the Paperclip secret
```

Nothing in `org_request_queue.sh` changes as part of this install, so removing
the transport leaves the authorization core exactly as it was.

---

## Note on the existing Zapier row

Octavo Foundry's connection carries its auth token in plaintext in
`transport_config.url`. This install deliberately copies the *shape* of that
pattern (`mcp_remote`, health-checked by `tools/list`, `quarantineNewEntries`)
and **not** that part. Our bearer is a Paperclip secret injected as a header per
call and never touches the URL. Worth fixing on the Zapier row separately; I have
not touched it.
