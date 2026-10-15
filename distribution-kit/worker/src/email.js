// Bounded inbound-reply handler for the probe Worker (Cloudflare Email Routing).
//
// This module implements ONLY the `email()` path. It reuses the merged
// foundation contract (config shape, KV adapter) and does not re-create it.
// Design constraints (from case_b45a055a + copy-review adjudication):
// - Exact configured recipient per probe; no catch-all. Wrong recipient,
//   oversize and malformed MIME are rejected BEFORE parsing/storage.
// - Minimal subject/plain-text + sender stored; no HTML, no attachments.
// - Idempotent retried deliveries via content-stable dedupe IDs.
// - Stored mail is untrusted user data. It never becomes instructions,
//   tool calls, credential operations or publish/deletion instructions.
// - No auto-replies, forwards, or remote fetches. The handler never sends.
// - No raw email/body logs. Deletion is never driven by a spoofable From.
//
// Cloudflare shape (https://developers.cloudflare.com/email-routing/email-workers/):
//   export default { async email(message, env, ctx) { ... } }
// where message exposes { from, to, rawSize, raw, headers, setReject } and
// `raw` is a ReadableStream of the raw RFC-822 bytes. Local fixtures mimic
// that shape (string or stream) without network or production targets.

import { normalizeEmail } from "./storage.js";
import { inboxStatus } from "./config.js";

export { inboxStatus };

// Bounds caps: config values above these are rejected as misconfiguration.
export const MAX_RAW_BYTES_CAP = 1024 * 1024; // 1 MiB absolute ceiling
export const MAX_SUBJECT_CHARS_CAP = 500;
export const MAX_TEXT_CHARS_CAP = 10000;
export const MAX_INBOX_RECORDS_CAP = 100000;

export function normalizeRecipient(addr) {
  return String(addr || "").trim().toLowerCase();
}

// Exact-match routing. No catch-all, no wildcard, no subdomain expansion.
export function isExactRecipient(envelopeTo, configuredAddress) {
  return (
    normalizeRecipient(envelopeTo) === normalizeRecipient(configuredAddress) &&
    normalizeRecipient(configuredAddress) !== ""
  );
}

export function validateInboxConfig(inbox) {
  if (inbox === undefined || inbox === null) return { ok: true, errors: [] };
  if (typeof inbox !== "object" || Array.isArray(inbox)) {
    return { ok: false, errors: ["inbox must be an object"] };
  }
  if (inbox.enabled !== true && inbox.enabled !== false) {
    return { ok: false, errors: ["inbox.enabled must be boolean"] };
  }
  if (inbox.enabled === false) return { ok: true, errors: [] };
  const fake = { inbox };
  const st = inboxStatus(fake);
  return { ok: st.ready, errors: st.reasons };
}

