# Scanner finding adapter: offline scan outcome to native finding payload

`ops/paperclip/scanner_finding_adapter.py` maps one completed run of the
pinned secret scanner to a native issue-create or issue-update payload,
or to a refusal. It is a pure offline function plus a thin CLI: no
network, no credentials, no database, no board writes. The native issue
is the finding record — this adapter adds no packet file format, proof
schema, exporter or findings store. Submitting a payload, and verifying
that a submission landed, is the caller's job under the caller's own
authority; this document authorizes no live or automated invocation.

## Pinned producer

The one supported producer is `verification/secret-scan.sh`, pinned by
git blob id in the adapter (`SCANNER_SCRIPT_BLOB`). Only its documented
exit contract is encoded: exit 0 clean, exit 1 finding. A scanner change
must re-pin the blob and re-review the contract in the same change; the
test suite recomputes the blob from the file and fails on drift.

## Input

One JSON document (CLI: stdin, or `--input PATH`; at most 1048576 bytes,
duplicate keys and nonfinite numbers rejected):

```json
{
  "schema_version": 1,
  "repo_slug": "owner/repo",
  "source_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "scanner": {"name": "secret-scan", "script_blob": "<pinned blob id>"},
  "scan": {
    "exit_code": 1,
    "spawn_ok": true,
    "complete": true,
    "shallow": false,
    "truncated": false,
    "stderr_nonempty": false,
    "occurrences": [
      {"rule_id": "github-token", "path": "src/app.py", "lines": [7]}
    ]
  },
  "responder": "<opaque assignee reference, chosen by the caller>",
  "evidence_links": ["https://example.invalid/evidence/1"]
}
```

`scan` carries either `occurrences` (metadata extracted by the caller)
or `raw_stdout` (the pinned scanner's own output). With `raw_stdout`,
only the `path:line:` prefix of each `git grep -n` line is read; the
matched content after it is discarded immediately. Rule ids are the
pinned scanner's five rules: `github-token`, `private-key`,
`discord-webhook`, `stripe-live-key`, `gitignore-coverage`.

## UNKNOWN means refusal

Any other exit code, spawn failure, an incomplete or shallow scan,
truncated scanner output, non-empty stderr, a scanner blob other than
the pin, output lines the parser cannot account for, or an exit code
that disagrees with the parsed occurrences is UNKNOWN. The adapter then
emits `{"status": "refusal", "reason_code": ...}` with no payloads and
the CLI exits 2. UNKNOWN creates nothing and closes nothing; the caller
reports it as unmeasured. A clean scan (exit 0, zero occurrences) is
`{"status": "none"}` — no create payload and no close payload.

## Identity and bounded replay

The logical identity of a finding is repository slug + rule id +
repo-relative path. Scanner blob, source head and line numbers are
revision evidence, not identity: one rule in one path is one finding,
however many lines match, and occurrence lines are listed up to a bound
(50) with the full count kept — an over-long line list is a truncated
list on a standing finding, never a clean result.

Create payloads carry `idempotencyKey = "scanner-finding:v1:" +
sha256(identity-marker)`, where the marker is the canonical JSON of the
three identity fields. That key deduplicates duplicate deliveries inside
the native create-key retention window (7 days); adapter tests prove
key determinism only — a runtime dedup claim needs a duplicate-delivery
fixture against the real native API. Perpetual uniqueness is **not**
claimed.

Optional `deliveries` entries (`rule_id`, `path`, `age_days`,
`existing_issue_ids`) tell the adapter where a finding sits in that
window. Absence of an entry asserts a first delivery. Exactly one
existing id yields an update payload instead of a create. After the
window (`age_days >= 7`) without exactly one verified existing id, or
with an unknown (`null`) age or multiple ids, the adapter refuses —
never a blind create, never a silent drop. The identity marker printed
in every payload is a lookup aid for finding that existing id; it is
not an atomic claim.

## Closure

A clean scan alone never closes anything. A closure payload requires an
explicit `closure_request`: exactly one existing finding id, a
`fix_head` equal to this scan's `source_head`, zero occurrences for the
identity in this scan, the same pinned scanner blob as the original
detection (`original_scanner_script_blob`; a different blob is
unorderable offline and its output contract is unreviewed, so it
refuses), and a non-empty `independent_acceptance_ref` https link with a host. A closure path
containing `:` refuses (`closure_path_ambiguous`): `git grep -n` output
cannot attribute such a path unambiguously, so zero occurrences is unprovable. The
adapter checks presence and shape only; the genuineness of the
acceptance is the submitter's and the accepting reviewer's to verify.
Accepted-risk and false-positive outcomes are separate decisions and
are never emitted by this adapter.

## Output and exit codes

- exit 0, `status: "payload"`: one `findings[]` entry per logical
  finding, each with identity, bounded lines/count, scanner pin, source
  head, evidence links, and exactly one of `issue_create`
  (`title`, `description`, `idempotencyKey`, `assigneeAgentId`, optional
  `parentId`/`projectId` from the caller's `parent_ref`/`project_ref`)
  or `issue_update` (`issueId`, `comment`); plus `closure` when a
  closure request passed every check.
- exit 0, `status: "none"`: clean scan, nothing to create or close.
- exit 2, `status: "refusal"`: `reason_code` and a static `reason`;
  refusal output never echoes unvalidated input.

## What this does not claim

No immutable custody: a submitted payload's native activity trail is an
attribution surface, not proof the adapter can verify. No live trigger:
wiring this adapter into CI, a routine or any automated caller needs
its own decision naming that trigger. No delivery guarantee: if a
submission fails, the finding exists only when the caller confirms the
native create/update landed.
