/**
 * omniroute-broker — the verb table and the approval policy (case_2748a903).
 *
 * Pure: no I/O, no host services, no network. The authorization decision is a
 * function of (verb, body, actor) alone so it can be unit-tested directly rather
 * than inferred from a route test. Same discipline as the retired token broker's
 * scope.js/ownership.js.
 *
 * ## Why a table and not a proxy
 *
 * The broker holds an OmniRoute credential that can do everything (see README —
 * `POST /api/providers` requires the TOP scope in every credential class
 * OmniRoute has). If the broker forwarded caller-supplied method+path, it would
 * simply be the management key with extra steps: a caller could reach
 * `GET /api/keys/<id>/reveal` and read another company's plaintext key.
 *
 * So the caller never supplies a method or a path. The caller supplies a VERB
 * NAME, which is looked up here by EXACT STRING EQUALITY in a deny-by-default
 * table. No substrings, no prefixes, no regex, no normalisation — the same rule
 * case_2963068b's gate_allowlist arrived at after Claude turned out to be reachable
 * through 351 ids, 14 of which contained neither "claude" nor "anthropic".
 * An unknown verb is a 404, never a pass-through.
 */

/** Approval classes, in increasing order of ceremony. */
export const APPROVAL = Object.freeze({
  /** Ungated. Read-only, and the response is scrubbed by redact.js. */
  NONE: "none",
  /** One responsible agent: the assignee of the issue, proven by ownership.js. */
  SINGLE: "single",
  /** Two distinct agents. Real money is downstream of these. */
  DUAL: "dual",
});

export class VerbError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "VerbError";
    this.status = status;
  }
}

/**
 * The complete set of operations this broker will perform. Anything absent is
 * refused — including every route that exists on the management API but is not
 * listed here, which is the point.
 *
 * Deliberately ABSENT, and each absence is a decision:
 *
 *   /api/keys, /api/keys/:id/reveal, /api/keys/:id/regenerate
 *       Key management. `reveal` returns another key's plaintext (confirmed:
 *       the handler ends `return NextResponse.json({key: r.key})`), and the keys
 *       on this box belong to other companies. There is no verb for this table
 *       that could ever be safe, so there is no verb.
 *   /api/cli/tokens
 *       Mints `oma_` access tokens. A broker that can mint credentials is a
 *       credential-handout path wearing a broker costume.
 *   /api/oauth, /api/auth, /api/policy, /api/services, /api/mcp, /api/shutdown
 *       Session, policy and process-lifecycle surfaces. Not routing.
 *   /api/settings/database, /api/providers/health-autopilot/actions
 *       In OmniRoute's own ALWAYS_PROTECTED_API_PATHS.
 *
 * `paidTraffic: true` marks a verb that can change which provider serves billed
 * traffic. It forces DUAL regardless of the declared class — see
 * `classifyApproval`.
 */
