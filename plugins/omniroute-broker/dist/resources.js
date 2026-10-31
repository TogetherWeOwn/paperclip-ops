/**
 * omniroute-broker — which company a provider, combo or mapping id belongs to.
 *
 * Pure w.r.t. decisions: the lookup is injected, so the rule can be tested with
 * a plain Map. Persistence lives in worker.js (`ctx.state.*`).
 *
 * ## Why this file exists
 *
 * ownership.js proves the caller holds ITS OWN ISSUE. That says nothing about
 * the id in the request: OmniRoute has one management plane for every company on
 * the box, and the broker's credential spans all of it. Without a binding, any
 * agent holding any issue could update or delete a provider another company
 * configured, or point its own combo at another company's connection.
 *
 * So a mutating verb may only name an id that is BOUND to the caller's company:
 *
 *   - created through this broker by that company (recorded on success), or
 *   - listed for that company by an operator in `adoptedResourceIds` config —
 *     the path for resources created before the broker, e.g. by apply.sh.
 *
 * Everything else is refused, including ids the broker has never seen. Deny by
 * default, as everywhere else in this plugin. The refusal does not say whether
 * the id exists or who holds it.
 *
 * Reads are not bound: they are ungated by the owner's line and every response
 * is scrubbed by redact.js.
 */

export const BOUND_RESOURCES = Object.freeze(["provider", "combo", "mapping"]);

export class ResourceBindingError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ResourceBindingError";
    this.status = status;
  }
}

/** Plugin-state key of a binding record. */
export const bindingKey = (resource, id) => `binding:${resource}:${id}`;

/**
 * List every bound-resource id a mutating call names, with the resource class
 * it must be bound as.
 *
 *   - the `:id` path param of an update, priority change or delete
 *   - `models[].connectionId` of a combo body (a leg on another company's connection)
 *   - `comboId` of a mapping body (routing traffic into another company's combo)
 *
 * `models[].providerId` names a provider NODE, which no verb here creates or
 * edits, so there is nothing to bind it to; the connection id is what carries
 * the stored credential.
 */
export function referencedResources(verb, { params = {}, body = null } = {}) {
  if (verb.method === "GET") return [];
  const refs = [];

  if ((verb.params ?? []).includes("id") && BOUND_RESOURCES.includes(verb.resource)) {
    refs.push({ resource: verb.resource, id: params?.id, field: ":id" });
  }

  if (verb.resource === "combo" && Array.isArray(body?.models)) {
    for (const [index, member] of body.models.entries()) {
      if (member && member.connectionId !== undefined) {
        refs.push({ resource: "provider", id: member.connectionId, field: `models[${index}].connectionId` });
      }
    }
  }

  if (verb.resource === "mapping" && body && body.comboId !== undefined) {
    refs.push({ resource: "combo", id: body.comboId, field: "comboId" });
  }

  return refs;
}

/**
 * Refuse unless every id the call names is bound to `companyId`.
 *
 * @param lookupBinding async (resource, id) => stored record or null
 * @param adopted       the company's `adoptedResourceIds` config, or undefined
 */
export async function assertResourcesBound({ companyId, verb, params, body, lookupBinding, adopted }) {
  if (!companyId) throw new ResourceBindingError("Company id required to check resource binding.", 403);

  for (const ref of referencedResources(verb, { params, body })) {
    if (typeof ref.id !== "string" || ref.id.length === 0) {
      throw new ResourceBindingError(`${verb.name}: "${ref.field}" must be a non-empty string id.`, 400);
    }
    const adoptedIds = adopted?.[ref.resource];
    if (Array.isArray(adoptedIds) && adoptedIds.includes(ref.id)) continue;

    const record = await lookupBinding(ref.resource, ref.id);
    if (!record || record.companyId !== companyId) {
      throw new ResourceBindingError(
        `${verb.name}: ${ref.resource} "${ref.id}" (${ref.field}) is not bound to your company. ` +
          "The broker mutates only resources your company created through it, or that an operator " +
          "listed for your company in adoptedResourceIds.",
        403,
      );
    }
  }
}

/** Where OmniRoute may put the created record, per resource. */
const CREATE_ENVELOPES = Object.freeze({
  provider: ["connection", "provider"],
  combo: ["combo"],
  mapping: ["mapping"],
});

const SAFE_ID = /^[A-Za-z0-9_.-]{1,128}$/;

/** Read the id of a just-created record, or null if the response does not carry one. */
export function extractCreatedId(payload, resource) {
  if (!payload || typeof payload !== "object") return null;
  const candidates = [payload, ...(CREATE_ENVELOPES[resource] ?? []).map((key) => payload[key])];
  for (const candidate of candidates) {
    const id = candidate && typeof candidate === "object" ? candidate.id : undefined;
    if (typeof id === "string" && SAFE_ID.test(id)) return id;
    if (Number.isInteger(id)) return String(id);
  }
  return null;
}

/**
 * What a successful call does to the binding table: a create binds the new id
 * to the caller's company, a delete releases the id. Anything else changes
 * nothing. Returns null when there is nothing to write.
 */
export function bindingChange({ verb, params, payload, companyId, issueId, agentId, now }) {
  if (!BOUND_RESOURCES.includes(verb.resource)) return null;

  if (verb.method === "POST" && !(verb.params ?? []).length) {
    const id = extractCreatedId(payload, verb.resource);
    if (!id) return { action: "unbound", resource: verb.resource, id: null };
    return {
      action: "bind",
      resource: verb.resource,
      id,
      record: {
        companyId,
        resource: verb.resource,
        id,
        issueId: issueId ?? null,
        agentId: agentId ?? null,
        boundAt: new Date(now).toISOString(),
      },
    };
  }

  if (verb.method === "DELETE") {
    return { action: "release", resource: verb.resource, id: params?.id ?? null };
  }

  return null;
}

export default { assertResourcesBound, referencedResources, bindingChange, extractCreatedId, bindingKey };
