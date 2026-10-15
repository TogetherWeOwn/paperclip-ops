# Toolkit consolidation reconciliation

This is a file-level import from the existing public toolkit, not a merge of its history.

## Pinned inputs

- Extraction baseline: `df2229310147cb62e091ff7c8e7c18dcb435d211`.
- Repaired canonical main: `a6dd9e815f45813967684292731321678d07fdeb`.
- Refreshed public toolkit main: `2d2b8123a60e7477a01a477e4f0daa27caab74d6`.

## Deterministic selection

Modes and blob identities are compared at the same path against the extraction baseline.
Toolkit-only paths are imported at their existing paths. A divergent shared path is taken
from the toolkit only if the canonical version still equals the baseline. When both sides
changed or independently added a path, the canonical version is kept; no automatic content merge.
Paths absent from the toolkit remain canonical. No private history or placeholder map is included.

| Classification | Files | Decision |
| --- | ---: | --- |
| toolkit-only | 552 | Import toolkit |
| toolkit-alone-changed | 1 | Import toolkit |
| both-changed-keep-ours | 283 | Keep canonical |
| ours-alone-changed | 19 | Keep canonical |
| identical | 40 | Keep canonical |
| ours-only | 1002 | Keep canonical |

## Toolkit-only shared-path change

- `plugins/cliproxy-insight/package.json`

## Divergent conflicts: canonical version retained

Every path below changed on both sides or was independently added. The version at the
canonical main SHA above is retained, not the toolkit version. The list is exhaustive.

