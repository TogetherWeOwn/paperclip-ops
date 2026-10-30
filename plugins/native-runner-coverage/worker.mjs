import { classifyCoverage } from "./classifier.mjs";
import { toolDeclaration } from "./manifest.mjs";
import { withResultContract } from "./result-contract.mjs";

const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const identifier = (value) => typeof value === "string" && value.length > 0
  && value.length <= 200 && value.trim() === value && !/[\s\x00-\x1f\x7f]/.test(value);
const response = (data) => ({ content: JSON.stringify(data), data });

function failure(status, reason) {
  const data = classifyCoverage(null);
  data.status = status;
  data.reasonCodes = ["BACKEND_UNOBSERVABLE", "AUTHORITY_NOT_TESTED", "CONTENT_NOT_SCANNED", reason];
  data.bindingCheck = status === "REFUSED" ? "REFUSED" : "INCOMPLETE";
  return response(withResultContract(data));
}

/** Registration has no unscoped config read; every invocation resolves its company. */
export function registerCoverageTool(ctx) {
  ctx.tools.register(toolDeclaration.name, toolDeclaration, async (params, runCtx) => {
    if (!identifier(runCtx?.companyId)) return failure("REFUSED", "MISSING_INVOCATION_COMPANY");
    let scope;
    try {
      scope = await ctx.config.get(runCtx.companyId);
    } catch {
      // Config errors may contain sensitive host context. Never return or log them.
      return failure("UNKNOWN", "CONFIG_UNAVAILABLE");
    }
    if (!record(scope) || scope.companyId !== runCtx.companyId) {
      return failure("REFUSED", "INVOCATION_COMPANY_MISMATCH");
    }
    if (!record(params)) return failure("REFUSED", "INVALID_INPUT");
    let observation = params.observation;
    if (record(observation)) {
      observation = {
        binding: observation.binding,
        // No caller assertion can become independently measured evidence. Preserve
        // source-only inference rather than promote it to a reported store sighting.
        basis: observation.basis === "source" ? "source" : "reported",
        provider: observation.provider,
        target: observation.target,
        lifecycle: observation.lifecycle,
        stores: observation.stores,
        lease: observation.lease,
      };
    }
    return response(classifyCoverage({
      scope: { companyId: scope.companyId, agentIds: scope.agentIds },
      binding: params.binding,
      agent: params.agent,
      run: params.run,
      observation,
      now: new Date().toISOString(),
    }));
  });
}
