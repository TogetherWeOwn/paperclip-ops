import type { PaperclipPluginManifestV1 } from "@paperclipai/plugin-sdk";

import { CLIPROXY_INSIGHT_CONFIG_SCHEMA } from "./config/schema.js";
import { JOB_KEYS, PLUGIN_API_VERSION, PLUGIN_ID, PLUGIN_VERSION, ROUTE_KEYS, TOOL_NAMES } from "./constants.js";

/**
 * cliproxy-insight (case_22968ca3).
 *
 * Turns CLIProxy's in-memory-only usage/cooldown telemetry into durable
 * Paperclip history. Polls the sanitized telemetry lane the operator stood up
 * on 2026-09-05 (case_daf05f17) — `request-rates.json` for per-provider success/
 * failed counters and `model-usage-v1.json` for per-model capacity windows —
 * persists each observation, and surfaces it to the board UI and to agents.
 *
 * v0.2.0 re-points the plugin from CLIProxy to that lane. The three things
 * v0.1.0 had to guess are now measured, and all three were wrong: the lane
 * authenticates with `x-api-key` (not `Authorization: Bearer`), it serves two
 * named static files (not one invented `/usage-summary` aggregate), and its
 * provider keys include `codex`/`codex-spark` — which the old hardcoded
 * six-provider allowlist would have silently discarded.
 *
 * **The owner-reserved gate this card carried no longer applies.** case_22968ca3
 * required owner approval to place the CLIProxy management key as a Paperclip
 * secret, because that key can read `/v0/management/auth-files` and return
 * provider credentials in clear. The lane design removes the need: a host-side
 * collector holds the management key and publishes sanitized JSON, and the
 * only credential Paperclip holds is a lane bearer that can read two static
 * files and reach nothing else. The management key is not placed, contained,
 * or projected — it never enters Paperclip.
 *
 * Read-only forever: never writes CLIProxy config, never triggers an account
 * action. Still ships with `pollingEnabled: false` — installing is not
 * enabling, the same sequencing dispatch (`wakeEnabled`) and model-selection
 * (`mode: "advise"`) both used.
 */
export const manifest: PaperclipPluginManifestV1 = {
  id: PLUGIN_ID,
  apiVersion: PLUGIN_API_VERSION,
  version: PLUGIN_VERSION,
  displayName: "CLIProxy Insight",
  description:
    "Durable per-upstream-provider usage/cooldown telemetry from the sanitized CLIProxy lane, plus per-model capacity windows. Read-only forever.",
  author: "CTO & Chief AI Officer (Paperclip)",
  categories: ["automation"],

  capabilities: [
    // The substrate. Every poll runs from here.
    "jobs.schedule",
    // Outbound GET to the public HTTPS telemetry lane.
    "http.outbound",
    // Resolve the lane bearer at call time only — never cached, logged, or
    // written to state (ADR-0004 discipline).
    "secrets.read-ref",
    // Durable snapshot + cooldown-event persistence — this IS the deliverable:
    // Paperclip becomes the history neither CLIProxy nor the lane keeps.
    "plugin.state.read",
    "plugin.state.write",
    // Weekly-scorecard metrics, every firing (dispatch precedent: a gauge
    // that stops being written is indistinguishable from a healthy zero).
    "metrics.write",
    // ctx.activity.log() on poll failure, cooldown transition, and change.
    "activity.log.write",
    // get_provider_usage, so an agent can consult quota state before a burst.
    "agent.tools.register",
    // Usage summary surface for the board UI.
    "api.routes.register",
  ],

  entrypoints: { worker: "./dist/worker.js" },

  jobs: [
    {
      jobKey: JOB_KEYS.poll,
      displayName: "CLIProxy usage poll",
      description:
        "Reads the sanitized telemetry lane and persists per-provider counters and per-model capacity windows. No-op (heartbeat metric only) while config.pollingEnabled is false or no secret is configured.",
      // The host collector republishes every 2 minutes. Polling at */5 keeps
      // history meaningfully fresh without ever retrying inside a firing —
      // a tight retry loop is how a poller IP-bans itself (case_22968ca3 recon).
      schedule: "*/5 * * * *",
    },
  ],

  instanceConfigSchema: CLIPROXY_INSIGHT_CONFIG_SCHEMA as unknown as Record<string, unknown>,

  tools: [
    {
      name: TOOL_NAMES.getCollectorEvidence,
      displayName: "Get collector evidence",
      description:
        "Read company-scoped aggregate snapshot counts and JSON byte lengths for a bounded observation window, without provider identities or raw records. Missing lifecycle and diagnostic instrumentation remain unavailable. Never polls or changes the collector.",
      parametersSchema: {
        type: "object",
        additionalProperties: false,
        required: ["companyId"],
        properties: {
          companyId: { type: "string", minLength: 1 },
          windowSeconds: { type: "integer", minimum: 60, maximum: 86400, default: 3600 },
        },
      },
    },
    {
      name: TOOL_NAMES.getProviderUsage,
      displayName: "Get provider usage",
      description:
        "Read the most recently persisted CLIProxy usage state: per-provider success/failed counters, cooldown history, and per-model capacity windows, each flagged stale when older than the freshness budget. Optionally select one persisted lane to return only that lane's stored history; provider snapshots and modelUsage keep their existing scope. Stored history only — not serving readiness or an admission decision. Read-only; consults stored history, never calls CLIProxy.",
      parametersSchema: {
        type: "object",
        required: ["companyId"],
        properties: {
          companyId: { type: "string", description: "Company whose CLIProxy history applies" },
          provider: {
            type: "string",
            description:
              "Optional. One provider key as published by the lane (e.g. claude, codex, codex-spark, kimi, opencode-go). Omit to return every provider observed so far.",
          },
          lane: {
            type: "string",
            description:
              "Optional. One exact lane id from the persisted lane index — an entry in `lanes` from an unfiltered call (e.g. claude.json). Omit to return every persisted lane. Unknown ids are rejected as unknown_lane; a provider name is not a lane id.",
          },
        },
      },
    },
  ],

  apiRoutes: [
    {
      routeKey: ROUTE_KEYS.usageSummary,
      method: "GET",
      path: "/usage-summary",
      auth: "board-or-agent",
      capability: "api.routes.register",
      checkoutPolicy: "none",
      companyResolution: { from: "query", key: "companyId" },
    },
  ],
};

export default manifest;