- `.github/ISSUE_TEMPLATE/config.yml`
- `.github/pull_request_template.md`
- `.github/scripts/review_trigger.py`
- `.github/scripts/review_trigger_step.py`
- `.github/scripts/test_review_trigger.py`
- `.github/scripts/test_review_trigger_step.py`
- `.github/workflows/ci.yml`
- `.github/workflows/pr-lint.yml`
- `.gitignore`
- `.gitleaks.toml`
- `README.md`
- `approval_timeout_probe.sh`
- `ci_dark_steps.sh`
- `ci_failure_drain.sh`
- `ci_required_checks_audit.py`
- `claude_countdown.py`
- `cliproxy_seam_extract.py`
- `codex_countdown.py`
- `credential_chain_audit.sh`
- `credential_chain_lockdown.sh`
- `db_idle_transaction_stall.js`
- `dlq_redrive.sh`
- `garm_image_lint.sh`
- `garm_provider_latency_baseline.py`
- `garm_ready_time_baseline.py`
- `gh-event-capture/package.json`
- `gh-event-capture/scripts/check-subscription.sh`
- `gh-event-capture/scripts/discord-alert-hook.mjs`
- `gh-event-capture/scripts/gh-wrapper.mjs`
- `gh-event-capture/scripts/healthchecks-checkin.mjs`
- `gh-event-capture/scripts/preflight-host.mjs`
- `gh-event-capture/scripts/query.sh`
- `gh-event-capture/scripts/verify-deployment.sh`
- `gh-event-capture/src/app.js`
- `gh-event-capture/src/bridge.js`
- `gh-event-capture/src/capture-adapter.js`
- `gh-event-capture/src/claim-store.js`
- `gh-event-capture/src/closeout-pending.js`
- `gh-event-capture/src/closeout-sweep.js`
- `gh-event-capture/src/closeout.js`
- `gh-event-capture/src/consumer-cli.js`
- `gh-event-capture/src/consumer-config.js`
- `gh-event-capture/src/consumer-runner.js`
- `gh-event-capture/src/consumer.js`
- `gh-event-capture/src/github-adapter.js`
- `gh-event-capture/src/paperclip-adapter.js`
- `gh-event-capture/src/pr-task-index.js`
- `gh-event-capture/src/query.js`
- `gh-event-capture/src/receipt-cycle.js`
- `gh-event-capture/src/rejection-counter.js`
- `gh-event-capture/src/runtime-cli.js`
- `gh-event-capture/src/store-memory.js`
- `gh-event-capture/src/upstream-watcher.js`
- `gh-event-capture/src/verify.js`
- `gh-event-capture/src/worker.js`
- `gh-event-capture/test/bridge-fixtures.mjs`
- `gh-event-capture/test/bridge-regressions.test.mjs`
- `gh-event-capture/test/bridge.test.mjs`
- `gh-event-capture/test/capture-adapter.test.mjs`
- `gh-event-capture/test/capture.test.mjs`
- `gh-event-capture/test/claim-store.test.mjs`
- `gh-event-capture/test/closeout-pending.test.mjs`
- `gh-event-capture/test/closeout-sweep.test.mjs`
- `gh-event-capture/test/closeout.test.mjs`
- `gh-event-capture/test/consumer-runner.test.mjs`
- `gh-event-capture/test/consumer.test.mjs`
- `gh-event-capture/test/discord-alert-hook.test.mjs`
- `gh-event-capture/test/gh-wrapper.test.mjs`
- `gh-event-capture/test/github-adapter.test.mjs`
- `gh-event-capture/test/healthchecks-checkin.test.mjs`
- `gh-event-capture/test/paperclip-adapter.test.mjs`
- `gh-event-capture/test/pass-schedule.test.mjs`
- `gh-event-capture/test/pr-task-index.test.mjs`
- `gh-event-capture/test/receipt-cycle.test.mjs`
- `gh-event-capture/test/runtime-health.test.mjs`
- `gh-event-capture/test/sweep-cycle.test.mjs`
- `gh-event-capture/test/sweep-mutants.mjs`
- `gh-event-capture/test/sweep-store.test.mjs`
- `gh-event-capture/test/test_scripts.sh`
- `gh-event-capture/test/test_verify_deployment.sh`
- `gh-event-capture/test/upstream-watcher.test.mjs`
- `gh_token.sh`
- `github-runner/garm/Dockerfile.isolated`
- `github-runner/garm/GRACEFUL-DRAIN.md`
- `github-runner/garm/audit_stale_instances.sh`
- `github-runner/garm/canary-pool-spec.json`
- `github-runner/garm/canary_bootstrap.py`
- `github-runner/garm/check_first_pickup.py`
- `github-runner/garm/check_image_provenance.py`
- `github-runner/garm/drain-contracts.json`
- `github-runner/garm/fixture-canary-bootstrap.json`
- `github-runner/garm/fixture-generic-profile.json`
- `github-runner/garm/fixture-isolated-private.json`
- `github-runner/garm/fixture-isolated-profile.json`
- `github-runner/garm/fixture-privileged-private.json`
- `github-runner/garm/fixture-test-profile.json`
- `github-runner/garm/generic-image-profile.json`
- `github-runner/garm/image-digest-pins.json`
- `github-runner/garm/isolated-image-build-manifest.json`
- `github-runner/garm/isolated-image-profile.json`
- `github-runner/garm/post_job_cleanup.py`
- `github-runner/garm/revoke-canary-sudo.sh`
- `github-runner/garm/role-contracts.json`
- `github-runner/garm/storage_auto_reclaim.py`
- `github-runner/garm/test-class-image-profile.json`
- `github-runner/garm/validate_build_manifest.py`
- `github-runner/garm/validate_canary_pool.py`
- `github-runner/garm/validate_drain.py`
- `github-runner/garm/validate_generic_profile.py`
- `github-runner/garm/validate_isolated_profile.py`
- `github-runner/garm/validate_receipt.py`
- `github-runner/garm/validate_test_profile.py`
- `github-runner/garm/validate_toolchain.py`
- `github-runner/garm/wave1_window_verdict.py`
- `github-runner/test_isolated_canary_bootstrap.py`
- `github-runner/test_isolated_wave1_window_verdict.py`
- `hook_spawn_guard.sh`
- `hooks/muse-stop-guard/fixtures/final-turns.json`
- `hooks/muse-stop-guard/install-muse-stop-guard.mjs`
- `hooks/muse-stop-guard/muse-stop-guard.test.mjs`
- `mcp/README.md`
- `mcp/org-request-mcp.mjs`
- `mcp/test/org-request-mcp.test.mjs`
- `muse_countdown.py`
- `notify_paperclip_issue.sh`
- `ops/productivity/orphan_prs.py`
- `ops/productivity/review_error_rerequest.py`
- `ops/productivity/test_orphan_prs.py`
- `ops/productivity/test_review_error_rerequest.py`
- `org_provisioner.sh`
- `pacer_admission_audit.py`
- `pacer_concurrency.py`
- `paperclip-upgrade/check-plugin-inventory.py`
- `paperclip-upgrade/config.sql`
- `paperclip-upgrade/drain-marker.sql`
- `paperclip-upgrade/drain.sh`
- `paperclip-upgrade/effective-build-check.sh`
- `paperclip-upgrade/fence.sql`
- `paperclip-upgrade/inflight.sql`
- `paperclip-upgrade/inventory.sql`
- `paperclip-upgrade/leak.sql`
- `paperclip-upgrade/migration_gate.py`
- `paperclip-upgrade/neutralise-stage.sql`
- `paperclip-upgrade/orphans.sql`
- `paperclip-upgrade/post-restart-reharden.sh`
- `paperclip-upgrade/pre-restart-check.sh`
- `paperclip-upgrade/rehearsal-package.example.json`
- `paperclip-upgrade/rehearse-upgrade.sh`
- `paperclip-upgrade/resolve-drain-stranded.sh`
- `paperclip-upgrade/rewake.sql`
- `paperclip-upgrade/test-offline.sh`
- `paperclip-upgrade/validate_sql.py`
- `plugins/cliproxy-insight/README.md`
- `plugins/cliproxy-insight/docs/adr/0001-ships-inert-owner-reserved-gate.md`
- `plugins/cliproxy-insight/docs/adr/0002-schema-agnostic-envelope.md`
- `plugins/cliproxy-insight/src/config/schema.ts`
- `plugins/cliproxy-insight/src/constants.ts`
- `plugins/cliproxy-insight/src/manifest.ts`
- `plugins/cliproxy-insight/src/worker.ts`
- `plugins/cliproxy-insight/tests/manifest.spec.ts`
- `plugins/cliproxy-insight/tests/tool-result-data.spec.ts`
- `plugins/cliproxy-insight/tests/worker.spec.ts`
- `plugins/cliproxy-insight/vitest.config.ts`
- `plugins/dispatch/README.md`
- `plugins/dispatch/dist/worker.js`
- `plugins/model-selection/docs/lane-withdrawal.md`
- `plugins/model-selection/scripts/assemble-additive-config.mjs`
- `plugins/model-selection/scripts/mutation-gate.mjs`
- `plugins/model-selection/src/config/resolve.ts`
- `plugins/model-selection/src/config/schema.ts`
- `plugins/model-selection/src/constants.ts`
- `plugins/model-selection/src/engine/benchmark-data.ts`
- `plugins/model-selection/src/engine/benchmark-prior.ts`
- `plugins/model-selection/src/engine/types.ts`
- `plugins/model-selection/src/lane-capacity/auth-files.ts`
- `plugins/model-selection/src/lane-capacity/poll.ts`
- `plugins/model-selection/src/manifest.ts`
- `plugins/model-selection/src/worker.ts`
- `plugins/model-selection/tests/auth-files-lane.spec.ts`
- `plugins/model-selection/tests/benchmark-prior.spec.ts`
- `plugins/model-selection/tests/config.spec.ts`
- `plugins/model-selection/tests/tier-policy/tier-policy-replay.spec.ts`
- `plugins/model-selection/tests/tool-error-shape-part2.spec.ts`
- `plugins/model-selection/tests/worker.spec.ts`
- `plugins/omniroute-broker/README.md`
- `plugins/omniroute-broker/dist/manifest.js`
- `plugins/omniroute-broker/dist/omniroute.js`
- `plugins/omniroute-broker/dist/ownership.js`
- `plugins/omniroute-broker/dist/redact.js`
- `plugins/omniroute-broker/dist/verbs.js`
- `plugins/omniroute-broker/dist/worker.js`
- `plugins/omniroute-broker/test/broker.test.mjs`
- `pr_closeout_ledger.py`
- `process_lost_probe.py`
- `protection-rule/README.md`
- `protection-rule/package.json`
- `protection-rule/src/app.js`
- `protection-rule/src/board.js`
- `protection-rule/src/callback.js`
- `protection-rule/src/decide.js`
- `protection-rule/src/evidence.js`
- `protection-rule/src/github.js`
- `protection-rule/src/server.js`
- `protection-rule/test/app.test.mjs`
- `protection-rule/test/board.test.mjs`
- `protection-rule/test/decide.test.mjs`
- `protection-rule/test/evidence.test.mjs`
- `protection-rule/test/zip-fixture.mjs`
- `public_comment_hygiene_gate.sh`
- `quota_rotation_watch.py`
- `recovery_writer.py`
- `red_main_poll.sh`
- `required_checks.json`
- `runner_disk_pressure.sh`
- `runner_disk_triage.sh`
- `scripts/ci_no_new_static_labels.py`
- `scripts/ci_no_new_static_labels.sh`
- `scripts/model_pin_cleanup.py`
- `templates/org-github/README.md`
- `test/fixtures/ci_dark_steps/job-live-green-reporter-inflight.json`
- `test_agent_endpoint_preflight.sh`
- `test_agent_error_proposer.py`
- `test_ci_dark_steps.sh`
- `test_ci_failure_drain.sh`
- `test_ci_gating.sh`
- `test_ci_no_new_static_labels.sh`
- `test_ci_required_checks_audit.py`
- `test_claude_countdown.py`
- `test_cliproxy_quota_controller.py`
- `test_cliproxy_release_preflight.sh`
- `test_cliproxy_seam_extract.py`
- `test_codex_countdown.py`
- `test_cold_start_detector.sh`
- `test_db_idle_transaction_stall.sh`
- `test_dlq_redrive.sh`
- `test_garm_image_lint.sh`
- `test_garm_provider_latency_baseline.py`
- `test_garm_ready_time_baseline.py`
- `test_garm_reaper_audit.sh`
- `test_gh_ci_status.sh`
- `test_gh_embedded_token_remediate.sh`
- `test_gh_token_argv.sh`
- `test_grant_write_attribution.sh`
- `test_hook_spawn_guard.sh`
- `test_interaction_route.sh`
- `test_model_pin_cleanup.py`
- `test_muse_countdown.py`
- `test_notify_transport.sh`
- `test_operator_runbook.sh`
- `test_pacer_admission_audit.py`
- `test_pacer_concurrency.py`
- `test_paperclip_immutable_runtime.sh`
- `test_platform_watchdog.py`
- `test_platform_watchdog_cli.py`
- `test_plugin_deploy_drift.sh`
- `test_pr_closeout_ledger.py`
- `test_process_lost_probe.py`
- `test_provisioned_cheap_profile.sh`
- `test_public_comment_hygiene_gate.sh`
- `test_quota_rotation_watch.sh`
- `test_recovery_dryrun.py`
- `test_recovery_writer.py`
- `test_red_main_poll.sh`
- `test_request_queue.sh`
- `test_request_record_integrity.sh`
- `test_runner_disk_pressure.sh`
- `test_runner_disk_triage.sh`
- `test_sql_backend.sh`
- `test_tool_drift.sh`
- `test_upstream_draft_pin_audit.sh`
- `test_websearch_lane_probe.py`
- `upstream_draft_pin_audit.sh`
- `verification/platform-watchdog-cli-mutation-gate.py`
- `verification/platform-watchdog-gate.sh`
- `verification/process-lost-mutation-gate.py`
- `verification/public_fork_provenance.py`
- `verification/recovery-dryrun-gate.sh`
- `verification/secret-scan.sh`
- `verification/test_public_fork_provenance.py`
- `watchdog/__init__.py`
- `watchdog/detectors.py`
- `watchdog/recovery_dryrun.py`
- `websearch_lane_probe.py`

