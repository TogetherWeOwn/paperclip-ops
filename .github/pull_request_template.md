<!--
Title: Conventional Commits header, e.g. `fix(auth): refuse expired sudo sessions`.
Types: feat fix perf refactor test docs build ci chore revert style security. Max 100 chars, no trailing period.
This repository is public. Never include internal card IDs, private links, host data, or credentials.
Write short, plain sentences. Fill in every section: the `pr-lint` check reads them.
See README.md for publication and contribution safety.
-->

## Thinking Path

<!--
  Required. Trace your reasoning from the project down to this change, as a blockquote.
  Start with what this repo is, narrow through the subsystem and the problem, and end with
  why this PR exists. Aim for 5-8 steps; the check needs at least 3 real ones.
-->

> - paperclip-ops is the operator tooling for our PaperclipAI company: credentials, provisioning, host checks
> - [Which subsystem or capability is involved]
> - [What problem or gap exists]
> - [Why it needs to be addressed]
> - This pull request ...
> - The benefit is ...

## Linked Issues or Issue Description

<!--
  Required for feat, fix, perf, refactor and security. Pick ONE:
  (A) Public issue exists: link it, e.g. `Fixes #123`.
  (B) No public issue exists: describe the problem without private references.
  Original author/card linkage for migrated work belongs on the private board only.
-->

Describe the problem or link a public issue.

## What Changed

<!-- One bullet per logical unit. -->

-

## Verification

<!-- Commands you ran and their results, so a reviewer can repeat them. -->

-

## Risks

<!-- Migration safety, breaking changes, behaviour shifts, credential or host impact. "Low risk" if genuinely minor. -->

-

## Model Used

<!--
  Required. Provider, exact model ID or version, context window, reasoning mode and tool use if known.
  If no AI model was used, write "None - human-authored".
-->

-

## Checklist

<!-- Judgment items only. Anything a machine can verify (CI green on the exact
  head, `npm run check` green, secrets/`node_modules` in the diff) is set by
  the readiness handler in "Merge readiness" below and covered by gitleaks and
  the required checks — never ticked by an agent. -->

- [ ] I wrote a thinking path that runs from the project to this change
- [ ] I named the model used, with its version
- [ ] I searched for duplicate or related PRs and linked them above
- [ ] I ran the tests locally and they pass
- [ ] I added or updated tests where applicable
- [ ] I updated the documentation this change touches

## Merge readiness

<!-- readiness:start -->
## Merge readiness (bot-maintained — do not edit)

Head: pending — the handler fills this on the first event.

| Check | State |
|---|---|

Paperclip Review: pending
Mergeable: pending
Updated: pending
<!-- readiness:end -->
