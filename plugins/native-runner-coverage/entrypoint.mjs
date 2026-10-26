import { definePlugin, runWorker } from "@paperclipai/plugin-sdk";
import { registerCoverageTool } from "./worker.mjs";

const plugin = definePlugin({
  async setup(ctx) {
    registerCoverageTool(ctx);
  },
});

export default plugin;
runWorker(plugin, import.meta.url);
