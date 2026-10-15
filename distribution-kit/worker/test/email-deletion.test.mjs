// Group inbox-5: verified deletion, retention expiry, dedupe. Never
// auto-deletes on a spoofable From. Config separates disabled collection
// from handler readiness.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { collectionStatus, inboxStatus } from "../src/config.js";
import { loadJson, makeKv, makeRequest, makeEmailMessage, rawSimple } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");
const alpha = loadJson("worker/probe.config.json");

describe("inbox verified deletion and retention", () => {
  it("verified store deletion removes one reply; second delete reports absent", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const res = await app.email(
      makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw: rawSimple({ messageId: "<person-b45c97ae@example.invalid>" }) }),
    );
    assert.equal(await app.store.hasInbox(beta.probeId, res.dedupeId), true);
    // Verified path: exact (probeId, dedupeId) through the authorized store
    // call (see AGENT-INBOX.md) — never an email From.
    assert.equal(await app.store.deleteInbox(beta.probeId, res.dedupeId), true);
    assert.equal(await app.store.hasInbox(beta.probeId, res.dedupeId), false);
    assert.equal(await app.store.deleteInbox(beta.probeId, res.dedupeId), false);
  });

  it("a 'please delete me' email never deletes signup or inbox records", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    // Seed one consented signup.
    const signupRes = await app.fetch(
      makeRequest({
        method: "POST",
        host: beta.hostname,
        path: "/api/signup",
        headers: { origin: beta.allowedOrigins[0] },
        body: { probeId: beta.probeId, email: "person-c69f40b6@example.invalid", consent: true },
      }),
    );
    assert.equal(signupRes.status, 200);
    // Attacker spoofs the victim From and asks for deletion.
    const evil = await app.email(
      makeEmailMessage({
        from: "person-c69f40b6@example.invalid",
        to: beta.inbox.address,
        raw:
          `From: person-c69f40b6@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
          `Subject: delete me\r\nMessage-ID: <person-afd993d8@example.invalid>\r\n` +
          `Content-Type: text/plain\r\n\r\nplease DELETE person-c69f40b6@example.invalid\r\n`,
      }),
    );
    assert.equal(evil.accepted, true);
    // Signup survives; the request itself is stored as one inert inbox note.
    assert.equal(await app.store.hasSignup(beta.probeId, "person-c69f40b6@example.invalid"), true);
    assert.equal((await app.store.listInboxIds(beta.probeId)).length, 1);
  });

  it("inbox retention sweep removes stale replies, keeps fresh ones", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const now = Date.parse("2026-10-06T00:00:00Z");
    const oldAt = new Date(now - 100 * 24 * 3600 * 1000).toISOString();
    const freshAt = new Date(now - 2 * 24 * 3600 * 1000).toISOString();
    await app.store.saveInbox(
      { probeId: beta.probeId, dedupeId: "mid-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", to: beta.inbox.address, from: "person-182de2ad@example.invalid", subject: "old", text: "old", receivedAt: oldAt, noticeVersion: "v" },
      1000,
    );
    await app.store.saveInbox(
      { probeId: beta.probeId, dedupeId: "mid-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", to: beta.inbox.address, from: "person-c1639e2b@example.invalid", subject: "fresh", text: "fresh", receivedAt: freshAt, noticeVersion: "v" },
      1000,
    );
    const removed = await app.store.sweepExpiredInbox(beta.probeId, 90, now);
    assert.equal(removed, 1);
    assert.equal(await app.store.hasInbox(beta.probeId, "mid-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), false);
    assert.equal(await app.store.hasInbox(beta.probeId, "mid-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), true);
  });

  it("config separates disabled collection from handler readiness", () => {
    // Alpha seed: signup collection closed AND inbox disabled (both fail closed).
    assert.equal(collectionStatus(alpha).open, false);
    assert.equal(inboxStatus(alpha).ready, false);
    // Beta fixture: signup open AND inbox ready (independent true/true).
    assert.equal(collectionStatus(beta).open, true);
    assert.equal(inboxStatus(beta).ready, true);
    // Mixed: collection closed but inbox ready — handler still runs.
    const mixed = JSON.parse(JSON.stringify(beta));
    mixed.collectionEnabled = false;
    mixed.privacy.placeholder = true;
    assert.equal(collectionStatus(mixed).open, false);
    assert.equal(inboxStatus(mixed).ready, true);
    // Mixed: inbox disabled but collection open — signup still runs.
    const mixed2 = JSON.parse(JSON.stringify(beta));
    mixed2.inbox.enabled = false;
    assert.equal(collectionStatus(mixed2).open, true);
    assert.equal(inboxStatus(mixed2).ready, false);
  });

  it("no HTTP or email path deletes inbox records", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const res = await app.email(
      makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw: rawSimple({ messageId: "<person-8cfc5321@example.invalid>" }) }),
    );
    for (const path of ["/api/inbox/delete", "/api/delete", "/api/signup"]) {
      if (path === "/api/signup") continue;
      const r = await app.fetch(makeRequest({ host: beta.hostname, path }));
      assert.equal(r.status, 404, path);
    }
    assert.equal(await app.store.hasInbox(beta.probeId, res.dedupeId), true);
  });
});
