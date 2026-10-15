# Staging app-health source module

This module renders staging-only health and restart-observation fragments. It
contains no live deployment inventory or authority to change a service.
Run `bash ops/case_744981bd/test_staging_app_health.sh` from the repository root.

A restart observation is pushed under the group-qualified key
`staging_staging-restart-<app>`. The required evaluator is
`ops/case_73259b15/coolify_restart_signal.sh`; confirm that the reviewed evaluator
is actually available before wiring restart observations. Until then, keep only
the staging pull and TCP checks and skip the restart pushes. An observation is
not permission to restart an application.

See [the source runbook](../../docs/runbooks/staging-app-health.md). Endpoint
addresses and credentials belong in separately protected configuration, never Git.
