// Group 1: same build renders two fixture probes by changing one config file.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { validateConfig, checkHostname, DISALLOWED_DOMAINS } from "../src/config.js";
import { loadJson, makeKv, makeRequest, text } from "./helpers.mjs";

const alpha = loadJson("worker/probe.config.json");
const beta = loadJson("fixtures/probe-beta.config.json");

describe("config reuse: one build, two fixture probes", () => {
  it("both fixture configs validate clean", () => {
    assert.equal(validateConfig(alpha).ok, true);
    assert.equal(validateConfig(beta).ok, true);
  });

  it("same createApp build renders different titles per config", async () => {
    const appA = createApp({ rawConfig: alpha, kv: makeKv() });
    const appB = createApp({ rawConfig: beta, kv: makeKv() });
    const resA = await appA.fetch(makeRequest({ host: alpha.hostname }));
    const resB = await appB.fetch(makeRequest({ host: beta.hostname }));
    const a = await text(resA);
    const b = await text(resB);
    assert.match(a, /Small tools and early experiments/);
    assert.match(b, /Fixture Beta/);
    assert.ok(!a.includes("Fixture Beta"), "alpha must not leak beta content");
    assert.ok(!b.includes("We are testing ideas before deciding"), "beta must not leak alpha body");
  });

  it("wrong hostname is rejected without revealing config", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const res = await app.fetch(makeRequest({ host: "evil.example" }));
    assert.equal(res.status, 404);
    assert.ok(!(await text(res)).includes(alpha.probeId));
  });

  it("every disallowed domain and its subdomains fail validation", () => {
    for (const d of DISALLOWED_DOMAINS) {
      for (const host of [d, `probe.${d}`]) {
        const bad = { ...alpha, hostname: host };
        const r = validateConfig(bad);
        assert.equal(r.ok, false, host);
        assert.ok(r.errors.some((e) => e.includes("disallowed")), host);
        assert.equal(checkHostname(host, { ...alpha, hostname: "other.example" }), false);
      }
    }
  });

  it("malformed configs fail closed (500, no content served)", async () => {
    const app = createApp({ rawConfig: { nope: true }, kv: makeKv() });
    assert.ok(app.configErrors.length > 0);
    const res = await app.fetch(makeRequest({ host: "x.example" }));
    assert.equal(res.status, 500);
  });

  it("no dead view-limit field: unbounded aggregate counting is the design", () => {
    for (const c of [alpha, beta]) {
      assert.ok(!("maxViewsPerProbePerDay" in (c.limits || {})), "dead field removed");
    }
  });

  it("config carries content, paths, collection flag and privacy metadata", () => {
    for (const c of [alpha, beta]) {
      assert.ok(typeof c.content.title === "string");
      for (const p of ["privacy", "changelog", "health", "signup"]) {
        assert.ok(c.paths[p].startsWith("/"), p);
      }
      assert.equal(typeof c.collectionEnabled, "boolean");
      assert.ok(typeof c.privacy.noticeVersion === "string");
    }
  });
});
