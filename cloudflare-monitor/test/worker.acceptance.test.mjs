import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import {
  HTTP_CHECK, TLS_CHECK, READ_TOKEN, WEBHOOK,
  config, environment, MemoryStorage, requestInfo, assertNoSensitiveText,
} from './support.mjs';

const originalFetch = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('Unexpected network access in an offline test'); };
after(() => { globalThis.fetch = originalFetch; });
const { checkHttp } = await import('../src/monitor.mjs');
const { Monitor, default: worker } = await import('../src/worker.mjs');
const ORIGIN = 'https://monitor.example.com';
const DRILL_TOKEN = 'example-only-staging-drill-token-0000000000';

function workerEnvironment(name = 'staging', withObjects = false) {
  const env = environment(config([HTTP_CHECK, {
    id: 'api', name: 'Example API', url: 'https://api.example.com/health',
  }], [TLS_CHECK]), { ENVIRONMENT: name, DRILL_TOKEN });
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
          storage: new MemoryStorage(),
          blockConcurrencyWhile(callback) {
            const result = guarded.then(callback);
            guarded = result.catch(() => {});
            return result;
          },
        };
        const object = new Monitor(state, env);
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
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body,
  });
}

function assertRefused(response) {
  assert.ok(response.status >= 400 && response.status < 500, 'A refused drill or fixture must return a client refusal');
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
