// Group 2: accessible page, working privacy/changelog links, honest wording.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { loadJson, makeKv, makeRequest, text } from "./helpers.mjs";

const alpha = loadJson("worker/probe.config.json");

describe("honest landing page", () => {
  it("landing is accessible HTML with lang, main, label and live region", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const html = await text(await app.fetch(makeRequest({ host: alpha.hostname })));
    assert.match(html, /<html lang="en">/);
    assert.match(html, /<main>/);
    assert.match(html, /<meta name="viewport"/);
    assert.match(html, /coming soon|preview|not available|not open/i);
  });

  it("enabled fixture renders a labelled, unchecked consent form", async () => {
    const beta = loadJson("fixtures/probe-beta.config.json");
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    const html = await text(await app.fetch(makeRequest({ host: beta.hostname })));
    assert.match(html, /<form[^>]*action="\/api\/signup"/);
    assert.match(html, /<label for="email">/);
    assert.match(html, /type="checkbox"[^>]*name="consent"/);
    assert.ok(!/checked/.test(html.split("consent")[1].split(">")[0]), "consent must be unchecked by default");
    assert.match(html, /Join the interest list/);
    assert.match(html, /not an available product|not a purchase/i);
  });

  it("privacy and changelog links resolve to real pages", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const landing = await text(await app.fetch(makeRequest({ host: alpha.hostname })));
    assert.ok(landing.includes('href="/privacy"'), "landing links privacy");
    assert.ok(landing.includes('href="/changelog"'), "landing links changelog");
    const privacy = await text(await app.fetch(makeRequest({ host: alpha.hostname, path: "/privacy" })));
    assert.match(privacy, /Privacy notice/);
    assert.match(privacy, /DRAFT/);
    const log = await text(await app.fetch(makeRequest({ host: alpha.hostname, path: "/changelog" })));
    assert.match(log, /Changelog/);
    assert.match(log, /0\.1\.0/);
  });

  it("no fake proof, checkout, tracking pixels, third-party analytics or promises", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    for (const path of ["/", "/privacy", "/changelog"]) {
      const html = await text(await app.fetch(makeRequest({ host: alpha.hostname, path })));
      const low = html.toLowerCase();
      assert.ok(!html.includes("<script"), `${path}: no scripts at all`);
      assert.ok(!html.includes("<img"), `${path}: no tracking pixels`);
      assert.ok(!low.includes("testimonial") && !low.includes("user count"), `${path}: no fake proof`);
      assert.ok(!low.includes("checkout") && !low.includes("buy now"), `${path}: no checkout`);
      assert.ok(!low.includes("launching on") && !low.includes("available on"), `${path}: no launch promises`);
      assert.ok(!low.includes("google-analytics") && !low.includes("segment.io"), `${path}: no third-party analytics`);
    }
  });

  it("health is liveness-only and unknown paths 404", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const kv = makeKv();
    const app2 = createApp({ rawConfig: alpha, kv });
    await app.fetch(makeRequest({ host: alpha.hostname, path: "/health" }));
    const health = await (await app.fetch(makeRequest({ host: alpha.hostname, path: "/health" }))).json();
    assert.equal(health.ok, true);
    assert.equal(health.probe, alpha.probeId);
    const nf = await app.fetch(makeRequest({ host: alpha.hostname, path: "/admin" }));
    assert.equal(nf.status, 404);
    // Health checks leave no view counts behind.
    assert.equal(await app2.store.viewCount(alpha.probeId, "2099-01-01"), 0);
  });
});