export const VERBS = Object.freeze({
  // ── Reads. Ungated per the owner's line, but never raw: every response
  // goes through redact.js, because a management-plane provider record embeds
  // the upstream credential in the clear.
  "providers.list": {
    method: "GET",
    path: "/api/providers",
    approval: APPROVAL.NONE,
    resource: "provider",
    summary: "List configured providers.",
  },
  "providers.get": {
    method: "GET",
    path: "/api/providers/:id",
    approval: APPROVAL.NONE,
    resource: "provider",
    params: ["id"],
    summary: "Read one provider.",
  },
  "combos.list": {
    method: "GET",
    path: "/api/combos",
    approval: APPROVAL.NONE,
    resource: "combo",
    summary: "List combos.",
  },
  "models.list": {
    method: "GET",
    path: "/api/models",
    approval: APPROVAL.NONE,
    resource: "model",
    summary: "List the model catalogue.",
  },
  "mappings.list": {
    method: "GET",
    path: "/api/model-combo-mappings",
    approval: APPROVAL.NONE,
    resource: "mapping",
    summary: "List model->combo routing mappings.",
  },

  // ── Single approval: the responsible agent, proven by holding the issue.
  // SINGLE is the floor for a body of plain fields only. Each of these verbs
  // has a body allowlist (`BODY_CONSTRAINTS` below), endpoint fields escalate
  // to DUAL, and a credential field is refused at every class. An `:id` must
  // name a resource bound to the caller's company (resources.js).
  "providers.create": {
    method: "POST",
    path: "/api/providers",
    approval: APPROVAL.SINGLE,
    resource: "provider",
    summary: "Register a new provider node.",
  },
  "providers.update": {
    method: "PUT",
    path: "/api/providers/:id",
    approval: APPROVAL.SINGLE,
    resource: "provider",
    params: ["id"],
    summary: "Update an existing provider.",
  },
  "combos.create": {
    method: "POST",
    path: "/api/combos",
    approval: APPROVAL.SINGLE,
    resource: "combo",
    summary: "Create a combo.",
  },
  "combos.update": {
    method: "PUT",
    path: "/api/combos/:id",
    approval: APPROVAL.SINGLE,
    resource: "combo",
    params: ["id"],
    summary: "Update a combo.",
  },

  // ── Two keys. Destructive, or moves billed traffic.
  "providers.delete": {
    method: "DELETE",
    path: "/api/providers/:id",
    approval: APPROVAL.DUAL,
    resource: "provider",
    params: ["id"],
    summary: "Delete a provider.",
  },
  "combos.delete": {
    method: "DELETE",
    path: "/api/combos/:id",
    approval: APPROVAL.DUAL,
    resource: "combo",
    params: ["id"],
    summary: "Delete a combo.",
  },
  "providers.set-priority": {
    method: "PUT",
    path: "/api/providers/:id/priority",
    approval: APPROVAL.DUAL,
    paidTraffic: true,
    resource: "provider",
    params: ["id"],
    summary: "Reorder provider preference — changes who serves billed traffic.",
  },

  // ── Mappings. A combo is inert configuration; the MAPPING is the object that
  // actually moves traffic, because it is what a bare model id resolves through.
  // So both mutations sit in the two-key class, and `mappings.create` carries an
  // additional constraint the caller cannot opt out of — see
  // `assertMappingCreate` below.
  //
  // On the class: the CISO's ruling proposed SINGLE for create. That cannot be
  // implemented as stated. The route's own contract requires `priority`, and
  // `priority` and `enabled` are both PAID_TRAFFIC_KEYS, so `classifyApproval`
  // escalates every conforming call to DUAL anyway. Declaring SINGLE would be a
  // label that never matches behaviour. It is declared DUAL to say what it does,
  // and `paidTraffic` is set explicitly so the class does not depend on the
  // caller happening to include a tripwire key.
  "mappings.create": {
    method: "POST",
    path: "/api/model-combo-mappings",
    approval: APPROVAL.DUAL,
    paidTraffic: true,
    resource: "mapping",
    summary: "Point a model-id pattern at a combo — changes who serves that traffic.",
  },
  "mappings.delete": {
    method: "DELETE",
    path: "/api/model-combo-mappings/:id",
    approval: APPROVAL.DUAL,
    resource: "mapping",
    params: ["id"],
    summary: "Remove a mapping. Without this, mappings.create is a one-way door.",
  },
});

/**
 * Body keys that indicate the operation changes which provider serves paid
 * traffic, or re-points billed capacity.
 *
 * This is case_2963068b's SECONDARY TRIPWIRE pattern, and it has the same one-way
 * property: it can only ever require MORE approval, never less. A false positive
 * costs one extra approver. A false negative is caught by nothing, which is why
 * the list is deliberately broad and matched on the key's presence, not its
 * value — setting `enabled: false` on the provider that currently serves paid
 * traffic is exactly as consequential as setting it true.
 */
