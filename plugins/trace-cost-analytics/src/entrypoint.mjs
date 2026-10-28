import { definePlugin, runWorker } from '@paperclipai/plugin-sdk';
import { registerOutcomeCostTool } from './worker.mjs';

const plugin = definePlugin({
  async setup(ctx) {
    registerOutcomeCostTool(ctx);
  },
});

export default plugin;
runWorker(plugin, import.meta.url);
