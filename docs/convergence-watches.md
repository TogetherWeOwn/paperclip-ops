# Read-only convergence digest fragment

`ops/convergence_watches.py` evaluates five fixed SELECT queries over a private,
normalized export. It is an **offline digest fragment**, not an installed monitor.
It never connects to a database, calls an API, changes a card, repairs a workspace,
pauses a run, changes a model, or schedules work. There is no write-enabled mode.
Any eventual existing daily digest must own collection, cadence, deduplication
and delivery. Inspection did not identify that digest or a routing hook. This
fragment is paused and must not be activated. Do not attach it to an unrelated
alert loop or add a timer.

## Invocation and outcomes

```sh
python3 ops/convergence_watches.py --snapshot /private/export.json --format json
python3 ops/convergence_watches.py --snapshot /private/export.json --format markdown
```

The default is `paperclip.convergence-report.v1` JSON. Markdown emits **nothing**
when all five counts are zero; otherwise it contains only findings and coverage
gaps. Every finding has the fixed route `existing_daily_digest`. Route that fragment
through the existing platform-reliability digest recipient, not to an owner inbox.
No new notification transport, approval path, or automatic repair is supplied.

| Exit | Meaning | Existing digest caller action |
| --- | --- | --- |
| 0 | Complete export; all five counts zero | Append no alert |
| 1 | One or more positive counts, no coverage gaps | Append the finding fragment once |
| 2 | Invalid manifest, incomplete source, query unavailable or query budget exceeded | Append UNKNOWN, preserving any measured findings; do not claim healthy |

Do not run this command under an unchecked `set -e` and silently discard a nonzero
result. Capture output and status, accept only 0/1/2, and pass findings/gaps into the
existing digest's single delivery. A caller/collector failure must also be UNKNOWN.
The report always says `runtime_qualified: false`: an offline count is not a deploy
qualification, approval, native-fix verdict, or proof of live alert delivery.

## Private export contract

The root has exactly these keys (unknown versions/keys fail closed):

```json
{
  "schema": "paperclip.convergence-export.v1",
  "company_id": "00000000-0000-4000-8000-000000000004",
  "artifact": {
    "base_sha": "bba6a45f0d364cfd962ac59b3bdf2426a8222adb",
    "source_sha": "bba6a45f0d364cfd962ac59b3bdf2426a8222adb",
    "image_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "start": 1000,
  "end": 87400,
  "sources": {}
}
```

These identifiers/digest are examples, not a runtime manifest. `base_sha` pins
`paperclipai/paperclip` tag `v2026.1012.0`. The authorized exporter supplies the exact
residual source commit and immutable image digest actually observed. Accepting their
shape does not verify the build, the eight-patch limit, or a deployment authority.
Do not substitute a guessed source SHA or "latest" image. All rows must belong to
the root company; the authorized collector must filter by `company_id` **before**
normalizing. Missing company/coverage evidence means `complete: false`.

All timestamps are nonnegative integer Unix seconds, UTC. The digest window is
`[start,end)` and must be exactly 86,400 seconds. Preserve event ordering when
rounding source timestamps. Sources are independently qualified: an absent or
invalid source makes its dependent watch UNKNOWN, without hiding other findings.
Each supplied source has exactly:

```json
{"complete": true, "boundary_complete": true, "start": 700, "end": 87400, "rows": []}
```

`complete` means all pages were read without errors or truncation, and the source
mapping/schema was verified for the named artifact. `boundary_complete` means the
additional history specified below was included. These are exporter assertions,
not something this offline evaluator can independently prove. If either assertion
cannot be supported, supply false (or omit the source). Never label a partial
export complete just to produce a zero. Empty **complete** evidence is valid zero.
At most 5,000 rows per source and 8 MiB per input are accepted. Never truncate to
these limits: mark the source incomplete or use a reviewed collector aggregation.

### Five definitions and required rows

