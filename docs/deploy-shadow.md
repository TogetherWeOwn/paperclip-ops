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

## Local verification

```sh
npm --prefix gh-event-capture run check
npm --prefix gh-event-capture test
node --test gh-event-capture/test/closeout-deploy-wiring.test.mjs
```

The wiring tests cover off-mode byte compatibility, refusing writer spies,
positive staging-success observation, negative cases, listing/body admission,
lock-held drainage, deadline containment, cancellation and sibling-drain
behavior. Before promotion, changing the empty live enrollment to observation
enrollment must fail the would-record and zero-writer assertions (hand-mutant
check).
