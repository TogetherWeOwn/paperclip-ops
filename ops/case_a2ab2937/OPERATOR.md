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

Run `bash test_case_00b2eb59_probe.sh` from the repository root. Keep malformed,
failed-login, secret-non-disclosure and endpoint-fragment controls intact.

## Rollback

A separately authorized deployment must record its prior configuration and exact
revision. Restore only the state owned by that deployment. Do not stop an existing
monitor without an owned hold and expiry, and do not rotate or delete credentials
as part of source-test cleanup. The public tests make no live installation claim.
