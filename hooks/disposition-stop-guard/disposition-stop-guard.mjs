#!/usr/bin/env node
// Claude Code `Stop` hook for every agent: refuse to let a run end on a card the platform
// will have to repair.
//
// WHY THIS EXISTS. AGENTS.md already says how a run ends: `done`, `in_review` with a path,
// `blocked` with a blocker, or `in_progress` with a scheduled monitor. Runs
// still end on `in_progress` or `todo` with nothing that will ever wake the card, or with a
// monitor whose budget is spent. Paperclip then queues an `issue_disposition_repair` run
// (403 of 6,363 runs in one 24 h window, 6.3%), and after two failed repairs it escalates
// the card to `blocked`. A rule that is written down and still broken 400 times a day should
// be code.
//
// WHAT IT DOES. On the main-thread `Stop` event of a run that has PAPERCLIP_TASK_ID it reads
// the card with the run's own credential (two read-only GETs, started together: the card and its
// interactions, each on a ~900 ms budget against the 1 s Stop budget, with a shared card cache
// covering the tail: fresh cache skips the network, stale cache decides when the network is
// slow). If the card is `in_progress`
// or `todo` and has no live path, or its monitor is exhausted, it prints
// `{"decision":"block","reason":...}` and exits 0. Claude Code then continues the turn in the
// same warm session with the reason as the model's next input.
//
// LIVE PATH. Any of: a scheduled monitor, an unresolved blocker, a pending interaction, a
// recovery the platform already owns (except the repair's own `deliberate_wait_without_target`
// wait, which stays active through the repair run and is never a live path), a scheduled retry
// for another run (a retry row for this very run is this run, not a future wake), a review participant.
// A card that is `done`, `cancelled`,
// `blocked` or `in_review` is never touched: the platform already validated those writes.
// A `{"resume": true}` PATCH or comment is NOT a live path. Measured over 24 h to 2026-10-10 18:17Z
// (HANDOFF.md): 85 runs ended on an open card with nothing else armed after a resume write, and 70
// (82%) were followed by a repair wake; 339 that made no resume write ended the same way and 237
// (70%) were. `resume` restarts work on a closed card; it does not keep an open one alive. The
// same table: an armed monitor 3 of 2,062 repaired, a pending interaction 6 of 209, a blocker 6 of 74.
//
// WHAT IT NEVER DOES (each is pinned by a test):
//   - block more than MAX_HANDBACKS times per run. The counter lives outside the transcript
//     and is the real loop guard; `stop_hook_active` is honoured on top.
//   - block while background tasks or scheduled wakeups exist: the session is paused on
//     purpose, not finished.
//   - block on an error, a missing credential, an unreadable or timed-out card
//     (`card-timeout` / `interactions-timeout` are their own fail-open classes, so a slow
//     control plane cannot spike `card-unreadable`), or after the deadline.
//     Every failure path allows the stop and exits 0.
//   - block a card another agent owns or another run holds, or a chat conversation.
//   - write a card, a comment or anything else. It only reads.
//   - write model text or card text anywhere. The decision log holds reasons and lengths.
//   - touch the Muse guard. It has its own counter file and its own log.
//
// MODE. `observe` logs what it would have done and never blocks. `enforce` blocks. `off`
// does nothing. The default is DEFAULT_MODE, set from the replay in HANDOFF.md; the config
// file and the environment can change it without a settings edit.
//
// KILL SWITCH AND TUNING. A file named DISABLED next to this script allows every stop,
// including in runs that are already in flight. disposition-stop-guard.config.json beside it
// ({"mode": "observe", "maxHandbacks": 2}) retunes the guard; environment variables win.

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

export const MAX_HANDBACKS_DEFAULT = 2;
/**
 * Set from the 24 h replay (HANDOFF.md): 68 of 5,084 runs no repair followed would have been handed
 * back, 1.3% of guarded runs against the 2% line above which the default must stay `observe`.
 */
export const DEFAULT_MODE = "enforce";
export const MODES = ["off", "observe", "enforce"];
const HARD_DEADLINE_MS = 8000;
// The Stop budget is 1 s (p95 <= 1 s before enforce). The card read is the
// slow one (observe 23:37Z->05:41Z, 624 stops: p50 533 ms, p95 3.1 s, max 6.5 s), so each
// read gets a short budget and a shared card cache covers the tail. A slow read never
// stalls the fleet: without usable cache it allows (fail-open, `card-timeout`), with
// stale cache it decides from cache (marked `cached: true`, never `card-unreadable`).
export const REQUEST_TIMEOUT_MS_DEFAULT = 900;
export const REQUEST_TIMEOUT_MS_MIN = 50;
export const REQUEST_TIMEOUT_MS_MAX = 8000;
// A fresh cache entry skips the network entirely (single-digit ms). A stale entry is
// only used when the network is slow or fails, so a fast API always wins over cache.
export const CACHE_FRESH_MS_DEFAULT = 30 * 1000;
export const CACHE_STALE_MS_DEFAULT = 5 * 60 * 1000;
const LOG_ROTATE_BYTES = 20 * 1024 * 1024;

const GUARDED_STATUSES = new Set(["in_progress", "todo"]);

const nonEmpty = (value) => Array.isArray(value) && value.length > 0;

