# protection-rule

Custom deployment protection rule, one Org App endpoint serving several
environments ([case_61f942c4](/TOG/issues/case_61f942c4) slice C, generalized per owner
2026-10-04 for production launches). It answers GitHub
`deployment_protection_rule` webhooks and **approves** only on a recorded CEO
GO for the exact artifact under review. It **rejects** everything else.

Two decision modes, selected per environment by the policy table in
`src/environments.js`:

- `staging-migrate-apply` on `TogetherWeOwn/two-bot-next` (`plan` mode): a
  successful unprotected claim job publishes the request before apply waits;
  the prior plan's **Rust projection digest**, source, target, pending versions
  and approval references match that authenticated claim, **green required CI
  and an independent Paperclip Review are authenticated on every commit the
  apply trusts** (migration source, producing plan head, apply workflow head),
  **and** the CEO posted `GO <64hex plan hash>` on the pinned parent card.
  Workflow head SHAs identify workflow code; `source_sha` identifies the
  reviewed checkout. They need not equal each other.
- `production` on `TogetherWeOwn/kagi-shinobi`, `TogetherWeOwn/two-web-next`
  and `TogetherWeOwn/two-bot-next` (`sha` mode): the CEO posted
  `GO <40hex head SHA>` for the exact commit under review on that
  environment's card. Production deploys carry no plan artifact, so the GO
  binds the commit itself.

The GO card is part of the approval key: a GO posted anywhere else approves
nothing. Adding an environment is a code change on purpose — scope that moves
without review is how an approver starts approving the wrong thing.

This replaces the owner reviewer on staging first; once live and signed off
([case_a13f9e23](/TOG/issues/case_a13f9e23)), the owner reviewer on the production
environments is replaced with this rule. Until an environment's rule is live
and registered, its owner reviewer stays in place (rollback posture).

## 1. Read this before you rely on it

**This is a closed-loop approver, not a monitor.** A silent endpoint does not
mean "no deployments pending" — it means nothing was answered. If this service
is down, deployments hang until GitHub's protection-rule timeout expires and
fails them closed. Watch the service, not the absence of approvals.

**A row of approval proves the secret was held, not that GitHub asked.** The
webhook signature proves possession of the App webhook secret. Anyone holding
that secret can forge a delivery that approves a deployment that was never
requested. The decision inputs are therefore recomputed from fresh API reads
(run, artifacts, comments) — never from delivery payloads — but the trigger
itself is still trust in the secret. Guard it like a credential, because it is
one.

**CEO GO is board text matched by narrow markers.** A comment counts only
when authored by the CEO agent identity and naming a digest bound to the
literal `GO` marker (`GO <64hex>` for plan hashes, `GO <40hex>` for deploy
SHAs). The two markers are disjoint by construction. Ordinary discussion never
approves. If the marker convention changes, update `src/board.js` and its
suite together — a scanner that matches more than the convention says is an
approval forged by prose.

## 2. How it decides

For each `deployment_protection_rule` delivery:

1. Verify the webhook signature (fail-closed: 401 unsigned, 503 unconfigured,
   413 oversized — all before any credential is used).
2. Resolve the (repository, environment) pair against the policy table.
   Foreign repos get the HTTP error, never a verdict; unserved environments
   on known repos get the HTTP error too, so the deployment never hangs on an
   answer this App cannot deliver.
3. Mint ONE installation token for THIS installation only, from the
   configured App identity. On any mint failure the verdict is REJECT; no
   other credential is ever tried (never substitute credentials).
4. Plan mode: authenticate the main `workflow_dispatch` run's repository,
   workflow path/id, head and current attempt; fresh-read the pending
   `staging-migrate-apply` environment and successful attempt-scoped plan/claim
   jobs. Read exactly one non-expired `staging-migrate-apply-claim` artifact,
   containing `staging-migrate-apply-claim.json` and the strict v1 claim.
   Its timestamp must fall within that claim job, and its run/attempt/head
   bindings must agree. Never read a step inside the waiting apply job.
