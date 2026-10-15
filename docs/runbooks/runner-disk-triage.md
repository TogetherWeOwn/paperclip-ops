# Runner disk and OOM triage: source contract

This public runbook is observational guidance, not a host inventory or permission
to delete data. Tests use synthetic fixtures only. The source responder role is
`devops`; live ownership and host selection belong in private operational state.

## Classify the signal

`ops/case_a814f742/evaluate-runner-disk.sh` and its Gatus fragment distinguish
filesystem usage, inode pressure, LXD pool usage and VM root usage. Source alert
thresholds are at or above 80% warning and 90% critical. A missing or failed
measurement is not zero usage. Read the exact sample and freshness first.

For an OOM alert, inspect the bounded journal evidence and memory limits before
assuming disk pressure caused the kill. The OOM pusher reports a killed-process
line or absent delivery; those are different failure classes. Do not restart or
delete resources based solely on an alert label.

## Safe investigation sequence

1. Verify which filesystem, pool or VM the sample actually describes.
2. Distinguish bytes from inodes, and stale measurements from current pressure.
3. Identify the owning workload and preserve active jobs, credentials and evidence.
4. Choose a separately authorized reversible remedy with a recorded rollback.
5. Verify the same source metric and workload health after the remedy.

No pruning, instance deletion, restart, threshold change or capacity increase is
authorized here. A monitor hold needs an owner and expiry. Changes to thresholds
must update the evaluator, alert descriptions and tests together.

## Offline verification

Run `bash test_case_c7afa8e6_runner_disk.sh` from the repository root. The pusher
is `ops/case_a814f742/push-runner-disk.sh`; its registry key is
`platform_runner-disk`. Synthetic success and failure controls do not attest a
host's current capacity or installed monitoring.
