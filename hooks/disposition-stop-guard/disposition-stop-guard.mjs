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
// interactions). If the card is `in_progress`
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
//   - block on an error, a missing credential, an unreadable card, or after the 8 s deadline.
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
// The card read is the slow one (p50 2.6 s, p90 4.6 s measured 2026-10-10); the interactions read is light.
const REQUEST_TIMEOUT_MS = 6500;
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

/** One authenticated GET against the run's own API. Never throws: a failure is `{ ok: false }`. */
export async function defaultFetchJson(env, path, timeoutMs = REQUEST_TIMEOUT_MS) {
  const base = String(env.PAPERCLIP_API_URL ?? "").replace(/\/+$/, "").replace(/\/api$/, "");
  const key = env.PAPERCLIP_API_KEY;
  if (!base || !key) return { ok: false, status: 0 };
  try {
    const response = await fetch(`${base}${path}`, {
      method: "GET",
      headers: { authorization: `Bearer ${key}`, accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) return { ok: false, status: response.status };
    return { ok: true, status: response.status, body: await response.json() };
  } catch {
    return { ok: false, status: 0 };
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
  const fetchJson = deps.fetchJson ?? ((path) => defaultFetchJson(env, path));
  const readCounter = deps.readHandbacks ?? readHandbacks;
  const writeCounter = deps.writeHandbacks ?? writeHandbacks;
  const killSwitch = deps.killSwitchPresent ?? (() => existsSync(join(HERE, "DISABLED")));
  const config = deps.config ?? loadConfig();

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

  const issuePath = `/api/issues/${encodeURIComponent(taskId)}`;
  // Both reads start together: a card that turns out fine wastes one light read, and a card that
  // needs the interactions does not pay for a second round trip against the 8 s deadline.
  const cardPending = fetchJson(issuePath);
  const interactionsRead = fetchJson(`${issuePath}/interactions`);
  interactionsRead.catch(() => {}); // never an unhandled rejection if the card read fails first
  const cardRead = await cardPending;
  if (!cardRead.ok) return { action: "allow", why: "card-unreadable", mode, httpStatus: cardRead.status };
  const card = cardRead.body;
  const ctx = { agentId: env.PAPERCLIP_AGENT_ID, runId: env.PAPERCLIP_RUN_ID };

  let verdict = classifyCard(card, ctx);
  if (verdict.action === "need-extras") {
    const interactions = await interactionsRead;
    if (!interactions.ok) return { action: "allow", why: "interactions-unreadable", mode, kind: verdict.kind, status: card.status };
    verdict = classifyCard(card, { ...ctx, pendingInteractions: countPendingInteractions(interactions.body) });
  }
  if (verdict.action !== "block") return { action: "allow", why: verdict.why, mode, status: card.status };

  const reason = handbackReason(verdict, card);
  if (mode === "observe") {
    return { action: "allow", why: verdict.why, wouldBlock: true, kind: verdict.kind, mode, status: card.status, reasonChars: reason.length };
  }
  if (!writeCounter(env, key, counter.handbacks + 1)) {
    return { action: "allow", why: "counter-unwritable", mode, kind: verdict.kind, status: card.status };
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
  };
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
