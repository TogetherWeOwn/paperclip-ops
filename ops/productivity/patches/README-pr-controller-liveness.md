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
- `../pr_controller_wakes.py`: correlates the comment id with the server's
  `agent_wakeup_requests` record. The existing read-only SQL client is injected;
  this module performs no database writes or new wake API calls.
- `pr-controller-notify-guard.patch`: wires the notification-only author-wake
  gate into the live wake path. Four hunks: the `notify_author_wake` entry
  point beside the classification seam, a `required` hoist feeding
  `classify()`, a verdict-record block immediately after `state[key] = rec`,
  and a `ci_red` suppress gate in `main()` right after the wake key.
  Suppress fires only for `ci_red` with no author-actionable failure (empty
  red list, or only known shared-base checks red while the required set is
  known). The verdict is recorded alongside every state/head update, before
  any exit that can save, and cleared when the state leaves `ci_red` — so a
  saved same-head suppress verdict always describes the checks of the pass
  that saved it, and a later genuine failure on the same head re-records a
  fresh verdict. The gate reads the recorded verdict and skips render and
  post without consuming the wake cap or marking the transition delivered.
  Verdicts, eligibility, checks and gates are unchanged. Unknown required
  sets fail closed to `wake`.
- `../pr_controller_wake_guard.py`: the pure-stdlib decision module behind
  that gate (`actionable_red`, `author_wake`, `KNOWN_BASE_EXECUTED`).
  Deterministic, no I/O, no host imports.
- `../fixtures/pr_controller_backstop_before.py`: exact pre-fix function, used
  only for offline patch testing. It is not a runtime controller.
- `../fixtures/pr_controller_notify_before.py`: exact pre-fix
  `latest_check_contexts`/`classify`/`wake_text` seam (lines 1-99,
  byte-identical to the original evidence) plus live lines 335 through EOF
  appended verbatim for the wiring hunks. Evidence only; not a runtime
  controller.

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
  test_pr_controller_backstop test_pr_controller_wakes \
  test_pr_controller_notify_guard test_pr_controller_notify_wiring)
```

The backstop and notify-guard suites apply their patch with `--fuzz=0` to
their fixture. To test a full staged controller without importing its host
dependencies:

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
   apply the backstop patch followed by the wake patch followed by the
   notify-guard patch with `patch --batch --fuzz=0 -p1`. A mismatch is a
   stop, not permission to force or fuzzy-apply it.
3. Stage `pr_controller_wakes.py` and `pr_controller_wake_guard.py` beside the
   candidate. Run the offline suites on the candidate and the existing host
   controller tests. Compare the complete candidate diff: unrelated
   classification, hold, review and merge code must be unchanged. Compile the
   candidate without executing its `main`.
4. Atomically install the helper first, then the candidate, preserving host file
   ownership/mode. Use the existing scheduled invocation; this packet requires no
   Paperclip, database, or model-proxy restart, agent config change, or gate change.
5. Apply the pump skip on the host (the pump file itself is host-only and is
   not part of this packet). In `pr_pump.py` `main()`, inside the
   `for key, rec in act:` loop, immediately after the per-(card, PR, state,
   head) signature-dedup `continue` and before the `full, num` split, insert:
   ```python
   if rec.get('state') == 'ci_red':
       nv = rec.get('notify_verdict') or {}
       if (nv.get('verdict') == 'suppress' and rec.get('head')
               and nv.get('head') == rec.get('head')):
           continue
   ```
   The controller records the verdict in the same pass as the state/head it
   describes, before every exit that can save, and clears it whenever the
   state leaves `ci_red`; a saved same-head suppress verdict therefore
   always describes the checks of the pass that saved it. The skip requires
   all three equalities (stored `ci_red`, same head, recorded `suppress`),
   so a missing verdict, a head change, or a refreshed `wake` verdict fails
   closed to the legacy pump. Preserve the pump file and its hash before editing; compile
   it; dry-run the pump (no `--apply`) and confirm a suppressed record no
   longer appears as `would pump` while a genuinely actionable `ci_red`
   still does.
6. Record exact public head, before/after hashes, commands and verification receipt.
   Observe a real backstop trigger with no intervening re-arm and a skipped event
   followed by a claimed run. Offline green alone does not satisfy live acceptance.

Rollback: atomically restore the preserved controller and any previous helper.
Retain state and pending acknowledgements; never roll back or edit database rows,
remove holds, alter wake configuration, cancel runs, or force a review/merge.
If step 5 was applied, remove the inserted pump block and restore the preserved
pump file the same way.
