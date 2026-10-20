# Staging-preflight carrier contract

Source: `host-ops/staging_preflight_carrier.py`. Conformance suite:
`test_host_ops_staging_preflight_carrier.py` (offline, stdlib only).

## Purpose and non-goals

The carrier is a fail-closed, disabled-by-default, fixed-purpose gate around
the disabled staging-preflight receipt adapter. It validates one dispatch
request against a root-owned installed manifest and, only when every check
passes, execs the fixed installed adapter as
`[interpreter, "-I", <adapter>]` with fixed argv, a scrubbed environment and
no stdin. It then validates and minimizes the adapter's receipt.

The carrier performs no network I/O itself. It has no install, enable or
activate mode; shipping it changes nothing about what may run. A queue
acknowledgement is never dispatch proof: only a validated adapter receipt
from an actually executed fixed path yields a passing dispatch receipt.
There is no silent fallback: a missing or unverifiable fixed path refuses.

## Provenance (verified offline, never executed)

The adapter bundle is inert input; it is not vendored here because its
deployment pins stay private. Verified 2026-10-20 by byte hash:

- Archive `staging-preflight-adapter-offline.zip`, 39609 bytes,
  sha256 `434ba41bde6ae8da561b84719ba954539b8d4957609cb2384ce8afb955e81ac6`.
- Every extracted file matches the archive's `MANIFEST.json` sha256.
- The four reviewed-source git blob sha1s match the adapter's own pin table:
  health-contract probe `aaf646a221a1a961a70effa51b1ff6182c1a17a8`,
  build-identity helper `ccfe8acdac487b8367d952f6d6fb38e30c39b3be`,
  rollout helper `6f4e71b558e136c00b4adf7562524cbd1fecae06`,
  probe tests `69e61f84701f6efafdc99ff0e21f7d049ea2ce6f`.
- The adapter ships `EXPIRES_AT = 0` (disabled) and stays disabled in this tree.

A future install must re-verify these exact bytes and obtain independent
review before any activation. No pin refresh or expiry extension is granted
by this source.

## Request schema (`staging-preflight-dispatch/v1`)

A bounded (<= 16 KiB) JSON object read from `--request PATH` with exactly
these keys; any undeclared key (command, argv, endpoints, credentials, paths)
refuses. The file must be a regular file, not a symlink, not world-writable.

| Field | Shape |
|---|---|
| `schema` | exactly `staging-preflight-dispatch/v1` |
| `action` | exactly `staging-preflight` (the only fixed purpose) |
| `request_id` | `[A-Za-z0-9_-]{1,64}` (one-shot key, filename-safe) |
| `principal` | `[a-z_][a-z0-9_-]{0,31}`; must equal the manifest principal |
| `approval_id` | `[A-Za-z0-9_.:-]{1,128}`; must equal the installed approval |
| `expires_at` | integer epoch; must satisfy now < expires <= approval bound |
| `targets` | exactly the nine keys below, equal to the manifest targets |

Target shapes: `revision` 40-hex; `build_id` digits-digits (each half <= 20);
`worker_version`, `application_id`, `rollout_id` lowercase UUID;
`namespace_id` 32-hex; `target_version` integer 1..2^31-1; `image`
`registry.cloudflare.com/<32-hex>/two-bot-next-twobotcontainer-staging@sha256:<64-hex>`
(immutable digest only, never a tag); `origin_sha256` 64-hex.

## Installed manifest schema (`staging-preflight-manifest/v1`)

Root-owned, not group/other-writable, <= 16 KiB, exactly these keys:

```json
{
  "schema": "staging-preflight-manifest/v1",
  "enabled": false,
  "adapter_sha256": "<64-hex-sha256 of the exact installed adapter bytes>",
  "approval": {"id": "<approval token>", "expires_at": "<epoch>"},
  "principal": {"user": "<fixed service account>"},
  "targets": {"revision": "<40-hex>", "build_id": "<run-attempt>",
    "worker_version": "<uuid>", "application_id": "<uuid>",
    "namespace_id": "<32-hex>", "rollout_id": "<uuid>",
    "target_version": "<int>", "image": "<immutable digest ref>",
    "origin_sha256": "<64-hex>"}
}
```

`enabled` must be exactly `true` or every dispatch refuses with
`carrier_disabled`; the documented default is `false`. A caller-provided
hash is never authority: the manifest digest is the only one that counts.

## Dispatch receipt schema (`staging-preflight-dispatch-receipt/v1`)

Printed to stdout and stored as `<request_id>.receipt.json` in the state
directory once the adapter has run. Exactly these keys; informational
adapter keys are dropped, unknown keys refuse the receipt:

`kind`, `schema`, `action`, `request_id`, `approval_id`, `principal`,
`adapter_sha256`, `targets` (manifest echo), `interval`
(`claimed_at`, `finished_at`, `expires_at`), `outcome` (`pass`/`fail`),
`failure` (fixed-vocabulary code or null), `observations`
(`surface`, `status`, `worker_version_matched` only), `adapter_exit`.

Receipts never carry raw payloads, bodies, headers, remote exception text,
tokens or member data. Child stderr is drained and discarded.

## Entrypoint and fixed paths

- Carrier: `/usr/local/sbin/paperclip-staging-preflight-carrier`
  (root-owned; invoked with exactly `--request PATH`).
- Adapter: `/usr/local/libexec/paperclip-host-ops/staging-preflight-adapter.py`
  (root-owned, digest-bound, invoked as `[interpreter, "-I", <adapter>]`).
