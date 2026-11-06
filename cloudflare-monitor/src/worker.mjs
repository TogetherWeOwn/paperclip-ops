import { runMonitor, validateConfig } from './monitor.mjs';

const STALE_MS = 15 * 60000;

function json(body, status = 200) {
  return Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
}

function authorized(request, token) {
  return typeof token === 'string' && token.length >= 32
    && request.headers.get('Authorization') === `Bearer ${token}`;
}

function object(env, name) {
  return env.MONITOR.get(env.MONITOR.idFromName(name));
}

export class Monitor {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (request.method === 'GET' && path === '/healthz') {
      const snapshot = await this.state.storage.get('snapshot');
      const fresh = Number.isFinite(snapshot?.lastRunAt)
        && Date.now() - snapshot.lastRunAt >= 0 && Date.now() - snapshot.lastRunAt < STALE_MS;
      const healthy = fresh && snapshot.delivery?.ok === true
        && snapshot.checks?.length > 0 && snapshot.checks.every(check => check.ok);
      return json({ ok: Boolean(healthy), fresh: Boolean(fresh) }, healthy ? 200 : 503);
    }
    if (request.method !== 'POST' || !['/run', '/drill'].includes(path)) return json({ code: 'not_found' }, 404);
    if (path === '/drill' && this.env.ENVIRONMENT !== 'staging') return json({ code: 'not_found' }, 404);
    // The entire observation/delivery/write sequence is serialized, including I/O.
    return this.state.blockConcurrencyWhile(async () => {
      try {
        let env = this.env;
        let fetchImpl = fetch;
        if (path === '/drill') {
          if (!authorized(request, env.DRILL_TOKEN)) return json({ code: 'unauthorized' }, 401);
          const length = Number(request.headers.get('Content-Length'));
          if (!length || length > 128) return json({ code: 'invalid_drill' }, 400);
          const input = await request.json();
          if (!['down', 'up'].includes(input.phase)) return json({ code: 'invalid_drill' }, 400);
          const config = validateConfig(JSON.parse(env.MONITOR_CONFIG));
          const origin = new URL(request.url).origin;
          env = { ...env, MONITOR_CONFIG: JSON.stringify({
            ...config,
            httpChecks: config.httpChecks.map(check => ({
              ...check, url: `${origin}/fixture/http/${check.id}/${input.phase}`,
            })),
          }) };
          fetchImpl = async (url, options) => {
            const target = new URL(url);
            if (target.hostname === 'api.cloudflare.com') {
              // Expiry cannot safely be induced on a live certificate. Exercise the
              // same evaluator with a clearly synthetic managed-certificate record.
              const zoneId = target.pathname.split('/')[4];
              const hosts = config.tlsChecks.filter(check => check.zoneId === zoneId).map(check => check.hostname);
              return Response.json({ success: true, result: [{
                status: 'active', hosts,
                certificates: [{ hosts, expires_on: new Date(Date.now()
                  + (input.phase === 'down' ? 1 : 90) * 86400000).toISOString() }],
              }] });
            }
            return fetch(url, options);
          };
        }
        const receipt = await runMonitor(env, this.state.storage, fetchImpl);
        return json({ ...receipt, mode: path === '/drill' ? 'staging_fixture' : 'live' },
          receipt.delivery.ok ? 200 : 502);
      } catch {
        return json({ code: 'monitor_run_failed', retryable: true }, 503);
      }
    });
  }
}

export default {
  async scheduled(_event, env, context) {
    context.waitUntil((async () => {
      const response = await object(env, 'live').fetch('https://monitor.example/run', { method: 'POST' });
      if (!response.ok) throw new Error('monitor_scheduled_run_failed');
    })());
  },

  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method === 'GET' && path === '/healthz') {
      return object(env, 'live').fetch(request);
    }
    if (env.ENVIRONMENT === 'staging') {
      if (request.method === 'GET' && /^\/fixture\/http\/[a-z0-9_-]{1,48}\/(up|down)$/.test(path)) {
        return json({ fixture: true }, path.endsWith('/up') ? 200 : 503);
      }
      if (request.method === 'POST' && path === '/drill') {
        if (!authorized(request, env.DRILL_TOKEN)) return json({ code: 'unauthorized' }, 401);
        return object(env, 'drill').fetch(request);
      }
    }
    return json({ code: 'not_found' }, 404);
  },
};
