# Pre-review rubric: the 10 buckets reviewers keep finding

Run `pre-review.sh` first (it checks the mechanical half deterministically).
Then self-check the judgment half below — one question per bucket — and fix
everything found before marking the PR ready. Re-run both on each fix delta.

| # | Bucket | The reviewer flags it when… | Ask yourself… | Fix rule |
|---|---|---|---|---|
| 1 | Untested branch | Changed behavior with no covering test | "Which changed line would catch a regression?" | Add the test, not just the fix |
| 2 | Docs drift | Code changed, docs/claims did not | "Is every changed behavior still described?" | Update the doc with the code |
| 3 | CI logic | Workflow change that skips, hangs, or overruns | "Does every job time out, and does the gating still hold?" | Prove it with a CI run, not prose |
| 4 | Generated drift | Source changed, generated output stale | "What must be regenerated, and did I run it?" | Regenerate; never hand-edit output |
| 5 | Conflicts / stale | Markers left, or base moved under the branch | "Am I diffed against today's base?" | Rebase first, then re-review |
| 6 | Unhandled errors | New failure mode with no handling | "What happens when this fails?" | Handle it or fail closed loudly |
| 7 | Authz | Secret, credential, or permission widened | "Who gains what, and where does the secret live?" | Least privilege; secrets stay in the store |
| 8 | Perf | New unbounded wait, fetch, or scan | "What bounds this on the biggest input?" | Bound it (timeout/retry/limit) or measure it |
| 9 | Dead state | TODOs, debug leftovers, unreferenced code | "Would I defend keeping this line?" | Delete it; history keeps it |
| 10 | Spec mismatch | Title, body, or commits misdescribe the diff | "Does the PR text match the diff exactly?" | Fix the code or fix the claim — never weaken a test to fit |

Scoring (same bar as review): 5/5 means no actionable defect in any bucket.
A finding stands when it names a concrete failing scenario; drop the rest.
Two fix rounds max — a third means the change is too large: split it.
