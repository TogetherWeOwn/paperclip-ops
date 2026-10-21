# PR template: Agent Experience (AX) block

Destination: merge this section into the org PR template in
`TogetherWeOwn/.github` (file `.github/pull_request_template.md`),
directly after the `## Attestation` section and before `## Risks`.

Every process, tool, and artifact is designed for its real consumer.
Anything used by AI agents (code, repos, docs, CLIs, APIs, workflows,
cards, review output, runbooks) must have great Agent Experience: an
agent can discover it, use it correctly first time, and recover from
failure without a human, at the lowest token cost. Anything used by
humans gets UI/UX instead.

## Agent Experience (AX)

<!--
Consumer: who uses what this PR changes.
agent = CLIs, scripts, APIs, workflows, repo layout, docs-for-agents,
  review output, runbooks. human = end-user surfaces (app UI, bot
  replies, owner reports). both = mixed surface; call out which parts.
-->

Consumer: <agent | human | both>

<!--
One line per AX item: evidence, or N/A with a one-line reason.
AX1 machine-readable output (JSON/non-TTY, stable schema).
AX2 explicit errors with remediation (code, retryable, next action).
AX3 stable identifiers; no "latest" semantics.
AX4 safe by construction (idempotent, --dry-run, typed prod mode).
AX5 knowledge lives in the repo; generated docs are CI-checked.
AX6 small, layered, token-efficient context.
AX7 discoverable via tools (registered, says when to use it).
AX8 verifiable feedback loop (ships with a runnable check).
An "agent-friendly" change is a hypothesis until it is measured.
-->

- AX1: <evidence or N/A: reason>
- AX2: <evidence or N/A: reason>
- AX3: <evidence or N/A: reason>
- AX4: <evidence or N/A: reason>
- AX5: <evidence or N/A: reason>
- AX6: <evidence or N/A: reason>
- AX7: <evidence or N/A: reason>
- AX8: <evidence or N/A: reason>

## Operator merge note

Insert the `## Agent Experience (AX)` section above verbatim (heading,
Consumer line, and the eight AX lines with their two HTML comments).
Do not replace existing sections. Keep the "bot-maintained — do not
edit" readiness block untouched. Verify `pr-lint` still parses the
template after the edit (the `pr-lint` job's own contract test).

This block is advisory at rollout: authors fill it, reviewers sample
it. A `pr-lint` gate that requires the Consumer line on
agent-surface PRs is a separate follow-up, not part of this drop.
Rollback: revert the template commit; no workflow or ruleset changes
are part of this handoff.
