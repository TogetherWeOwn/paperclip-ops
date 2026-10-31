import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';
import { setImmediate as nextTurn } from 'node:timers/promises';
import {
  NOW, HTTP_CHECK, TLS_CHECK, READ_TOKEN, WEBHOOK, ORIGIN, DRILL_TOKEN,
  config, environment, MemoryStorage, requestInfo, assertNoSensitiveText,
  denyNetwork, offlineTransport, assertReceipt, assertMentionsDisabled,
  deferred, bounded, jsonResponse,
} from './support.mjs';

const deniedNetwork = denyNetwork();
afterEach(() => assert.equal(deniedNetwork.attempts.length, 0));
after(() => deniedNetwork.restore());
const { checkHttp } = await import('../src/monitor.mjs');
const { Monitor, default: worker } = await import('../src/worker.mjs');
const API_CHECK = { id: 'api', name: 'Example API', url: 'https://api.example.com/health' };

function workerEnvironment(name = 'staging', withObjects = false, options = {}) {
  const env = environment(options.configuration ?? config([HTTP_CHECK, API_CHECK], [TLS_CHECK]), {
    ENVIRONMENT: name, DRILL_TOKEN,
  });
  const accesses = [];
  const objects = new Map();
  const namespace = {
    idFromName: (id) => id,
    get(id) {
      accesses.push(id);
      if (!withObjects) throw new Error('This refused or fixture request must not reach an object');
      if (!objects.has(id)) {
        let guarded = Promise.resolve();
        const state = {
          storage: new MemoryStorage(new Map(), options.beforePut),
          blockConcurrencyWhile(callback) {
            const result = guarded.then(callback);
            guarded = result.catch(() => {});
            return result;
          },
        };
        const object = new Monitor(state, env);
        // Deliberately no fetch queue here: only the real object may serialize runs.
        objects.set(id, { fetch: (input, init) => object.fetch(new Request(input, init)) });
      }
      return objects.get(id);
    },
    getByName(id) { return namespace.get(namespace.idFromName(id)); },
  };
  env.MONITOR = namespace;
  return { env, accesses };
}

function drillRequest(phase, token = DRILL_TOKEN, body = JSON.stringify({ phase })) {
  return new Request(`${ORIGIN}/drill`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
      'content-length': String(Buffer.byteLength(body)),
    },
    body,
  });
}

function assertRefused(response) {
  assert.ok(response.status >= 400 && response.status < 500, 'A refused drill or fixture must return a client refusal');
}

function harness(t, options = {}) {
  t.mock.timers.enable({ apis: ['Date'], now: NOW });
  const { env } = workerEnvironment('staging', true, options);
  const configuration = JSON.parse(env.MONITOR_CONFIG);
  const initialConfig = env.MONITOR_CONFIG;
  const net = offlineTransport(env, {
    ...options,
    fixtureFetch: (info) => worker.fetch(info.request, env),
  });
  t.mock.method(globalThis, 'fetch', net.fetchImpl);
  return {
    env, net,
    async schedule() {
      const waits = [];
      try {
        await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: Date.now() }, env, {
          waitUntil: (promise) => { waits.push(Promise.resolve(promise)); },
        });
        await Promise.all(waits);
      } finally {
        net.assertDestinations();
      }
    },
    async drill(phase) {
      const request = drillRequest(phase);
      assert.ok(Buffer.byteLength(await request.clone().text()) <= 128);
      const before = net.requests.length;
      const response = await worker.fetch(request, env);
      net.assertDestinations();
      assert.equal(response.status, 200, 'A valid authenticated drill must execute and return a receipt');
      const receipt = await response.json();
      assertReceipt(receipt, [...configuration.httpChecks, ...configuration.tlsChecks], phase === 'up');
      const drillRequests = net.requests.slice(before);
      assert.equal(drillRequests.filter((info) => info.kind === 'http' || info.kind === 'tls').length, 0,
        'Drills must not fetch configured applications or the real certificate API');
      const fixtures = drillRequests.filter((info) => info.kind === 'fixture');
      assert.equal(fixtures.length, configuration.httpChecks.length);
      assert.deepEqual(fixtures.map((info) => new URL(info.url).pathname).sort(),
        configuration.httpChecks.map((check) => `/fixture/http/${check.id}/${phase}`).sort());
      assert.equal(env.MONITOR_CONFIG, initialConfig, 'A drill must not mutate monitored targets');
      if (receipt.delivery.ok && receipt.delivery.sent) assert.match(receipt.delivery.messageId, /^\d+$/);
      return receipt;
    },
    async health() {
      const before = net.requests.length;
      const response = await worker.fetch(new Request(`${ORIGIN}/healthz`), env);
      const value = await response.json();
      assert.equal(net.requests.length, before, 'Reading health must not refresh stale state by probing');
      assert.deepEqual(Object.keys(value).sort(), ['fresh', 'ok']);
      assert.equal(typeof value.ok, 'boolean');
      assert.equal(typeof value.fresh, 'boolean');
      assertNoSensitiveText(value, [HTTP_CHECK.url, TLS_CHECK.zoneId, READ_TOKEN, WEBHOOK, DRILL_TOKEN]);
      return { status: response.status, ...value };
    },
  };
}

