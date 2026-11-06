import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';
import {
  NOW, HTTP_CHECK, TLS_CHECK, WEBHOOK, READ_TOKEN, DRILL_TOKEN,
  config, environment, activePack, certificateReply, jsonResponse, MemoryStorage,
  denyNetwork, offlineTransport, assertReceipt, assertMentionsDisabled, assertNoSensitiveText,
} from './support.mjs';

const deniedNetwork = denyNetwork();
afterEach(() => assert.equal(deniedNetwork.attempts.length, 0));
after(() => deniedNetwork.restore());
const { runMonitor } = await import('../src/monitor.mjs');

function scenario(configuration = config(), options = {}) {
  const env = environment(configuration);
  const values = new Map();
  const net = offlineTransport(env, options);
  let now = NOW;
  return {
    env, net,
    async tick(healthy) {
      // Recreate the storage facade to require durable state, not facade-local state.
      const receipt = await runMonitor(env, new MemoryStorage(values), net.fetchImpl, now);
      now += 300000;
      net.assertDestinations();
      assertReceipt(receipt, [...configuration.httpChecks, ...configuration.tlsChecks], healthy);
      return receipt;
    },
  };
}

for (const kind of ['HTTP', 'TLS']) {
  test(`${kind} healthy-first, DOWN, stable DOWN, recovery and stable health notify only on delivered transitions`, async () => {
    let healthy = true;
    const checks = kind === 'HTTP' ? config() : config([], [TLS_CHECK]);
    const s = scenario(checks, {
      http: () => new Response(null, { status: healthy ? 200 : 503 }),
      certificates: () => jsonResponse(certificateReply([activePack({
        certificates: [{
          ...activePack().certificates[0],
          expires_on: healthy ? '2030-02-01T00:00:00.000Z' : '2030-01-02T00:00:00.000Z',
        }],
      })])),
    });
    assert.equal((await s.tick(true)).delivery.sent, false);
    assert.equal(s.net.notifications.length, 0);
    healthy = false;
    const down = await s.tick(false);
    assert.equal(down.delivery.ok, true);
    assert.equal(down.delivery.messageId, '1000000001');
    assert.equal(s.net.notifications.length, 1);
    assert.equal((await s.tick(false)).delivery.sent, false);
    assert.equal(s.net.notifications.length, 1);
    healthy = true;
    const recovered = await s.tick(true);
    assert.equal(recovered.delivery.ok, true);
    assert.equal(recovered.delivery.messageId, '1000000002');
    assert.equal(s.net.notifications.length, 2);
    assert.equal((await s.tick(true)).delivery.sent, false);
    assert.equal(s.net.notifications.length, 2);
    const id = kind === 'HTTP' ? HTTP_CHECK.id : TLS_CHECK.id;
    assert.match(s.net.payloads()[0].content, new RegExp(`\\bALERT ${id}:`));
    assert.match(s.net.payloads()[1].content, new RegExp(`\\bRECOVERED ${id}:`));
    s.net.payloads().forEach(assertMentionsDisabled);
  });
}

for (const failure of ['HTTP rejection', 'transport rejection', 'missing message receipt']) {
  test(`failed DOWN delivery (${failure}) is retried before delivered state suppresses it`, async () => {
    let deliver = false;
    const s = scenario(config(), {
      http: () => new Response(null, { status: 503 }),
      webhook: (_info, attempt) => {
        if (deliver) return jsonResponse({ id: String(1000000000 + attempt) });
        if (failure === 'transport rejection') throw new Error('Example-only transport failure');
        if (failure === 'missing message receipt') return jsonResponse({});
        return jsonResponse({ message: 'example-only denied' }, 503);
      },
    });
    const failed = await s.tick(false);
    assert.equal(failed.delivery.ok, false);
    assert.equal(failed.delivery.messageId, undefined);
    assert.equal(s.net.notifications.length, 1);
    deliver = true;
    const retried = await s.tick(false);
    assert.equal(retried.delivery.ok, true);
    assert.equal(retried.delivery.messageId, '1000000002');
    assert.equal(s.net.notifications.length, 2);
    assert.equal((await s.tick(false)).delivery.sent, false);
    assert.equal(s.net.notifications.length, 2);
    assert.deepEqual(s.net.payloads()[0], s.net.payloads()[1], 'Retry must describe the same undelivered transition');
  });
}