export const PAID_TRAFFIC_KEYS = Object.freeze([
  "priority",
  "weight",
  "enabled",
  "isActive",
  "active",
  "fallbackOrder",
  "routingPriority",
  "billing",
  "billingMode",
  "costTier",
  "payg",
  "quota",
  "usageLimits",
  "rateLimit",
]);

/** Recursively test whether any paid-traffic key appears anywhere in the body. */
export function touchesPaidTraffic(value, depth = 0) {
  // Bounded so a hostile or cyclic-looking body cannot spin here. A body deeper
  // than this is refused by `assertPlainJson` before it ever reaches us.
  if (depth > 8 || value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((item) => touchesPaidTraffic(item, depth + 1));
  for (const [key, nested] of Object.entries(value)) {
    if (PAID_TRAFFIC_KEYS.includes(key)) return true;
    if (touchesPaidTraffic(nested, depth + 1)) return true;
  }
  return false;
}

/**
 * Body keys that say WHERE a provider sends traffic, and so where OmniRoute
 * sends the credential it stores for that provider.
 *
 * A partial PUT keeps the stored key. Re-pointing the upstream of an existing
 * provider therefore hands that key to the new host, which makes an endpoint
 * change at least as consequential as a paid-traffic change. Same one-way
 * tripwire: presence anywhere in the body escalates to DUAL.
 *
 * `providerSpecificData` is listed whole because its contents are per-provider
 * and are where OmniRoute keeps `baseUrl`. The name pattern catches the
 * spellings an allowlisted object may carry inside it.
 */
export const ENDPOINT_KEYS = Object.freeze(["providerSpecificData"]);
export const ENDPOINT_KEY_PATTERN = /(url|uri|host|endpoint|proxy|apibase|origin|domain|address)/i;

/** Recursively test whether any endpoint-bearing key appears anywhere in the body. */
export function touchesEndpoint(value, depth = 0) {
  if (depth > 8 || value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((item) => touchesEndpoint(item, depth + 1));
  for (const [key, nested] of Object.entries(value)) {
    if (ENDPOINT_KEYS.includes(key) || ENDPOINT_KEY_PATTERN.test(key)) return true;
    if (touchesEndpoint(nested, depth + 1)) return true;
  }
  return false;
}

/**
 * Body keys that carry a credential. Refused on every verb, at every approval
 * class: a credential in a body would sit in plugin state as a pending proposal
 * and be shown to the approver, and the broker exists so that credentials do not
 * travel through agents. Setting or rotating an upstream key stays an operator
 * action.
 */
export const CREDENTIAL_KEY_PATTERN = /(key|token|secret|passw|credential|authori[sz]ation|^auth$|cookie|^headers?$)/i;

/** Return the path of the first credential-named key in the body, or null. */
export function findCredentialKey(value, path = "", depth = 0) {
  if (depth > 8 || value === null || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const [index, item] of value.entries()) {
      const found = findCredentialKey(item, `${path}[${index}]`, depth + 1);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value)) {
    const here = path ? `${path}.${key}` : key;
    if (CREDENTIAL_KEY_PATTERN.test(key)) return here;
    const found = findCredentialKey(nested, here, depth + 1);
    if (found) return found;
  }
  return null;
}

/** Look up a verb by exact name. Unknown verbs are refused, never forwarded. */
export function resolveVerb(name) {
  if (typeof name !== "string" || name.length === 0) {
    throw new VerbError("A verb name is required.", 400);
  }
  // Exact equality against own properties only. `Object.hasOwn` matters:
  // "constructor" and "toString" are truthy on a plain object's prototype and
  // would otherwise resolve to a function here.
  if (!Object.hasOwn(VERBS, name)) {
    throw new VerbError(
      `Unknown verb "${name}". This broker performs only its declared verbs; ` +
        `it is not a proxy for the management API.`,
      404,
    );
  }
  return { name, ...VERBS[name] };
}

/**
 * Decide the approval class actually required for this call.
 *
 * The declared class is a floor, never a ceiling: the tripwire may raise
 * SINGLE to DUAL, and nothing here can lower a class.
 */
export function classifyApproval(verb, body) {
  if (verb.approval === APPROVAL.NONE) {
    // A read cannot be escalated and cannot be de-escalated. Reads carry no
    // body at all (`buildRequest` refuses one), so the tripwire is not consulted.
    return { approval: APPROVAL.NONE, escalated: false, reason: "read" };
  }

  if (verb.paidTraffic) {
    return {
      approval: APPROVAL.DUAL,
      escalated: verb.approval !== APPROVAL.DUAL,
      reason: "verb is declared as changing paid-traffic routing",
    };
  }

  if (verb.approval === APPROVAL.DUAL) {
    return { approval: APPROVAL.DUAL, escalated: false, reason: "verb is declared dual-approval" };
  }

  if (touchesEndpoint(body)) {
    return {
      approval: APPROVAL.DUAL,
      escalated: true,
      reason:
        "request body sets an upstream endpoint field, which decides where the provider's stored credential is sent " +
        `(${ENDPOINT_KEYS.join(", ")}, or a key matching ${ENDPOINT_KEY_PATTERN})`,
    };
  }

  if (touchesPaidTraffic(body)) {
    return {
      approval: APPROVAL.DUAL,
      escalated: true,
      reason:
        "request body sets a field that can change which provider serves paid traffic " +
        `(one of: ${PAID_TRAFFIC_KEYS.join(", ")})`,
    };
  }

  return { approval: APPROVAL.SINGLE, escalated: false, reason: "verb is declared single-approval" };
}

/**
 * Reject anything that is not a plain, finite, bounded JSON object before it is
 * inspected or forwarded. Guards the tripwire (which walks the body) and the
 * digest (which stringifies it) against prototype pollution and unbounded input.
 */
export function assertPlainJson(body, depth = 0) {
  if (body === null || body === undefined) return {};
  if (typeof body !== "object" || Array.isArray(body)) {
    throw new VerbError("Request body must be a JSON object.", 400);
  }
  if (depth === 0) {
    const serialized = JSON.stringify(body);
    if (serialized === undefined || serialized.length > 64_000) {
      throw new VerbError("Request body is not serialisable or is too large.", 413);
    }
  }
  if (depth > 8) throw new VerbError("Request body is nested too deeply.", 400);
  for (const key of Object.keys(body)) {
    if (key === "__proto__" || key === "constructor" || key === "prototype") {
      throw new VerbError(`Request body may not contain the key "${key}".`, 400);
    }
    const value = body[key];
    if (value !== null && typeof value === "object") assertPlainJson(value, depth + 1);
  }
  return body;
}

/* ────────────────────────────────────────────────────────────────────────────
 * Per-verb body constraints.
 *
 * These are SERVER-SIDE and there is no flag that disables them. They are the
 * reason the broker path is strictly narrower than the operator path it
 * replaces: a hand-run apply.sh trusts its input file and enforces none of this.
 * ──────────────────────────────────────────────────────────────────────────── */

/** Exactly the keys the shipped route accepts. Deny-by-default, as everywhere. */
export const MAPPING_BODY_KEYS = Object.freeze([
  "pattern",
  "comboId",
  "priority",
  "enabled",
  "description",
]);

/**
 * A mapping pattern is glob-matched by OmniRoute: the resolver escapes it, then
 * `*`->`.*` and `?`->`.`, anchors it, and compiles it CASE-INSENSITIVE. So a
 * single `*` can capture model ids nobody enumerated — including ids that do not
 * exist yet. Blast radius that cannot be reviewed is refused outright.
 */
export const MAPPING_WILDCARD = /[*?]/;

/**
 * Claude routing is this company's highest-stakes lever, and it is matched on the
 * FAMILY, not on the substring "claude": case_570ada89 was a bypass that a `claude`
 * check cleared, and 13 `aug/` ids carry no "claude" at all. Moving Claude
 * traffic stays an explicit operator action with owner visibility; it is not
 * something the broker will do for two agents.
 *
 * `prism` is here for a DIFFERENT reason than the family names, and the
 * difference is the whole lesson. This guard only ever sees the caller's pattern
 * STRING — it never holds the catalogue record — so any rule it can express is
 * id-shaped. `aug/prism-a` is `"name": "Prism (Claude + Gemini)"`: a blended
 * model whose Claude-ness lives in `name` and leaves no trace in the id. An
 * id-shaped rule cannot derive that; the token has to be enumerated. Found live
 * on 2026-08-25 by `preflight:case_e20effbb` against the real 1432-id catalogue — the
 * earlier 480-id fixture did not contain it, so a fixture-only run certified
 * "0 escaped" while this was open.
 *
 * Accepted over-block: `aug/prism-b` is `"Prism (GPT + Kimi)"` and carries no
 * Claude, but shares the token. case_05be2d4e's 52 planned patterns name neither
 * prism id, so refusing both costs nothing addressable today. Blocking a
 * non-Claude model is a recoverable annoyance; letting Claude traffic through
 * is the failure this guard exists to prevent.
 *
 * `mythos` is a current Claude family name. It is included even though the live
 * OmniRoute catalogue does not expose a Mythos id today: the guard must reject a
 * future provider alias such as `aug/mythos5` on first appearance, rather than
 * waiting for catalogue drift to turn it into another live bypass.
 *
 * If a future blended id shares the Prism shape, it must be added here too —
 * that class of model is not derivable from the id and will not announce itself.
 */
export const MAPPING_PROTECTED_FAMILY = /(claude|sonnet|opus|haiku|fable|mythos|prism)/i;

/** Matches the shipped route's zod: pattern is 1..500. */
const MAPPING_PATTERN_MAX = 500;

/**
 * Validate a `mappings.create` body. Throws `VerbError` on any violation.
 *
 * Note the priority rule is a REQUIREMENT, not a default. The route defaults it
 * to 0, and a silent 0 is a routing decision nobody made — mappings are ordered
 * `priority DESC`, so an omitted priority quietly loses to every existing
 * mapping. The caller must say what it means.
 */
export function assertMappingCreate(body) {
  const unknown = Object.keys(body).filter((key) => !MAPPING_BODY_KEYS.includes(key));
  if (unknown.length > 0) {
    throw new VerbError(
      `mappings.create accepts only ${MAPPING_BODY_KEYS.join(", ")}; refusing unknown key(s): ${unknown.join(", ")}.`,
      400,
    );
  }

  const { pattern, comboId, priority, enabled, description } = body;

  if (typeof pattern !== "string" || pattern.length === 0 || pattern.length > MAPPING_PATTERN_MAX) {
    throw new VerbError(`mappings.create requires "pattern" as a string of 1..${MAPPING_PATTERN_MAX} characters.`, 400);
  }
  if (MAPPING_WILDCARD.test(pattern)) {
    throw new VerbError(
      `mappings.create refuses wildcard patterns ("*" or "?"). Pattern ${JSON.stringify(pattern)} would match an ` +
        `open-ended set of model ids, including ids that do not exist yet. List the ids explicitly.`,
      400,
    );
  }
  if (MAPPING_PROTECTED_FAMILY.test(pattern)) {
    throw new VerbError(
      `mappings.create refuses patterns naming a protected model family (${MAPPING_PROTECTED_FAMILY.source}). ` +
        `Re-pointing Claude-family traffic is not a brokered operation at any approval level.`,
      403,
    );
  }

  if (typeof comboId !== "string" || comboId.trim().length === 0) {
    throw new VerbError('mappings.create requires "comboId" as a non-empty string.', 400);
  }
  if (!Number.isInteger(priority)) {
    throw new VerbError(
      'mappings.create requires an explicit integer "priority". Mappings are ordered by priority DESC; ' +
        "omitting it silently defaults to 0, which is a routing decision nobody made.",
      400,
    );
  }
  if (enabled !== undefined && typeof enabled !== "boolean") {
    throw new VerbError('mappings.create requires "enabled" to be a boolean when present.', 400);
  }
  if (description !== undefined && (typeof description !== "string" || description.length > 1000)) {
    throw new VerbError('mappings.create requires "description" to be a string of at most 1000 characters.', 400);
  }

  return body;
}

/**
 * Updatable top-level keys per provider/combo verb. Deny-by-default: a key
 * absent here is refused, not forwarded. Shapes are taken from the operator
 * script this broker replaces (omniroute/case_31532730-register-cliproxy.sh).
 *
 * `provider` is accepted on create and NOT on update: re-typing an existing
 * connection carries its stored key to a different upstream. Credential keys
 * (`apiKey`, …) appear in no list; `assertNoCredential` refuses them anyway, with
 * a clearer message.
 */
export const BODY_KEYS = Object.freeze({
  "providers.create": Object.freeze(["provider", "name", "isActive", "priority", "providerSpecificData"]),
  "providers.update": Object.freeze(["name", "isActive", "priority", "providerSpecificData"]),
  "providers.set-priority": Object.freeze(["priority"]),
  "combos.create": Object.freeze(["name", "description", "strategy", "models", "enabled"]),
  "combos.update": Object.freeze(["name", "description", "strategy", "models", "enabled"]),
  "providers.delete": Object.freeze([]),
  "combos.delete": Object.freeze([]),
  "mappings.delete": Object.freeze([]),
});

/** Keys of one combo leg (`models[]`). Same deny-by-default rule. */
export const COMBO_MEMBER_KEYS = Object.freeze(["kind", "model", "providerId", "connectionId", "weight"]);

function refuseUnknownKeys(verbName, object, allowed, where = "") {
  const unknown = Object.keys(object).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    const scope = where ? ` in ${where}` : "";
    const accepted = allowed.length > 0 ? `only ${allowed.join(", ")}` : "no body";
    throw new VerbError(
      `${verbName} accepts ${accepted}${scope}; refusing unknown key(s): ${unknown.join(", ")}.`,
      400,
    );
  }
}

