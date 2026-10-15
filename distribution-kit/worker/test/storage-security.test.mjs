// Group 6: bounded storage, tested deletion/retention, no public admin/export.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { loadJson, makeKv, makeRequest } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");

function post(email, over = {}) {
  return makeRequest({
    method: "POST",
    host: beta.hostname,
    path: "/api/signup",
    headers: { origin: beta.allowedOrigins[0] },
    body: { probeId: beta.probeId, email, consent: true, ...over },
  });
}

describe("storage bounds, deletion and surface", () => {
  it("deleteSignup removes one record; second delete reports absent", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    await app.fetch(post("gone@example.com"));
    assert.equal(await app.store.hasSignup(beta.probeId, "gone@example.com"), true);
    assert.equal(await app.store.deleteSignup(beta.probeId, "gone@example.com"), true);
    assert.equal(await app.store.hasSignup(beta.probeId, "gone@example.com"), false);
    assert.equal(await app.store.deleteSignup(beta.probeId, "gone@example.com"), false);
    assert.equal(await app.store.deleteSignup(beta.probeId, "never@example.com"), false);
  });

  it("sweepExpired removes stale records and keeps fresh ones", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const now = Date.parse("2026-10-03T00:00:00Z");
    const old = new Date(now - 100 * 24 * 3600 * 1000).toISOString();
    const fresh = new Date(now - 2 * 24 * 3600 * 1000).toISOString();
    await kv.put(
      `signup:${beta.probeId}:old@example.com`,
      JSON.stringify({ email: "old@example.com", probeId: beta.probeId, consentAt: old, noticeVersion: "v", origin: "o", probeHostname: "h" }),
    );
    await kv.put(
      `signup:${beta.probeId}:fresh@example.com`,
      JSON.stringify({ email: "fresh@example.com", probeId: beta.probeId, consentAt: fresh, noticeVersion: "v", origin: "o", probeHostname: "h" }),
    );
    const removed = await app.store.sweepExpired(beta.probeId, 90, now);
    assert.equal(removed, 1);
    assert.equal(await app.store.hasSignup(beta.probeId, "old@example.com"), false);
    assert.equal(await app.store.hasSignup(beta.probeId, "fresh@example.com"), true);
  });

  it("signup storage is bounded: full list refuses with 503 and stores nothing new", async () => {
    const tiny = JSON.parse(JSON.stringify(beta));
    tiny.limits.maxSignups = 2;
    const kv = makeKv();
    const app = createApp({ rawConfig: tiny, kv });
    assert.equal((await app.fetch(post("a1@example.com"))).status, 200);
    assert.equal((await app.fetch(post("a2@example.com"))).status, 200);
    const full = await app.fetch(post("a3@example.com"));
    assert.equal(full.status, 503);
    assert.equal(await app.store.hasSignup(tiny.probeId, "a3@example.com"), false);
  });

  it("no public admin/export endpoint exists", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    for (const path of ["/admin", "/export", "/api/signups", "/api/views", "/api/delete", "/metrics"]) {
      const res = await app.fetch(makeRequest({ host: beta.hostname, path }));
      assert.equal(res.status, 404, path);
    }
  });

  it("signup endpoint never reflects input: no XSS echo, no address echo", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const evil = '"><script>alert(1)</script>@example.com';
    const res = await app.fetch(post(evil));
    const body = await res.text();
    assert.ok(!body.includes("<script>"), "no reflected markup");
    assert.ok(!body.includes(evil), "no address echo");
  });

  it("GET on the signup route is 405 and non-JSON bodies are 400", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    const get = await app.fetch(makeRequest({ host: beta.hostname, path: "/api/signup" }));
    assert.equal(get.status, 405);
    const bad = await app.fetch(
      makeRequest({
        method: "POST",
        host: beta.hostname,
        path: "/api/signup",
        headers: { origin: beta.allowedOrigins[0] },
        body: "this is not json{",
      }),
    );
    assert.equal(bad.status, 400);
  });
});
