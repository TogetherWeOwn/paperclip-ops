# Native Runner coverage classifier

**Source-only candidate. Not installed, wired, or a credential scanner.**

A small native plugin tool classifies sanitized metadata supplied by an existing
security hygiene routine. It cannot establish that credentials are clean, usable,
revoked, or removed. Its only statuses are `UNKNOWN`, `ADVERSE`, and `REFUSED`.
There is deliberately no safety-green result.

## Why this boundary

The existing routine supplies execution and cadence. The inspected native plugin
SDK has agent reads and issue run summaries, but no native execution-session,
backend, store, or credential-lifetime reader. Agent configuration identifies an
adapter, not an observed execution path. Plugin conversational task sessions are
not native provider sessions. A new host helper or a broader read grant would not
be a justified substitute for that missing interface.

This plugin therefore has **no data-read, HTTP, secret, filesystem, database,
scheduler, wake, or mutation capability**. Its manifest declares only
`agent.tools.register`, necessary to expose its single synchronous tool. Source
review is not permission to install it, grant that capability, configure it, or
change a routine. It adds no new package dependency or service. The entrypoint
uses the host's existing `@paperclipai/plugin-sdk` at a separately reviewed rollout.

The tool checks a trusted company-scoped configuration against the invocation's
company, then checks the supplied company/agent/run/session bindings for
self-consistency. **It does not authenticate caller-supplied metadata.** A
self-consistent report is not independent evidence of an actual server binding.
Results say `bindingCheck: REPORTED_CONSISTENT`, not verified. The tool preserves
`source` inference and normalizes all other caller provenance to `reported`;
it never promotes source-only store claims. The pure classifier additionally
supports `synthetic` for offline fixtures. None is measured runtime evidence.
No value supplied by a caller can request a measured result.

## Configuration and invocation

The host's company-scoped plugin configuration must contain exactly one
`companyId` and exactly three distinct `agentIds`. There is no default company,
fleet enumeration, wildcard, caller-supplied scope, or fallback configuration.
The historical manifest field name is `instanceConfigSchema`; runtime access is
`ctx.config.get(runCtx.companyId)` inside the handler, not an unscoped setup read.
Real policy values belong in private reviewed configuration, never in this repo.

Tool: `native_runner_coverage`.

Input is a **projection**, not raw API responses or files:

```json
{
  "binding": {"companyId":"example-company","agentId":"example-agent-a","runId":"example-run","sessionId":"example-session"},
  "agent": {"id":"example-agent-a","companyId":"example-company","adapterType":"paperclip_runner"},
  "run": {"id":"example-run","companyId":"example-company","agentId":"example-agent-a","status":"failed","sessionId":"example-session"},
  "observation": {
    "basis":"reported",
    "binding":{"companyId":"example-company","agentId":"example-agent-a","runId":"example-run","sessionId":"example-session"},
    "provider":"codex",
    "target":"local",
    "lifecycle":"recovery",
    "stores":{"mcpHeader":"present","sqlite":"present"},
    "lease":{"expiresAt":"2026-01-01T00:00:00Z","revoked":false}
  }
}
```

The pure export is `classifyCoverage({scope,binding,agent,run,observation?,now})`
in `classifier.mjs`. `now` is a UTC ISO timestamp, supplied by the tool's clock,
not by tool callers. IDs in output are deliberately omitted; the routine owns the
private association between its invocation and the returned enums.

`run.sessionId` is the routine's explicit projection of a nonempty native session
ID from an already-authorized observation. Do not substitute the current
observer's run, a display name, a controller directory, a hashed slot name, or a
plugin conversational task-session ID. A receipt also needs that exact session
ID. Missing joins remain `UNKNOWN`; conflicting IDs are `REFUSED`. In particular,
a company run-list summary does not expose a native session ID and cannot alone
satisfy that join. A failure before a native session identity is available remains
unknown, even when a startup-only path is plausible.

Do not pass adapter/runtime config, provider profiles/checkpoints, raw paths,
headers, tokens, digests, transcript contents, errors/stacks, or log bodies. Output
is an allowlisted projection and never echoes these objects or identifiers.

## Classification

All eight retention classes are always present: `controller`, `journal`,
`providerState`, `mcpHeader`, `transcript`, `sqlite`, `runLog`, `optionalTrace`.
Their values are `UNKNOWN`, `PRESENT`, or `NOT_FOUND_IN_OBSERVED_SCOPE`.
`not_found` is an observation only of the reported local scope, never global
absence or a content-clean result. Unspecified, remote, custom, or source-only
stores stay `UNKNOWN`. No content is read to produce any of these enums.

