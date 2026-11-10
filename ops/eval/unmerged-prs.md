# Managed PR inventory verification carrier

The adjacent [script](unmerged-prs.mjs), [tests](unmerged-prs.test.mjs) and
[frozen input](fixtures/unmerged-prs.snapshot.json) are staged managed-source
candidates. This repository is their GitHub Actions verification carrier, not
the installed source of truth. Publication does not install a skill, change a
routine, authorize a source-card action or establish operational adoption.

## Separate series

Without `--inventory`, the existing fields remain observed recently-done-card
measurements: `doneCardsScanned`, `prLinks`, `unmergedOnDone`, `closedOnDone`,
`staleLabels`, `unknown`, and their counts. The default window remains three days.
Rows remain per work product, including duplicates and the original source URLs.
They are not renamed to all-PR totals. `legacyCompleteness` makes incomplete reads
explicit without silently changing historical observed counter meanings.

`--inventory` adds `inventory.schema: "m25-inventory/v2"`. It enumerates live open
PRs, including bots and drafts, and distinguishes nonterminal ownership,
no-card, cancelled-only, done-only, and mixed terminal ownership. Canonical URLs
are lowercase HTTPS GitHub PR URLs with positive safe-integer numbers; a trailing
slash is accepted, other paths, credentials, queries, fragments and suffixes are
not. Every source/work-product/owner provenance row survives canonical dedupe;
there is no arbitrarily selected primary owner. Reason links do not establish
ownership or cause additional PR discovery.

Archived repositories are explicitly listed and excluded from additive inventory.
Legacy reads of recently-done links remain separate even for archived repositories.
Partial reads, malformed responses, alias errors, repeated cursors/pages, page
caps and unavailable data produce UNKNOWN completeness and null sensitive totals.
Observed lower bounds are separately named; they are not zero-completeness claims.
The default page safety cap is 10,000 requests per list. Reaching it is UNKNOWN,
never successful exhaustion. Offset lists request through the empty terminal page,
not merely a short page; cursor lists require explicit terminal pageInfo.

## Universe and reason coverage

Live collection combines the supplied daily PR-flow/audit repository evidence,
all native projects and their git-repository workspaces, linked work products,
and the visible GitHub repository lists of every evidenced owner. Organization
lists use the organization endpoint, including visible private repositories.
Public user-repository lists cannot prove private coverage and are marked UNKNOWN.
Missing daily audit evidence, unsupported workspace hosts, inaccessible repos and
other discovery gaps are explicit coverage failures. The claim is always
**enumerated-repositories-only**, never all company PRs or all GitHub repositories.
No capped search is used as exhaustive inventory.

Supply an independently saved, revision-pinned audit scope JSON using
`--scope-file`: `{ "repositories": ["example/repo"], "evidence": ["saved audit reference"] }`.
These are supplied provenance, not authenticated attestations. Keep actual private
repository and task evidence on the native task rather than in this public carrier.
No live company inventory or ownership repair is established by the synthetic input.

The script reads closed-unmerged linked PR bodies, ordinary issue comments, review
comments and review bodies, plus every linked source card's description/comments.
A reason on either surface counts. The convention is:

```text
Closed unmerged: <duplicate of|superseded by|need lost|mismatch|burden> <https-link>; unique work: <none|kept at https-link>; follow-ups: <none|live cards https-link[, https-link]>
```

`reason-found`, `absent-with-complete-read`, and `UNKNOWN` are distinct. A reason
may be found despite incomplete remaining reads, which remain recorded as such.
Supersession is a legitimate disposition, not delivery and not an instruction to
reopen a replaced PR. Unique-work and follow-up text is retained for source-owner
judgment. No close, reopen, merge, reassignment, source status repair, automatic
child creation, checkout, comment or finding disposition is authorized.

## Mutation containment

Additive evidence always has `operationalPermission: false`. `--fix-labels` selects
only the existing recent-done stale-label candidates, never inventory-only rows.
Fresh source detail and complete interaction reads can identify live work,
pending review and human holds. They cannot atomically prove absence of queued
work or grant source-action authority. Consequently this candidate **refuses all
label writes**, reporting each candidate as skipped or UNKNOWN with a named
reason. This is an intentional conservative compatibility change, not a claim
that null run IDs make a mutation safe. A supported atomic source guard and
separate authorization would be prerequisites to restoring writes. The candidate
contains no native mutation transport. Frozen replay refuses `--fix-labels`.

## Replay and custody

```sh
node --test ops/eval/unmerged-prs.test.mjs
node ops/eval/unmerged-prs.mjs --inventory --from-file ops/eval/fixtures/unmerged-prs.snapshot.json
```

These are GitHub Actions verification commands, not instructions to run a full
suite on a shared host. CI runs the exact checked-out candidate, prints source,
test and input SHA-256 values, compares two fixed-clock replay files byte-for-byte,
and requires the assertions to reject a nonterminal-inventory regression mutant.
Mutation RED evidence is not a historical-baseline RED claim. Live collection and
managed application are not run by CI; the fixture carries no credentials.

Managed mapping is byte-identical:

| Carrier | Managed target |
| --- | --- |
| `ops/eval/unmerged-prs.mjs` | `scripts/unmerged-prs.mjs` |
| `ops/eval/unmerged-prs.test.mjs` | `scripts/unmerged-prs.test.mjs` |
| `ops/eval/fixtures/unmerged-prs.snapshot.json` | `scripts/fixtures/unmerged-prs.snapshot.json` |

The managed applier must compare the expected version and before hashes, apply
only reviewed paths, and read back exact after hashes. A carrier merge is not that
application. The private task holds the exact baseline-to-candidate patch,
per-path payload, managed version pin and exact-head Actions receipts. Reverting
this carrier removes verification source only, not an installed skill or runtime.

REA angle: reuse native managed versions, work-product provenance, the existing
detector and the existing Actions carrier pattern; no new service or scheduler.
