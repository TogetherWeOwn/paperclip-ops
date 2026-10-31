import assert from 'node:assert/strict';
import { inspect } from 'node:util';

export const NOW = Date.parse('2030-01-01T00:00:00.000Z');
export const HTTP_CHECK = Object.freeze({
  id: 'web',
  name: 'Example web',
  url: 'https://status.example.com/health',
});
export const TLS_CHECK = Object.freeze({
  id: 'tls',
  name: 'Example TLS',
  zoneId: 'a'.repeat(32),
  hostname: 'status.example.com',
});
// Fabricated API-shape fixture, permitted only with denied/injected network access.
export const WEBHOOK = 'https://discord.com/api/webhooks/000000000000000000/example-only-offline-webhook-token';
export const READ_TOKEN = 'example-only-certificate-read-token';
export const ORIGIN = 'https://monitor.example.com';
export const DRILL_TOKEN = 'example-only-staging-drill-token-0000000000';

export function config(httpChecks = [HTTP_CHECK], tlsChecks = []) {
  return structuredClone({ httpChecks, tlsChecks });
}

export function environment(configuration = config(), extra = {}) {
  return {
    MONITOR_CONFIG: JSON.stringify(configuration),
    ALERT_WEBHOOK_URL: WEBHOOK,
    CF_CERTIFICATES_READ_TOKEN: READ_TOKEN,
    ...extra,
  };
}

export function activePack(overrides = {}) {
  return {
    id: 'example-pack',
    type: 'universal',
    status: 'active',
    hosts: ['example.com', 'status.example.com'],
    certificates: [{
      id: 'example-certificate',
      status: 'active',
      hosts: ['example.com', 'status.example.com'],
      expires_on: '2030-02-01T00:00:00.000Z',
      issuer: 'Example issuer',
      signature: 'ECDSAWithSHA256',
      uploaded_on: '2029-12-01T00:00:00.000Z',
      modified_on: '2029-12-01T00:00:00.000Z',
    }],
    ...structuredClone(overrides),
  };
}

export function certificateReply(packs = [activePack()], extra = {}) {
  return {
    success: true,
    errors: [],
    messages: [],
    result: structuredClone(packs),
    result_info: { page: 1, per_page: 50, count: packs.length, total_count: packs.length },
    ...extra,
  };
}

export function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

export class MemoryStorage {
  constructor(values = new Map(), beforePut) {
    this.values = values;
    this.beforePut = beforePut;
  }

  async get(key) {
    return structuredClone(this.values.get(key));
  }

  async put(key, value) {
    if (this.beforePut) await this.beforePut();
    this.values.set(key, structuredClone(value));
  }
}

export function requestInfo(input, init = {}) {
  const request = new Request(input, init);
  return {
    request,
    url: request.url,
    method: request.method,
    headers: request.headers,
    signal: init.signal ?? (input instanceof Request ? input.signal : undefined),
  };
}

export function assertNoSensitiveText(value, forbidden) {
  const rendered = typeof value === 'string' ? value : inspect(value, { depth: null });
  for (const text of forbidden) {
    assert.ok(!rendered.includes(text), 'A receipt, notification or log exposed sensitive input');
  }
}

export function denyNetwork() {
  const original = globalThis.fetch;
  const attempts = [];
  globalThis.fetch = async () => {
    attempts.push('denied');
    throw new Error('Unexpected network access in an offline test');
  };
  return { attempts, restore: () => { globalThis.fetch = original; } };
}

export function offlineTransport(env, options = {}) {
  const configuration = JSON.parse(env.MONITOR_CONFIG);
  const targets = new Set(configuration.httpChecks.map((check) => check.url));
  const certificatePaths = new Set(configuration.tlsChecks.map((check) =>
    `/client/v4/zones/${check.zoneId}/ssl/certificate_packs`));
  const destination = new URL(env.ALERT_WEBHOOK_URL);
  const requests = [];
  const notifications = [];
  const fetchImpl = async (input, init) => {
    const info = requestInfo(input, init);
    const url = new URL(info.url);
    requests.push(info);
    if (url.origin === destination.origin && url.pathname === destination.pathname) {
      info.kind = 'webhook';
      notifications.push(await info.request.text());
      return options.webhook
        ? options.webhook(info, notifications.length)
        : jsonResponse({ id: String(1000000000 + notifications.length) });
    }
    if (targets.has(info.url)) {
      info.kind = 'http';
      return options.http ? options.http(info) : new Response(null, { status: 200 });
    }
    if (url.hostname === 'api.cloudflare.com' && certificatePaths.has(url.pathname)) {
      info.kind = 'tls';
      return options.certificates ? options.certificates(info) : jsonResponse(certificateReply());
    }
    if (options.fixtureFetch && url.origin === ORIGIN &&
        /^\/fixture\/http\/[a-z0-9-]+\/(down|up)$/.test(url.pathname)) {
      info.kind = 'fixture';
      return options.fixtureFetch(info);
    }
    info.kind = 'unexpected';
    throw new Error('Unexpected offline transport destination');
  };

  // Always call outside the transport: evaluators may catch transport errors as DOWN.
  function assertDestinations() {
    for (const info of requests) {
      assert.notEqual(info.kind, 'unexpected', 'A run attempted an unapproved offline destination');
      if (info.kind === 'webhook') {
        const url = new URL(info.url);
        assert.equal(info.method, 'POST');
        assert.equal(url.searchParams.get('wait'), 'true');
        assert.deepEqual([...url.searchParams.keys()], ['wait']);
        assert.equal(info.headers.get('authorization'), null);
      } else {
        assert.equal(info.method, 'GET');
        if (info.kind === 'tls') {
          assert.equal(info.headers.get('authorization'), `Bearer ${env.CF_CERTIFICATES_READ_TOKEN}`);
        } else {
          assert.equal(info.headers.get('authorization'), null, 'Probe requests must not forward credentials');
          assert.equal(info.request.redirect, 'manual');
        }
      }
    }
    for (const body of notifications) assert.ok(JSON.parse(body));
  }
  return {
    fetchImpl, requests, notifications, assertDestinations,
    payloads: () => notifications.map((body) => JSON.parse(body)),
  };
}

export function assertReceipt(receipt, checks, healthy) {
  assert.ok(receipt && Array.isArray(receipt.checks), 'A run must return check receipts');
  assert.deepEqual(receipt.checks.map((check) => check.id).sort(), checks.map((check) => check.id).sort());
  if (healthy !== undefined) {
    assert.ok(receipt.checks.every((check) => check.ok === healthy), 'Every configured check must have the expected outcome');
  }
  assert.equal(typeof receipt.delivery.sent, 'boolean');
}

export function assertMentionsDisabled(payload) {
  assert.ok(payload.allowed_mentions, 'Mention suppression must be explicit');
  assert.deepEqual(payload.allowed_mentions.parse, []);
  assert.deepEqual(payload.allowed_mentions.users ?? [], []);
  assert.deepEqual(payload.allowed_mentions.roles ?? [], []);
  assert.notEqual(payload.allowed_mentions.replied_user, true);
}

export function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

export async function bounded(promise, milliseconds = 1500) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error('Offline operation did not reach its checkpoint')), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