| Watch / source | Exact row keys | Count and boundary semantics |
| --- | --- | --- |
| `rapid_reblock` / `status_transitions` | `id`, `issue_id`, `at`, `sequence`, `status` | Count blocked **transition events** in the daily window with a preceding done transition on the same issue 0–300 seconds earlier, inclusive. Include events from `start-300`. A later legitimate reopen is not proof of a bug: this is a regression candidate, never a repair instruction. |
| `over_cap_starts` / `run_intervals` plus `cap_intervals` | Runs: `id`, `agent_id`, `start`, `end`. Caps: `id`, `agent_id`, `start`, `end`, `cap` | Count daily starts for which same-agent overlapping runs exceed the cap effective **at that start**. Include all pre-window runs still active during the window. Intervals are half-open; null end means still active. Starts sharing a timestamp are measured as a batch, not arbitrarily ordered by UUID. Missing historical caps, gaps at a start, or overlapping cap intervals are UNKNOWN. |
| `workspace_incoherence` / `workspace_observations` | `id`, `at`, `coherent` | Count observed incoherent workspace checks in the daily window; boolean `coherent=false`. Require a verified existing workspace check/event source, not today's workspace metadata or absence of an error string. `boundary_complete` attests that every expected daily check was observed; unobserved workspaces are not coherent. Count checks, not unique workspaces. |
| `duplicate_review_heads` / `reviews` | `id`, `endpoint_id`, `repository_id`, `pull_number`, `head_sha`, `at`, `assessed` | Count groups with >1 row, grouped by endpoint + immutable repository ID + PR + exact head, where at least one row was created/updated in the window. Include **all historical siblings** of touched groups, even if older than source start. Report assessed/unassessed row totals within those duplicate groups. A different head/endpoint/repository/PR is never a duplicate. |
| `stdin_deadline_failures` / `run_failures` | `id`, `at`, `failed`, `error`, `stderr` | Count failed runs finishing in the window whose error or stderr contains the case-insensitive literal `no stdin data received in 3s`. The source-backed signature is intentionally narrow; generic timeouts, EPIPE and successful runs do not match. Count each run once. |

All row IDs must be unique within a source. String identifiers must be nonempty
where they are grouping keys. `at`, interval endpoints, sequence, cap and PR number
are integers (booleans/floats are not accepted); caps/PR numbers must be positive.
`coherent`, `assessed` and `failed` must be JSON booleans. Heads are full lowercase
40-hex SHAs. Error/stderr fields are strings, with absent text normalized to `""`.
No source row, path, repository name, error text or input exception is printed.
The report carries input SHA-256 plus exact artifact identities for private custody.

For status transitions, `sequence` is a unique increasing per-issue ordinal of
**actual status changes**, not all issue updates. Time must be nondecreasing and
adjacent statuses must differ. Tied timestamps require a provable source order;
if that order is unavailable, the source is not complete. This preserves the
inclusive zero-second boundary without counting a later done as an earlier one.

For review rows, `at` is the last created/updated time, not request time. `assessed`
means `assessment IS NOT NULL`, **not** state=completed, complete=true, score=5,
or favorable approval. Do not filter unassessed, superseded or failed siblings out
of this diagnostic export. Any requested exclusion needs a reviewed contract
change; do not silently borrow fork-only columns such as `shadow_label`.

## Source mapping and unresolved integration boundary

Verified against official source commit
`bba6a45f0d364cfd962ac59b3bdf2426a8222adb`:

- `packages/db/src/schema/activity_log.ts` and `server/src/routes/issues.ts`:
  `activity_log`, action `issue.updated`, `details.status`, `details._previous`.
  Only actual status changes belong in the transition export; preserve ordering.
- `packages/db/src/schema/heartbeat_runs.ts` and `agent_config_revisions.ts`:
  run start/finish times and before/after configuration history exist. An exporter
  must prove cap-history completeness and the effective runtime-config key before
  producing cap intervals. Today's agent config is **not** a historical fallback.
- `packages/db/src/schema/chat_github.ts`: `chat_github_reviews` supplies company,
  endpoint, repository ID, PR, head, assessment, created/updated times.
- Stdin signature evidence: `TogetherWeOwn/paperclip` commit
  `0c190fc01eed6f02f6816bedaf49c1ca91f1239e`,
  `packages/adapter-utils/src/server-utils.test.ts:634-640`. Reading that evidence
  is not carrying its implementation or building its deployment branch.

