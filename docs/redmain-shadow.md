# Red-main shadow observation

The product consumer can observe default-branch CI without opening incidents or
changing PR wake decisions. It is **off by default**. No runtime configuration is
changed by shipping this code.

An optional consumer configuration block enrolls repositories for observation:

```json
{
  "closeout": {
    "redmainShadow": {
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
observes each enrolled repository once under the receipt lock. It reuses
`getMainHeadChecks`: one GraphQL default-branch head/check-rollup snapshot, then
one REST branch-protection read for required checks. These are **two transport
requests**, sharing the pass's request counter, deadline and cancellation signal.
No extra listing, retries, credential fallback or board/index lookup is added.
The existing adapter rejects unreadable legacy branch protection, including
ruleset-only repositories, rather than guessing that no checks are required.

Admission defers before **each** transport request when no surplus remains. A
partial head read without its required-check set counts as deferred, never a
verdict. A deadline beginning inside either post-primary shadow drain preserves
the primary outcome; the deferred summary still emits. Stops predating the drain,
request-budget stops and cancellation continue to fail the pass. In-flight reads
settle before the lock releases.

## Evidence and non-effects

The result gains `redmainShadow: { counters, byReason }` and the log sink receives
one `closeout-redmain-shadow` JSON summary per unstopped or drain-contained pass.
Only fixed reason labels and numeric counters are logged: no titles, bodies,
URLs, raw error text or unvalidated head fields. Read/shape failures count as
errors and do not prevent later repositories from being observed.

The watcher always receives **empty live enrollment** and a store whose incident
writers throw. A hook-local memory map models would-open, already-open and
would-recover transitions only; it is discarded with the hook at pass end. A new
pass observes red again as would-open. Nothing is persisted to incident, claim,
pending, index or receipt state by this hook. There is no board/task creation,
comment, suppression, merge or disarm mutation. Red-main observation never
suppresses or gates the independent disarm shadow.

## Local verification

```sh
npm --prefix gh-event-capture run check
npm --prefix gh-event-capture test
node --test gh-event-capture/test/closeout-redmain-wiring.test.mjs
```

The wiring tests cover off-mode byte compatibility, refusing writer spies,
in-memory transitions, malformed/unreadable heads, both transport admissions,
lock-held drainage, deadline containment, cancellation and sibling-drain behavior.
Before promotion, changing the empty live enrollment to observation enrollment
must fail the would-open and zero-writer assertions (hand-mutant check).
