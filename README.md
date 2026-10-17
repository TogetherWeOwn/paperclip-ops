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
workflow checks both the working tree and all fetched Git history. Do not disable
it to import a patch. Changes derived from an old private PR must be reviewed as
plain diffs and committed freshly; keep author/card custody on the private board,
not in public branch names, commit messages, or PR text.

The optional toolkit restoration script uses pinned public archives and checksums.
Its legacy compatibility manifest is not a release-readiness certificate: verify
restored paths and their contents after sanitization before using it for a build.
Never add a private artifact to make an old fixture pass.

## What is here

Tracked root operator utilities are indexed below. This is a source index, not an
execution or deployment checklist; inspect each utility's usage and prerequisites
before running it. `cliproxy_seam_extract.py` is an exempt one-shot forensic
parser, not a standing operator command. Test entry points conventionally begin
with `test_`; imported `test-` entry points are indexed too.

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
| `muse_countdown.py` |
| `native_ssh_citation_gate.py` |
| `notify_paperclip_issue.sh` |
| `omniroute_combo_cli.sh` |
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
| `two_bot_staging_precheck.py` |
| `upstream_draft_pin_audit.sh` |
| `upstream_gate.sh` |
| `upstream_lint.py` |
| `upstream_prepush_hook.sh` |
| `upstream_reputation.py` |
| `upstream_watch.sh` |
| `warn_first_inventory.py` |
| `websearch_lane_probe.py` |

### Consolidated toolkit entry points

These additional root utilities, libraries, and test entry points retain the same
source-only status and require inspection before use.

| Tool |
| --- |
| `acceptance_org_lib.sh` |
| `acceptance_rehearsal.sh` |
| `acp_crash_triage.sh` |
| `agent_endpoint_preflight.sh` |
| `app_tree_guard.sh` |
| `auth_denial_discriminator.py` |
| `blocked_audit.sh` |
| `breaker_cotenancy_probe.sh` |
| `capability_gate.sh` |
| `capture_network_name_evidence.sh` |
| `channel_drift.sh` |
| `cliproxy_quota_contract.py` |
| `cliproxy_quota_controller.py` |
| `cliproxy_release_preflight.sh` |
| `cold_start_detector.sh` |
| `discord_job_health_monitor.sh` |
| `dispatch-acceptance-measurement.py` |
| `external_disclosure.js` |
| `gh_ci_status.sh` |
| `gh_embedded_token_remediate.py` |
| `gh_override_scope_audit.sh` |
| `gh_scope_residue.sh` |
| `gh_scope_residue_monitor.sh` |
| `git_remote_credential_scan.sh` |
| `interaction_envelope_lint.sh` |
| `interaction_route.sh` |
| `interaction_triage.sh` |
| `liveness_reconciler.py` |
| `liveness_reconciler_source.js` |
| `model_lane_probe.sh` |
| `mount-escape-repro.sh` |
| `op_approval_executor.sh` |
| `opencode_shared_budget_probe.sh` |
| `operator_runbook.sh` |
| `org_access_review.sh` |
| `org_orphan_retire.sh` |
| `pacing_verdict.py` |
| `paperclip_activation_gate.sh` |
| `pg_source.js` |
| `plugin_deploy_drift.sh` |
| `plugin_manifest_gate.sh` |
| `post_rollup_comment.sh` |
| `predispatch-deploy-gate.sh` |
| `queue_liveness.sh` |
| `quota_brake.sh` |
| `quota_burn_derive.py` |
| `rehearsal_authorized_preflight.sh` |
| `rehearsal_endpoint_preflight.sh` |
| `review_dedup.py` |
| `runner_capacity_headroom.sh` |
| `secret_exposure_audit.sh` |
| `sibling_guard.sh` |
| `skills.sh` |
| `test-dispatch-acceptance-measurement.py` |
| `test-effective-boundary-reconcile.sh` |
| `test-grant-declaration-symmetry.sh` |
| `test-host-audit-wrapper-checks.sh` |
| `test-mapping-guard-calibration.mjs` |
| `test-orphan-grant-retirement.sh` |
| `test-predispatch-deploy-gate.sh` |
| `test-selfhosted-runner-gate.sh` |
| `tool_drift.sh` |
| `upstream_filing_readiness.sh` |

## Contributing

Use a feature branch and a reviewed pull request. Report actual tests and their
results, preserve the attribution of original contributors, and keep hostnames,
private network addresses, member details, secrets, and internal task references
out of public text and files. Production configuration is outside this tree.
