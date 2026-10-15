# Hindsight recall canary: source contract

This public runbook describes an offline-tested observational probe. It is not
an installed-service inventory and grants no deployment, retention, credential
creation or remediation authority. Verify with the repository-root command
`bash test_case_79158b71_hindsight_recall.sh`; tests use synthetic local fixtures.

## Configuration boundary

`push-hindsight-recall.sh` reads `HINDSIGHT_RECALL_URL`,
`HINDSIGHT_RECALL_BEARER` and `HINDSIGHT_CANARY_QUERY` from separately protected
configuration. Keep the bearer, query, bank identity and memory contents out of
Git. Use an existing authorized recall-scoped credential. Gatus delivery uses
`GATUS_URL` and `GATUS_EXTERNAL_TOKEN`, also outside Git.

The source makes one recall POST per invocation and no retention write. The
fixed query must match deliberately seeded canary content. The source defaults
are `HINDSIGHT_LATENCY_MS=45000` and `HINDSIGHT_RECALL_TIMEOUT_S=60`; timeout must
exceed the latency threshold, so slow-but-full responses remain observable.

### recall-canary

The key is `hindsight_recall-canary` (`hindsight/recall-canary`). Non-200 replies,
transport failure, malformed JSON, and empty results fail closed. Inspect the
failure class first. A successful but empty result does not establish that a
memory was deleted: verify the query, scope and seed through the authorized
private operational path. Never weaken the non-empty assertion or disable
retention to make the probe green.

### recall-latency

The key is `hindsight_recall-latency` (`hindsight/recall-latency`). Elapsed time
strictly greater than 45000 milliseconds, or a probe timeout, breaches the source
threshold. A full response may still be too slow. Correlate latency with the
canary result before distinguishing capacity pressure from a failed recall.
Do not mask the problem by raising a threshold past the consuming tool timeout.
Update fragment descriptions and probe defaults together, with tests.

## Deployment and rollback boundary

The fragment is `gatus-endpoints.yaml`; the probe is `push-hindsight-recall.sh`.
No scheduler, memory seed or credential is created by this source. Any future
deployment requires separate authorization, exact-revision evidence and a
recorded rollback. A monitor hold needs an owner and expiry; this document does
not authorize stopping an existing timer or modifying stored memories.
