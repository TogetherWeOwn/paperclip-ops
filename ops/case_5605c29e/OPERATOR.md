# Keeper ingest freshness: source contract

This public runbook describes an offline-tested probe, not an installed service.
It grants no deployment, credential-creation or remediation authority. Run the
repository-root suite `bash test_case_6b0b5439_keeper_ingest.sh` with synthetic
fixtures; do not point tests at production.

## Configuration boundary

`push-keeper-ingest.sh` reads `KEEPER_FRESHNESS_URL`, an optional existing
read-scoped `KEEPER_FRESHNESS_BEARER`, and the Gatus delivery variables
`GATUS_URL` and `GATUS_EXTERNAL_TOKEN` from separately protected configuration.
Never commit those values. The endpoint must be a staging/test surface;
production-named hosts are refused without a probe request. The response field
is `last_successful_ingest`, either an ISO-8601 timestamp or Unix epoch seconds.

The source defaults are `KEEPER_WARN_LAG_S=1800`,
`KEEPER_CRIT_LAG_S=3600`, and `KEEPER_PROBE_TIMEOUT_S=15`.
Change description thresholds and evaluator defaults together, with tests.

### ingest-freshness

The key is `keeper_ingest-freshness` (`keeper/ingest-freshness`). Lag at or above
1800 seconds is warning; at or above 3600 seconds is critical. Non-200 replies,
transport errors, malformed or absent timestamps, and timestamps more than
300 seconds in the future fail closed. A future clock produces `clock-anomaly`.

The operational responder should inspect the failure class and whether any
usage was expected before diagnosing an idle period as broken ingestion. Check
the upstream data source and clock before changing thresholds. Never turn a
missing or malformed value into a healthy sample. This probe is observational;
any repair or deployment needs a separately authorized action.

## Deployment and rollback boundary

The fragment is `gatus-endpoints.yaml`; the script makes one GET per invocation.
No scheduler is installed by this source. Any future deployment must record its
exact reviewed revision, protected environment, scheduler ownership and rollback.
A timer hold requires an expiry and an owner; this runbook does not authorize
stopping an existing monitor. The probe does not write Keeper data.
