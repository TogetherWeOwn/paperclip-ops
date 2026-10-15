// Group inbox-2: bounded minimal storage; no HTML/attachments; idempotent
// dedupe; per-probe separation; no raw email/body logs.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { loadJson, makeKv, makeEmailMessage, rawSimple, rawMultipart } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");
const alpha = loadJson("worker/probe.config.json");

function betaApp(kv) {
  return createApp({ rawConfig: beta, kv });
}

describe("inbox bounded minimal storage", () => {
  it("stores minimal subject/plain-text + sender; truncates to bounds", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const longSubject = "S".repeat(500);
    const longText = "T".repeat(5000);
    const raw =
      `From: person-9a475fda@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
      `Subject: ${longSubject}\r\nMessage-ID: <person-42ff770d@example.invalid>\r\n` +
      `Content-Type: text/plain; charset=utf-8\r\n\r\n${longText}\r\n`;
    const msg = makeEmailMessage({ from: "person-f8ef825f@example.invalid", to: beta.inbox.address, raw });
    const res = await app.email(msg);
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.ok(rec);
    assert.equal(rec.from, "person-9a475fda@example.invalid");
    assert.equal(rec.to, beta.inbox.address.toLowerCase());
    assert.ok(rec.subject.length <= beta.inbox.maxSubjectChars);
    assert.ok(rec.text.length <= beta.inbox.maxTextChars);
    assert.equal(rec.kind, "user-provided-reply");
    assert.equal(rec.untrusted, true);
    assert.deepEqual(Object.keys(rec).sort(), ["from", "kind", "noticeVersion", "probeId", "receivedAt", "subject", "text", "to", "untrusted"]);
  });

  it("multipart stores plain part only; HTML and attachments never persist", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const raw = rawMultipart({ text: "keep this plain", html: "<script>alert(1)</script>", attachment: "c2VjcmV0LWJ5dGVz" });
    const msg = makeEmailMessage({ from: "person-9a475fda@example.invalid", to: beta.inbox.address, raw });
    const res = await app.email(msg);
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.ok(rec.text.includes("keep this plain"));
    assert.ok(!rec.text.includes("<script>"));
    assert.ok(!rec.text.includes("c2VjcmV0LWJ5dGVz"));
    // Raw MIME is never stored verbatim.
    for (const v of kv._map.values()) {
      assert.ok(!String(v).includes("BOUNDARY123") || String(v).includes("keep this plain"));
    }
    const stored = [...kv._map.values()].join("\n");
    assert.ok(!stored.includes("<script>alert(1)</script>"));
    assert.ok(!stored.includes("c2VjcmV0LWJ5dGVz"));
  });

  it("HTML-only mail stores empty text (never executable HTML)", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const raw =
      `From: person-9a475fda@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
      `Subject: html only\r\nMessage-ID: <person-efd8ccce@example.invalid>\r\n` +
      `Content-Type: text/html; charset=utf-8\r\n\r\n<b>buy now</b>\r\n`;
    const msg = makeEmailMessage({ from: "person-9a475fda@example.invalid", to: beta.inbox.address, raw });
    const res = await app.email(msg);
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.text, "");
  });

  it("retried delivery with same Message-ID is idempotent (one record)", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const raw = rawSimple({ messageId: "<person-fc12d386@example.invalid>", text: "once" });
    const first = await app.email(makeEmailMessage({ from: "person-d7a63662@example.invalid", to: beta.inbox.address, raw }));
    const second = await app.email(makeEmailMessage({ from: "person-d7a63662@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(first.accepted, true);
    assert.equal(second.accepted, true);
    assert.equal(second.duplicate, true);
    assert.equal(first.dedupeId, second.dedupeId);
    const ids = await app.store.listInboxIds(beta.probeId);
    assert.equal(ids.length, 1);
  });

  it("same raw without Message-ID dedupes by content hash", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const raw = `From: person-328ab60f@example.invalid\r\nTo: ${beta.inbox.address}\r\nSubject: no id\r\nContent-Type: text/plain\r\n\r\nsame body\r\n`;
    const a = await app.email(makeEmailMessage({ from: "person-328ab60f@example.invalid", to: beta.inbox.address, raw }));
    const b = await app.email(makeEmailMessage({ from: "person-328ab60f@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(a.dedupeId, b.dedupeId);
    assert.equal(b.duplicate, true);
  });

  it("per-probe separation: beta mail never lands under alpha", async () => {
    const kv = makeKv();
    const appBeta = betaApp(kv);
    const appAlpha = createApp({ rawConfig: { ...alpha, inbox: { ...beta.inbox, enabled: true } }, kv });
    const raw = rawSimple({ messageId: "<person-7ecb18b3@example.invalid>" });
    const res = await appBeta.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(res.accepted, true);
    assert.equal(await appBeta.store.hasInbox(beta.probeId, res.dedupeId), true);
    assert.equal(await appAlpha.store.hasInbox(alpha.probeId, res.dedupeId), false);
    assert.equal(await appAlpha.store.getInbox(alpha.probeId, res.dedupeId), null);
    // Cross-probe read with correct id but wrong probe returns null.
    assert.equal(await appBeta.store.getInbox(alpha.probeId, res.dedupeId), null);
  });

  it("no raw email/body logs: handler returns status only, never content", async () => {
    const kv = makeKv();
    const app = betaApp(kv);
    const secret = "secret-body-xyz-123";
    const raw = rawSimple({ messageId: "<person-1db0899e@example.invalid>", text: secret, subject: "subj-secret-456" });
    const logged = [];
    const origLog = console.log;
    const origErr = console.error;
    console.log = (...a) => logged.push(a.join(" "));
    console.error = (...a) => logged.push(a.join(" "));
    try {
      const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
      assert.ok(!JSON.stringify(res).includes(secret));
      assert.ok(!JSON.stringify(res).includes("subj-secret-456"));
    } finally {
      console.log = origLog;
      console.error = origErr;
    }
    assert.ok(!logged.join("\n").includes(secret), "handler must not log bodies");
  });
});