async function sha256Hex(input) {
  const bytes = new TextEncoder().encode(String(input));
  // Works in Workers and in Node >= 18 (global Web Crypto).
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function validMessageId(value) {
  if (typeof value !== "string") return false;
  const v = value.trim();
  if (v.length === 0 || v.length > 256) return false;
  if (/\s/.test(v)) return false;
  if (v.includes("<") || v.includes(">")) {
    return /^<[^<>\s]{1,254}>$/.test(v);
  }
  return /^[^<>\s@]+@[^<>\s@]+\.[^<>\s@]+$/.test(v) || /^[A-Za-z0-9._-]{1,128}@[A-Za-z0-9.-]{1,128}$/.test(v);
}

// Stable dedupe ID. Prefers a bounded Message-ID when present; otherwise
// hashes the bounded raw payload. Both branches hash so the key never
// carries raw content and stays fixed length.
export async function dedupeIdFor({ messageId, raw }) {
  if (messageId && validMessageId(messageId)) {
    const h = await sha256Hex(`mid:${String(messageId).trim().toLowerCase()}`);
    return `mid-${h.slice(0, 32)}`;
  }
  const h = await sha256Hex(`raw:${String(raw || "")}`);
  return `raw-${h.slice(0, 32)}`;
}

// Bounded raw ingest. Cloudflare delivers `message.raw` as a ReadableStream;
// fixtures may also pass a string, Blob, or byte array. Returns
// { ok:true, text } with at most maxBytes of UTF-8 input, or { ok:false }.
// The actual byte count is enforced here even when the envelope rawSize lies.
export async function readRawBounded(raw, maxBytes) {
  if (typeof raw === "string") {
    if (raw.length === 0 || raw.length > maxBytes) return { ok: false };
    if (new TextEncoder().encode(raw).length > maxBytes) return { ok: false };
    return { ok: true, text: raw };
  }
  let bytes = null;
  if (raw instanceof ArrayBuffer) bytes = new Uint8Array(raw);
  else if (ArrayBuffer.isView(raw)) {
    bytes = new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength);
  } else if (raw && typeof raw.arrayBuffer === "function" && Number.isInteger(raw.size)) {
    if (raw.size <= 0 || raw.size > maxBytes) return { ok: false };
    try {
      bytes = new Uint8Array(await raw.arrayBuffer());
    } catch {
      return { ok: false };
    }
  } else if (raw && typeof raw.getReader === "function") {
    bytes = await readStreamBounded(raw.getReader(), maxBytes);
    if (!bytes) return { ok: false };
  } else {
    return { ok: false };
  }
  if (!bytes || bytes.length === 0 || bytes.length > maxBytes) return { ok: false };
  return { ok: true, text: new TextDecoder("utf-8", { fatal: false }).decode(bytes) };
}

async function readStreamBounded(reader, maxBytes) {
  const chunks = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const bytes = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      total += bytes.length;
      if (total > maxBytes) {
        try {
          await reader.cancel();
        } catch {}
        return null;
      }
      chunks.push(bytes);
    }
  } catch {
    return null;
  } finally {
    try {
      reader.releaseLock && reader.releaseLock();
    } catch {}
  }
  if (total === 0) return null;
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Transfer-decode one leaf body to bytes. Unknown/7bit encodings pass the
// raw chars through; undecodable base64 returns null (drop, don't store).
function transferToBytes(bodyStr, encoding) {
  const enc = String(encoding || "").trim().toLowerCase();
  if (enc === "base64") {
    const clean = bodyStr.replace(/\s+/g, "");
    if (clean.length === 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(clean)) return null;
    try {
      const bin = atob(clean);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return bytes;
    } catch {
      return null;
    }
  }
  if (enc === "quoted-printable" || enc === "quotedprintable") {
    const bytes = [];
    for (let i = 0; i < bodyStr.length; i++) {
      if (bodyStr[i] !== "=") {
        const code = bodyStr.charCodeAt(i);
        bytes.push(code <= 0xff ? code : 0x3f);
        continue;
      }
      const hex = bodyStr.slice(i + 1, i + 3);
      if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
        bytes.push(parseInt(hex, 16));
        i += 2;
      } else if (hex[0] === "\r" && hex[1] === "\n") {
        i += 2; // soft line break: skip
      } else if (hex[0] === "\n") {
        i += 1; // lone-LF soft break: skip
      } else {
        bytes.push(0x3d); // lone "=" kept literally
      }
    }
    return new Uint8Array(bytes);
  }
  return new TextEncoder().encode(bodyStr);
}