Source inspection did **not** identify an existing platform daily convergence
digest, routing hook, or persisted workspace-coherence verdict source. The retained
owner-routing daily poster is unrelated and must not be repurposed. The route name
in this fragment is a proposed contract, **not an implemented transport**. The
fragment remains draft and NOT QUALIFIED for runtime use. Collection/delivery
cannot proceed under an existing-digest-only scope without an actual existing
source/route or an explicit scope decision. No new collector, clock, transport,
production query, or deployment is authorized by this document.

Inspection also identified these unresolved normalization requirements:

- Activity rows have timestamps and random IDs, not a proven per-issue sequence.
  A timestamp tie must remain incomplete unless actual order can be established;
  sorting UUIDs is not evidence of transition order. All status writers and
  previous-status semantics still need qualification.
- The official cap is `runtimeConfig.heartbeat.maxConcurrentRuns`, default 20,
  clamped to 1–50 (`server/src/services/heartbeat.ts` and
  `packages/shared/src/constants.ts` at the pinned base). Exported `cap` must be
  the effective normalized historical value, not a raw configured value. The
  official running-status count is not yet proven equivalent to start/finish
  interval overlap; config creation/history coverage is likewise unqualified.
- No persisted daily workspace verdict was found. Metadata or an on-demand
  readiness/repair operation cannot be substituted for observations. Keep this
  source missing/incomplete rather than manufacturing successful checks.
- Review `at` must faithfully cover both creation and updates, full-head format
  must validate (otherwise UNKNOWN), and historical sibling expansion and stable
  repository identity remain exporter obligations, not facts proved here.
- The stdin signature above is fork-test evidence only. It was not located in
  the official base's runtime source. Failed/finished timestamps and stderr
  truncation also remain unqualified. An empty export cannot establish that the
  watch is applicable or that a native fix shipped.

## Offline acceptance

Run the independent, protected test suites without contacting any live service:

```sh
python3 -B -m unittest -v test_convergence_watches test_convergence_watches_safety
```

The 59 tests exercise query boundaries, missing/malformed evidence, input privacy,
resource budgets, descriptor safety, and same-connection write denial. They are
wired into the existing hosted `suites` job and local fast inventory; draft PRs
still skip hosted heavy suites under the existing policy. A passing offline test
run does not qualify source mappings, a live digest, a build, or deployment.

Direct-function tests currently expose unclosed SQLite connection warnings:
SQLite's transaction context manager does not close the connection. The protected
safety test also inspects that connection after evaluation. Explicit closure needs
a coordinated implementation/test-contract change, not an implementer edit to the
acceptance test or warning suppression. No persistent-process resource-growth
qualification is claimed.

## Read-only and resource boundary

The only external I/O is a bounded read of the given local regular-file export
and stdout. Open is nonblocking; the opened descriptor must be a regular file of
at most 8 MiB. FIFOs/devices/sockets are rejected, and symlinks are refused where
`O_NOFOLLOW` is available. The existing caller must also retain an overall command
deadline for local filesystem/host stalls; SQL budgets do not bound OS I/O.
Queries are fixed source-controlled SQL over bound JSON values in SQLite
`:memory:`; there is no SQL/path interpolation or database URL. SQLite uses
memory-only temporary storage, `query_only=ON`, and an authorizer permitting only
SELECT, READ and FUNCTION operations. Each query has a two-second/2,000,000-VM-step
budget. Any SQLite failure becomes UNKNOWN, never zero. No extra package, network,
subprocess, environment-secret lookup, disk state or alternate credential exists.
The exporter/digest transport must retain its own read-only grants and fences.

## Retirement and rollback

No watch auto-retires on a quiet day, upstream merge, changed version string, or
favorable stale-head review. Remove a watch only in a reviewed change that names
the first **deployed native release/image digest containing the fix**, exact fix
commit, and passing independent boundary fixtures on that same artifact. For
workspace and cap regressions without a verified native fix, retain the watch;
instructions or historical zero counts are not a substitute. Preserve all source
history and evidence when retiring a watch; this is not production cleanup.

Rollback of integration is to omit this fragment from the existing digest call,
not stop the digest or change any security/approval gate. No rollback mutates
application data. Runtime activation remains subject to the existing rehearsal,
review and deployment contract; this document does not grant it.
