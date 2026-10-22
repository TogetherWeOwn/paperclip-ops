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
  The existing `timeoutAt` is parsed tolerantly (HH:MM or HH:MM:SS, with or
  without `Z`); an unparseable value is preserved and never crashes a pass.
- `pr-controller-wakes.patch`: wires request/claim acknowledgement into the
  initial-event and stale-reminder paths. All existing admission/hold guards stay.
- `pr-controller-ci.patch`: routes the `ci_red` wake decision through the
  rerun-once / 3-round-cap policy before the normal wake post, and skips
  stale reminders while a PR is capped. Requires the wakes packet (it anchors
  on both `post_wake` call sites). A mismatch at install is a stop.
- `../pr_controller_wakes.py`: correlates the comment id with the server's
  `agent_wakeup_requests` record. The existing read-only SQL client is injected;
  this module performs no database writes or new wake API calls.
- `../pr_controller_ci.py`: the wake-policy helper. First red on an eligible
  check reruns once (`gh run rerun <id> --failed`) and holds the wake; a
  passing retry records one flake, a failing retry wakes verbatim and counts
  one round; the 3rd red round posts one re-scope comment instead of a wake;
  3 flakes in 7 days open one `[flake]` card. Deny lists win, unknown shapes
  wake as today, ledger failures are absorbed. Kill switch:
  `PR_CONTROLLER_CI_DISABLE=1` or the `PR_CONTROLLER_CI_DISABLE_FILE` flag
  file restores wake-on-first-red with no code change.
- `../pr_controller_ci_host.py`: host glue translating the classifier's
  `detail` payload into helper check rows, resolving paid/private cost via
  the GitHub API, logging every rerun and cap comment with an UNDO line, and
  running the 24h log-only shadow under a separate state key.
- `../flake_ledger.py` additions: `record_flake` (one fail-then-pass pair
  adds two observations and one flip, atomic write, never overwrites corrupt
  evidence), `LedgerClient` (never-raising `verdict`, raising `record_flake`
  for queue-and-retry), and `quarantine_card` (open or update the single
  `[flake]` card).
- `../fixtures/pr_controller_ci_before.py`: anchor regions transcribed from
  the reviewed wakes post-image, used only for offline patch testing. It is
  not a runtime controller.
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

The ci-policy suites (acceptance contract, ops surface, host glue, patch
packet, ledger recording) run the same way:

```sh
(cd ops/productivity && python3 -B -m unittest -v \
  test_pr_controller_ci test_pr_controller_ci_ops test_pr_controller_ci_host \
  test_pr_controller_ci_patch test_flake_ledger)
```

The backstop suite applies the patch with `--fuzz=0` to its fixture, and the
ci-patch suite does the same for the ci packet. Hunk-shape rule for future
packets: GNU `patch --fuzz=0` rejects a hunk whose leading context outnumbers
its trailing context (change must sit at or before the hunk middle), so keep
leading context minimal and anchor on unique lines. To test a full
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

## CI-policy packet installation (rerun-once / 3-round cap)

Requires the wakes packet above (the ci patch anchors on its `post_wake`
call sites). Pin the host `pr_controller.py` SHA before staging; every step
below runs against that pinned copy.

1. Preserve the controller, state, metadata and hashes as above, plus a
   `.bak` of the controller for instant rollback.
2. Apply `pr-controller-ci.patch` with `patch --batch --fuzz=0 -p1`. A
   mismatch is a stop: re-pin, do not force or fuzzy-apply.
3. Stage `pr_controller_ci.py`, `pr_controller_ci_host.py` and the updated
   `flake_ledger.py` beside the candidate. Run the ci-policy offline suites
   and the existing host `test_pr_controller.py`. Compile the candidate
   without executing its `main`.
4. Shadow first: set `CI_POLICY_SHADOW=1` and run 24h log-only. The live
   wake path is unchanged; every pass logs a `ci-policy shadow ... would-*`
   line. Post the would-rerun, would-wake and would-cap counts for review
   before enable.
5. Enable by unsetting `CI_POLICY_SHADOW`. Every rerun and every cap comment
   is logged in `ops/worker-vps/operator-actions.log` with an UNDO.
6. Kill switch: `PR_CONTROLLER_CI_DISABLE=1` (or the flag file named by
   `PR_CONTROLLER_CI_DISABLE_FILE`) restores wake-on-first-red immediately
   with no code change and no restart beyond the next scheduled pass.

Rollback: set the kill switch, or atomically restore the controller `.bak`
(helpers may stay; they are inert without the call site).