function decodeBytes(bytes, charset) {
  const label = String(charset || "").trim();
  if (/^[A-Za-z0-9_-]{1,40}$/.test(label)) {
    try {
      return new TextDecoder(label, { fatal: false }).decode(bytes);
    } catch {}
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

function charsetOf(contentTypeRaw) {
  const m = String(contentTypeRaw || "").match(/charset="?([^";\s]+)"?/i);
  return m ? m[1].slice(0, 40) : "";
}

// Unfold RFC-822 headers into a lowercase-name Map (first occurrence wins,
// values capped). Shared by the top level and every MIME part.
function parseHeaderBlock(headerText) {
  const lines = String(headerText).split(/\r?\n/);
  const headers = new Map();
  let current = null;
  for (const line of lines) {
    if (/^[ \t]/.test(line) && current) {
      headers.set(current, `${headers.get(current)} ${line.trim()}`.slice(0, 2000));
      continue;
    }
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    const name = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (!name || value.length === 0) continue;
    if (!headers.has(name)) {
      headers.set(name, value.slice(0, 2000));
      current = name;
    }
  }
  return headers;
}

function splitPartHeadBody(part) {
  const blankCrlf = part.indexOf("\r\n\r\n");
  const blankLf = part.indexOf("\n\n");
  if (blankCrlf >= 0 && (blankLf < 0 || blankCrlf < blankLf)) {
    return { head: part.slice(0, blankCrlf), body: part.slice(blankCrlf + 4) };
  }
  if (blankLf >= 0) return { head: part.slice(0, blankLf), body: part.slice(blankLf + 2) };
  return { head: "", body: part };
}

function isAttachment(headers) {
  return String(headers.get("content-disposition") || "").toLowerCase().includes("attachment");
}

function isPlainText(headers) {
  return String(headers.get("content-type") || "text/plain").toLowerCase().includes("text/plain");
}

// Recursion cap: real mail nests a few levels; deeper nesting is treated
// as malformed instead of risking a stack overflow in the parser.
const MAX_MIME_DEPTH = 10;

// One leaf part -> decoded text, or null when it is not a storable leaf.
// Nested multiparts recurse; the boundary keeps its original case.
function extractLeafText(part, depth) {
  const { head, body } = splitPartHeadBody(part);
  if (!head) return null;
  const headers = parseHeaderBlock(head);
  const contentTypeRaw = headers.get("content-type") || "text/plain";
  const nested = contentTypeRaw.match(/boundary="?([^";\s]+)"?/i);
  if (nested && contentTypeRaw.toLowerCase().includes("multipart/")) {
    if (depth >= MAX_MIME_DEPTH) throw new Error("mime nesting too deep");
    const inner = walkParts(body, nested[1].slice(0, 70), depth + 1);
    // A nested section with no plain-text leaf is "not found here", not
    // "found empty": return null so the outer walk keeps scanning later
    // sibling parts instead of stopping with empty text.
    return inner === "" ? null : inner;
  }
  if (!isPlainText(headers) || isAttachment(headers)) return null;
  const bytes = transferToBytes(body, headers.get("content-transfer-encoding") || "");
  if (!bytes) return "";
  return decodeBytes(bytes, charsetOf(contentTypeRaw)).replace(/\r\n/g, "\n").trim();
}

// Walk top-level parts between delimiter lines. First qualifying leaf wins;
// HTML-only or attachments-only mail yields "". Never falls back to HTML.
// Throws on over-deep nesting; callers convert that to a malformed reject.
function walkParts(bodyText, boundary, depth = 0) {
  const delim = new RegExp(`^--${escapeRegExp(boundary)}(?:--)?[ \t]*$`, "gm");
  const marks = [];
  let m;
  for (;;) {
    m = delim.exec(bodyText);
    if (!m) break;
    marks.push({ index: m.index, len: m[0].length, close: /--[ \t]*$/.test(m[0]) });
    if (marks.length > 500) break;
  }
  if (marks.length === 0) return "";
  for (let i = 0; i < marks.length; i++) {
    if (marks[i].close) break; // epilogue follows the close delimiter
    const start = marks[i].index + marks[i].len;
    const end = i + 1 < marks.length ? marks[i + 1].index : bodyText.length;
    const found = extractLeafText(bodyText.slice(start, end), depth);
    if (found !== null) return found;
  }
  return "";
}

