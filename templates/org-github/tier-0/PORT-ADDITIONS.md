# Tier-0 staged port: post-pin additions (case_f3ff4554)

The staged files under `tier-0/` are a verbatim port of the source pin
(`SOURCE`, PR #824) — except the files listed below. Each addition is an
org-level rule pack authored after the pin for the blocker-ledger
'security' class; the existing Gate 5 folder scan consumes them with zero
workflow edits (the gate scans the whole rules folder). `check-staged-drift.sh`
exempts exactly these files from the verbatim-port comparison, and fails if
a staged-but-unlisted file appears or a listed file goes missing.

On the next re-port from a moved pin, reconcile: any addition since merged
upstream drops off this list; the rest stay exempt. The staged
`semgrep-rules/README.md` stays verbatim from the pin by design (the drift
gate forbids hand-edits to ported files), so until that re-port it still
says "two live rules" — this manifest is the current record.

## Additions (machine list — one bullet per file, keep the shape)

- `semgrep-rules/security-ssrf-redirect.yaml` — SSRF/open-redirect pack: dynamic-fetch and user-influenced-redirect rules for JS/TS + Python (WARNING severity: triage hint only; the gate `mode` input flips the whole gate report-only -> enforcing together, so promotion is all-or-nothing until the gate grows per-rule inputs upstream in PR #824).
- `semgrep-rules/security-secrets-extended.yaml` — extended-secrets pack: real-shape Discord/Slack/AWS/OpenAI/Stripe/GitHub credential rules (ERROR, test/spec/e2e/fixture/vendor-excluded, example placeholders skipped).

## Coverage gain (blocker-ledger 'security' class, 18% of Claude blockers)

Before: 2 live semgrep rules (hardcoded-credential assignment, eval/shell-exec)
plus gitleaks defaults; no SSRF rule, no open-redirect rule, no
provider-shaped secret rules at org level.

After: 12 live semgrep rules (+10). Deterministically covered classes move
from 1 of 4 (credential assignment) to 4 of 4 (credential assignment,
provider-shaped secrets, SSRF fetch shapes, open-redirect shapes) for the
direct shape; indirect/laundered flows stay with the Tier-2 reviewer by
design (`security-boundary` bucket).

## Not in this slice (follow-ups)

- CodeQL custom queries: staged shared config lives at
  `templates/org-github/codeql/codeql-config.yml` (`security-extended`
  suite). Per-repo CodeQL workflow wiring needs the central repo plus the
  per-repo language matrix — owned by the foundation rollout
  (case_e9259c75).
- Org required-workflow/ruleset application (privileged operator step): apply
  `rulesets/tier0-report-only.json` per `rulesets/OPERATOR-RUNBOOK.md`, then
  flip the ruleset stage per measured FP rate. Routed to the Operator with that exact
  action — no agent applies rulesets.
