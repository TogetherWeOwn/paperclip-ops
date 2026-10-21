# PR-controller liveness integration

This is a focused integration packet, **not a replacement controller release**.
The public snapshot does not contain the deployed controller baseline. Shipping
small, reviewable diffs avoids importing a private operational tree or replacing
its inventory, classification, review, merge, capacity, hold, or live-run rules.

## Contents

- `pr-controller-backstop.patch`: fixes `ensure_backstop` without moving an
  existing board/controller deadline, including a due deadline. Only a future
  assignee monitor shorter than twelve hours is stretched; the resulting
  controller-managed deadline is attributed to the board so it is stretched once.
  Legacy controller notes are recognized because earlier controller writes
  retained assignee attribution. An absent review monitor is still created.
- `pr-controller-wakes.patch`: wires request/claim acknowledgement into the
  initial-event and stale-reminder paths. All existing admission/hold guards stay.
- `../pr_controller_wakes.py`: correlates the comment id with the server's
  `agent_wakeup_requests` record. The existing read-only SQL client is injected;
  this module performs no database writes or new wake API calls.
- `../fixtures/pr_controller_backstop_before.py`: exact pre-fix function, used
  only for offline patch testing. It is not a runtime controller.

A comment's HTTP success records a pending request, not delivery. Subsequent
inventory passes observe a queued/deferred request without posting duplicates.
A skipped request (independent of reason), failed request, or cancelled request
becomes retryable subject to per-key exponential backoff (1h, 2h, 4h, ... capped
at 24h) and a 5-attempt cap, plus the unchanged controller guards. A missing
request row after a comment is bounded the same way. Delivery is acknowledged
when both a run id and claim timestamp exist, or when the server coalesces the
wake onto an already-queued run (status coalesced with a run id). Retry state
resets on claim or event/head/owner change. Changing the event/head or owning
issue invalidates the pending correlation. Read failures preserve the pending
state instead of treating it as delivery.

For pre-packet state that recorded only `woke`, reconciliation matches the exact
controller-generated message on the owning issue. Positive skipped evidence
reopens delivery. Missing legacy evidence preserves the old deduplication marker
rather than causing an inventory-wide migration flood; it is not proof of claim.
This is not a general repair for parked recovery actions or blocked cards.

## Offline verification

```sh
(cd ops/productivity && python3 -B -m unittest -v \
  test_pr_controller_backstop test_pr_controller_wakes)
```

The backstop suite applies the patch with `--fuzz=0` to its fixture. To test a full
staged controller without importing its host dependencies:

```sh
PR_CONTROLLER_SOURCE=/path/to/staged/pr_controller.py \
  python3 -B ops/productivity/test_pr_controller_backstop.py -v
```

Only the selected function is AST-loaded. No controller `main`, inventory pass,
comment, service restart, or host mutation is run by these tests.

## Installation contract

The authorized host executor must attest the current source and check for a live
peer deployment before staging. Use the final merged public head only after its
required CI and Paperclip Review 5/5. Never install this entire public snapshot.

1. Preserve the existing controller, helper if present, state, metadata and hashes.
2. In a separate staging directory containing `ops/productivity/pr_controller.py`,
   apply the backstop patch followed by the wake patch with `patch --batch
   --fuzz=0 -p1`. A mismatch is a stop, not permission to force or fuzzy-apply it.
3. Stage `pr_controller_wakes.py` beside the candidate. Run the offline suites on
   the candidate and the existing host controller tests. Compare the complete
   candidate diff: unrelated classification, hold, review and merge code must be
   unchanged. Compile the candidate without executing its `main`.
4. Atomically install the helper first, then the candidate, preserving host file
   ownership/mode. Use the existing scheduled invocation; this packet requires no
   Paperclip, database, or model-proxy restart, agent config change, or gate change.
5. Record exact public head, before/after hashes, commands and verification receipt.
   Observe a real backstop trigger with no intervening re-arm and a skipped event
   followed by a claimed run. Offline green alone does not satisfy live acceptance.

Rollback: atomically restore the preserved controller and any previous helper.
Retain state and pending acknowledgements; never roll back or edit database rows,
remove holds, alter wake configuration, cancel runs, or force a review/merge.
