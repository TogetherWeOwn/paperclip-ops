# Paperclip host operations broker runbook

The broker implements Phase 0 of case_4f8471e2. It is read-only and unprivileged. Host deployment is an
operator step because an agent cannot create a host service account or install outside `/paperclip`.

## node7b1ff338 Docker runtime (case_81816394)

Image inspection uses one fixed rootful-Docker command:
`sudo -n -u root /usr/bin/docker image inspect <pinned-ref> --format '{{json .}}'`.
The image reference and root run-as binding must match the rendered sudoers mirror.
The broker account receives exactly three fixed reads — container logs, container
state, one image read — not Docker-group membership or unrestricted socket access.
Docker image JSON may omit `Digest`; the result schema reports that field as null,
not an invented digest.

Service/log reads target the node7b1ff338 Docker runtime (Compose project
`paperclip`, container `paperclip`): `docker logs` for the tail and
`docker inspect --format '{{json .State}}'` for container state. The MCP DB
corroborator runs default-user exec against `paperclip-db`. Writes remain
disabled; no host install, key change, or restart is authorized here.

Docker-group/socket access is root-equivalent. `docker exec -u node` changes the
in-container process user, not the caller's daemon privileges. Any MCP socket
access needs independent CISO sign-off before installation.
Sources: [image inspect](https://docs.docker.com/reference/cli/docker/image/inspect/),
[exec user](https://docs.docker.com/reference/cli/docker/container/exec/),
[Docker-group warning](https://docs.docker.com/engine/install/linux-postinstall/).

## Install

0. Fresh hosts only: the installer in step 4 is upgrade-only and refuses
   without a broker identity. Run the bounded preparation as root from the
   same reviewed checkout first. It creates the reader group, the service
   account and the protected directories the installer validates, creates
   the requests group unless one already exists, adopts a pre-existing system
   requests group and empty spool directories,
   and refuses on anything else (existing keys, service, control
   directories, or spool data it would have to take over). It never
   generates, rotates, reads or repairs keys.

   A pre-existing requests group is adopted WITH its members preserved
   (observed on node7b1ff338: `paperclip-host-requests` GID 986 with member
   `ubuntu`): preparation never edits group membership — the reader only
   joins via `useradd -G`, nothing is evicted and nothing is widened — and
   pins the exact pre-existing member list in its journal. The installer
   then refuses any new member, and any removal of a pinned member, after
   preparation. It also refuses a journal whose pinned list does not equal
   the asserted list, a journal that omits that list, and a truncated journal
   whose last line is not `}`. Before running
   preparation, confirm every current member is authorized, record the
   list in the install packet, and pass it back as the adoption assertion.
   Preparation refuses unless the observed set equals this list exactly,
   and refuses any account other than the reader whose primary group is the
   requests gid or the reader gid (invisible to `getent group` but with the
   same access). An unreadable or empty account table refuses too:

   ```bash
   getent group paperclip-host-requests
   getent passwd | awk -F: '$4 == 986 {print $1, $4}'
   ```

   Before the host window, confirm the account and group databases are local
   only. Every `passwd`, `group` and `initgroups` line must list only `files`:
   a directory source can hide primary-group holders or group members from the
   checks above, and preparation and the installer refuse any other source.
   Stop and escalate on any other output:

   ```bash
   grep -E '^[[:space:]]*(passwd|group|initgroups)([[:space:]:]|$)' /etc/nsswitch.conf
   ```

   Expected: `passwd: files` and `group: files`. Ubuntu's default `systemd`
   source would refuse here; accepting it is a CISO decision, not a local edit.

   ```bash
   sudo ./host-ops/prepare-first-install.sh --expect-requests-members ubuntu
   ```

   With no `--expect-requests-members` flag a group carrying members
   refuses. Never include `paperclip-host-reader` in the expected list: it
   joins during preparation.

   Preserve its PREPARE JOURNAL path and ROLLBACK lines. Rollback never
   deletes an adopted group or evicts its members, and it removes only what
   preparation created. The journal root and the per-run journal directory
   are evidence and stay; the installer refuses a root or entry that is not
   root-owned mode 0700, so copy a journal to read it rather than changing its
   mode. A failure after the first host change prints
   ROLLBACK lines and marks the journal `"phase": "aborted"`, which the
   installer refuses; the journal stays as evidence. Re-running preparation
   refuses once the identity exists; that refusal means preparation is done,
   not broken. An existing installation skips this step entirely.

1. Review the `host-ops/` tree at a specific 40-hex commit in a clean clone.
2. Create the response-signing RSA key in a controlled host session, export only its public key, and
   independently record the public key's SHA-256. Never copy the private key into `/paperclip` or an
   agent process.
3. Read the image reference this host actually runs — do not assume `paperclip-local`, and do not
   retag an old build to make the check pass (that would make the broker attest a stale image):

   ```bash
   docker inspect paperclip --format '{{.Config.Image}}'
   ```

   `Config.Image` is usually a tag. A tag is NOT
   sufficient: the installer and the broker require an immutable
   `name@sha256:<64hex>` pin and refuse anything else. Resolve the running
   image's digest without retagging, rebuilding or restarting anything:

   ```bash
   docker image inspect <the reference from above> --format '{{json .RepoDigests}}'
   ```

   Pass the resulting `name@sha256:...` value as `--paperclip-image-ref`. If
   the host yields no digest for the running image, stop and escalate on the
   install chain: a tag-form reference fails closed in `image_binding.py`
   (`immutable registry @sha256 image reference required`), and retagging
   another build to that name would make the broker attest an image the host
   is not running.

   The digest a host yields is evidence for the install chain, not a
   property of this runbook: record it there with its receipt. A recorded
   digest is not image, key, CISO or install acceptance.

4. Run `install.sh` as root with the reviewed commit, public-key output path, the independently
   recorded public-key SHA-256, and `--paperclip-image-ref <the reference from step 3>`. The installer
   exports `host-ops/` directly from that immutable commit, validates the pre-existing service
   account/groups and signing key pair, then copies only those bytes. The image reference is rendered
   into both the exact-argv sudoers grant and `config.json`, which the broker requires to agree.
5. Edit `/etc/paperclip-host-ops/config.json` as root. Deployment-specific values are
   `paperclipDataPath`, `opsToolingPath`, and the bounded tooling search roots. The node7b1ff338 Docker port
   retired `paperclipHostUser`/`paperclipHostUid`: the three Docker grants are container-fixed, not
   user-targeted, and the broker refuses a config that still names those keys — re-run `install.sh`
   instead of hand-editing the config. Do **not** hand-edit `paperclipImageRef`: it must name the same
   image as the installed sudoers grant, and the broker refuses to start if the two disagree. When the
   deployed image rolls, re-run `install.sh` with the new `--paperclip-image-ref` so both move together.

   The two are **not** byte-identical, and that is intentional. sudoers reads a bare `:` as the
   run-as separator, so `install.sh` escapes every colon in the grant as `\:` — both accepted
   reference forms carry one (`name:tag` and `name@sha256:<64hex>`). `config.json` stores the plain
   reference and the broker unescapes the grant before comparing. Copying the grant's bytes into
   `config.json` by hand would therefore make the broker refuse to start.
6. Run `visudo -cf /etc/sudoers.d/paperclip-host-ops-broker`, then restart the service after config
   changes.

The installed executable is `/usr/local/libexec/paperclip-host-ops/broker.py`; state, private key, and
audit remain under host-owned `/var` paths. Nothing authoritative is loaded from git at runtime.

## Request

From an agent-visible request spool mounted **without any host credential or control socket**:

```bash
python3 host-ops/broker.py request \
  --spool /path/to/request-spool \
  --verb health.inspect \
  --request-id HRQ-<unique-safe-id> \
  --agent-id "$PAPERCLIP_AGENT_ID" \
  --run-id "$PAPERCLIP_RUN_ID" \
  --issue-id "$PAPERCLIP_TASK_ID" \
  --ttl 300
```

The request CLI accepts no path, command, URL, shell, token, unit, image, or approval argument. The host
polls the spool; the agent never calls a daemon or socket.

Phase-1 write requests (case_b1f57205: `plugin.install`, `plugin.restart`, `unit.install`,
`cliproxy.apply`, `coolify.env.set`, `script.run`) take one JSON object of names and SHAs only:

```bash
python3 host-ops/broker.py request \
  --spool /path/to/request-spool \
  --verb plugin.install \
  --arguments-json '{"package":"dispatch-src","sha":"<40-hex-reviewed-commit>"}' \
  --request-id HRQ-<unique-safe-id> \
  --agent-id "$PAPERCLIP_AGENT_ID" \
  --run-id "$PAPERCLIP_RUN_ID" \
  --issue-id "$PAPERCLIP_TASK_ID" \
  --ttl 300
```

There is deliberately no per-field flag: no path, command, URL, shell, token, unit, image,
or secret value can ever be a request argument. Allow-lists resolve host-side from the
name/SHA pair; `coolify.env.set` takes key *names* only and values are minted host-side.

## Preview and verification

The broker writes a signed executor-rendered preview before execution, and a signed response after it.
Verify either with the independently pinned public-key hash:

```bash
bundle=/path/to/previews/HRQ-....bundle
base=${bundle##*/}
base=${base%.bundle}
python3 host-ops/broker.py verify \
  --public-key /path/to/pinned/response-signing.pub.pem \
  --expected-key-sha256 '<independently recorded 64-hex hash>' \
  --response "$bundle/$base.json" \
  --signature "$bundle/$base.sig"
```

The `.bundle` directory is the atomic publication unit; derive both sibling paths from the same selected
bundle rather than expanding independent JSON and signature globs. Use the same command for `responses/`.
Do not trust an unsigned JSON file or a public key delivered in
the same untrusted channel without the independent hash pin.

## Approval

Phase 0 safe reads are fixed operations and need no per-call human authorization. The requester identity
is attribution only.

Phase-1 writes (case_b1f57205) render a signed dry-run preview naming the planned writes, then always
refuse with `write_execution_disabled`: no executor runs and no host write happens. The broker ships
with `writesEnabled: false` and every Phase-1 spec ships `enabled: false`; OWNER decision O4 must be
recorded on case_b1f57205 before a follow-up slice may flip either switch. There is no approval command
that can change that. The dry-run preview's `effectClass` is `write_dry_run` (reads stay
`read_only`), and the preview is the review artifact a board/human authorization would later bind to.

The `paperclip.deploy` and `paperclip.restart` specifications predate case_b1f57205, require a single-use
board/human technical authorization, and keep their immediate refusal with no preview. This runbook
intentionally has no steps for creating or consuming one because case_e848a83d-class credential isolation
is still reproducible. An operator must continue to execute reviewed write procedures directly until
that technical precondition changes.

### Owner-approved write tiers (case_5248bcfa)

case_5248bcfa layers an owner-approved tier model ahead of the O4 gate above; it narrows who a write
request has to name, not whether one can execute in this build. Every verb now carries a `tier`:

- `read` -- automatic, unchanged.
- `routine-write` (`plugin.install`, `plugin.restart`, `unit.install`, `cliproxy.apply`,
  `coolify.env.set`, `script.run`, `coolify.app.deploy`, `coolify.app.restart`) -- the request must carry a non-empty `reason` and an `approval`
  object naming the configured `ceoAgentId` as `approver`, fresh within the approval TTL, and
  single-use via the existing replay store. `agentApprovalAllowed` stays `false`: no CLI flag exists
  to attach `reason` or `approval`, so a request built by `broker.py request` can never satisfy this
  tier, by design.
- `paperclip-restart` (`paperclip.deploy`, `paperclip.restart`) -- the same CEO approval, plus a
  `drainReceipt` attesting `runningRuns: 0` and `newDispatchStopped: true`, fresh within a tight TTL.
  The broker also verifies zero running runs host-side before executing; that host-side check has no
  wiring in this build and always returns unknown, so caller attestation alone can never authorize a
  restart -- the request refuses with `drain_verification_unavailable` even when fully authorized.
- `owner-reserved` -- key/credential lifecycle, firewall, SSH, and spend verbs are refused with
  `owner_reserved` before any other check, including config or approval. This tier is never available
  through this broker, at any authorization level.

A missing or mismatched approver refuses with `approval_unconfigured` or `approval_invalid`; a stale
approval refuses with `approval_stale`; a missing, stale, or non-zero-run drain receipt refuses with
`drain_required`. A staging-app restart with no host-verified successful deploy refuses with
`deploy_verification_unavailable` (no ledger wiring in this build) or `deploy_required` (synthetic
ledger shows failed/missing). Every accepted, resumed, or refused audit entry embeds the requester triple, the
`reason`, and the `approver` alongside the existing hash chain.

### Exact-target staging-app verbs (case_802335d7, disabled specs)

`coolify.app.deploy` takes only `{app, sha}`; `coolify.app.restart` takes only
`{app}`. The Coolify app UUID, `environment`, pinned `allowedReleases` set and
`deployPath` resolve host-side from the versioned `stagingApps` catalogue in
`writeAllowList` (see below); the request never carries endpoint URLs, tokens,
argv, or secret values, and any undeclared argument refuses with
`invalid_arguments` before any catalogue lookup. Missing catalogue, unknown
app, `environment != staging`, malformed entry, or a release outside the
pinned set fails closed (`write_manifest_required`, `app_not_allowlisted`,
`prod_target_refused`, `staging_catalogue_invalid`, `release_not_allowlisted`)
with no preview. `coolify.env.set` is not a deploy: it only sets allow-listed
env entries and cannot change the running release.

Restart binds the running release and prior container state; its preImage and
postconditions record both, and its rollback restores the prior state at the
bound release. A restart can never bypass a failed deploy: without a
host-verified successful-deploy record it refuses (see codes above). No
executor or inverse exists in this build -- an unimplemented path refuses
rather than succeeding or no-op-ing -- and the MCP transport exposes no
deploy/restart tool. Real staging registration, the Coolify deploy-ledger
query, the tested inverse, and activation remain separately reviewed work;
case_cdaf2970 stays the single install handoff.

## Execution and postconditions

For a read, success requires all of:

- a canonical hash matching the exact request;
- an unexpired TTL at host acceptance;
- first use of the request id;
- verb and arguments matching the fixed production table;
- an executor-generated preview published as one atomic JSON/signature bundle;
- the fixed read completing within its timeout and matching its per-verb result schema;
- a hash-chained `execution.succeeded` audit record;
- a response published as one atomic JSON/signature bundle, carrying the audit sequence and entry hash.

## Refusal, expiry, and replay

Refusals are signed responses and host audit rows. Common codes:

| Code | Meaning / remedy |
|---|---|
| `unknown_verb` | use a named fixed read verb; no arbitrary extension exists |
| `invalid_arguments` | remove requester-supplied arguments |
| `canonical_hash_mismatch` | discard and recreate the request; do not repair it in place |
| `expired` / `ttl_too_long` | submit a fresh unique request within the ten-minute maximum |
| `replay` | request id was already consumed; create a new request id |
| `write_execution_disabled` | use the current board/human operator write procedure |
| `write_manifest_required` | register the versioned host-owned allow-list; no catalogue, no preview |
| `app_not_allowlisted` | use a catalogued staging app name; unknown and prod names refuse |
| `prod_target_refused` | staging only; production targets never deploy/restart through this broker |
| `staging_catalogue_invalid` | fix the catalogue entry (UUID, release set, deploy path) host-side |
| `release_not_allowlisted` | deploy only a pinned catalogue release; floating/cross-app SHAs refuse |
| `deploy_required` | record a successful deploy first; restart cannot bypass a failed deploy |
| `deploy_verification_unavailable` | no deploy-ledger wiring in this build; restart refuses before the O4 gate |
| `audit_corrupt` | stop the broker; preserve audit/state; investigate before accepting more work |

A request id is consumed after shape/hash/TTL validation and before execution, and is never reusable
after a host-accepted refusal or executor failure. An already-expired request does not reserve the id;
this prevents a stale pre-burn from denying a later valid request while still preventing an accepted
request from being replayed under changed host conditions.

## Audit evidence

- `/var/log/paperclip-host-ops/audit.jsonl` — append-only, hash-chained host audit
- `/var/lib/paperclip-host-ops/audit-head.json` — atomically replaced chain-head checkpoint used to detect complete-tail truncation
- `/var/lib/paperclip-host-ops/state.sqlite3` — single-use reservation and final status
- `previews/*.bundle/<basename>.json` + `<basename>.sig` — executor-rendered preview
- `responses/*.bundle/<basename>.json` + `<basename>.sig` — result/refusal and linked audit record

The broker takes `/var/lib/paperclip-host-ops/broker.lock` before audit verification and holds it
through replay reservation, request execution, and audit append. Concurrent `run` or `run-once`
processes therefore serialize instead of allocating the same chain tail. It verifies the full audit
chain while holding that lock and refuses to run if the chain was edited or truncated. Request claims
fsync both the requester queue and broker-owned processing directory before processing. Signed output
staging stays below the broker-only `.staging/` directory; publication fsyncs the complete JSON/signature
bundle, sets its final mode while it is still unreachable to requesters, atomically renames it into the
output channel, then fsyncs both source and output directories.
Preserve the audit and replay database together for incident analysis.

## Rollback and uninstall

Phase 0 performs no write, so request-level rollback is vacuous. To roll back the deployment:

1. `systemctl disable --now paperclip-host-ops-broker.service`
2. preserve audit, replay state, installed commit, config, and public-key hash;
3. remove `/etc/sudoers.d/paperclip-host-ops-broker`, the service unit, tmpfiles entry, config, and
   `/usr/local/libexec/paperclip-host-ops`;
4. after evidence retention, remove the service user/groups and `/var` directories.

For a future write verb, the registry requires pre-image capture, automatic inverse, bounded timeout,
and postconditions. A verb without a tested inverse is not registrable.

## Bounded log tail (`logs.read` / MCP `read_logs`)

This read-only verb has **no arguments**. Its only allowed target is the node7b1ff338
`paperclip` container, using an exact `docker logs paperclip --tail 200 --since 15m
--timestamps` grant. Host operator verified 2026-10-05: node7b1ff338 runs no systemd
unit for the application at all (not-found/inactive, empty FragmentPath); the
runtime is Compose project `paperclip`, service `server`, container
`paperclip` (`/home/ubuntu/stacks/paperclip/compose.yaml`, local log driver).
The time window (900 seconds), event count (200), raw input (64 KiB), serialized
response (64 KiB) and subprocess timeout (20 seconds) are fixed, not caller or
config options. Oversized raw output refuses rather than truncating partial JSON.
The broker reads stdout into bounded memory only; stderr is discarded and errors
use fixed messages, never raw container-log diagnostics.

**Intentional redaction tradeoff:** output is metadata-only. Every message is
`[REDACTED]`; only the parsed Docker timestamp (as epoch microseconds) and the
fixed informational priority (6 — the local driver records no per-line severity)
survive. Message text, extra fields, command lines and identifiers are all
dropped **before** result hashing, audit, signing or bundle persistence.
Pattern-based token matching cannot reliably detect unlabelled or multiline
secrets. This conservative initial contract does not claim to expose useful
message text; richer message allow-lists would need separate security review.

The MCP transport exposes `read_logs` using the same authenticated identity and
signature verification as the existing read tools. The sudoers template adds only
this exact container-log command. This is a **source change, not an installed grant**:
installation review and an authorized host rollout must verify container-log
access and signed responses in the real service sandbox. No host mutation or
deployment is performed by this change; existing write-activation gates remain
untouched.

Docker logs option semantics: https://docs.docker.com/reference/cli/docker/container/logs/.

## Versioned write-preview manifests (writes remain disabled)

The optional `writeAllowList` in the host-owned broker config is a **preview-only**
contract. No manifest is shipped or approved by default. Without it, `coolify.env.set`,
`script.run`, `coolify.app.deploy`, and `coolify.app.restart` refuse before producing a
preview. Other Phase-1 previews are unchanged.
A syntactically valid manifest does not authorize execution: all write specs remain
`enabled: False`, and there is no write backend.

Manifest format (references only; never put secret values in this object):

- `version`: integer `1`; `revision`: immutable 40-hex reviewed manifest commit.
- `apps`: map keyed by app name. Each entry has `keys` (per-app list of allowed key
  names) and `mapping: {name, sha}` (safe name and 40-hex immutable server-side mapping
  revision). Requested keys must be distinct and a subset of that app's keys.
- `scripts`: map keyed by script name. Each entry has `sha` (exact requested commit),
  `writeScope` (non-empty list of canonical absolute target paths; not `/`, dot segments
  or duplicate separators), `inverse: {script, sha, testEvidenceSha256}`, and
  `compositionReviewSha256`. The inverse is pinned to the same script commit. Evidence
  fields are 64-hex artifact digests, not caller assertions in request arguments.
- `stagingApps` (case_802335d7): map keyed by app name. Each entry has `coolifyAppUuid`
  (UUID), `environment` (must be exactly `staging`), `allowedReleases` (1-64
  unique 40-hex pinned SHAs), and `deployPath` (canonical absolute host path;
  not `/`, no dot segments, never a URL). Deploy requires `arguments.sha` to
  equal one pinned entry; restart binds the running release of a succeeded
  deploy (see above). Synthetic entries only in examples; real staging
  registration is independently reviewed and never part of this contract.

The broker validates the selected entry before previewing it and binds the manifest
revision into the signed preview. It never copies mapping values, evidence contents,
or host pre-images into a response. Missing scope, inverse evidence, composition
review or pinned mapping refuses. These are structural checks, **not verification of
approval or of the referenced evidence**.

Activation still requires the separately reviewed backend/install gates. That backend
must verify approval of the exact manifest revision, resolve mappings server-side,
capture **full prior env values and absence** in protected host-only storage, and
restore them exactly on rollback. Script registration must actually test each inverse
and review combined script/unit effects, including prior enable/active states. All
name-to-path resolution must canonicalize under the declared root and refuse symlink
escape; request name validation is not runtime confinement. None of those host writes
or activation gates is implemented or enabled by this preview contract.

## Tests

```bash
python3 test_host_ops_broker.py
./test_host_ops_install.sh
./test_host_ops_prepare_first_install.sh
bash -n host-ops/install.sh host-ops/install-lib.sh host-ops/prepare-first-install.sh
python3 -m py_compile host-ops/broker.py
```

The installer completes artifact syntax/grammar, signing-key, pin, and pre-existing identity
preflight before replacing any live file. It never creates the service
identity: a wholly absent identity refuses (`transactional upgrade requires
the existing broker identity`), and a partially configured or incompatible
identity refuses before mutation. Fresh hosts run
`host-ops/prepare-first-install.sh` first (Install step 0), which creates
exactly the missing identity and directories under its own journal and
rollback; then the operator key step (Install step 2); then this installer.

The suite covers request, preview, execution, verification, refusal, expiry, replay prevention,
complete-tail audit truncation, request-path replacement, durable cross-directory claims, signed-pair
failure publication, same-byte verification under path replacement, FIFO/symlink refusal, strict
per-verb result schemas, multi-process serialization, the literal read/write authority sets and
routing, installer/service/tmpfiles/sudoers consistency, immutable commit export, signing-key-pair and
service-identity validation contracts, the O4-gated dry-run write previews (signed preview, then
signed refusal, no executor), typed write
pre-image/postcondition/inverse contracts, and the test seam's fail-closed behavior. It runs
`systemd-analyze verify` and `visudo -cf` when those validators are installed. It uses generated keys
and fabricated facts; it needs no host credential.

The unit runs two `ExecStartPre` checks inside the final service sandbox before the broker can become
active. A root-prefixed `cmp` first proves `/etc/paperclip-host-ops/sudoers.rendered` is byte-identical
to the real `/etc/sudoers.d/paperclip-host-ops-broker`, which must remain `root:root 0440` for sudo to
accept it. The second check runs unprivileged as `paperclip-host-reader`; it reads that mirror, then
executes and validates the exact sudo-backed `service.inspect` and `image.inspect` commands. Keeping
the functional check unprivileged proves the daemon's real sudo authorization rather than bypassing it
as root. A deployment where the grant drifts or filesystem restrictions block rootful Docker therefore
fails closed at service start.

If `ExecStartPre` refuses with `fixed host read failed with exit 125: ... image not known`, the
configured `paperclipImageRef` names an image this host does not carry. Re-read the reference from the
running unit (Install step 3) and re-run `install.sh` with it. Do not retag another build to that name
to get past the refusal: the broker would then attest an image the host is not running.

`RestrictNamespaces=` is deliberately absent. The rootful Docker client needs full
namespace access, and systemd's namespace seccomp filter rejects namespace entry
unless every type is allowed. Listing every type is equivalent to omitting the
filter, so the unit keeps the other hardening directives and relies on the exact
sudo argv, narrow writable paths and live acceptance check instead.

Offline CI uses fabricated host facts and proves the source-level unit contract, narrow authority and
`ExecStartPre` wiring, but it has no live rootful Docker to exercise. The installer
runs `systemd-analyze verify` against the pinned unit before mutation; that grammar check also cannot
prove live namespace compatibility. A green CI run is therefore not installation evidence for this
compatibility boundary. Preserve the target
host's installer output plus `systemctl status` and relevant journal output showing that the real
install restarted `paperclip-host-ops-broker.service`, the unprivileged acceptance check passed and
the service remained active.
