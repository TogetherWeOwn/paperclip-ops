# ADR: split Paperclip server and agent OS authority for native SSH

> **Slice D provenance (recovery, no behavior change):** §§1.0/5.2/8.2 recovered
> here as the full file from ops-tooling `50f68ebcad` for the deploy-branch port.
> case_37f8c070 carrier `e5aa2570`, ADR SHA-256 `d554da57…c5fa794a` (267-line carrier;
> this is the 365-line re-validated file). Deploy-branch base
> @ `19141e2e5d` per the port plan. Citations below stay keyed to the `/app`
> installed tree as measured 2026-08-31; the expected-absent to expected-present
> flip ships with the seam in slice C, in the same change as slice B.
> `native_ssh_citation_gate.py` is byte-identical to `50f68ebcad` (blob `775ee9ce`);
> `test_native_ssh_citation_gate.sh` carries one fix vs `50f68ebcad`: `run_case`
> uses a here-string instead of `printf | grep -qF` to avoid SIGPIPE under `pipefail`
> (no gate-behavior change). `native_ssh_authority_audit.py`,
> `native_ssh_dispatch_matrix_audit.py` and their tests existed at `50f68ebcad` but are
> NOT recovered in this slice (last available at `50f68ebcad`); the §5.1/§5.3 links and
> the §8.1 three-artifact list stay pending until a named follow-up slice recovers them.
> Mutation suite `test_native_ssh_citation_gate.sh` is red-by-design on the current deployed
> `/app` tree: the unmutated baseline already reports `[DRIFT] remote_runtime_staging` and
> `[ABSENT] adapter_execute_seam` (exit 1), so `drift_is_detected`/`absent_anchor_detected`
> assert generic `DRIFT`/`ABSENT` and pass without mutation; only `missing_file`,
> `ambiguous`, and `undeployed_seam` discriminate until slice C re-keys the citations.
> No code behavior change here.

**Issue:** case_01fa2cdd · **Status:** proposed, host action not authorized · **Author:** CISO · **Date:** 2026-08-29 · **Re-validated:** 2026-08-31

**Decision:** put the Paperclip control plane and every agent run in different kernel security domains, then make native-SSH launch issuance a root-owned, single-purpose operation. Do not activate either preserved case_d75fae8e candidate until the hostile-agent acceptance suite passes from the real agent identity.

This artifact authorizes **no** uid/gid change, key creation, install, deploy, restart, canary, or activation.

---

## 1. Finding

### 1.0 Re-validation on 2026-08-31 (all four blockers now `done`)

