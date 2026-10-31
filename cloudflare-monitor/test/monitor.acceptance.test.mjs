import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import {
  NOW, HTTP_CHECK, TLS_CHECK, WEBHOOK, READ_TOKEN,
  config, environment, activePack, certificateReply, jsonResponse,
  MemoryStorage, requestInfo, assertNoSensitiveText, denyNetwork, offlineTransport,
} from './support.mjs';

const deniedNetwork = denyNetwork();
afterEach(() => assert.equal(deniedNetwork.attempts.length, 0));
after(() => deniedNetwork.restore());
const { validateConfig, checkHttp, checkCertificates, runMonitor } = await import('../src/monitor.mjs');

async function evaluateCertificate(makeResponse, check = TLS_CHECK, token = READ_TOKEN) {
  const requests = [];
  const observation = await checkCertificates(check, token, async (input, init) => {
    requests.push(requestInfo(input, init));
    return makeResponse();
  }, NOW);
  // Assert outside the transport: an evaluator must not swallow a failed assertion as DOWN.
  if (token) {
    assert.equal(requests.length, 1);
    const request = requests[0];
    const url = new URL(request.url);
    assert.equal(url.hostname, 'api.cloudflare.com');
    assert.equal(url.pathname, `/client/v4/zones/${TLS_CHECK.zoneId}/ssl/certificate_packs`);
    assert.equal(request.method, 'GET');
    assert.equal(request.headers.get('authorization'), `Bearer ${token}`);
  } else {
    assert.equal(requests.length, 0, 'Missing credentials must not cause an unauthenticated API request');
  }
  assert.equal(observation.id, check.id);
  return observation;
}

test('accepts public HTTPS health checks and the maximum eight checks of each kind', () => {
  assert.doesNotThrow(() => validateConfig(config()));
  assert.doesNotThrow(() => validateConfig(config([], [TLS_CHECK])));
  const httpChecks = Array.from({ length: 8 }, (_, n) => ({ ...HTTP_CHECK, id: `web-${n}` }));
  const tlsChecks = Array.from({ length: 8 }, (_, n) => ({ ...TLS_CHECK, id: `tls-${n}` }));
  assert.doesNotThrow(() => validateConfig(config(httpChecks, tlsChecks)));
});

// These deliberately invalid addresses are validation inputs, never probe targets.
for (const [label, url] of [
  ['plain HTTP', 'http://status.example.com/health'],
  ['an IPv4 address', 'https://192.0.2.1/health'],
  ['loopback', 'https://127.0.0.1/health'],
  ['a private IPv4 address', `https://${[10, 0, 0, 1].join('.')}/health`],
  ['an IPv6 address', 'https://[2001:db8::1]/health'],
  ['IPv6 loopback', 'https://[::1]/health'],
  ['localhost', 'https://localhost/health'],
  ['a localhost subdomain', 'https://example.localhost/health'],
  ['a local suffix', `https://${['example', 'local'].join('.')}/health`],
  ['a private suffix', `https://${['example', 'internal'].join('.')}/health`],
  ['a single-label hostname', 'https://example/health'],
  ['credentials', 'https://fixture:example-only@status.example.com/health'],
  ['a nonstandard port', 'https://status.example.com:8443/health'],
  ['a query string', 'https://status.example.com/health?fixture=example-only'],
  ['a fragment', 'https://status.example.com/health#fixture'],
]) {
  test(`rejects HTTP configuration containing ${label}`, () => {
    assert.throws(() => validateConfig(config([{ ...HTTP_CHECK, url }])));
  });
}

for (const kind of ['httpChecks', 'tlsChecks']) {
  test(`refuses a ninth ${kind} entry`, () => {
    const c = config([], []);
    c[kind] = Array.from({ length: 9 }, (_, n) => ({
      ...(kind === 'httpChecks' ? HTTP_CHECK : TLS_CHECK), id: `check-${n}`,
    }));
    assert.throws(() => validateConfig(c));
  });
}

test('rejects duplicate check IDs instead of sharing notification state', () => {
  assert.throws(() => validateConfig(config([HTTP_CHECK, { ...HTTP_CHECK }])));
  assert.throws(() => validateConfig(config([HTTP_CHECK], [{ ...TLS_CHECK, id: HTTP_CHECK.id }])));
});

