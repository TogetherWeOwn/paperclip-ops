# <REPO> — agent start here

Build/test: `<FAST COMMAND>` (fast tier, hermetic; CI runs the same).
Deploy: `docs/deploy.md` (staging first, then production).
Review: run the author self-review before marking a PR ready; fix every FAIL.
Skills: `agent-experience` for anything agents consume; repo skills below.

## Agent Experience (AX) rules

1. Declare the consumer of every new surface: `agent | human | both`.
2. Agent surfaces get AX1-AX8: each item N/A or with evidence.
3. CLIs and scripts ship `--json` and `--dry-run`; errors carry a code plus the next action.
4. Keep this file a short map (under ~120 lines); detail lives in docs and skills.
5. Register new capabilities where agents look (`--help` with examples, skill catalog).
6. Every change ships with a runnable check that proves it worked.
7. An "agent-friendly" change is a hypothesis until a metric moves; name the metric.

## Operator seeding note

Destination: `repo-template/AGENTS.md` in the org `.github` repo, plus
new-repo provisioning that copies it to the root of every new repo as
`AGENTS.md` with `<REPO>` and `<FAST COMMAND>` filled in. Rollback:
stop seeding; already-seeded copies are ordinary repo files.
