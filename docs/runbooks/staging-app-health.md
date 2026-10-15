# Staging app-health source contract

Use synthetic fixtures for source tests and staging only for any separately
authorized integration check. This document is not a deployment inventory and
does not authorize production requests, restarts or credential changes.

The renderer in `ops/case_744981bd/` describes pull/TCP checks plus restart
observations. Configure real staging addresses outside Git. A restart observation
must be pushed under `staging_staging-restart-<app>`: Gatus requires the group and
name, not just the endpoint name.

## Evaluator prerequisite

Before enabling restart observations, verify that the reviewed
`ops/case_73259b15/coolify_restart_signal.sh` evaluator is available in the
selected deployment revision. If it is absent or unverified, skip the restart pushes
and retain only the independently valid staging pull and TCP checks.
Missing observations must not be represented as healthy restart counters.

## Response and rollback boundary

Inspect the failed check and sample freshness before selecting a remedy. A
restart observation is evidence about an application, not authority to restart
it. Record an exact reviewed revision and previous configuration for any
separately authorized deployment. Roll back only the changes owned by that
deployment; an existing monitor requires an owned hold and expiry before a stop.

## Offline verification

Run `bash ops/case_744981bd/test_staging_app_health.sh` from the repository root.
The tests retain group-qualified-key, evaluator-dependency and negative renderer
controls. Passing source tests is not proof that a live evaluator is installed.