| Reported, consistently bound local facts | Path classification | Status |
|---|---|---|
| Dot controller present; journal not found in inspected scope | `DOT_STARTUP_ONLY` | `UNKNOWN` |
| Dot journal and provider-state present | `DOT_RETAINED` | `UNKNOWN` |
| Native Codex MCP header present | `CODEX_LOCAL_MCP_HEADER` | `ADVERSE` |
| Incomplete/unobservable path or source-only inference | `UNKNOWN` | `UNKNOWN` |
| Conflicting company/agent/run/session binding | `UNKNOWN` | `REFUSED` |

A retained header remains adverse in warm, detached, recovery, terminal, expired,
and revoked-record cases. Private file modes or an expired record are not an
accepted credential exception. `DOT_STARTUP_ONLY` does not prove that execution
never reached another store or root; `DOT_RETAINED` is retention, not disclosure.

`live`, `warm`, `detached`, `recovery`, and `terminal` describe separately reported
session lifecycle. A terminal heartbeat status alone never closes the provider.
Lease results are only `unexpired_record`, `expired_record`, `revoked_record`, or
`UNKNOWN`. Invalid/missing timestamps are unknown; a recorded revoke is not a
live revocation test. Controller, environment, PRP, MCP gateway, and external
provider authorities are different lifetimes and must not be conflated.
`authority` is always `UNKNOWN`, `contentScan` always `NOT_PERFORMED`, and backend
is always `UNKNOWN` because this slice has no backend observation contract.

## Versioned results and remediation

Every classifier and tool outcome, including invocation/config failures, has
`schema_id: native-runner-coverage-result.v1`. The tool returns `{content, data}`:
`content` is the JSON serialization of `data`, with the same semantics.
[`result-contract.mjs`](result-contract.mjs) exports the exact JSON Schema
`RESULT_SCHEMA` (all fields required, no extra properties) and the immutable
`REMEDIATION_CATALOGUE`. This is the existing plugin's contract, not a new registry.

| Field | JSON type and allowed values |
|---|---|
| `schema_id` | string: `native-runner-coverage-result.v1` |
| `status` | string: `UNKNOWN`, `ADVERSE`, `REFUSED` |
| `reasonCodes` | nonempty array of unique strings from the complete mapping below; existing order preserved |
| `pathClass` | string: `UNKNOWN`, `DOT_STARTUP_ONLY`, `DOT_RETAINED`, `CODEX_LOCAL_MCP_HEADER` |
| `evidenceBasis` | string: `metadata`, `reported`, `source`, `synthetic` (pure fixtures only) |
| `lifecycle` | string: `UNKNOWN`, `live`, `warm`, `detached`, `recovery`, `terminal` |
| `leaseState` | string: `UNKNOWN`, `unexpired_record`, `expired_record`, `revoked_record` |
| `authority`, `backend` | string: always `UNKNOWN` |
| `contentScan` | string: always `NOT_PERFORMED` |
| `target` | string: `UNKNOWN`, `local`, `remote`, `custom` |
| `bindingCheck` | string: `REPORTED_CONSISTENT`, `INCOMPLETE`, `REFUSED` |
| `stores` | object: exactly the eight retention keys above, each `UNKNOWN`, `PRESENT`, or `NOT_FOUND_IN_OBSERVED_SCOPE` |
| `nextActions` | nonempty unique array of objects with exactly `reasonCode`, `action`, `instruction` strings |

There is exactly one action per reason, in `reasonCodes` order. Each instruction
is the fixed string associated with that action below, never supplied metadata,
IDs, paths, errors, configuration or content. Structural schema validation alone
does not prove this correspondence: consumers also check each entry against
`REMEDIATION_CATALOGUE[result.reasonCodes[index]]`. Advice does not change status
or provenance, execute recovery, grant access or establish an observation.

| Action | Exact fixed instruction |
|---|---|
| `CORRECT_SUPPLIED_METADATA` | Correct supplied metadata only from facts already available within the authorized scope; never invent missing observations. |
| `USE_AUTHORIZED_NATIVE_SURFACE` | Use only an already-authorized native metadata surface if it exposes the missing fact; otherwise keep it UNKNOWN and report the unsupported boundary. |
| `PRESERVE_UNKNOWN_AND_REPORT` | Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration. |

Complete reason-to-action mapping (including malformed inputs and worker overrides):

