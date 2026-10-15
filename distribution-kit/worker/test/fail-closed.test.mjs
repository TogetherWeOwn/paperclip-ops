// Group 4: fail closed — collection disabled unless the full gate holds.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { collectionStatus, SEED_DISABLED_BODY } from "../src/config.js";
import { loadJson, makeKv, makeRequest, text } from "./helpers.mjs";

const alpha = loadJson("worker/probe.config.json");
const beta = loadJson("fixtures/probe-beta.config.json");

function signupBody(over = {}) {
  return { probeId: "fixture-beta", email: "a@example.com", consent: true, ...over };
}

describe("fail-closed collection gate", () => {
  it("seed config ships collection disabled with the exact seed wording", () => {
    assert.equal(alpha.collectionEnabled, false);
    assert.equal(alpha.content.body, SEED_DISABLED_BODY);
    const st = collectionStatus(alpha);
    assert.equal(st.open, false);
    assert.ok(st.reasons.length > 0);
  });

  it("disabled seed rejects signups with 403 and stores nothing", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    const res = await app.fetch(
      makeRequest({
        method: "POST",
        host: alpha.hostname,
        path: "/api/signup",
        headers: { origin: alpha.allowedOrigins[0] },
        body: { probeId: alpha.probeId, email: "a@example.com", consent: true },
      }),
    );
    assert.equal(res.status, 403);
    assert.equal(kv._map.size, 0);
  });

  it("disabled landing says signup is not open and carries a DRAFT banner", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const html = await text(await app.fetch(makeRequest({ host: alpha.hostname })));
    assert.match(html, /Email signup is not open\./);
    assert.ok(!html.includes("not open yet"), "disabled copy must not hint availability");
    assert.match(html, /DRAFT/);
    assert.ok(!html.includes("<form"), "disabled page must not render a signup form");
  });

  it("placeholder notice can never enable collection, even with everything else set", () => {
    const almost = JSON.parse(JSON.stringify(beta));
    almost.privacy.placeholder = true;
    const st = collectionStatus(almost);
    assert.equal(st.open, false);
    assert.ok(st.reasons.some((r) => r.includes("placeholder")));
  });

  it("each missing gate item keeps collection closed", () => {
    const cases = [
      ["collectionEnabled", (c) => ((c.collectionEnabled = false), c)],
      ["controller", (c) => ((c.privacy.controller = ""), c)],
      ["contact", (c) => ((c.privacy.contact = ""), c)],
      ["country", (c) => ((c.privacy.country = ""), c)],
      ["approval", (c) => ((c.privacy.approval = false), c)],
    ];
    for (const [name, mutate] of cases) {
      const c = mutate(JSON.parse(JSON.stringify(beta)));
      assert.equal(collectionStatus(c).open, false, name);
    }
  });

  it("privacy page marks withdrawal pending and names no reply channel", async () => {
    const app = createApp({ rawConfig: alpha, kv: makeKv() });
    const html = await text(await app.fetch(makeRequest({ host: alpha.hostname, path: "/privacy" })));
    assert.match(html, /Withdrawal method pending/);
    assert.ok(!html.toLowerCase().includes("reply to any message"), "must not describe a channel that does not exist");
  });

  it("open-collection privacy page names the configured contact, never pending", async () => {
    assert.equal(collectionStatus(beta).open, true);
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    const html = await text(await app.fetch(makeRequest({ host: beta.hostname, path: "/privacy" })));
    assert.match(html, /To withdraw your consent, contact /);
    assert.ok(html.includes(beta.privacy.contact), "open path names the real configured contact");
    assert.ok(!html.includes("Withdrawal method pending"), "open path must not claim disabled");
    assert.ok(!html.toLowerCase().includes("reply to any message"), "must not describe a channel that does not exist");
  });

  it("fully-configured fixture opens collection and accepts a signup", async () => {
    assert.equal(collectionStatus(beta).open, true);
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const res = await app.fetch(
      makeRequest({
        method: "POST",
        host: beta.hostname,
        path: "/api/signup",
        headers: { origin: beta.allowedOrigins[0] },
        body: signupBody(),
      }),
    );
    assert.equal(res.status, 200);
  });
});