// RFC 2047 encoded-words in Subject (e.g. =?UTF-8?B?...?=). Unknown charsets
// or malformed words are left literal; never interpreted.
function decodeOneWord(charset, enc, data, literal) {
  let bytes = null;
  if (enc === "b" || enc === "B") {
    const clean = data.replace(/\s+/g, "");
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(clean)) return literal;
    try {
      const bin = atob(clean);
      bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    } catch {
      return literal;
    }
  } else {
    const out = [];
    for (let i = 0; i < data.length; i++) {
      const ch = data[i];
      if (ch === "_") {
        out.push(0x20);
        continue;
      }
      if (ch !== "=") {
        const code = data.charCodeAt(i);
        out.push(code <= 0xff ? code : 0x3f);
        continue;
      }
      const hex = data.slice(i + 1, i + 3);
      if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
        out.push(parseInt(hex, 16));
        i += 2;
      } else {
        out.push(0x3d);
      }
    }
    bytes = new Uint8Array(out);
  }
  return decodeBytes(bytes, charset);
}

export function decodeEncodedWords(value) {
  const input = String(value || "");
  if (!input.includes("=?")) return input;
  const re = /=\?([^?\s]{1,40})\?([bBqQ])\?([^?]*)\?=/g;
  let out = "";
  let last = 0;
  let count = 0;
  let m;
  for (;;) {
    m = re.exec(input);
    if (!m || count >= 50) break;
    count++;
    out += input.slice(last, m.index);
    out += decodeOneWord(m[1], m[2], m[3], m[0]);
    last = m.index + m[0].length;
  }
  return out + input.slice(last);
}

// Minimal bounded MIME extraction. Returns { ok, subject, text, messageId }
// or { ok:false, reason }. Never persists HTML or attachments; HTML-only
// mail yields empty text. Callers must bound raw BEFORE calling.
export function parseBoundedEmail(raw, { maxSubjectChars, maxTextChars }) {
  if (typeof raw !== "string" || raw.length === 0) return { ok: false, reason: "empty" };
  // Split headers/body on the first blank line (CRLF or LF).
  let headerText = raw;
  let bodyText = "";
  const crlf = raw.indexOf("\r\n\r\n");
  const lf = raw.indexOf("\n\n");
  let sep = -1;
  let sepLen = 0;
  if (crlf >= 0 && (lf < 0 || crlf < lf)) {
    sep = crlf;
    sepLen = 4;
  } else if (lf >= 0) {
    sep = lf;
    sepLen = 2;
  }
  if (sep >= 0) {
    headerText = raw.slice(0, sep);
    bodyText = raw.slice(sep + sepLen);
  } else {
    bodyText = "";
  }
  const headers = parseHeaderBlock(headerText);
  const subjectRaw = headers.get("subject") || "";
  const messageIdRaw = headers.get("message-id") || headers.get("messageid") || "";
  const contentTypeRaw = headers.get("content-type") || "text/plain";
  const contentType = contentTypeRaw.toLowerCase();
  // Subject is untrusted data: decode RFC 2047 words, strip line breaks,
  // trim + hard truncate, never interpreted.
  const subject = decodeEncodedWords(subjectRaw).replace(/[\r\n]+/g, " ").trim().slice(0, maxSubjectChars);
  let text = "";
  const boundaryMatch = contentTypeRaw.match(/boundary="?([^";\s]+)"?/i);
  if (boundaryMatch && contentType.includes("multipart/")) {
    // Recursive walk: nested multiparts resolve to their text/plain leaf;
    // HTML-only or attachments-only mail yields empty text, never HTML.
    // Over-deep nesting (or any parser throw) rejects as malformed rather
    // than escaping email() as an exception.
    try {
      text = walkParts(bodyText, boundaryMatch[1].slice(0, 70));
    } catch {
      return { ok: false, reason: "too-deep" };
    }
  } else if (contentType.includes("text/html")) {
    text = "";
  } else {
    const bytes = transferToBytes(bodyText, headers.get("content-transfer-encoding") || "");
    text = bytes ? decodeBytes(bytes, charsetOf(contentTypeRaw)).replace(/\r\n/g, "\n").trim() : "";
  }
  text = text.slice(0, maxTextChars);
  return { ok: true, subject, text, messageId: String(messageIdRaw || "").slice(0, 256) };
}