| Reason code | Action |
|---|---|
| `BACKEND_UNOBSERVABLE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `AUTHORITY_NOT_TESTED` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `CONTENT_NOT_SCANNED` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `COVERAGE_INCOMPLETE` | `USE_AUTHORIZED_NATIVE_SURFACE` |
| `INVALID_INPUT` | `CORRECT_SUPPLIED_METADATA` |
| `INVALID_OBSERVATION` | `CORRECT_SUPPLIED_METADATA` |
| `INVALID_POLICY` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `COMPANY_MISMATCH` | `CORRECT_SUPPLIED_METADATA` |
| `AGENT_MISMATCH` | `CORRECT_SUPPLIED_METADATA` |
| `ADAPTER_MISMATCH` | `CORRECT_SUPPLIED_METADATA` |
| `RUN_MISMATCH` | `CORRECT_SUPPLIED_METADATA` |
| `SESSION_MISMATCH` | `CORRECT_SUPPLIED_METADATA` |
| `MISSING_BINDING_METADATA` | `CORRECT_SUPPLIED_METADATA` |
| `MISSING_SESSION_JOIN` | `USE_AUTHORIZED_NATIVE_SURFACE` |
| `MISSING_RECEIPT_BINDING` | `CORRECT_SUPPLIED_METADATA` |
| `UNSUPPORTED_EVIDENCE_BASIS` | `CORRECT_SUPPLIED_METADATA` |
| `SOURCE_ONLY` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `SYNTHETIC_FIXTURE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `REPORTED_NOT_MEASURED` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `STORE_SCOPE_UNOBSERVABLE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `RETAINED_PROVIDER_STATE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `RETAINED_MCP_HEADER` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `MISSING_INVOCATION_COMPANY` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `CONFIG_UNAVAILABLE` | `PRESERVE_UNKNOWN_AND_REPORT` |
| `INVOCATION_COMPANY_MISMATCH` | `PRESERVE_UNKNOWN_AND_REPORT` |

Malformed supplied metadata can be corrected only from already-available facts.
A missing session join needs an actually exposed, already-authorized native fact,
not a guessed identifier. Invalid/unavailable policy or invocation context is an
unsupported boundary, not advice to change configuration. No hint authorizes
credential acquisition, raw-store reads, helpers, schedule changes or scope bypass.

Complete synthetic example of tool `data` for consistent binding metadata with
no observation (not a live observation or measured recovery result):

```json
{
  "status": "UNKNOWN",
  "reasonCodes": ["BACKEND_UNOBSERVABLE", "AUTHORITY_NOT_TESTED", "CONTENT_NOT_SCANNED", "COVERAGE_INCOMPLETE"],
  "pathClass": "UNKNOWN",
  "evidenceBasis": "metadata",
  "lifecycle": "UNKNOWN",
  "leaseState": "UNKNOWN",
  "authority": "UNKNOWN",
  "contentScan": "NOT_PERFORMED",
  "stores": {
    "controller": "UNKNOWN", "journal": "UNKNOWN", "providerState": "UNKNOWN", "mcpHeader": "UNKNOWN",
    "transcript": "UNKNOWN", "sqlite": "UNKNOWN", "runLog": "UNKNOWN", "optionalTrace": "UNKNOWN"
  },
  "backend": "UNKNOWN",
  "target": "UNKNOWN",
  "bindingCheck": "REPORTED_CONSISTENT",
  "schema_id": "native-runner-coverage-result.v1",
  "nextActions": [
    {"reasonCode": "BACKEND_UNOBSERVABLE", "action": "PRESERVE_UNKNOWN_AND_REPORT", "instruction": "Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration."},
    {"reasonCode": "AUTHORITY_NOT_TESTED", "action": "PRESERVE_UNKNOWN_AND_REPORT", "instruction": "Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration."},
    {"reasonCode": "CONTENT_NOT_SCANNED", "action": "PRESERVE_UNKNOWN_AND_REPORT", "instruction": "Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration."},
    {"reasonCode": "COVERAGE_INCOMPLETE", "action": "USE_AUTHORIZED_NATIVE_SURFACE", "instruction": "Use only an already-authorized native metadata surface if it exposes the missing fact; otherwise keep it UNKNOWN and report the unsupported boundary."}
  ]
}
```

Source-only tests validate every reason, the exact schema/catalogue and this
example; they prove no installed-consumer or operational recovery benefit.

## Helper-free routine wiring candidate (not applied)

Only the routine owner may accept this step and any separately authorized rollout.
Preserve existing triggers, holds and checks. Do not replace or broaden an
existing redactor's coverage claim.

