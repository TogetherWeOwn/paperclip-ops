# Workspace sync-back size: alert-only observer

This external observer does not select, copy, stop, clean or move workspaces.
It does not modify SSH transport, Paperclip core, allocation policy, CI gates or
readiness. No timer, host install, endpoint activation or rollout is included.
**Never put it in a transport/execution gate or use its exit status to admit or
refuse work.** It reports observations, not deployment approval.

## Measurement contract

Feed one sanitized JSON object on stdin:

```json
{"schema_version":1,"measurement":"sync_back_payload_bytes","complete":true,"bytes":2000000001}
```

`bytes` is the complete payload byte count selected for one workspace sync-back,
not aggregate run-folder size, allocated disk usage, rounded GiB, free disk,
network protocol overhead, or a partial count. The external producer must establish
that meaning and completeness; this observer cannot prove a producer's assertion.
It does not walk paths or inspect files. The producer must not send file contents,
credentials, environment dumps, command lines or private paths. Additional fields
are ignored and never copied to the report or alert.

The schema version is the integer `1`, measurement is the exact string above,
and completeness must be the boolean `true`. The count must be a JSON integer
from 0 through 9,007,199,254,740,991 (the exact-integer limit of JS alert consumers).
Numeric strings, booleans, floats, negative/out-of-range counts, missing fields,
unsupported measurements, duplicate keys and invalid/nonfinite JSON are unknown.
Input must be strict UTF-8. Unpaired surrogate escapes anywhere in keys or values
(including ignored/nested fields) are unknown; valid escaped pairs are accepted.
Input is bounded to 4096 bytes; oversized, unreadable or empty input is unknown.
No raw input or parser exception is printed.

## Boundary and output

```sh
python3 ops/paperclip/workspace_sync_back_signal.py < sanitized-observation.json
```

The output schema is `workspace-sync-back-signal.v1`. It includes the byte count
(or null), fixed threshold/unit, `measurement_qualified`, `status`, `reason`,
`alert_required`, fixed-format `message` and a `gatus` projection. Every result
has `alert_only: true` and `blocks_execution: false`.

| Complete integer bytes | Status | Alert required | Gatus success |
| --- | --- | --- | --- |
| 0 through 1,999,999,999 | `within_threshold` | false | true |
| 2,000,000,000 | `within_threshold` | false | true |
| 2,000,000,001 and above, within supported range | `oversized` | true | false |
| Unknown/malformed/incomplete | `unknown` | true | false |

The threshold is strictly **greater than 2,000,000,000 bytes**, decimal **2 GB**
(1 GB = 1,000,000,000 bytes). Equality is not oversized. 2 GiB = 2,147,483,648
bytes and is oversized. An unknown observation is explicitly nonqualifying,
never zero or healthy. `within_threshold` is only a size observation, not a
passing rehearsal, security check, transport health result or rollout verdict.
The classifier returns 0 for all observations, including oversized and unknown.

## Reuse the existing alert sink

`ops/paperclip/push-workspace-sync-back.sh` consumes the same stdin and reuses
`ops/case_658bf6b9/_gatus_push.sh`; it introduces no alternate credentials or
HTTP implementation. Invoke it with `bash` as an **independent observer** only.
Gatus URL/token configuration remains private runtime environment, following the
shared helper's existing credential contract. Neither is emitted by this wrapper.

The fixed endpoint key is `platform_workspace-sync-back`. A valid within-threshold
measurement sends `success=true`; oversized and unknown send `success=false`
with only the fixed-format sanitized message. This permits downstream recovery
without treating an unknown observation as healthy. Successful delivery exits 0
regardless of the size/unknown classification. Nonzero means observer or alert
**delivery** failed, not that sync-back is forbidden. Stdout retains classification
JSON; stderr reports only fixed delivery state/remediation. Classification alone
is never proof of delivery. No retries, mutation, cleanup or execution coupling
are added.

An authorized installer must separately configure a matching external endpoint,
existing alert routes and an appropriate missing-observation heartbeat. No example
heartbeat interval is prescribed here: event timing/producer availability has not
been qualified. Until that endpoint and complete measurement producer are verified,
this source is **not qualified as a live monitor**. Do not reuse the aggregate
run-folder monitor or label its measurement as sync-back payload bytes.

## Verification and qualification

Boundary/error checks must cover both classifier JSON and stubbed sink requests:
below/equal/above, decimal versus binary units, zero, incomplete/missing/wrong-type
counts, duplicate/nonfinite/malformed/oversized input, no raw-input disclosure,
unknown never healthy, and sink failure distinct from classification. Run only
offline fixtures for source verification; never use real credentials or send
synthetic failures to a live alert endpoint.

A source/fixture pass does not qualify an official runtime artifact. Qualification
still needs an exact producer/artifact identity, two consecutive required passing
rehearsals (the second on the final image), end-to-end alert/recovery evidence,
independent review and the governing rollout decision. This observer grants none
of those. Host installation, transport instrumentation and rollout belong to
separate authorized work.
