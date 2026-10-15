// setup + entries offline suite. Fake host ctx, fake REST collect: no
// network, no board credentials. Pins the wiring contract: disabled ticks
// do nothing, 304s stay silent, digests wake exactly once with an
// idempotency marker, and ack/ETag state persists only after delivery.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup } from "../src/worker/setup.js";
import { JOB_KEYS } from "../src/manifest.js";
import {
  PrSidebarPage,
  PrDetailTab,
  PrCountsWidget,
  checklistFor,
} from "../src/ui/entries.js";

const T0 = 1_758_000_000_000;
const HEAD_A = "a".repeat(40);
const HEAD_B = "b".repeat(40);
const KEY = "org/repo#7";

const TRACKING = Object.freeze({
  repository: "org/repo",
  number: 7,
  issueId: "issue-7",
  identifier: "DEMO-7",
  cardOpen: true,
  title: "Fix the thing",
  prUrl: "https://example.invalid/org/repo/pull/7",
  kind: "org",
  uiStatus: "ours-to-do",
  owner: "agent-a",
  openedAtMs: T0 - 86_400_000,
});

function snapshot(headSha) {
  return {
    repository: "org/repo",
    number: 7,
    state: "open",
    merged: false,
    headSha,
    ci: { rollup: "green", sinceMs: null },
    mergeable: "mergeable",
    reviewDecision: "review_required",
    threads: [],
    comments: [],
    lastOurResponseAtMs: null,
    lastMaintainerAtMs: T0 - 3_600_000,
    fetchedAtMs: T0,
    readOk: true,
  };
}

function fakeCtx(over = {}) {
  const store = {};
  const wakes = [];
  return {
    ctx: {
      jobs: {
        registered: {},
        register(k, fn) {
          this.registered[k] = fn;
        },
      },
      issues: {
        wakes,
        async requestWakeup(issueId, companyId, opts) {
          wakes.push({ issueId, companyId, opts });
          return over.wakeupResult ?? { ok: true };
        },
      },
      secrets: {
        async resolve() {
          return "test-token";
        },
      },
      state: {
        store,
        async get() {
          return store.state ?? null;
        },
        async set(addr) {
          store.state = addr.state;
        },
      },
    },
    wakes,
    store,
  };
}

const OPTS = {
  companyId: "company-1",
  configPath: "plugins/pr-tracker",
  secretRef: { type: "secret_ref", secretId: "github-org-app", version: "latest" },
  scope: { upstream: [], org: ["org/repo"] },
};

function collectWith(headSha, status = 200) {
  return async () => [
    {
      key: KEY,
      status,
      etag: "etag-1",
      input: { prev: null, next: snapshot(headSha), tracking: TRACKING, nowMs: T0 },
    },
  ];
}

test("setup registers pollPrs and requires its options", () => {
  const { ctx } = fakeCtx();
  const api = setup(ctx, { ...OPTS, collect: collectWith(HEAD_A) });
  assert.equal(typeof ctx.jobs.registered[JOB_KEYS.pollPrs], "function");
  assert.equal(typeof api.tick, "function");
  assert.throws(() => setup(ctx, {}), /companyId/);
  assert.throws(() => setup(ctx, { ...OPTS }), /collect/);
});

test("disabled probe stops the tick with zero reads and zero wakes", async () => {
  const { ctx, wakes, store } = fakeCtx();
  let collected = 0;
  const api = setup(ctx, {
    ...OPTS,
    enabledProbe: async () => false,
    collect: async () => {
      collected += 1;
      return [];
    },
  });
  const out = await api.tick();
  assert.equal(out.outcome, "stopped-disabled");
  assert.equal(collected, 0);
  assert.equal(wakes.length, 0);
  assert.equal(store.state, undefined);
});