- Manifest: `/etc/paperclip-host-ops/staging-preflight-manifest.json`.
- State: `/var/lib/paperclip-host-ops/staging-preflight/` (service-owned,
  mode 0700; `<request_id>.claim` + `<request_id>.receipt.json`).

Child process: fixed argv (no request-derived argument exists), stdin
closed, working directory `/`, environment exactly the present subset of
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`,
`PAPERCLIP_RUN_SCRATCH_DIR`, `HOME` plus fixed `PATH=/usr/bin:/bin` and a
fixed `C.UTF-8` locale. Any proxy variable in the carrier's own
environment refuses before dispatch. Stdout and stderr are streamed
with a hard byte ceiling while the adapter runs: the first chunk past
64 KiB + 1 of stdout or 16 KiB + 1 of stderr terminates the whole
adapter process group at once and produces `adapter_output_over_cap`;
the whole dispatch is bounded at 180 seconds.

Exit codes: `0` dispatched and passed; `1` dispatched and failed (receipt
written, one-shot consumed); `2` refused before the adapter ran
(`REFUSED: <code>` on stderr, empty stdout); `3` the adapter ran but its
receipt could not be stored (`ERROR: receipt_unwritable`, one-shot
consumed, the claim file remains as evidence of the attempt).

## Validation order

Request read/shape, proxy refusal, manifest read/ownership/shape/enabled,
approval freshness, invoker-is-manifest-principal, request bindings
(principal, approval, expiry window, exact targets), adapter
read/ownership/digest, state checks, atomic one-shot claim, dispatch.
Refusal codes are fixed vocabulary: `request_unreadable`,
`request_too_large`, `request_not_json`, `request_schema_refused`,
`proxy_environment_refused`, `manifest_unreadable`, `manifest_too_large`,
`manifest_ownership_refused`, `manifest_refused`, `carrier_disabled`,
`approval_expired`, `principal_mismatch`, `authorization_refused`,
`request_expired`, `request_expiry_beyond_approval`, `targets_refused`,
`adapter_unreadable`, `adapter_too_large`, `adapter_ownership_refused`,
`adapter_digest_mismatch`, `state_refused`, `state_exhausted`,
`request_replayed`. Post-dispatch failure codes: `dispatch_failed`,
`dispatch_timeout`, `adapter_output_over_cap`, `adapter_receipt_refused`,
`observed_worker_mismatch`, `control_plane_mismatch`, or the adapter's
own shape-checked fixed code. A receipt that cannot be stored after the
adapter ran raises `receipt_unwritable` (exit 3), never a refusal.

## One-shot and concurrency

Each `request_id` dispatches at most once across concurrent processes
(`O_CREAT|O_EXCL` claim under an exclusive state-dir lock). A replay, a
dispatch crash, a timeout and an invalid receipt all leave the claim
consumed; a new approved request id is required. At most 128 live claims;
claims expired more than a day are pruned unless a receipt already
attests their id (an attested id stays claimed, so a replay after the
prune window still refuses instead of executing twice). Non-regular
entries under a claim name (FIFO, directory, ...) are never opened for
reading or pruned; they count toward the live cap so a stuffed store
refuses loudly instead of hanging. Anything else refuses with
`state_exhausted`.

## Receipt-side rechecks

The carrier independently rechecks the adapter's report before attesting
it: at most two `runtime_observations` in the fixed `health`-then-`readyz`
order with early stop after a non-200 first surface; each observed worker
version must equal the manifest target; a passing report must present
exactly the health+readyz pair with both statuses 200 and carry a
`control_plane` binding exactly equal to the manifest targets. Only
allowlisted metadata bindings (the eight control-plane identity keys and
per-surface status) reach the dispatch receipt; every other adapter-emitted
key refuses it. The adapter's own fixed metadata surface stays exactly its
four GET-only reads (Worker deployments, application list, pinned rollout
list row, pinned rollout row); this source adds no endpoint, token, role
or write.

## Install preconditions

1. Review the exact carrier bytes and the exact adapter bytes at pinned
   digests; record both digests and the review identity out of band.
2. Materialize the fixed paths above with root ownership and the stated
   modes; the state directory is service-owned mode 0700 and empty.
3. Write the manifest with `"enabled": false` first and confirm every
   dispatch refuses with `carrier_disabled`; flip to `true` only under the
   separate activation decision with a short future approval bound.
4. No network access, credential fetch, policy change or service restart is
   part of installing this source.

## Disable and rollback

- Disable: set `"enabled": false` in the installed manifest (or remove the
  manifest); confirm a dispatch refuses with `carrier_disabled` /
  `manifest_unreadable`.
- Remove the new artifact: delete the carrier entry, the adapter file, the
  manifest and the state directory; confirm no fixed path remains and the
  entrypoint is gone. Rollback touches nothing else on the host.
- Rollback of a dispatch is not applicable: the adapter performs read-only
  observation only; a failed dispatch leaves the one-shot claim and its
  failure receipt as evidence.

## Conformance entry

```sh
python3 -B ./test_host_ops_staging_preflight_carrier.py
```

The suite stages fixture install roots under mktemp with a synthetic
test-only adapter, a fake clock and a fake authority environment, and
traverses the same dispatch function the installed entrypoint uses. It
covers refusals with no adapter execution or state mutation, the one
positive fixed-path dispatch, one-shot
concurrent replay, expiry, stale hashes/identity, the two-GET
ceiling/order/early-stop recheck, bounds, the metadata allowlist, receipt
minimization/redaction and queue-ack-vs-dispatch distinction. A passing
suite never establishes installed execution.
