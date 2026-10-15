# Gatus source module registry

This is an offline source index, not a deployed endpoint inventory. File,
fixture and endpoint references describe contracts exercised by the retained
source tests. No entry claims installation, current health, host ownership or
authorization to execute. Runtime configuration stays private. Live wiring and
operational evidence require separate private verification.

Endpoint keys use `group_name`; endpoint identities use `group/name`.

## Core

Source: `gatus-config.yaml`, `_gatus_push.sh`, `module_wiring_allowlist.json`.
Suites: `test_case_60330d6f_status_pilot.sh`.

- `two/two-web-prod`, `two/two-web-staging`
- `platform/paperclip-api`, `platform/discord-bot`
- `platform/lxc-ci-vms`: `push-lxc-state.sh`
- `platform/oom-kills`: `push-oom-kills.sh`
- `platform/ci-queue-age`: `push-ci-queue-age.sh`
- `platform/tls-expiry`
- `platform/garm-pool`: `push-garm-pool.sh`
- `platform/workers-error-rate`: `push-workers-error-rate.sh`
- `platform/github-rate-limit`: `push-github-rate-limit.sh`

## Paperclip invariants

Source: `ops/case_654dd2ec/gatus-endpoints.yaml`,
`ops/case_654dd2ec/push-paperclip-invariants.sh`.
Suite: `test_case_1372613a_invariants.sh`.

- `paperclip/agents-error-10m`
- `paperclip/failed-subtype-success`
- `paperclip/cards-rewoken-6h`
- `paperclip/missing-disposition-reparks`
- `paperclip/unassigned-infocus-cards`
- `paperclip/db-pool-waiters`

## Fleet run stall

`paperclip/fleet-run-stall`: `ops/case_b79f6544/gatus-endpoints.yaml`,
`ops/case_b79f6544/push-fleet-run-stall.sh`,
`test_case_da79e241_fleet_run_stall.py`.

## CI main and runtime

Source: `ops/case_c542accd/gatus-endpoints.yaml`,
`ops/case_c542accd/push-ci-main.sh`.
Suite: `test_case_8ee359f2_ci_main.sh`.

- `ci/main-togetherweown-two-bot-next`
- `ci/main-togetherweown-two-web-next`
- `ci/main-togetherweown-paperclip-ops`
- `ci/runtime-togetherweown-two-bot-next`
- `ci/runtime-togetherweown-two-web-next`
- `ci/runtime-togetherweown-paperclip-ops`
- `ci/runner-units`

## CLIProxy lanes

Source: `gatus-cliproxy-lanes.yaml`, `push-cliproxy-lanes.sh`, `_gatus_push.sh`.
Suite: `test_case_60330d6f_cliproxy_lanes.sh`.

- `cliproxy/lane-claude` / `cliproxy_lane-claude`
- `cliproxy/lane-codex` / `cliproxy_lane-codex`
- `cliproxy/lane-zai` / `cliproxy_lane-zai`
- `cliproxy/lane-opencode-go` / `cliproxy_lane-opencode-go`

## Runner census

`platform/runner-census`: `ops/case_ebbb469a/gatus-endpoints.yaml`,
`ops/case_ebbb469a/push-runner-census.py`,
`ops/case_ebbb469a/test_runner_census.py`,
`ops/case_ebbb469a/mutation_gate.py`.

## Discord bot

`platform/discord-bot` / `platform_discord-bot`: `gatus-config.yaml`,
`push-discord-bot.sh`, `_gatus_push.sh`, `test_case_60330d6f_discord_bot.sh`.

## Merge queue

`platform/merge-queue` / `platform_merge-queue`: `gatus-merge-queue.yaml`,
`push-merge-queue.sh`, `_gatus_push.sh`, `test_case_60330d6f_merge_queue.sh`,
`fixtures/merge-queue-healthy.example.json`,
`fixtures/merge-queue-saturated.example.json`.

## GARM reaper

`platform/garm-reaper`: `gatus-config.yaml`, `push-garm-reaper.sh`,
`audit_stale_instances.sh`, `github-runner/garm/TTL-REAPER-POLICY.md`,
`_gatus_push.sh`, `test_garm_reaper_audit.sh`.

