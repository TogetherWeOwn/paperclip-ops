# Read-only access-gap collector — v1

This is reviewed source for an additive `run-eval-harvest` extension. Publication
is not installation, routine activation, a grant, a routing verdict or evidence
that access improved. The native routine still owns card creation, independent
proposal review and implementation admission. No scheduler, host inventory,
database connection, provider request, secret lookup or mutation is included.

## Entry points and evidence custody

- `evaluateAccessGaps(input)` in [access-gap.mjs](access-gap.mjs) is pure:
  deterministic versioned JSON, no clock, environment, filesystem or network.
- `collectAccessInputs(options)` in [access-gap-collect.mjs](access-gap-collect.mjs)
  accepts an injected GET function and the existing M26 `collectInteractions`
  function. It enumerates all interactions by **creation** time, including
  accepted/rejected/pending and closed-card interactions; it does not change M26
  routing semantics. M26 must enumerate hidden issues and all relevant pages.
- [access-gap-cli.mjs](access-gap-cli.mjs) supports:

  ```sh
  node ops/eval/access-gap-cli.mjs --from-file frozen-input.json
  # Only after separate native skill installation, from its scripts directory:
  node access-gap-cli.mjs --collect 2026-01-01T00:00:00Z --metadata metadata.json
  ```

  The day must already be complete. Windows are half-open `[since,until)`, UTC
  midnight boundaries; the pure API also accepts multiple whole UTC days.
  `asOf` cannot precede `until`. Stock is observed at `asOf`, not reconstructed
  from daily flow. Emit explicit UNKNOWN when its snapshot is incomplete.
  Exit 0 means accounting completed, not evidence sufficiency or authorization.
  Exit 2 is a generic refusal; raw error messages never reach stdout/stderr.

The CLI reuses the existing bounded duplicate-key-rejecting `parseFrozen` parser
from [delivery-effectiveness.mjs](delivery-effectiveness.mjs). Its input files
must be regular, non-symlink files, valid UTF-8, at most 2 MiB and depth 32.
Streams have at most 20,000 records before duplicate removal; parser arrays have
its stricter existing 10,000-record bound. Duplicate IDs with identical payloads
collapse; conflicting IDs refuse the input. Stable JSON object-key ordering is
lexical; arrays preserve semantic order. Record sets are explicitly sorted before
hashing; unlike the delivery scorer's canonicalizer, arrays are not all bags.
Hashes are SHA256 over UTF-8 JSON, no trailing newline. IDs use ASCII letters,
digits and `_.:/@-`, maximum 256 characters.

## Frozen input and metadata

Required top-level fields:

```text
schema: "access-gap-input.v1"
window: { since, until }
asOf, operatorId, newCardsAlreadyCreated: integer 0..3
interactions, runs, toolOutputs, cards, cardEvents, incidents, providers: [records]
coverage: { <each stream>: receipt }
legacyMetrics?: { existing metric keys and unchanged values }
legacyCoverage?: { status, ...producer evidence }
```

A receipt is `{since,until,complete,truncated,errors,pages,revision,sourceSha256,
recordCount,recordsSha256}`. `pages` must be every consecutive page number from
1, including one successful empty page for an empty result. `recordCount` and
`recordsSha256` bind the deduplicated sorted records, using `hash` exported by
`access-gap.mjs`; sort canonical record JSON lexically with the declared `en`
comparison. `sourceSha256` is the producer artifact hash; `revision` names its
version. These pins are supplied provenance, not cryptographic authentication or
proof that the producer's inventory was honest. Independently verify native
receipts, official schema provenance and policy metadata before consuming any
proposal. A boolean alone does not authenticate authority.

For native collection, metadata uses `schema: "access-gap-metadata.v1"`,
`operatorId`, `newCardsAlreadyCreated`, optional `interactionAnnotations`,
`cardAnnotations`, `expectedRefusalCalls`, and the last three streams plus their
coverage receipts. The collector does not obtain credentials to fill missing
provider metadata. Missing metadata is UNKNOWN, not a reason to hand-count.
Never put credentials, connection strings, customer rows or unrestricted native
responses in metadata. Persist sanitized output only; frozen raw log input is
sensitive and is not a public CI artifact.

Record contracts:

