# GARM CI alert-to-action source contract

This is source-only triage guidance. It neither identifies a live fleet nor
authorizes changing pools, runners, credentials, services or schedules. Tests
must use synthetic data or an explicitly authorized staging environment.

## Identify the breached condition

The source `devops` responder first reads the alert's threshold and producing
probe. Distinguish eligible runner-waiting jobs from jobs blocked by dependencies,
approval, concurrency or a different label. Queue age is measured from job data,
not inferred from the age of an entire workflow run. Missing API data must not
be treated as an empty, healthy queue.

The generic queue-age source default is over 60 minutes, configurable through
`QUEUE_AGE_THRESHOLD_MIN`. GARM health also considers enabled/healthy pool state,
minimum-idle shortfall and eligible-label queue pressure through
`GARM_QUEUE_THRESHOLD_MIN`. Read configured values rather than substituting the
generic default for a pool-specific bound.

## Investigate before changing capacity

1. Verify freshness, delivery and the eligible job/label set.
2. Compare the pool's enabled state, provisioned capacity and pending provisioning.
3. Check disk, inodes, memory and runner health before requesting more capacity.
4. Record the exact failed condition and choose a separately authorized remedy.
5. Verify queue movement and workload health against the same evidence source.

A reaper alert identifies candidates for review, not authorization to delete
instances. Graceful drain, active-job checks and the source TTL contract remain
required. Do not bypass review gates or move public jobs to an unreviewed runner.

## Verification and rollback

Run the relevant offline pool/queue/reaper suites and preserve their negative
controls. Any actual deployment records its reviewed revision, previous
configuration and rollback. Do not stop monitoring without an owned, expiring
hold. A sustained queue-age escalation is described in
`ops/case_658bf6b9/queue-age-escalation.md`.
