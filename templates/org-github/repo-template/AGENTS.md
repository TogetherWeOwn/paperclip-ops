# <REPO> — agent start here

Build/test: `<FAST COMMAND>` (fast tier, hermetic; CI runs the same).
Deploy: `<DEPLOY DOC>` (staging first, then production).
Review: run the author self-review before marking a PR ready; fix every FAIL.
Skills: `agent-experience` for anything agents consume; `<REPO SKILLS>`.

## Agent Experience (AX) rules

1. Declare the consumer of every new surface: `agent | human | both`.
2. Agent surfaces get AX1-AX8: each item N/A or with evidence.
3. CLIs and scripts ship `--json` and `--dry-run`; errors carry a code plus the next action.
4. Keep this file a short map (under ~120 lines); detail lives in docs and skills.
5. Register new capabilities where agents look (`--help` with examples, skill catalog).
6. Every change ships with a runnable check that proves it worked.
7. An "agent-friendly" change is a hypothesis until a metric moves; name the metric.

## Test behavior, not implementation

Test observable claims and boundary inputs; name invariants before bounded,
seeded property tests. Reproduce semantic counterexamples before treating regex
matches as defects. Account for every expected case; skips, zero tests and
bounded random samples are not exhaustive proof. Framework setup, reusable CI,
completion evidence and rollback: [behavioral tests](docs/BEHAVIORAL-TESTS.md).

## Operator seeding note

Destination: `repo-template/AGENTS.md` in the org `.github` repo, plus
new-repo provisioning that copies it to the root of every new repo as
`AGENTS.md` with `<REPO>`, `<FAST COMMAND>`, `<DEPLOY DOC>` and
`<REPO SKILLS>` filled in. Also copy `repo-template/BEHAVIORAL-TESTS.md`
to the new repo's `docs/BEHAVIORAL-TESTS.md` so the map link resolves.
Rollback: stop seeding; already-seeded copies are ordinary repo files.