- Interaction: `{id,createdAt,addresseeUserId,status,kind,issueId?,sourceRunId?,
  requestedByAgentId?,requestEvidence?}`. `requestEvidence` is independently
  authored `{sourceInteractionId,complete,kinds}`. Only one supported kind
  establishes a category: `scoped_tool_use` → `agent_doable_with_access`,
  `host_os` → `host_only`, `board_setting` → `board_only`,
  `credential_create_rotate` → `owner_credential`. Missing, unsupported or mixed
  kinds → `unknown-mixed`. Free text, title prefixes and quoted rules never
  establish agent-doable intent. This does not decide semantic routing or
  authorize the request. Platform connection requests remain separately marked.
- Run: `{id,agentId,createdAt,logBytes?}`. Optional `logBytes` is the native
  terminal UTF-8 byte count; when supplied, EOF must match it. Tool output:
  `{id,runId,callId,at,origin,text,
  instructionRead,expectedRefusal,transportStatus?,isError?}`. Only
  `origin: "tool_result"` is eligible. Wrapper completion does not prove HTTP
  success: a completed curl may still report 403. JSON record collections and
  nested record messages are data, not failure diagnostics. Only top-level error
  envelopes, HTTP error statuses or explicitly failed calls' diagnostic fields
  establish structured error evidence. Mere quota inventory is not a quota failure;
  the rule requires an exhausted/exceeded/insufficient condition.
  The parser accepts ACP completion/failure and stream-json `tool_result`
  blocks, not user text/model prose. Two passes join source-read and test-call
  exclusions even when completions arrive first. Instruction/source reads are
  excluded conservatively, including namespaced `functions.Read` and
  `mcp__filesystem__Read`, `read_file`, `read_text_file` and `read_multiple_files`
  identities, without matching unrelated provider readers. Shell source detection
  follows `env`/`command` prefixes, control keywords and redirections to the invoked
  program; wrapper option values, loop-header words, redirection targets and quoted
  API arguments do not themselves identify source reads. Literal heredoc bodies
  for non-shell consumers are skipped as data, including multiple heredocs and
  tab-stripping `<<-`; unquoted bodies without `$`, backticks or backslashes are
  also literal. Shell interpreters in program position (stdin, here-strings, pipes,
  arguments or script files), unquoted expansion-bearing bodies and incomplete
  heredocs are unsupported: their results are excluded and log coverage becomes
  UNKNOWN rather than claiming a measured zero. Active command substitutions
  (`$(...)` and backticks) are unsupported, including inside double quotes.
  Backslash-newline continuations join shell words and expansion openers before
  syntax recognition; single-quoted text and literal heredoc bodies are not rewritten.
  Simple arithmetic shifts are expressions, not heredoc operators. Supported
  arithmetic contains only decimal digits, whitespace, parentheses and numeric
  operators; variable names, array subscripts and other active arithmetic are
  UNKNOWN because recursive evaluation may execute substitutions. Alternate
  arithmetic `$[...]`, parameter expansions `${...}` (including array subscripts
  and substring offsets), arithmetic commands `((...))` and `let` in program
  position are unsupported, even when a particular expression appears literal.
  Nested quotes that split an active expansion across lexical fragments also remain UNKNOWN,
  while single-quoted or escaped substitution/arithmetic text stays literal.
  Valid string message content is prose, not tool evidence or a parse loss.
  Malformed message-content containers or blocks are counted as parse losses
  without discarding valid frames or blocks in the same run.
  This is a conservative lexical filter,
  not a shell interpreter or proof of arbitrary command semantics. Declared
  expected-refusal calls and test commands stay diagnostic. Unrecognized/raw frames make log coverage UNKNOWN rather
  than being guessed into tool results. Raw output is hashed but never emitted;
  run/call IDs locate evidence in its native access-controlled source.
- Card snapshot: `{id,status,accessRelated,gapKey?,group?,gapClass?,agentId?,
  targetScope?,tier?}`. Native routine/producer supplies reviewed annotations,
  not regex guesses from a title. Native identity/status always win over
  annotations; missing boolean `accessRelated` annotations make native stock
  coverage UNKNOWN. Every relevant nonterminal card also needs a valid modern
  `gap:<sha256>` key or a legacy group/class identity, plus agent/scope/tier for
  grants, before native card coverage can certify proposal dedupe. A stock boolean
  alone is insufficient; unknown dedupe readiness defers proposals and updates.
  Stock uses unique blocked/deferred/backlog
  access-related cards; flow uses separate status-event IDs, never added to
  stock or failures as if disjoint populations.
