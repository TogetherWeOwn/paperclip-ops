// collect-rest offline suite. Fake transport keyed by URL: no network, no
// credentials. Pins the mapper vocabulary, ETag/304 and rate-limit paths,
// and the fail-loud contract (bad reads are `unknown`, never `silent`).

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ciRollupFromChecks,
  mergeableFromPr,
  reviewDecisionFromReviews,
  fetchJson,
  fetchAllPages,
  snapshotFromRest,
  createRestCollector,
} from "../src/worker/collect-rest.js";
import { normalizeSnapshot, decideUpstreamWatch } from "../src/decision-core/upstream-watcher.js";
import { HOUR_MS, runPollTick } from "../src/worker/poll-prs.js";

/** Baseline ack for a fresh read, exactly as `runPollTick` would persist it. */
function ackOf(read, tracking = TRACK) {
  const decided = decideUpstreamWatch({
    prev: null,
    next: read.input.next,
    tracking,
    policy: undefined,
    nowMs: Date.now(),
    cardMarkers: [],
  });
  assert.equal(decided.action, "baseline");
  return decided.nextAck;
}

const T0 = 1_758_000_000_000;
const HEAD_A = "a".repeat(40);
const ISO = new Date(T0 - 3_600_000).toISOString();

function headers(map = {}) {
  const lower = Object.fromEntries(Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]));
  return { get: (k) => lower[String(k).toLowerCase()] ?? null };
}

function prBody(over = {}) {
  return {
    number: 7,
    state: "open",
    merged: false,
    merged_at: null,
    title: "Fix the thing",
    html_url: "https://example.invalid/org/repo/pull/7",
    user: { login: "owner-x", type: "User" },
    head: { sha: HEAD_A },
    mergeable: true,
    mergeable_state: "clean",
    created_at: ISO,
    updated_at: ISO,
    ...over,
  };
}

test("ci rollup: red wins with earliest start, then pending, green, unknown", () => {
  assert.deepEqual(ciRollupFromChecks([]), { rollup: "unknown", sinceMs: null });
  assert.deepEqual(
    ciRollupFromChecks([{ status: "completed", conclusion: "success" }]),
    { rollup: "green", sinceMs: null },
  );
  assert.deepEqual(
    ciRollupFromChecks([
      { status: "completed", conclusion: "success" },
      { status: "in_progress", conclusion: null },
    ]),
    { rollup: "pending", sinceMs: null },
  );
  const red = ciRollupFromChecks([
    { status: "completed", conclusion: "success" },
    { status: "completed", conclusion: "failure", started_at: ISO },
  ]);
  assert.equal(red.rollup, "red");
  assert.equal(red.sinceMs, Date.parse(ISO));
});

test("mergeable and review vocabularies", () => {
  assert.equal(mergeableFromPr({ mergeable: true }), "mergeable");
  assert.equal(mergeableFromPr({ mergeable: false }), "conflicting");
  assert.equal(mergeableFromPr({ mergeable: null, mergeable_state: "behind" }), "behind");
  assert.equal(mergeableFromPr({}), "unknown");
  assert.equal(reviewDecisionFromReviews([]), "review_required");
  assert.equal(reviewDecisionFromReviews([{ state: "COMMENTED" }]), "review_required");
  assert.equal(reviewDecisionFromReviews([{ state: "APPROVED" }]), "approved");
  assert.equal(
    reviewDecisionFromReviews([{ state: "APPROVED" }, { state: "CHANGES_REQUESTED" }]),
    "changes_requested",
  );
  assert.equal(
    reviewDecisionFromReviews([{ state: "CHANGES_REQUESTED" }, { state: "DISMISSED" }]),
    "changes_requested",
  );
  assert.equal(reviewDecisionFromReviews([{ state: "DISMISSED" }]), "review_required");
});

