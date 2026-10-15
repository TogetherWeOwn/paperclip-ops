# Tier-0 org Semgrep starter pack (case_5789b2d5)

Two live rules, two disabled templates. The bar for a rule to stay live is
the rollout rule on case_5789b2d5: under 5% false positives over the last 20 PRs.
A rule that guesses at repo-specific names (authz helpers, grant writes)
cannot clear that bar blind, so those ship as `paths:`-scoped templates
each repo enables by naming its own functions — not as generic regexes.

- `generic-dangerous-functions.yaml` — live: `eval`, shell-exec with
  string interpolation. Language-scoped, low FP by construction.
- `generic-secrets-in-code.yaml` — live: hardcoded credential assignments
  outside test fixtures.
- `template-authz-helper.yaml` — DISABLED template (stub callee matches
  nothing): route handlers must call the repo's authz helper. Enable by
  replacing the stub with the repo's helper call shape.
- `template-check-then-write.yaml` — DISABLED template (stub callee matches
  nothing): no check-then-write on grant/allow writes without an atomic
  operation. Enable by replacing the stub with the repo's grant-write sinks.
