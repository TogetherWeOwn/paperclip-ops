# Sustained queue-age source contract

This public document describes the alert configuration, not a current incident.
The source `ci-queue-age` endpoint first breaches when the oldest eligible
runner-waiting job exceeds `QUEUE_AGE_THRESHOLD_MIN` (default 60 minutes).
The escalation alert has failure-threshold 24 at the documented five-minute
push cadence: approximately two hours of sustained failing samples. This is a
sample-count contract, not proof of exact wall-clock duration during outages.

The source responder role is `devops`. On escalation, verify probe freshness,
eligible job labels and the current queue before distinguishing unavailable
runners from dependency, approval or concurrency waits. Use the authorized
operational contact path; this document does not wake or assign anyone.

Follow `docs/runbooks/garm-ci-oncall-alert-to-action.md`. Preserve evidence,
record a bounded next action, and verify queue movement after any separately
authorized remedy. Never fabricate a healthy sample, raise the threshold to hide
a stalled queue, bypass review, or stop a monitor without an owned hold and expiry.
Changing cadence requires recalculating the sample-count threshold and updating
the fragment descriptions and tests together.