test("fetchJson sends ETag + token and normalizes exhausted 403 to 429", async () => {
  const seen = {};
  const fake = async (url, { headers: h }) => {
    seen.url = url;
    seen.headers = h;
    return {
      status: 403,
      headers: headers({ "x-ratelimit-remaining": "0" }),
      json: async () => ({ message: "rate limited" }),
    };
  };
  const res = await fetchJson(fake, "https://example.invalid/x", {
    token: "tok",
    etag: "e1",
  });
  assert.equal(res.status, 429);
  assert.equal(seen.headers["If-None-Match"], "e1");
  assert.equal(seen.headers.Authorization, "Bearer tok");
});

test("fetchAllPages follows Link and reports 304 for an ETag hit", async () => {
  const p1 = async () => ({
    status: 200,
    headers: headers({ link: '<https://example.invalid/p2>; rel="next"', etag: "e2" }),
    json: async () => [{ id: 1 }],
  });
  const p2 = async () => ({
    status: 200,
    headers: headers({}),
    json: async () => [{ id: 2 }],
  });
  const fake = async (url) => (url.includes("p2") ? p2() : p1());
  const out = await fetchAllPages(fake, "https://example.invalid/p1", {});
  assert.deepEqual(out.items, [{ id: 1 }, { id: 2 }]);
  assert.deepEqual(out.pages, { fetched: 2, total: null });

  const cached = async () => ({
    status: 304,
    headers: headers({}),
    json: async () => {
      throw new Error("no body on 304");
    },
  });
  const hit = await fetchAllPages(cached, "https://example.invalid/p1", { etag: "e1" });
  assert.equal(hit.notModified, true);
});

test("snapshotFromRest builds a core-valid snapshot", () => {
  const snap = snapshotFromRest({
    repository: "org/repo",
    pr: prBody(),
    issueComments: [
      {
        id: 11, user: { login: "owner-x", type: "User" },
        created_at: ISO, updated_at: ISO, body: "ack",
      },
    ],
    reviewComments: [
      {
        id: 22, user: { login: "greptile[bot]", type: "Bot" },
        created_at: ISO, updated_at: ISO, body: "score 4/5",
      },
    ],
    reviews: [{ state: "COMMENTED" }],
    checkRuns: [{ status: "completed", conclusion: "success" }],
    ourLogins: ["owner-x"],
    nowMs: T0,
  });
  const { snapshot } = normalizeSnapshot(snap);
  assert.equal(snapshot.ci.rollup, "green");
  assert.equal(snapshot.threads.length, 1);
  assert.equal(snapshot.threads[0].resolved, false);
  assert.equal(snapshot.comments.length, 1);
  assert.equal(snapshot.comments[0].authorKind, "human");
  assert.equal(snapshot.lastOurResponseAtMs, Date.parse(ISO));
  assert.equal(snapshot.lastMaintainerAtMs, Date.parse(ISO));
});

function routeFetch(routes) {
  return async (url) => {
    for (const [match, res] of routes) {
      if (url.includes(match)) {
        return {
          status: res.status ?? 200,
          headers: headers(res.headers ?? {}),
          json: async () => res.body,
        };
      }
    }
    throw new Error(`unexpected url ${url}`);
  };
}

const TRACK = {
  repository: "org/repo",
  number: 7,
  issueId: "issue-7",
  identifier: "DEMO-7",
  cardOpen: true,
};

function detailRoutes(prOver = {}, reviewBody = "looks good") {
  return [
    ["/pulls/7/comments", { body: [{ id: 22, user: { login: "greptile[bot]", type: "Bot" }, created_at: ISO, updated_at: ISO, body: reviewBody }] }],
    ["/pulls/7/reviews", { body: [] }],
    ["/pulls/7", { body: prBody(prOver) }],
    ["/issues/7/comments", { body: [] }],
    ["/check-runs", { body: { check_runs: [{ status: "completed", conclusion: "success" }] } }],
    ["/pulls?state=open", { body: [{ number: 7, head: { sha: HEAD_A }, updated_at: ISO }] }],
  ];
}

