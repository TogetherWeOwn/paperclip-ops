const MAX_CHECKS = 8;
const TIMEOUT_MS = 8000;
const EXPIRY_MS = 14 * 86400000;

function publicHostname(value) {
  return typeof value === 'string' && value.length <= 253
    && /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i.test(value)
    && !/\.(?:localhost|local|internal|invalid|test)$/i.test(value)
    && !/(?:^|\.)localhost$/i.test(value);
}

export function validateConfig(config) {
  if (!config || !Array.isArray(config.httpChecks) || !Array.isArray(config.tlsChecks)
      || config.httpChecks.length > MAX_CHECKS || config.tlsChecks.length > MAX_CHECKS
      || config.httpChecks.length + config.tlsChecks.length === 0) {
    throw new Error('invalid_monitor_config');
  }
  const ids = new Set();
  for (const check of [...config.httpChecks, ...config.tlsChecks]) {
    if (!check || typeof check.id !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,47}$/.test(check.id)
        || typeof check.name !== 'string' || !/^[a-zA-Z0-9 ._-]{1,64}$/.test(check.name)
        || ids.has(check.id)) throw new Error('invalid_check_identity');
    ids.add(check.id);
  }
  for (const check of config.httpChecks) {
    const url = new URL(check.url);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
        || url.port || !publicHostname(url.hostname)) throw new Error('invalid_http_target');
  }
  for (const check of config.tlsChecks) {
    if (typeof check.zoneId !== 'string' || !/^[a-f0-9]{32}$/.test(check.zoneId) || !publicHostname(check.hostname)) {
      throw new Error('invalid_tls_target');
    }
  }
  return config;
}

function result(check, ok, code) {
  return { id: check.id, ok, code };
}

async function request(fetchImpl, url, options = {}) {
  return fetchImpl(url, {
    ...options,
    redirect: 'manual',
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
}

export async function checkHttp(check, fetchImpl = fetch) {
  try {
    const response = await request(fetchImpl, check.url, {
      headers: { 'User-Agent': 'Cloudflare-Uptime-Monitor/1.0' },
      cache: 'no-store',
    });
    await response.body?.cancel();
    return result(check, response.status === 200, `http_${response.status}`);
  } catch {
    return result(check, false, 'http_request_failed');
  }
}

function covers(pattern, hostname) {
  if (typeof pattern !== 'string') return false;
  pattern = pattern.toLowerCase();
  hostname = hostname.toLowerCase();
  return pattern === hostname || (pattern.startsWith('*.')
    && hostname.split('.').length === pattern.split('.').length
    && hostname.endsWith(pattern.slice(1)));
}

export async function checkCertificates(check, token, fetchImpl = fetch, now = Date.now()) {
  if (!token) return result(check, false, 'tls_credentials_missing');
  try {
    const response = await request(fetchImpl,
      `https://api.cloudflare.com/client/v4/zones/${check.zoneId}/ssl/certificate_packs?status=active&per_page=50`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    if (!response.ok) {
      await response.body?.cancel();
      return result(check, false, 'tls_api_failed');
    }
    const data = await response.json();
    if (data.success !== true || !Array.isArray(data.result)
        || data.result.length >= 50 || (data.result_info?.total_pages ?? 1) > 1) {
      return result(check, false, 'tls_inventory_invalid');
    }
    const expiries = [];
    for (const pack of data.result) {
      if (pack.status !== 'active' || !Array.isArray(pack.hosts)
          || !pack.hosts.some(host => covers(host, check.hostname))) continue;
      if (!Array.isArray(pack.certificates) || pack.certificates.length === 0) {
        return result(check, false, 'tls_inventory_invalid');
      }
      for (const certificate of pack.certificates) {
        const hosts = certificate.hosts ?? pack.hosts;
        if (!Array.isArray(hosts)) return result(check, false, 'tls_inventory_invalid');
        if (!hosts.some(host => covers(host, check.hostname))) continue;
        const expiry = typeof certificate.expires_on === 'string'
          ? Date.parse(certificate.expires_on) : NaN;
        if (!Number.isFinite(expiry)) return result(check, false, 'tls_expiry_unknown');
        expiries.push(expiry);
      }
    }
    if (!expiries.length) return result(check, false, 'tls_coverage_missing');
    return result(check, Math.min(...expiries) > now + EXPIRY_MS,
      Math.min(...expiries) > now + EXPIRY_MS ? 'tls_valid' : 'tls_expiring');
  } catch {
    return result(check, false, 'tls_request_failed');
  }
}

function webhookUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.hostname !== 'discord.com' || url.port
      || url.username || url.password || url.search || url.hash
      || !/^\/api\/webhooks\/\d+\/[A-Za-z0-9_-]+$/.test(url.pathname)) {
    throw new Error('invalid_alert_destination');
  }
  url.searchParams.set('wait', 'true');
  return url.toString();
}

export async function runMonitor(env, storage, fetchImpl = fetch, now = Date.now()) {
  const config = validateConfig(JSON.parse(env.MONITOR_CONFIG));
  const destination = webhookUrl(env.ALERT_WEBHOOK_URL);
  const previous = await storage.get('snapshot');
  const delivered = { ...(previous?.delivered ?? {}) };
  const checks = await Promise.all([
    ...config.httpChecks.map(check => checkHttp(check, fetchImpl)),
    ...config.tlsChecks.map(check => checkCertificates(check, env.CF_CERTIFICATES_READ_TOKEN, fetchImpl, now)),
  ]);
  const events = checks.filter(check => check.ok !== (Object.hasOwn(delivered, check.id) ? delivered[check.id] : true));
  let delivery = { ok: true, sent: false };
  if (events.length) {
    try {
      const response = await request(fetchImpl, destination, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: `${env.ENVIRONMENT === 'staging' ? '[TEST] ' : ''}Uptime monitor\n`
            + events.map(check => `${check.ok ? 'RECOVERED' : 'ALERT'} ${check.id}: ${check.code}`).join('\n'),
          allowed_mentions: { parse: [] },
        }),
      });
      if (!response.ok) throw new Error('alert_delivery_failed');
      const message = await response.json();
      if (typeof message.id !== 'string' || !/^\d+$/.test(message.id)) {
        throw new Error('alert_receipt_invalid');
      }
      delivery = { ok: true, sent: true, messageId: message.id };
      for (const check of events) delivered[check.id] = check.ok;
    } catch {
      delivery = { ok: false, sent: false, code: 'alert_delivery_failed' };
    }
  }
  for (const check of checks) if (!Object.hasOwn(delivered, check.id) && check.ok) delivered[check.id] = true;
  const activeIds = new Set(checks.map(check => check.id));
  for (const id of Object.keys(delivered)) if (!activeIds.has(id)) delete delivered[id];
  const snapshot = { lastRunAt: now, checks, delivered, delivery };
  await storage.put('snapshot', snapshot);
  return { lastRunAt: now, checks, delivery };
}