/** Refuse a credential-named key anywhere in the body, at every approval class. */
export function assertNoCredential(verbName, body) {
  const found = findCredentialKey(body);
  if (found) {
    throw new VerbError(
      `${verbName} refuses credential field "${found}". The broker does not carry upstream credentials ` +
        "at any approval level; setting or rotating a provider key is an operator action.",
      403,
    );
  }
}

function keysOnly(verbName) {
  return (body) => refuseUnknownKeys(verbName, body, BODY_KEYS[verbName]);
}

function assertProviderBody(verbName) {
  return (body) => {
    refuseUnknownKeys(verbName, body, BODY_KEYS[verbName]);
    const psd = body.providerSpecificData;
    if (psd !== undefined && (psd === null || typeof psd !== "object" || Array.isArray(psd))) {
      throw new VerbError(`${verbName} requires "providerSpecificData" to be an object when present.`, 400);
    }
  };
}

function assertComboBody(verbName) {
  return (body) => {
    refuseUnknownKeys(verbName, body, BODY_KEYS[verbName]);
    if (body.models === undefined) return;
    if (!Array.isArray(body.models)) {
      throw new VerbError(`${verbName} requires "models" to be an array when present.`, 400);
    }
    for (const [index, member] of body.models.entries()) {
      if (member === null || typeof member !== "object" || Array.isArray(member)) {
        throw new VerbError(`${verbName} requires each "models" entry to be an object.`, 400);
      }
      refuseUnknownKeys(verbName, member, COMBO_MEMBER_KEYS, `models[${index}]`);
      // An unpinned leg lets OmniRoute choose the connection, possibly one
      // another company configured. Pinning is what makes resources.js able to
      // check the leg, so it is required, not optional.
      if (typeof member.connectionId !== "string" || member.connectionId.length === 0) {
        throw new VerbError(
          `${verbName} requires "connectionId" on models[${index}]; an unpinned leg cannot be checked against your company's connections.`,
          400,
        );
      }
      if (member.kind !== undefined && member.kind !== "model") {
        throw new VerbError(`${verbName} accepts only kind "model" legs; models[${index}] has kind ${JSON.stringify(member.kind)}.`, 400);
      }
    }
  };
}