/** A blocker only counts while it is still unresolved. `cancelled` does not resolve a blocker. */
export function unresolvedBlockers(card) {
  const listed = Array.isArray(card?.blockedBy) ? card.blockedBy.filter((blocker) => blocker?.status !== "done") : [];
  const counted = Number(card?.blockerAttention?.unresolvedBlockerCount ?? 0);
  return Math.max(listed.length, Number.isFinite(counted) ? counted : 0);
}

/** The monitor block, whichever field the API serves it in. */
export function monitorOf(card) {
  const monitor = card?.executionState?.monitor ?? card?.executionPolicy?.monitor ?? null;
  return monitor && typeof monitor === "object" ? monitor : null;
}

export function isMonitorExhausted(card) {
  const monitor = monitorOf(card);
  if (!monitor) return false;
  const { attemptCount, maxAttempts } = monitor;
  return Number.isInteger(attemptCount) && Number.isInteger(maxAttempts) && attemptCount >= maxAttempts;
}

/**
 * Is this card, as the run leaves it, one the platform will repair?
 *
 * Pure: the card is the API shape of `GET /api/issues/:id`, `ctx` carries the run identity and the
 * one fact that needs a second read. While `pendingInteractions` is `undefined` and the card would
 * otherwise be handed back, the answer is `need-extras` so the caller can fetch it; the replay passes
 * it so it never needs to. `ctx.resumeQueued` is for the replay only: the hook never sets it,
 * because a resume write is not a live path (see the header).
 *
 * Returns `{ action: "allow" | "block" | "need-extras", why, kind? }`.
 */
export function classifyCard(card, ctx = {}) {
  if (!card || typeof card !== "object") return { action: "allow", why: "no-card" };
  const status = String(card.status ?? "");
  if (!GUARDED_STATUSES.has(status)) return { action: "allow", why: `status-${status || "unknown"}` };
  // Chat turns (Agent Chat, and GitHub/Slack channel threads) end in_progress or todo by design.
  if (card.conversationAgentId || card.originKind === "chat_channel" || card.externalChannelBinding) {
    return { action: "allow", why: "conversation" };
  }
  // A card the run's agent does not own cannot be fixed by this run, and writes would 403.
  if (!ctx.agentId || card.assigneeAgentId !== ctx.agentId) return { action: "allow", why: "not-assignee" };
  if (card.executionRunId && ctx.runId && card.executionRunId !== ctx.runId) {
    return { action: "allow", why: "other-run-holds-card" };
  }

  const monitor = monitorOf(card);
  if (card.monitorNextCheckAt || monitor?.nextCheckAt) return { action: "allow", why: "monitor-scheduled" };
  if (unresolvedBlockers(card) > 0) return { action: "allow", why: "blockers" };
  // The disposition repair upserts its own wait (kind `deliberate_wait_without_target`) and leaves
  // it active through the repair run, so it must not count as a live path: that run is exactly
  // the one whose ending triggers the next repair or the escalation to `blocked`. Any other
  // recovery the platform owns is a live path.
  const recoveryAction = card.activeRecoveryAction;
  const retry = card.scheduledRetry;
  // A scheduled-retry row for this very run is not a future wake: it is this run. Repair
  // attempts 2+ park a `scheduled_retry` row (reason `issue_disposition_repair`) while the run
  // executes, and the card carries it until the next repair or the escalation. A row for another
  // run is a real future wake and a live path. A row with no runId cannot be proven ours, so it
  // stays a live path.
  const retryIsLive = Boolean(retry) && retry.runId !== ctx.runId;
  // successfulRunHandoff stays on the card after resolution or escalation. Only a required
  // handoff for another run (or with no correctiveRunId, which we cannot prove is ours) is live.
  // A required handoff for this run is not a future wake; the corrective run must still choose a
  // valid disposition. A null correctiveRunId cannot be proven to be this run. Do not use
  // hasLiveContinuation: it can include this run itself.
  const handoff = card.successfulRunHandoff;
  const handoffIsLive = Boolean(handoff) && handoff.state === "required" && handoff.correctiveRunId !== ctx.runId;
  const recoveryIsLive =
    (recoveryAction && recoveryAction.kind !== "deliberate_wait_without_target") ||
    retryIsLive ||
    handoffIsLive;
  if (recoveryIsLive) {
    return { action: "allow", why: "platform-recovery-pending" };
  }
  const execution = card.executionState;
  if (execution?.currentParticipant || execution?.reviewRequest) return { action: "allow", why: "review-path" };

  const kind = isMonitorExhausted(card) ? "monitor-exhausted" : "no-live-path";
  if (ctx.pendingInteractions === undefined) return { action: "need-extras", why: kind, kind };
  if (ctx.pendingInteractions > 0) return { action: "allow", why: "interaction-pending" };
  if (ctx.resumeQueued) return { action: "allow", why: "continuation-queued" };
  return { action: "block", why: kind, kind };
}

/** Pending interactions from `GET /api/issues/:id/interactions`. */
export function countPendingInteractions(interactions) {
  return Array.isArray(interactions) ? interactions.filter((item) => item?.status === "pending").length : 0;
}

