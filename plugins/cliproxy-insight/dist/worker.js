// src/worker.ts
import { definePlugin, runWorker } from "@paperclipai/plugin-sdk";

// src/config/secret-ref.ts
var ALLOWED_KEYS = /* @__PURE__ */ new Set([
  "type",
  "secretId",
  "version",
  "projectionClass",
  "projectionAllowlistKey"
]);
var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function validateSecretRefShape(value, path) {
  if (value === null || value === void 0) return null;
  if (typeof value === "string") {
    return `${path} must be a Paperclip secret reference object, not a string. A pasted credential is never stored \u2014 use the secret picker, which submits { type: "secret_ref", secretId }.`;
  }
  if (!isRecord(value)) {
    return `${path} must be an object of the form { type: "secret_ref", secretId } or null`;
  }
  if (value.type !== "secret_ref") {
    return `${path} is not a secret reference: it must be { type: "secret_ref", secretId, version? }. An object holding a credential value would be stored in this company's config in clear.`;
  }
  if (typeof value.secretId !== "string" || !UUID.test(value.secretId)) {
    return `${path}.secretId must be the UUID of a Paperclip secret`;
  }
  if (value.version !== void 0 && value.version !== "latest" && !(typeof value.version === "number" && Number.isInteger(value.version) && value.version > 0)) {
    return `${path}.version must be "latest" or a positive integer`;
  }
  const extra = Object.keys(value).filter((key) => !ALLOWED_KEYS.has(key));
  if (extra.length > 0) {
    return `${path} carries unexpected field(s): ${extra.sort().join(", ")}. A secret reference holds no value, only a pointer.`;
  }
  return null;
}

// src/constants.ts
var PLUGIN_ID = "togetherweown.cliproxy-insight";
var PLUGIN_VERSION = "0.3.1";
var SEED_PROVIDER_ORDER = [
  "claude",
  "codex",
  "codex-spark",
  "openai",
  "antigravity",
  "opencode-go",
  "xai",
  "kimi"
];
var TOOL_NAMES = {
  /** Read-only: current usage/cooldown state for one or all providers. */
  getProviderUsage: "get_provider_usage",
  /** Counts-only evidence; never returns provider or lane records. */
  getCollectorEvidence: "get_collector_evidence"
};
var ROUTE_KEYS = {
  usageSummary: "usage-summary"
};
var JOB_KEYS = {
  poll: "cliproxy-poll"
};
var LANE_PATHS = {
  requestRates: "request-rates.json",
  modelUsage: "model-usage-v1.json"
};
var LANE_AUTH_HEADER = "x-api-key";
var DEFAULT_LANE_FILES = [
  "claude.json",
  "codex.json",
  "kimi.json",
  "opencode-go.json",
  "zai.json",
  "antigravity.json"
];
var LANE_COOLDOWN_FIELDS = [
  "exhausted_until",
  "exhaustedUntil",
  "cooldown_until",
  "cooldownUntil",
  "rate_limited_until",
  "rateLimitedUntil"
];
var LANE_COOLDOWN_REASON_FIELDS = [
  "exhausted_reason",
  "exhaustedReason",
  "cooldown_reason",
  "cooldownReason"
];
var LANE_UNSERVICEABLE_HEALTH = ["exhausted", "unavailable"];
var STATE_KEYS = {
  provider: (provider) => `cliproxy-insight:provider:${provider}`,
  /** Plugin state has no scan/list, so the set of observed providers is itself state. */
  providerIndex: "cliproxy-insight:provider-index",
  modelUsage: "cliproxy-insight:model-usage",
  cooldownEvents: (provider) => `cliproxy-insight:cooldown-events:${provider}`,
  /** One persisted snapshot per lane document, keyed by its file name. */
  lane: (laneFile) => `cliproxy-insight:lane:${laneFile}`,
  /** Same reason as `providerIndex`: state offers no scan. */
  laneIndex: "cliproxy-insight:lane-index"
};
var MAX_COOLDOWN_EVENTS_PER_PROVIDER = 200;
var REQUEST_RATE_FIELDS = {
  success: ["success", "successes", "successCount", "success_count", "ok"],
  failed: ["failed", "failures", "failureCount", "failure_count", "errors"]
};
var SUPPORTED_SCHEMA_VERSION = 1;

