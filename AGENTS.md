# paperclip-ops — agent start here

Consumer: **agent**. This is a repository map, adapted from the
[agent seed](templates/org-github/repo-template/AGENTS.md), not a replacement for
higher-priority instructions, task scope or execution authority.

## Find the source and the doer

| Need | Start here |
| --- | --- |
| Root operator utilities | [README source index](README.md#what-is-here); inspect the named script and its sibling `test_*` files |
| Focused checks and prerequisites | [Verification inventory](scripts/verify-fast-inventory.json), selected through the read-only plan below |
| Shared helpers and operational components | [lib/](lib/), [ops/](ops/), [plugins/](plugins/); follow the changed component's local docs and tests |
| GitHub checks and reusable repository templates | [Workflows](.github/workflows/), [check implementations](.github/scripts/), [org templates](templates/org-github/README.md) |
| Installation and runtime boundary | [Publication boundary](README.md#fresh-history-publication-candidate), [host bootstrap](host-bootstrap/README.md), [execution image](execution-image/README.md) |
| Native task ownership and defect routing | [Single-doer delivery](docs/single-doer-delivery.md#deterministic-defect-routing); the native assignee is authoritative, not this directory map |

Use tracked paths and full base/head commit SHAs in evidence. Inventory suite IDs
identify checks; they are not service-owner identities. Keep private task IDs,
actual ownership records and delivery evidence on the native task, not here.
Pin a task document by key **and saved revision**, rather than an unqualified
“latest” link. The [delivery manifest procedure](docs/single-doer-delivery.md#one-revision-linked-manifest)
explains revision-safe updates; do not introduce another ownership registry.

## Choose verification before running it

Run from the repository root. Canonical environment setup and execution details
live in [README verification](README.md#verification); this is the mode selector.

| Intent | Shipped command | Boundary |
| --- | --- | --- |
| Discover options | `bash scripts/verify-fast.sh --help` | Help only |
| Inspect scope and readiness | `make verify-fast-plan` | Offline, read-only plan; use `bash scripts/verify-fast.sh --plan --json` for clean JSON stdout |
| List stable suite IDs | `bash scripts/verify-fast.sh --list --json` | Offline discovery, not execution; locate a focused command in the plan, not an invented suite-selector flag |
| Run without dependency preparation | `bash scripts/verify-fast.sh --no-prepare` | Executes checks; not a read-only plan |
| Run the default fast tier | `make verify-fast` | Executes checks and may prepare dependencies; **not read-only** |
| Parse gate only | `make verify-fast-parse` | Executes the parse gate, not the full fast tier; **not read-only** |

Fixture-only verification is not a staging test, a live API probe or deployment
evidence. Follow each component's documented prerequisites; an execution mode
is not permission to touch production. A missing prerequisite is a refusal to
address, not a check to bypass. Publishing source does not install a runtime,
activate automation or authorize host hooks, timers, cron or database writes.

## Native procedures, one canonical home each

- Task outcome, acceptance criteria and anti-goals: [task scope](docs/task-scope.md).
- Slice size, preparation depth, prototypes and reconsideration triggers:
  [right-sized planning](docs/right-sized-planning.md). Use native `plan` documents
  and real dependency edges, not a parallel planning system.
- One doer through implementation and fixes, defect lookup and multi-repository
  evidence: [single-doer delivery](docs/single-doer-delivery.md).
- Saved examples, immutable skill versions, isolated runs and replay verdicts:
  [versioned skill fixtures](docs/versioned-skill-fixtures.md). Library presence
  is not assignment authority; a completed run is not an assertion pass.

These links describe reusable procedures, not installed company instructions or
proof of live adoption. Preserve independently authored acceptance tests.

## Code lookup, diagnostics and proof

Search tracked paths/symbols before reading whole components. If symbolic
navigation is already provisioned, use the [code-nav usage](ops/agent-tooling/serena-pilot-b/BUILDER_TOOLS_SNIPPET.md)
and [source](ops/agent-tooling/serena-pilot-b/code_nav.py); it starts a server and
uses external cache/config state, so it is not a no-side-effect discovery check.
This map does not install it. Missing tooling is not a reason to change the harness.

For machine outcomes and remediation, follow the existing
[documentation gate JSON contract](docs/docs-drift.md#local-check) and
[reconciler mode/outcome contract](docs/reconciler-outcomes.md), then inspect the
reported source path, stable code and next action. Do not assume every legacy
script has the same JSON or dry-run interface. This map adds no executable API.

Before a PR, validate changed relative links and examples, run the applicable
[documentation gate](docs/docs-drift.md) and [public-safety checks](.github/workflows/public-safety.yml),
and record exact commands, exits and unverified effects. Undo this documentation
with a reviewed revert; no host rollback or installation is implied.

Measure discovery on five new delivery tasks over seven days: correct focused
check/native procedure found first attempt, without clarification, out of tasks
sampled. Report failures and an empty sample honestly; the map alone proves no
speed, token or cost improvement. Keep sample evidence on the private task.
