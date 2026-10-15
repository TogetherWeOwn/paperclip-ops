// Group inbox-7: review-findings repro (Paperclip Review 2/5 on 0e440608).
// Each test fails on the pre-fix code and passes after:
//  1. stream `raw` ingest (Cloudflare passes a ReadableStream, not a string)
//  2. nested multipart resolves to the text/plain leaf (no HTML leak)
//  3. transfer-encoded bodies and RFC 2047 subjects are decoded
//  4. record limits/sweeps/listings follow KV cursors past one page
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { loadJson, makeKv, makeEmailMessage, rawSimple } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");

function streamOf(text, chunkBytes = 7) {
  const bytes = new TextEncoder().encode(text);
  let off = 0;
  return new ReadableStream({
    pull(controller) {
      if (off >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(bytes.slice(off, off + chunkBytes));
      off += chunkBytes;
    },
  });
}

function rawNested({ text = "nested plain wins", html = '<img src="https://evil.example/pixel.png">' } = {}) {
  const outer = "OUTER456";
  const inner = "INNER789";
  const b64 = Buffer.from(text, "utf8").toString("base64");
  return (
    `From: person-9a475fda@example.invalid\r\n` +
    `To: ${beta.inbox.address}\r\n` +
    `Subject: nested check\r\n` +
    `Message-ID: <person-9929529d@example.invalid>\r\n` +
    `Content-Type: multipart/mixed; boundary="${outer}"\r\n` +
    `\r\n` +
    `--${outer}\r\n` +
    `Content-Type: multipart/alternative; boundary="${inner}"\r\n` +
    `\r\n` +
    `--${inner}\r\n` +
    `Content-Type: text/plain; charset=utf-8\r\n` +
    `Content-Transfer-Encoding: base64\r\n` +
    `\r\n` +
    `${b64}\r\n` +
    `--${inner}\r\n` +
    `Content-Type: text/html; charset=utf-8\r\n` +
    `\r\n` +
    `<html><body>${html}</body></html>\r\n` +
    `--${inner}--\r\n` +
    `--${outer}\r\n` +
    `Content-Type: application/octet-stream\r\n` +
    `Content-Disposition: attachment; filename="evil.bin"\r\n` +
    `\r\n` +
    `aGVsbG8=\r\n` +
    `--${outer}--\r\n`
  );
}

describe("review fixes: stream ingest, nested MIME, decoding, paging", () => {
  it("stream raw is accepted and stored like a string body", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawSimple({ messageId: "<person-5a8d4d00@example.invalid>", text: "stream body here" });
    const msg = makeEmailMessage({
      from: "person-9a475fda@example.invalid",
      to: beta.inbox.address,
      raw: streamOf(raw),
      rawSize: Buffer.byteLength(raw, "utf8"),
    });
    const res = await app.email(msg);
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.text, "stream body here");
  });

  it("Blob and byte-array raw are accepted", async () => {
    for (const [name, raw] of [
      ["blob", new Blob([rawSimple({ messageId: "<person-b32513dd@example.invalid>", text: "blob body" })])],
      ["bytes", new TextEncoder().encode(rawSimple({ messageId: "<person-763a48da@example.invalid>", text: "bytes body" }))],
    ]) {
      const kv = makeKv();
      const app = createApp({ rawConfig: beta, kv });
      const size = name === "blob" ? raw.size : raw.length;
      const msg = makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw, rawSize: size });
      const res = await app.email(msg);
      assert.equal(res.accepted, true, name);
    }
  });

  it("oversize stream is rejected mid-read and stores nothing", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const big = `Subject: big\r\nMessage-ID: <person-0623f146@example.invalid>\r\nContent-Type: text/plain\r\n\r\n${"z".repeat(beta.inbox.maxRawBytes + 100)}\r\n`;
    const msg = makeEmailMessage({
      from: "person-7b125b07@example.invalid",
      to: beta.inbox.address,
      raw: streamOf(big, 8192),
      rawSize: 1000, // envelope understates: actual bytes still enforced
    });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.equal(kv._map.size, 0);
  });

  it("non-string non-stream raw is malformed", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const msg = makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw: 42, rawSize: 2 });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.equal(res.rejected, "malformed");
  });

  it("nested multipart stores only the plain leaf: no HTML, img, or boundary lines", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawNested();
    const msg = makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw });
    const res = await app.email(msg);
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.text, "nested plain wins");
    assert.ok(!rec.text.includes("<"), "no HTML leaked");
    assert.ok(!rec.text.includes("OUTER456") && !rec.text.includes("INNER789"), "no boundary lines");
  });

  it("quoted-printable singlepart body is decoded", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw =
      `From: person-7b125b07@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
      `Subject: qp\r\nMessage-ID: <person-359fe4e4@example.invalid>\r\n` +
      `Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\ncaf=C3=A9=\r\nlatte\r\n`;
    const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.text, "cafélatte"); // =C3=A9 decoded, =\r\n soft break joined
  });

  it("RFC 2047 encoded subjects are decoded (B and Q)", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    for (const [mid, subject, want] of [
      ["<person-6e441400@example.invalid>", "=?UTF-8?B?Y2Fmw6k=?=", "café"],
      ["<person-845db35b@example.invalid>", "=?UTF-8?Q?hello_=E2=82=AC?=", "hello €"],
    ]) {
      const raw =
        `From: person-7b125b07@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
        `Subject: ${subject}\r\nMessage-ID: ${mid}\r\n` +
        `Content-Type: text/plain\r\n\r\nbody\r\n`;
      const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
      assert.equal(res.accepted, true);
      const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
      assert.equal(rec.subject, want, mid);
    }
  });

  it("over-deep nesting is rejected as malformed, never throws", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const levels = 30;
    let inner = `--B${levels + 1}\r\nContent-Type: text/plain\r\n\r\ndeep leaf\r\n--B${levels + 1}--\r\n`;
    for (let i = levels; i >= 1; i--) {
      inner = `--B${i}\r\nContent-Type: multipart/mixed; boundary="B${i + 1}"\r\n\r\n${inner}--B${i}--\r\n`;
    }
    const raw =
      `From: person-7b125b07@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
      `Subject: deep\r\nMessage-ID: <person-77040776@example.invalid>\r\n` +
      `Content-Type: multipart/mixed; boundary="B1"\r\n` +
      `\r\n` +
      inner;
    let res;
    let threw = false;
    try {
      res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    } catch {
      threw = true;
    }
    assert.equal(threw, false);
    assert.equal(res.accepted, false);
    assert.equal(res.rejected, "malformed");
    assert.equal(kv._map.size, 0);
  });

  it("HTML-only nested section does not hide a later plain sibling", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const outer = "OUTERHTML";
    const inner = "INNERHTML";
    const raw =
      `From: person-7b125b07@example.invalid\r\nTo: ${beta.inbox.address}\r\n` +
      `Subject: html first\r\nMessage-ID: <person-73109511@example.invalid>\r\n` +
      `Content-Type: multipart/mixed; boundary="${outer}"\r\n` +
      `\r\n` +
      `--${outer}\r\n` +
      `Content-Type: multipart/alternative; boundary="${inner}"\r\n` +
      `\r\n` +
      `--${inner}\r\n` +
      `Content-Type: text/html; charset=utf-8\r\n` +
      `\r\n` +
      `<html><body>html only here</body></html>\r\n` +
      `--${inner}--\r\n` +
      `--${outer}\r\n` +
      `Content-Type: text/plain; charset=utf-8\r\n` +
      `\r\n` +
      `later plain wins\r\n` +
      `--${outer}--\r\n`;
    const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(res.accepted, true);
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.text, "later plain wins");
  });

  it("record limit holds past one KV page", async () => {
    const kv = makeKv({ pageSize: 3 });
    const app = createApp({ rawConfig: beta, kv });
    for (let i = 0; i < 5; i++) {
      const r = await app.store.saveInbox(
        {
          probeId: beta.probeId,
          dedupeId: `mid-${String(i).padStart(32, "a")}`,
          to: beta.inbox.address,
          from: "person-7b125b07@example.invalid",
          subject: `s${i}`,
          text: `t${i}`,
          receivedAt: new Date().toISOString(),
          noticeVersion: "v1",
        },
        5,
      );
      assert.equal(r.stored, true, `record ${i}`);
    }
    const over = await app.store.saveInbox(
      {
        probeId: beta.probeId,
        dedupeId: `mid-${"b".repeat(32)}`,
        to: beta.inbox.address,
        from: "person-7b125b07@example.invalid",
        subject: "over",
        text: "over",
        receivedAt: new Date().toISOString(),
        noticeVersion: "v1",
      },
      5,
    );
    assert.equal(over.full, true);
    const ids = await app.store.listInboxIds(beta.probeId, 100);
    assert.equal(ids.length, 5);
  });

  it("retention sweep crosses page boundaries", async () => {
    const kv = makeKv({ pageSize: 2 });
    const app = createApp({ rawConfig: beta, kv });
    const old = new Date(Date.now() - 100 * 24 * 60 * 60 * 1000).toISOString();
    const fresh = new Date().toISOString();
    for (let i = 0; i < 5; i++) {
      await app.store.saveInbox(
        {
          probeId: beta.probeId,
          dedupeId: `mid-${String(i).padStart(32, "c")}`,
          to: beta.inbox.address,
          from: "person-7b125b07@example.invalid",
          subject: "s",
          text: "t",
          receivedAt: i < 3 ? old : fresh,
          noticeVersion: "v1",
        },
        100,
      );
    }
    const removed = await app.store.sweepExpiredInbox(beta.probeId, 90);
    assert.equal(removed, 3);
    assert.equal((await app.store.listInboxIds(beta.probeId, 100)).length, 2);
  });
});