for (const phase of ['down', 'up']) {
  test(`production refuses a ${phase} drill even when the token matches`, async () => {
    const { env, accesses } = workerEnvironment('production');
    assertRefused(await worker.fetch(drillRequest(phase), env));
    assert.equal(accesses.length, 0);
  });

  test(`production refuses the ${phase} HTTP fixture even when the token matches`, async () => {
    const { env, accesses } = workerEnvironment('production');
    const request = new Request(`${ORIGIN}/fixture/http/web/${phase}`, {
      headers: { authorization: `Bearer ${DRILL_TOKEN}` },
    });
    assertRefused(await worker.fetch(request, env));
    assert.equal(accesses.length, 0);
  });
}

test('missing environment selection does not enable staging drills', async () => {
  const { env, accesses } = workerEnvironment();
  delete env.ENVIRONMENT;
  assertRefused(await worker.fetch(drillRequest('down'), env));
  assert.equal(accesses.length, 0);
});

for (const token of ['', 'example-only-wrong-token']) {
  test(`staging rejects a ${token ? 'wrong' : 'missing'} drill bearer token`, async () => {
    const { env, accesses } = workerEnvironment();
    const request = drillRequest('down', token);
    if (!token) request.headers.delete('authorization');
    assertRefused(await worker.fetch(request, env));
    assert.equal(accesses.length, 0);
  });
}

test('staging rejects an oversized drill body before any probe', async (t) => {
  const { env } = workerEnvironment('staging', true);
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    return new Response(null, { status: 200 });
  });
  const body = JSON.stringify({ phase: 'down', padding: 'x'.repeat(129) });
  assertRefused(await worker.fetch(drillRequest('down', DRILL_TOKEN, body), env));
  assert.equal(requests.length, 0);
});

test('staging rejects an unknown drill phase without executing a check', async (t) => {
  const { env } = workerEnvironment('staging', true);
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    return new Response(null, { status: 200 });
  });
  assertRefused(await worker.fetch(drillRequest('unknown'), env));
  assert.equal(requests.length, 0);
});

for (const id of ['web', 'api']) {
  test(`staging HTTP fixture takes ${id} down and up through the real HTTP evaluator without touching its target`, async () => {
    const { env, accesses } = workerEnvironment();
    const initialConfig = env.MONITOR_CONFIG;
    const target = JSON.parse(initialConfig).httpChecks.find((check) => check.id === id);
    const fetched = [];
    for (const [phase, healthy] of [['down', false], ['up', true]]) {
      const fixture = `${ORIGIN}/fixture/http/${id}/${phase}`;
      const observation = await checkHttp({ ...target, url: fixture }, async (input, init) => {
        const info = requestInfo(input, init);
        fetched.push(info);
        return worker.fetch(info.request, env);
      });
      assert.equal(observation.id, id);
      assert.equal(observation.ok, healthy, `The ${phase} fixture must exercise the same evaluator`);
    }
    assert.equal(fetched.length, 2);
    assert.ok(fetched.every((request) => request.method === 'GET'));
    assert.ok(fetched.every((request) => new URL(request.url).pathname.startsWith(`/fixture/http/${id}/`)));
    assert.ok(fetched.every((request) => request.url !== target.url));
    assert.equal(env.MONITOR_CONFIG, initialConfig);
    assert.equal(accesses.length, 0);
  });
}

test('never-run health fails closed and exposes only the two documented booleans', async () => {
  const { env } = workerEnvironment('staging', true);
  const response = await worker.fetch(new Request(`${ORIGIN}/healthz`), env);
  assert.equal(response.status, 503);
  const health = await response.json();
  assert.deepEqual(health, { ok: false, fresh: false });
  assertNoSensitiveText(health, [HTTP_CHECK.url, TLS_CHECK.zoneId, READ_TOKEN, WEBHOOK, DRILL_TOKEN]);
});

for (const kind of ['HTTP', 'TLS', 'mixed']) {
  test(`authenticated ${kind} staging drills execute DOWN/up and repeated phases through real evaluators`, async (t) => {
    const configuration = kind === 'HTTP' ? config([HTTP_CHECK, API_CHECK])
      : kind === 'TLS' ? config([], [TLS_CHECK]) : config([HTTP_CHECK, API_CHECK], [TLS_CHECK]);
    const h = harness(t, { configuration });
    assert.equal((await h.drill('up')).delivery.sent, false);
    assert.equal(h.net.notifications.length, 0);
    assert.equal((await h.drill('down')).delivery.messageId, '1000000001');
    assert.equal(h.net.notifications.length, 1);
    assert.equal((await h.drill('down')).delivery.sent, false);
    assert.equal(h.net.notifications.length, 1);
    assert.equal((await h.drill('up')).delivery.messageId, '1000000002');
    assert.equal(h.net.notifications.length, 2);
    assert.equal((await h.drill('up')).delivery.sent, false);
    assert.equal(h.net.notifications.length, 2);
    const payloads = h.net.payloads();
    for (const check of [...configuration.httpChecks, ...configuration.tlsChecks]) {
      assert.equal((payloads[0].content.match(new RegExp(`\\bALERT ${check.id}:`, 'g')) ?? []).length, 1);
      assert.equal((payloads[1].content.match(new RegExp(`\\bRECOVERED ${check.id}:`, 'g')) ?? []).length, 1);
    }
    payloads.forEach(assertMentionsDisabled);
    assertNoSensitiveText(payloads, [READ_TOKEN, WEBHOOK, DRILL_TOKEN]);
  });
}

