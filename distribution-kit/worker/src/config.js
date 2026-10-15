// Probe configuration loading, validation and the fail-closed collection gate.
//
// One build serves many probes: the ONLY per-probe file is probe.config.json.
// Swapping that file (same schema version) re-renders a different probe with no
// source change. Provisioning DNS/routes for a genuinely new domain is NOT a
// config swap; see DOMAIN-SETUP.md.

export const SCHEMA_VERSION = 1;

// Spare-domain probes must never live on these hosts (Studio VENUES.md).
// Matched exactly or as a subdomain (e.g. probe.togetherweown.com).
export const DISALLOWED_DOMAINS = [
  "togetherweown.com",
  "two.gg",
  "kofra.app",
  "kofra.io",
  "nntune.com",
];

// Exact seed wording for the disabled-collection demo page (acceptance doc).
export const SEED_DISABLED_BODY =
  "We are testing ideas before deciding what to build. This page is a preview, not a product for sale. Email signup is not open.";

const PROBE_ID_RE = /^[a-z0-9-]{1,64}$/;

function isDisallowedHostname(hostname) {
  const h = String(hostname || "").toLowerCase();
  return DISALLOWED_DOMAINS.some((d) => h === d || h.endsWith("." + d));
}

function isHttpsUrl(value) {
  try {
    const u = new URL(String(value));
    return u.protocol === "https:";
  } catch {
    return false;
  }
}

// Validate a raw parsed config object. Returns { ok, errors, config }.
// Never throws on bad input; callers fail closed on !ok.
export function validateConfig(raw) {
  const errors = [];
  const c = raw && typeof raw === "object" ? raw : null;
  if (!c) return { ok: false, errors: ["config must be a JSON object"], config: null };

  if (c.schemaVersion !== SCHEMA_VERSION) {
    errors.push(`schemaVersion must be ${SCHEMA_VERSION}`);
  }
  if (typeof c.probeId !== "string" || !PROBE_ID_RE.test(c.probeId)) {
    errors.push("probeId must match /^[a-z0-9-]{1,64}$/");
  }
  if (typeof c.hostname !== "string" || c.hostname.trim() === "") {
    errors.push("hostname must be a non-empty string");
  } else if (isDisallowedHostname(c.hostname)) {
    errors.push(`hostname "${c.hostname}" is on a disallowed domain`);
  }
  if (!Array.isArray(c.allowedOrigins) || c.allowedOrigins.length === 0) {
    errors.push("allowedOrigins must be a non-empty array of https URLs");
  } else if (!c.allowedOrigins.every(isHttpsUrl)) {
    errors.push("allowedOrigins must all be https URLs");
  }
  if (typeof c.collectionEnabled !== "boolean") {
    errors.push("collectionEnabled must be boolean");
  }
  const content = c.content || {};
  for (const k of ["title", "heading", "body"]) {
    if (typeof content[k] !== "string" || content[k].trim() === "") {
      errors.push(`content.${k} must be a non-empty string`);
    }
  }
  const paths = c.paths || {};
  for (const k of ["privacy", "changelog", "health", "signup"]) {
    if (typeof paths[k] !== "string" || !paths[k].startsWith("/")) {
      errors.push(`paths.${k} must be a path starting with /`);
    }
  }
  const privacy = c.privacy || {};
  if (typeof privacy.noticeVersion !== "string" || privacy.noticeVersion.trim() === "") {
    errors.push("privacy.noticeVersion must be a non-empty string");
  }
  if (typeof privacy.placeholder !== "boolean") {
    errors.push("privacy.placeholder must be boolean");
  }
  if (typeof privacy.notice !== "string" || privacy.notice.trim() === "") {
    errors.push("privacy.notice must be a non-empty string");
  }
  if (!Array.isArray(c.changelog) || c.changelog.length === 0) {
    errors.push("changelog must be a non-empty array of entries");
  } else {
    for (const e of c.changelog) {
      if (!e || typeof e.version !== "string" || typeof e.date !== "string" || !Array.isArray(e.notes)) {
        errors.push("each changelog entry needs version, date and notes[]");
        break;
      }
    }
  }
  const limits = c.limits || {};
  for (const k of ["maxEmailLength", "maxSignups", "signupWindowMs", "maxSignupsPerWindow", "retentionDays"]) {
    if (!Number.isInteger(limits[k]) || limits[k] <= 0) {
      errors.push(`limits.${k} must be a positive integer`);
    }
  }
  if (Number.isInteger(limits.maxEmailLength) && limits.maxEmailLength > 1000) {
    errors.push("limits.maxEmailLength must not exceed 1000");
  }
  // Inbox block is optional (absent = handler disabled). When present and
  // enabled, it is validated strictly; disabled inbox needs no further
  // fields. Inbox readiness is independent of signup collection.
  if (c.inbox !== undefined && c.inbox !== null) {
    const ib = c.inbox;
    if (typeof ib !== "object" || Array.isArray(ib)) {
      errors.push("inbox must be an object");
    } else if (ib.enabled !== true && ib.enabled !== false) {
      errors.push("inbox.enabled must be boolean");
    } else if (ib.enabled === true) {
      if (typeof ib.address !== "string" || ib.address.trim() === "") {
        errors.push("inbox.address must be a non-empty string");
      } else {
        const addr = ib.address.trim().toLowerCase();
        if (!/^[^\s@\u007f]{1,64}@[^\s@\u007f]{1,253}\.[^\s@\u007f]{2,}$/.test(addr)) {
          errors.push("inbox.address must be a valid email");
        } else if (isDisallowedHostname(addr.split("@")[1] || "")) {
          errors.push(`inbox.address domain "${addr.split("@")[1]}" is on a disallowed domain`);
        }
      }
      for (const k of ["maxRawBytes", "maxSubjectChars", "maxTextChars", "maxRecords", "retentionDays"]) {
        if (!Number.isInteger(ib[k]) || ib[k] <= 0) {
          errors.push(`inbox.${k} must be a positive integer`);
        }
      }
      if (Number.isInteger(ib.maxRawBytes) && ib.maxRawBytes > 1024 * 1024) {
        errors.push("inbox.maxRawBytes must not exceed 1048576");
      }
      if (Number.isInteger(ib.maxSubjectChars) && ib.maxSubjectChars > 500) {
        errors.push("inbox.maxSubjectChars must not exceed 500");
      }
      if (Number.isInteger(ib.maxTextChars) && ib.maxTextChars > 10000) {
        errors.push("inbox.maxTextChars must not exceed 10000");
      }
    }
  }
  return { ok: errors.length === 0, errors, config: errors.length === 0 ? c : null };
}

