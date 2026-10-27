import { withResultContract } from "./result-contract.mjs";

const STORE_KEYS = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];
const LIFECYCLES = ["live", "warm", "detached", "recovery", "terminal"];
const BASE_REASONS = ["BACKEND_UNOBSERVABLE", "AUTHORITY_NOT_TESTED", "CONTENT_NOT_SCANNED"];
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const identifier = (value) => typeof value === "string" && value.length > 0
  && value.length <= 200 && value.trim() === value && !/[\s\x00-\x1f\x7f]/.test(value);

function result(status, reason) {
  return {
    status,
    reasonCodes: [...BASE_REASONS, reason],
    pathClass: "UNKNOWN",
    evidenceBasis: "metadata",
    lifecycle: "UNKNOWN",
    leaseState: "UNKNOWN",
    authority: "UNKNOWN",
    contentScan: "NOT_PERFORMED",
    stores: Object.fromEntries(STORE_KEYS.map((key) => [key, "UNKNOWN"])),
    backend: "UNKNOWN",
    target: "UNKNOWN",
    bindingCheck: status === "REFUSED" ? "REFUSED" : "INCOMPLETE",
  };
}

function timestamp(value) {
  if (typeof value !== "string"
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)) return null;
  const parsed = Date.parse(value);
  // Date.parse silently normalizes invalid calendar dates; do not admit them.
  return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 19) === value.slice(0, 19)
    ? parsed : null;
}

function leaseState(lease, now) {
  const at = timestamp(now);
  if (!record(lease) || at === null) return "UNKNOWN";
  if (lease.revoked === true) return "revoked_record";
  const expiry = timestamp(lease.expiresAt);
  if (lease.revoked !== false || expiry === null) return "UNKNOWN";
  return expiry <= at ? "expired_record" : "unexpired_record";
}

/** Pure classification of reported metadata, not independent binding authentication. */
export function classifyCoverage(input) {
  return withResultContract(classifyMetadata(input));
}

function classifyMetadata(input) {
  if (!record(input)) return result("REFUSED", "INVALID_INPUT");
  const { scope, binding, agent, run, observation, now } = input;
  if (!record(scope) || !identifier(scope.companyId) || !Array.isArray(scope.agentIds)
      || scope.agentIds.length !== 3 || !scope.agentIds.every(identifier)
      || new Set(scope.agentIds).size !== 3) return result("REFUSED", "INVALID_POLICY");
  if (!record(binding) || !record(agent) || !record(run)) {
    return result("UNKNOWN", "MISSING_BINDING_METADATA");
  }

  // Check every available conflicting identifier before considering partial evidence.
  const companyIds = [binding.companyId, agent.companyId, run.companyId];
  if (companyIds.some((id) => identifier(id) && id !== scope.companyId)) {
    return result("REFUSED", "COMPANY_MISMATCH");
  }
  const agentIds = [binding.agentId, agent.id, run.agentId];
  if (agentIds.some((id) => identifier(id) && !scope.agentIds.includes(id))
      || new Set(agentIds.filter(identifier)).size > 1) {
    return result("REFUSED", "AGENT_MISMATCH");
  }
  if (identifier(binding.runId) && identifier(run.id) && binding.runId !== run.id) {
    return result("REFUSED", "RUN_MISMATCH");
  }
  if (agent.adapterType !== undefined && agent.adapterType !== "paperclip_runner") {
    return result("REFUSED", "ADAPTER_MISMATCH");
  }
  if (identifier(binding.sessionId) && identifier(run.sessionId) && binding.sessionId !== run.sessionId) {
    return result("REFUSED", "SESSION_MISMATCH");
  }
  if (![...companyIds, ...agentIds, binding.runId, run.id].every(identifier)
      || agent.adapterType !== "paperclip_runner") {
    return result("UNKNOWN", "MISSING_BINDING_METADATA");
  }

  const output = result("UNKNOWN", "COVERAGE_INCOMPLETE");
  const joinedSession = identifier(binding.sessionId) && identifier(run.sessionId);
  if (joinedSession) output.bindingCheck = "REPORTED_CONSISTENT";
  if (observation === undefined) return output;
  if (!record(observation)) return result("UNKNOWN", "INVALID_OBSERVATION");

  const receipt = observation.binding;
  if (!record(receipt)) return result("UNKNOWN", "MISSING_RECEIPT_BINDING");
  for (const [key, expected, code] of [
    ["companyId", scope.companyId, "COMPANY_MISMATCH"],
    ["agentId", binding.agentId, "AGENT_MISMATCH"],
    ["runId", binding.runId, "RUN_MISMATCH"],
    ["sessionId", binding.sessionId, "SESSION_MISMATCH"],
  ]) {
    if (identifier(receipt[key]) && identifier(expected) && receipt[key] !== expected) {
      return result("REFUSED", code);
    }
  }
  if (!joinedSession || ![receipt.companyId, receipt.agentId, receipt.runId, receipt.sessionId].every(identifier)) {
    return result("UNKNOWN", "MISSING_SESSION_JOIN");
  }
  if (!["reported", "synthetic", "source"].includes(observation.basis)) {
    return result("UNKNOWN", "UNSUPPORTED_EVIDENCE_BASIS");
  }
  output.evidenceBasis = observation.basis;
  output.reasonCodes.push(observation.basis === "synthetic" ? "SYNTHETIC_FIXTURE"
    : observation.basis === "source" ? "SOURCE_ONLY" : "REPORTED_NOT_MEASURED");
  if (observation.basis === "source") return output;

  if (["local", "remote", "custom"].includes(observation.target)) output.target = observation.target;
  if (LIFECYCLES.includes(observation.lifecycle)) output.lifecycle = observation.lifecycle;
  // A terminal run is not a provider close; only an explicit lifecycle report is used.
  output.leaseState = leaseState(observation.lease, now);
  if (observation.target !== "local") {
    output.reasonCodes.push("STORE_SCOPE_UNOBSERVABLE");
    return output;
  }
  if (!["dot", "codex"].includes(observation.provider) || !record(observation.stores)) return output;

  for (const key of STORE_KEYS) {
    const state = observation.stores[key];
    output.stores[key] = state === "present" ? "PRESENT"
      : state === "not_found" ? "NOT_FOUND_IN_OBSERVED_SCOPE" : "UNKNOWN";
  }
  if (output.stores.mcpHeader === "PRESENT") {
    output.status = "ADVERSE";
    output.reasonCodes.push("RETAINED_MCP_HEADER");
    if (observation.provider === "codex") output.pathClass = "CODEX_LOCAL_MCP_HEADER";
  }
  if (observation.provider === "dot") {
    if (output.stores.journal === "PRESENT" && output.stores.providerState === "PRESENT") {
      output.pathClass = "DOT_RETAINED";
      output.reasonCodes.push("RETAINED_PROVIDER_STATE");
    } else if (output.stores.controller === "PRESENT"
        && output.stores.journal === "NOT_FOUND_IN_OBSERVED_SCOPE") {
      output.pathClass = "DOT_STARTUP_ONLY";
      output.reasonCodes.push("DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE");
    }
  }
  return output;
}
