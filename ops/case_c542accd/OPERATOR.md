# CI conclusion and runtime monitor source contract

Reusable source documentation only: not installation evidence or authority to
query production. Tests use disposable GitHub, systemctl and transport stubs.
The private historical `job_baselines.json` measurement table is intentionally
not published. Do not invent measured durations or reuse another installation's
calibration. An authorized installation must supply a reviewed `CI_BASELINES_FILE`
with positive fallback/job durations and auditable seeds. Missing input refuses
before any health claim; offline behavior tests supply explicit synthetic data.

Review an immutable bundle, endpoint identities, GitHub read scope, configured
`GH_REPOS` and runner-unit scope before an independent deployment. Entries in
`GH_REPOS` must map to distinct endpoint slugs: dots and slashes both become
dashes, so `o/a.b` and `o/a-b` would share one key — the run refuses instead
of letting the last write mask a red main. Store
`GATUS_URL`, `GATUS_EXTERNAL_TOKEN` and authentication in protected configuration.
Export env-file assignments and refuse execution if sourcing fails. The source
schedule is five minutes against a ten-minute heartbeat; it is not evidence that
any host has the monitor installed.

### main-conclusion

Owner: on-call. Inspect the latest main-head checks for the configured repository.
Failure, cancellation and other red conclusions are not successful completion.
Queued/running checks need their own progress signal; empty or unreadable API
results fail closed. Verify the exact head rather than accepting an older green
run. This monitor never reruns, cancels or approves CI.

### job-runtime

Owner: on-call. A completed duration or in-flight age strictly above the configured
factor times the reviewed baseline trips the runtime signal (default factor two).
The explicit fallback handles unlisted jobs. Inspect real progress, baseline
provenance and runner conditions before changing the calibration. Skipped jobs
are excluded, not used as zero-duration evidence. Do not replace an absent
baseline file with invented measurements to get a green monitor.

### runner-units

Owner: devops. Inspect the named inactive unit and its recent failure evidence.
Distinguish a down runner from an unreadable status query. The read-only check
does not authorize restarting a unit or interrupting an active job. Preserve
isolation and configured pool routing during any separately reviewed repair.

## Rollback

Save prior bundle, measured baseline file and endpoint/environment configuration
before an authorized deployment. Restore those reviewed artifacts together if
verification fails. Stopping a critical schedule requires an owned hold with
expiry and replacement observation. Never delete evidence or report artificial
health to hide a monitor failure.