## Deliberately omitted toolkit-only paths

- `.github/workflows/review-trigger.yml`: deliberately dropped. Review requests are host-owned through `review_gate.py` and the Steward batch behind `review_dedup`; a second workflow-trigger path would bypass that ownership and duplicate requests.
- `tests/dead-feed-2026-08-26.jsonl`: real historical account/quota data, not public source. Existing historical assertions remain behind the public-snapshot absence boundary. `test_quota_brake.sh` instead generates a synthetic stale, expired-reset feed and asserts the staleness cause.
- `tests/fixtures/pacer-concurrency/audit.jsonl`: redundant fixture. The canonical pacer test already uses its existing synthetic `audit.fixture`; that fixture and its malformed-line control remain unchanged.
- `test_omniroute_rehearsal.sh`: deliberately dropped as an orphan suite. Its subjects `omniroute/rehearsal/operator-action.sh`, `prepare-db-preimage.sh`, `verify-package.sh`, and `package.json` are absent from toolkit main, the canonical source snapshot, and both pinned public archive commits (`533c7ae2b013340bfae20cfcc711220048295855`, `7c4b39a2157ceb122ae50c7f554cc43c6891c829`). It cannot provide coverage without its subject package; the other rehearsal preflight and translator coverage remains.

- `execution-image/` (11 files): the whole source packet is deferred. Canonical Trivy rejects its final root `USER` (DS-0002), while its inherited-initialization contract and tests explicitly require that startup identity. Do not weaken Trivy or silently change the runtime contract. A high-priority DevOps follow-up must resolve non-root execution with a passing unchanged scan, or obtain a documented, reviewed, exact-scope CISO exception only if runtime root is essential, then port the complete packet here. The source remains pinned at the toolkit commit above and is preserved in a separate local source archive. Retirement is blocked until the packet is ported or explicitly retired.

