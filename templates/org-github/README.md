# Org `.github` additions: change-detection CI standard (case_a99daa6c) + Tier-0 gate, self-review checklist, rulesets (case_4784a03d)

Staged source for the org `TogetherWeOwn/.github` repo (extends templates
PR #1 / case_2a6077b7). Our credential scope does not cover that repo, so an
Operator applies these files there after this PR merges.

## Destination map

| This file | Goes to (in `TogetherWeOwn/.github`) |
|---|---|
| `tier-0/tier-0-quality-gate.yml` | `.github/workflows/tier-0-quality-gate.yml` (reusable `workflow_call`) |
| `tier-0/ADOPTION.md` | `TIER-0-ADOPTION.md` (repo root: caller snippet, ci-ok wiring, rollout) |
| `tier-0/semgrep-rules/README.md` | `semgrep-rules/README.md` (org starter-pack rules) |
| `tier-0/semgrep-rules/generic-dangerous-functions.yaml` | `semgrep-rules/generic-dangerous-functions.yaml` (live rule) |
| `tier-0/semgrep-rules/generic-secrets-in-code.yaml` | `semgrep-rules/generic-secrets-in-code.yaml` (live rule) |
| `tier-0/semgrep-rules/template-authz-helper.yaml` | `semgrep-rules/template-authz-helper.yaml` (disabled template) |
| `tier-0/semgrep-rules/template-check-then-write.yaml` | `semgrep-rules/template-check-then-write.yaml` (disabled template) |
| `tier-0/semgrep-rules/security-ssrf-redirect.yaml` | `semgrep-rules/security-ssrf-redirect.yaml` (live rule, post-pin addition — see `tier-0/PORT-ADDITIONS.md`) |
| `tier-0/semgrep-rules/security-secrets-extended.yaml` | `semgrep-rules/security-secrets-extended.yaml` (live rule, post-pin addition — see `tier-0/PORT-ADDITIONS.md`) |
| `tier-0/PORT-ADDITIONS.md` | `semgrep-rules/PORT-ADDITIONS.md` (post-pin additions manifest + coverage gain; the drift gate exempts listed files) |
| `codeql/codeql-config.yml` | `codeql/codeql-config.yml` (shared `security-extended` suite config) |
| `pull-request-template/SELF-REVIEW-CHECKLIST.md` | merged into `.github/pull_request_template.md` as a `### Self-review (10 buckets)` subsection (see the file's Operator merge note) |
| `rulesets/tier0-report-only.json` | org ruleset payload, stage 1 (`evaluate`); applied via `gh api`, not copied |
| `rulesets/tier0-required.json` | org ruleset payload, stage 2 (`active`, no earlier than 24h after stage 1); applied via `gh api`, not copied |
| `rulesets/OPERATOR-RUNBOOK.md` | reference only (stays here; exact admin commands, placeholders, rollback) |
| `tests/test_tier0_org_template.sh` | reference only (stays here; structural contract for everything above) |
| `tests/test_security_rules.sh` | reference only (stays here; pins the case_f3ff4554 security pack + local scan rollout) |
| `tier-0/SOURCE` | reference only (stays here; recorded source pin the drift gate diffs against) |
| `check-staged-drift.sh` | reference only (stays here; offline staged-vs-source drift gate, wired into `verify-fast`) |
| `CI-STANDARD.md` | `CI-STANDARD.md` (repo root) |
| `workflow-templates/changes.yml` | `.github/workflows/changes.yml` (reusable `workflow_call`) |
| `workflow-templates/starter-ci.yml` | `workflow-templates/starter-ci.yml` (new-repo choice) |
| `workflow-templates/starter-ci.properties.json` | `workflow-templates/starter-ci.properties.json` (required name/description metadata) |
| `test-missing/blocker_test_checks.py` | `.github/scripts/blocker_test_checks.py` (verbatim copy of the repo-root source; fix upstream, then re-copy) |
| `scripts/detect_changes.py` | reference only (stays here; the workflow embeds the same rules) |
| `tests/test_detect_changes.sh` | reference only (stays here) |
| `tests/test_detect_workflow.py` | reference only (executes the actual workflow step in disposable local repos) |
| `tests/test_template_contract.py` | reference only (metadata, concurrency expressions, actual documented lint commands) |
| `tests/test_policy_checkout.py` | reference only (real pinned checkout action; symlink regression plus normal-directory control) |
| `scripts/ci_policy.py` | `.github/scripts/ci_policy.py` (warn mode; wired into `pr-lint`, needs no ruleset change) |
| `tests/test_ci_policy.py` | `.github/scripts/test_ci_policy.py` (unit tests, run beside the lint) |
| `pull-request-template/AX-BLOCK.md` | merged into `.github/pull_request_template.md` as a `## Agent Experience (AX)` section after `## Attestation` (see the file's Operator merge note) |
| `repo-template/AGENTS.md` | `repo-template/AGENTS.md` (new-repo seed; provisioning copies it to each new repo root — see the file's seeding note) |
| `ax-lint/ax-lint.yml` | `.github/workflows/ax-lint.yml` (reusable `workflow_call`, advisory warn default) |
| `ax-lint/ax_lint.py` | `ax-lint/ax_lint.py` (the lint the workflow checks out at the invoked ref) |
| `ax-metrics/ax-metrics.yml` | PRIVATE ops repo `.github/workflows/ax-metrics.yml` (weekly schedule; cost/card data stays internal — see the file's header) |
| `ax-metrics/ax_metrics.py` | PRIVATE ops repo `ax-metrics/ax_metrics.py` (ships with the workflow above) |
| `review/AX-DIMENSION.md` | reference only (stays here; exact AX dimension text for the host-side review owner to install) |
| `tests/test_ax_org_template.py` | reference only (stays here; structural contract for everything above) |
| `tests/test_ax_lint_evidence.py` | reference only (stays here; immutable acceptance contract for located evidence + conservative CLI matching) |

The agent seed's planning rules reference the public
[right-sized planning guide](../../docs/right-sized-planning.md). Merge that guide
before distributing a seed that links to it. The seed is new-repository source,
not an update to effective company agent instructions or existing repositories.
An authorized instruction maintainer adopts it through the existing reviewed
path and verifies effective instructions/new tasks before claiming rollout.
No host automation or skill-assignment change is part of adoption.

> `scripts/ci_policy.py` + `tests/test_ci_policy.py` are a verbatim copy of
> `paperclip-ops@4fb40122` (PR #656, warn mode). Do not edit the copy:
> fix upstream, then re-copy. Fail flip (`CI_POLICY_MODE=error`) only after
> the case_6b39c80f parent confirms every repo child merged and verified.

> `tier-0/tier-0-quality-gate.yml` + `tier-0/semgrep-rules/` are a port of
> `paperclip-ops@61754d9dd4` (PR #824, case_5789b2d5), unmerged at stage
> time — the staged header names the source commit. Tag `tier0-source/61754d9d`
> keeps the pin reachable after #824 rebases or merges. Do not hand-edit the
> port: fix upstream in #824, then re-port — except post-pin org additions
> listed in `tier-0/PORT-ADDITIONS.md`, which the drift gate exempts by name.
> The Operator re-syncs this copy
> after #824 merges and before applying it to the org repo. The recorded pin
> (`tier-0/SOURCE`) plus `check-staged-drift.sh` (wired into `verify-fast`)
> fail on any hand-edit or pin bump without a re-port; pass an explicit ref
> to check the staged copy against a newer source head instead of the pin. The ruleset
> payloads are never copied as files; a human org admin applies them with
> the runbook (ruleset writes are 403 for the GitHub App).

## AX acceptance CI and recovery

This repository's GitHub-hosted `suites` job runs these existing suites as
separate `AX acceptance - <filename>` steps, gated on `full` or `core` changes:

- `templates/org-github/tests/test_ax_lint_branches.py`
- `templates/org-github/tests/test_ax_lint_evidence.py`
- `templates/org-github/tests/test_ax_org_template.py`

Template/helper changes select `core`; workflow changes select `full`. The
steps reuse the hash-pinned PyYAML installation from
`scripts/static-label-guard-requirements.txt`. No credentials or live service
calls are needed (HTTP behavior is exercised only against a disposable loopback
fixture). No consumer installation, lint enforcement change, scheduled job or
new branch-protection check is implied.

To reproduce a failing step non-interactively from the repository root, use
Python 3.12 on Linux x86_64 (the requirements file pins that wheel). Prepare
the same dependency and run its exact suite command (or all three below):

```bash
set -euo pipefail
work="$(mktemp -d)"
python3.12 -m venv "$work/venv"
export PATH="$work/venv/bin:$PATH"
python3 -m pip install --disable-pip-version-check \
  --only-binary=:all: --no-deps --require-hashes \
  --target "$work/deps" -r scripts/static-label-guard-requirements.txt
export PYTHONPATH="$work/deps"
python3 -B -m unittest discover -v -s templates/org-github/tests -p 'test_ax_lint_branches.py'
python3 -B -m unittest discover -v -s templates/org-github/tests -p 'test_ax_lint_evidence.py'
python3 -B -m unittest discover -v -s templates/org-github/tests -p 'test_ax_org_template.py'
```

Each command preserves unittest's non-TTY output and exit status (0 passes;
nonzero fails), and its filename identifies the failed CI step. Fix the
implementation or configuration, not the immutable acceptance tests; rerun the
same command. The separate `AX metric contract suites` step also discovers
`test_ax_*py`, including the Paperclip-window suite; its now-covered exemption
is absent. Unrelated non-AX template suites remain outside these lanes.

## Security-rule fixture verification

`tests/test_security_rules.sh` runs the staged rules with Semgrep (verified
at 1.138.0 and the gate pin 1.128.1). This repo's CI does not install Semgrep
or run that suite; run it locally with `SEMGREP` set to the absolute path of
an existing binary (not via a venv on `PATH`, so the suite retains system
Python/PyYAML). When the suite is invoked with `CI` set, a missing binary is
fatal; local runs without it explicitly skip engine fixtures. The
drift-exemption cases are pinned on every CI run by
`python3 test_staged_drift.py` (SyntheticDrift), so they do not depend on
this suite's engine fixtures or on the recorded source pin.

## Operator adoption wiring (required, not delivered by copying scripts)

The live org `workflow-templates/pr-lint.yml` currently checks PR conventions
inline; it does not invoke this policy lint. The Operator must apply these steps
on the org adoption PR and on each existing repository adoption PR:

1. Install the policy script and tests at `.github/scripts/ci_policy.py` and
   `.github/scripts/test_ci_policy.py` in **each caller repo**, not only the org
   repo. Merge that source installation first. The trusted-source checkout below
   reads the PR base; both files must already exist there before enabling it.
   Script-based conventions adopters must also have `pr_standards.py`,
   `test_pr_standards.py` in `.github/scripts` and `.github/pull_request_template.md`
   on that trusted base. New-repo provisioning must seed the applicable scripts,
   tests and template before its first linted PR; selecting a workflow template
   does not copy scripts into a new repository.
2. Keep the existing conventions behavior and required `pr-lint` job. Use
   `pull_request`, never `pull_request_target`, hosted `ubuntu-latest`,
   `contents: read`, no secrets and the existing five-minute job timeout.
   Replace any existing checkout with the sibling pair below: **no earlier
   checkout may populate the workspace root**. Start from the fresh hosted
   workspace; trusted code first, candidate text second. Neither target is
   inside the PR tree, so a PR-supplied `.ci-policy-source` symlink cannot
   redirect trusted-checkout cleanup into candidate workflows.
   - **Inline conventions** (org `workflow-templates/pr-lint.yml`): leave the
     checkout-independent inline check unchanged, then append the two checkouts
     and two policy steps below. Omit the script-based conventions steps.
   - **Script-based conventions** (existing adopters): use the complete six-step
     sequence below, replacing the old checkout and conventions steps. Both
     checkouts must precede `Unit-test the lint` and `Check title, body and commits`.
     Redirect both commands from `.github/scripts` to
     `.ci-policy-source/.github/scripts`; the root no longer contains repo files.
     Preserve the conventions environment mappings and existing job-level
     `REQUIRE_CARD_REF` / `PR_STANDARDS_MODE` settings. Keep all commands at the
     workspace root. Never execute candidate conventions or policy code/tests.

<!-- ci-policy-adoption-steps -->
```yaml
      - name: Check out trusted policy source
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          ref: ${{ github.event.pull_request.base.sha || github.sha }}
          path: .ci-policy-source
          persist-credentials: false
      - name: Check out candidate workflow files
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          path: .ci-policy-candidate
          persist-credentials: false
      - name: Unit-test the lint
        run: |
          python3 -I -m unittest discover -s .ci-policy-source/.github/scripts -p 'test_pr_standards.py'
      - name: Check title, body and commits
        env:
          EVENT: ${{ github.event_name }}
          TITLE: ${{ github.event.pull_request.title }}
          BODY: ${{ github.event.pull_request.body }}
          AUTHOR: ${{ github.event.pull_request.user.login }}
          HEAD_REF: ${{ github.event.pull_request.head.ref }}
          REPO_PRIVATE: ${{ github.event.repository.private }}
          COMMITS: ${{ toJSON(github.event.commits) }}
        run: |
          python3 -I .ci-policy-source/.github/scripts/pr_standards.py
      - name: CI change-gating policy
        env:
          CI_POLICY_MODE: warn
        run: |
          shopt -s nullglob
          workflows=(.ci-policy-candidate/.github/workflows/*.yml .ci-policy-candidate/.github/workflows/*.yaml)
          python3 -I .ci-policy-source/.github/scripts/ci_policy.py "${workflows[@]}"
      - name: Policy lint unit tests
        run: |
          python3 -I -m unittest discover -s .ci-policy-source/.github/scripts -p 'test_ci_policy.py'
```
<!-- /ci-policy-adoption-steps -->

3. Fix `pr-lint` concurrency while editing it: use the starter's PR-number group
   only on PR events, a unique `github.run_id` group otherwise, and PR-only
   `cancel-in-progress`. The existing unconditional cancellation is not the
   current standard. Preserve other title/body rules and all required checks.
4. Record the adoption PR/SHA and logs proving policy annotations appear with
   `CI_POLICY_MODE=warn` and the 10 policy tests run. Copying files alone is not
   evidence that the lint runs. No new required check or ruleset change is needed.
   The fail flip remains gated by the parent rollout; do not enable error mode.

Sources: [workflow-template metadata](https://docs.github.com/en/actions/how-tos/reuse-automations/create-workflow-templates),
[checkout inputs](https://github.com/actions/checkout/tree/v4.2.2#usage),
[Python isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I).

## After applying

1. Pin the `changes.yml` SHA; replace `<PINNED_SHA>` in `starter-ci.yml`.
   Supply the metadata pair and replace all placeholder job commands. Never
   adopt the starter's broad docs globs without checking that the matched files
   are genuinely documentation for that repository.
2. Verify per case_6b39c80f: one docs-only PR skips heavy jobs with `ci-ok`
   green, and one dependency-file change runs everything. Verify the warn-mode
   lint steps above too; these local tests are not live GitHub proof.
3. Rollback: revert the org-repo/adopter wiring commit; keep the prior required
   checks intact. No credential or ruleset changes are part of this handoff.

## Local verification (no credentials)

```sh
./templates/org-github/tests/test_detect_changes.sh
python3 -I -m unittest discover -s templates/org-github/tests -p 'test_detect_workflow.py'
python3 -I -m unittest discover -s templates/org-github/tests -p 'test_template_contract.py'
PYTHONPATH=templates/org-github/scripts \
  python3 -m unittest discover -s templates/org-github/tests -p 'test_ci_policy.py'
./templates/org-github/tests/test_tier0_org_template.sh
./templates/org-github/check-staged-drift.sh
python3 -B -m unittest discover -s templates/org-github/tests -p 'test_ax_*py'
```

Covers 65 matcher fixtures/controls, 22 actual-workflow tests and five template
contract tests: docs-only, area-only, 46 root/nested dependency files (including
`npm-shrinkwrap.json`), shared workspace/compiler config even under code areas,
`.github`, shared/build files, conservative full runs for unknown paths/deletions,
renames, Unicode/control-character and non-UTF-8 filenames, missing refs,
disconnected history, failed diffs, Python caller-module/PYTHONPATH isolation,
Git-environment/template-hook isolation, and the `ci-ok` failure path. Template
contract tests evaluate the actual concurrency expressions and metadata,
verify that script-based conventions commands/environment survive relocation,
and execute the documented trusted-source warn-mode policy commands in scratch.
The separate real-checkout regression runs both documented checkout actions with
local Git remotes, then conventions and policy commands together in their actual
order. Its symlink fixture and ordinary-directory control must preserve candidate
YAML byte-for-byte, emit two policy warnings, run both trusted test suites and
never execute candidate scripts/tests or use their altered PR template. The
conventions entry point must accept valid PR metadata and still reject invalid
PR titles and main-commit subjects; no check is bypassed to fix the layout.
Neither suite dispatches live GitHub runs or performs org adoption. Fixture
initialization explicitly selects an empty template
directory; inherited `GIT_TEMPLATE_DIR` hooks cannot execute. A benign executable
hook positive control proves the hook is active and unchanged. The workflow tests
execute the exact embedded shell/Python block and compare it to the reference
matcher; they use local Git remotes, not GitHub or credentials. Temporary repos
use `PAPERCLIP_RUN_SCRATCH_DIR` or `RUNNER_TEMP` when set. Policy tests cover the
10 warn-mode cases (warnings print, exit 0).

To run the two real-checkout regressions, supply the local `dist/index.js` from
`actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683` (v4.2.2):

```sh
CHECKOUT_ACTION_DIST=/path/to/actions-checkout/dist/index.js \
  python3 -I -m unittest discover -s templates/org-github/tests -p 'test_policy_checkout.py' -v
```

The tests verify the bundled JavaScript's SHA-256
`9a320e563c4dc21e2291453f0c922906ef8d98cbf4961dfadb2eb4f1a0fcc15e`
before executing it. They do not download dependencies or use credentials;
Git URL rewrites point both checkout calls at disposable local repositories.
Missing or mismatched action source fails, never skips the test. These regressions
are a separate local verification command, not wired into the existing repo CI.

The staged starter intentionally has placeholder test/lint/secret-scan commands;
replace them before adoption. Its verbatim warn-mode policy lint reports three
known warnings: the reusable `changes` call cannot set a caller-side timeout
(the callee sets 5 minutes), and the always-on lint/secret placeholders have
10-minute ceilings. Keep these warnings visible; do not disable the lint or
add unsupported YAML to silence them. Pin the reusable workflow and verify
real docs-only/dependency PRs after applying; local fixtures are not live CI proof.

This staging directory is private. Before copying into a public org repo, the
Operator must remove private card IDs/links from file content and use a public-safe
branch/PR/commit. The reference policy-lint files remain byte-identical here.