> For each of the three privately configured agents in the one configured company,
> use only existing authorized native metadata observations. Make a minimal agent
> projection (id, companyId, adapterType), bound run projection (id, companyId,
> agentId, status, native session ID if actually available), and an expected
> binding. Never enumerate other companies or host roots. If no native session ID
> is observable, omit it and keep the result UNKNOWN.
>
> If a previously authorized metadata-only receipt exists for that exact binding,
> supply only its provider/target/lifecycle/store-presence/lease-record enums. Do
> not fetch config, credentials, transcripts, SQLite contents, traces, or logs to
> fill holes. No receipt means omit observation, not fabricate absent stores.
>
> Invoke the native `native_runner_coverage` tool once per bound projection. Keep
> the private agent/run association in the routine's task document. Report both the
> evidence basis and unsupported facts. An absent tool, missing config, unavailable
> metadata surface, refused binding, or tool error is an adverse/UNKNOWN coverage
> gap, never a passed hygiene check. Do not install a helper, add a schedule,
> modify stores, retry with another credential, or grant access from this step.

This is an instruction candidate, not a deployment recipe. The routine may
perform the same bounded metadata reasoning without a plugin, but source-only
claims must remain visibly separate from actual observations.

## Exact unsupported facts

- Native backend, actual execution target, alternate/custom/remote roots and
  complete run/session inventory. Reported target is not independently measured.
- Filesystem presence, mode, owner, custody, path safety and retention after close.
- Transcript, journal, provider-state, SQLite, run-log and optional-trace contents;
  no content-clean, no-secret or no-disclosure claim. No rollout files does not
  mean no transcript copies.
- Credential acceptance, gateway DB revocation, provider stop, cleanup completion,
  cross-process/agent OS isolation, and any end-to-end lifecycle property.
- Freshness or authenticity of supplied historical receipts. The wrapper forces
  reported provenance; it cannot promote a caller's assertion to measured proof.
- Source/live-process equivalence; the source trace below is not binary attestation.

## Source contract trace

These anchors were inspected in source snapshot
`bba6a45f0d364cfd962ac59b3bdf2426a8222adb`, not a deployed binary. They establish
API shape and possible behavior, not runtime retention or credential acceptance:

- `packages/plugins/sdk/src/types.ts:266-289,445-451,998-1016`: tool invocation
  company context, company config, registration and result contract.
- `packages/plugins/sdk/src/types.ts:1277-1288,1333-1343,1377-1387`: bounded
  issue/subtree run summaries. Company scope comes from the wrapper/query; the
  summary has no native session ID, backend, store or credential-lifetime facts.
- `packages/plugins/sdk/src/types.ts:1656-1695,1723-1751,2127-2235`: agent reads,
  separate conversational task sessions, and the typed host context. No dedicated
  native inventory/store reader is exposed. Generic HTTP/database/local-folder
  capabilities are not a substitute and are not requested by this plugin.
- `server/src/services/plugin-host-services.ts:1090-1123,1420-1426,3220-3273`:
  company/issue-filtered summaries limited to 100, config lookup and separate
  plugin conversational sessions.
- `server/src/services/plugin-registry.ts:290-296` and
  `packages/plugins/sdk/src/host-client-factory.ts:650-668,695-710`: company-scoped
  config lookup and invocation-company enforcement.
- `server/src/services/plugin-capability-validator.ts:200-213`: declaring tools
  requires `agent.tools.register` even without data access.
- `server/src/services/native-runtime/native-session-executor.ts:1588-1599,1650-1666,10839-10845,10900-10911`:
  scoped native storage, configurable roots and remote roots. A default-root miss
  is not global absence.
- `packages/paperclip-runner/src/drivers/runtime-context-materializer.ts:309-365`:
  an isolated native Codex home can materialize an MCP header and provider
  credentials. This does not establish observed retention or usability.
- `server/src/services/native-runtime/managed-native-credentials.ts:17-76`: warm
  and detached ownership differs from owned close/copy-back/removal. This is not
  an executed cleanup or revocation test.
- `doc/architecture/paperclip-runner.md:138-164`: server attribution versus runner
  supervision, and PRP bootstrap/connection-lease binding. PRP documentation is
  not proof of external provider shutdown.

This snapshot has no Dot broker implementation. Dot-specific journal/controller
classifications are supplied-report vocabulary and synthetic controls only, not
independently verified Dot source or runtime observations. Revalidate the SDK
contract against the actual host version before any separately authorized rollout.

## Offline verification

```sh
node --test plugins/native-runner-coverage/test/*.test.mjs
```

All fixtures must be synthetic. Tests exercise classifier behavior and the tool
boundary, not live DB/provider revocation, real bearer replay, filesystem scans,
or the effectiveness of any deployed control. Rollback of this uninstalled source
slice is a source revert; no runtime or credential action is part of it.