// src/collector-evidence.ts
var MAX_PROVIDERS = 64;
var MAX_LANES = 32;
function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
function index(value, limit) {
  if (value === null || value === void 0) return null;
  if (!Array.isArray(value) || value.length > limit || value.some((item) => typeof item !== "string" || item.length === 0 || item.length > 128)) {
    throw new Error("invalid_evidence_index");
  }
  return [...new Set(value)];
}
async function summarize(read, keys, startMs, endMs) {
  if (keys === null) return null;
  let missing = 0;
  let outsideWindow = 0;
  let invalidTimestamp = 0;
  let snapshots = 0;
  let jsonBytes = 0;
  for (const key of keys) {
    const value = await read(key);
    if (value === null || value === void 0) {
      missing += 1;
      continue;
    }
    const snapshot = record(value);
    const polledMs = typeof snapshot.polledAt === "string" ? Date.parse(snapshot.polledAt) : NaN;
    if (!Number.isFinite(polledMs)) {
      invalidTimestamp += 1;
      continue;
    }
    if (polledMs < startMs || polledMs > endMs) {
      outsideWindow += 1;
      continue;
    }
    snapshots += 1;
    jsonBytes += new TextEncoder().encode(JSON.stringify(value)).byteLength;
  }
  return { indexed: keys.length, snapshots, jsonBytes, missing, outsideWindow, invalidTimestamp };
}
async function collectorEvidence(read, companyId, windowSeconds, nowMs, settings) {
  const providers = index(await read(STATE_KEYS.providerIndex), MAX_PROVIDERS);
  const lanes = index(await read(STATE_KEYS.laneIndex), MAX_LANES);
  const startMs = nowMs - windowSeconds * 1e3;
  const providerSnapshots = await summarize(
    read,
    providers?.map(STATE_KEYS.provider) ?? null,
    startMs,
    nowMs
  );
  const laneSnapshots = await summarize(
    read,
    lanes?.map(STATE_KEYS.lane) ?? null,
    startMs,
    nowMs
  );
  const modelSnapshot = await summarize(read, [STATE_KEYS.modelUsage], startMs, nowMs);
  return {
    schemaVersion: 1,
    companyId,
    pluginId: PLUGIN_ID,
    pluginVersion: PLUGIN_VERSION,
    generatedAt: new Date(nowMs).toISOString(),
    qualification: "partial",
    deployedArtifactId: null,
    installedLifecycle: null,
    settingsSource: "resolved_running_worker_config",
    settings: {
      pollingEnabled: settings.pollingEnabled,
      staleAfterSeconds: settings.staleAfterSeconds,
      maxCooldownEventsPerProvider: settings.maxCooldownEventsPerProvider,
      diagnosticSampleRate: null,
      diagnosticRetentionSeconds: null,
      diagnosticRedactionPolicy: null
    },
    snapshotWindow: {
      start: new Date(startMs).toISOString(),
      end: new Date(nowMs).toISOString(),
      semantics: "current_snapshots_with_polledAt_in_window_not_event_history",
      byteSemantics: "utf8_json_snapshot_values_excluding_state_keys_and_storage_overhead",
      providerSnapshots,
      laneSnapshots,
      modelSnapshot
    },
    diagnosticEvents: null,
    diagnosticInputBytes: null,
    droppedSelfOriginEvents: null,
    redactedFields: null
  };
}