test("first sight baselines with no wake; head push digests with one idempotent wake", async () => {
  const { ctx, wakes, store } = fakeCtx();
  let head = HEAD_A;
  const api = setup(ctx, { ...OPTS, collect: () => collectWith(head)() });
  const first = await api.tick();
  assert.equal(first.results[0].outcome, "baseline");
  assert.equal(wakes.length, 0);
  assert.ok(store.state.acks[KEY]);
  assert.equal(store.state.etags[KEY], "etag-1");

  head = HEAD_B;
  const second = await api.tick();
  assert.equal(second.results[0].outcome, "digest");
  assert.equal(wakes.length, 1);
  assert.equal(wakes[0].issueId, "issue-7");
  assert.equal(wakes[0].opts.reason, "pr-change");
  assert.match(wakes[0].opts.idempotencyKey, /^[0-9a-f]{24}$|recovery-writer:/);
});

test("304 stays silent and performs no wake", async () => {
  const { ctx, wakes } = fakeCtx();
  const api = setup(ctx, { ...OPTS, collect: collectWith(HEAD_A, 304) });
  const out = await api.tick();
  assert.equal(out.results[0].outcome, "silent");
  assert.equal(out.wakes, 0);
  assert.equal(wakes.length, 0);
});

test("failed delivery withholds the ack advance", async () => {
  const { ctx, wakes, store } = fakeCtx({ wakeupResult: false });
  let head = HEAD_A;
  const api = setup(ctx, { ...OPTS, collect: () => collectWith(head)() });
  await api.tick();
  head = HEAD_B;
  const out = await api.tick();
  assert.equal(out.results[0].delivered, false);
  assert.equal(wakes.length, 1);
  assert.equal(store.state.acks[KEY].snapshot.headSha, HEAD_A);
});

test("getData projects rows, counts, and totals", async () => {
  const { ctx } = fakeCtx();
  const api = setup(ctx, { ...OPTS, collect: collectWith(HEAD_A) });
  await api.tick();
  const data = await api.getData({ nowMs: T0 });
  assert.equal(data.totalCount, 1);
  assert.equal(data.rows[0].repo, "org/repo");
  assert.equal(data.rows[0].badge, "warning");
  assert.deepEqual(data.counts, { needsUs: 1, waiting: 0, redCi: 0 });
  const empty = await api.getData({ filter: { status: "merged" }, nowMs: T0 });
  assert.equal(empty.totalCount, 0);
});

test("performAction wakes one known PR and rejects unknown keys", async () => {
  const { ctx, wakes } = fakeCtx();
  const api = setup(ctx, { ...OPTS, collect: collectWith(HEAD_A) });
  await api.tick();
  const res = await api.performAction({ action: "wake", key: KEY });
  assert.deepEqual(res, { ok: true });
  assert.equal(wakes.length, 1);
  await assert.rejects(api.performAction({ action: "wake", key: "nope" }), /unknown PR key/);
  await assert.rejects(api.performAction({ action: "merge" }), /unknown action/);
});

test("F1: untracked reads persist no null and getData never throws", async () => {
  const { ctx, wakes, store } = fakeCtx();
  const validNext = {
    repository: "org/repo",
    number: 9,
    state: "open",
    merged: false,
    headSha: HEAD_A,
    ci: { rollup: "green", sinceMs: null },
    mergeable: "mergeable",
    reviewDecision: "review_required",
    threads: [],
    comments: [],
    lastOurResponseAtMs: null,
    lastMaintainerAtMs: null,
    fetchedAtMs: T0,
    readOk: true,
  };
  const api = setup(ctx, {
    ...OPTS,
    collect: async () => [
      { key: "org/repo#9", status: 200, input: { prev: null, next: validNext, tracking: null, nowMs: T0 } },
    ],
  });
  const out = await api.tick();
  assert.equal(out.results[0].outcome, "untracked");
  assert.equal(wakes.length, 0);
  assert.ok(!("org/repo#9" in store.state.tracking));
  const data = await api.getData({ nowMs: T0 });
  assert.equal(data.totalCount, 0);
  // Legacy null entries are skipped, not dereferenced.
  store.state.tracking["org/repo#9"] = null;
  const data2 = await api.getData({ nowMs: T0 });
  assert.equal(data2.totalCount, 0);
});

