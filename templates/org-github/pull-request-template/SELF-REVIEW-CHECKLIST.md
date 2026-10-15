# PR self-review checklist: the 10 buckets (case_4784a03d)

Destination: merge this section into the `## Checklist` area of the org PR
template in `TogetherWeOwn/.github` (file `.github/pull_request_template.md`).
It is the judgment half of pre-review. The mechanical half is deterministic:
`pre-review.sh` (case_509672c4, staged beside this template) plus the Tier-0
gate (`../tier-0/`) — run both first, then work this list, then fix
everything found before marking the PR ready. Re-run all three on each fix
delta.

Scoring (same bar as review): 5/5 means no actionable defect in any bucket.
A finding stands only when it names a concrete failing scenario. Two fix
rounds max — a third means the change is too large: split it.

## Self-review (author: tick each line only after checking it on the diff)

- [ ] 1 Untested branch — every changed behavior has a covering test. Asked:
      "which changed line would catch a regression?" Added the test, not just the fix.
- [ ] 2 Docs drift — every changed behavior is still described. Updated the
      doc with the code, in this PR.
- [ ] 3 CI logic — every job times out and the gating still holds. Proved
      with a CI run on this head, not prose. No `paths:` on gating workflows.
- [ ] 4 Generated drift — regenerated everything the source change requires
      (ran the generator; never hand-edited its output). Tree clean after.
- [ ] 5 Conflicts / stale — diffed against today's base (rebased first when
      the base moved), no markers left.
- [ ] 6 Unhandled errors — every new failure mode is handled or fails closed
      loudly. Asked: "what happens when this fails?"
- [ ] 7 Authz — no secret, credential, or permission widened. Least
      privilege; secrets stay in the store. Named who gains what.
- [ ] 8 Perf — every new wait, fetch, or scan is bounded
      (timeout/retry/limit) or measured on the biggest input.
- [ ] 9 Dead state — no TODOs, debug leftovers, or unreferenced code.
      Defended or deleted every line; history keeps it.
- [ ] 10 Spec mismatch — title, body, and commits describe the diff exactly.
      Fixed the code or fixed the claim — never weakened a test to fit.

## Operator merge note

The org template already has a `## Checklist` with judgment items. Append
the ten lines above as a `### Self-review (10 buckets)` subsection; do not
replace existing items. Keep the "bot-maintained — do not edit" readiness
block untouched. Verify `pr-lint` still parses the template after the edit
(Gate 4 covers the workflow; the template parse is the `pr-lint` job's own
contract test).
