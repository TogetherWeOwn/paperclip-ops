// Group 5: atomic aggregate per-day/per-probe page views, nothing per-visitor.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createApp } from "../src/worker.js";
import { utcDay } from "../src/storage.js";
import { loadJson, makeKv, makeRequest } from "./helpers.mjs";

const alpha = loadJson("worker/probe.config.json");
const beta = loadJson("fixtures/probe-beta.config.json");
const today = utcDay();

describe("aggregate page views", () => {
  it("landing hits increment a per-probe per-day counter", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    assert.equal(await app.store.viewCount(alpha.probeId, today), 0);
    await app.fetch(makeRequest({ host: alpha.hostname }));
    await app.fetch(makeRequest({ host: alpha.hostname }));
    assert.equal(await app.store.viewCount(alpha.probeId, today), 2);
    assert.equal(kv._map.get(`views:${alpha.probeId}:${today}`), "2");
  });

  it("counts are per-probe: fixtures do not share keys", async () => {
    const kv = makeKv();
    const appA = createApp({ rawConfig: alpha, kv });
    const appB = createApp({ rawConfig: beta, kv });
    await appA.fetch(makeRequest({ host: alpha.hostname }));
    assert.equal(await appB.store.viewCount(beta.probeId, today), 0);
    await appB.fetch(makeRequest({ host: beta.hostname }));
    assert.equal(await appA.store.viewCount(alpha.probeId, today), 1);
    assert.equal(await appB.store.viewCount(beta.probeId, today), 1);
  });

  it("health, privacy, changelog, assets, 404s and fixture traffic are excluded", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/health" }));
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/privacy" }));
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/changelog" }));
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/style.css" }));
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/nope" }));
    await app.fetch(makeRequest({ host: alpha.hostname, query: "?fixture_test=1" }));
    assert.equal(await app.store.viewCount(alpha.probeId, today), 0);
  });

  it("no request metadata is stored: keys are aggregates and signups only", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    await app.fetch(
      makeRequest({
        host: alpha.hostname,
        headers: { "user-agent": "TestBot/1.0", referer: "https://search.example/" },
      }),
    );
    for (const k of kv._map.keys()) {
      assert.match(k, /^(views|signup):/);
    }
    const v = kv._map.get(`views:${alpha.probeId}:${today}`);
    assert.equal(typeof v, "string");
    assert.ok(!JSON.stringify([...kv._map.values()]).includes("TestBot"), "no UA retained");
  });

  it("METRICS.md documents bot and reach uncertainty and the page-views label", () => {
    const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
    const md = readFileSync(join(root, "METRICS.md"), "utf8").toLowerCase();
    assert.ok(md.includes("page views"), "labels counts as page views");
    assert.ok(md.includes("bot"), "documents bot uncertainty");
    assert.ok(md.includes("not a person") || md.includes("never") && md.includes("unique"), "documents reach uncertainty");
  });
});