// Main entry used by worker.js `email()` and by isolated fixtures.
// Never sends, never fetches, never logs bodies. Returns a small status
// object with no email content inside (no reflection).
export async function handleEmail(message, { config, store }) {
  const inbox = config.inbox;
  const ready = inboxStatus(config);
  if (!ready.ready) {
    try {
      message.setReject && message.setReject("inbox disabled");
    } catch {}
    return { accepted: false, rejected: "disabled" };
  }
  // 1. Exact recipient gate (before any parse/storage).
  if (!isExactRecipient(message.to, inbox.address)) {
    try {
      message.setReject && message.setReject("unknown recipient");
    } catch {}
    return { accepted: false, rejected: "unknown-recipient" };
  }
  // 2. Size gate (before parsing).
  const rawSize = message.rawSize;
  if (!Number.isInteger(rawSize) || rawSize <= 0 || rawSize > inbox.maxRawBytes) {
    try {
      message.setReject && message.setReject("oversize");
    } catch {}
    return { accepted: false, rejected: "oversize" };
  }
  // Cloudflare delivers raw as a byte stream; fixtures may pass a string,
  // Blob, or byte array. Actual bytes are enforced here even if the
  // envelope rawSize understates them.
  const read = await readRawBounded(message.raw, inbox.maxRawBytes);
  if (!read.ok) {
    try {
      message.setReject && message.setReject("malformed");
    } catch {}
    return { accepted: false, rejected: "malformed" };
  }
  const raw = read.text;
  // 3. Sender gate: envelope From must be a bounded email. Stored as the
  // reply address only; it never authorizes deletion or instructions.
  const sender = normalizeEmail(message.from, 254);
  if (!sender.ok) {
    try {
      message.setReject && message.setReject("malformed");
    } catch {}
    return { accepted: false, rejected: "malformed-sender" };
  }
  // 4. Bounded parse (HTML/attachments dropped inside). A parser throw
  // (e.g. pathological nesting) rejects as malformed, never escapes email().
  let parsed;
  try {
    parsed = parseBoundedEmail(raw, {
      maxSubjectChars: inbox.maxSubjectChars,
      maxTextChars: inbox.maxTextChars,
    });
  } catch {
    parsed = { ok: false };
  }
  if (!parsed.ok) {
    try {
      message.setReject && message.setReject("malformed");
    } catch {}
    return { accepted: false, rejected: "malformed" };
  }
  // 5. Idempotent dedupe (retried deliveries reuse the same record).
  const dedupeId = await dedupeIdFor({ messageId: parsed.messageId, raw });
  if (await store.hasInbox(config.probeId, dedupeId)) {
    return { accepted: true, duplicate: true, dedupeId };
  }
  const saved = await store.saveInbox(
    {
      probeId: config.probeId,
      dedupeId,
      to: normalizeRecipient(message.to),
      from: sender.email,
      subject: parsed.subject,
      text: parsed.text,
      receivedAt: new Date().toISOString(),
      noticeVersion: config.privacy.noticeVersion,
    },
    inbox.maxRecords,
  );
  if (saved.duplicate) return { accepted: true, duplicate: true, dedupeId };
  if (saved.full) {
    try {
      message.setReject && message.setReject("inbox full");
    } catch {}
    return { accepted: false, rejected: "full" };
  }
  if (!saved.stored) {
    try {
      message.setReject && message.setReject("malformed");
    } catch {}
    return { accepted: false, rejected: "malformed" };
  }
  return { accepted: true, dedupeId };
}