- Card event: `{id,cardId,kind,at,accessRelated?,authorized?,success?,version?,
  tier?,agentId?}`. Opened/merged/enabled/adopted remain separate ledger states.
  Adoption needs a successful authorized use and matching earlier authorized
  enabled evidence for the same card, version, agent and tier. A merged label
  does not imply enabled/adopted. Unauthorized or explicitly failed enablement
  proves neither state. Incomplete event coverage defeats evidence.
- Provider: `{id,group,package,packagePresent,inventoryComplete,tools,
  policyComplete,policyRevision,grants,capabilityMap,snapshot}`. Tool entries are
  `{operation,name}`; grant metadata is `{agentId,scope,tier,operationIds,
  eligible,granted,prohibited}`. No credential values or implicit permission.
  Capability map `{revision,sha256,sourceUrl,operations}` operations name
  `{id,scope,tier,entitled,tool,status}`; tiers `read|write|reserved`, statuses
  `implemented|reviewed|enabled` or explicitly incomplete. Snapshot
  `{revision,sha256,sourceUrl,operations:[operationId],previousSha256?,
  addedOperations?:[operationId]}` names the official schema snapshot. Missing
  map/snapshot/inventory/policy and unmapped operations prevent complete coverage.
  Every mapped operation and declared addition must belong to the current
  official snapshot; mismatches are UNKNOWN and refuse proposals. Implemented,
  reviewed and enabled counts join the exact installed operation/tool pair in a
  present package, not map status alone. Counts are reported separately per tier;
  incomplete inventories remain UNKNOWN even if some installed tools are observed.
- Incident: `{id,at,group,operation,targetScope,tier,agentId,schemaRevision?,
  evidenceKeys,validation}`. Evidence keys join `interaction:<id>`, `tool:<id>`
  and `card:<id>`. Validation `{completeRequest,currentCapabilityPolicy,revision,
  materialReproduced?,ownerSeed?}` must be independently source-linked. It is
  evidence, never an approval, grant or assertion that a policy refusal is wrong.

The legacy metrics object and its coverage are transported separately, not
recomputed or redefined. No legacy source file is modified. With no supplied
harvest output, legacy coverage is UNKNOWN. Do not infer legacy metrics from the
new detector; its classification filters intentionally differ.

## Proposal and dedupe contract

Only validated, joined, covered incidents can produce proposal inputs:

- Missing package: one **full-surface / all-tier** package candidate per group,
  not many endpoint cards. Known incomplete implementation is reported, not
  confused with missing schema/policy evidence.
- Missing grant: an existing typed tool plus explicitly eligible, not granted,
  not prohibited metadata, scoped to the affected **agent + resource + tier**.
  Route is connection/config authorization, never a new tool or automatic grant.
- Drift: additions in a changed pinned official snapshot extend the existing
  package. One **full-surface / all-tier / schema-revision** update covers the
  added operations; event-level evidence and individual scopes remain in the
  source incidents, not separate function cards. Unchanged/unknown snapshots do
  not establish drift. A valid previous snapshot hash is required, and additions
  already implemented by their exact installed tool do not create drift work.
  Conflicting metadata for one agent/resource/tier/operation makes policy UNKNOWN.

`gapKey` hashes `[group,class,targetScope,tier,grantAgentOrNull,driftRevisionOrNull]`.
Existing nonterminal cards match exact keys or legacy group/class records;
modern `gap:` keys cannot fall back across different schema revisions. Legacy
grant matches also require exact agent/resource/tier. Unknown existing
card coverage defers proposals and updates. Matches emit evidence updates, not
new cards. Rank affected agents, then distinct runs, then stable gap key. Apart
from independently reproduced material gaps or an authorized owner seed, require
two distinct source events (same-run streaming repeats count once). All classes
share a maximum **3 new cards per daily execution**, less
`newCardsAlreadyCreated`. This is a proposal cap, not an atomic card-creation
lock: the native routine must reread current cards/counts immediately before
creating cards, avoid concurrent duplicate executions and persist native IDs.

Discovery faults, quota and expected/policy refusals never propose wider access.
A 403 without causal evidence is `unknown_access`, not automatically a missing
grant. Diagnostics report unique interaction/run/card units, total denominators,
unknown category fraction and non-additive per-kind runs. Observed lower bounds
are labeled `observed`; incomplete denominators yield null rates, not zero.

## Network/resource boundary and threat model

