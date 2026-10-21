# Deploy-success shadow observation

The product consumer can observe staging deployment outcomes without recording
intents or running QA smoke. It is **off by default**. No runtime configuration
is changed by shipping this code.

An optional consumer configuration block enrolls repositories for observation:

```json
{
  "closeout": {
    "deployShadow": {
      "enabledRepositories": ["TogetherWeOwn/paperclip-ops"]
    }
  }
}
```

Every entry must be a unique `owner/repo` already in `allowedRepositories`.
Unknown keys or malformed enrollment fail before transport or state effects.
An absent block or empty list creates no hook, adds no reads, emits no lines and
adds no result field. Remove the block or empty the list to roll back observation.

## Reads and containment

After the primary event and sweep streams and any index work settle, the runner
observes each enrolled repository once under the receipt lock. It reuses the
existing deployment-success watcher: one bounded `deployment_status` listing
(seven-day window, paged) plus one body read per delivery, capped at 1000
bodies per repository. Every listing page and body read shares the pass's
request counter, deadline and cancellation signal. No extra listing, retries,
credential fallback or board/index lookup is added.

Admission defers before **each** transport request when no surplus remains. An
incomplete listing or an unread body counts as deferred or error, never a
verdict. A deadline beginning inside any post-primary shadow drain preserves
the primary outcome; the deferred summary still emits. Stops predating the
drain, request-budget stops and cancellation continue to fail the pass.
In-flight reads settle before the lock releases.

## Evidence and non-effects

The result gains `deployShadow: { counters, byReason }` and the log sink
receives one `closeout-deploy-shadow` JSON summary per unstopped or
drain-contained pass. Only fixed reason labels and numeric counters are logged:
no titles, bodies, URLs, SHAs, repository names, raw error text or unvalidated
delivery fields. Read/shape failures count as errors and do not prevent later
repositories from being observed.

The watcher always receives **empty live enrollment** and an intent store whose
`recordIntent` throws; `findIntent` consults nothing and always misses. A row
that changes event between listing and read is a counted error, never a
verdict. No suppression input is connected: red-main counters never suppress
this hook. Nothing is persisted to intent, claim, pending, index or receipt
state. There is no QA smoke execution, task creation, comment, wake, merge or
disarm mutation. Deploy-success observation never suppresses or gates the
independent disarm and red-main shadows.

## Deployment-failure shadow observation

A sibling observer watches `deployment_status` deliveries with an exact,
case-sensitive `failure` or `error` state. It is enrolled independently under
`closeout.deployFailShadow` with the same enrollment rules: unique `owner/repo`
values already in `allowedRepositories`, unknown keys refused before effects,
absent or empty meaning fully off with no extra reads, logs, result field or
state change. Remove the block or empty the list to roll back observation.

```json
{
  "closeout": {
    "deployFailShadow": {
      "enabledRepositories": ["TogetherWeOwn/paperclip-ops"]
    }
  }
}
```

The failure observer drains after the success observer under the same receipt
lock, with the same bounds (seven-day window, paged listing, at most 1000 body
reads per repository), the same shared surplus admission and the same
deadline/cancellation containment. The deployment environment is recorded, never
matched: staging and production failures both count. `success` stays on the
success path and every other state is acknowledged non-failure; none of them
can become a failure intent.

The result gains `deployFailShadow: { counters, byReason }` and the log sink
receives one `closeout-deployfail-shadow` JSON summary per unstopped or
drain-contained pass. Summaries and logs carry fixed counters and reason labels
only: no URLs, SHAs, repository names, bodies or error text. The log pointer is
first-safe-wins across the documented link chain; credentialed or otherwise
unsafe candidates are skipped in favor of a safe fallback, and an all-unsafe
chain records a null pointer with a counted `unsafePointer` drop. Live
enrollment stays empty, the intent writer throws, and no suppression input is
connected: red-main counters never suppress this hook. Nothing is persisted,
and no task, comment, wake, merge or disarm mutation can occur.

## Local verification

```sh
npm --prefix gh-event-capture run check
npm --prefix gh-event-capture test
node --test gh-event-capture/test/closeout-deploy-wiring.test.mjs
node --test gh-event-capture/test/closeout-deployfail-wiring.test.mjs
```

The wiring tests cover off-mode byte compatibility, refusing writer spies,
positive staging-success observation, negative cases, listing/body admission,
lock-held drainage, deadline containment, cancellation and sibling-drain
behavior. Before promotion, changing the empty live enrollment to observation
enrollment must fail the would-record and zero-writer assertions (hand-mutant
check).