const safeIdentifier = (card) => (/^[A-Za-z]{2,10}-\d{1,7}$/.test(String(card?.identifier ?? "")) ? card.identifier : "this card");

/** The rule, in one short block. Wording is not pinned by tests; the facts in it are. */
export function handbackReason(verdict, card) {
  const id = safeIdentifier(card);
  const status = card?.status === "todo" ? "todo" : "in_progress";
  const monitor = monitorOf(card);
  const lead =
    verdict.kind === "monitor-exhausted"
      ? `${id} is ${status} and its monitor budget is used up (${monitor?.attemptCount} of ${monitor?.maxAttempts} checks), so nothing will wake it.`
      : `${id} is ${status} and has no live path: no monitor is scheduled, nothing blocks it and no interaction is pending.`;
  const how =
    verdict.kind === "monitor-exhausted"
      ? "A monitor cannot be re-armed on this card (422 max_attempts_exhausted). Either wait in this run in bounded " +
        "sleeps, or open a child card for the wait (it gets its own monitor budget, maxAttempts 100) and block this card on it."
      : "Waiting on CI or review: arm executionPolicy.monitor now with maxAttempts 100 (the cap) and checks at least 30 " +
        "minutes apart, sized from the CI tail you saw. More work to do: keep working in this run, or open a child card " +
        "and block this card on it.";
  return (
    `${lead} If this run ends now, Paperclip queues a disposition-repair run and, after 2 failed repairs, ` +
    `escalates the card to blocked. ${how} A resume PATCH does not count as a live path. Finished: status done with ` +
    "the deliverable. Never end the run on todo. Do this now with a tool call, then give a short final summary."
  );
}