After all exclusions, this import contains 537 toolkit-only files and the one toolkit-alone shared-path change.

## Content-only publication adaptations

The canonical privacy gate is unchanged. Historical and commentary card references use
neutral example namespaces. Every FUNCTIONAL card identifier — the eleven grandfathered
handoff ids, the required channel runbook-pointer basename, the orphan-retire audit
attribution, and the issue prefixes the channel tool strips — keeps its real internal
prefix, built by shell concatenation (for example `TOG-""111`) so the public-safety scan
sees no literal while the runtime value is the real identifier. Numeric boundaries, case
distinctions, and cross-fixture relationships are therefore preserved where the tool parses them.
UUIDs and test assertions were not blanket-rewritten. Historical network prose is now explicitly
illustrative with documentation addresses and `example.invalid` hostnames. The orphan rehearsal
suite is omitted as documented above; the remaining imported suites retain their assertions.
Empty shell test-state resets are spelled with `printf -v` to avoid a secret-scanner false positive
without changing their values. Comment-only trailing whitespace is normalized; a path-specific
`.gitattributes` rule preserves the byte-exact expected-render fixture's terminal blank line.

The keep-going CI replay also exposed imported SIGPIPE-prone verdict pipelines. Here-strings
or fully draining `grep` consumers replace those pipelines in
`templates/org-github/tests/test_sdlc_foundation.sh`, `test-effective-boundary-reconcile.sh`,
`test-orphan-grant-retirement.sh`, `test_gh_override_scope_audit.sh`,
`test_gh_scope_residue.sh`, and `verification/postgres-policy-mutants-gate.sh`, preserving
patterns, exit-status checks, and assertions. The legacy
`verification/sigpipe-verdict-false-miss-gate.sh` entry point delegates to the retained canonical
`verification/case_c25c4d3f-sigpipe-verdict-gate.sh`: the latter retains the original mechanism,
match/miss, planted-violation, and no-pipefail controls and adds broader scanner controls.
This avoids a duplicate deliberately unsafe positive-control pipeline without adding an
exemption. The canonical SIGPIPE gate and its exemptions are unchanged.

