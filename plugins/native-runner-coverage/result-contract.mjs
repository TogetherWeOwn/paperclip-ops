const VERSION = "native-runner-coverage-result.v1";
const STORE_KEYS = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];
const INSTRUCTIONS = {
  CORRECT_SUPPLIED_METADATA: "Correct supplied metadata only from facts already available within the authorized scope; never invent missing observations.",
  USE_AUTHORIZED_NATIVE_SURFACE: "Use only an already-authorized native metadata surface if it exposes the missing fact; otherwise keep it UNKNOWN and report the unsupported boundary.",
  PRESERVE_UNKNOWN_AND_REPORT: "Preserve the classification, keep unobserved facts UNKNOWN, and report the unsupported boundary without expanding access or changing configuration.",
};
const REASON_ACTION = {
  BACKEND_UNOBSERVABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  AUTHORITY_NOT_TESTED: "PRESERVE_UNKNOWN_AND_REPORT",
  CONTENT_NOT_SCANNED: "PRESERVE_UNKNOWN_AND_REPORT",
  COVERAGE_INCOMPLETE: "USE_AUTHORIZED_NATIVE_SURFACE",
  INVALID_INPUT: "CORRECT_SUPPLIED_METADATA",
  INVALID_OBSERVATION: "CORRECT_SUPPLIED_METADATA",
  INVALID_POLICY: "PRESERVE_UNKNOWN_AND_REPORT",
  COMPANY_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  AGENT_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  ADAPTER_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  RUN_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  SESSION_MISMATCH: "CORRECT_SUPPLIED_METADATA",
  MISSING_BINDING_METADATA: "CORRECT_SUPPLIED_METADATA",
  MISSING_SESSION_JOIN: "USE_AUTHORIZED_NATIVE_SURFACE",
  MISSING_RECEIPT_BINDING: "CORRECT_SUPPLIED_METADATA",
  UNSUPPORTED_EVIDENCE_BASIS: "CORRECT_SUPPLIED_METADATA",
  SOURCE_ONLY: "PRESERVE_UNKNOWN_AND_REPORT",
  SYNTHETIC_FIXTURE: "PRESERVE_UNKNOWN_AND_REPORT",
  REPORTED_NOT_MEASURED: "PRESERVE_UNKNOWN_AND_REPORT",
  STORE_SCOPE_UNOBSERVABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  DEFAULT_SCOPE_MISS_NOT_GLOBAL_ABSENCE: "PRESERVE_UNKNOWN_AND_REPORT",
  RETAINED_PROVIDER_STATE: "PRESERVE_UNKNOWN_AND_REPORT",
  RETAINED_MCP_HEADER: "PRESERVE_UNKNOWN_AND_REPORT",
  MISSING_INVOCATION_COMPANY: "PRESERVE_UNKNOWN_AND_REPORT",
  CONFIG_UNAVAILABLE: "PRESERVE_UNKNOWN_AND_REPORT",
  INVOCATION_COMPANY_MISMATCH: "PRESERVE_UNKNOWN_AND_REPORT",
};

function freezeTree(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freezeTree);
    Object.freeze(value);
  }
  return value;
}

export const REMEDIATION_CATALOGUE = freezeTree(Object.fromEntries(
  Object.entries(REASON_ACTION).map(([reason, action]) => [reason, {
    action, instruction: INSTRUCTIONS[action],
  }]),
));

const reasons = Object.keys(REMEDIATION_CATALOGUE).sort();
const enumeration = (values) => ({ type: "string", enum: values });

/** Structural schema; reason/action correspondence is the catalogue's contract. */
export const RESULT_SCHEMA = freezeTree({
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: VERSION,
  type: "object",
  additionalProperties: false,
  required: [
    "status", "reasonCodes", "pathClass", "evidenceBasis", "lifecycle", "leaseState",
    "authority", "contentScan", "stores", "backend", "target", "bindingCheck",
    "schema_id", "nextActions",
  ],
  properties: {
    schema_id: { type: "string", const: VERSION },
    status: enumeration(["UNKNOWN", "ADVERSE", "REFUSED"]),
    reasonCodes: { type: "array", minItems: 1, uniqueItems: true, items: enumeration(reasons) },
    pathClass: enumeration(["UNKNOWN", "DOT_STARTUP_ONLY", "DOT_RETAINED", "CODEX_LOCAL_MCP_HEADER"]),
    evidenceBasis: enumeration(["metadata", "reported", "synthetic", "source"]),
    lifecycle: enumeration(["UNKNOWN", "live", "warm", "detached", "recovery", "terminal"]),
    leaseState: enumeration(["UNKNOWN", "unexpired_record", "expired_record", "revoked_record"]),
    authority: { type: "string", const: "UNKNOWN" },
    contentScan: { type: "string", const: "NOT_PERFORMED" },
    backend: { type: "string", const: "UNKNOWN" },
    target: enumeration(["UNKNOWN", "local", "remote", "custom"]),
    bindingCheck: enumeration(["REPORTED_CONSISTENT", "INCOMPLETE", "REFUSED"]),
    stores: {
      type: "object", additionalProperties: false, required: STORE_KEYS,
      properties: Object.fromEntries(STORE_KEYS.map((key) => [key,
        enumeration(["UNKNOWN", "PRESENT", "NOT_FOUND_IN_OBSERVED_SCOPE"])])),
    },
    nextActions: {
      type: "array", minItems: 1, uniqueItems: true,
      items: {
        type: "object", additionalProperties: false,
        required: ["reasonCode", "action", "instruction"],
        properties: {
          reasonCode: enumeration(reasons),
          action: enumeration(Object.keys(INSTRUCTIONS)),
          instruction: enumeration(Object.values(INSTRUCTIONS)),
        },
      },
    },
  },
});

/** Apply only to the final sanitized result, after every reason-code override. */
export function withResultContract(result) {
  return {
    ...result,
    schema_id: VERSION,
    nextActions: result.reasonCodes.map((reasonCode) => {
      const { action, instruction } = REMEDIATION_CATALOGUE[reasonCode];
      return { reasonCode, action, instruction };
    }),
  };
}
