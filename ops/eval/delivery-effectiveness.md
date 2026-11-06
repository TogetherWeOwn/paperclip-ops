# Frozen delivery evidence — contract v1

This is a pure, standard-library Node 20+ accounting function, not a collector,
policy enforcer or effectiveness experiment. The adjacent synthetic assertions
run on the existing GitHub Actions CI. Neither a fixture PASS nor an accepted
producer artifact is evidence of live qualification or causal effectiveness.
There is no installation, scheduler, network request, credential or host action.

## Interfaces and custody

- `scoreDelivery(bundle)` returns a new JSON-compatible score without mutating the
  bundle, reading a clock, environment, file or network, or certifying its tests.
- `canonicalStringify(value)` normalizes object keys and sorts every array recursively;
  object keys are inserted lexically, then JavaScript JSON enumeration emits
  integer-index keys numerically before other keys. This rule is deterministic;
  **all arrays in this version are unordered bags**, not ordered timelines.
  Timeline order comes only from event timestamps and the tie rule below.
  `canonicalHash(value)` is SHA256 of those UTF-8 canonical JSON bytes. No newline
  is hashed. This is a custom canonical form, not RFC 8785/JCS: array order is
  discarded and lone surrogates use JavaScript JSON escaping. External verifiers
  must use this versioned algorithm, not substitute a JCS implementation.
  Duplicate records are checked before safe identical-ID deduplication.
  Conflicting payloads are errors, never last-write-wins. The normalized input
  hash uses deduplicated records; the membership pin uses canonical members.
- `node ops/eval/delivery-effectiveness.mjs --from-file <frozen.json>` reads only
  the explicitly named regular file (not a symlink, FIFO or device). It refuses invalid UTF-8, JSON, duplicate
  object keys, unknown fields, unsupported schema/policy and excessive inputs.
  Exit 0 means scoring completed, **not evidence sufficiency**. Exit 2 is a
  malformed-input/refusal error on stderr, with no authorization.
- Limits: 2 MiB UTF-8 input, 1,000 members, 10,000 total event/run/coverage records
  before deduplication, and nesting depth 32. Strings used as identifiers/labels
  are nonempty and at most 512 UTF-16 code units. Numbers must be finite; counts,
  tokens and run milliseconds are nonnegative safe integers. Their aggregate
  totals must also be safe integers and the aggregate currency total must remain
  finite; otherwise cost is UNKNOWN with null totals, never rounded sufficient
  accounting. Currency uses finite JavaScript numbers, not decimal cash billing.
- Source, test and contract hashes are supplied provenance, not authenticated
  attestations. The CLI additionally reports the actual scorer-file hash and
  whether the supplied source hash matches. `outputSha256` covers the pure score
  excluding that self-hash; the CLI adds a separate adapter block. Pin the
  canonical entire CLI envelope hash in an external manifest when using it.

A managed-source consumer may copy the scorer and test byte-for-byte to
`scripts/delivery-effectiveness.mjs` and `scripts/delivery-effectiveness.test.mjs`.
The adjacent import still works. This document maps to
`references/delivery-effectiveness.md`; only this documentation path mapping and
its CLI example need adapting. Publication here does not apply the managed
source or change a routine. Native consent, version compare-and-swap and exact
byte/hash readback remain separate prerequisites.

## Input shape

Every object rejects unknown keys. All required fields are shown below; event
`head`, `actorId`, `receipt` and `correction` are optional evidence fields. Dates
are real UTC calendar instants, with a `Z` suffix and either no fraction or
exactly three millisecond digits. No offset, local date or rollover is accepted.
The two adjacent, equal-duration windows are half-open `[startUTC,endUTC)`.
`asOfUTC` must reach the post-window start; it may precede its end. Future events
and runs are refused, while future window time stays censored. A partial window
includes events and arrivals exactly at asOf, consistently with final evidence
replay; events at the actual half-open window end are still excluded. Censored
durations and `observedThroughUTC` stop at asOf without adding a millisecond.
Executable symlinks resolve to the scorer and run normally; the named input
file itself must still be a regular, non-symlink file.

```text
{
  schema: "delivery-bundle.v1", policy: "delivery-policy.v1", asOfUTC,
  scope: { repository, workclass, correction },
  windows: { pre: { startUTC, endUTC }, post: { startUTC, endUTC } },
  provenance: {
    sources: [{ id, revision, sha256 }],
    sourceSha256, testSha256, contractSha256
  },
  members: [{ id, sourceId, executorId, reviewerIds: [id], pr,
              arrivedAtUTC, releaseRequired }],
  membershipSha256,
  events: [{ id, unitId, kind, atUTC, head?, actorId?, receipt?, correction? }],
  runs: [{ id, unitId, sourceId, executorId, membershipSha256, atUTC,
           tokens, runMs, subscriptionEquivalentUSD }],
  coverage: [{ id, unitId, stream, fromUTC, throughUTC, complete,
               expectedPages, fetchedPages: [1], errors: [], truncated,
               expectedRecords, recordIds: [id], recordsSha256 }],
  confounders: { concurrentExposure, changedMix, comparableUntreated }
}
```