// src/worker.ts
var DEFAULT_BASE_URL = "https://host-a965dd17.example.invalid/telemetry/cliproxy";
function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
function firstNumber(record2, fields) {
  for (const field of fields) {
    const value = record2[field];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}
function toolRejection(error, extra = {}) {
  return { ok: false, error, ...extra };
}
function usageRejection(error, nextAction) {
  return { error, data: toolRejection(error, { retryable: false, next_action: nextAction }) };
}
function unknownLaneResult() {
  return usageRejection("unknown_lane", "use a lane id from the persisted lane index");
}
function resolveConfig(raw) {
  const r = asRecord(raw);
  return {
    pollingEnabled: r.pollingEnabled === true,
    baseUrl: typeof r.baseUrl === "string" && r.baseUrl.length > 0 ? r.baseUrl.replace(/\/+$/, "") : DEFAULT_BASE_URL,
    laneApiKeySecretRef: r.laneApiKeySecretRef ?? null,
    requestTimeoutMs: typeof r.requestTimeoutMs === "number" ? r.requestTimeoutMs : 1e4,
    maxCooldownEventsPerProvider: typeof r.maxCooldownEventsPerProvider === "number" ? r.maxCooldownEventsPerProvider : MAX_COOLDOWN_EVENTS_PER_PROVIDER,
    staleAfterSeconds: typeof r.staleAfterSeconds === "number" ? r.staleAfterSeconds : 600,
    // An explicitly empty array means "poll no lane documents" and is honoured;
    // only an absent or non-array value falls back to the default set.
    laneFiles: Array.isArray(r.laneFiles) ? r.laneFiles.filter((f) => typeof f === "string" && f.length > 0) : [...DEFAULT_LANE_FILES],
    legacyAggregateFiles: r.legacyAggregateFiles === true
  };
}
var TIMED_OUT = Symbol("cliproxy-insight:timeout");
async function fetchLaneFile(ctx, config, fileName, apiKey) {
  let timer;
  try {
    const raced = await Promise.race([
      ctx.http.fetch(`${config.baseUrl}/${fileName}`, {
        method: "GET",
        headers: { [LANE_AUTH_HEADER]: apiKey }
      }),
      new Promise((resolve) => {
        timer = setTimeout(() => resolve(TIMED_OUT), config.requestTimeoutMs);
      })
    ]);
    if (raced === TIMED_OUT) {
      return { ok: false, status: null, reason: "timeout" };
    }
    const response = raced;
    if (response.status !== 200) {
      return { ok: false, status: response.status, reason: `http_${response.status}` };
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      return { ok: false, status: response.status, reason: "malformed_json" };
    }
    return { ok: true, status: response.status, body };
  } catch {
    return { ok: false, status: null, reason: "network" };
  } finally {
    clearTimeout(timer);
  }
}
function extractProviderRecords(body, polledAt) {
  const root = asRecord(body);
  const nested = asRecord(root.providers);
  const source = Object.keys(nested).length > 0 ? nested : root;
  const observedAt = typeof root.observedAt === "string" ? root.observedAt : typeof root.generatedAt === "string" ? root.generatedAt : polledAt;
  const records = [];
  for (const [provider, value] of Object.entries(source)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const record2 = value;
    records.push({
      provider,
      polledAt,
      observedAt,
      success: firstNumber(record2, REQUEST_RATE_FIELDS.success),
      failed: firstNumber(record2, REQUEST_RATE_FIELDS.failed),
      raw: value
    });
  }
  return { observedAt, records };
}
function cooldownReason(raw) {
  const r = asRecord(raw);
  if (r.exhausted === true) return "exhausted_flag";
  if (typeof r.cooldownUntil === "string" || typeof r.cooldown_until === "string") {
    return "cooldown_until";
  }
  if (typeof r.exhaustedAt === "string") return "exhausted_at";
  const state = typeof r.state === "string" ? r.state.toLowerCase() : null;
  if (state === "exhausted" || state === "unavailable") return `state_${state}`;
  if (r.serviceable === false) return "not_serviceable";
  return null;
}
function firstString(record2, fields) {
  for (const field of fields) {
    const value = record2[field];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}
function extractLaneDocument(body, laneFile, polledAt) {
  const root = asRecord(body);
  if (root.schemaVersion !== SUPPORTED_SCHEMA_VERSION) return null;
  const records = Array.isArray(root.records) ? root.records.filter(
    (r) => !!r && typeof r === "object" && !Array.isArray(r)
  ) : [];
  return {
    schemaVersion: 1,
    laneFile,
    polledAt,
    observedAt: typeof root.observedAt === "string" ? root.observedAt : polledAt,
    staleAfterSeconds: typeof root.staleAfterSeconds === "number" ? root.staleAfterSeconds : null,
    records
  };
}
function laneCooldown(record2, nowMs) {
  const r = asRecord(record2);
  const nested = asRecord(r.cooldown);
  const until = firstString(r, LANE_COOLDOWN_FIELDS) ?? (typeof nested.until === "string" ? nested.until : null);
  if (until) {
    const untilMs = Date.parse(until);
    if (!Number.isFinite(untilMs)) return { until, reason: "cooldown_unparseable_until" };
    if (untilMs > nowMs) {
      const reason = firstString(r, LANE_COOLDOWN_REASON_FIELDS) ?? (typeof nested.reason === "string" && nested.reason.length > 0 ? nested.reason : "cooldown_until");
      return { until, reason: reason.slice(0, 120) };
    }
  }
  const health = typeof r.health === "string" ? r.health.toLowerCase() : null;
  if (health && LANE_UNSERVICEABLE_HEALTH.includes(health)) {
    return { until: null, reason: `health_${health}` };
  }
  return null;
}
function laneAccountId(record2, laneFile) {
  const lane = record2.lane;
  return typeof lane === "string" && lane.length > 0 ? lane : laneFile.replace(/\.json$/i, "");
}
function isStale(observedAt, staleAfterSeconds, nowMs) {
  if (typeof observedAt !== "string") return true;
  const observedMs = Date.parse(observedAt);
  if (!Number.isFinite(observedMs)) return true;
  return nowMs - observedMs > staleAfterSeconds * 1e3;
}
async function readLaneIndex(read) {
  const storedIndex = await read(STATE_KEYS.laneIndex);
  return Array.isArray(storedIndex) ? [...new Set(storedIndex.filter((l) => typeof l === "string" && l.trim().length > 0))] : [];
}
async function laneSummary(read, config, nowMs, lanes) {
  const laneSnapshots = {};
  let accountsCooling = 0;
  for (const laneFile of lanes) {
    const snapshot = await read(STATE_KEYS.lane(laneFile));
    if (!snapshot) {
      laneSnapshots[laneFile] = null;
      continue;
    }
    const accounts = (snapshot.records ?? []).map((record2) => {
      const cooldown = laneCooldown(record2, nowMs);
      if (cooldown) accountsCooling += 1;
      return {
        account: laneAccountId(record2, laneFile),
        health: typeof record2.health === "string" ? record2.health : null,
        cooldown,
        record: record2
      };
    });
    laneSnapshots[laneFile] = {
      ...snapshot,
      stale: isStale(
        snapshot.observedAt,
        typeof snapshot.staleAfterSeconds === "number" ? snapshot.staleAfterSeconds : config.staleAfterSeconds,
        nowMs
      ),
      accounts
    };
  }
  return { lanes, laneSnapshots, accountsCooling };
}
function createPlugin() {
  let context = null;
  const configuredCompanies = /* @__PURE__ */ new Set();
  let missingCompanyIdentity = false;
  const isConfiguredCompany = (companyId) => !missingCompanyIdentity && configuredCompanies.size === 1 && configuredCompanies.has(companyId);
  return definePlugin({
    multiCompanyConfig: true,
    async onConfigChanged(_config, change) {
      if (!change?.companyId?.trim()) {
        missingCompanyIdentity = true;
        await context?.metrics.write("cliproxy_insight.company_scope_refused", 1, {
          reason: "missing_company_id"
        });
        return;
      }
      configuredCompanies.add(change.companyId);
      if (configuredCompanies.size !== 1) {
        await context?.metrics.write("cliproxy_insight.company_scope_refused", 1, {
          reason: "multiple_companies"
        });
        throw new Error("cliproxy-insight requires exactly one configured company; polling stopped");
      }
    },
    async setup(ctx) {
      context = ctx;
      const key = (companyId, stateKey) => ({
        scopeKind: "company",
        scopeId: companyId,
        stateKey
      });
      const readProviderIndex = async (companyId) => {
        const stored = await ctx.state.get(key(companyId, STATE_KEYS.providerIndex));
        return Array.isArray(stored) ? stored.filter((p) => typeof p === "string") : [];
      };
      const orderProviders = (providers) => {
        const rank = (p) => {
          const index2 = SEED_PROVIDER_ORDER.indexOf(p);
          return index2 === -1 ? SEED_PROVIDER_ORDER.length : index2;
        };
        return [...new Set(providers)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
      };
      const pollOneCompany = async (companyId, config) => {
        if (!config.pollingEnabled) {
          await ctx.metrics.write("cliproxy_insight.poll_skipped_disabled", 1, { companyId });
          return;
        }
        if (!config.laneApiKeySecretRef) {
          await ctx.metrics.write("cliproxy_insight.poll_skipped_no_secret", 1, { companyId });
          return;
        }
        let apiKey;
        try {
          apiKey = await ctx.secrets.resolve(config.laneApiKeySecretRef, {
            companyId,
            configPath: "laneApiKeySecretRef"
          });
        } catch {
          ctx.logger.warn("cliproxy-insight: could not resolve the lane bearer", {
            companyId,
            reason: "secret_resolve_failed"
          });
          await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
            companyId,
            reason: "secret_resolve_failed"
          });
          return;
        }
        if (!isConfiguredCompany(companyId)) return;
        const polledAt = (/* @__PURE__ */ new Date()).toISOString();
        const nowMs = Date.parse(polledAt);
        const [laneResults, rates, modelUsage] = await Promise.all([
          Promise.all(
            config.laneFiles.map(async (laneFile) => ({
              laneFile,
              result: await fetchLaneFile(ctx, config, laneFile, apiKey)
            }))
          ),
          config.legacyAggregateFiles ? fetchLaneFile(ctx, config, LANE_PATHS.requestRates, apiKey) : null,
          config.legacyAggregateFiles ? fetchLaneFile(ctx, config, LANE_PATHS.modelUsage, apiKey) : null
        ]);
        let anySucceeded = false;
        const lanesStored = [];
        let accountsCooling = 0;
        for (const { laneFile, result } of laneResults) {
          if (!result.ok) {
            await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
              companyId,
              file: laneFile,
              reason: result.reason
            });
            continue;
          }
          const snapshot = extractLaneDocument(result.body, laneFile, polledAt);
          if (!snapshot) {
            await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
              companyId,
              file: laneFile,
              reason: "unsupported_schema_version"
            });
            await ctx.activity.log({
              companyId,
              message: `CLIProxy insight: ${laneFile} is not schemaVersion ${SUPPORTED_SCHEMA_VERSION}; snapshot not stored.`
            });
            continue;
          }
          anySucceeded = true;
          lanesStored.push(laneFile);
          const previous = await ctx.state.get(
            key(companyId, STATE_KEYS.lane(laneFile))
          );
          await ctx.state.set(
            key(companyId, STATE_KEYS.lane(laneFile)),
            snapshot
          );
          const previousByAccount = /* @__PURE__ */ new Map();
          for (const record2 of previous?.records ?? []) {
            previousByAccount.set(laneAccountId(record2, laneFile), record2);
          }
          for (const record2 of snapshot.records) {
            const accountId = laneAccountId(record2, laneFile);
            const cooldown = laneCooldown(record2, nowMs);
            if (cooldown) accountsCooling += 1;
            const previousCooldown = laneCooldown(previousByAccount.get(accountId), nowMs);
            const isTransition = !!cooldown && (!previousCooldown || previousCooldown.until !== cooldown.until || previousCooldown.reason !== cooldown.reason);
            if (!isTransition) continue;
            const eventKey = key(companyId, STATE_KEYS.cooldownEvents(accountId));
            const existing = await ctx.state.get(eventKey);
            const events = Array.isArray(existing) ? existing : [];
            events.unshift({
              at: polledAt,
              provider: accountId,
              reason: cooldown.reason,
              raw: { laneFile, until: cooldown.until, health: record2.health ?? null }
            });
            await ctx.state.set(eventKey, events.slice(0, config.maxCooldownEventsPerProvider));
            await ctx.activity.log({
              companyId,
              message: `CLIProxy insight: ${accountId} entered cooldown (${cooldown.reason}${cooldown.until ? `, until ${cooldown.until}` : ""})`
            });
          }
        }
        if (config.laneFiles.length > 0) {
          const knownLanes = await readLaneIndex((stateKey) => ctx.state.get(key(companyId, stateKey)));
          await ctx.state.set(
            key(companyId, STATE_KEYS.laneIndex),
            [.../* @__PURE__ */ new Set([...knownLanes, ...lanesStored])].sort()
          );
          await ctx.metrics.write("cliproxy_insight.lanes_observed", lanesStored.length, {
            companyId
          });
          await ctx.metrics.write("cliproxy_insight.lane_accounts_cooling", accountsCooling, {
            companyId
          });
        }
        if (rates === null) {
        } else if (!rates.ok) {
          await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
            companyId,
            file: LANE_PATHS.requestRates,
            reason: rates.reason
          });
          await ctx.activity.log({
            companyId,
            message: `CLIProxy insight: ${LANE_PATHS.requestRates} unavailable (${rates.reason}). Not retrying within this firing.`
          });
        } else {
          anySucceeded = true;
          const { records } = extractProviderRecords(rates.body, polledAt);
          const seen = orderProviders(records.map((r) => r.provider));
          let changed = 0;
          for (const record2 of records) {
            const stateKey = key(companyId, STATE_KEYS.provider(record2.provider));
            const previous = await ctx.state.get(stateKey);
            const next = { schemaVersion: 1, ...record2 };
            if (JSON.stringify(previous?.raw) !== JSON.stringify(next.raw)) changed += 1;
            await ctx.state.set(stateKey, next);
            const reason = cooldownReason(record2.raw);
            const previousReason = cooldownReason(previous?.raw);
            if (reason && reason !== previousReason) {
              const eventKey = key(companyId, STATE_KEYS.cooldownEvents(record2.provider));
              const existing = await ctx.state.get(eventKey);
              const events = Array.isArray(existing) ? existing : [];
              events.unshift({ at: polledAt, provider: record2.provider, reason, raw: record2.raw });
              await ctx.state.set(eventKey, events.slice(0, config.maxCooldownEventsPerProvider));
              await ctx.activity.log({
                companyId,
                message: `CLIProxy insight: ${record2.provider} entered cooldown (${reason})`
              });
            }
          }
          const index2 = orderProviders([...await readProviderIndex(companyId), ...seen]);
          await ctx.state.set(key(companyId, STATE_KEYS.providerIndex), index2);
          await ctx.metrics.write("cliproxy_insight.providers_observed", seen.length, { companyId });
          if (changed > 0) {
            await ctx.activity.log({
              companyId,
              message: `CLIProxy insight: ${changed} provider(s) changed`
            });
          }
        }
        if (modelUsage === null) {
        } else if (!modelUsage.ok) {
          await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
            companyId,
            file: LANE_PATHS.modelUsage,
            reason: modelUsage.reason
          });
        } else {
          const body = asRecord(modelUsage.body);
          const version = body.schemaVersion;
          if (version !== SUPPORTED_SCHEMA_VERSION) {
            await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
              companyId,
              file: LANE_PATHS.modelUsage,
              reason: "unsupported_schema_version"
            });
            await ctx.activity.log({
              companyId,
              message: `CLIProxy insight: ${LANE_PATHS.modelUsage} reports schemaVersion ${String(version)}; this plugin implements ${SUPPORTED_SCHEMA_VERSION}. Snapshot not stored.`
            });
          } else {
            anySucceeded = true;
            const models = asRecord(body.models);
            await ctx.state.set(key(companyId, STATE_KEYS.modelUsage), {
              schemaVersion: 1,
              polledAt,
              observedAt: typeof body.observedAt === "string" ? body.observedAt : polledAt,
              staleAfterSeconds: typeof body.staleAfterSeconds === "number" ? body.staleAfterSeconds : null,
              telemetry: typeof body.telemetry === "string" ? body.telemetry : "unavailable",
              reasonCode: typeof body.reasonCode === "string" ? body.reasonCode : null,
              models
            });
            await ctx.metrics.write("cliproxy_insight.models_observed", Object.keys(models).length, {
              companyId
            });
          }
        }
        if (anySucceeded) {
          await ctx.metrics.write("cliproxy_insight.poll_ok", 1, { companyId });
        }
      };
      ctx.tools.register(
        TOOL_NAMES.getCollectorEvidence,
        {
          displayName: "Get collector evidence",
          description: "Read bounded aggregate snapshot evidence without raw provider or lane records.",
          parametersSchema: {
            type: "object",
            additionalProperties: false,
            required: ["companyId"],
            properties: {
              companyId: { type: "string", minLength: 1 },
              windowSeconds: { type: "integer", minimum: 60, maximum: 86400, default: 3600 }
            }
          }
        },
        async (params, runCtx) => {
          const input = asRecord(params);
          const companyId = typeof input.companyId === "string" ? input.companyId : "";
          const windowSeconds = input.windowSeconds === void 0 ? 3600 : input.windowSeconds;
          if (!companyId || !Number.isInteger(windowSeconds) || typeof windowSeconds !== "number" || windowSeconds < 60 || windowSeconds > 86400 || Object.keys(input).some((name) => name !== "companyId" && name !== "windowSeconds")) {
            return { error: "invalid_evidence_request", data: toolRejection("invalid_evidence_request") };
          }
          if (runCtx.companyId !== companyId || !isConfiguredCompany(companyId)) {
            return { error: "company is not configured", data: toolRejection("company is not configured") };
          }
          try {
            const config = resolveConfig(await ctx.config.get(companyId));
            if (!isConfiguredCompany(companyId)) {
              return { error: "company is not configured", data: toolRejection("company is not configured") };
            }
            const data = await collectorEvidence(
              (stateKey) => ctx.state.get(key(companyId, stateKey)),
              companyId,
              windowSeconds,
              Date.now(),
              config
            );
            if (!isConfiguredCompany(companyId)) {
              return { error: "company is not configured", data: toolRejection("company is not configured") };
            }
            return { data };
          } catch {
            return { error: "evidence_unavailable", data: toolRejection("evidence_unavailable") };
          }
        }
      );
      ctx.tools.register(
        TOOL_NAMES.getProviderUsage,
        {
          displayName: "Get provider usage",
          description: "Read the most recently persisted CLIProxy usage state: per-provider success/failed counters, current cooldown status, and per-model capacity windows, each flagged stale when older than the freshness budget. Optionally select one persisted lane to return only that lane's stored history; provider snapshots and modelUsage keep their existing scope. Stored history only \u2014 not serving readiness or an admission decision. Read-only; consults stored history, never calls CLIProxy.",
          parametersSchema: {
            type: "object",
            required: ["companyId"],
            properties: {
              companyId: { type: "string", description: "Company whose CLIProxy history applies" },
              provider: {
                type: "string",
                description: "Optional. One exact provider key from the persisted provider index (`providers` in an unfiltered call, e.g. claude or codex). Omit to return every provider observed so far. Invalid keys are rejected as unknown_provider."
              },
              lane: {
                type: "string",
                description: "Optional. One exact lane id from the persisted lane index \u2014 an entry in `lanes` from an unfiltered call (e.g. claude.json). Omit to return every persisted lane. Unknown ids are rejected as unknown_lane; a provider name is not a lane id."
              }
            }
          }
        },
        async (params) => {
          const input = asRecord(params);
          const companyId = typeof input.companyId === "string" ? input.companyId : "";
          if (!companyId)
            return { error: "companyId is required", data: toolRejection("companyId is required") };
          if (!isConfiguredCompany(companyId))
            return { error: "company is not configured", data: toolRejection("company is not configured") };
          const rejectLane = async (reason) => {
            try {
              await ctx.metrics.write("cliproxy_insight.lane_selection_refused", 1, { companyId, reason });
            } catch {
            }
            return unknownLaneResult();
          };
          const selectedLane = input.lane;
          if (selectedLane !== void 0 && (typeof selectedLane !== "string" || selectedLane.trim().length === 0)) {
            return rejectLane("invalid_shape");
          }
          const requested = input.provider;
          if (requested !== void 0 && (typeof requested !== "string" || requested.trim().length === 0)) {
            return usageRejection("unknown_provider", "use a provider key from the persisted provider index");
          }
          const config = resolveConfig(await ctx.config.get(companyId));
          if (!isConfiguredCompany(companyId))
            return { error: "company is not configured", data: toolRejection("company is not configured") };
          const nowMs = Date.now();
          const readState = (stateKey) => ctx.state.get(key(companyId, stateKey));
          const indexedLanes = await readLaneIndex(readState);
          if (!isConfiguredCompany(companyId))
            return { error: "company is not configured", data: toolRejection("company is not configured") };
          if (typeof selectedLane === "string" && !indexedLanes.includes(selectedLane)) {
            return rejectLane("unknown_id");
          }
          const indexedProviders = await readProviderIndex(companyId);
          if (!isConfiguredCompany(companyId))
            return { error: "company is not configured", data: toolRejection("company is not configured") };
          if (typeof requested === "string" && !indexedProviders.includes(requested)) {
            return usageRejection("unknown_provider", "use a provider key from the persisted provider index");
          }
          const providers = typeof requested === "string" ? [requested] : indexedProviders;
          const lane = await laneSummary(
            readState,
            config,
            nowMs,
            typeof selectedLane === "string" ? [selectedLane] : indexedLanes
          );
          const snapshots = {};
          for (const provider of providers) {
            const record2 = await ctx.state.get(
              key(companyId, STATE_KEYS.provider(provider))
            );
            snapshots[provider] = record2 ? { ...record2, stale: isStale(record2.observedAt, config.staleAfterSeconds, nowMs) } : null;
          }
          const modelUsage = asRecord(await ctx.state.get(key(companyId, STATE_KEYS.modelUsage)));
          const hasModelUsage = Object.keys(modelUsage).length > 0;
          return {
            data: {
              version: PLUGIN_VERSION,
              providers,
              snapshots,
              lanes: lane.lanes,
              laneSnapshots: lane.laneSnapshots,
              accountsCooling: lane.accountsCooling,
              modelUsage: hasModelUsage ? {
                ...modelUsage,
                stale: isStale(
                  modelUsage.observedAt,
                  typeof modelUsage.staleAfterSeconds === "number" ? modelUsage.staleAfterSeconds : config.staleAfterSeconds,
                  nowMs
                )
              } : null
            }
          };
        }
      );
      ctx.jobs.register(JOB_KEYS.poll, async (_job) => {
        if (missingCompanyIdentity || configuredCompanies.size !== 1) {
          await ctx.metrics.write("cliproxy_insight.poll_skipped_company_scope", 1);
          return;
        }
        const companyId = [...configuredCompanies][0];
        if (!companyId) return;
        try {
          const config = resolveConfig(await ctx.config.get(companyId));
          if (!isConfiguredCompany(companyId)) return;
          await pollOneCompany(companyId, config);
        } catch {
          ctx.logger.warn("cliproxy-insight: poll failed for company", {
            companyId,
            reason: "unhandled"
          });
          await ctx.metrics.write("cliproxy_insight.poll_errors", 1, {
            companyId,
            reason: "unhandled"
          });
        }
      });
      ctx.logger.info("CLIProxy Insight worker ready", { version: PLUGIN_VERSION });
    },
    async onHealth() {
      return { status: "ok", message: `CLIProxy Insight ${PLUGIN_VERSION}` };
    },
    /**
     * Note what this is NOT: the persisting write, `POST /plugins/:id/config`,
     * validates against `instanceConfigSchema` with Ajv and never calls this
     * hook — only `POST /plugins/:id/config/test` (a dry run) reaches it. So
     * this is an operator-facing check, not a gate; anything that must
     * actually be refused at write time has to live in `src/config/schema.ts`.
     * The secret-ref shape guard below is the one exception that matters:
     * duplicated in spirit by the schema's `type: ["object","null"]`, but the
     * host's `format:"secret-ref"` validator is a documented no-op, so this
     * hook is the only place a malformed secret-ref is ever actually caught
     * before persistence, same as the Model Router (ADR-0004).
     */
    async onValidateConfig(config) {
      const errors = [];
      const warnings = [];
      const resolved = resolveConfig(config);
      const secretRefError = validateSecretRefShape(
        config.laneApiKeySecretRef,
        "laneApiKeySecretRef"
      );
      if (secretRefError) errors.push(secretRefError);
      if (resolved.pollingEnabled && !resolved.laneApiKeySecretRef) {
        errors.push(
          "pollingEnabled is true but laneApiKeySecretRef is not set \u2014 polling would resolve no key and every firing would skip. Reference the cliproxy-usage-lane-key secret, or leave pollingEnabled false."
        );
      }
      if (resolved.pollingEnabled && /127\.0\.0\.1|localhost|\[::1\]/i.test(resolved.baseUrl)) {
        errors.push(
          "baseUrl points at loopback. CLIProxy binds host-loopback only (case_31532730) and is unreachable from any plugin worker; baseUrl must be the public HTTPS telemetry lane (case_daf05f17)."
        );
      }
      if (/\/v0\/management/i.test(resolved.baseUrl)) {
        errors.push(
          "baseUrl points at the CLIProxy management API. That surface returns credentials in clear and is never read by this plugin \u2014 use the sanitized telemetry lane (case_daf05f17)."
        );
      }
      if (resolved.pollingEnabled && resolved.laneFiles.length === 0 && !resolved.legacyAggregateFiles) {
        errors.push(
          "pollingEnabled is true but laneFiles is empty and legacyAggregateFiles is false \u2014 every firing would fetch nothing. List the lane documents to poll (default: claude.json, codex.json, kimi.json, opencode-go.json, zai.json, antigravity.json)."
        );
      }
      if (resolved.pollingEnabled && resolved.baseUrl.startsWith("http://")) {
        errors.push("baseUrl must be https \u2014 the lane bearer would otherwise be sent in clear.");
      }
      return { ok: errors.length === 0, errors, warnings };
    },
    async onApiRequest(input) {
      if (!context) return { status: 503, body: { error: "worker is not initialised" } };
      if (input.routeKey !== ROUTE_KEYS.usageSummary) {
        return { status: 404, body: { error: `unknown route ${input.routeKey}` } };
      }
      const companyId = input.companyId;
      if (!isConfiguredCompany(companyId)) {
        return { status: 403, body: { error: "company is not configured" } };
      }
      const config = resolveConfig(await context.config.get(companyId));
      if (!isConfiguredCompany(companyId)) {
        return { status: 403, body: { error: "company is not configured" } };
      }
      const nowMs = Date.now();
      const scope = (stateKey) => ({
        scopeKind: "company",
        scopeId: companyId,
        stateKey
      });
      const stored = await context.state.get(scope(STATE_KEYS.providerIndex));
      const providers = Array.isArray(stored) ? stored.filter((p) => typeof p === "string") : [];
      const snapshots = {};
      for (const provider of providers) {
        const record2 = await context.state.get(
          scope(STATE_KEYS.provider(provider))
        );
        snapshots[provider] = record2 ? { ...record2, stale: isStale(record2.observedAt, config.staleAfterSeconds, nowMs) } : null;
      }
      const modelUsage = asRecord(await context.state.get(scope(STATE_KEYS.modelUsage)));
      const hasModelUsage = Object.keys(modelUsage).length > 0;
      const readRouteState = (stateKey) => context.state.get(scope(stateKey));
      const lane = await laneSummary(
        readRouteState,
        config,
        nowMs,
        await readLaneIndex(readRouteState)
      );
      return {
        status: 200,
        body: {
          version: PLUGIN_VERSION,
          providers,
          snapshots,
          lanes: lane.lanes,
          laneSnapshots: lane.laneSnapshots,
          accountsCooling: lane.accountsCooling,
          modelUsage: hasModelUsage ? {
            ...modelUsage,
            stale: isStale(
              modelUsage.observedAt,
              typeof modelUsage.staleAfterSeconds === "number" ? modelUsage.staleAfterSeconds : config.staleAfterSeconds,
              nowMs
            )
          } : null
        }
      };
    }
  });
}
var plugin = createPlugin();
var worker_default = plugin;
runWorker(plugin, import.meta.url);
export {
  cooldownReason,
  createPlugin,
  worker_default as default,
  extractLaneDocument,
  extractProviderRecords,
  isStale,
  laneAccountId,
  laneCooldown
};
//# sourceMappingURL=worker.js.map