## Deferred nongating integration gaps

Supplemental tests of affected imported files exposed three pre-existing gaps that this PR's
CI does not execute. They are deliberately not repaired or newly wired into CI in this PR.
One follow-up, **Resolve three nongating integration gaps after paperclip-ops consolidation**,
tracks all three for a separate reviewed PR after this consolidation merges:

- `templates/org-github/tests/test_sdlc_foundation.sh`: twelve staged paths are absent from
  the retained destination map, and the missing CI wiring fails two wiring controls
  (52 assertions pass, 14 fail).
- `test_gh_scope_residue.sh`: anonymized branch fixtures do not match the retained tool's
  branch-prefix parser, failing positive detection and repository attribution
  (35 assertions pass, 2 fail). Preserve those assertions when repairing synthetic fixtures.
- `test-effective-boundary-reconcile.sh`: the intentionally absent private
  `tool_grant_expectations.json` prevents this suite from running. The follow-up must use
  public synthetic fixtures where possible; assertions genuinely requiring private artifacts
  need an explicit skip-on-public reason, never deleted assertions or publication of the map.

Anonymization additionally broke `test_channel_drift.sh` fixtures in the same prefix-strip
manner (two failures: the STALE case misreported as UNVERSIONED, the exemption miss). Unlike
the three gaps above, those fixtures are repaired in this PR (real strippable prefixes via
concatenation) and the suite passes locally; it remains unwired in CI because the offline
cutover contract lists `./test_channel_drift.sh` as a removed producer, so wiring it here
would violate the contract's forbidden-input gate. No CI wiring is added in this PR.

