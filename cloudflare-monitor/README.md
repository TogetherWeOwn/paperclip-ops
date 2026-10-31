# Cloudflare uptime monitor

A standalone Workers Free-compatible monitor: Cron Triggers, one SQLite-backed
Durable Object per environment, passive HTTPS checks, managed edge-certificate
expiry, and a Discord webhook. It does not install anything on a host or change
any monitored application. There is no public status dashboard or target list.

## Contract

- Five-minute cadence; exact HTTP 200 required; redirects are failures. Requests
  have an eight-second timeout and do not read application response bodies.
- TLS expiry is read from Cloudflare's **active managed certificate packs**.
  Every matching active certificate must expire more than 14 days away. Missing
  coverage, malformed expiry, unavailable credentials and API failures are DOWN,
  never silently healthy. Wildcards match exactly one label.
- This is **not** an origin-certificate or arbitrary Internet TLS scanner.
  Workers currently does not implement `TLSSocket.getPeerCertificate()` or
  `getPeerX509Certificate()`. Targets must use the Cloudflare-managed edge
  certificates being queried. A successful HTTPS request separately validates
  the served connection's trust/hostname; pack inventory does not prove which
  specific certificate was served. Verify the DNS/proxy contract before use.
- Healthy first observation is silent. An unhealthy transition and its recovery
  each produce one message; stable states do not repeat. Failed delivery does
  not advance delivered state, so a subsequent tick retries. If the first DOWN
  delivery fails and the target recovers before delivery, there is no invented
  recovery notification.
- One object serializes observation, notification and persistence. Discord has
  no transactional webhook idempotency with object storage: a process failure
  after delivery but before persistence can duplicate a message. Delivery is
  at-least-once, **not** exactly-once under that crash window.
- `/healthz` returns only `{ok,fresh}`. Never-run, older-than-15-minute,
  unhealthy-target and failed-delivery snapshots return 503. It exposes no
  URLs, zone IDs, alert destinations or raw upstream errors.

## Private configuration

Actual target URLs, account/zone identifiers, destinations and tokens belong in
Worker secrets, not this repository, workflow logs or public evidence.

`MONITOR_CONFIG` has this shape (reserved example values only):

```json
{
  "httpChecks": [
    {"id":"web-production","name":"Web production","url":"https://www.example.com/up"},
    {"id":"web-staging","name":"Web staging","url":"https://staging.example.com/up"},
    {"id":"platform-api","name":"Platform API","url":"https://platform.example.com/api/health"}
  ],
  "tlsChecks": [
    {"id":"web-production-tls","name":"Web production TLS","zoneId":"00000000000000000000000000000000","hostname":"www.example.com"},
    {"id":"web-staging-tls","name":"Web staging TLS","zoneId":"00000000000000000000000000000000","hostname":"staging.example.com"},
    {"id":"platform-api-tls","name":"Platform API TLS","zoneId":"11111111111111111111111111111111","hostname":"platform.example.com"}
  ]
}
```

At most eight HTTP and eight TLS checks. IDs are unique, stable ASCII slugs;
labels contain only ASCII letters, numbers, spaces, dots, underscores and hyphens.
HTTP URLs must use public HTTPS hostnames without ports, credentials, query
strings or fragments. Do not point checks at login flows or mutating routes.
Use public unauthenticated health endpoints; a login redirect is not healthy.

Required secret bindings:

| Binding | Narrow scope |
| --- | --- |
| `MONITOR_CONFIG` | Actual public health URLs and their managed edge certificate zones |
| `ALERT_WEBHOOK_URL` | One existing authorized Discord webhook, `https://discord.com/api/webhooks/...` |
| `CF_CERTIFICATES_READ_TOKEN` | SSL and Certificates Read on only the monitored zones; not an account-edit token |
| `DRILL_TOKEN` | Staging only, at least 32 characters; never install on production |

Use granted native connection/secret provisioning. Do not retrieve, rotate or
copy unrelated application credentials. The runtime does not need Workers Edit,
DNS Edit, billing access, application tokens or database credentials.

## Free-tier admission

Do not upgrade plans, enable paid add-ons, or deploy until the selected account
and resources are confirmed eligible for **Workers Free**. If paid service is
required, stop rather than accepting a charge. A paid account's usage is not
proven cost-free just because this workload is small.

