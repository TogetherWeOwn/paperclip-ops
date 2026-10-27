import { analyzeSnapshot } from './analytics.mjs';
import { manifest } from './manifest.mjs';

/** SDK-free registration so repo tests can drive the real handler. */
export function registerOutcomeCostTool(ctx) {
  ctx.tools.register('analyze_outcome_costs', manifest.tools[0], async (args, toolCtx) => {
    if (!toolCtx?.companyId) return { content: 'Company scope is required.', data: { ok: false, reason: 'missing-company-scope' } };
    try {
      const report = analyzeSnapshot(args?.snapshot, toolCtx.companyId);
      return {
        content: `${report.totals.outcomes} caller-attested delivered outcomes across ${report.totals.runs} runs. ` +
          `Known spend USD ${report.totals.knownCostUsd}; ${report.totals.unknownCostRuns} run costs unknown. ` +
          `All-attempt cost per outcome USD ${report.totals.costPerOutcomeUsd ?? 'unknown'}. ` +
          'Passive snapshot analysis, not independently verified delivery or a routing decision.',
        data: { ok: true, ...report },
      };
    } catch {
      // Never echo untrusted snapshots, provider payloads or identifiers on a refusal.
      return { content: 'Invalid, inconsistent, oversized or cross-company snapshot. Nothing was changed.', data: { ok: false, reason: 'invalid-snapshot' } };
    }
  });
}
