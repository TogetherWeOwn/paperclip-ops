# Readiness snapshot contract

The readiness sweep reads through `getReadinessSnapshot`. It uses the same
single-query, exact-head snapshot as `getCloseoutSnapshot`, but does not use
push age to decide readiness.

- An explicit GraphQL `pushedDate: null` stays `headPushedMs: null`. No timestamp
  is borrowed from a check, the clock, or the commit's author date. An absent or
  invalid push date still fails the read; a real date still checks clock order.
- Check names can repeat across workflows, reruns and status/check sources.
  Every entry is validated before aggregation. Each name passes only if all its
  entries pass (`success`, `neutral`, `skipped`). A completed failure wins over
  pending, and pending wins over passing. Ties and output order are deterministic.
  This deliberately does not infer which workflow run is newest: a later passing
  rerun cannot hide another failure on the same head.
- Strict closeout readers retain their push-age and duplicate-name guards.
  Lifecycle, identity, head-window, label and rollup completeness guards remain
  in force for both readers. Snapshot failure causes no ruleset read or write,
  remains a counted error, and is never retried within the pass.

## Safe Actions evidence

Successful snapshot records and their log lines include `headPushTime=present`
(or `null`), `repeatedNames` (number of names occurring more than once), and
`repeatedEntries` (entries beyond the first per name). These are facts about the
read, not a green verdict on the checks.

Failed reads retain `snapshot-read-failed` and add a fixed `snapshotFailure`:
`query-transport`, `query-response`, `head-outside-window`,
`check-rollup-incomplete`, `check-malformed`, or `snapshot-invalid`. Transport
includes timeout, output-limit and invalid-JSON failures; a failed CLI invocation
may also contain a GraphQL error, so this is not proof of a missing permission.
Raw child output, exception text, check names, titles, bodies and labels are not
included in these diagnostics.

## Verification and rollback

Run `npm --prefix gh-event-capture run check` and
`npm --prefix gh-event-capture test`. The independent
`test/readiness-snapshot-regressions.test.mjs` covers null dates, repeated checks
in all orders, strict closeout guards, counted failures, zero-write read-only
passes and sanitized output.

The workflow executes reviewed base/default-branch code, never PR-head code.
Local tests and PR-triggered sweep runs therefore do not establish that a fix
works with the job token. After merge, inspect a trusted-default-branch pass:
require nonzero evaluated snapshots, zero snapshot errors, and the new facts.
The flag-off policy must still show zero mutation counts. No permission or
credential change, guard bypass, flag-on or blind rerun is part of this repair.
Rollback is a reviewed revert of the snapshot-reader change; it restores the
strict readiness rejection without changing the workflow's grants or policy.