`repository` is one canonical lowercase `https://github.com/owner/repository`,
without trailing slash, credentials, query or fragment. Repository names may
begin with a dot, underscore or hyphen (for example `.github`); the path segments
`.` and `..` are refused. Workclass and correction
are version-pinned labels, not universal industry-wide WIP laws. Membership is
immutable, includes pre-PR sources, and is not selected by outcomes. A member's
`pr` is a positive PR number or `null`; source and non-null PR mappings must be
unique. Members cannot arrive after the frozen asOf cutoff. At least one
independent reviewer is required; reviewer IDs must be unique and the executor
cannot be a reviewer. Changing any membership identity requires a new membership hash and a
new analysis registration outside the scorer; the function cannot authenticate
that registration. PR head hashes are 40 lowercase hex characters; SHA256 fields
are 64 lowercase hex characters.

A receipt is `{ id, kind, revision, sha256, persisted }`. Supported admission or
application evidence must be an actual server-persisted per-unit receipt for
this correction, at/after post-window start, not approval, consent or run start.
An admission/application receipt ID cannot be reused across different units.
Receipt authenticity and authorized reviewer membership must be independently
established by the producer of the frozen bundle. This function validates the
explicit joins, not the external server signature or authorization policy.

Each member needs an `events` coverage receipt spanning its entire history from
arrival through asOf. Reversed coverage intervals are refused. A `runs` receipt
is separately needed for cost. The receipt
must assert complete, no errors and no truncation, list every consecutive page
starting at 1 exactly once, agree with expected page and record counts, match
all record IDs exactly and match the canonical hash of that member's records.
An empty stream still requires an explicit successful page receipt. A missing
page, record, receipt, bad hash, error or truncation defeats `complete:true`.
A snapshot or selected comment subset is not complete history: leave coverage
absent or explicitly incomplete. Never manufacture pagination success.

## Events and accounting

Recognized event kinds:

- `head`, `started`, `blocked`, `unblocked`, `reviewing`, `ready`;
- `review_arrived`, `review_completed`;
- `admission`, `application`, `approval`, `run_started`, `fixture_pass`;
- `merged`, `closed`, `reopened`;
- `accepted_artifact`, `accepted_delivery`, `release_accepted`;
- `cancelled`, `withdrawn`, `failed`, `rework`, `rollback`, `security`.

IDs must be stable in the frozen evidence source. Identical record IDs/payloads
are safely deduplicated. Differing payloads with the same ID or ambiguous joins
are refused. Replay sorts by timestamp, then kind (with `merged` before other
kinds), then event ID. This is a declared tie convention, not a reconstruction of
unknown server order; the normalizer must not present ambiguous causal order as
known fact. A head event establishes the exact PR head. Changing it invalidates
prior merge, acceptance and release evidence. A reopen also invalidates terminal
evidence and resets ready/review waits. Head changes reset those joins too.
An ordinary pre-evidence head update does not itself defeat acceptance. An
invalidation of existing proof stays UNKNOWN until fresh exact-head terminal
proof qualifies it again; it is not a permanent historical head-drift penalty.
First-start age is retained across a reopen; it is not an estimate of active
service time. Repeated acceptance/release receipts do not move the first
effective terminal timestamp. An artifact-to-delivery upgrade without an
invalidation is also not a second completion.

An acceptance event needs a persisted `acceptance` receipt, the exact current
head and an actor in the member's independent reviewer list. Artifact acceptance
and live delivery acceptance are different event kinds. A `release_accepted`
event needs a `release` receipt with the same exact-head/reviewer requirements.
Delivery additionally needs a valid merge and, when `releaseRequired:true`,
applicable release acceptance. An asserted delivery receipt without an already
valid merge is insufficient evidence, not a sufficient WIP observation. Fixture
success alone never accepts anything. An authorized cancellation/withdrawal needs
a persisted `withdrawal` receipt from a declared reviewer. A subsequent ordinary
close preserves that authorized cleanup; unsupported closure is not completion.
Authorized cleanup is not head-specific and survives head updates, but a reopen
invalidates it. Acceptance sufficiency requires a terminal category for every
member; fully observed backlog or started WIP is still missing acceptance.
Invalid acceptance records (including a delivery receipt before any valid merge)
remain historical evidence issues even if later receipts qualify an outcome;
this conservative v1 rule does not silently erase contradictory supplied proof.

