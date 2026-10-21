# Paperclip Review: Agent Experience (AX) dimension

Reference only (stays here). The Paperclip Review prompt lives
host-side; the review owner applies the dimension below there. It
fits the risk-based review policy: blocking only where an agent's
wrong first use breaks deploys, production, or ops tooling.

## Dimension text

Score Agent Experience only on surfaces whose consumer is an agent
or both (CLIs, scripts, APIs, workflows, repo layout,
docs-for-agents, review output, runbooks). Read the PR's Consumer
declaration first; when it says `human`, this dimension is N/A.

Check AX1-AX8; each item is either N/A or carries evidence:

- AX1 Machine-readable by default (JSON/non-TTY, stable schema).
- AX2 Explicit errors with remediation (code, retryable, next action).
- AX3 Stable identifiers; no "latest" semantics.
- AX4 Safe by construction (idempotent, --dry-run, typed prod mode).
- AX5 Knowledge in the repo; generated docs CI-checked.
- AX6 Small, layered, token-efficient context.
- AX7 Discoverable via tools (registered, says when to use it).
- AX8 Verifiable feedback loop (ships with a runnable check).

## Severity mapping

- Blocking (request changes): AX2 or AX4 gaps on deploy, production,
  or ops tooling. Examples: a deploy script that exits 0 on failure
  with no error code; a prod-affecting action reachable through one
  boolean flag with no typed mode and no dry run.
- Non-blocking nits: AX6 or AX7 gaps anywhere. Examples: an AGENTS.md
  that grew into a manual; a new script with no `--help` example.
  Say what to move or register, briefly.
- Between: other AX gaps are minor findings. Weigh them by blast
  radius: agent-only internal tooling leans nit; shared or
  frequently-run surfaces lean blocking when the wrong first use
  corrupts state or pages a human.

## Calibration notes

- One batched AX pass per review round; do not re-litigate N/A calls
  the author evidenced unless the evidence is wrong.
- Never invent a consumer: when the Consumer line is missing, ask for
  it once instead of guessing.
- An "agent-friendly" claim without a named metric is a nit at most;
  measurement, not prose, settles it.

## Owner handoff note

Applying this dimension needs the host-side review prompt, which no
repo PR can change. Track that application as follow-up work owned by
the review owner; this file is the exact text to install. Rollback:
remove the dimension from the prompt; repo-side AX blocks and lints
keep working unchanged.