The CLI resolves only its injected Paperclip API credential at call time and
uses it solely in GET Authorization headers. It returns no credential, fetches
no secret values and logs no raw responses. Allowed paths are this company's
agents/issues/runs, issue interactions and run logs; metadata never chooses the
host or a continuation URL. Redirects, URL credentials, fragments and unexpected
base paths are refused. There is no HTTP passthrough, arbitrary module loading,
provider HTTP executor, SQL, writes or privileged fallback.

Per collection: 500 maximum network reads, 16 MiB aggregate responses, 2 MiB per
response, two-minute request-start deadline, 15-second per-request timeout,
20,000-record scan ceiling, an independent 20,000-unique-tool-result ceiling,
and at most 40 × 1 MiB log pages per run (defaults 10 × 250 KiB). Results are
retained in stable run/result order up to the ceiling; overflow sets UNKNOWN
with bounded observations instead of refusing the report. Request caching avoids
duplicate M26/list reads. No retry loop or
long-lived worker; cancellation/exit leaves no resource or timer to tear down.
The existing run route's 1,000-row cap, missing EOF proof, failed/truncated pages,
unknown JSON event types, unjoined run IDs, frame parse losses and scan limits
make coverage UNKNOWN. Wrapper chunks are reassembled per stream before parsing.
A short page/chunk is not
log EOF proof. Each numeric continuation must equal the accumulated UTF-8 byte
count, including on a terminal page; a supplied native terminal `logBytes` must
also match EOF. Gaps, overlaps, size mismatches and later-page GET failures
retain already retrieved complete frames with UNKNOWN coverage and null failure
rates; incomplete frames never become observations. Input denials are reported as stable reason classes, not raw
exception messages that may contain secrets. Supplied IDs/hashes are provenance,
not an authentication oracle; the producer must not present a selected subset
as a complete census.

Residual boundaries: execution under a shared host UID is not process isolation;
this source does not repair that. Metadata annotations and official-snapshot
claims require independent verification. The collector cannot prove current
native grant authority or safe enablement from fixture data. It creates no audit
service: each native GET is attributable through the run-scoped API credential;
output source hashes/coverage records provide accounting, not a new authority DB.

## Actions verification and separate application

The existing Actions CI runs
`node --test ops/eval/access-gap.test.mjs ops/eval/access-gap-collect.test.mjs ops/eval/access-gap-regressions.test.mjs` in
the cheap Node 24 `access-gap-fixtures` job, including drafts, aggregated into
`ci-ok`. Its three-minute workflow timeout is mirrored under the exact check-run
name `Access-gap collector fixtures` in `ops/case_e1e23b50/hung_job_thresholds.json`.
Full suites and CI mirrors run only on Actions. Fixtures
are synthetic and perform no live API calls. They are not staging, installation,
production authorization or comparable post-adoption evidence. The first draft
may skip heavy jobs under existing CI policy; skipped tests are not a pass.
Native Security then final QA and exact-head settled required checks precede the
final reviewer's pinned squash merge.

After merge and independent acceptance, the designated config applier uses the
company skill **files API with expectedVersionId**, retaining all existing files:

| Reviewed repository file | Managed skill path |
| --- | --- |
| `ops/eval/access-gap.mjs` | `scripts/access-gap.mjs` |
| `ops/eval/access-gap-collect.mjs` | `scripts/access-gap-collect.mjs` |
| `ops/eval/access-gap-cli.mjs` | `scripts/access-gap-cli.mjs` |
| `ops/eval/access-gap.test.mjs` | `scripts/access-gap.test.mjs` |
| `ops/eval/access-gap-collect.test.mjs` | `scripts/access-gap-collect.test.mjs` |
| `ops/eval/access-gap-regressions.test.mjs` | `scripts/access-gap-regressions.test.mjs` |
| `ops/eval/access-gap.md` | `references/access-gap.md` |

Retain the existing `owner-routing-collect.mjs` and its time module unchanged.
Verify the installed `delivery-effectiveness.mjs` hash against the reviewed
parser dependency before application; mismatch is a prerequisite, not permission
to overwrite it. Pin reviewed source/test hashes and actual new version ID, then
read back every applied file byte-for-byte. Do not write runtime skill snapshots
or installed package bytes. The native review receipt carries exact application
payload/files and version pins privately; public source contains no instance IDs.
Only after application may its authorized owner run one bounded complete-day
collection and verify routine readback. Missing sources remain UNKNOWN in that
result. Parent completion needs native install receipt, bounded live output and
routine readback; no claim of better access before comparable post-adoption data.