for (const status of [200, 201, 204, 301, 302, 307, 308, 401, 429, 500, 503]) {
  test(`HTTP ${status} is ${status === 200 ? 'healthy' : 'DOWN, not exact HTTP 200'}`, async (t) => {
    const response = new Response(status === 204 ? null : 'example-only application body', {
      status, headers: { location: 'https://other.example.com/health' },
    });
    let bodyReads = 0;
    for (const method of ['text', 'json', 'arrayBuffer', 'blob', 'formData', 'bytes']) {
      const original = response[method].bind(response);
      t.mock.method(response, method, (...args) => { bodyReads += 1; return original(...args); });
    }
    if (response.body) {
      const getReader = response.body.getReader.bind(response.body);
      t.mock.method(response.body, 'getReader', (...args) => {
        const reader = getReader(...args);
        const read = reader.read.bind(reader);
        t.mock.method(reader, 'read', (...readArgs) => { bodyReads += 1; return read(...readArgs); });
        return reader;
      });
      for (const method of ['values', Symbol.asyncIterator, 'pipeTo', 'pipeThrough']) {
        const original = response.body[method].bind(response.body);
        t.mock.method(response.body, method, (...args) => { bodyReads += 1; return original(...args); });
      }
    }
    const requests = [];
    const observation = await checkHttp(HTTP_CHECK, async (input, init) => {
      requests.push(requestInfo(input, init));
      return response;
    });
    assert.deepEqual({ id: observation.id, ok: observation.ok }, { id: 'web', ok: status === 200 });
    assert.equal(requests.length, 1, 'A redirect must not fetch its destination');
    assert.equal(requests[0].url, HTTP_CHECK.url);
    assert.equal(requests[0].method, 'GET');
    assert.equal(requests[0].headers.get('authorization'), null);
    assert.equal(bodyReads, 0, 'HTTP checks may cancel bodies but must not read them');
  });
}

test('requests manual redirects so fetch cannot hide a login redirect behind a 200', async () => {
  const observation = await checkHttp(HTTP_CHECK, async (_input, init) => {
    if (init?.redirect !== 'manual') return new Response(null, { status: 200 });
    return new Response(null, { status: 302, headers: { location: 'https://login.example.com/' } });
  });
  assert.equal(observation.ok, false, 'Following a redirect must not turn it into health');
});

test('aborts a stalled HTTP fetch within the documented eight-second bound', { timeout: 12000 }, async () => {
  let probeSignal;
  const watchdog = new AbortController();
  try {
    const observation = await Promise.race([
      checkHttp(HTTP_CHECK, async (input, init) => {
        probeSignal = requestInfo(input, init).signal;
        if (!(probeSignal instanceof AbortSignal)) throw new Error('HTTP probe has no abort signal');
        return new Promise((_resolve, reject) => {
          const aborted = () => reject(new DOMException('Example abort', 'AbortError'));
          if (probeSignal.aborted) aborted();
          else probeSignal.addEventListener('abort', aborted, { once: true });
        });
      }),
      delay(10000, null, { signal: watchdog.signal }).then(() => assert.fail('Eight-second HTTP abort did not settle')),
    ]);
    assert.ok(probeSignal instanceof AbortSignal, 'HTTP probes require a bounded abort signal');
    assert.equal(probeSignal.aborted, true);
    assert.equal(observation.ok, false);
  } finally {
    watchdog.abort();
  }
});

const tlsCases = [
  ['exact certificate hostname', activePack(), true],
  ['one-label wildcard', activePack({
    hosts: ['example.com', '*.example.com'],
    certificates: [{ ...activePack().certificates[0], hosts: ['example.com', '*.example.com'] }],
  }), true],
  ['unrelated certificate hostname', activePack({
    hosts: ['other.example.com'],
    certificates: [{ ...activePack().certificates[0], hosts: ['other.example.com'] }],
  }), false],
  ['pack metadata without actual certificate coverage', activePack({
    certificates: [{ ...activePack().certificates[0], hosts: ['other.example.com'] }],
  }), false],
  ['inactive pack with a valid certificate', activePack({ status: 'pending_validation' }), false],
  ['missing certificates', activePack({ certificates: [] }), false],
  ['missing expiry', activePack({
    certificates: [{ id: 'example-certificate', status: 'active', hosts: ['status.example.com'] }],
  }), false],
  ['malformed expiry', activePack({
    certificates: [{ ...activePack().certificates[0], expires_on: 'not-a-date' }],
  }), false],
  ['expired certificate', activePack({
    certificates: [{ ...activePack().certificates[0], expires_on: '2029-12-31T23:59:59.000Z' }],
  }), false],
  ['exactly fourteen days remaining', activePack({
    certificates: [{ ...activePack().certificates[0], expires_on: '2030-01-15T00:00:00.000Z' }],
  }), false],
  ['fourteen days plus one millisecond remaining', activePack({
    certificates: [{ ...activePack().certificates[0], expires_on: '2030-01-15T00:00:00.001Z' }],
  }), true],
  ['one expiring certificate among otherwise healthy matching certificates', activePack({
    certificates: [activePack().certificates[0], {
      ...activePack().certificates[0], id: 'example-second-certificate', expires_on: '2030-01-02T00:00:00.000Z',
    }],
  }), false],
];