[SQLite Durable Objects are available on Workers Free](https://developers.cloudflare.com/durable-objects/platform/pricing/).
Per environment, 288 scheduled runs/day and at most 16 probes/run fit the published
100,000 Worker/object requests/day, 5 million SQLite row reads/day, 100,000 row
writes/day and 5 GB storage limits. Only one bounded snapshot is stored. At the
8-second probe plus 8-second delivery bounds, scheduled wall-time is under
4,608 seconds/day per environment; a 128 MB object is under 576 GB-seconds/day,
well below 13,000 GB-seconds/day. Actual CPU must still be measured against the
Free plan's CPU limits in staging. Public health traffic consumes request quota;
Free-plan limits fail closed rather than creating overage permission.

These are estimates, not evidence of the account's current plan, cron execution,
resource limits, live secret bindings or delivered alerts. No deploy has been
performed merely by publishing this source.

## Verify, stage, deploy and roll back

Run the offline acceptance suite before asking for review:

```sh
cd cloudflare-monitor
npm run check
npm test
```

### Offline transport contract

Production accepts only Discord webhook URLs; do not relax that allowlist for
fixtures. Offline acceptance tests may use an entirely fabricated Discord-shaped
destination (numeric placeholder ID and an obvious `example-only` token) **only**
with an injected transport and a global network-denial stub installed before
loading the runtime. This exception applies to the destination's required API
shape, not to monitored targets: HTTP/TLS target fixtures still use reserved
example domains. No real webhook, credential or network request is permitted.
Webhook receipts are injected JSON objects with numeric string message IDs;
Discord's `wait=true` query must be handled by the stub.

Tests must retain rejection coverage for non-Discord destinations. Invalid
private-address/suffix cases are offline validation inputs, not inventory or
probe destinations; construct those inputs from explicit fixture components so
publication checks do not mistake them for actual endpoints. Do not exempt test
files from the public-safety scanner.

A lightweight CI job runs these checks even on draft PRs and is included in
`ci-ok`; a draft's skipped heavy suites are not monitor acceptance evidence.

Deployment must use the merged, CI-green, independently reviewed exact source
through the repository's deploy workflow. Use pinned Wrangler **4.156.0**; account
selection is private via `CLOUDFLARE_ACCOUNT_ID`. No account ID is committed.
The two configs create separate workers, object namespaces and state. Production
uses `wrangler.toml`; staging uses `wrangler.staging.toml`.

Before production:

1. Confirm free-plan admission and authorized secret provisioning. Record current
   Worker version, schedules and bindings privately as the rollback baseline.
2. Deploy staging first and record immutable version/source SHA. Verify real
   passive health and managed TLS results. API access errors are blockers.
3. POST `/drill` on the **staging worker only** with bearer `DRILL_TOKEN`, JSON
   `{"phase":"down"}`, then `{"phase":"up"}`. Preserve JSON receipts and the
   actual Discord message IDs. The request body must be at most 128 bytes.
4. HTTP drills fetch real staging-hosted `/fixture/http/<id>/down|up` endpoints;
   TLS drills use synthetic one-day/90-day certificate records through the same
   evaluator. Label that proof synthetic; it is not a real certificate-expiry
   event. The drill object is separate from live state. No target app is changed.
5. Check each configured ID has ALERT and RECOVERED once; run repeated stable
   phases and confirm `delivery.sent=false`. Production rejects every drill and
   fixture route regardless of authorization header. Never inject faults in a
   production application or production monitor.
6. Deploy identical reviewed source to production. Record source/version, cron
   execution and healthy passive observations. Monitor validation is incomplete
   without actual delivered alert/recovery receipts.

Roll back with pinned Wrangler `rollback <recorded-version-id> --config <config>`
through the same repository workflow. Never delete the object namespace or state
as rollback. On a first deployment there is no previous version: a reviewed fix
or disabling the new worker's schedule is the containment path, with the lost
alerting explicitly recorded. No TWO application rollback should be needed.

## Legacy host-signal disposition

The legacy modules are source contracts, not proof of currently installed
telemetry. Removing Gatus does not magically move these signals into Workers.

| Previous signal | Source contract | Native source / residual gap |
| --- | --- | --- |
| OOM events | `../ops/case_658bf6b9/push-oom-kills.sh` | Kernel journal. No existing authorized hosted API verified; uncovered. Do not rebuild a host push cron. |
| Disk bytes/inodes, pools, VM disks | `../ops/case_a814f742/push-runner-disk.sh` | Filesystem/storage provider telemetry. No hosted replacement verified; uncovered. |
| CI queue age | `../ops/case_658bf6b9/_ci_queue_age.sh` | GitHub Actions jobs API. Use genuinely queued jobs, pagination and completeness checks, not workflow age. Existing GitHub status/notifications remain native sources; new queue alert delivery is not claimed here. |
| Runner registration/online/busy | `../ops/case_ebbb469a/push-runner-census.py` | GitHub organization runners API, with runner-read grant. Native inventory is not itself an alert. |
| Pool provisioning/min-idle/capacity | `../ops/case_658bf6b9/push-garm-pool.sh` | Pool-manager state; no authorized hosted API verified. GitHub online runners cannot establish provisioning health. |
| Pool update freshness | `../ops/case_658bf6b9/push-pool-freshness.sh` | Pool-manager timestamps; not equivalent to CI activity. Uncovered without native API. |
| Workers errors | `../ops/case_658bf6b9/push-workers-error-rate.sh` | Cloudflare native analytics/logs. Zero traffic is not measured health. |
| Database/backup freshness | `../ops/case_658bf6b9/MODULES.md` | Provider-native backup/health records where authorized; no database probes or new host jobs in this monitor. |

Do not republish actual hosts, private identifiers, source credentials or legacy
push tokens in a migration inventory. Record verified adoption and remaining
coverage gaps on the private operations task.
