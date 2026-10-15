// Minimal storage adapter with bounded inputs, atomic aggregate views and a
// tested deletion/retention mechanism.
//
// Stored records (waitlist signups only):
//   signup:<probeId>:<normalizedEmail> -> { email, probeId, consentAt,
//       noticeVersion, origin, probeHostname }
// No free text, no IP, no user-agent, no referrer, no cookies, no fingerprints.
// Aggregate views: views:<probeId>:<yyyy-mm-dd> -> integer count.
// Stored replies (bounded inbox, separate keys):
//   inbox:<probeId>:<dedupeId> -> { probeId, to, from, subject, text,
//       receivedAt, noticeVersion, kind, untrusted }
// `text`/`subject` are user-provided untrusted data: bounded plain text only,
// never HTML, never attachments, never raw MIME. `kind` is always
// "user-provided-reply" so readers never confuse replies with consented
// signup records (which carry no free text at all).
//
// Backend contract: any object with async get(key), put(key, value) and
// list(prefix) + delete(key) (Cloudflare KV exposes all four). The adapter
// never stores request metadata — callers must not pass it in.

// Bounded email check shared by the adapter and the HTTP handler.
export function normalizeEmail(raw, maxLength) {
  if (typeof raw !== "string") return { ok: false };
  const email = raw.trim().toLowerCase();
  if (email.length === 0 || email.length > maxLength) return { ok: false };
  if (email.length > 254) return { ok: false };
  // Practical RFC-5322 subset: one @, dot in domain, no spaces, no controls.
  const EMAIL_RE = /^[^\s@\u007f]{1,64}@[^\s@\u007f]{1,253}\.[^\s@\u007f]{2,}$/;
  if (!EMAIL_RE.test(email)) return { ok: false };
  return { ok: true, email };
}

export function utcDay(date = new Date()) {
  return date.toISOString().slice(0, 10);
}

export function signupKey(probeId, normalizedEmail) {
  return `signup:${probeId}:${normalizedEmail}`;
}

export function viewKey(probeId, day) {
  return `views:${probeId}:${day}`;
}

export function inboxKey(probeId, dedupeId) {
  return `inbox:${probeId}:${dedupeId}`;
}

const DEDUPE_RE = /^(mid|raw)-[0-9a-f]{32}$/;
const PROBE_RE = /^[a-z0-9-]{1,64}$/;

function validInboxFields({ probeId, dedupeId, to, from, subject, text }) {
  if (typeof probeId !== "string" || !PROBE_RE.test(probeId)) return false;
  if (typeof dedupeId !== "string" || !DEDUPE_RE.test(dedupeId)) return false;
  if (typeof to !== "string" || to.length === 0 || to.length > 254) return false;
  if (typeof from !== "string" || from.length === 0 || from.length > 254) return false;
  if (typeof subject !== "string" || typeof text !== "string") return false;
  return true;
}