/**
 * Verb name -> body validator. EVERY mutating verb has one; `assertVerbConstraints`
 * refuses a mutating verb that does not, so a verb added later without an
 * allowlist fails closed instead of forwarding whatever it is given.
 */
const BODY_CONSTRAINTS = Object.freeze({
  "providers.create": assertProviderBody("providers.create"),
  "providers.update": assertProviderBody("providers.update"),
  "providers.set-priority": keysOnly("providers.set-priority"),
  "providers.delete": keysOnly("providers.delete"),
  "combos.create": assertComboBody("combos.create"),
  "combos.update": assertComboBody("combos.update"),
  "combos.delete": keysOnly("combos.delete"),
  "mappings.create": assertMappingCreate,
  "mappings.delete": keysOnly("mappings.delete"),
});

/**
 * Apply a verb's declared body constraint.
 *
 * Called from `buildRequest`, deliberately: `buildRequest` is on BOTH the propose
 * path and the approve path, so a constraint cannot be dodged by getting a body
 * stored as a proposal and then approved. Wiring it into either handler alone
 * would leave exactly that hole.
 */
export function assertVerbConstraints(verb, body) {
  const check = BODY_CONSTRAINTS[verb.name];
  if (!check) {
    throw new VerbError(`Verb "${verb.name}" declares no body allowlist; refusing to forward its body.`, 500);
  }
  assertNoCredential(verb.name, body ?? {});
  check(body ?? {});
  return body;
}

