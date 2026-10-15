# Two-web promote: live identity from Cloudflare

Use synthetic fixtures for source tests. This document is not a deployment
inventory and does not authorize production requests, deploys, migrations, or
credential changes. Real account ids, tokens and host names live outside this
repo and are injected at runtime.

## Rule

"Live production" is the Cloudflare active deployment: the first deployment's
version id, mapped to its build commit. Release metadata and tags are never
live identity.

1. Read `GET /accounts/{account_id}/workers/scripts/{script_name}/deployments`
   and take the first deployment's versions list with traffic percentages.
2. Map the single serving version id through
   `GET /accounts/{account_id}/builds/builds?version_ids={version_id}` to the
   build record commit hash.

## Fail closed

Refuse the promotion with a clear reason when:

- there are no deployments or no versions;
- any version entry is not an object with a `version_id` (never silently
  pick the identified half of a split);
- any traffic percentage is not a number, or known percentages do not
  total 100;
- more than one version shares live traffic (never silently pick one).
  A version at 0% is staged, not serving, so a 100/0 smoke-test layout
  still promotes the 100% version;
- the live version has no known build, the build has no commit, or build
  metadata is not an object.

The CLI exits 2 with `refused: <reason>` on every refusal above, so shell
callers can treat rc 2 as the refusal signal.

The decision and the recorded rollback command both use the derived commit.
A stale tag must never feed either path. This helper is a standalone
offline port: it is not wired into `scripts/dispatch_deploy.sh`, and the
rollback argv it emits (`scripts/dispatch_deploy.sh rollback --commit
<live-commit> --smoke-cmd <cmd>`) replays there with the caller's smoke
command.

## Offline verification

Run from the repository root:

```sh
(cd ops/productivity && python3 -m unittest -v test_two_web_promote)
python3 ops/productivity/two_web_promote.py --help
```

Passing source tests prove the fixture contract only, never that a live
deployment was read. A release tag that differs from the served version must
yield the served commit or a refusal.
