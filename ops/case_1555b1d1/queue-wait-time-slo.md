# Queue pickup wait-time source specification

This is a source-only proposal with synthetic contract tests, not measured fleet
performance or an installed monitor. The class labels below describe a reusable
model; this public repository's own CI remains GitHub-hosted. No runner inventory,
capacity change, alert activation or private incident history is included.

## 1. Metric and deduplication boundary

Measure pickup delay separately from job runtime and provider provisioning
operations. A valid evaluator must define its observation window, eligible job
population and p95 calculation; missing or malformed data must remain unknown.
Do not infer pickup health from a completed workflow's total elapsed time.

### Dedupe against sibling metrics

- `case_60a899d5`: retain the distinct existing queue observation; do not emit a duplicate alert under another name.
- `case_c542accd`: CI main/runtime monitoring measures runtime and outcome, not pickup delay.
- `case_d61d5dad`: provider operation timing measures provisioning operations, not job pickup delay.

## 2. Proposed source thresholds

These values preserve the reviewed source specification. They are not a claim
that any live fleet meets an SLO. Calibrate with approved staging observations
before activating a new monitor, and update descriptions and tests together.

<!-- SLO-TABLE -->
| Class | Warn | Page |
|---|---|---|
| `isolated` | 40m | 60m |
| `selfhosted` | 30m | 45m |
| `ephemeral` | 10m | 20m |
<!-- /SLO-TABLE -->

## 3. Candidate endpoint fragment

Do not merge this fragment into an installed configuration without a separately
reviewed evaluator, non-collision check, delivery test and recorded rollback.
Token values belong in protected environment configuration, never in source.

<!-- SLO-ENDPOINTS -->
```yaml
  - name: ci-wait-time-isolated
    group: platform
    token: "${GATUS_EXTERNAL_TOKEN}"
    heartbeat:
      interval: 10m
    alerts:
      - type: discord
        description: "two-isolated pickup p95 above SLO (warn 40m / page 60m)"
      - type: custom
        description: "two-isolated pickup p95 above SLO (warn 40m / page 60m)"

  - name: ci-wait-time-selfhosted
    group: platform
    token: "${GATUS_EXTERNAL_TOKEN}"
    heartbeat:
      interval: 10m
    alerts:
      - type: discord
        description: "two-selfhosted pickup p95 above SLO (warn 30m / page 45m)"
      - type: custom
        description: "two-selfhosted pickup p95 above SLO (warn 30m / page 45m)"

  - name: ci-wait-time-ephemeral
    group: platform
    token: "${GATUS_EXTERNAL_TOKEN}"
    heartbeat:
      interval: 10m
    alerts:
      - type: discord
        description: "two-ephemeral pickup p95 above SLO (warn 10m / page 20m)"
      - type: custom
        description: "two-ephemeral pickup p95 above SLO (warn 10m / page 20m)"
```
<!-- /SLO-ENDPOINTS -->

## 4. Offline acceptance and rollback boundary

Run `bash test_case_f277272d_queue_wait_time_slo.sh` from the repository root.
The suite checks table/fragment agreement, missing endpoints, collisions, secret
shapes, dedupe boundaries and fail-closed mutation-harness setup. A passing source
suite is not host-render or live-delivery evidence. Any future installation must
record its exact revision and prior configuration; stopping an existing monitor
requires an owned hold with expiry.
