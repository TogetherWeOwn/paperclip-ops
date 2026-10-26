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