case_e4054a66, case_37f8c070, case_72878c4a and case_073ef548 are all live `done` (verified by direct GET per id, not from the
parent's `blockedBy` snapshot). Every measurement below was re-taken from the hostile
agent identity on 2026-08-31. **Nothing in this ADR's finding changed**, and two claims
made elsewhere on the board do not survive re-measurement:

1. **case_e4054a66's accepted seam is not deployed.** Its acceptance comment cites
   `host-executor.ts:206-232,371-376` and a throw at `server-utils.ts:2388-2394`
   preceding ssh resolution at `:2406`. In the installed tree there is no
   `host-executor` file anywhere under `/app` and zero occurrences of `hostExecutor`,
   `assertAssignedModel`, or `launchAuthorization`. Deployed `server-utils.ts:2388-2394`
   is the Bubblewrap sandbox block and `:2406` is the Windows `cmd` branch; ssh actually
   resolves at `:2351` with **no** authority throw before it. The accepted commit
   `a36166c1` is case_0e66fe37's seam-resolution work, reachable only from
   unmerged successor branches. Merged and
   accepted is not deployed — the native-SSH carrier is currently unguarded in `/app`.
2. **The deployed tree has not moved.** `/app/server/dist/index.js` mtime is
   2026-08-18 03:23; `server-utils.ts` is 2026-08-18 03:17. No blocker's fix reached it.

Citations are now machine-checked. [`native_ssh_citation_gate.py`](../native_ssh_citation_gate.py)
verifies each `file:line` in this ADR by anchor text and distinguishes `present` /
`drifted` / `absent`, and refuses (exit 2) rather than first-matching a non-unique
anchor. It also asserts the four case_e4054a66 symbols stay absent, so if that seam ever does
deploy, this section fails loudly instead of rotting. Current run: 10/10 present, 4/4
absent-as-expected, `failures=0 audit_errors=0`. Its mutation gate
[`test_native_ssh_citation_gate.sh`](../test_native_ssh_citation_gate.sh) is 7/7 with a
positive control and a negative control.

### 1.1 Measured boundary defect

The stated boundary defect is present in the installed runtime, and one part is worse than the issue description:

- Paperclip server PID 2 and all observed agent/adapter/plugin processes run as effective uid/gid `1000:1000`.
- Server and agent share the same user, PID, mount, cgroup, network, IPC, and UTS namespaces.
- Both occupy unified cgroup `/`.
- `/proc` has no `hidepid` or `subset` mount option.
- From this hostile agent identity, `/proc/2/cmdline`, `/proc/2/environ`, and `/proc/2/fd` are reachable. Secret values were not printed.
- `/paperclip/instances/default/secrets/decision-signing.key` is owned by `1000:1000`, mode `0600`, and an ordinary agent can open it. The existing server signing code requires its key to be owned by the current Paperclip process user and will create/fix it as that user (`/app/server/src/services/decision-signing.ts:17`, `:84-132`).
- `/app/server/dist/index.js` is itself owned by uid/gid 1000 and writable to this agent identity. Root-owning a new signer while leaving the running server executable agent-writable would preserve a direct server-code impersonation path.

The live SSH carrier is also confirmed. Paperclip constructs arbitrary remote command, argv, cwd, and environment into an `ssh` spawn target (`/app/packages/adapter-utils/src/ssh.ts:1224`), and the shared child-process helper directly spawns that target (`/app/packages/adapter-utils/src/server-utils.ts:3248`). Runtime assets are copied into an agent-writable remote tree under `.paperclip-runtime` (`/app/packages/adapter-utils/src/remote-managed-runtime.ts:128`). A control staged through that tree cannot be authoritative.

The shared child-process helper is not the complete run boundary. The heartbeat calls adapter modules in-process (`/app/server/src/services/heartbeat.ts:15564`), external adapter packages are dynamically imported into the server (`/app/server/src/adapters/plugin-loader.ts:180`), and ACP execution creates its embedded runtime outside `runChildProcess()` (`/app/packages/adapter-utils/src/acpx-engine/execute.ts:3283`). ACPX also has separate agent and terminal child-spawn sites. A post-spawn callback can record a PID but cannot race-free establish uid, namespace, or cgroup authority. Therefore isolating only `runChildProcess()` would leave ACP and server-loaded adapter code outside the promised boundary.

The supplied recovery objects exist and match exactly:

- Ops `2d9844b93c52e1d862d9ec6608ad20843f45813d`, tree `d658c69fecc150ec32999e9b3280f0522c7133aa`.
- Core `e5d575146d6e250380d89d83ee08b3d84ef8462c`, tree `9cbe8af80e995e9a17862e9791080fffed0`.

Both correctly fail native SSH closed. Neither creates the missing authority split.

### 1.2 Re-validation on 2026-10-21 (seam live dormant; gate re-pinned and flipped)

On 2026-10-21 the case_e4054a66 seam is live dormant in `/app` (tog.21 `e93ce1ba`;
CISO dormant-accept case_00112b14; activation prohibited). §1.0 claim 1 ("not
deployed") and the `file:line` pins in §1.0–§1.1 describe the 2026-08-31 tree;
for the live tree they are superseded by the pins
[`native_ssh_citation_gate.py`](../native_ssh_citation_gate.py) now enforces:
the seam symbols `hostExecutor` (9 occurrences), `HostExecutor` (5) and
`authorizeLaunch` (1) must be PRESENT in `server-utils.ts` (zero in `ssh.ts`),
and the ten citation anchors are re-pinned to their tog.21 lines (both relocated
anchors resolved as renames, not removals). `assertAssignedModel` and
`launchAuthorization` were dropped per the 2026-09-01 finding (identifiers never
existed in the accepted tree). Current run: 10/10 present, 3/3
present-as-expected, `failures=0 audit_errors=0`; mutation suite 7/7. The
slice-D preamble's "red-by-design on deployed" note is superseded likewise: the
expected-present flip it anticipated ships in this change.

### 1.3 Re-validation on 2026-10-22 (tog.22; pins unchanged, suite hermetic)

On 2026-10-22 every pin was re-checked against the live tog.22 tree
(`8b8dcb7d5444719b6e269a875d773ce3609a28fb`, image tag `tog.22-8b8dcb7d`). The
eight commits since tog.21 `e93ce1ba` touch `server-utils.ts` (+30/−21) and
`heartbeat.ts` (+2/−2) outside the cited regions: all ten citation anchors
resolve to their §1.2 lines, each unique within its file, and the seam counts
are unchanged — `hostExecutor` 9, `HostExecutor` 5, `authorizeLaunch` 1, all in
`server-utils.ts`, zero in `ssh.ts`. No pin differs from §1.2, so no old → new
line mapping is needed. Current run: 10/10 present, 3/3 present-as-expected,
`failures=0 audit_errors=0`; mutation suite 7/7.

The mutation suite is hermetic in this port: it synthesizes its fixture tree at
the pinned lines instead of copying the live `/app` tree, so it runs in public
CI. The seam-absent mutation rewrites the seam symbols in place (same-line
substitution, no line deletes), so the mutated run fails exactly on the three
seam symbols with no extra `DRIFT`s; whole-line deletes would shift every later
anchor and bury the seam verdict under drift noise. The slice-D preamble's
point-in-time figures (the 365-line count, the byte-identical gate blob) describe
the recovery snapshot, not this tree, and are superseded here along with its
red-by-design note; the §3 pins below track the live tree.

### 1.4 Review hardening on 2026-10-22 (per-file lexical checks)

The gate now checks each seam independently in each file: the required-file
`occurrences` count is from `server-utils.ts` only, and `ssh_occurrences` must be
zero. Relocating a seam into `ssh.ts` or adding a second copy there fails.
Counts skip comments and quoted literals and match whole identifiers. A regex
literal is an atomic token in both files: the required file skips it, while
the forbidden file counts identifiers inside it without lexing its body as
code. The historical `HostExecutor` label includes the exact deployed
identifiers `NativeSshHostExecutorTarget` and `getActiveNativeSshHostExecutor`,
not arbitrary identifiers containing that substring. The live counts remain
9/5/1, with all ten anchors unchanged and zero occurrences in `ssh.ts`.

This remains a lexical citation guard, not a TypeScript parser or proof of an
executed authorization path. Template literal *text* is excluded, but
`${...}` interpolations are executable code, so identifiers inside them are
counted in both files. Division is distinguished from a regex literal by the
previous significant token, and line comments end at U+2028/U+2029 as well as
CR/LF. Because the regex is consumed as one token, body punctuation (a quote,
`//`, a backtick, `}`) can never desync the scan and hide a seam after it,
while a seam inside the body in the forbidden file still fails closed. The
host preflight and hostile-agent suite in §5 remain mandatory; this gate
cannot authorize activation.

### What this run could not establish

An agent container cannot prove the host's systemd unit definitions, host uid database, root-owned filesystem layout, SELinux/AppArmor policy, or cgroup delegation configuration. Those are mandatory operator preflight inputs, not assumptions to fill from inside the container.

---

## 2. Security invariant

A hostile agent must be unable to obtain or manufacture a native-SSH launch authorization even after arbitrary code execution as the agent OS identity.

That requires four independent boundaries:

1. **Control-plane principal:** Paperclip server has a dedicated uid/gid that no agent or plugin process uses.
2. **Run principal:** every agent run has a distinct ephemeral or allocated uid/gid, PID/mount namespace, private `/proc`, and delegated cgroup.
3. **Issuance principal:** a root-owned signer has no agent-visible key and accepts only a narrowly typed request from the expected server service identity.
4. **Launch principal:** a root-owned remote gate consumes one authorization once, verifies every authority field, then creates and supervises the SSH child in its assigned uid/cgroup.

A uid split alone is insufficient. A server uid that executes agent-writable code, inherits agent-writable configuration, or can be impersonated through a shared cgroup/socket still collapses the boundary.

---

## 3. Smallest architecture that creates a real wall

### 3.1 Host service identities

Create these identities only during an approved migration:

- `paperclip-svc`: static system uid/gid, no login shell, owns the Paperclip service state required by the server.
- `paperclip-agent-N`: per-run ephemeral identity via systemd `DynamicUser=yes`, or a bounded preallocated uid pool when persistent workspace ownership requires stable ids.
- root: owns the signer, verifier key material, launch ledger, and immutable launcher binaries.

Do not use a single shared `paperclip-agent` uid. It would isolate agents from the server but not from one another, preserving credential and process theft across concurrent runs.

### 3.2 Server unit

Run the server as `paperclip-svc` with:

- `NoNewPrivileges=yes`;
- `ProtectSystem=strict`, with explicit writable state directories only;
- root-owned `/app` or an immutable image, including every loaded server/plugin module and every ancestor;
- private temporary directories;
- no agent workspace mounted writable into the server service;
- a dedicated cgroup such as `paperclip-server.service`;
- signer socket access through a root-owned parent directory traversable only by `paperclip-svc`.

The migration must first move/chown server-owned state. Starting the service under a new uid against uid-1000-owned mutable code or secrets is not an authority split.

### 3.3 Per-run executor

Put the **entire adapter executor**, not merely its final CLI child, behind a host executor interface. The strongest minimal complete seam is the `adapter.execute(...)` boundary at `/app/server/src/services/heartbeat.ts:25879`: the server starts one IPC-backed worker in the run's OS domain, and logs, lifecycle events, tool callbacks, and the terminal result cross a typed channel. This covers CLI children, ACP agent children, ACP terminal commands, generic process adapters, and dynamically loaded adapter code with one boundary. A narrower `runChildProcess()` integration is acceptable only as a staged prototype and must remain fail-closed for ACP/external-adapter paths until those paths move behind the same executor.

For each run, the server asks systemd to create a transient scope/service with:

- a run-specific uid/gid;
- a run-specific cgroup named from a server-derived run id;
- new PID and mount namespaces;
- `/proc` mounted privately with `hidepid=2` or a PID namespace containing only the run;
- only its own workspace/state bind-mounted writable;
- server state, signer path, host credentials, and other run workspaces absent from its mount namespace;
- `PrivateDevices=yes`, `NoNewPrivileges=yes`, capability bounding set empty, and no access to the system bus or privileged runtime socket;
- lifecycle tied to the scope with `KillMode=control-group` and a finite stop timeout.

The server records the systemd unit name, cgroup path, uid/gid, and immutable launch-spec digest before exposing the run as started.

### 3.4 Root signer

Use a small root-owned service over an `AF_UNIX` `SOCK_SEQPACKET` socket. The signer:

1. reads peer pid/uid/gid via `SO_PEERCRED`;
2. rejects unless peer uid is exactly `paperclip-svc`;
3. resolves `/proc/<peer-pid>/cgroup` and rejects unless it is exactly the expected server unit cgroup;
4. verifies the peer executable and loaded deployment generation against an operator-pinned immutable deployment manifest;
5. accepts no caller-selected authority data other than a server-created opaque request id;
6. loads the launch specification from server-owned durable state and independently resolves the current run, agent, issue, assigned-model digest, adapter, environment, workspace, argv template, artifact hashes, audience, maximum lifetime, and nonce;
7. signs a canonical, versioned envelope with Ed25519; and
8. atomically records the nonce as issued in a root-owned ledger before replying.

The signer must not accept `runId`, `agentId`, `issueId`, model, argv, artifact hash, expiry, uid, cgroup, or destination as request parameters. The request grammar is `{version, opaqueRequestId}`. Treat the database/server state as untrusted input unless each field is bound to the live run and checked against an operator-owned policy manifest.

`SO_PEERCRED` uid by itself is not sufficient; service cgroup and immutable deployment identity are required. A root signer callable by any process under `paperclip-svc` is still compromised if the server loads agent-writable code.

### 3.5 Remote launch gate

The remote root-owned gate verifies:

- signature/version/audience;
- request and artifact digests;
- exact run/agent/issue/company/model assignment;
- exact command and complete argv;
- expiry and a maximum lifetime fixed by policy;
- single-use nonce using an atomic create/transaction;
- expected supervisor binary, unit, cgroup, uid/gid, and receipt path.

Only after all checks pass may it access SSH/provider credentials or create a child. The child starts in a dedicated systemd scope and the gate writes a root-owned receipt containing the accepted envelope digest, child pid/start time, cgroup inode/path, and terminal disposition. Missing or tampered ledger, receipt, supervisor, cgroup, or policy fails closed.

### 3.6 Core integration point

The complete upstream boundary belongs at the common adapter-execution seam, with the process-launch seam as a subordinate implementation point:

- replace the in-process `adapter.execute(...)` call near `/app/server/src/services/heartbeat.ts:25879` with an IPC-backed per-run executor launched from a server-derived immutable launch-spec reference;
- within that worker, extend `runChildProcess()` near `/app/packages/adapter-utils/src/server-utils.ts:4833` with the host-executor target used by CLI/process children;
- move or adapt both ACPX agent and ACP terminal spawn sites behind the same worker/executor before claiming ACP coverage;
- make native SSH request a signer-issued envelope before `resolveSpawnTarget()` can construct `ssh`;
- keep workspace staging separate and explicitly non-authoritative;
- persist executor/signer receipt metadata through the heartbeat run before declaring the child started.

This avoids duplicating authority logic in every adapter and includes adapter code itself in the hostile run domain. Until the worker boundary exists, native SSH may remain explicitly blocked rather than claiming partial protection from the CLI-only seam.

---

## 4. Rejected alternatives

| Alternative | Rejection |
|---|---|
| Service-held signing key under uid 1000 | Agent can read it now. |
| Root-owned signer with uid-1000 `SO_PEERCRED` allowlist | Server and agent are the same uid and cgroup now. |
| Dedicated server uid only | Cross-agent theft and server `/proc` visibility remain; agent-writable server code can still impersonate the service. |
| Shared agent uid with private workspaces | Concurrent agents remain one kernel principal. |
| Container-only user namespace without host cgroup/mount separation | Does not prove host service identity or protect signer reachability by itself. |
| Put the gate/key in `.paperclip-runtime` or the synced SSH home | That tree is copied for and writable by the remote agent identity. |
| Let the server send all signed fields to the signer | A compromised server process can select run/model/argv/expiry. The signer must resolve authority fields from an opaque id and policy. |
| Agent calls a public/root HTTP signer | Introduces a bearer credential into the same domain already proven unable to keep agent credentials separate. |
| Rely on signatures without replay/supervisor state | A valid ticket can be reused or a child can outlive its accepted authority. |

---

## 5. Exact acceptance suite

All tests run against a non-production fixture first. The final canary runs only after separate host-action approval.

### 5.1 Static and preflight checks

Run `native_ssh_authority_audit.py` *(pending recovery — not in this slice, last available at `50f68ebcad`)* as the hostile agent identity:

```bash
python3 native_ssh_authority_audit.py \
  --server-pid "$(<approved-server-pid-source>)" \
  --agent-pid "$$" \
  --key /var/lib/paperclip-native-ssh-signer/signing.key \
  --signer-socket /run/paperclip-native-ssh-signer/request.sock \
  --expected-server-uid <paperclip-svc-uid> \
  --expected-agent-uid <run-uid>
```

Exit `0` is required. An unreadable or absent observation is not silently clean: the script exits `1` or `2` and names the failed check.

Also assert every component and ancestor of server executable, signer binary, socket directory, policy manifest, key, ledger, launcher, and remote gate is root-owned and non-writable to all server/agent ids.

### 5.2 Hostile-agent negative matrix

For each row, capture four snapshots before and after: process/cgroup tree, provider/network request count, git refs/worktrees/commits, and workspace/artifact inventory. The delta must be zero.

| Case | Mutation | Required refusal |
|---|---|---|
| Key read | open/read/link/rename/bind-mount signer key | kernel denial; zero bytes |
| Socket access | connect directly as agent uid | kernel denial or signer `peer_unauthorized` |
| Peer impersonation | proxy from agent cgroup, inherited fd, pid reuse | cgroup/service/executable mismatch |
| Run selector | choose another/current/nonexistent run | signer accepts no such field; opaque id mismatch |
| Agent selector | substitute agent/company/issue | signer-derived values differ; refuse |
| Model selector | alternate model or digest | assignment provenance mismatch |
| Command selector | change executable, argv item/order, shell wrapper, env | complete launch-spec digest mismatch |
| Artifact selector | change gate/hook/settings hash or path | manifest/hash mismatch |
| Expiry selector | extend/not-before skew/expired ticket | policy or time-window refusal |
| Replay | consume identical ticket twice/concurrently | exactly one ledger consume; second refuses |
| Ledger tamper | delete/truncate/replace/revert nonce ledger | signer/gate unavailable; zero launch |
| Supervisor tamper | kill/replace supervisor before or during launch | scope terminated; terminal failure receipt |
| Receipt tamper | delete/replace receipt path or content | no success state; quarantine/fail closed |
| Cgroup tamper | move child, replace scope, reuse cgroup name/inode | supervisor terminates child; refusal recorded |
| Child escape | daemonize/double-fork/session detach | `KillMode=control-group` removes all descendants |
| Crash path | kill signer/server/gate/executor at every transition | no orphan launch; issued-but-unused nonce terminalized |

Every refusal record must include a stable reason code and request digest, never secrets or raw credentials.

### 5.3 Pre-dispatch assigned-model authority matrix

The reproduced case_099241db bypass adds a separate server/adapter acceptance layer before the native-SSH signer or launcher is reachable. A direct-only or assigned-model guard is not effective if an agent can first invoke another execution mechanism that starts a child, nested run, provider call, network request, or alternate model.

Run `native_ssh_dispatch_matrix_audit.py` *(pending recovery — not in this slice, last available at `50f68ebcad`)* against a trace emitted by server-owned integration-test instrumentation. The required matrix is the Cartesian product of:

- invocation family: `skill`, `workflow`, `task`, `agent`, `background_agent`, `model_selection`, `fallback`;
- override channel: `direct`, `agent_writable_instructions`, `hook`, `environment`, `scratch`, `tool_alias`, `settings_precedence`, `path_shadowing`.

That is exactly 56 cases. Every fixture deliberately includes an agent-writable instruction recommending delegation so the policy must override prompt and configuration advice rather than relying on model compliance.

For every case, the trace must prove all of the following:

1. exactly one attempt reached exactly one policy evaluation;
2. the decision was `deny` in phase `pre_dispatch`, before tool alias resolution, queueing, child creation, provider selection, or network dispatch;
3. the run became durably `failed` with exactly one stable reason code matching the policy decision;
4. successful handoff and successful continuation counts remained zero;
5. correction comment `162888ef-27c9-4ef3-8262-2230a6c698a2` remained the sole pending and next wake comment;
6. dispatch, child, nested-run, provider, network, alternate-model, success-handoff, and success-continuation counters all remained zero.

A generic tool error, model refusal, board intervention, post-dispatch cancellation, skipped continuation, or absent instrumentation does not pass. The trace source must be server-owned and append-only for the test duration; an agent-authored JSON file is input-shape validation, not acceptance evidence. The validator rejects missing and duplicate matrix cells so a partial suite cannot read green.

The implementation owner for this layer is case_e4054a66 because the gate must execute at the server/common adapter-execution seam before the per-run executor dispatches anything. case_37f8c070 owns the hostile fixture and counter/trace collection. case_72878c4a must include the relevant deployed settings, hook, alias, PATH, and instrumentation provenance in read-only preflight. case_073ef548 remains a prerequisite because agent-writable deployed server code could remove or forge the gate.

### 5.4 Positive controls

One exact assigned native-SSH fixture request must create one child in the expected uid/cgroup, one provider/network request, and one terminal receipt. Separately, one permitted direct-only non-nested fixture must reach the expected assigned provider exactly once. These controls are necessary: otherwise a completely disabled path satisfies every zero-side-effect negative assertion.

---

## 6. Migration, backup, and rollback

### Before any host change

1. Stop issuance of new native-SSH runs and wait for existing scopes to terminate.
2. Record package/image digest, service unit and drop-ins, uid/gid maps, directory ownership/modes/ACLs, mount and cgroup layout, server database backup id, and hashes of every candidate binary/config.
3. Take filesystem pre-images of server state whose ownership will change. Preserve numeric uid/gid and ACL/xattr metadata.
4. Verify both preserved candidates by commit and tree hash; do not deploy them.
5. Test the full restore on a disposable copy. A backup not restored is not rollback evidence.

### Activation order

1. Install immutable root-owned binaries and policy with signer disabled.
2. Create identities and migrate ownership while the service is stopped.
3. Start the server under `paperclip-svc` with native SSH still fail-closed.
4. Validate local/non-SSH execution and the hostile-identity audit.
5. Start signer with no accepted deployment generation; verify refusal.
6. Pin the reviewed server/executor generation, enable fixture issuance, run the complete matrix.
7. Only then authorize a separate live canary and rebase the preserved case_d75fae8e candidates.

### Rollback triggers

Rollback immediately on any of: unexpected shared uid/namespace/cgroup, agent-readable server `/proc`, writable immutable path, signer reachable from agent identity, unaccounted child, acceptance counter delta, missing receipt, or inability to prove ledger integrity.

### Rollback procedure

1. Disable native-SSH issuance first.
2. Stop all agent scopes and signer; verify no descendants remain.
3. Restore prior service unit/image and numeric ownership/ACL pre-image.
4. Restore database only if the schema changed; otherwise retain new audit records.
5. Verify that the restored prior image contains the preserved core candidate by commit and tree hash,
   or verify that an independent native-SSH disablement control is active. If neither condition is
   met, keep the server stopped and issuance disabled.
6. Restart the prior server only after the fail-closed condition is verified. Re-run the
   hostile-identity audit. Rollback is complete only when the prior state is explicitly measured and
   all partial new identities/sockets/keys are accounted for.

Do not delete the new key or ledger until rollback evidence is captured; revoke/disable access first, then archive or destroy under the credential rotation procedure.

---

## 7. Residual risk

- Root or kernel compromise defeats this design.
- A compromised immutable Paperclip server generation is an authorized signer caller. Deployment review and root-owned generation pins remain critical.
- systemd and `/proc` policy differ by distribution/container mode; fixture success does not replace a hostile live-identity canary.
- Database provenance is not automatically trustworthy merely because the signer reads it. The signer must bind to a live run and operator-owned policy, and the receipt must record exactly what it resolved.
- Per-run DynamicUser ids can be recycled. Receipts and cgroup identity must bind start time/inode and nonce, not uid alone.
- Native SSH's remote host remains a second trust domain with its own root gate, ledger, supervisor, and backup obligations.
- The installed source tree is not a git checkout, so the exact deployed source commit cannot be derived from `/app` alone. Package/image provenance must be supplied by the operator before activation.

---

## 8. Continuation path

**Validated 2026-08-31.** All four blockers (case_e4054a66, case_37f8c070, case_72878c4a, case_073ef548) are live `done`, so the
design work this card owns is complete. What remains is not design — it is a **host
action that no agent on this board is authorized to take**, plus one deployment gap the
blockers did not close.

### 8.1 What case_01fa2cdd has delivered (design scope complete)

- The measured finding, re-validated 2026-08-31 (§1.0, §1.1).
- The four-boundary invariant (§2) and the smallest architecture meeting it (§3).
- Eight rejected alternatives with the reason each fails (§4).
- The acceptance suite: preflight (§5.1), a 16-row hostile-agent matrix (§5.2), the
  56-case pre-dispatch matrix (§5.3), and positive controls (§5.4).
- Migration, backup, rollback triggers and procedure (§6); residual risk (§7).
- Three offline, mutation-gated audit artifacts: `native_ssh_authority_audit.py`,
  `native_ssh_dispatch_matrix_audit.py`, `native_ssh_citation_gate.py` — only
  `native_ssh_citation_gate.py` (+ its test) is recovered in this slice; the other two
  (and their tests) stay pending at `50f68ebcad` until a named follow-up slice.

### 8.2 The gap the blockers did NOT close

The 2026-08-31 deployment gap (§1.0) is superseded by §§1.2–1.3: case_e4054a66
is `done` and its accepted seam is live but dormant in `/app`. Activation remains
prohibited. Neither closing that case nor finding its source identifiers establishes
the OS authority split or the installed runtime's fail-closed behavior. Those require
the host preflight and hostile-agent acceptance evidence in §5.

### 8.3 The single remaining actor

Everything left requires **root on the host**: creating `paperclip-svc` and per-run
identities, root-owning `/app`, installing the signer, and mounting `/proc` with
`hidepid`. No agent identity in this company can do any of it, and case_d75fae8e explicitly
authorizes no host mutation. Therefore:

1. **Operator (human, via President & COO):** supply the read-only preflight artifacts
   §5.1 requires — systemd unit definitions, host uid database, root-owned filesystem
   layout, LSM policy, cgroup delegation — then approve a migration *rehearsal on a
   disposable copy*. This is the gating step for everything below.
2. **Upstream Paperclip implementer:** land the `adapter.execute(...)` executor seam
   (§3.3, §3.6) and the signer client. Fixture tests only, no deployment.
3. **Ops/security implementer:** build the root signer, remote gate, hostile-agent
   harness, and immutable deployment manifest as reviewable packages.
4. **case_d75fae8e** resumes only after the §5.2 and §5.3 matrices pass from the real hostile
   agent identity. Rebase the preserved candidates (ops `2d9844b9`, core `e5d57514` —
   both re-verified by commit and tree hash on 2026-08-31), replace the fail-closed stub
   with the reviewed signer protocol, then request activation approval separately.

Until step 1 completes, native SSH activation must remain prohibited. The preserved
core candidate fails closed, but that does not establish the installed runtime's
fail-closed behavior. A live but dormant seam is not hostile-agent acceptance.
