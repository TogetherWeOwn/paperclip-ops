// Reusable consent-first probe Worker.
//
// Routes (paths come from probe.config.json):
//   GET  /            honest coming-soon landing (+ aggregate page-view count)
//   GET  /privacy     privacy notice (DRAFT banner while placeholder)
//   GET  /changelog   plain changelog rendered from config
//   GET  /health      liveness only; never counted, never stores anything
//   POST /api/signup  consented waitlist signup; fail-closed
// Everything else: 404. No public admin/export endpoint exists by design,
// including no public inbox/admin/export route (see AGENT-INBOX.md).
// Email ingress arrives only via the Cloudflare Email Routing `email()`
// handler (exact configured recipient per probe; no catch-all). The handler
// never auto-replies, forwards, or fetches remote content.
// No cookies are set. No third-party requests are made. No client-side JS.

import {
  validateConfig,
  checkHostname,
  collectionStatus,
  normalizeHost,
} from "./config.js";
import { createStore, normalizeEmail, utcDay } from "./storage.js";
import { handleEmail } from "./email.js";

// Query param marking controlled test traffic: served normally, never counted.
export const TEST_TRAFFIC_PARAM = "fixture_test";

// Generic messages. The acceptance message is identical for first-time and
// duplicate signups so responses never reveal whether an address is listed.
// It is sent only after the record is persisted (or an existing same-probe
// record is confirmed); storage failures never claim success.
export const ACCEPTED_MESSAGE =
  "Your interest has been recorded. This is not a purchase or a reservation.";
export const DISABLED_MESSAGE = "Email signup is not open.";