5. Authenticate the prior producing run in the same repository/workflow on
   main: dispatch, completed/success, successful plan job, skipped apply job
   (plan-mode provenance). Read exactly one non-expired
   `staging-migrate-manifest` artifact containing `staging-migrate-manifest.json`.
   Reconcile its read-only manifest and recompute the exact Rust projection:
   `source_sha` plus LF, each pending decimal version plus LF, then every source
   migration `version:description:sha384` plus LF, in producer order. Require
   computed digest == embedded digest == claim. Compare source/target/pending/
   recovery/ACL refs separately; the digest does not include all those fields.
   Verify the source is in both workflow heads' main histories.
6. Source evidence (`src/source.js`), for each distinct commit in
   {`claim.source_sha`, apply workflow head, producing plan head}. Ancestry and
   a CEO GO are not review, so each commit must prove two things from fresh
   GitHub reads, never from the delivery:
   - **CI**: every required check in the policy (`ci-ok`, `gitleaks`,
     `pr-lint`) has at least one run **on that exact commit**, produced by the
     pinned CI App (GitHub Actions, `15368`), and every such run is
     `completed`/`success`. Skipped, neutral, pending, another App's run and a
     run bound to another commit are not CI. Failure reason:
     `source_ci_unverified`.
   - **Review**: the commit is the `merge_commit_sha` of exactly one pull
     request merged into this repository's `main` (a squash merge makes a new
     commit; the review ran on the pull request head, and `merge_commit_sha` is
     GitHub's own link back). On that head there is exactly one latest
     `Paperclip Review` run from the pinned review App (`5191093`,
     `togetherweown-reviewer`), `completed`/`success`, titled `5/5`, bound to
     that head, recorded against that pull request number and head (its
     `external_id` ends `:<number>:<head sha>`), and finished no later than the
     merge. Absent, ambiguous, lower-scored, stale, other-App, other-pull-request
     or post-merge review fails with `source_review_unverified`; a result that
     does not cover all three commits is `source_evidence_missing`. A check-run
     list whose `total_count` exceeds 100 or disagrees with its rows is refused,
     and so is a pull-request list of 100 (it has no total, so a full page may be
     truncated).
7. Scan the pinned card for CEO GO on the exact digest and reconfirm the
   current apply attempt/environment before deciding. SHA mode remains the
   existing exact workflow-head CEO-GO policy; no claim, plan or source reads.
8. `POST` the verdict to `deployment_callback_url`: `{ environment_name,
   state: approved|rejected, comment }`. The comment carries the stable
   reason id only — no hashes, no SHAs.

`src/decide.js` is the pure decision table; every other module establishes
one of its inputs. `decide` approves exactly two rows (verified plan + source
evidence + GO, exact-SHA GO) and rejects every other shape, including every
error shape. In plan mode the source-evidence gate sits after the plan/claim
bindings and before the CEO GO, so a posted GO never masks an unreviewed
source.

## 3. Run it (operator)

Secrets travel by inherited environment, never argv. All four are required;
the server refuses to start half-configured (exit 2):

- `PROTECTION_RULE_WEBHOOK_SECRET` — App webhook secret
- `PROTECTION_RULE_APP_ID` — GitHub App id for installation-token minting
- `PROTECTION_RULE_KEY_FILE` — path to the App RSA key (0600, read once)
- `PROTECTION_RULE_BOARD_TOKEN` — Paperclip credential for the CEO-GO scan
- `PROTECTION_RULE_BOARD_ORIGIN` — board origin, e.g. `https://board.example`
- `PROTECTION_RULE_PORT` — listen port (default 8788, loopback only)

The protected scope — (repository, environment) pairs plus each pair's GO
card — is pinned in `src/environments.js`, not configured: there is no scope
override variable, so an operator can never set a scope the service ignores.

```bash
node src/server.js
```

`GET /health` reports liveness only — never configuration or review history.
`POST /protection-rule` is the webhook route. Unknown paths are 404.

Register the endpoint as a custom deployment protection rule on each served
environment (operator host packet [case_d6f400af](/TOG/issues/case_d6f400af) for
staging; production registration follows CISO sign-off
[case_a13f9e23](/TOG/issues/case_a13f9e23)): the App needs Deployments read+write and
the `deployment_protection_rule` event subscription, installed on every
served repository.

## 4. Test it