export function createStore(kv, limits) {
  const maxSignups = limits.maxSignups;

  // Cloudflare KV list() returns at most 1000 keys per page with a cursor.
  // Every scan here follows the cursor to the end; counts stop early once
  // they reach the caller's cap. MAX_LIST_PAGES bounds a pathological
  // never-complete cursor chain.
  const MAX_LIST_PAGES = 10000;

  async function listPage(prefix, cursor) {
    const args = { prefix, limit: 1000 };
    if (cursor !== undefined) args.cursor = cursor;
    const res = await kv.list(args);
    if (Array.isArray(res)) return { names: res, complete: true, cursor: undefined };
    const keys = res && res.keys ? res.keys.map((k) => k.name) : [];
    const complete = !res || res.list_complete !== false;
    const next = !complete && typeof res.cursor === "string" && res.cursor ? res.cursor : undefined;
    return { names: keys, complete: complete || next === undefined, cursor: next };
  }

  async function listAllKeys(prefix) {
    const out = [];
    let cursor;
    for (let page = 0; page < MAX_LIST_PAGES; page++) {
      const { names, complete, cursor: next } = await listPage(prefix, cursor);
      for (const n of names) out.push(n);
      if (complete) break;
      cursor = next;
    }
    return out;
  }

  async function countWithPrefix(prefix, cap) {
    const stopAt = Number.isInteger(cap) && cap > 0 ? cap : Number.MAX_SAFE_INTEGER;
    let n = 0;
    let cursor;
    for (let page = 0; page < MAX_LIST_PAGES; page++) {
      const { names, complete, cursor: next } = await listPage(prefix, cursor);
      n += names.length;
      if (n >= stopAt || complete) break;
      cursor = next;
    }
    return n;
  }

  return {
    // Idempotent signup: writing the same key twice keeps one record.
    // Returns { stored: true } on first write, { stored: false, duplicate: true }
    // when the normalized address is already present. Callers MUST return the
    // same acceptance message for both, so responses never enumerate addresses.
    async signup({ probeId, email, consentAt, noticeVersion, origin, probeHostname }) {
      const norm = normalizeEmail(email, limits.maxEmailLength);
      if (!norm.ok) return { stored: false, invalid: true };
      const key = signupKey(probeId, norm.email);
      const existing = await kv.get(key, "json").catch(() => null);
      if (existing) return { stored: false, duplicate: true };
      const current = await countWithPrefix(`signup:${probeId}:`, maxSignups);
      if (current >= maxSignups) return { stored: false, full: true };
      await kv.put(
        key,
        JSON.stringify({
          email: norm.email,
          probeId,
          consentAt,
          noticeVersion,
          origin,
          probeHostname,
        }),
      );
      return { stored: true };
    },

    async hasSignup(probeId, email) {
      const norm = normalizeEmail(email, limits.maxEmailLength);
      if (!norm.ok) return false;
      const v = await kv.get(signupKey(probeId, norm.email)).catch(() => null);
      return v !== null && v !== undefined;
    },

    // Atomic per-day/per-probe aggregate increment. KV read-modify-write is
    // the smallest atomic unit available; contention only undercounts within
    // a single burst and never stores per-visitor data. No request metadata
    // is recorded here by design.
    async recordView(probeId, day) {
      const key = viewKey(probeId, day);
      const raw = await kv.get(key).catch(() => null);
      const next = (raw === null || raw === undefined ? 0 : parseInt(raw, 10) || 0) + 1;
      await kv.put(key, String(next));
      return next;
    },

    async viewCount(probeId, day) {
      const raw = await kv.get(viewKey(probeId, day)).catch(() => null);
      return raw === null || raw === undefined ? 0 : parseInt(raw, 10) || 0;
    },

    // Verified deletion: removes one signup; returns true iff a record existed.
    async deleteSignup(probeId, email) {
      const norm = normalizeEmail(email, limits.maxEmailLength);
      if (!norm.ok) return false;
      const key = signupKey(probeId, norm.email);
      const existing = await kv.get(key).catch(() => null);
      if (existing === null || existing === undefined) return false;
      await kv.delete(key);
      return true;
    },

    // Retention sweep: deletes signup records older than retentionDays.
    // Returns the number of records removed. consentAt is ISO-8601 UTC.
    async sweepExpired(probeId, retentionDays, nowMs = Date.now()) {
      const cutoff = nowMs - retentionDays * 24 * 60 * 60 * 1000;
      const keys = await listAllKeys(`signup:${probeId}:`);
      let removed = 0;
      for (const key of keys) {
        const raw = await kv.get(key, "json").catch(() => null);
        const at = raw && typeof raw.consentAt === "string" ? Date.parse(raw.consentAt) : NaN;
        if (Number.isNaN(at) || at < cutoff) {
          await kv.delete(key);
          removed += 1;
        }
      }
      return removed;
    },

    // Bounded inbox write. Stores minimal plain-text reply only; never HTML,
    // attachments or raw MIME. Idempotent on (probeId, dedupeId): a second
    // write with the same IDs returns { duplicate:true } and stores nothing
    // new. Enforces per-probe maxRecords (passed as inboxMaxRecords).
    async saveInbox({ probeId, dedupeId, to, from, subject, text, receivedAt, noticeVersion }, inboxMaxRecords = 1000) {
      if (!validInboxFields({ probeId, dedupeId, to, from, subject, text })) {
        return { stored: false, invalid: true };
      }
      if (typeof receivedAt !== "string" || Number.isNaN(Date.parse(receivedAt))) {
        return { stored: false, invalid: true };
      }
      const key = inboxKey(probeId, dedupeId);
      const existing = await kv.get(key, "json").catch(() => null);
      if (existing) return { stored: false, duplicate: true };
      const current = await countWithPrefix(`inbox:${probeId}:`, inboxMaxRecords);
      if (current >= inboxMaxRecords) return { stored: false, full: true };
      await kv.put(
        key,
        JSON.stringify({
          probeId,
          to,
          from,
          subject,
          text,
          receivedAt,
          noticeVersion: typeof noticeVersion === "string" ? noticeVersion : "",
          kind: "user-provided-reply",
          untrusted: true,
        }),
      );
      return { stored: true };
    },

    async hasInbox(probeId, dedupeId) {
      if (typeof probeId !== "string" || typeof dedupeId !== "string") return false;
      if (!PROBE_RE.test(probeId) || !DEDUPE_RE.test(dedupeId)) return false;
      const v = await kv.get(inboxKey(probeId, dedupeId)).catch(() => null);
      return v !== null && v !== undefined;
    },

    // Narrow authorized read: exact (probeId, dedupeId) only. Wrong probe
    // returns null (per-probe separation). No listing, no export here.
    async getInbox(probeId, dedupeId) {
      if (typeof probeId !== "string" || typeof dedupeId !== "string") return null;
      if (!PROBE_RE.test(probeId) || !DEDUPE_RE.test(dedupeId)) return null;
      const v = await kv.get(inboxKey(probeId, dedupeId), "json").catch(() => null);
      return v === undefined ? null : v;
    },

    // Bounded ID listing for the authorized agent path (control-plane KV
    // read, never HTTP). Limit is capped to keep scans small.
    async listInboxIds(probeId, limit = 100) {
      if (typeof probeId !== "string" || !PROBE_RE.test(probeId)) return [];
      const n = Math.max(1, Math.min(Number.isInteger(limit) ? limit : 100, 1000));
      const keys = await listAllKeys(`inbox:${probeId}:`);
      return keys.slice(0, n).map((k) => k.slice(`inbox:${probeId}:`.length));
    },

    // Verified deletion: the ONLY inbox deletion path. Removes one reply;
    // returns true iff a record existed. Never called from email handling
    // on From alone — callers must verify through an authenticated channel
    // (see AGENT-INBOX.md) before invoking.
    async deleteInbox(probeId, dedupeId) {
      if (typeof probeId !== "string" || typeof dedupeId !== "string") return false;
      if (!PROBE_RE.test(probeId) || !DEDUPE_RE.test(dedupeId)) return false;
      const key = inboxKey(probeId, dedupeId);
      const existing = await kv.get(key).catch(() => null);
      if (existing === null || existing === undefined) return false;
      await kv.delete(key);
      return true;
    },

    // Inbox retention sweep: deletes replies older than retentionDays based
    // on receivedAt (ISO-8601 UTC). Returns the number removed.
    async sweepExpiredInbox(probeId, retentionDays, nowMs = Date.now()) {
      const cutoff = nowMs - retentionDays * 24 * 60 * 60 * 1000;
      const keys = await listAllKeys(`inbox:${probeId}:`);
      let removed = 0;
      for (const key of keys) {
        const raw = await kv.get(key, "json").catch(() => null);
        const at = raw && typeof raw.receivedAt === "string" ? Date.parse(raw.receivedAt) : NaN;
        if (Number.isNaN(at) || at < cutoff) {
          await kv.delete(key);
          removed += 1;
        }
      }
      return removed;
    },
  };
}