// Sliding-window in-memory rate limiter. Keys live only in this Map for the
// length of one window; they are never persisted, logged or exported.
export function createRateLimiter({ windowMs, maxHits }) {
  const hits = new Map();
  return {
    check(key, nowMs = Date.now()) {
      const cutoff = nowMs - windowMs;
      let arr = hits.get(key) || [];
      arr = arr.filter((t) => t > cutoff);
      if (arr.length >= maxHits) {
        hits.set(key, arr);
        return false;
      }
      arr.push(nowMs);
      hits.set(key, arr);
      return true;
    },
    size() {
      return hits.size;
    },
  };
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function baseHeaders(contentType) {
  return {
    "content-type": contentType,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    // No framing, no permissions: this page needs neither.
    "content-security-policy":
      "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "cache-control": "no-store",
  };
}

function pageShell({ title, heading, bodyHtml }) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111;background:#fff}
a{color:#0b4fd6}a:visited{color:#0b4fd6}:focus-visible{outline:3px solid #0b4fd6;outline-offset:2px}
form{margin-top:1.5rem}label{display:block;margin:.75rem 0}
input[type=email]{display:block;width:100%;max-width:22rem;padding:.5rem;font-size:1rem;margin-top:.25rem}
button{padding:.6rem 1.2rem;font-size:1rem;margin-top:.5rem}
.draft{border:2px solid #8a6d00;background:#fff8e1;padding:.75rem 1rem;margin:1rem 0}
nav{margin-top:2rem;font-size:.9rem}
</style>
</head>
<body>
<main>
<h1>${escapeHtml(heading)}</h1>
${bodyHtml}
</main>
</body>
</html>`;
}

function landingBody(config, status) {
  const c = config.content;
  const links =
    `<nav><a href="${escapeHtml(config.paths.privacy)}">Privacy</a> · ` +
    `<a href="${escapeHtml(config.paths.changelog)}">Changelog</a></nav>`;
  if (!status.open) {
    return (
      `<p>${escapeHtml(c.body)}</p>\n` +
      `<p>${escapeHtml(DISABLED_MESSAGE)}</p>\n` +
      (config.privacy.placeholder ? `<p class="draft"><strong>DRAFT</strong> — pending critique and approval. Email signup is disabled on this page. Do not send personal information here.</p>\n` : "") +
      links
    );
  }
  return (
    `<p>${escapeHtml(c.body)}</p>\n` +
    `<form method="post" action="${escapeHtml(config.paths.signup)}">\n` +
    `<input type="hidden" name="probeId" value="${escapeHtml(config.probeId)}">\n` +
    `<label for="email">Email address\n` +
    `<input type="email" id="email" name="email" required autocomplete="email" maxlength="${config.limits.maxEmailLength}"></label>\n` +
    `<label for="consent"><input type="checkbox" id="consent" name="consent" value="yes"> ` +
    `${escapeHtml(`I agree to receive email updates about ${c.title}. I can withdraw my consent at any time. See the Privacy notice.`)}</label>\n` +
    `<button type="submit">Join the interest list</button>\n` +
    `<p role="status" aria-live="polite"></p>\n` +
    `</form>\n` +
    links
  );
}

function privacyBody(config) {
  const p = config.privacy;
  const row = (k, v) => `<p><strong>${escapeHtml(k)}:</strong> ${escapeHtml(v || "pending")}</p>\n`;
  // The withdrawal wording follows the fail-closed gate: while collection is
  // closed the method is explicitly pending; once open the gate guarantees a
  // real contact, so the row names that configured contact. It never names a
  // reply-to channel — no outbound mail exists.
  const deletion = collectionStatus(config).open
    ? `To withdraw your consent, contact ${p.contact}; your address will be deleted.`
    : "Withdrawal method pending — no withdrawal channel is operational while collection is disabled. Do not send personal information here.";
  return (
    (p.placeholder
      ? `<p class="draft"><strong>DRAFT</strong> — pending critique and approval. ${escapeHtml(p.notice)}</p>\n`
      : `<p>${escapeHtml(p.notice)}</p>\n`) +
    row("Controller", p.controller) +
    row("Contact", p.contact) +
    row("Country", p.country) +
    row("Purpose", "Email updates about this probe, only with your explicit consent.") +
    row("Retention", `At most ${config.limits.retentionDays} days; deleted sooner on withdrawal.`) +
    row("Sharing", "None. Addresses are never sold or shared with third parties.") +
    row("Deletion", deletion) +
    row("Notice version", p.noticeVersion) +
    `<nav><a href="/">Home</a> · <a href="${escapeHtml(config.paths.changelog)}">Changelog</a></nav>`
  );
}

function changelogBody(config) {
  const entries = config.changelog
    .map(
      (e) =>
        `<section><h2>${escapeHtml(e.version)} — ${escapeHtml(e.date)}</h2>\n<ul>\n` +
        e.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("\n") +
        `\n</ul></section>`,
    )
    .join("\n");
  return (
    `<p>This changelog lists what changed on this probe page. It claims no users, no commitments and no demand.</p>\n` +
    entries +
    `\n<nav><a href="/">Home</a> · <a href="${escapeHtml(config.paths.privacy)}">Privacy</a></nav>`
  );
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: baseHeaders("application/json; charset=utf-8") });
}

// One build, one config file. createApp wires a validated config to handlers;
// tests call this with each fixture config to prove config-only reuse.
export function createApp({ rawConfig, kv }) {
  const { ok, errors, config } = validateConfig(rawConfig);
  if (!ok) {
    const broken = errors;
    return {
      config: null,
      configErrors: broken,
      async fetch() {
        return new Response("Probe is misconfigured.", {
          status: 500,
          headers: baseHeaders("text/plain; charset=utf-8"),
        });
      },
    };
  }
  const store = createStore(kv, config.limits);
  const limiter = createRateLimiter({
    windowMs: config.limits.signupWindowMs,
    maxHits: config.limits.maxSignupsPerWindow,
  });

  async function fetch(request) {
    const url = new URL(request.url);
    const host = normalizeHost(request.headers.get("host") || url.hostname);
    if (!checkHostname(host, config)) {
      return new Response("Not found.", { status: 404, headers: baseHeaders("text/plain; charset=utf-8") });
    }
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "GET" && path === "/") {
      const html = pageShell({
        title: config.content.title,
        heading: config.content.heading,
        bodyHtml: landingBody(config, collectionStatus(config)),
      });
      // Aggregate first-party page-view count. Skipped for controlled test
      // traffic; counts are per-probe per-day totals, never per-visitor.
      if (url.searchParams.get(TEST_TRAFFIC_PARAM) !== "1") {
        await store.recordView(config.probeId, utcDay()).catch(() => {});
      }
      return new Response(html, { status: 200, headers: baseHeaders("text/html; charset=utf-8") });
    }

    if (request.method === "GET" && path === config.paths.privacy) {
      const html = pageShell({ title: `Privacy — ${config.content.title}`, heading: "Privacy notice", bodyHtml: privacyBody(config) });
      return new Response(html, { status: 200, headers: baseHeaders("text/html; charset=utf-8") });
    }

    if (request.method === "GET" && path === config.paths.changelog) {
      const html = pageShell({ title: `Changelog — ${config.content.title}`, heading: "Changelog", bodyHtml: changelogBody(config) });
      return new Response(html, { status: 200, headers: baseHeaders("text/html; charset=utf-8") });
    }

    if (request.method === "GET" && path === config.paths.health) {
      return json(200, { ok: true, probe: config.probeId, collection: collectionStatus(config).open ? "open" : "closed" });
    }

    if (path === config.paths.signup) {
      if (request.method !== "POST") {
        return new Response("Method not allowed.", { status: 405, headers: baseHeaders("text/plain; charset=utf-8") });
      }
      return handleSignup(request, url);
    }

    return new Response("Not found.", { status: 404, headers: baseHeaders("text/plain; charset=utf-8") });
  }

  async function handleSignup(request) {
    // Fail closed first: never accept input when collection is not fully open.
    const status = collectionStatus(config);
    if (!status.open) {
      return json(403, { ok: false, message: DISABLED_MESSAGE });
    }
    // Origin gate: browser posts carry Origin; it must be explicitly allowed.
    const origin = request.headers.get("origin") || "";
    if (!config.allowedOrigins.includes(origin)) {
      return json(403, { ok: false, message: "Request origin is not allowed." });
    }
    // The rendered page is a no-JS HTML form, so accept native form posts
    // as well as JSON. Both paths normalize to the same { probeId, email,
    // consent } shape and run the identical validation below.
    const contentType = request.headers.get("content-type") || "";
    let body;
    if (
      contentType.includes("application/x-www-form-urlencoded") ||
      contentType.includes("multipart/form-data")
    ) {
      try {
        const form = await request.formData();
        body = {
          probeId: form.get("probeId"),
          email: form.get("email"),
          // A checked box submits consent=yes; an unchecked box omits the
          // field entirely, and any other value is not explicit consent.
          consent: form.get("consent") === "yes",
        };
      } catch {
        return json(400, { ok: false, message: "Request body must be a valid form submission." });
      }
    } else {
      try {
        body = await request.json();
      } catch {
        return json(400, { ok: false, message: "Request body must be JSON." });
      }
    }
    // Cross-probe inputs rejected: body probeId must equal this probe.
    if (body.probeId !== config.probeId) {
      return json(400, { ok: false, message: "Unknown probe." });
    }
    // Explicit unchecked consent enforced server-side.
    if (body.consent !== true) {
      return json(400, { ok: false, message: "Explicit consent is required." });
    }
    const norm = normalizeEmail(body.email, config.limits.maxEmailLength);
    if (!norm.ok) {
      return json(400, { ok: false, message: "A valid email address is required." });
    }
    // Bounded rate: ephemeral in-memory window, never persisted.
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    if (!limiter.check(`${config.probeId}:${ip}`)) {
      return json(429, { ok: false, message: "Too many requests. Please try again later." });
    }
    const result = await store.signup({
      probeId: config.probeId,
      email: norm.email,
      consentAt: new Date().toISOString(),
      noticeVersion: config.privacy.noticeVersion,
      origin,
      probeHostname: normalizeHost(request.headers.get("host") || ""),
    });
    if (result.invalid) {
      return json(400, { ok: false, message: "A valid email address is required." });
    }
    if (result.full) {
      return json(503, { ok: false, message: DISABLED_MESSAGE });
    }
    // stored:true and duplicate:true return the IDENTICAL message: no
    // address enumeration is possible through this endpoint.
    return json(200, { ok: true, message: ACCEPTED_MESSAGE });
  }

  // Cloudflare Email Routing entry. `message` mimics the EmailMessage shape
  // ({ from, to, rawSize, raw, setReject }). No outbound send, no fetch,
  // no logging of bodies. Rejections call setReject (drop) and store nothing.
  async function email(message) {
    return handleEmail(message, {
      config,
      store,
    });
  }

  return { config, configErrors: [], fetch, email, store, limiter };
}
// Note: production wiring (probe.config.json + PROBE_KV binding + Email
// Routing rule to the exact inbox address) is documented in wrangler.toml
// and INBOX-RUNBOOK.md. This file keeps one createApp with fetch + email;
// there is no second Worker framework.
