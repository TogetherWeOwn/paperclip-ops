const id = { type: "string", minLength: 1, maxLength: 200 };
const binding = {
  type: "object", additionalProperties: false,
  required: ["companyId", "agentId", "runId"],
  properties: { companyId: id, agentId: id, runId: id, sessionId: id },
};
const storeNames = [
  "controller", "journal", "providerState", "mcpHeader",
  "transcript", "sqlite", "runLog", "optionalTrace",
];

export const toolDeclaration = {
  name: "native_runner_coverage",
  displayName: "Classify native Runner coverage",
  description: "Classify reported, sanitized native metadata under a fixed company/agent policy. Returns native-runner-coverage-result.v1 with fixed nextActions; schema and remediation catalogue: result-contract.mjs. No content scan, independent measurement, safety-green verdict, or new access authority.",
  parametersSchema: {
    type: "object", additionalProperties: false,
    required: ["binding", "agent", "run"],
    properties: {
      binding,
      agent: {
        type: "object", additionalProperties: false,
        required: ["id", "companyId", "adapterType"],
        properties: { id, companyId: id, adapterType: { const: "paperclip_runner" } },
      },
      run: {
        type: "object", additionalProperties: false,
        required: ["id", "companyId", "agentId", "status"],
        properties: { id, companyId: id, agentId: id, status: { type: "string", maxLength: 50 }, sessionId: id },
      },
      observation: {
        type: "object", additionalProperties: false,
        required: ["binding", "basis"],
        properties: {
          binding,
          basis: { type: "string", enum: ["reported", "source"] },
          provider: { type: "string", enum: ["dot", "codex"] },
          target: { type: "string", enum: ["local", "remote", "custom"] },
          lifecycle: { type: "string", enum: ["live", "warm", "detached", "recovery", "terminal"] },
          stores: {
            type: "object", additionalProperties: false,
            properties: Object.fromEntries(storeNames.map((key) => [key, {
              type: "string", enum: ["present", "not_found", "unknown"],
            }])),
          },
          lease: {
            type: "object", additionalProperties: false,
            properties: { expiresAt: { type: "string", maxLength: 30 }, revoked: { type: "boolean" } },
          },
        },
      },
    },
  },
};

export default {
  id: "togetherweown.native-runner-coverage",
  apiVersion: 1,
  version: "0.1.0",
  displayName: "Native Runner coverage classifier",
  description: "Source-only, report-only classifier. Missing native observations remain UNKNOWN; no store access or credential remediation.",
  author: "TogetherWeOwn",
  categories: ["automation"],
  capabilities: ["agent.tools.register"],
  entrypoints: { worker: "./entrypoint.mjs" },
  instanceConfigSchema: {
    type: "object", additionalProperties: false,
    required: ["companyId", "agentIds"],
    properties: {
      companyId: id,
      agentIds: { type: "array", minItems: 3, maxItems: 3, uniqueItems: true, items: id },
    },
  },
  tools: [toolDeclaration],
};
