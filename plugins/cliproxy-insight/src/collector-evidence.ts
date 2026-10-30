import { PLUGIN_ID, PLUGIN_VERSION, STATE_KEYS } from "./constants.js";

const MAX_PROVIDERS = 64;
const MAX_LANES = 32;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function index(value: unknown, limit: number): string[] | null {
  if (value === null || value === undefined) return null;
  if (!Array.isArray(value) || value.length > limit ||
      value.some((item) => typeof item !== "string" || item.length === 0 || item.length > 128)) {
    throw new Error("invalid_evidence_index");
  }
  return [...new Set(value)] as string[];
}

/** Only aggregate numbers leave the worker; producer strings never do. */
async function summarize(
  read: (key: string) => Promise<unknown>,
  keys: string[] | null,
  startMs: number,
  endMs: number,
) {
  if (keys === null) return null;
  let missing = 0;
  let outsideWindow = 0;
  let invalidTimestamp = 0;
  let snapshots = 0;
  let jsonBytes = 0;
  for (const key of keys) {
    const value = await read(key);
    if (value === null || value === undefined) {
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

/**
 * A bounded inventory of current snapshots, NOT a traffic/diagnostic history.
 * No cooldown/audit history is read or pruned. Missing instrumentation stays null.
 */
export async function collectorEvidence(
  read: (key: string) => Promise<unknown>,
  companyId: string,
  windowSeconds: number,
  nowMs: number,
  settings: {
    pollingEnabled: boolean;
    staleAfterSeconds: number;
    maxCooldownEventsPerProvider: number;
  },
) {
  const providers = index(await read(STATE_KEYS.providerIndex), MAX_PROVIDERS);
  const lanes = index(await read(STATE_KEYS.laneIndex), MAX_LANES);
  const startMs = nowMs - windowSeconds * 1000;
  const providerSnapshots = await summarize(
    read, providers?.map(STATE_KEYS.provider) ?? null, startMs, nowMs,
  );
  const laneSnapshots = await summarize(
    read, lanes?.map(STATE_KEYS.lane) ?? null, startMs, nowMs,
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
      diagnosticRedactionPolicy: null,
    },
    snapshotWindow: {
      start: new Date(startMs).toISOString(),
      end: new Date(nowMs).toISOString(),
      semantics: "current_snapshots_with_polledAt_in_window_not_event_history",
      byteSemantics: "utf8_json_snapshot_values_excluding_state_keys_and_storage_overhead",
      providerSnapshots,
      laneSnapshots,
      modelSnapshot,
    },
    diagnosticEvents: null,
    diagnosticInputBytes: null,
    droppedSelfOriginEvents: null,
    redactedFields: null,
  };
}
