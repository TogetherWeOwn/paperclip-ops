// Group 3: explicit unchecked consent enforced server-side.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp, ACCEPTED_MESSAGE } from "../src/worker.js";
import { loadJson, makeKv, makeRequest } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");

function post(body, headers = {}) {
  return makeRequest({
    method: "POST",
    host: beta.hostname,
    path: "/api/signup",
    headers: { origin: beta.allowedOrigins[0], ...headers },
    body,
  });
}

function postForm(fields, headers = {}) {
  const encoded = new URLSearchParams(fields).toString();
  return makeRequest({
    method: "POST",
    host: beta.hostname,
    path: "/api/signup",
    headers: { origin: beta.allowedOrigins[0], "content-type": "application/x-www-form-urlencoded", ...headers },
    body: encoded,
  });
}

function formFields(over = {}) {
  return { probeId: beta.probeId, email: "user@example.com", consent: "yes", ...over };
}
const good = (over = {}) => ({ probeId: beta.probeId, email: "user@example.com", consent: true, ...over });

describe("consent-first signup", () => {
  it("absent or false consent is rejected", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    for (const body of [good({ consent: false }), good({ consent: "yes" }), { probeId: beta.probeId, email: "u@example.com" }]) {
      const res = await app.fetch(post(body));
      assert.equal(res.status, 400, JSON.stringify(body));
    }
  });

  it("invalid and oversized emails are rejected", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    const bad = ["not-an-email", "a@b", "@x.com", "u@x", "a person-72e2f716@example.invalid", "", "x".repeat(250) + "@example.com"];
    for (const email of bad) {
      const res = await app.fetch(post(good({ email })));
      assert.equal(res.status, 400, email);
    }
  });

  it("unacceptable origin and cross-probe inputs are rejected", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    const evil = await app.fetch(post(good(), { origin: "https://evil.example" }));
    assert.equal(evil.status, 403);
    const none = await app.fetch(
      makeRequest({ method: "POST", host: beta.hostname, path: "/api/signup", body: good() }),
    );
    assert.equal(none.status, 403);
    const cross = await app.fetch(post(good({ probeId: "other-probe" })));
    assert.equal(cross.status, 400);
  });

  it("duplicate signup is idempotent with the identical message (no enumeration)", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const first = await (await app.fetch(post(good()))).json();
    const second = await (await app.fetch(post(good()))).json();
    assert.equal(first.message, ACCEPTED_MESSAGE);
    assert.equal(second.message, first.message);
    assert.ok(!ACCEPTED_MESSAGE.toLowerCase().includes("if your request was accepted"), "ack must be unconditional, post-persistence wording");
    // One stored record despite two posts.
    const keys = [...kv._map.keys()].filter((k) => k.startsWith(`signup:${beta.probeId}:`));
    assert.equal(keys.length, 1);
  });

  it("only email + minimum consent/probe metadata is persisted; no free text", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    await app.fetch(post({ ...good(), note: "please ignore this free text", name: "Mallory" }));
    const keys = [...kv._map.keys()];
    assert.equal(keys.length, 1);
    const rec = JSON.parse(kv._map.get(keys[0]));
    assert.deepEqual(Object.keys(rec).sort(), ["consentAt", "email", "noticeVersion", "origin", "probeHostname", "probeId"]);
    assert.ok(!("note" in rec) && !("name" in rec));
    assert.equal(rec.email, "user@example.com");
  });

  it("native form submit succeeds end-to-end (hidden probeId, checked consent)", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    // The rendered landing form must carry the probeId the server requires.
    const html = await (await app.fetch(makeRequest({ host: beta.hostname }))).text();
    assert.match(html, /<input type="hidden" name="probeId"/);
    const res = await app.fetch(postForm(formFields()));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).message, ACCEPTED_MESSAGE);
    assert.equal(await app.store.hasSignup(beta.probeId, "user@example.com"), true);
  });

  it("native form submit without consent (unchecked box) is rejected", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const { consent: _dropped, ...unchecked } = formFields();
    const res = await app.fetch(postForm(unchecked));
    assert.equal(res.status, 400);
    assert.equal(kv._map.size, 0);
  });

  it("rate limit trips with 429 and limiter state is never persisted", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    let saw429 = false;
    for (let i = 0; i < beta.limits.maxSignupsPerWindow + 5; i++) {
      const res = await app.fetch(
        makeRequest({
          method: "POST",
          host: beta.hostname,
          path: "/api/signup",
          headers: { origin: beta.allowedOrigins[0], "cf-connecting-ip": "203.0.113.9" },
          body: good({ email: `u${i}@example.com` }),
        }),
      );
      if (res.status === 429) saw429 = true;
    }
    assert.equal(saw429, true);
    // Signup records only — no limiter keys persisted.
    for (const k of kv._map.keys()) assert.ok(k.startsWith("signup:") || k.startsWith("views:"), k);
  });
});