test("failed delivery rolls back list-URL ETags so the next tick retries", async () => {
  const { ctx, wakes, store } = fakeCtx({ wakeupResult: false });
  const LIST = "https://example.invalid/list";
  let head = HEAD_A;
  let listEtag = "list-v1";
  const api = setup(ctx, {
    ...OPTS,
    collect: async ({ etags }) => {
      etags[LIST] = listEtag;
      return [
        {
          key: KEY,
          status: 200,
          etag: `etag-${head.slice(0, 1)}`,
          input: { prev: null, next: snapshot(head), tracking: TRACKING, nowMs: T0 },
        },
      ];
    },
  });
  await api.tick();
  assert.equal(store.state.etags[LIST], "list-v1");
  assert.equal(store.state.etags[KEY], "etag-a");

  // Digest tick whose wake fails: the list advance rolls back and the
  // per-read ETag does not advance, so the next tick re-collects.
  head = HEAD_B;
  listEtag = "list-v2";
  const out = await api.tick();
  assert.equal(out.results[0].delivered, false);
  assert.equal(wakes.length, 1);
  assert.equal(store.state.etags[LIST], "list-v1");
  assert.equal(store.state.etags[KEY], "etag-a");
  assert.equal(store.state.acks[KEY].snapshot.headSha, HEAD_A);
});

test("a failed wake is re-derived on the next tick even when the list answered 304 before", async () => {
  const wake = { wakeupResult: false };
  const { ctx, wakes, store } = fakeCtx(wake);
  const LIST = "https://example.invalid/list";
  const ackedHead = () => store.state.acks[KEY]?.snapshot.headSha ?? HEAD_A;
  let head = HEAD_A;
  let serverList = "list-v1";
  const served = [];
  const api = setup(ctx, {
    ...OPTS,
    collect: async ({ etags }) => {
      if (etags[LIST] === serverList) {
        served.push(304);
        // A list 304 re-reads the PR from its stored snapshot, not the live head.
        return [
          {
            key: KEY,
            status: 200,
            refresh: true,
            input: { prev: null, next: snapshot(ackedHead()), tracking: TRACKING, nowMs: T0 },
          },
        ];
      }
      served.push(200);
      etags[LIST] = serverList;
      return [
        {
          key: KEY,
          status: 200,
          etag: `etag-${head.slice(0, 1)}`,
          input: { prev: null, next: snapshot(head), tracking: TRACKING, nowMs: T0 },
        },
      ];
    },
  });
  const first = await api.tick();
  assert.equal(first.results[0].outcome, "baseline");

  head = HEAD_B;
  serverList = "list-v2";
  const failed = await api.tick();
  assert.equal(failed.results[0].delivered, false);
  assert.equal(wakes.length, 1);
  assert.equal(store.state.etags[LIST], "list-v1");

  wake.wakeupResult = { ok: true };
  const retried = await api.tick();
  assert.equal(retried.results[0].delivered, true);
  assert.equal(wakes.length, 2);
  assert.equal(wakes[1].opts.idempotencyKey, wakes[0].opts.idempotencyKey);
  assert.equal(store.state.acks[KEY].snapshot.headSha, HEAD_B);

  const quiet = await api.tick();
  assert.equal(quiet.results[0].outcome, "silent");
  assert.equal(wakes.length, 2);
  assert.deepEqual(served, [200, 200, 200, 304]);
});

