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

## Planning rules

- One doer and one independently reviewable outcome per slice; include its check and rollback.
- Scale planning to risk and coupling, not diff size; preserve every existing gate.
- Bound uncertain prototypes by question, budget and stop condition; retaining code needs normal review.
- Reassess remaining cost/benefit on changed evidence; ignore sunk cost and expose high-impact risks early.
- Queue only concrete followups with a doer, exit evidence and reconsideration trigger; optional polish is not a blocker.
- Use native task plans and dependencies, never host automation. Copy-ready planning fragment and adoption checks: [right-sized planning](https://github.com/TogetherWeOwn/paperclip-ops/blob/main/docs/right-sized-planning.md).
- Hand work between agents one owner at a time with a short packet and result: [minimal native handoff convention](https://github.com/TogetherWeOwn/paperclip-ops/blob/main/docs/minimal-orchestration.md).

## Operator seeding note

Destination: `repo-template/AGENTS.md` in the org `.github` repo, plus
new-repo provisioning that copies it to the root of every new repo as
`AGENTS.md` with `<REPO>`, `<FAST COMMAND>`, `<DEPLOY DOC>` and
`<REPO SKILLS>` filled in. Rollback: stop seeding; already-seeded
copies are ordinary repo files.
