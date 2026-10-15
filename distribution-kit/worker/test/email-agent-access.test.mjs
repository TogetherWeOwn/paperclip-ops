// Group inbox-3: authorized narrow agent read works on fixtures;
// public/unauthorized list/read/export is rejected; no new credential or
// public mailbox endpoint is introduced.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { loadJson, makeKv, makeRequest, makeEmailMessage, rawSimple } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");

describe("inbox authorized agent access", () => {
  it("authorized store read returns the controlled stored fixture", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawSimple({ messageId: "<person-4231d903@example.invalid>", text: "agent readable" });
    const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(res.accepted, true);
    // Narrow exact-key read (the documented agent path: exact probe + id via
    // already-authorized KV/control-plane access, never HTTP).
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.ok(rec);
    assert.equal(rec.probeId, beta.probeId);
    assert.ok(rec.text.includes("agent readable"));
    const ids = await app.store.listInboxIds(beta.probeId, 10);
    assert.ok(ids.includes(res.dedupeId));
  });

  it("no public HTTP inbox route exists (list/read/export all 404)", async () => {
    const app = createApp({ rawConfig: beta, kv: makeKv() });
    for (const path of ["/inbox", "/api/inbox", "/api/replies", "/api/inbox/list", "/api/inbox/export", "/admin/inbox", "/export"]) {
      const res = await app.fetch(makeRequest({ host: beta.hostname, path }));
      assert.equal(res.status, 404, path);
    }
  });

  it("unauthorized cross-probe read returns null (no broad access)", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawSimple({ messageId: "<person-4c91d35c@example.invalid>" });
    const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: beta.inbox.address, raw }));
    assert.equal(await app.store.getInbox("other-probe", res.dedupeId), null);
    assert.equal(await app.store.getInbox(beta.probeId, "mid-00000000000000000000000000000000"), null);
    assert.deepEqual(await app.store.listInboxIds("other-probe"), []);
  });

  it("inbox listing is bounded (never unbounded export)", async () => {
    const kv = makeKv();
    const tiny = JSON.parse(JSON.stringify(beta));
    tiny.inbox.maxRecords = 5;
    const app = createApp({ rawConfig: tiny, kv });
    for (let i = 0; i < 5; i++) {
      const raw = rawSimple({ messageId: `<bulk${i}@fixtures.example>`, text: `m${i}` });
      const r = await app.email(makeEmailMessage({ from: `u${i}@fixtures.example`, to: tiny.inbox.address, raw }));
      assert.equal(r.accepted, true);
    }
    const all = await app.store.listInboxIds(tiny.probeId, 1000);
    assert.ok(all.length <= 5);
    const one = await app.store.listInboxIds(tiny.probeId, 2);
    assert.equal(one.length, 2);
  });

  it("inbox-full refuses with rejection and stores nothing new", async () => {
    const kv = makeKv();
    const tiny = JSON.parse(JSON.stringify(beta));
    tiny.inbox.maxRecords = 1;
    const app = createApp({ rawConfig: tiny, kv });
    const first = await app.email(
      makeEmailMessage({ from: "person-d7a63662@example.invalid", to: tiny.inbox.address, raw: rawSimple({ messageId: "<person-a347312b@example.invalid>" }) }),
    );
    assert.equal(first.accepted, true);
    const second = await app.email(
      makeEmailMessage({ from: "person-a1555156@example.invalid", to: tiny.inbox.address, raw: rawSimple({ messageId: "<person-eaea8017@example.invalid>" }) }),
    );
    assert.equal(second.accepted, false);
    assert.equal(second.rejected, "full");
    assert.equal((await app.store.listInboxIds(tiny.probeId)).length, 1);
  });
});