// Normalize a request Host header for comparison (lowercase, strip port).
export function normalizeHost(hostHeader) {
  return String(hostHeader || "").toLowerCase().split(":")[0].trim();
}

// Hostname/probe-identity check. The request must arrive on the configured
// hostname; anything else is rejected without revealing config.
export function checkHostname(requestHost, config) {
  return normalizeHost(requestHost) === String(config.hostname).toLowerCase();
}

// Inbox handler readiness. INDEPENDENT of signup collection: disabled
// signup collection does not disable the inbox, and a disabled inbox does
// not affect signup. The handler runs only when ready is true.
export function inboxStatus(config) {
  const reasons = [];
  const inbox = config && typeof config === "object" ? config.inbox : null;
  if (!inbox || typeof inbox !== "object") return { ready: false, reasons: ["inbox is not configured"] };
  if (inbox.enabled !== true) return { ready: false, reasons: ["inbox.enabled is not true"] };
  if (typeof inbox.address !== "string" || inbox.address.trim() === "") {
    reasons.push("inbox.address is missing");
  } else {
    const addr = inbox.address.trim().toLowerCase();
    if (!/^[^\s@\u007f]{1,64}@[^\s@\u007f]{1,253}\.[^\s@\u007f]{2,}$/.test(addr)) {
      reasons.push("inbox.address is not a valid email");
    } else if (isDisallowedHostname(addr.split("@")[1] || "")) {
      reasons.push(`inbox.address domain "${addr.split("@")[1]}" is disallowed`);
    }
  }
  for (const k of ["maxRawBytes", "maxSubjectChars", "maxTextChars", "maxRecords", "retentionDays"]) {
    if (!Number.isInteger(inbox[k]) || inbox[k] <= 0) reasons.push(`inbox.${k} must be a positive integer`);
  }
  return { ready: reasons.length === 0, reasons };
}

// Fail-closed collection gate. Collection is OPEN only when every item holds:
// an explicit opt-in flag, a non-placeholder notice, real controller/contact/
// country facts, and an approval flag. Anything missing -> closed + reasons.
// A placeholder notice can NEVER enable collection.
export function collectionStatus(config) {
  const reasons = [];
  const p = config.privacy || {};
  if (config.collectionEnabled !== true) reasons.push("collectionEnabled is not true");
  if (p.placeholder === true) reasons.push("privacy notice is a placeholder");
  if (typeof p.controller !== "string" || p.controller.trim() === "") {
    reasons.push("privacy.controller is missing");
  }
  if (typeof p.contact !== "string" || p.contact.trim() === "") {
    reasons.push("privacy.contact is missing");
  }
  if (typeof p.country !== "string" || p.country.trim() === "") {
    reasons.push("privacy.country is missing");
  }
  if (p.approval !== true) reasons.push("privacy.approval is not true");
  if (typeof p.notice !== "string" || p.notice.trim() === "") {
    reasons.push("privacy.notice is missing");
  }
  return { open: reasons.length === 0, reasons };
}