## Runner disk

`platform/runner-disk`: `../case_a814f742/gatus-endpoints.yaml`,
`../case_a814f742/push-runner-disk.sh`,
`../case_a814f742/evaluate-runner-disk.sh`, `_gatus_push.sh`,
`test_case_c7afa8e6_runner_disk.sh`.

## Queue forecast

`platform/ci-queue-forecast` / `platform_ci-queue-forecast`:
`gatus-queue-forecast.yaml`, `push-queue-forecast.sh`, `_ci_queue_age.sh`,
`_gatus_push.sh`, `test_case_60330d6f_queue_forecast.sh`.

## CI hung job

`platform/ci-hung-job`: `ops/case_e1e23b50/gatus-endpoints.yaml`,
`ops/case_e1e23b50/push-ci-hung-job.sh`, `hung_job_eval.py`,
`hung_job_thresholds.json`, `test_case_f9bc8d5a_hung_job.py`.

## Shadow window

- `router/shadow-freshness` / `router_shadow-freshness`
- `router/window-landing` / `router_window-landing`

Source: `gatus-shadow-window.yaml`, `push-shadow-window.sh`, `_gatus_push.sh`.
Fixture: `fixtures/window-landing-gauge.example.json`.
Suite: `test_case_60330d6f_shadow_window.sh`.

The source validates `WINDOW_GAUGE_MAX_AGE_SEC`; a `stale-gauge` fails closed.
A fixture is refused (`fixture-refused`) outside the explicit offline test path.
The same shard torn-tail rule prevents combining inconsistent evidence.

## Enforce readiness

`router/enforce-readiness` / `router_enforce-readiness`:
`gatus-enforce-readiness.yaml`, `push-enforce-readiness.sh`, `_gatus_push.sh`,
`test_case_60330d6f_enforce_readiness.sh`,
`fixtures/enforce-readiness-ready.example.json`,
`fixtures/enforce-readiness-notready.example.json`.

## Pool freshness

`platform/pool-freshness` / `platform_pool-freshness`:
`gatus-pool-freshness.yaml`, `push-pool-freshness.sh`, `_gatus_push.sh`,
`test_case_c99d8d47_pool_freshness.sh`,
`fixtures/pool-freshness-fresh.example.json`,
`fixtures/pool-freshness-stale.example.json`.

## Keeper ingest freshness

`keeper/ingest-freshness`: `../case_5605c29e/gatus-endpoints.yaml`,
`../case_5605c29e/push-keeper-ingest.sh`,
`test_case_6b0b5439_keeper_ingest.sh`.

## Staging login smoke

`two/staging-login-smoke`: `../case_a2ab2937/gatus-endpoints.yaml`,
`../case_a2ab2937/push-staging-login-smoke.sh`,
`test_case_00b2eb59_probe.sh`.

## Hindsight recall

`hindsight/recall-canary`, `hindsight/recall-latency`:
`../case_34ee2cf0/gatus-endpoints.yaml`,
`../case_34ee2cf0/push-hindsight-recall.sh`,
`test_case_79158b71_hindsight_recall.sh`.

## Balancer silence

`platform/balancer-silence`: `gatus-balancer-silence.yaml`,
`push-balancer-silence.sh`, `_gatus_push.sh`,
`test_case_60330d6f_balancer_silence.sh`.
Registry checker: `ops/case_658bf6b9/check_module_wiring.py`.

## Neon staging

`platform/neon-staging`: `gatus-config.yaml`, `push-neon-reachability.sh`,
`test_case_60330d6f_dependency_probes.sh`.

## Database backup freshness

`paperclip/db-backup-freshness`: `../case_66436e78/gatus-endpoints.yaml`,
`../case_66436e78/push-db-backup-freshness.sh`,
`test_case_f1c0153c_db_backup_freshness.sh`.

## Gatus heartbeat

`platform/gatus-heartbeat`: `ops/case_56fff4c6/gatus-endpoints.yaml`.