```bash
npm run check   # parse every module and test
npm test        # offline producer-shaped contract + signature/GO/callback suites
```

The suite imports Bot publisher/Rust golden vectors at a pinned pushed revision,
actual archiver-produced stored ZIPs, and trimmed real GitHub check-run and pull-request
responses for a merged Bot commit (CI and `Paperclip Review`). See
[`test/fixtures/README.md`](test/fixtures/README.md) for source hashes and
regeneration. These are synthetic offline producer-contract checks, **not** a
live staging or callback test. No DB/credential operation is needed.

Run the hand-mutation proof from the repository root:

```sh
node protection-rule/test/mutation-check.cjs  # requires PAPERCLIP_SCRATCH_DIR
```

It copies to run-owned scratch, proves the baseline green, parse-checks each
mutant and requires an actual failed test, not a timeout or process crash.
Mutants cover artifact naming, byte-vs-projection hashing, truncated IDs,
producing provenance, embedded-hash-only trust, target binding, failed claim
jobs, descriptor refusal, absent CEO GO and rerun inheritance, plus source
evidence: unpinned CI or review App, ignored conclusion/score/head, review after
merge, ambiguous or unmerged pull request, wrong base, truncated pages, the
workflow/plan heads left unchecked, and an optional or partial evidence gate.

## 5. Engineering repair is not live approval

[case_25ea29ac](/TOG/issues/case_25ea29ac) repairs the consumer, coordinated with the
Bot producer repair [case_160d4f87](/TOG/issues/case_160d4f87). Interim environment
reviewers **stay**. Merge does not register an environment, remove a reviewer,
exercise a live callback, dispatch migrations or authorize production. The
replacement ruling remains on the same
[CISO review](/TOG/issues/case_a13f9e23#document-ciso-review).

[case_b995c00c](/TOG/issues/case_b995c00c) adds the source-evidence gate above, the
remaining source finding in the same CISO review (2026-10-07 re-review). It is
source only: it registers nothing, creates no credential and changes no
environment. Three choices are the repair's reading of the finding and return to
that CISO review rather than being treated as settled:

- **Which commits.** Evidence is required on the migration source, the
  producing plan head and the apply workflow head, because each runs code or
  yields data the apply trusts (the runner is built from the source; the plan
  and claim jobs run workflow YAML from their heads). Narrowing the set is one
  line in `app.js`.
- **Review identity.** The review run's `external_id` shape
  (`<id>:<pull request number>:<head sha>`) was observed on live review runs (PR #626 and #633 heads), not a
  documented contract; the gate requires its tail and fails closed if the
  format changes.
- **Squash-merge binding.** Review is tied to a squash commit through GitHub's
  `merge_commit_sha`, not through tree equality with the reviewed head: a squash
  of a head onto a moved base can differ in tree while carrying the reviewed
  diff. Stronger content binding was not invented here.

"Independent" means produced by the pinned review App. GitHub cannot attribute a
pull request to one agent (every agent pushes as one App), so author-versus-
reviewer separation is not provable from these reads and is not claimed.

Before any separately authorized registration, verify the installed App has
Actions read for runs/jobs/artifacts/pending environments, Contents read for
source ancestry comparisons, Checks read for check runs, Pull requests read
for the commit-to-merged-pull-request association, and Deployments read/write
for the verdict. (Whether the public Bot repository serves the two new reads
to a token without those two grants is not verified here: confirm it at the
separately authorized install. A missing grant fails closed.) This repair does not create, rotate or broaden a credential. A missing grant
fails closed; it is never answered by substituting another identity.

Evidence budgets fail closed: two MiB per metadata response, at most 100 jobs,
artifacts or check runs and fewer than 100 associated pull requests (a truncated
page is refused), 16 MiB per downloaded ZIP, one
regular one-MiB JSON file, 40 JSON nesting levels. IDs and attempts are full
positive canonical int64 decimal strings; migration versions retain Rust i64
precision. Duplicate keys, floats/exponents, duplicate versions, unsafe ZIPs
and mismatched run/job/attempt/target bindings refuse. Download bodies are
bounded while streaming and have a deadline. Tokens go only to the exact API
origin; object-storage redirect requests never carry Authorization.
