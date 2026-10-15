// Rebuild what a card looked like at a moment in time from its activity events, in the shape
// `GET /api/issues/:id` serves, so the Stop guard's own classifier can be replayed on history.
//
// Approximate by construction: activity does not record every field (the live card also has
// `executionRunId`, scheduled retries and recovery actions), so a replay is a statement about the
// classifier on reconstructed state, not a measurement of the live hook. census.mjs says so.

import { classifyCard } from "../disposition-stop-guard.mjs";

const INTERACTION_OPEN = "issue.thread_interaction_created";
const INTERACTION_CLOSE = /^issue\.thread_interaction_(accepted|answered|rejected|expired|withdrawn)$/;

export function emptyState() {
  return {
    known: false,
    status: null,
    blockers: 0,
    pendingInteractions: new Set(),
    monitor: { everScheduled: false, armed: false, nextCheckAt: null, attemptCount: 0, maxAttempts: null, status: null },
  };
}

/** Events must be sorted by createdAt. Applies every event at or before `tMs`. */
export function stateAt(events, tMs) {
  const state = emptyState();
  for (const event of events) {
    if (Date.parse(event.createdAt) > tMs) break;
    state.known = true;
    const d = event.details ?? {};
    const monitor = state.monitor;
    switch (event.action) {
      case "issue.created":
        if (d.status) state.status = d.status;
        break;
      case "issue.checked_out":
        state.status = "in_progress";
        break;
      case "issue.updated": {
        const status = d.status ?? d.changesStatusTo;
        if (status) state.status = status;
        if (d.executionStatePresent) {
          const m = d.monitor;
          if (m && typeof m === "object") {
            monitor.everScheduled = true;
            monitor.nextCheckAt = m.nextCheckAt ?? null;
            monitor.armed = Boolean(m.nextCheckAt);
            if (Number.isInteger(m.attemptCount)) monitor.attemptCount = m.attemptCount;
            if (Number.isInteger(m.maxAttempts)) monitor.maxAttempts = m.maxAttempts;
            monitor.status = m.status ?? monitor.status;
          } else {
            monitor.armed = false;
            monitor.nextCheckAt = null;
          }
        }
        if ("monitorNextCheckAt" in d) {
          monitor.nextCheckAt = d.monitorNextCheckAt ?? null;
          monitor.armed = Boolean(d.monitorNextCheckAt);
        }
        break;
      }
      case "issue.monitor_scheduled":
        monitor.everScheduled = true;
        monitor.armed = true;
        monitor.nextCheckAt = d.nextCheckAt ?? monitor.nextCheckAt;
        if (Number.isInteger(d.maxAttempts)) monitor.maxAttempts = d.maxAttempts;
        monitor.status = "scheduled";
        break;
      case "issue.monitor_triggered":
        monitor.everScheduled = true;
        monitor.armed = false;
        monitor.nextCheckAt = null;
        if (Number.isInteger(d.attemptCount)) monitor.attemptCount = d.attemptCount;
        if (Number.isInteger(d.maxAttempts)) monitor.maxAttempts = d.maxAttempts;
        monitor.status = "triggered";
        break;
      case "issue.monitor_cleared":
        monitor.armed = false;
        monitor.nextCheckAt = null;
        monitor.status = "cleared";
        break;
      case "issue.blockers_updated":
        state.blockers = Array.isArray(d.blockedByIssueIds) ? d.blockedByIssueIds.length : 0;
        break;
      case "issue.blockers_resolved_wake_emitted":
        state.blockers = 0;
        break;
      case INTERACTION_OPEN:
        if (d.interactionId) state.pendingInteractions.add(d.interactionId);
        break;
      default:
        if (INTERACTION_CLOSE.test(event.action) && d.interactionId) state.pendingInteractions.delete(d.interactionId);
    }
  }
  return state;
}

/** Did this run queue a continuation? Mirrors `runQueuedContinuation`, on slim events. */
export function resumeQueuedBy(events, runId) {
  return events.some((event) => event.runId === runId && event.details?.resumeIntent === true);
}

/** A card shaped like the API response, built from a reconstructed state. */
export function pseudoCard(state, issue) {
  const monitor = state.monitor;
  return {
    status: state.status,
    conversationAgentId: issue?.conversationAgentId ?? null,
    originKind: issue?.originKind ?? null,
    externalChannelBinding: issue?.hasExternalChannelBinding ? {} : null,
    assigneeAgentId: issue?.assigneeAgentId ?? null,
    executionRunId: null,
    monitorNextCheckAt: monitor.armed ? monitor.nextCheckAt : null,
    executionState: monitor.everScheduled
      ? {
          monitor: {
            status: monitor.status,
            nextCheckAt: monitor.armed ? monitor.nextCheckAt : null,
            attemptCount: monitor.attemptCount,
            maxAttempts: monitor.maxAttempts,
          },
        }
      : null,
    blockedBy: Array.from({ length: state.blockers }, () => ({ status: "todo" })),
    blockerAttention: { unresolvedBlockerCount: 0 },
  };
}

/** What the card looked like to the guard, as a stable key for the live-path table. */
export function signatureOf(state, resumed) {
  const monitor = state.monitor;
  const exhausted =
    monitor.everScheduled && !monitor.armed && Number.isInteger(monitor.maxAttempts) && monitor.attemptCount >= monitor.maxAttempts;
  const monitorClass = monitor.armed ? "armed" : exhausted ? "exhausted" : monitor.everScheduled ? "fired-or-cleared" : "none";
  return [
    state.status ?? "unknown",
    `monitor:${monitorClass}`,
    `blockers:${state.blockers > 0 ? "yes" : "no"}`,
    `interactions:${state.pendingInteractions.size > 0 ? "yes" : "no"}`,
    `resume:${resumed ? "yes" : "no"}`,
  ].join(" | ");
}

/**
 * The guard's verdict for one finished run, on reconstructed state. `resumeIsLivePath` is the
 * what-if: the hook itself never counts a resume write (it is not a live path), but the replay
 * can show what that would have changed.
 */
export function replayRun(run, issue, events, { resumeIsLivePath = false } = {}) {
  const end = Date.parse(run.finishedAt);
  const state = stateAt(events, end);
  if (!state.known) return { action: "allow", why: "no-activity", state, signature: "no-activity" };
  const resumed = resumeQueuedBy(events, run.id);
  const verdict = classifyCard(pseudoCard(state, issue), {
    agentId: run.agentId,
    runId: run.id,
    pendingInteractions: state.pendingInteractions.size,
    ...(resumeIsLivePath ? { resumeQueued: resumed } : {}),
  });
  return { ...verdict, state, signature: signatureOf(state, resumed) };
}

/** The COO's card-state classes, for the card as it stood when a repair wake started. */
export function cardStateClass(state) {
  if (!state.known) return "unknown";
  const monitor = state.monitor;
  const exhausted =
    monitor.everScheduled &&
    !monitor.armed &&
    Number.isInteger(monitor.maxAttempts) &&
    monitor.attemptCount >= monitor.maxAttempts;
  if (exhausted) return "monitor exhausted (any status)";
  const status = state.status;
  if (status === "blocked" || status === "done" || status === "cancelled" || status === "in_review") return status;
  const fired = monitor.everScheduled && monitor.status === "triggered" && !monitor.armed;
  const label = fired ? "monitor fired, not re-armed" : monitor.armed ? "monitor armed" : "no monitor";
  if (status === "in_progress" || status === "todo") return `${status} + ${label}`;
  return "other";
}