/** Optional tuning file beside the script, so an operator can retune without editing settings. */
export function loadConfig(dir = HERE) {
  try {
    const parsed = JSON.parse(readFileSync(join(dir, "disposition-stop-guard.config.json"), "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function resolveMode(env, config) {
  const wanted = String(env.DISPOSITION_STOP_GUARD_MODE ?? config.mode ?? DEFAULT_MODE).toLowerCase();
  return MODES.includes(wanted) ? wanted : DEFAULT_MODE;
}

/** Per-read budget in ms. Precedence: environment, then the config file, then the default. */
export function requestTimeoutMs(env = {}, config = {}) {
  const raw = env.DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS ?? config.requestTimeoutMs ?? "";
  const parsed = Number.parseInt(String(raw), 10);
  if (String(raw) === "" || !Number.isInteger(parsed)) return REQUEST_TIMEOUT_MS_DEFAULT;
  return Math.min(REQUEST_TIMEOUT_MS_MAX, Math.max(REQUEST_TIMEOUT_MS_MIN, parsed));
}

function cacheTtls(env = {}, config = {}) {
  const freshRaw = env.DISPOSITION_STOP_GUARD_CACHE_FRESH_MS ?? config.cacheFreshMs ?? "";
  const staleRaw = env.DISPOSITION_STOP_GUARD_CACHE_STALE_MS ?? config.cacheStaleMs ?? "";
  const freshParsed = Number.parseInt(String(freshRaw), 10);
  const staleParsed = Number.parseInt(String(staleRaw), 10);
  const fresh = String(freshRaw) === "" || !Number.isInteger(freshParsed) ? CACHE_FRESH_MS_DEFAULT : Math.max(0, freshParsed);
  const stale = String(staleRaw) === "" || !Number.isInteger(staleParsed) ? CACHE_STALE_MS_DEFAULT : Math.max(fresh, staleParsed);
  return { fresh, stale };
}

/**
 * Shared card cache, so a slow control plane does not stall every Stop.
 * Deliberately NOT the run scratch dir (deleted with the run): the tail to bound is
 * first-Stops of unrelated runs on the same box hitting the same slow API. Keyed by
 * task id; entries carry no credential, only the card shape the classifier reads.
 * A block (or would-block) verdict is never served from cache and never written:
 * the Stop after a hand-back always re-reads, so a card the agent just fixed
 * (done, or a monitor armed) cannot be blocked again from the pre-fix card.
 */
export function cacheFileFor(env = {}, taskId = "") {
  const dir = env.DISPOSITION_STOP_GUARD_CACHE_DIR || join(tmpdir(), "disposition-stop-guard");
  const safe = String(taskId).replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 128) || "unknown";
  return { dir, file: join(dir, `card-cache.${safe}.json`) };
}

/**
 * The card shape the classifier reads, and nothing else. Title, description and
 * every other API field never reach the cache file (the guard never writes
 * model text or card text anywhere).
 */
export function projectCardForCache(card) {
  if (!card || typeof card !== "object") return null;
  const out = {};
  if (card.status !== undefined) out.status = card.status;
  if (card.identifier !== undefined) out.identifier = card.identifier;
  if (card.conversationAgentId !== undefined) out.conversationAgentId = card.conversationAgentId;
  if (card.originKind !== undefined) out.originKind = card.originKind;
  if (card.externalChannelBinding !== undefined) {
    // The classifier only checks truthiness; keep the value when it is a
    // primitive, otherwise keep presence so no card text is stored.
    const value = card.externalChannelBinding;
    out.externalChannelBinding = value && typeof value === "object" ? true : value;
  }
  if (card.assigneeAgentId !== undefined) out.assigneeAgentId = card.assigneeAgentId;
  if (card.executionRunId !== undefined) out.executionRunId = card.executionRunId;
  if (card.monitorNextCheckAt !== undefined) out.monitorNextCheckAt = card.monitorNextCheckAt;
  if (card.executionState !== undefined && card.executionState !== null) {
    if (typeof card.executionState !== "object") {
      out.executionState = card.executionState;
    } else {
      const projected = {};
      if (card.executionState.monitor !== undefined && card.executionState.monitor !== null) {
        if (typeof card.executionState.monitor !== "object") {
          projected.monitor = card.executionState.monitor;
        } else {
          const monitor = {};
          if (card.executionState.monitor.status !== undefined) monitor.status = card.executionState.monitor.status;
          if (card.executionState.monitor.nextCheckAt !== undefined) monitor.nextCheckAt = card.executionState.monitor.nextCheckAt;
          if (card.executionState.monitor.attemptCount !== undefined) monitor.attemptCount = card.executionState.monitor.attemptCount;
          if (card.executionState.monitor.maxAttempts !== undefined) monitor.maxAttempts = card.executionState.monitor.maxAttempts;
          projected.monitor = monitor;
        }
      } else if (card.executionState.monitor !== undefined) {
        projected.monitor = card.executionState.monitor;
      }
      if (card.executionState.currentParticipant !== undefined) {
        projected.currentParticipant = card.executionState.currentParticipant && typeof card.executionState.currentParticipant === "object" ? true : card.executionState.currentParticipant;
      }
      if (card.executionState.reviewRequest !== undefined) {
        projected.reviewRequest = card.executionState.reviewRequest && typeof card.executionState.reviewRequest === "object" ? true : card.executionState.reviewRequest;
      }
      out.executionState = projected;
    }
  }
  if (card.executionPolicy !== undefined && card.executionPolicy !== null) {
    if (typeof card.executionPolicy !== "object") {
      out.executionPolicy = card.executionPolicy;
    } else if (card.executionPolicy.monitor !== undefined) {
      const monitor = card.executionPolicy.monitor;
      if (monitor === null || typeof monitor !== "object") {
        out.executionPolicy = { monitor };
      } else {
        const projected = {};
        if (monitor.status !== undefined) projected.status = monitor.status;
        if (monitor.nextCheckAt !== undefined) projected.nextCheckAt = monitor.nextCheckAt;
        if (monitor.attemptCount !== undefined) projected.attemptCount = monitor.attemptCount;
        if (monitor.maxAttempts !== undefined) projected.maxAttempts = monitor.maxAttempts;
        out.executionPolicy = { monitor: projected };
      }
    }
  }
  if (Array.isArray(card.blockedBy)) {
    out.blockedBy = card.blockedBy.map((blocker) =>
      blocker && typeof blocker === "object" ? { status: blocker.status } : { status: undefined },
    );
  }
  if (card.blockerAttention !== undefined && card.blockerAttention !== null) {
    if (typeof card.blockerAttention !== "object") {
      out.blockerAttention = card.blockerAttention;
    } else if (card.blockerAttention.unresolvedBlockerCount !== undefined) {
      out.blockerAttention = { unresolvedBlockerCount: card.blockerAttention.unresolvedBlockerCount };
    } else {
      out.blockerAttention = {};
    }
  }
  if (card.activeRecoveryAction !== undefined && card.activeRecoveryAction !== null) {
    if (typeof card.activeRecoveryAction !== "object") {
      out.activeRecoveryAction = card.activeRecoveryAction;
    } else if (card.activeRecoveryAction.kind !== undefined) {
      out.activeRecoveryAction = { kind: card.activeRecoveryAction.kind };
    } else {
      out.activeRecoveryAction = {};
    }
  }
  if (card.scheduledRetry !== undefined && card.scheduledRetry !== null) {
    if (typeof card.scheduledRetry !== "object") {
      out.scheduledRetry = card.scheduledRetry;
    } else if (card.scheduledRetry.runId !== undefined) {
      out.scheduledRetry = { runId: card.scheduledRetry.runId };
    } else {
      out.scheduledRetry = {};
    }
  }
  if (card.successfulRunHandoff !== undefined && card.successfulRunHandoff !== null) {
    if (typeof card.successfulRunHandoff !== "object") {
      out.successfulRunHandoff = card.successfulRunHandoff;
    } else {
      const projected = {};
      if (card.successfulRunHandoff.state !== undefined) projected.state = card.successfulRunHandoff.state;
      if (card.successfulRunHandoff.correctiveRunId !== undefined) projected.correctiveRunId = card.successfulRunHandoff.correctiveRunId;
      out.successfulRunHandoff = projected;
    }
  }
  return out;
}

export function readCardCache(env, taskId) {
  const { file } = cacheFileFor(env, taskId);
  try {
    const parsed = JSON.parse(readFileSync(file, "utf8"));
    if (!parsed || typeof parsed !== "object" || parsed.taskId !== String(taskId)) return null;
    if (!parsed.card || typeof parsed.card !== "object") return null;
    if (!Number.isInteger(parsed.ts)) return null;
    const pending = Number.isInteger(parsed.pendingInteractions) && parsed.pendingInteractions >= 0 ? parsed.pendingInteractions : undefined;
    return { ts: parsed.ts, card: parsed.card, pendingInteractions: pending, ageMs: Date.now() - parsed.ts };
  } catch {
    return null;
  }
}

export function writeCardCache(env, taskId, { card, pendingInteractions } = {}) {
  const projected = projectCardForCache(card);
  if (!projected) return false;
  const { dir, file } = cacheFileFor(env, taskId);
  try {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const temporary = `${file}.${process.pid}.tmp`;
    const payload = {
      v: 1,
      taskId: String(taskId),
      ts: Date.now(),
      card: projected,
      ...(Number.isInteger(pendingInteractions) && pendingInteractions >= 0 ? { pendingInteractions } : {}),
    };
    writeFileSync(temporary, `${JSON.stringify(payload)}\n`, { mode: 0o600 });
    renameSync(temporary, file);
    // The hand-back counter lives in the run scratch dir in production, so its
    // prune never touches this dir. Prune here instead: every cache write bounds
    // the shared dir, in tests and in production alike.
    try {
      const ttls = cacheTtls(env, {});
      pruneOldCache(dir, Math.min(ttls.stale, 48 * 3600 * 1000));
    } catch {
      /* pruning must never fail the write */
    }
    return true;
  } catch {
    return false;
  }
}

export function deleteCardCache(env, taskId) {
  const { file } = cacheFileFor(env, taskId);
  try {
    unlinkSync(file);
    return true;
  } catch {
    return false;
  }
}

/** Prune expired card-cache entries in any cache dir (shared or isolated). Bounded. */
function pruneOldCache(dir, maxAgeMs = 48 * 3600 * 1000) {
  try {
    const cutoff = Date.now() - maxAgeMs;
    let removed = 0;
    for (const name of readdirSync(dir)) {
      if (removed >= 50) break;
      if (!name.startsWith("card-cache.") || !name.endsWith(".json")) continue;
      try {
        if (statSync(join(dir, name)).mtimeMs < cutoff) {
          unlinkSync(join(dir, name));
          removed += 1;
        }
      } catch {
        /* another stop is pruning too */
      }
    }
  } catch {
    /* a missing dir is not an error */
  }
}

function stateFile(env, key) {
  const dir =
    env.DISPOSITION_STOP_GUARD_STATE_DIR ||
    env.PAPERCLIP_RUN_SCRATCH_DIR ||
    join(tmpdir(), "disposition-stop-guard");
  // The file name carries the guard's name so it can never collide with the Muse guard's counter
  // when both live in the same run scratch directory.
  return { dir, file: join(dir, `disposition-stop-guard.${String(key).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`) };
}

export function readHandbacks(env, key) {
  const { file } = stateFile(env, key);
  try {
    const parsed = JSON.parse(readFileSync(file, "utf8"));
    return Number.isInteger(parsed?.handbacks) && parsed.handbacks >= 0
      ? { ok: true, handbacks: parsed.handbacks }
      : { ok: false, handbacks: 0 };
  } catch (error) {
    // A missing file is a fresh run. Anything else means the counter is unreliable.
    return error?.code === "ENOENT" ? { ok: true, handbacks: 0 } : { ok: false, handbacks: 0 };
  }
}

export function writeHandbacks(env, key, handbacks) {
  const { dir, file } = stateFile(env, key);
  try {
    mkdirSync(dir, { recursive: true });
    const temporary = `${file}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify({ handbacks, updatedAt: new Date().toISOString() })}\n`);
    renameSync(temporary, file);
    pruneOldState(dir);
    return true;
  } catch {
    return false;
  }
}

/** Only the shared fallback directory needs pruning; a run scratch dir is deleted with the run. */
function pruneOldState(dir) {
  if (!dir.endsWith("disposition-stop-guard")) return;
  const cutoff = Date.now() - 48 * 3600 * 1000;
  let removed = 0;
  for (const name of readdirSync(dir)) {
    if (removed >= 50) break;
    if (!name.endsWith(".json")) continue;
    try {
      const path = join(dir, name);
      if (statSync(path).mtimeMs < cutoff) {
        unlinkSync(path);
        removed += 1;
      }
    } catch {
      /* another stop is pruning too */
    }
  }
}

/**
 * One authenticated GET against the run's own API. Never throws: a failure is `{ ok: false }`.
 * A read that outlives its budget reports `timeout: true` so the caller can tell a slow
 * control plane apart from a true error (`card-unreadable` stays for real failures).
 */
export async function defaultFetchJson(env, path, timeoutMs = REQUEST_TIMEOUT_MS_DEFAULT) {
  const base = String(env.PAPERCLIP_API_URL ?? "").replace(/\/+$/, "").replace(/\/api$/, "");
  const key = env.PAPERCLIP_API_KEY;
  if (!base || !key) return { ok: false, status: 0, timeout: false };
  try {
    const response = await fetch(`${base}${path}`, {
      method: "GET",
      headers: { authorization: `Bearer ${key}`, accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) return { ok: false, status: response.status, timeout: false };
    return { ok: true, status: response.status, body: await response.json() };
  } catch (error) {
    const timeout = error?.name === "TimeoutError" || error?.name === "AbortError";
    return { ok: false, status: 0, timeout };
  }
}

/**
 * The whole decision, with every side effect injected. Returns
 * `{ action: "allow" | "block", why, reason?, kind?, status?, handbacks? }`. In `observe` mode a
 * card that would be handed back is an `allow` with `wouldBlock: true`.
 */
export async function decide(input, env, deps = {}) {
  try {
    return await decideUnsafe(input, env, deps);
  } catch {
    // Any failure, including a fetch dependency that throws, allows the stop.
    return { action: "allow", why: "error" };
  }
}

async function decideUnsafe(input, env, deps) {
  const config = deps.config ?? loadConfig();
  const budget = requestTimeoutMs(env, config);
  const fetchJson = deps.fetchJson ?? ((path) => defaultFetchJson(env, path, budget));
  const readCounter = deps.readHandbacks ?? readHandbacks;
  const writeCounter = deps.writeHandbacks ?? writeHandbacks;
  const killSwitch = deps.killSwitchPresent ?? (() => existsSync(join(HERE, "DISABLED")));
  const readCache = deps.readCardCache ?? readCardCache;
  const saveCache = deps.writeCardCache ?? writeCardCache;

  if (killSwitch()) return { action: "allow", why: "kill-switch" };
  if (!input || typeof input !== "object") return { action: "allow", why: "no-input" };
  if (input.hook_event_name && input.hook_event_name !== "Stop") return { action: "allow", why: "not-stop-event" };

  const mode = resolveMode(env, config);
  if (mode === "off") return { action: "allow", why: "mode-off" };
  const taskId = String(env.PAPERCLIP_TASK_ID ?? "").trim();
  if (!taskId) return { action: "allow", why: "no-task" };

  if (nonEmpty(input.background_tasks) || nonEmpty(input.session_crons)) {
    return { action: "allow", why: "background-work-pending", mode };
  }

  // Precedence: environment, then the config file, then the default.
  const max = Number.parseInt(env.DISPOSITION_STOP_GUARD_MAX_HANDBACKS ?? config.maxHandbacks ?? "", 10);
  const cap = Number.isInteger(max) && max >= 0 ? max : MAX_HANDBACKS_DEFAULT;
  const key = env.PAPERCLIP_RUN_ID || input.session_id || "unknown";
  const counter = readCounter(env, key);
  // stop_hook_active means a stop hook already continued this turn. The counter is a stronger
  // guard than the flag, but if the counter cannot be trusted the flag wins.
  if (input.stop_hook_active && !counter.ok) return { action: "allow", why: "active-and-counter-unreadable", mode };
  if (counter.handbacks >= cap) return { action: "allow", why: "handback-cap-reached", mode, handbacks: counter.handbacks };

  const ctx = { agentId: env.PAPERCLIP_AGENT_ID, runId: env.PAPERCLIP_RUN_ID };
  const ttls = cacheTtls(env, config);
  // A cached card is only usable by the run that could still own it: ownership or the
  // holding run changing under us (reassign, release to todo, repair run) invalidates the
  // entry. Monitor/blocker/interaction drift inside the TTL is the accepted staleness
  // tradeoff (bounded: 30 s fresh, 5 min stale-only-when-slow), documented in HANDOFF.md.
  const cachedCardUsable = (entry) => {
    const card = entry?.card;
    if (!card || typeof card !== "object") return false;
    if (!ctx.agentId || card.assigneeAgentId !== ctx.agentId) return false;
    if (card.executionRunId && ctx.runId && card.executionRunId !== ctx.runId) return false;
    return true;
  };
  let stale = null;
  try {
    stale = readCache(env, taskId);
  } catch {
    stale = null;
  }
  const remember = (card, pendingInteractions) => {
    try {
      saveCache(env, taskId, { card, pendingInteractions });
    } catch {
      /* the cache must never change the decision */
    }
  };
  const forget = () => {
    try {
      const del = deps.deleteCardCache ?? deleteCardCache;
      del(env, taskId);
    } catch {
      /* the cache must never change the decision */
    }
  };
  // Shared finish: a cached verdict carries `cached: true` so the log can tell a
  // sub-millisecond cache decision apart from a network one. Everything else is identical.
  // A block (or would-block) is never kept: the entry is deleted so the Stop after a
  // hand-back always re-reads instead of re-blocking from the pre-fix card.
  const finish = (verdict, card, extra = {}) => {
    if (verdict.action !== "block") return { action: "allow", why: verdict.why, mode, status: card.status, ...extra };
    forget();
    const reason = handbackReason(verdict, card);
    if (mode === "observe") {
      return { action: "allow", why: verdict.why, wouldBlock: true, kind: verdict.kind, mode, status: card.status, reasonChars: reason.length, ...extra };
    }
    if (!writeCounter(env, key, counter.handbacks + 1)) {
      return { action: "allow", why: "counter-unwritable", mode, kind: verdict.kind, status: card.status, ...extra };
    }
    return {
      action: "block",
      why: verdict.why,
      kind: verdict.kind,
      mode,
      status: card.status,
      handbacks: counter.handbacks + 1,
      reasonChars: reason.length,
      reason,
      ...extra,
    };
  };
  const fresh = stale && stale.ageMs < ttls.fresh && cachedCardUsable(stale) ? stale : null;
  const usableStale = stale && stale.ageMs < ttls.stale && cachedCardUsable(stale) ? stale : null;

  // Fast path: a fresh cache entry skips the network entirely, but never for a
  // block. A block is always re-read: the agent may have fixed the card after the
  // hand-back inside the fresh window, and the cached pre-fix card must not
  // re-block it. A fast API always wins over cache otherwise: stale entries are
  // only consulted when the network is slow.
  if (fresh) {
    const cachedCtx =
      fresh.pendingInteractions === undefined ? { ...ctx } : { ...ctx, pendingInteractions: fresh.pendingInteractions };
    const verdict = classifyCard(fresh.card, cachedCtx);
    if (verdict.action !== "need-extras") {
      if (verdict.action === "block") {
        forget();
      } else {
        return finish(verdict, fresh.card, { cached: true });
      }
    }
    // Fresh card, but the cached interactions count is missing: fetch only the light read.
    if (fresh.pendingInteractions === undefined) {
      const issuePath = `/api/issues/${encodeURIComponent(taskId)}`;
      let interactions = null;
      try {
        interactions = await fetchJson(`${issuePath}/interactions`, budget);
      } catch {
        // A fetch dependency that throws allows the stop (fail-safe), as before.
        return { action: "allow", why: "error" };
      }
      if (!interactions.ok) {
        return {
          action: "allow",
          why: interactions.timeout ? "interactions-timeout" : "interactions-unreadable",
          mode,
          kind: verdict.kind,
          status: fresh.card.status,
          ...(interactions.timeout ? { timeout: true } : {}),
        };
      }
      const pending = countPendingInteractions(interactions.body);
      const cachedVerdict = classifyCard(fresh.card, { ...ctx, pendingInteractions: pending });
      if (cachedVerdict.action === "block") {
        forget();
      } else {
        remember(fresh.card, pending);
        return finish(cachedVerdict, fresh.card, { cached: true });
      }
    }
  }

  const issuePath = `/api/issues/${encodeURIComponent(taskId)}`;
  // Both reads start together: a card that turns out fine wastes one light read, and a card that
  // needs the interactions does not pay for a second round trip against the read budget.
  // Each read aborts at the budget (default 900 ms); the hard deadline stays as backstop.
  const cardPending = fetchJson(issuePath, budget);
  const interactionsRead = fetchJson(`${issuePath}/interactions`, budget);
  if (interactionsRead && typeof interactionsRead.catch === "function") {
    interactionsRead.catch(() => {}); // never an unhandled rejection if the card read fails first
  }
  let cardRead = null;
  try {
    cardRead = await cardPending;
  } catch {
    // A fetch dependency that throws allows the stop (fail-safe), as before.
    return { action: "allow", why: "error" };
  }
  if (!cardRead.ok) {
    // Slow or failed card read: a usable stale cache decides (marked cached, never
    // `card-unreadable`); without one the stop is allowed and a timeout keeps its own
    // class so the `card-unreadable` count cannot spike on a slow control plane.
    // Only a read with no authoritative answer may fall back to cache: a fast 4xx
    // (revoked credential, deleted card) keeps the pre-cache fail-open behaviour.
    const noAnswer = cardRead.timeout === true || cardRead.status === 0 || cardRead.status >= 500;
    const usable = noAnswer ? usableStale : null;
    if (usable) {
      const usableCtx =
        usable.pendingInteractions === undefined ? { ...ctx } : { ...ctx, pendingInteractions: usable.pendingInteractions };
      let verdict = classifyCard(usable.card, usableCtx);
      if (verdict.action === "need-extras") {
        // The card read failed but the parallel interactions read may have made it: a
        // stale card plus a fresh count is still a decision, not a fail-open.
        let interactions = null;
        try {
          interactions = await interactionsRead;
        } catch {
          interactions = null;
        }
        if (interactions && interactions.ok) {
          const pending = countPendingInteractions(interactions.body);
          const freshVerdict = classifyCard(usable.card, { ...ctx, pendingInteractions: pending });
          if (freshVerdict.action === "block") {
            // A stale card plus a fresh count is still a stale card: never block from it.
            forget();
            return {
              action: "allow",
              why: cardRead.timeout ? "card-timeout" : "card-unreadable",
              mode,
              httpStatus: cardRead.status,
              ...(cardRead.timeout ? { timeout: true } : {}),
            };
          }
          remember(usable.card, pending);
          verdict = freshVerdict;
        } else if (usable.pendingInteractions !== undefined) {
          verdict = classifyCard(usable.card, { ...ctx, pendingInteractions: usable.pendingInteractions });
        } else {
          return {
            action: "allow",
            why: cardRead.timeout ? "card-timeout" : "card-unreadable",
            mode,
            httpStatus: cardRead.status,
            ...(cardRead.timeout ? { timeout: true } : {}),
          };
        }
      }
      if (verdict.action === "block") {
        // Never serve a block from a stale card: the post-fix read may just be slow.
        // Fail open on the read's own class and drop the stale block entry.
        forget();
        return {
          action: "allow",
          why: cardRead.timeout ? "card-timeout" : "card-unreadable",
          mode,
          httpStatus: cardRead.status,
          ...(cardRead.timeout ? { timeout: true } : {}),
        };
      }
      return finish(verdict, usable.card, { cached: true });
    }
    return {
      action: "allow",
      why: cardRead.timeout ? "card-timeout" : "card-unreadable",
      mode,
      httpStatus: cardRead.status,
      ...(cardRead.timeout ? { timeout: true } : {}),
    };
  }
  const card = cardRead.body;
  const ctx2 = { agentId: env.PAPERCLIP_AGENT_ID, runId: env.PAPERCLIP_RUN_ID };

  let verdict = classifyCard(card, ctx2);
  if (verdict.action === "need-extras") {
    let interactions = null;
    try {
      interactions = await interactionsRead;
    } catch {
      return { action: "allow", why: "error", mode, kind: verdict.kind, status: card.status };
    }
    if (!interactions.ok) {
      // A stale count for the same still-owned card still decides, but never a
      // block: a stale zero must not hand back a run whose live count is pending.
      if (usableStale && usableStale.pendingInteractions !== undefined) {
        const staleVerdict = classifyCard(card, { ...ctx2, pendingInteractions: usableStale.pendingInteractions });
        if (staleVerdict.action === "block") {
          return {
            action: "allow",
            why: interactions.timeout ? "interactions-timeout" : "interactions-unreadable",
            mode,
            kind: verdict.kind,
            status: card.status,
            ...(interactions.timeout ? { timeout: true } : {}),
          };
        }
        return finish(staleVerdict, card, { cached: true });
      }
      return {
        action: "allow",
        why: interactions.timeout ? "interactions-timeout" : "interactions-unreadable",
        mode,
        kind: verdict.kind,
        status: card.status,
        ...(interactions.timeout ? { timeout: true } : {}),
      };
    }
    const pending = countPendingInteractions(interactions.body);
    verdict = classifyCard(card, { ...ctx2, pendingInteractions: pending });
    // A block is never cached: the next Stop always re-reads. Only an allow (or a
    // need-extras that cannot happen here) is worth remembering.
    if (verdict.action !== "block") remember(card, pending);
  } else {
    // The fresh card decided without the count: store allows alone so a later Stop
    // that needs extras refetches the count instead of trusting a count from an older card.
    if (verdict.action !== "block") remember(card);
  }
  return finish(verdict, card);
}

const SILENT_WHYS = new Set(["kill-switch", "no-input", "not-stop-event", "mode-off", "no-task"]);

function logDecision(env, decision, input, startedAt) {
  if (SILENT_WHYS.has(decision.why)) return;
  const path = env.DISPOSITION_STOP_GUARD_LOG || join(HERE, "decisions.jsonl");
  try {
    try {
      if (statSync(path).size > LOG_ROTATE_BYTES) renameSync(path, `${path}.1`);
    } catch {
      /* no log yet */
    }
    const line = {
      ts: new Date().toISOString(),
      action: decision.action,
      wouldBlock: decision.wouldBlock === true,
      why: decision.why,
      kind: decision.kind ?? null,
      mode: decision.mode ?? null,
      cardStatus: decision.status ?? null,
      httpStatus: decision.httpStatus ?? null,
      handbacks: decision.handbacks ?? null,
      reasonChars: decision.reasonChars ?? null,
      cached: decision.cached === true,
      timeout: decision.timeout === true,
      agent: env.PAPERCLIP_AGENT_ID ?? null,
      run: env.PAPERCLIP_RUN_ID ?? null,
      session: input?.session_id ?? null,
      stopHookActive: Boolean(input?.stop_hook_active),
      ms: Date.now() - startedAt,
    };
    appendFileSync(path, `${JSON.stringify(line)}\n`);
  } catch {
    /* a log that cannot be written must never change the decision */
  }
}

function readStdin(timeoutMs) {
  return new Promise((resolve) => {
    let raw = "";
    const timer = setTimeout(() => resolve(raw), timeoutMs);
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      raw += chunk;
    });
    process.stdin.on("end", () => {
      clearTimeout(timer);
      resolve(raw);
    });
    process.stdin.on("error", () => {
      clearTimeout(timer);
      resolve(raw);
    });
  });
}

/** The hard deadline, clamped to HARD_DEADLINE_MS; a shorter one is for tests. */
export function deadlineMs(env = {}) {
  const wanted = Number.parseInt(env.DISPOSITION_STOP_GUARD_DEADLINE_MS ?? "", 10);
  return Number.isInteger(wanted) && wanted > 0 ? Math.min(wanted, HARD_DEADLINE_MS) : HARD_DEADLINE_MS;
}

async function main() {
  setTimeout(() => process.exit(0), deadlineMs(process.env)).unref();
  const startedAt = Date.now();
  let input = null;
  try {
    input = JSON.parse(await readStdin(2000));
  } catch {
    process.exit(0);
  }
  let decision;
  try {
    decision = await decide(input, process.env);
  } catch {
    process.exit(0);
  }
  logDecision(process.env, decision, input, startedAt);
  if (decision.action === "block") {
    process.stdout.write(`${JSON.stringify({ decision: "block", reason: decision.reason })}\n`);
  }
  process.exit(0);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