test("collector emits a fresh valid read, then refreshes check-runs on unchanged head", async () => {
  const collect = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const scope = { upstream: [], org: ["org/repo"] };
  const etags = {};
  const first = await collect({ scope, etags, token: "tok" });
  assert.equal(first.length, 1);
  assert.equal(first[0].key, "org/repo#7");
  assert.equal(first[0].status, 200);
  normalizeSnapshot(first[0].input.next);

  // Quiet tick: same head, stale updated_at. The collector skips the 4
  // detail reads but still refreshes check-runs and re-runs decide input
  // with this tick's clock — never a bare 304.
  const prevAcks = { "org/repo#7": ackOf(first[0]) };
  const second = await collect({ scope, etags, token: "tok", prevAcks });
  assert.equal(second.length, 1);
  assert.equal(second[0].key, "org/repo#7");
  assert.equal(second[0].status, 200);
  assert.equal(second[0].refresh, true);
  assert.ok(second[0].input);
  assert.equal(second[0].input.next.headSha, HEAD_A);
  assert.equal(second[0].input.next.ci.rollup, "green");
  assert.ok(second[0].input.next.fetchedAtMs >= first[0].input.next.fetchedAtMs);
  normalizeSnapshot(second[0].input.next);
});

test("red CI on a quiet PR surfaces through the head-skip refresh", async () => {
  const green = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const scope = { upstream: [], org: ["org/repo"] };
  const etags = {};
  const first = await green({ scope, etags, token: "tok" });
  assert.equal(first[0].input.next.ci.rollup, "green");

  const redRoutes = detailRoutes();
  const redIdx = redRoutes.findIndex(([m]) => m === "/check-runs");
  redRoutes[redIdx] = ["/check-runs", {
    body: {
      check_runs: [
        { status: "completed", conclusion: "failure", started_at: new Date(Date.now() - 60_000).toISOString() },
      ],
    },
  }];
  const red = createRestCollector({
    fetchImpl: routeFetch(redRoutes),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const prevAcks = { "org/repo#7": ackOf(first[0]) };
  const second = await red({ scope, etags: {}, token: "tok", prevAcks });
  assert.equal(second[0].status, 200);
  assert.equal(second[0].input.next.ci.rollup, "red");
});

test("quiet PR past the response SLA wakes once via the list-304 refresh, then stays silent", async () => {
  const realNow = Date.now;
  let now = T0;
  Date.now = () => now;
  try {
    const listEtag = '"quiet-list-1"';
    const counts = { list200: 0, list304: 0 };
    const detail = routeFetch([
      ["/pulls/7/comments", { body: [{ id: 31, user: { login: "reviewer-y", type: "User" }, created_at: new Date(T0).toISOString(), updated_at: new Date(T0).toISOString(), body: "Please split this PR." }] }],
      ["/pulls/7/reviews", { body: [] }],
      ["/pulls/7", { body: prBody() }],
      ["/issues/7/comments", { body: [] }],
      ["/check-runs", { body: { check_runs: [{ status: "completed", conclusion: "success" }] } }],
    ]);
    const fetchImpl = async (url, init) => {
      if (!url.includes("/pulls?state=open")) return detail(url, init);
      if (init?.headers?.["If-None-Match"] === listEtag) {
        counts.list304 += 1;
        return { status: 304, headers: headers({ etag: listEtag }), json: async () => null };
      }
      counts.list200 += 1;
      return {
        status: 200,
        headers: headers({ etag: listEtag }),
        json: async () => [{ number: 7, head: { sha: HEAD_A }, updated_at: ISO }],
      };
    };
    const collect = createRestCollector({ fetchImpl, ourLogins: ["owner-x"], resolveTracking: () => TRACK });
    const scope = { upstream: [], org: ["org/repo"] };
    const etags = {};
    const acks = {};
    const digests = [];
    const deliver = async (decision) => {
      digests.push(decision);
      return { delivered: true };
    };
    const outcomes = [];
    const wakes = [];
    for (const hours of [1, 3, 5, 6]) {
      now = T0 + hours * HOUR_MS;
      const tick = await runPollTick({
        pluginEnabled: true,
        scope,
        etags,
        acks,
        collect: ({ scope: s, etags: e }) => collect({ scope: s, etags: e, token: "tok", prevAcks: acks }),
        deliver,
      });
      outcomes.push(tick.results.map((r) => r.outcome));
      wakes.push(tick.wakes);
      for (const r of tick.results) if (r.ack !== undefined) acks[r.key] = r.ack;
    }
    assert.deepEqual(outcomes, [["baseline"], ["silent"], ["digest"], ["silent"]]);
    assert.deepEqual(wakes, [0, 0, 1, 0]);
    assert.equal(counts.list200, 1);
    assert.equal(counts.list304, 3);
    assert.deepEqual(digests[0].changes.map((c) => c.kind), ["sla-response-breach"]);
  } finally {
    Date.now = realNow;
  }
});

test("quiet PR whose CI turns red wakes once through the list-304 refresh", async () => {
  const realNow = Date.now;
  let now = T0;
  Date.now = () => now;
  try {
    const listEtag = '"quiet-list-ci-1"';
    let checks = { etag: '"checks-green-1"', body: { check_runs: [{ status: "completed", conclusion: "success" }] } };
    const checkStatuses = [];
    const detail = routeFetch([
      ["/pulls/7/comments", { body: [] }],
      ["/pulls/7/reviews", { body: [] }],
      ["/pulls/7", { body: prBody() }],
      ["/issues/7/comments", { body: [] }],
    ]);
    const fetchImpl = async (url, init) => {
      if (url.includes("/check-runs")) {
        const unchanged = init?.headers?.["If-None-Match"] === checks.etag;
        checkStatuses.push(unchanged ? 304 : 200);
        if (unchanged) return { status: 304, headers: headers({ etag: checks.etag }), json: async () => null };
        return { status: 200, headers: headers({ etag: checks.etag }), json: async () => checks.body };
      }
      if (!url.includes("/pulls?state=open")) return detail(url, init);
      if (init?.headers?.["If-None-Match"] === listEtag) {
        return { status: 304, headers: headers({ etag: listEtag }), json: async () => null };
      }
      return {
        status: 200,
        headers: headers({ etag: listEtag }),
        json: async () => [{ number: 7, head: { sha: HEAD_A }, updated_at: ISO }],
      };
    };
    const collect = createRestCollector({ fetchImpl, ourLogins: ["owner-x"], resolveTracking: () => TRACK });
    const scope = { upstream: [], org: ["org/repo"] };
    const etags = {};
    const acks = {};
    const digests = [];
    const deliver = async (decision) => {
      digests.push(decision);
      return { delivered: true };
    };
    const outcomes = [];
    const wakes = [];
    for (const hours of [1, 2, 3]) {
      now = T0 + hours * HOUR_MS;
      if (hours === 2) {
        checks = {
          etag: '"checks-red-1"',
          body: { check_runs: [{ status: "completed", conclusion: "failure", started_at: new Date(now).toISOString() }] },
        };
      }
      const tick = await runPollTick({
        pluginEnabled: true,
        scope,
        etags,
        acks,
        collect: ({ scope: s, etags: e }) => collect({ scope: s, etags: e, token: "tok", prevAcks: acks }),
        deliver,
      });
      outcomes.push(tick.results.map((r) => r.outcome));
      wakes.push(tick.wakes);
      for (const r of tick.results) if (r.ack !== undefined) acks[r.key] = r.ack;
    }
    assert.deepEqual(outcomes, [["baseline"], ["digest"], ["silent"]]);
    assert.deepEqual(wakes, [0, 1, 0]);
    assert.deepEqual(digests[0].changes.map((c) => c.kind), ["ci"]);
    assert.equal(digests[0].changes[0].detail, "ci:green->red");
    assert.equal(checkStatuses.at(-1), 304);
  } finally {
    Date.now = realNow;
  }
});

test("list ETag is withheld when an item read fails", async () => {
  const routes = [
    ["/pulls/8", { status: 500, body: { message: "boom" } }],
    ["/pulls/7/comments", { body: [] }],
    ["/pulls/7/reviews", { body: [] }],
    ["/pulls/7", { body: prBody(), headers: {} }],
    ["/issues/7/comments", { body: [] }],
    ["/check-runs", { body: { check_runs: [] } }],
    ["/pulls?state=open", {
      body: [
        { number: 7, head: { sha: HEAD_A }, updated_at: ISO },
        { number: 8, head: { sha: HEAD_A }, updated_at: ISO },
      ],
      headers: { etag: "list-etag-1" },
    }],
  ];
  const collect = createRestCollector({
    fetchImpl: routeFetch(routes),
    resolveTracking: () => TRACK,
  });
  const etags = {};
  const out = await collect({ scope: { upstream: [], org: ["org/repo"] }, etags, token: "tok" });
  assert.equal(out.length, 2);
  assert.ok(out.some((r) => r.status === 200 && r.input));
  assert.ok(out.some((r) => r.status !== 200));
  assert.ok(!Object.values(etags).includes("list-etag-1"));
});

test("disappeared PR resolves to a closed read", async () => {
  const closedBody = { ...prBody(), state: "closed", merged: false, merged_at: new Date(T0).toISOString() };
  const routes = [
    ["/pulls/7/comments", { body: [] }],
    ["/pulls/7/reviews", { body: [] }],
    ["/pulls/7", { body: closedBody }],
    ["/issues/7/comments", { body: [] }],
    ["/check-runs", { body: { check_runs: [] } }],
    ["/pulls?state=open", { body: [] }],
  ];
  const collect = createRestCollector({
    fetchImpl: routeFetch(routes),
    resolveTracking: () => TRACK,
  });
  const seed = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const etags = {};
  const first = await seed({ scope: { upstream: [], org: ["org/repo"] }, etags, token: "tok" });
  const prevAcks = { "org/repo#7": ackOf(first[0]) };
  const out = await collect({
    scope: { upstream: [], org: ["org/repo"] },
    etags: {},
    token: "tok",
    prevAcks,
  });
  assert.equal(out.length, 1);
  assert.equal(out[0].status, 200);
  assert.equal(out[0].input.next.state, "closed");
  assert.equal(out[0].input.next.merged, true);
});

test("list 304 still refreshes known PRs for SLA clocks", async () => {
  const etags = {};
  const live = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const scope = { upstream: [], org: ["org/repo"] };
  const first = await live({ scope, etags, token: "tok" });
  // The fixture list carries no ETag header: seed the list URL so the next
  // tick takes the 304 path.
  const listUrl = "https://api.github.com/repos/org/repo/pulls?state=open&per_page=100";
  etags[listUrl] = "list-etag-seed";
  assert.ok(listUrl);

  const frozen = async () => ({
    status: 304,
    headers: headers({}),
    json: async () => {
      throw new Error("no body on 304");
    },
  });
  const cached = createRestCollector({
    fetchImpl: async (url) => {
      if (url.includes("/check-runs")) {
        return {
          status: 200,
          headers: headers({}),
          json: async () => ({ check_runs: [{ status: "completed", conclusion: "success" }] }),
        };
      }
      return frozen();
    },
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const prevAcks = { "org/repo#7": ackOf(first[0]) };
  const out = await cached({ scope, etags: { [listUrl]: etags[listUrl] }, token: "tok", prevAcks });
  assert.equal(out.length, 1);
  assert.equal(out[0].status, 200);
  assert.ok(out[0].input);
  normalizeSnapshot(out[0].input.next);
});

test("retired PRs (closed + closedSeen) cost zero requests", async () => {
  const live = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const scope = { upstream: [], org: ["org/repo"] };
  const first = await live({ scope, etags: {}, token: "tok" });
  const base = ackOf(first[0]);
  const retired = {
    ...base,
    closedSeen: true,
    snapshot: { ...base.snapshot, state: "closed", merged: true },
  };
  const prevAcks = { "org/repo#7": retired };

  // List 304: the retired PR emits no refresh.
  let fetches = 0;
  const counting = createRestCollector({
    fetchImpl: async (url) => {
      fetches += 1;
      if (url.includes("/pulls?state=open")) {
        return { status: 304, headers: headers({}), json: async () => { throw new Error("no body"); } };
      }
      throw new Error(`unexpected url ${url}`);
    },
    resolveTracking: () => TRACK,
  });
  const listUrl = "https://api.github.com/repos/org/repo/pulls?state=open&per_page=100";
  const silent = await counting({ scope, etags: { [listUrl]: "e1" }, token: "tok", prevAcks });
  assert.deepEqual(silent, []);
  assert.equal(fetches, 1);

  // Fresh empty list: no close-check for the retired PR either.
  const emptyList = createRestCollector({
    fetchImpl: async (url) => {
      fetches += 1;
      if (url.includes("/pulls?state=open")) {
        return { status: 200, headers: headers({}), json: async () => [] };
      }
      throw new Error(`unexpected url ${url}`);
    },
    resolveTracking: () => TRACK,
  });
  const out = await emptyList({ scope, etags: {}, token: "tok", prevAcks });
  assert.deepEqual(out, []);
  assert.equal(fetches, 2);
});

test("closed-but-unconverged PRs still resolve a close read", async () => {
  const live = createRestCollector({
    fetchImpl: routeFetch(detailRoutes()),
    ourLogins: ["owner-x"],
    resolveTracking: () => TRACK,
  });
  const scope = { upstream: [], org: ["org/repo"] };
  const first = await live({ scope, etags: {}, token: "tok" });
  const base = ackOf(first[0]);
  // Closed on the card, close digest not yet delivered: must still poll.
  const prevAcks = {
    "org/repo#7": { ...base, closedSeen: false, snapshot: { ...base.snapshot, state: "closed", merged: true } },
  };
  const closedBody = { ...prBody(), state: "closed", merged: false, merged_at: new Date(T0).toISOString() };
  const routes = [
    ["/pulls/7", { body: closedBody }],
    ["/pulls?state=open", { body: [] }],
  ];
  const collect = createRestCollector({
    fetchImpl: routeFetch(routes),
    resolveTracking: () => TRACK,
  });
  const out = await collect({ scope, etags: {}, token: "tok", prevAcks });
  assert.equal(out.length, 1);
  assert.equal(out[0].status, 200);
  assert.equal(out[0].input.next.state, "closed");
});

test("malformed detail maps to 422, never a crashing fresh read", async () => {
  const bad = detailRoutes({ head: {} });
  const collect = createRestCollector({
    fetchImpl: routeFetch(bad),
    resolveTracking: () => TRACK,
  });
  const out = await collect({ scope: { upstream: [], org: ["org/repo"] }, token: "tok" });
  assert.equal(out[0].status, 422);
});

test("upstream scope without an author resolves to a loud unknown", async () => {
  const collect = createRestCollector({
    fetchImpl: routeFetch([]),
    resolveTracking: () => TRACK,
  });
  const out = await collect({ scope: { upstream: ["other/repo"], org: [] }, token: "tok" });
  assert.equal(out.length, 1);
  assert.equal(out[0].key, "scope:other/repo");
  assert.equal(out[0].status, 0);
});
