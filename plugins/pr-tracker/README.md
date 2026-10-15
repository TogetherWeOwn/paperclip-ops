# PR Tracker plugin (staging scaffold)

Worker-first staging area for the PR Tracker Paperclip plugin. The final
home is the new **public** repo `TogetherWeOwn/paperclip-plugin-pr-tracker`
(repo creation is a GitHub-admin step, owned by the CEO as an `Operator:`
card); this scaffold exports verbatim when that repo exists. Rollback for
everything here is delete-the-directory: no core patch, no live wiring.

## What lives here

- `src/decision-core/upstream-watcher.js` — pure decision core ported from
  ops-tooling PR #539 (`gh-event-capture/src/upstream-watcher.js`, 533
  lines). No I/O: snapshot + ack in, at most one digest out
  (`silent`/`baseline`/`unknown`/`untracked`/`digest`/`latched`). Behavior
  is verbatim; only internal tracker references were scrubbed from comments
  and the collection contract now names the `pollPrs` 2-minute REST+ETag
  collector instead of the old 15-minute host schedule.
- `test/upstream-watcher.test.mjs` — the ported offline suite (27 tests,
  `node --test`). No network, no board credentials. Fixture identifiers are
  `DEMO-*` placeholders.
- `src/manifest.js` — staging plugin manifest: worker capabilities plus the
  `pollPrs` job at `*/2 * * * *`, plus the three UI slots (`sidebar` "Pull
  Requests", `detailTab` on issues, `dashboardWidget` counts) with
  `entrypoints.ui`. Slot shapes verified against the live SDK (see phase
  gate below).
- `src/ui/view-model.js` — pure sidebar view-model: 7-status vocabulary,
  `StatusBadge` mapping, `needs-us` filter set (saved default), combined
  status/repo/kind/owner/text filters, widget counts, and `DataTable` row
  projection. No rendering; the host SDK owns that.
- `src/worker/setup.js` — thin host adapter: `setup(ctx, options)` registers
  the `pollPrs` job, resolves the read-only secret ref, loads/saves
  ack+ETag+tracking state, delivers digests via idempotent task wakes, and
  exposes `getData`/`performAction` for the UI bridges. REST collection
  stays injected and fail-loud (`unknown`, never `silent`).
- `src/ui/entries.js` — UI bundle entry models behind the manifest
  `exportName`s: sidebar page, task tab with per-PR compliance checklist,
  and widget counts. The host prebuilds this to `./dist/ui.js`.
- `src/worker/collect-rest.js` — REST collector factory: ETag list polling
  with head-SHA skip, per-PR detail mapping (CI rollup, mergeable, review
  verdict, threads/comments as hashes, response times) to core snapshots.
  Rate-limited 403s normalize to 429; malformed reads map to `unknown`.
- `scripts/pack.sh` — mirrors dependency-free ESM into `dist/` with the two
  manifest entrypoints (`npm run build` wiring rides the export).
- `src/worker/poll-prs.js` — `pollPrs` collector skeleton. Pure and
  dependency-free: scope dedup (org kind wins overlaps), ETag conditional
  headers, status classifier (401/403 stops the source — never substitute
  credentials; 429/5xx backs off; anything else unexpected is `unknown`,
  never `silent`), bounded backoff with jitter, disabled-tick guard (zero
  reads, zero wakes), and `runPollTick` wiring the decision core with the
  4h/4h/7d worker policy. Ack persists only after durable delivery; failed
  delivery withholds the advance so the retry converges.
- `test/poll-prs.test.mjs` — offline suite for the above (11 tests), ending
  in a real-core integration pass: baseline first sight, digest a head push,
  deliver exactly once.

## Policy mapping (worker-side, shipped)

The core defaults stay 24h/24h/7d. The worker passes
`{ responseSlaMs: 4h, redCiSlaMs: 4h, silencePingMs: 7d }` (`workerPolicy()`);
the 12h re-wake plus CEO desk and the 24h `upstream-pr-compliance` red live
in the worker ladder (`POLICY.rewakeMs` / `POLICY.gatusRedMs`), not the
core. Org-PR intake (every open PR in TogetherWeOwn repos, plus
owner-authored upstream PRs by search) extends the upstream-only core at the
collection layer (`scopeTargets`); the decision function is unchanged.

## Phase gate (CTO condition — cleared 2026-10-04)

Slot shapes verified against the live SDK: all three slot types valid;
`detailTab` carries `entityTypes: ["issue"]`; sidebar `routePath` is a
lowercase single-segment slug; `entrypoints.ui` declared alongside the
slots. The prebuilt UI bundle entry (`./dist/ui.js`) and runtime wiring
(`jobs.register`, `requestWakeup`, secret refs, `plugin.state`) are the
next slice, built against the full `PLUGIN_SPEC.md`.

## Verify

```sh
node --test test/*.mjs
```

## Public-export hygiene

Clean: no `TOG-`/`PAP-` IDs, no private URLs, no secrets in this
directory. Keep it that way; board linkage travels via Paperclip work
products, never in this tree.