for (const initialized of [false, true]) {
  test(`recovery before failed DOWN delivery invents no pair (${initialized ? 'healthy baseline' : 'new monitor'})`, async () => {
    let status = 200;
    let deliver = false;
    const s = scenario(config(), {
      http: () => new Response(null, { status }),
      webhook: (_info, attempt) => deliver
        ? jsonResponse({ id: String(1000000000 + attempt) })
        : jsonResponse({ message: 'example-only denied' }, 503),
    });
    if (initialized) await s.tick(true);
    status = 503;
    assert.equal((await s.tick(false)).delivery.ok, false);
    assert.equal(s.net.notifications.length, 1);
    status = 200;
    deliver = true;
    assert.equal((await s.tick(true)).delivery.sent, false);
    assert.equal((await s.tick(true)).delivery.sent, false);
    assert.equal(s.net.notifications.length, 1, 'No stale DOWN or invented recovery may be sent');
    status = 503;
    await s.tick(false);
    status = 200;
    await s.tick(true);
    assert.equal(s.net.notifications.length, 3, 'A later genuine incident must still alert and recover');
    assert.match(s.net.payloads()[1].content, /\bALERT web:/);
    assert.match(s.net.payloads()[2].content, /\bRECOVERED web:/);
  });
}

test('failed recovery delivery is retried once and subsequent stable health is silent', async () => {
  let status = 503;
  let deliver = true;
  const s = scenario(config(), {
    http: () => new Response(null, { status }),
    webhook: (_info, attempt) => deliver
      ? jsonResponse({ id: String(1000000000 + attempt) })
      : jsonResponse({ message: 'example-only denied' }, 503),
  });
  await s.tick(false);
  status = 200;
  deliver = false;
  assert.equal((await s.tick(true)).delivery.ok, false);
  assert.equal(s.net.notifications.length, 2);
  deliver = true;
  assert.equal((await s.tick(true)).delivery.messageId, '1000000003');
  assert.equal(s.net.notifications.length, 3);
  assert.equal((await s.tick(true)).delivery.sent, false);
  assert.equal(s.net.notifications.length, 3);
  assert.deepEqual(s.net.payloads()[1], s.net.payloads()[2]);
});

test('a mixed incident reports only checks which changed and disables Discord mentions', async () => {
  let status = 200;
  const s = scenario(config([HTTP_CHECK], [TLS_CHECK]), {
    http: () => new Response(null, { status }),
  });
  await s.tick(true);
  status = 503;
  const down = await s.tick();
  assert.equal(down.checks.find((check) => check.id === 'web').ok, false);
  assert.equal(down.checks.find((check) => check.id === 'tls').ok, true);
  assert.equal(s.net.notifications.length, 1);
  const payload = s.net.payloads()[0];
  assert.match(payload.content, /\bALERT web:/);
  assert.doesNotMatch(payload.content, /\b(ALERT|RECOVERED) tls:/);
  assertMentionsDisabled(payload);
});

for (const failure of ['response body', 'transport exception']) {
  test(`full receipts, webhook messages and logs redact sensitive upstream ${failure}`, async (t) => {
    const privateUrl = `https://${['example', 'internal'].join('.')}/private-fixture`;
    const marker = 'EXAMPLE_ONLY_RAW_BODY_MUST_NOT_ESCAPE';
    const webhookToken = new URL(WEBHOOK).pathname.split('/').at(-1);
    const sensitive = [privateUrl, marker, READ_TOKEN, WEBHOOK, webhookToken, DRILL_TOKEN];
    const raw = sensitive.join(' ');
    const logs = [];
    for (const method of ['log', 'warn', 'error', 'info', 'debug']) {
      t.mock.method(console, method, (...args) => { logs.push(args); });
    }
    const s = scenario(config([HTTP_CHECK], [TLS_CHECK]), {
      http: () => {
        if (failure === 'transport exception') throw new Error(raw);
        return new Response(raw, { status: 503 });
      },
      certificates: () => new Response(raw, { status: 403 }),
      webhook: () => {
        if (failure === 'transport exception') throw new Error(raw);
        return new Response(raw, { status: 500 });
      },
    });
    const receipt = await s.tick(false);
    assert.equal(receipt.delivery.ok, false);
    assert.equal(s.net.notifications.length, 1);
    const payloads = s.net.payloads();
    payloads.forEach(assertMentionsDisabled);
    assertNoSensitiveText({ receipt, payloads, logs }, sensitive);
  });
}
