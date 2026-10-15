// Shared isolated-test harness: in-memory KV, fixture configs, request fakes.
// No network, no credentials, no Workers runtime needed.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

export function loadJson(rel) {
  return JSON.parse(readFileSync(join(root, rel), "utf8"));
}

// Minimal KV fake: string keys, string values, list({prefix}), delete.
// Mirrors the subset of the Cloudflare KV API the adapter uses, including
// real cursor pagination: list({ prefix, limit, cursor }) returns one page
// with { keys, list_complete, cursor }. pageSize simulates the 1000-key
// Cloudflare page; pass a small pageSize to prove callers follow cursors.
export function makeKv({ pageSize = 1000 } = {}) {
  const map = new Map();
  return {
    _map: map,
    async get(key, type) {
      const v = map.has(key) ? map.get(key) : null;
      if (v === null) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    async put(key, value) {
      map.set(key, typeof value === "string" ? value : String(value));
    },
    async delete(key) {
      map.delete(key);
    },
    async list({ prefix, limit, cursor }) {
      const all = [...map.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name }));
      const size = Math.max(1, Math.min(Number.isInteger(limit) ? limit : pageSize, pageSize));
      let start = 0;
      if (typeof cursor === "string" && cursor !== "") {
        const n = parseInt(cursor, 10);
        start = Number.isInteger(n) && n >= 0 ? n : 0;
      }
      const page = all.slice(start, start + size);
      const next = start + size;
      if (next >= all.length) return { keys: page, list_complete: true };
      return { keys: page, list_complete: false, cursor: String(next) };
    },
  };
}

// Minimal Request fake with the surface worker.js touches.
export function makeRequest({ method = "GET", host, path = "/", headers = {}, body = null, query = "" }) {
  const h = new Map(Object.entries({ host, ...headers }));
  return {
    method,
    url: `https://${host}${path}${query}`,
    headers: {
      get(name) {
        return h.has(name.toLowerCase()) || h.has(name) ? (h.get(name.toLowerCase()) ?? h.get(name)) : null;
      },
    },
    async json() {
      if (body === null || body === undefined) throw new Error("empty body");
      return typeof body === "string" ? JSON.parse(body) : body;
    },
    // Minimal FormData fake: parses urlencoded bodies like a native no-JS
    // form submit; plain objects are wrapped field-by-field.
    async formData() {
      if (body === null || body === undefined) throw new Error("empty body");
      if (typeof body === "string") {
        const params = new URLSearchParams(body);
        return { get: (k) => (params.has(k) ? params.get(k) : null) };
      }
      const entries = Object.entries(body);
      return { get: (k) => { const f = entries.find(([key]) => key === k); return f ? String(f[1]) : null; } };
    },
  };
}

export async function text(res) {
  return await res.text();
}

// Minimal EmailMessage fake with the surface email.js touches.
// No network, no sending, no production target. `raw` is a bounded string;
// `rawSize` is the envelope byte count checked BEFORE parsing.
export function makeEmailMessage({ from, to, raw, rawSize, headers = {} } = {}) {
  const rejects = [];
  const calls = { forward: 0, reply: 0, fetch: 0 };
  const body = typeof raw === "string" ? raw : "";
  const size = Number.isInteger(rawSize) ? rawSize : Buffer.byteLength(body, "utf8");
  return {
    from,
    to,
    raw: raw === undefined ? body : raw,
    rawSize: size,
    headers,
    _rejects: rejects,
    _calls: calls,
    setReject(reason) {
      rejects.push(String(reason || ""));
    },
    // Poisoned outbound surface: the handler must never call these.
    async forward() {
      calls.forward += 1;
      throw new Error("forward must never be called");
    },
    async reply() {
      calls.reply += 1;
      throw new Error("reply must never be called");
    },
  };
}

// Small bounded raw MIME builders for fixtures. All hostnames/addresses
// use fixtures.example (never resolves) and no stranger is contacted.
export function rawSimple({ subject = "Hello", messageId = "<person-ec43799a@example.invalid>", text = "Just testing." } = {}) {
  return (
    `From: person-9a475fda@example.invalid\r\n` +
    `To: person-ee6485ee@example.invalid\r\n` +
    `Subject: ${subject}\r\n` +
    `Message-ID: ${messageId}\r\n` +
    `Content-Type: text/plain; charset=utf-8\r\n` +
    `\r\n` +
    `${text}\r\n`
  );
}

export function rawMultipart({ text = "plain part", html = "<b>html part</b>", attachment = "aGVsbG8=" } = {}) {
  const b = "BOUNDARY123";
  return (
    `From: person-9a475fda@example.invalid\r\n` +
    `To: person-ee6485ee@example.invalid\r\n` +
    `Subject: multipart check\r\n` +
    `Message-ID: <person-c111f0b6@example.invalid>\r\n` +
    `Content-Type: multipart/mixed; boundary="${b}"\r\n` +
    `\r\n` +
    `--${b}\r\n` +
    `Content-Type: text/plain; charset=utf-8\r\n` +
    `\r\n` +
    `${text}\r\n` +
    `--${b}\r\n` +
    `Content-Type: text/html; charset=utf-8\r\n` +
    `\r\n` +
    `${html}\r\n` +
    `--${b}\r\n` +
    `Content-Type: application/octet-stream\r\n` +
    `Content-Disposition: attachment; filename="evil.bin"\r\n` +
    `\r\n` +
    `${attachment}\r\n` +
    `--${b}--\r\n`
  );
}
