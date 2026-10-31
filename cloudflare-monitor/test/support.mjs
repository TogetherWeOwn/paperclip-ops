import assert from 'node:assert/strict';

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
export const WEBHOOK = 'https://hooks.example.com/api/webhooks/fixture/example-only';
export const READ_TOKEN = 'example-only-certificate-read-token';

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
  constructor(values = new Map()) {
    this.values = values;
  }

  async get(key) {
    return structuredClone(this.values.get(key));
  }

  async put(key, value) {
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
  const rendered = typeof value === 'string' ? value : JSON.stringify(value);
  for (const text of forbidden) {
    assert.ok(!rendered.includes(text), 'A receipt, notification or log exposed sensitive input');
  }
}
