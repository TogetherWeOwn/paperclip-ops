#!/usr/bin/env node
import { openSync, fstatSync, readSync, closeSync, constants, readFileSync, realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { parseFrozen, LIMITS } from './delivery-effectiveness.mjs';
import { evaluateAccessGaps, canonical, utc } from './access-gap.mjs';
import { collectAccessInputs } from './access-gap-collect.mjs';

function readJson(path) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > LIMITS.bytes) throw new Error('invalid_input_file');
    const bytes = Buffer.alloc(LIMITS.bytes + 1); let used = 0, n;
    while ((n = readSync(fd, bytes, used, bytes.length - used, null)) > 0) {
      used += n; if (used > LIMITS.bytes) throw new Error('input_byte_cap');
    }
    return parseFrozen(new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, used)));
  } finally { closeSync(fd); }
}
export function paperclipGet({ base, key, company, maxReads = 500, maxBytes = 16 * 1024 * 1024, fetchImpl = fetch }) {
  const url = new URL(base.replace(/\/$/, '').replace(/\/api$/, ''));
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('invalid_api_base');
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(company) || typeof key !== 'string' || !key) throw new Error('missing_api_identity');
  const escaped = company.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const allowed = new RegExp(`^/(?:companies/${escaped}/(?:agents|issues|heartbeat-runs)|issues/[A-Za-z0-9_-]+/interactions|heartbeat-runs/[A-Za-z0-9_-]+/log)(?:\\?[^#]*)?$`);
  let reads = 0, bytes = 0;
  const deadline = Date.now() + 120000;
  return async path => {
    if (!allowed.test(path) || /[\r\n]/.test(path)) throw new Error('route_refused');
    if (++reads > maxReads || Date.now() >= deadline) throw new Error('collection_budget');
    const response = await fetchImpl(`${url.origin}/api${path}`, { method: 'GET', redirect: 'error',
      signal: AbortSignal.timeout(Math.max(1, Math.min(15000, deadline - Date.now()))), headers: { Authorization: `Bearer ${key}` } });
    if (!response.ok) throw new Error(`http_${response.status}`);
    if (Number(response.headers.get('content-length') ?? 0) > LIMITS.bytes) { await response.body?.cancel(); throw new Error('response_byte_cap'); }
    const reader = response.body.getReader(), chunks = []; let used = 0;
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        used += value.length; bytes += value.length;
        if (used > LIMITS.bytes || bytes > maxBytes) throw new Error('response_byte_cap');
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    return parseFrozen(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
  };
}
export async function main(args) {
  let input;
  if (args.length === 2 && args[0] === '--from-file') input = readJson(args[1]);
  else if (args.length === 4 && args[0] === '--collect' && args[2] === '--metadata') {
    const window = { since: args[1], until: new Date(utc(args[1]) + 86400000).toISOString() };
    const metadata = readJson(args[3]);
    if (metadata.schema !== 'access-gap-metadata.v1') throw new Error('metadata_schema');
    const env = process.env;
    const get = paperclipGet({ base: env.PAPERCLIP_API_URL ?? '', key: env.PAPERCLIP_API_KEY, company: env.PAPERCLIP_COMPANY_ID });
    // This sibling is existing managed skill source, never a fetched or user-chosen module.
    // Source-carrier fixture tests inject it; managed application retains the installed M26 files.
    const { collectInteractions } = await import('./owner-routing-collect.mjs');
    input = await collectAccessInputs({ company: env.PAPERCLIP_COMPANY_ID, operatorId: metadata.operatorId,
      window, asOf: new Date().toISOString(), get, collectInteractions, metadata });
  } else throw new Error('usage: --from-file <input.json> | --collect <UTC-day-start> --metadata <metadata.json>');
  const result = evaluateAccessGaps(input);
  const files = ['access-gap.mjs', 'access-gap-collect.mjs', 'access-gap-cli.mjs', 'delivery-effectiveness.mjs'];
  const sourceHashes = Object.fromEntries(files.map(name => [name, createHash('sha256').update(readFileSync(new URL(name, import.meta.url))).digest('hex')]));
  return canonical({ ...result, adapter: { sourceHashes, authorization: false } });
}
let isMain = false;
try { isMain = Boolean(process.argv[1]) && realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url)); } catch {}
if (isMain) {
  try { console.log(JSON.stringify(await main(process.argv.slice(2)))); }
  catch { console.error(JSON.stringify({ error: 'access_gap_refused', operationalAuthorization: false })); process.exitCode = 2; }
}