test("quiet-tick refresh keeps the stored enriched tracking", async () => {
  const { ctx, wakes, store } = fakeCtx();
  let mode = "full";
  const api = setup(ctx, {
    ...OPTS,
    collect: async () => {
      if (mode === "full") return collectWith(HEAD_A)();
      // Quiet tick: unenriched resolver output, as refreshOnePr returns it.
      return [
        {
          key: KEY,
          status: 200,
          refresh: true,
          input: {
            prev: null,
            next: snapshot(HEAD_A),
            tracking: {
              repository: "org/repo", number: 7, issueId: "issue-7",
              identifier: "DEMO-7", cardOpen: true,
            },
            nowMs: T0,
          },
        },
      ];
    },
  });
  await api.tick();
  assert.equal(store.state.tracking[KEY].title, "Fix the thing");

  mode = "refresh";
  const out = await api.tick();
  assert.equal(out.results[0].outcome, "silent");
  assert.equal(wakes.length, 0);
  // Row data survives the quiet tick: title, link, owner, kind intact.
  assert.equal(store.state.tracking[KEY].title, "Fix the thing");
  assert.equal(store.state.tracking[KEY].prUrl, "https://example.invalid/org/repo/pull/7");
  assert.equal(store.state.tracking[KEY].owner, "agent-a");
  assert.equal(store.state.tracking[KEY].kind, "org");
  const data = await api.getData({ nowMs: T0 });
  assert.equal(data.rows[0].title, "Fix the thing");
});

test("refresh lets live card state win over stored tracking", async () => {
  const { ctx, wakes, store } = fakeCtx();
  let mode = "full";
  let liveCardOpen = true;
  const api = setup(ctx, {
    ...OPTS,
    collect: async () => {
      if (mode === "full") return collectWith(HEAD_A)();
      return [
        {
          key: KEY,
          status: 200,
          refresh: true,
          input: {
            prev: null,
            next: snapshot(HEAD_A),
            tracking: {
              repository: "org/repo", number: 7, issueId: "issue-7",
              identifier: "DEMO-7", cardOpen: liveCardOpen,
            },
            nowMs: T0,
          },
        },
      ];
    },
  });
  await api.tick();
  assert.equal(store.state.tracking[KEY].cardOpen, true);

  // Card closed while the PR is quiet: the live cardOpen wins and the
  // reopen-mismatch digest fires, with stored enrichment surviving.
  mode = "refresh";
  liveCardOpen = false;
  const out = await api.tick();
  assert.equal(out.results[0].outcome, "digest");
  assert.equal(wakes.length, 1);
  assert.equal(store.state.tracking[KEY].cardOpen, false);
  assert.equal(store.state.tracking[KEY].title, "Fix the thing");
});

test("refresh keeps explicit untracked (null) instead of the stored entry", async () => {
  const { ctx, wakes, store } = fakeCtx();
  let mode = "full";
  const api = setup(ctx, {
    ...OPTS,
    collect: async () => {
      if (mode === "full") return collectWith(HEAD_A)();
      return [
        {
          key: KEY,
          status: 200,
          refresh: true,
          input: { prev: null, next: snapshot(HEAD_A), tracking: null, nowMs: T0 },
        },
      ];
    },
  });
  await api.tick();
  mode = "refresh";
  const out = await api.tick();
  assert.equal(out.results[0].outcome, "untracked");
  assert.equal(wakes.length, 0);
});

test("F2: collector-mutated list ETags persist across ticks", async () => {
  const { ctx, store } = fakeCtx();
  const api = setup(ctx, {
    ...OPTS,
    collect: async ({ etags }) => {
      etags["https://example.invalid/list"] = "list-etag-2";
      return [];
    },
  });
  await api.tick();
  assert.equal(store.state.etags["https://example.invalid/list"], "list-etag-2");
});