/** Path params are substituted, never concatenated. */
const SAFE_PARAM = /^[A-Za-z0-9_.-]{1,128}$/;

/**
 * Build the concrete upstream request for a verb.
 *
 * The caller influences exactly two things: the values of the declared path
 * params, and the body of a non-read verb. It never supplies a method, a path
 * template, a query string or a header.
 */
export function buildRequest(verb, { params = {}, body = null } = {}) {
  const declared = verb.params ?? [];
  let path = verb.path;

  for (const key of declared) {
    const raw = params?.[key];
    if (typeof raw !== "string" || !SAFE_PARAM.test(raw)) {
      throw new VerbError(
        `Verb "${verb.name}" requires path parameter "${key}" matching ${SAFE_PARAM}.`,
        400,
      );
    }
    path = path.replace(`:${key}`, encodeURIComponent(raw));
  }

  // Belt and braces: if a template placeholder survived, we would be about to
  // call a literal ":id" path. Fail closed rather than issue it.
  if (path.includes(":")) {
    throw new VerbError(`Verb "${verb.name}" has unsubstituted path parameters.`, 500);
  }

  const isRead = verb.method === "GET";
  if (isRead && body && Object.keys(body).length > 0) {
    throw new VerbError(`Verb "${verb.name}" is a read and does not take a body.`, 400);
  }

  const outgoing = isRead ? null : (body ?? {});
  if (!isRead) assertVerbConstraints(verb, outgoing);

  return { method: verb.method, path, body: outgoing };
}

export default {
  VERBS,
  APPROVAL,
  resolveVerb,
  classifyApproval,
  buildRequest,
  assertVerbConstraints,
  assertMappingCreate,
  assertNoCredential,
};