Each boundary snapshot separates `accepted_delivery`, `accepted_artifact`,
`merged_awaiting_acceptance`, `authorized_cleanup`, `unstarted_backlog` and
`started_wip`. Started blocked/reviewing work remains unfinished WIP. Never-started
blocked work stays backlog. Merges count once per unit in the window; closes do
not add another merge or a separate close count. Reopens and raw cancellation/withdrawal
events are separately counted. Closing accepted outcomes whose effective terminal
time falls in the window supply completed counts/latencies; an outcome invalidated
by reopen/head drift is not silently treated as still accepted at that boundary.
Raw merged-event counts are observations, not proof of valid acceptance.

Started and review waits that have not finished remain explicitly censored.
Blocked time is accumulated only for started, nonterminal work and clipped to the
observation window. Latencies are elapsed UTC milliseconds, not business time or
active service time. Every quantile is nearest-rank (`ceil(p*n)-1`); an empty
sample returns `null`, never zero. Ready-to-merge and review samples are separate.
Readiness is captured with its valid merge, not overwritten by later ready events.
Review arrivals/completions join on a declared reviewer `actorId`, with at most
one outstanding review per reviewer. Missing actor identity is inferred only
when the member has exactly one reviewer. Overlapping reviews from different
reviewers retain separate samples and censored waits (with `reviewerId`).
Undeclared/unidentified reviewers, duplicate outstanding arrivals for the same
reviewer, or unmatched completions mark acceptance UNKNOWN and supply no
fabricated latency for that ambiguous join. A head change or reopen invalidates
outstanding review waits but retains their known reviewer joins until completion;
those completions are consumed without an ambiguity penalty or latency sample.
Invalidated arrivals are not active censored waits. A fresh arrival after that
completion can establish a new sample. An overlapping new arrival before the
invalidated one completes is still ambiguous: the contract has no review-cycle
identifier with which to choose a completion's arrival. This does not restore
stale acceptance; fresh exact-head terminal evidence is still required.
Cost is UNKNOWN unless nonempty run evidence has complete coverage and immutable
unit/source/executor/membership joins; absence is not zero spend. The reported
currency is subscription-equivalent, not cash billing.

## Output and limits of inference

The pure output has four deliberately separate layers:

1. `fixtureAssertions.verdict: NOT_SUPPLIED`. Only the executed test harness
   emits an assertion verdict; live arithmetic never self-certifies a PASS.
2. `evidence`: claim-specific accounting, treatment and acceptance sufficiency;
   incomplete observations can be retained but are explicitly UNKNOWN.
3. `observed` and `descriptiveComparison`: frozen boundary accounting and an
   optional descriptive delta, never an operational decision. Comparison is
   INCONCLUSIVE with incomplete windows/history/acceptance, unproved treatment,
   per-unit treatment receipts not matching the post-window start, concurrent
   exposure, changed/unknown mix, or fewer than two completed outcomes in either
   window. Two is a versioned descriptive suppression rule, **not a statistical
   power threshold**. `concurrentExposure` must be exactly `none` and changedMix
   explicitly false to remove those two suppression reasons.
4. `causalEffectiveness.status: NOT_ESTABLISHED` and
   `operationalAuthorization:false` unconditionally. An asserted untreated cohort,
   a complete positive fixture, deterministic output or descriptive improvement
   cannot establish identification, effect size precision or live qualification.

Missing substantive-review history cannot become a zero wait. Lack of a
confounder record cannot establish non-exposure. Sparse cohorts and incomplete
run-cost joins do not improve by setting `complete:true`. Real task/source IDs,
native skill content, raw captures and live input/output manifests belong in the
private evidence system, never in this public source carrier.

## Actions-only verification

The existing CI step runs:

```sh
node ops/eval/delivery-effectiveness.test.mjs --verify
```

The assertions are written before the implementation. `--verify` first checks
the actual source, then generates named one-fault scorer copies in a temporary
directory and runs each designated assertion against its mutant. A control is
accepted only with exit 1 **and its designated `SEMANTIC:<case>` assertion**;
missing modules, syntax errors, timeout and import failures do not count. Logs
record each exact replacement, command, expected assertion, exit and verdict.
Temporary mutants are removed in a `finally` block. Neither production scoring
nor fixtures contain an enabled fault mode. Compare entire canonical score bytes
for ordering and frozen-clock determinism. Record exact head and Actions run/job
links separately; no local suite transcript is a substitute for required CI.
