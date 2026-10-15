# Status-page source and operator boundary

This public runbook records source contracts and example wiring only. It is not
an installed-service inventory. No host addresses, credentials, private incident
history or operational approval are published. Tests run against disposable local
fixtures; they must not probe production.

## Reviewed release contract

An independently authorized deployment must select an exact reviewed revision,
use `git worktree add --detach` to prepare an immutable release directory, and
validate the resulting configuration with the actual Gatus binary before
activation. Never serve a moving developer checkout. The common serving pointer
is `/opt/status-page/current`; prepare a replacement with `ln -sfn`, then rename
that temporary symlink atomically. Preserve the prior target for rollback. All
scheduled source paths must resolve through the same pointer. These instructions
do not authorize repointing any existing installation.

Keep environment files outside the release and protect them with restrictive
ownership and permissions. `GATUS_URL`, `GATUS_EXTERNAL_TOKEN`, provider tokens,
webhooks and database URLs must never be committed or printed. Placeholder only:

```text
DISCORD_BOT_HEALTH_URL=<paste an approved staging/test health URL into private configuration>
```

## Source scheduling examples

The following are source-contract examples, not a request to install cron jobs.
Review endpoint selection, cadence, credential scope and environment ownership
separately before deployment. `set -a` exports ordinary assignments; `&& exec`
ensures a failed environment-file load cannot run the probe. Balancer log/state
locations also belong in the protected environment, not in this repository.

```cron
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-lxc-state.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-oom-kills.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-ci-queue-age.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-neon-reachability.sh
17 4 * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-tls-expiry.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-workers-error-rate.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-github-rate-limit.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-discord-bot.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-balancer-silence.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-cliproxy-lanes.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-merge-queue.sh
*/15 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-garm-reaper.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-enforce-readiness.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-shadow-window.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-pool-freshness.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-queue-forecast.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_a814f742/push-runner-disk.sh
*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_66436e78/push-db-backup-freshness.sh
```

## Source validation and response

Run `bash test_case_60330d6f_status_pilot.sh` and the module-specific offline
suites from the repository root. The invariant helper's deployment fallback is
`/opt/status-page/current`; an authorized acceptance check must exercise an arm
such as `paperclip-inv agents-error-10m`, without printing its database URL.
A source test is not proof of a host deployment or credential access.

For an alert, inspect its THRESHOLD, OWNER and RUNBOOK fields and the producing
probe's exit class. Distinguish a failed health sample from missing delivery.
Check source freshness and transport/configuration errors before remediation.
The current source defaults include a ten-minute bot heartbeat window, fifteen
minutes for LXC/OOM pushes, a 26-hour TLS push window, Workers error ratio above
1%, and GitHub core remaining quota below 10. The fragment and evaluator are
canonical; update their descriptions and tests together when changing a bound.
For database reachability, confirm the staging-only endpoint guard first; never
weaken that guard or log a connection URL to diagnose a failed probe.

## Read-only diagnosis boundary

The source pilot permits an ONLY SSH host under a server-enforced forced-command
allowlist (`ro-shell.sh`); this is a proposed scope, not a host installation claim.
No forwarding, interactive shell, command chaining or unlisted command is allowed.
No fix proposal is approved by this document or by an alert. Any remediation
requires its own authorized, reviewable action and rollback evidence.

## Rollback boundary

A separately authorized deployment records its previous immutable pointer and
configuration before activation, and validates both forward and rollback paths.
Do not stop an existing monitor without an owned hold and expiry. Public source
validation never authorizes changing a live cron table, service or alert route.
