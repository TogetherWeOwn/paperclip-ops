# Staging login-smoke source contract

Never production. Never commit credentials, tokens, real accounts or deployment
addresses. This public document grants no deployment or service-change authority.
Tests use synthetic local responses; an actual smoke probe needs separate staging
scope and authorization.

## staging-login-smoke

The source probe checks a staging login and authenticated session. Store its
existing scoped `QA_AUTH_TOKEN` in a separately protected environment file with
`chmod 0600` and appropriate ownership. Do not place the token in arguments,
source control, command output or public evidence.

The scheduling contract is `*/10 * * * *` (every ten minutes), not an instruction
to install a cron job. Review the staging URL and account scope before activation.
When login fails, distinguish transport, authentication and session failures. A
collision involving the `qa-member` account can invalidate another smoke session;
verify account/session ownership rather than relaxing the assertion or moving
the probe to production.

## Offline verification

From the repository root, run the same source-only checks used in CI:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_staging_login_journey
PYTHONDONTWRITEBYTECODE=1 bash test_case_00b2eb59_probe.sh
```

The immutable journey suite uses generated synthetic values, recording adapters
and a Linux libseccomp network fence (libseccomp.so.2 is required). It checks fresh
cookie propagation, retry cleanup, strict response validation, redaction and
static/refusal modes. Keep malformed, failed-login, secret-non-disclosure and
endpoint-fragment controls intact.

## CLI and result contract

Canonical help is `bash ops/case_a2ab2937/push-staging-login-smoke.sh --help`.
These examples are static and require no credentials or network:

```sh
bash ops/case_a2ab2937/push-staging-login-smoke.sh --dry-run
bash ops/case_a2ab2937/push-staging-login-smoke.sh --dry-run --json
```

`--dry-run` only describes the three steps; it never creates private configs,
calls curl/Gatus, sleeps or attempts a session. `--json` alone is **not** a dry
run: like the no-flag entrypoint it executes the journey and existing push, and
requires separate runtime authorization. Unknown, repeated or conflicting flags
exit 2 before execution; `--help` cannot be combined with other flags. None of
these interfaces removes any CISO, release or installation HOLD.

The version 1 JSON envelope contains `schema_version: 1`,
`probe_id: "two_staging-login-smoke"`, `outcome` and ordered `steps` with stable
IDs `login`, `auth_status`, `up`. Attempted steps carry numeric `http_status` and
`latency_ms`; a retried login reports its final attempt. Dry-run steps instead
carry planned `method` and `path`. No bodies, headers, credentials or cookie
values are emitted. Error outcomes add `error.code`, boolean `error.retryable`
and a safe `error.next_action`. Retryability is diagnostic advice, not permission
to retry or change credentials. Codes are fixed identifiers, never server text:

- `qa_auth_missing`, `qa_auth_shape`, `staging_origin_refused`,
  `invalid_arguments`, `curl_missing`, `python_missing`, `temp_unavailable`,
  `temp_permissions`: refusal; repair the existing configuration/prerequisite.
- `login_transport`, `auth_status_transport`, `up_transport`, `backoff_failed`,
  `gatus_push_failed`: failure; transient/retryable without automatic new retries.
- `login_status`, `login_cookie`, `login_latency`, `auth_status_status`,
  `auth_status_body`, `auth_status_latency`, `up_status`, `up_origin`,
  `up_latency`, `retry_after_invalid`: fail closed; inspect the named step offline.
- `login_throttled`: retryable `throttled`, **not** authenticated-journey success.
- `probe_aborted`: retryable failure for an interrupted journey or final delivery,
  or an unexpected exit after preflight. Preserve HOLDs and the existing scheduler;
  this result never authorizes an immediate retry. INT/TERM retain exits 130/143.

Exit 0 means `ok`, static `dry_run`, help, or the existing non-paging repeated-429
policy. Exit 1 is a failed journey/push; exit 2 is refusal (no push). A repeated
429 still pushes Gatus true after one bounded Retry-After backoff (default 60s,
cap 120s), without running auth/status or /up. Consumers must inspect `outcome`,
not infer authentication from exit 0 or the Gatus bit. A failed Gatus delivery
changes the result to `failed`/`gatus_push_failed` and exits 1. Python 3 is
required for structured output as well as response validation.

Each login attempt truncates its private jar and responses, enables curl jar
writing, and passes only that jar to auth/status. Secure/HttpOnly must be exact
attributes on the exact `__Host-two_session` Set-Cookie record (with host-cookie
Path=/ and no Domain); duplicate session records fail. Auth/status must be a JSON
object whose top-level `authenticated` is boolean true; malformed/duplicate-key
JSON fails. /up uses no session and requires a single exact `x-two-origin` value
`two-web-next`, allowing HTTP field-name case and surrounding spaces/tabs but no
prefixes or extra values. No redirects are followed; ambient curl configs are
disabled. Private files are cleaned on exit and transport diagnostics stay private.

## Rollback

A separately authorized deployment must record its prior configuration and exact
revision. Restore only the state owned by that deployment. Do not stop an existing
monitor without an owned hold and expiry, and do not rotate or delete credentials
as part of source-test cleanup. The public tests make no live installation claim.
