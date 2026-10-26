# paperclip-ops

Operator tools, offline tests, and automation components for Paperclip deployments.
Licensed under MIT, matching the companion
[paperclip-operator-toolkit](https://github.com/TogetherWeOwn/paperclip-operator-toolkit).

## Fresh-history publication candidate

This tree is a sanitized source snapshot, not a clone of the previous operational
repository. Its initial commit has no parents. Historical commits, deployment
backups, live configuration, operational evidence, private runbooks, member data,
and credential references are intentionally excluded. Never import old Git
objects, bundles, branches, tags, or PR commits into this repository.

Deployment-specific identifiers have been replaced with reserved example
addresses and synthetic identifiers. They are not working production defaults.
Keep real configuration and credentials in an external private store; inject them
at installation/runtime. **Do not deploy this snapshot over a running host.**
Source-path mappings and private overlays require independent integration review
before a host can switch to a release from this repository.

Public-facing workflows use standard GitHub-hosted runners. Private infrastructure
canary, telemetry, and live review-trigger workflows are not included. The
retained integration workflows still need an exact-head compatibility pass after
the private-data extraction; a passing secret scan is not a claim that every
legacy suite passes or that this is a production-ready release.

## Safety checks

`.gitleaks.toml` extends the scanner's default rules. Its exceptions require both
an exact file path and the reviewed literal or test line, scoped to named rules.
They cover synthetic test
credentials and file-integrity hashes, not directory-wide exclusions. Exceptions
for removed historical fixtures do not authorize restoring those private files.

Run the secret and privacy checks before adding a new commit. The public-safety
workflow checks the working tree and commits introduced by the current pull
request or push; it does not rescan every fetched branch or main's earlier
history. A push whose old tip is missing (force-push) scans all reachable
history instead, since the rewritten commits are unknown. Previously landed
commits were covered by the scans that ran when they were introduced. Do not
disable the workflow to import a patch. Changes derived
from an old private PR must be reviewed as plain diffs and committed freshly;
keep author/card custody on the private board, not in public branch names, commit
messages, or PR text.

The optional toolkit restoration script uses pinned public archives and checksums.
Its legacy compatibility manifest is not a release-readiness certificate: verify
restored paths and their contents after sanitization before using it for a build.
Never add a private artifact to make an old fixture pass.

## What is here

Tracked root operator utilities are indexed below. This is a source index, not an
execution or deployment checklist; inspect each utility's usage and prerequisites
before running it. `cliproxy_seam_extract.py` is an exempt one-shot forensic
parser, not a standing operator command. Test entry points begin with `test_`.

For `host_db_backup.sh`, see the [publication receipt contract](docs/host-db-backup.md)
for offline discovery, no-write planning, machine outcomes and fixture checks.
This documentation does not authorize running a backup or changing a host.

For the external, alert-only workspace sync-back size observer, see the
[byte measurement and alert contract](docs/workspace-sync-back-signal.md).
It does not instrument transport or authorize host installation.

For the offline card/run/PR/head/deploy trace correlator, see the
[evidence, join and SLO contract](docs/trace-correlation.md).
It reads caller-assembled evidence only and authorizes no host action.

For the pure offline adapter from a pinned secret-scan outcome to a
native finding payload (or a refusal), see the
[scanner finding adapter contract](docs/scanner-finding-adapter.md).
It holds no credential, makes no network calls, and authorizes no live
or automated invocation.

For source-to-documentation accompaniment in pull requests, see the portable
[documentation drift gate](docs/docs-drift.md), including its JSON contract,
base-policy trust model and reusable CI interface.

For native skill versions, reusable examples and isolated pinned-version tests,
see the [versioned skill fixture procedure](docs/versioned-skill-fixtures.md).
It adds no runtime registry, host automation or skill-assignment authority.

For one policy across plan, build, and review phases, see the
[role, provider, and rubric coherence procedure](docs/role-provider-rubric-coherence.md).
It scopes guidance to role and phase, keeps task policy provider-independent,
and cites a single pinned rubric in every phase.

| Tool |
| --- |
| `public_snapshot_test_support.py` (test-only private-artifact boundary; not a runtime policy or operator command) |
| `acceptance_transport.sh` |
| `agent_error_proposer.py` |
| `approval_timeout_probe.sh` |
| `balancer_silence_guard.py` |
| `blocker_test_checks.py` |
| `bridge_row_inventory.py` |
| `capture_host_evidence.sh` |
| `capture_host_render.sh` |
| `check_workflow_job_permissions.py` |
| `case_8abb7523-measure.mjs` |
| `ci_dark_steps.sh` |
| `ci_failure_drain.sh` |
| `ci_node93f4bccf_guard.py` |
| `ci_required_checks_audit.py` |
| `ci_required_checks_ownership.py` |
| `claude_countdown.py` |
| `codex_countdown.py` |
| `coolify_capacity_headroom.sh` and `coolify_capacity_headroom.py` ([offline evidence contract](docs/capacity-evidence.md); keep these siblings together) |
| `credential_chain_audit.sh` |
| `credential_chain_lockdown.sh` |
| `db_backup_stall.sh` |
| `db_backup_stall_monitor.sh` |
| `db_idle_transaction_stall.js` |
| `deploy_window_manifest.py` |
| `deploy_window_open.sh` |
| `dlq_redrive.sh` |
| `docker_hygiene.sh` |
| `dropchannel_scan.sh` |
| `garm_image_lint.py` |
| `garm_image_lint.sh` |
| `garm_provider_latency_baseline.py` |
| `garm_ready_time_baseline.py` |
| `gh_access.sh` |
| `gh_token.sh` |
| `hook_spawn_guard.sh` |
| `host_db_backup.sh` |
| `kofra_preview_parity.py` |
| `migration_drift_gate.py` |
| `migration_rehearsal.sh` |
| `migration_sql.py` |
| `migration_squawk.sh` |
| `migration_timeout_preamble.sh` |
| `muse_countdown.py` |
| `native_ssh_citation_gate.py` (historical fork audit; [official-stable retirement scope](docs/official-stable-fork-controls.md)) |
| `notify_paperclip_issue.sh` |
| `omniroute_combo_cli.sh` |
| `op_queue_reject.sh` |
| `org_provisioner.sh` |
| `org_request_queue.sh` |
| `pacer_admission_audit.py` |
| `pacer_apply.py` |
| `pacer_concurrency.py` |
| `pr_closeout_ledger.py` |
| `pr_closeout_snapshot_repair.py` |
| `process_lost_probe.py` |
| `public_comment_hygiene_gate.sh` |
| `quota_rotation_watch.py` |
| `recovery_writer.py` |
| `red_main_poll.sh` |
| `review_route.sh` |
| `review_routing_weekly.py` |
| `runner_disk_pressure.sh` |
| `runner_disk_triage.sh` |
| `schema_drift.sh` |
| `shell_subst_guard.py` |
| `sibling_guard.sh` |
| `two_bot_staging_precheck.py` |
| `upstream_draft_pin_audit.sh` |
| `upstream_gate.sh` |
| `upstream_lint.py` |
| `upstream_prepush_hook.sh` |
| `upstream_reputation.py` |
| `upstream_watch.sh` |
| `warn_first_inventory.py` |
| `websearch_lane_probe.py` |

## Planning depth

Use [right-sized planning](docs/right-sized-planning.md) to bound a reviewable
slice, choose preparation by risk/coupling, and record prototypes, investment
reviews and followups in native task fields. It supplements existing acceptance
criteria and anti-goals; publishing the guide is not company instruction rollout.

## Native delivery workflow

Use the [single-doer delivery contract](docs/single-doer-delivery.md) for reusable
planner/builder fields, deterministic defect-routing decisions, and a
revision-linked multi-repository manifest. These are native task/document
conventions, not an automatic dispatcher or a production ownership catalog.

## Productivity reconcilers

For confirmed mutation counts, safe CLI modes, versioned JSON summaries and an
offline verification command, see [Reconciler outcomes](docs/reconciler-outcomes.md).
The source contract does not imply runtime installation; flow guard's existing
no-flag invocation is mutating.

## Task authoring

Use the [acceptance criteria and anti-goals contract](docs/task-scope.md) for
implementation task descriptions. It includes a copy-ready template, verifiable
milestones for oversized work, and an adoption fragment for an existing company
adapter. These are native-field authoring guidelines, not an installed skill or
a new admission gate; source publication does not establish live adoption.

## Verification

Run the fast tier with `make verify-fast` (`make verify-fast-parse` for the
parse gate only). To inspect suite scope and prerequisites without running or
preparing anything, use `make verify-fast-plan` (offline, read-only;
`scripts/verify-fast.sh --plan --json`).

The code-nav suite needs PyYAML. On a clean Linux x86_64 installation, prepare
an isolated Python 3.12 environment outside the checkout, then run the fast tier
with that interpreter on `PATH`:

```sh
python3.12 -m venv "$HOME/.cache/paperclip-ops/verify-fast-venv"
. "$HOME/.cache/paperclip-ops/verify-fast-venv/bin/activate"
python3 -m pip install --only-binary=:all: --no-deps --require-hashes \
  -r scripts/static-label-guard-requirements.txt
make verify-fast
```

This reuses CI's hash-pinned PyYAML 6.0.3 requirement; it does not install into
system Python. An already-provisioned interpreter with PyYAML 6.0.3 also works.
`--plan --json` reports parser readiness, and `--no-prepare` refuses when it is
missing. Normal execution does not automatically prepare this Python environment.

## Contributing

Use a feature branch and a reviewed pull request. Report actual tests and their
results, preserve the attribution of original contributors, and keep hostnames,
private network addresses, member details, secrets, and internal task references
out of public text and files. Production configuration is outside this tree.
