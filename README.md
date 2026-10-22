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

| Tool |
| --- |
| `public_snapshot_test_support.py` (test-only private-artifact boundary; not a runtime policy or operator command) |
| `acceptance_transport.sh` |
| `agent_error_proposer.py` |
| `approval_timeout_probe.sh` |
| `balancer_silence_guard.py` |
| `bridge_row_inventory.py` |
| `capture_host_evidence.sh` |
| `capture_host_render.sh` |
| `case_8abb7523-measure.mjs` |
| `ci_dark_steps.sh` |
| `ci_failure_drain.sh` |
| `ci_node93f4bccf_guard.py` |
| `ci_required_checks_audit.py` |
| `ci_required_checks_ownership.py` |
| `claude_countdown.py` |
| `codex_countdown.py` |
| `coolify_capacity_headroom.sh` |
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
| `native_ssh_citation_gate.py` |
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

## Productivity reconcilers

For confirmed mutation counts, safe CLI modes, versioned JSON summaries and an
offline verification command, see [Reconciler outcomes](docs/reconciler-outcomes.md).
The source contract does not imply runtime installation; flow guard's existing
no-flag invocation is mutating.

## Contributing

Use a feature branch and a reviewed pull request. Report actual tests and their
results, preserve the attribution of original contributors, and keep hostnames,
private network addresses, member details, secrets, and internal task references
out of public text and files. Production configuration is outside this tree.