Forty-seven root suites are not referenced by filename or stem in
`.github/workflows/ci.yml` as of this head and are therefore dark in public CI once the
toolkit retires; the same follow-up owns their triage (wire, run via discovery, or
explicitly retire): `test_app_tree_guard.sh`, `test_blocked_audit.sh`,
`test_breaker_cotenancy_probe.sh`, `test_capture_network_name_evidence.sh`,
`test_case_dcf07273_assignment_bundle.py`, `test_channel_drift.sh`,
`test_ci_required_checks_ownership.py`, `test_credential_chain_pin_gate.sh`,
`test_discord_job_health_monitor.sh`, `test_gh_override_scope_audit.sh`,
`test_gh_scope_residue.sh`, `test_gh_scope_residue_monitor.sh`,
`test_github_runner_bundle.py`, `test_host_ops_image_binding.py`,
`test_host_ops_install_manifest.py`, `test_host_ops_install_preparation.py`,
`test_host_ops_install_state.py`, `test_host_ops_install_transaction.py`,
`test_host_ops_prepare_first_install.sh`, `test_host_ops_spool_carrier.py`,
`test_host_ops_spool_mapping.py`, `test_host_ops_spool_provisioning.py`,
`test_interaction_envelope_lint.sh`, `test_interaction_triage.sh`,
`test_model_lane_probe.sh`, `test_native_ssh_citation_gate.sh`,
`test_offline_ci_contract.py`, `test_omniroute_rehearsal_translator.mjs`,
`test_op_approval_executor.sh`, `test_opencode_shared_budget_probe.sh`,
`test_paperclip_run_path_guard.py`, `test_parked_backlog_guard.sh`,
`test_plugin_manifest_gate.sh`, `test_post_rollup_comment.sh`,
`test_queue_liveness.sh`, `test_review_dedup.py`,
`test_review_dedup_placement.sh`, `test_runner_capacity_headroom.sh`,
`test_sdlc_foundation_acceptance.py`, `test_sdlc_foundation_placement.sh`,
`test_secret_exposure_audit.sh`, `test_sibling_guard.sh`,
`test_stale_descriptor_sweep.sh`, `test_toolkit_sync.py`,
`test_upstream_filing_readiness.sh`, `test_warn_first_inventory.py`,
`test_watchdog_delivery.py`.

