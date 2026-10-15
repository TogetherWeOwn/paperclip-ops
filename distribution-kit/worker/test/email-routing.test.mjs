// Group inbox-1: exact-recipient routing; no catch-all; safe rejection.
// Bounds are enforced BEFORE parsing/storage.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { inboxStatus } from "../src/config.js";
import { isExactRecipient } from "../src/email.js";
import { loadJson, makeKv, makeEmailMessage, rawSimple } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");
const alpha = loadJson("worker/probe.config.json");

describe("inbox exact-recipient routing", () => {
  it("routes only the exact configured address; no catch-all", () => {
    const addr = beta.inbox.address;
    assert.equal(isExactRecipient(addr, addr), true);
    assert.equal(isExactRecipient(addr.toUpperCase(), addr), true);
    assert.equal(isExactRecipient("person-72c85168@example.invalid", addr), false);
    assert.equal(isExactRecipient("", addr), false);
    assert.equal(isExactRecipient(addr, ""), false);
    assert.equal(isExactRecipient("person-7c3c7e62@example.invalid", addr), false);
  });

  it("wrong recipient is rejected with no storage", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawSimple();
    const msg = makeEmailMessage({ from: "person-9a475fda@example.invalid", to: "person-b2371a65@example.invalid", raw });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.equal(res.rejected, "unknown-recipient");
    assert.ok(msg._rejects.length === 1);
    assert.equal(kv._map.size, 0);
  });

  it("oversize envelope is rejected before parsing/storage", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const raw = rawSimple({ text: "x".repeat(100) });
    const msg = makeEmailMessage({
      from: "person-9a475fda@example.invalid",
      to: beta.inbox.address,
      raw,
      rawSize: beta.inbox.maxRawBytes + 1,
    });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.equal(res.rejected, "oversize");
    assert.equal(kv._map.size, 0);
  });

  it("oversize raw body is rejected even when envelope size lies", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    const big = "y".repeat(beta.inbox.maxRawBytes + 10);
    const msg = makeEmailMessage({
      from: "person-9a475fda@example.invalid",
      to: beta.inbox.address,
      raw: big,
      rawSize: 100,
    });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.ok(["oversize", "malformed"].includes(res.rejected));
    assert.equal(kv._map.size, 0);
  });

  it("malformed MIME (empty/non-string) is safely rejected", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    for (const bad of [
      makeEmailMessage({ from: "person-9a475fda@example.invalid", to: beta.inbox.address, raw: "", rawSize: 10 }),
      makeEmailMessage({ from: "person-9a475fda@example.invalid", to: beta.inbox.address, raw: null, rawSize: 10 }),
      makeEmailMessage({ from: "not-an-email", to: beta.inbox.address, raw: rawSimple(), rawSize: 200 }),
      makeEmailMessage({ from: "", to: beta.inbox.address, raw: rawSimple(), rawSize: 200 }),
    ]) {
      const res = await app.email(bad);
      assert.equal(res.accepted, false, JSON.stringify(res));
    }
    assert.equal(kv._map.size, 0);
  });

  it("disabled inbox (alpha seed) rejects even a well-formed reply", async () => {
    assert.equal(inboxStatus(alpha).ready, false);
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    const msg = makeEmailMessage({
      from: "person-9a475fda@example.invalid",
      to: alpha.inbox.address,
      raw: rawSimple(),
    });
    const res = await app.email(msg);
    assert.equal(res.accepted, false);
    assert.equal(res.rejected, "disabled");
    assert.equal(kv._map.size, 0);
  });
});