for (const [label, pack, healthy] of tlsCases) {
  test(`TLS evaluation: ${label}`, async () => {
    const observation = await evaluateCertificate(() => jsonResponse(certificateReply([pack])));
    assert.equal(observation.ok, healthy, label);
  });
}

for (const hostname of ['example.com', 'nested.status.example.com', 'notexample.com']) {
  test(`a wildcard alone does not cover ${hostname}`, async () => {
    const pack = activePack({
      hosts: ['*.example.com'],
      certificates: [{ ...activePack().certificates[0], hosts: ['*.example.com'] }],
    });
    const observation = await evaluateCertificate(
      () => jsonResponse(certificateReply([pack])), { ...TLS_CHECK, hostname },
    );
    assert.equal(observation.ok, false);
  });
}

for (const [label, makeResponse] of [
  ['empty pack inventory', () => jsonResponse(certificateReply([]))],
  ['missing result', () => jsonResponse({ success: true })],
  ['null result', () => jsonResponse({ success: true, result: null })],
  ['non-array result', () => jsonResponse({ success: true, result: {} })],
  ['non-object pack', () => jsonResponse(certificateReply([null]))],
  ['malformed JSON', () => new Response('example-only malformed JSON', { status: 200 })],
  ['API failure despite valid-looking result', () => jsonResponse(certificateReply([activePack()], { success: false }))],
  ['HTTP denial despite valid-looking result', () => jsonResponse(certificateReply(), 403)],
  ['API transport failure', () => { throw new Error('Example certificate API unavailable'); }],
]) {
  test(`TLS fails closed on ${label}`, async () => {
    const observation = await evaluateCertificate(makeResponse);
    assert.equal(observation.ok, false);
  });
}

test('missing certificate credentials fail closed without an unauthenticated API call', async () => {
  const observation = await evaluateCertificate(() => jsonResponse(certificateReply()), TLS_CHECK, '');
  assert.equal(observation.ok, false);
});

test('evaluator errors do not expose raw bodies, private URLs or credentials in observations or logs', async (t) => {
  const privateUrl = `https://${['example', 'internal'].join('.')}/private-fixture`;
  const bodyMarker = 'EXAMPLE_ONLY_RAW_BODY_MUST_NOT_ESCAPE';
  const sensitive = [bodyMarker, privateUrl, READ_TOKEN, WEBHOOK];
  const raw = sensitive.join(' ');
  const logs = [];
  for (const method of ['log', 'warn', 'error', 'info', 'debug']) {
    t.mock.method(console, method, (...args) => { logs.push(args); });
  }
  const http = await checkHttp(HTTP_CHECK, async () => { throw new Error(raw); });
  const tls = await evaluateCertificate(() => new Response(raw, { status: 403 }));
  assert.equal(http.ok, false);
  assert.equal(tls.ok, false);
  assertNoSensitiveText({ http, tls, logs }, sensitive);
});

test('first healthy run is silent with the fabricated Discord destination and injected transport', async () => {
  const env = environment();
  const net = offlineTransport(env);
  const receipt = await runMonitor(env, new MemoryStorage(), net.fetchImpl, NOW);
  net.assertDestinations();
  assert.ok(Array.isArray(receipt.checks));
  assert.deepEqual(receipt.checks.map((check) => check.id), ['web']);
  assert.equal(receipt.delivery.sent, false, 'First healthy observation must be silent');
  assert.equal(net.notifications.length, 0);
  assert.equal(net.requests.length, 1);
  assert.equal(net.requests[0].url, HTTP_CHECK.url);
});

for (const destination of [
  'https://hooks.example.com/api/webhooks/000000000000000000/example-only',
  'https://discord.com.example.org/api/webhooks/000000000000000000/example-only',
]) {
  test(`rejects a non-Discord destination at ${new URL(destination).hostname} before any fetch`, async () => {
    let calls = 0;
    await assert.rejects(runMonitor(
      environment(config(), { ALERT_WEBHOOK_URL: destination }),
      new MemoryStorage(),
      async () => { calls += 1; return jsonResponse({ id: '1000000001' }); },
      NOW,
    ), /invalid_alert_destination/);
    assert.equal(calls, 0);
  });
}