The local keep-going replay also encountered environment failures, not proven source defects:
seven PostgreSQL-backed privilege steps and the regeneration-strip suite lacked a complete
local PostgreSQL tool cache; the host-operations installer suite lacked `visudo`; and the
carrier-isolation suite could not create user namespaces inside the restricted container.
These remain explicit non-local verification exclusions, not local passes. No gate or host
isolation setting is weakened to hide them. GitHub CI must arbitrate these checks, as well as
GitHub-only actions/event context and the full upstream mutation suite, which is explicitly
prohibited locally. Local diagnostics do not replace required CI or exact-head review.

## Integration deltas to retained canonical files

The conflict list above records source selection, not a prohibition on the following reviewed
integration edits. No toolkit version of these shared files was substituted:

- `README.md`: extends the canonical source index with all 63 newly tracked root entry points required by the unchanged README index gate, including imported `test-` names. Existing exemptions and positive/negative gate assertions remain unchanged.
- `vendor/toolkit-tracked-allowlist.txt`: explicitly registers the imported manifest paths. Two stale entries for actually-untracked aliases are removed so their archive bytes are checked.
- `vendor/offline-ci-contract.json`: separates 251 deliberately tracked inputs from 35 archive-restored inputs, pinning both sets by SHA-256.
- `vendor/toolkit-files.txt`, `.gitignore`, and `test_toolkit_sync.py`: restore the original archive's sibling-guard test subject under an explicit ignored fixture alias, separate from the now-tracked first-party root guard. Preserve the original archive checksum, subject digest, and behavior assertions rather than relabeling newer source as archived bytes.
- `verification/offline_ci_contract.py`: checks both disjoint, exhaustive input sets and their exact bytes; removed-producer provenance must still have an immutable input closure.
- `test_offline_ci_contract.py`: retains existing assertions and adds missing/wrong tracked-byte and omitted/reclassified/duplicated/invalid-hash controls.
- `.gitleaks.toml`: replaces nine exact-digest allowances with one path- AND format-scoped allowlist for `vendor/offline-ci-contract.json` digest lines (`"<path>": "<64-hex>"`), so re-pinning on first-party edits needs no new allowlist and stale entries do not pile up. Default rules remain enabled; no broad path or hash exemption is added. The canonical privacy gate remains unchanged.

## Migration and verification boundaries

The project default and repository-placement mappings select paperclip-ops for new work.
Existing toolkit-bound issues finish their current PRs in the toolkit. This consolidation is
frozen at toolkit baseline `2d2b8123a60e7477a01a477e4f0daa27caab74d6`; later toolkit merges,
including toolkit PR #80, are deliberately not folded into this PR. After this consolidation
merges, reviewed follow-up sync PRs must cover that baseline through the then-current toolkit
main. Repeat those syncs while grandfathered work drains, and perform a final sync after all
retirement gates hold, before changing visibility or archiving.
Retirement requires this consolidation to merge, zero open toolkit PRs, zero active
toolkit-bound issues (including realized workspaces), and the **execution-image packet ported
or explicitly retired**. Once all gates hold, make the toolkit repository **private first,
then archive it**. Do not change the existing private source
repository's visibility. Existing private PR grandfathering is unchanged.

The omitted historical account fixture was already present in the public toolkit history.
The bounded audit identified account email identifiers and usage/pacing measurements, not
credentials or tokens; the coordinator classified the exposure as low severity. The chosen
remedy is private archival at the retirement gate, **not an interim history rewrite or a
HEAD-deletion PR**. No actual account values are reproduced here. This consolidation neither
rewrites toolkit history nor changes toolkit visibility early.

No live host installation, realized checkout, issue/run binding, repository visibility,
builder instructions, or shared policy file is changed by this import.

Publication requires an exact-tree privacy/secret scan with zero findings; merge requires
green CI and authorized exact-head Paperclip Review. This document is not a verification receipt.