test("401 stop-source drops the cached Bearer so rotation re-resolves next tick", async () => {
  const { ctx, store } = fakeCtx();
  let resolveCalls = 0;
  let currentSecret = "token-old";
  ctx.secrets.resolve = async () => {
    resolveCalls += 1;
    return currentSecret;
  };
  const seenTokens = [];
  let mode = 401;
  const api = setup(ctx, {
    ...OPTS,
    collect: async ({ token }) => {
      seenTokens.push(token);
      if (mode === 401) {
        return [{ key: KEY, status: 401 }];
      }
      return [
        {
          key: KEY,
          status: 200,
          etag: "etag-1",
          input: { prev: null, next: snapshot(HEAD_A), tracking: TRACKING, nowMs: T0 },
        },
      ];
    },
  });
  const first = await api.tick();
  assert.equal(first.results[0].via, "stop-source");
  assert.equal(first.results[0].outcome, "unknown");
  assert.equal(resolveCalls, 1);
  assert.equal(seenTokens[0], "token-old");

  // Secret value rotates (e.g. fresh 1h App Bearer stored under same ref).
  currentSecret = "token-new";
  mode = 200;
  const second = await api.tick();
  assert.equal(second.results[0].outcome, "baseline");
  assert.equal(resolveCalls, 2);
  assert.equal(seenTokens[1], "token-new");
  assert.ok(store.state.acks[KEY]);
});

test("200/304 ticks keep the cached Bearer (no re-resolve)", async () => {
  const { ctx } = fakeCtx();
  let resolveCalls = 0;
  ctx.secrets.resolve = async () => {
    resolveCalls += 1;
    return "stable-token";
  };
  const seenTokens = [];
  let status = 200;
  const api = setup(ctx, {
    ...OPTS,
    collect: async ({ token }) => {
      seenTokens.push(token);
      if (status === 304) {
        return [{ key: KEY, status: 304 }];
      }
      return [
        {
          key: KEY,
          status: 200,
          etag: "etag-1",
          input: { prev: null, next: snapshot(HEAD_A), tracking: TRACKING, nowMs: T0 },
        },
      ];
    },
  });
  const first = await api.tick();
  status = 304;
  const second = await api.tick();
  assert.equal(first.results[0].outcome, "baseline");
  assert.equal(second.results[0].via, "etag-304");
  assert.equal(resolveCalls, 1);
  assert.deepEqual(seenTokens, ["stable-token", "stable-token"]);
});

test("entries: sidebar page, detail tab, widget, checklist", () => {
  const records = [
    {
      repo: "org/repo", number: 7, title: "Fix", prUrl: "u", kind: "org",
      status: "ci-red", taskId: "issue-7", owner: "a", openedAtMs: T0 - 10,
    },
    {
      repo: "org/repo", number: 8, title: "Feat", prUrl: "u", kind: "org",
      status: "waiting-maintainer", taskId: "issue-8", owner: "b", openedAtMs: T0 - 20,
    },
  ];
  const page = PrSidebarPage({ records, nowMs: T0 });
  assert.equal(page.columns.length, 12);
  assert.equal(page.totalCount, 1); // saved default is needs-us
  const all = PrSidebarPage({ records, filter: {}, nowMs: T0 });
  assert.equal(all.totalCount, 2);
  assert.deepEqual(page.counts, { needsUs: 1, waiting: 1, redCi: 1 });
  const filtered = PrSidebarPage({ records, filter: { needsUs: true }, nowMs: T0 });
  assert.equal(filtered.totalCount, 1);

  const snaps = { "org/repo#7": snapshot(HEAD_A) };
  const tab = PrDetailTab({ records, snapshots: snaps, taskId: "issue-7", nowMs: T0 });
  assert.equal(tab.length, 1);
  assert.equal(tab[0].statusLabel, "CI red");
  const keys = Object.fromEntries(tab[0].checklist.map((c) => [c.key, c.ok]));
  assert.deepEqual(keys, { open: true, ci: true, mergeable: true, review: false, threads: true });

  assert.deepEqual(PrCountsWidget({ records }), { needsUs: 1, waiting: 1, redCi: 1 });
  assert.deepEqual(checklistFor({}), [
    { key: "open", label: "PR is open", ok: false },
    { key: "ci", label: "CI is green", ok: false },
    { key: "mergeable", label: "Mergeable (no conflicts)", ok: false },
    { key: "review", label: "Review approved", ok: false },
    { key: "threads", label: "No unresolved threads", ok: true },
  ]);
});
