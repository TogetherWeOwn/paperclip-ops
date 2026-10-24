# Review-only rebase carry: helper and integration boundary

## Current delivery state

`../review_rebase_carry.py` is a read-only, stdlib-only decision helper. It is
**not wired into a controller or check writer**. No feature is enabled by adding
or importing it. Host integration, the default-off control, shadow logging and
the pinned call-site patch packet are still pending. Do not install this branch
or treat the helper's `carry` return value as a GitHub check receipt.

The implementation uses the merge-group normalized-patch/digest semantics,
but does not modify the merge-group implementation or its pass/fail policy.

## Pure helper contract

- `own_patch(repo, base_sha, head_sha)` reads the patch between the unique
  merge-base and the head. Both inputs must be full lowercase commit SHAs;
  `repo` is a local directory, not a remote URL. Every git subprocess has a
  timeout, fixed executable, disabled hooks/external transports and sanitized
  git environment. Lazy fetch and replacement objects are disabled. Fetching
  missing pinned objects is the host caller's responsibility.
- `normalized_patch` preserves changed-line bytes, CRs, modes, binary payloads,
  EOF markers and delete/add record boundaries. Only hunk/context coordinates
  and blob ids are ignored. `digest` length-prefixes and sorts file records;
  it is deliberately not patch-id.
- `latest_review` scopes rows to a PR and head, then selects the newest id
  **before** testing completed/success/5/5. Conflicting newest rows, invalid
  ordering or malformed evidence do not authorize carry.
- `decide` returns either `('carry', proof)` or `('review', reason)`; evidence
  failures never escape as exceptions. Changed own-patch bytes, a base-ref
  change, any existing new-head review row, incomplete compare evidence and
  ambiguous merge-bases require review.
- `delta_files` returns changed-own-patch paths union all supplied prior
  blocking-finding paths, including removed files. Git or malformed findings
  evidence raises `GitError`; callers must fall back to full review, not
  interpret the error as an empty delta.
- The idempotency namespace is `paperclip-review-rebase-carry:v1:` and binds
  repository (case-insensitive), PR number and exact head. It is separate from
  the merge-group namespace. A proof for a superseded head does not apply.

Review rows in this helper are already trusted input. The host must first
establish App identity and genuine-review provenance. In particular, these
in-memory rows are **not** sufficient evidence for publishing a required check.

## Required host integration (not yet delivered)

The integration must satisfy all of these before it is eligible for installation:

1. Pin the exact host source hashes and stage copies. Generate a minimal unified
   diff from those regions; validate it with `patch --batch --fuzz=0 -p1`.
   A pin mismatch is a stop and re-pin, never permission to force/fuzzy apply.
   Preserve `.bak` files, metadata and before/after hashes for rollback.
2. Keep the sole carried-check writer in `review_carry`. Query all review
   check-runs (`filter=all`, bounded complete pagination). Only genuine
   `Paperclip Review` rows for App `5191093`, the original head and this PR can
   be sources. Select newest before checking score/status; exclude carried
   sources so carries cannot chain. Any new-head review row prevents carry.
3. Use trustworthy old-head base-ref and base-SHA snapshots. A moving current
   branch is not historical evidence. Missing old snapshots require full
   review. Re-read head/base and source-check identity immediately before
   publication. Serialize per repository/PR/new head and verify the returned
   check's App, name, head, conclusion and provenance. Publication/receipt
   failure must request review, never leave a head with neither path.
4. Include old/new heads, **both base SHAs**, digest, source check id, exact
   carry summary and review-only scope in the published summary. Explicitly
   state that all other required checks remain required. CI runs on the new
   head; a carry does not waive it.
5. Replace the compare-based round-context file list with own-patch delta union
   prior blocking findings, keeping late HIGH-path findings blocking. Classify
   `review_gate|review_carry|review_findings|rebase_carry` as HIGH. Never narrow
   on missing/truncated findings or any exception. Update the JSON context
   and ROUND CONTEXT note together; no unavailable-data-as-zero-files message.
6. Add a fail-closed control for **both carry and delta scoping**. Absent,
   unreadable or malformed configuration means disabled. Explicit shadow mode
   is log-only. Every carry and fallback must have a bounded, sanitized audit
   record with repository/PR/heads/bases/digest/reason and
   `UNDO: PATCH this same App-owned check to failure`.
7. Run the unchanged acceptance suite plus the host gate, fairness and
   merge-group carry suites on patched staged copies. Add independent
   adversarial coverage of identity, receipt, concurrency, TOCTOU, publication
   failures, missing historical bases and default-off behavior. Do not copy
   credential/mint code into a public integration packet.

Final-head security APPROVE and same-head CI plus Paperclip Review 5/5 remain
required before the authorized executor receives an installation request.

## Shadow, enable and rollback checklist

This checklist is a future execution contract, not evidence that shadowing or
installation has happened. No shadow counts or production metrics exist here.

- Run explicitly log-only for at least 24 hours. Record the interval and
  would-carry versus would-review counts, without publishing carried checks
  or narrowing live review scope.
- Hand-check 10 samples, at least 3 with non-empty own-patch changes that
  correctly require review. Retain the exact old/new heads, bases, source
  check and digest proof for each sample.
- Enable only after these gates and the security sign-off are satisfied.
  Record ACT / WHY / UNDO and the precise enabled interval.
- At 72 hours after enable, report re-review tokens per re-review versus first
  review (target at most 40%), Reviewer runs per merged PR (baseline about
  7.5), and revert/hotfix escape rate against the preceding seven days.
  Seven-day outcomes for newly merged PRs may still be immature at 72 hours;
  label that limitation rather than claiming a complete escape-rate window.
- Roll back by disabling **both** carry and delta scoping, or atomically
  restoring the preserved host `.bak` files. Invalidate any erroneous carried
  check by PATCHing that same App-owned check to failure; do not delete check
  history, waive CI, alter a ruleset or change the 5/5 policy.

## Local verification

```sh
(cd ops/productivity && python3 -B -m unittest -v test_review_rebase_carry)
```

The acceptance suite already runs in the hosted CI productivity test step;
adding the helper activates all its tests instead of its module-level skip.
No acceptance-test file was changed by the implementation.
