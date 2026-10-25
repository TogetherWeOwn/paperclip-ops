export const manifest = {
  id: 'togetherweown.trace-cost-analytics',
  apiVersion: 1,
  version: '0.1.0',
  displayName: 'Trace and Outcome Costs',
  description: 'Passive all-attempt outcome cost and trace diagnostics for caller-supplied native snapshots. No collection, inference or routing changes.',
  author: 'TogetherWeOwn',
  categories: ['automation'],
  capabilities: ['agent.tools.register'],
  entrypoints: { worker: './src/worker.mjs' },
  tools: [{
    name: 'analyze_outcome_costs',
    displayName: 'Analyze outcome costs',
    description: 'Analyze a bounded native snapshot. Outcome and trace evidence is caller-attested, not independently verified. Read-only; no model calls or data fetching.',
    parametersSchema: {
      type: 'object', additionalProperties: false, required: ['snapshot'],
      properties: { snapshot: { type: 'object', description: 'Version 1 snapshot documented in the plugin README. Must match the host company scope.' } },
    },
  }],
};
export default manifest;