test('drill and live state cannot recover, suppress or contaminate each other', async (t) => {
  let status = 200;
  const h = harness(t, { http: () => new Response(null, { status }) });
  await h.schedule();
  assert.deepEqual(await h.health(), { status: 200, ok: true, fresh: true });
  await h.drill('down');
  assert.equal(h.net.notifications.length, 1);
  assert.deepEqual(await h.health(), { status: 200, ok: true, fresh: true });
  await h.schedule();
  assert.equal(h.net.notifications.length, 1, 'Live health must not recover the drill incident');
  await h.drill('up');
  assert.equal(h.net.notifications.length, 2);
  status = 503;
  await h.schedule();
  assert.equal(h.net.notifications.length, 3, 'A live incident must not be suppressed by healthy drill state');
  assert.equal((await h.health()).status, 503);
  await h.drill('up');
  assert.equal(h.net.notifications.length, 3);
  assert.equal((await h.health()).status, 503, 'A healthy drill must not repair live state');
  status = 200;
  await h.schedule();
  assert.equal(h.net.notifications.length, 4);
  assert.equal((await h.health()).status, 200);
});

test('health is positive only after a real scheduled observation and rejects state older than fifteen minutes', async (t) => {
  const h = harness(t);
  assert.deepEqual(await h.health(), { status: 503, ok: false, fresh: false });
  await h.schedule();
  assert.deepEqual(await h.health(), { status: 200, ok: true, fresh: true });
  t.mock.timers.setTime(NOW + 899999);
  assert.deepEqual(await h.health(), { status: 200, ok: true, fresh: true });
  t.mock.timers.setTime(NOW + 900001);
  assert.deepEqual(await h.health(), { status: 503, ok: false, fresh: false });
});

test('fresh healthy targets do not make health positive while recovery delivery is still failing', async (t) => {
  let status = 503;
  let deliver = true;
  const h = harness(t, {
    http: () => new Response(null, { status }),
    webhook: (_info, attempt) => deliver
      ? jsonResponse({ id: String(1000000000 + attempt) })
      : jsonResponse({ message: 'example-only denied' }, 503),
  });
  await h.schedule();
  assert.equal(h.net.notifications.length, 1);
  status = 200;
  deliver = false;
  // The scheduled entrypoint may reject failed delivery; health must still report it.
  await h.schedule().catch((error) => assert.equal(error.message, 'monitor_scheduled_run_failed'));
  assert.equal(h.net.notifications.length, 2);
  const failed = await h.health();
  assert.equal(failed.status, 503);
  assert.equal(failed.ok, false);
  deliver = true;
  await h.schedule();
  assert.equal(h.net.notifications.length, 3);
  assert.deepEqual(await h.health(), { status: 200, ok: true, fresh: true });
});

for (const phase of ['probe', 'delivery', 'persistence']) {
  test(`concurrent object runs remain serialized while the first ${phase} is pending`, { timeout: 6000 }, async (t) => {
    const entered = deferred();
    const release = deferred();
    let held = false;
    let notificationStarted = false;
    const hold = async () => {
      if (held) return;
      held = true;
      entered.resolve();
      await release.promise;
    };
    const h = harness(t, {
      configuration: config(),
      http: async () => {
        if (phase === 'probe') await hold();
        return new Response(null, { status: 503 });
      },
      webhook: async (_info, attempt) => {
        notificationStarted = true;
        if (phase === 'delivery') await hold();
        return jsonResponse({ id: String(1000000000 + attempt) });
      },
      beforePut: async () => {
        if (phase === 'persistence' && notificationStarted) await hold();
      },
    });
    const first = h.schedule();
    const runs = [first];
    let observationsWhileHeld;
    let notificationsWhileHeld;
    let results;
    try {
      await bounded(entered.promise);
      runs.push(h.schedule());
      await nextTurn();
      observationsWhileHeld = h.net.requests.filter((info) => info.kind === 'http').length;
      notificationsWhileHeld = h.net.notifications.length;
    } finally {
      release.resolve();
      results = await bounded(Promise.allSettled(runs));
    }
    assert.ok(results.every((result) => result.status === 'fulfilled'), 'Every scheduled run must finish');
    assert.equal(observationsWhileHeld, 1, 'The next observation must wait for the whole preceding run');
    assert.equal(notificationsWhileHeld, phase === 'probe' ? 0 : 1);
    assert.equal(h.net.requests.filter((info) => info.kind === 'http').length, 2);
    assert.equal(h.net.notifications.length, 1, 'Concurrent stable DOWN runs must not duplicate delivery');
    h.net.assertDestinations();
  });
}
