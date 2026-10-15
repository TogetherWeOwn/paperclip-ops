// Group inbox-6: local fixtures demonstrate receipt, isolation, read and
// deletion without production targets or sending to a stranger. All
// addresses/hostnames are fixtures.example (never resolve).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/worker.js";
import { collectionStatus, inboxStatus } from "../src/config.js";
import { loadJson, makeKv, makeEmailMessage, rawSimple } from "./helpers.mjs";

const beta = loadJson("fixtures/probe-beta.config.json");
const alpha = loadJson("worker/probe.config.json");

describe("inbox local fixtures end-to-end", () => {
  it("fixture-only domains: no production host or real recipient appears", () => {
    const dump = JSON.stringify({ beta, alpha });
    for (const banned of ["togetherweown.com", "two.gg", "kofra.app", "kofra.io", "nntune.com"]) {
      assert.ok(!dump.includes(banned), banned);
    }
    assert.ok(beta.inbox.address.endsWith("@inbox.fixtures.example"));
    assert.ok(alpha.inbox.address.endsWith("@inbox.fixtures.example"));
    assert.ok(beta.hostname.endsWith(".fixtures.example"));
  });

  it("receipt -> authorized read -> verified deletion on fixtures", async () => {
    const kv = makeKv();
    const app = createApp({ rawConfig: beta, kv });
    // Receipt.
    const res = await app.email(
      makeEmailMessage({ from: "person-5890d7d4@example.invalid", to: beta.inbox.address, raw: rawSimple({ messageId: "<person-b1d31656@example.invalid>", text: "hello beta" }) }),
    );
    assert.equal(res.accepted, true);
    // Authorized read (exact-key store path, no owner inbox/login).
    const rec = await app.store.getInbox(beta.probeId, res.dedupeId);
    assert.equal(rec.from, "person-5890d7d4@example.invalid");
    // Verified deletion (authenticated store call, never From-based).
    assert.equal(await app.store.deleteInbox(beta.probeId, res.dedupeId), true);
    assert.equal(await app.store.hasInbox(beta.probeId, res.dedupeId), false);
  });

  it("isolation: two fixture probes never share inbox records", async () => {
    const kv = makeKv();
    const appBeta = createApp({ rawConfig: beta, kv });
    const alphaEnabled = { ...alpha, inbox: { ...beta.inbox, enabled: true, address: "person-77ab55fb@example.invalid" } };
    const appAlpha = createApp({ rawConfig: alphaEnabled, kv });
    const rB = await appBeta.email(
      makeEmailMessage({ from: "person-45b8f04f@example.invalid", to: beta.inbox.address, raw: rawSimple({ messageId: "<person-6c973cfc@example.invalid>" }) }),
    );
    const rawA = rawSimple({ messageId: "<person-a94497fc@example.invalid>" }).replace(beta.inbox.address, alphaEnabled.inbox.address);
    const rA = await appAlpha.email(makeEmailMessage({ from: "person-45b8f04f@example.invalid", to: alphaEnabled.inbox.address, raw: rawA }));
    assert.equal(rB.accepted, true);
    assert.equal(rA.accepted, true);
    assert.notEqual(rA.dedupeId, rB.dedupeId);
    assert.equal((await appBeta.store.listInboxIds(beta.probeId)).length, 1);
    assert.equal((await appAlpha.store.listInboxIds(alphaEnabled.probeId)).length, 1);
  });

  it("runbook pointer: INBOX-RUNBOOK.md exists and names an isolated check", async () => {
    const { readFileSync, existsSync } = await import("node:fs");
    const { dirname, join } = await import("node:path");
    const { fileURLToPath } = await import("node:url");
    const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
    const p = join(root, "INBOX-RUNBOOK.md");
    assert.equal(existsSync(p), true, "INBOX-RUNBOOK.md must exist");
    const body = readFileSync(p, "utf8");
    assert.match(body, /isolated/i);
    assert.match(body, /fixtures\.example/);
  });

  it("agent doc pointer: AGENT-INBOX.md documents the narrow read path", async () => {
    const { readFileSync, existsSync } = await import("node:fs");
    const { dirname, join } = await import("node:path");
    const { fileURLToPath } = await import("node:url");
    const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
    const p = join(root, "AGENT-INBOX.md");
    assert.equal(existsSync(p), true, "AGENT-INBOX.md must exist");
    const body = readFileSync(p, "utf8");
    assert.match(body, /getInbox|kv:key get/i);
    assert.match(body, /never.*From|spoofable/i);
  });

  it("disabled seed stays safe: alpha rejects receipt and stores nothing", async () => {
    assert.equal(collectionStatus(alpha).open, false);
    assert.equal(inboxStatus(alpha).ready, false);
    const kv = makeKv();
    const app = createApp({ rawConfig: alpha, kv });
    const res = await app.email(makeEmailMessage({ from: "person-7b125b07@example.invalid", to: alpha.inbox.address, raw: rawSimple() }));
    assert.equal(res.accepted, false);
    assert.equal(kv._map.size, 0);
  });
});
