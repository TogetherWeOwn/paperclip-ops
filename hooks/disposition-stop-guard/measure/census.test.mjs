// node --test hooks/disposition-stop-guard/measure/census.test.mjs
//
// The census and the replay run on a synthetic bundle whose every count is worked out by hand.

import assert from "node:assert/strict";
import test from "node:test";

import { census, renderText } from "./census.mjs";
import { slimEvent, slimRun } from "./collect-bundle.mjs";
import { cardStateClass, pseudoCard, replayRun, resumeQueuedBy, stateAt } from "./reconstruct.mjs";

const AGENT = "agent-a";
const T0 = Date.parse("2026-10-10T10:00:00.000Z");
const at = (minutes, seconds = 0) => new Date(T0 + minutes * 60_000 + seconds * 1000).toISOString();

const ev = (action, minutes, details = {}, runId = null) => ({ action, createdAt: at(minutes), runId, actorType: "agent", details });
const run = (id, issueId, wakeReason, start, end, extra = {}) => ({
  id,
  agentId: AGENT,
  issueId,
  wakeReason,
  status: "succeeded",
  createdAt: at(start),
  startedAt: at(start),
  finishedAt: at(end),
  ...extra,
});
const issue = (id, extra = {}) => ({ id, identifier: id.toUpperCase(), assigneeAgentId: AGENT, conversationAgentId: null, ...extra });
const monitorState = (nextCheckAt, attemptCount, maxAttempts, status) => ({ status, nextCheckAt, attemptCount, maxAttempts });
const updated = (minutes, details, runId = null) =>
  ev("issue.updated", minutes, { executionStatePresent: false, ...details }, runId);

// i1: a monitor wake that ends without re-arming; the platform repairs it 40 s later (the repair arms
//     a monitor). Caught.
// i2: a run that arms a monitor and ends. Fine.
// i3: the monitor budget (2) is spent; the run ends in_progress; repaired (the repair blocks the card).
//     Caught as monitor-exhausted.
// i4: no path, not repaired, but a continuation wake rescues it 10 min later and arms a monitor.
//     Flagged, recovered later.
// i5: no path, no repair wake, but the platform escalates the card to the board 30 min later. Flagged, escalated.
// i6: a chat conversation ending in_progress. Never flagged.
// i7: ends done. Fine. i8: ends with a resume PATCH from the run and nothing follows: the wrong hand-back.
// i9: another agent owns the card.
const BUNDLE = {
  meta: { collectedAt: at(300), agents: [{ name: "A", oldest: at(-600), truncated: false }, { name: "B", oldest: at(100), truncated: true }] },
  runs: [
    run("r1a", "i1", "issue_assigned", 0, 5),
    run("r1b", "i1", "issue_monitor_due", 60, 65),
    { ...run("r1c", "i1", "issue_disposition_repair", 65, 70), createdAt: at(65, 40) },
    run("r2a", "i2", "issue_assigned", 0, 5),
    run("r3a", "i3", "issue_monitor_due", 60, 65),
    { ...run("r3b", "i3", "issue_disposition_repair", 65, 70), createdAt: at(65, 30) },
    run("r4a", "i4", "issue_assigned", 0, 5),
    run("r4b", "i4", "issue_continuation_needed", 15, 20),
    run("r5a", "i5", "issue_assigned", 0, 5),
    run("r6a", "i6", "External chat message received", 0, 5),
    run("r7a", "i7", "issue_assigned", 0, 5),
    run("r8a", "i8", "issue_assigned", 0, 5),
    run("r9a", "i9", "issue_commented", 0, 5),
    run("rfail", "i5", "issue_commented", 100, 101, { status: "failed" }),
    run("r-old", "i2", "issue_assigned", -3000, -2990),
  ],
  issues: new Map([
    ["i1", { issue: issue("i1"), events: [
      ev("issue.created", -10, { status: "todo" }),
      ev("issue.checked_out", 0, {}, "r1a"),
      ev("issue.monitor_scheduled", 4, { nextCheckAt: at(60), maxAttempts: 6 }, "r1a"),
      ev("issue.monitor_triggered", 60, { attemptCount: 1, maxAttempts: 6 }),
      updated(64, { status: "in_progress" }, "r1b"),
      ev("issue.monitor_scheduled", 69, { nextCheckAt: at(200), maxAttempts: 6 }, "r1c"),
    ] }],
    ["i2", { issue: issue("i2"), events: [
      ev("issue.created", -10, { status: "todo" }),
      ev("issue.checked_out", 0, {}, "r2a"),
      ev("issue.monitor_scheduled", 4, { nextCheckAt: at(60), maxAttempts: 6 }, "r2a"),
    ] }],
    ["i3", { issue: issue("i3"), events: [
      ev("issue.created", -10, { status: "todo" }),
      ev("issue.checked_out", 0, {}),
      ev("issue.monitor_scheduled", 4, { nextCheckAt: at(60), maxAttempts: 2 }),
      ev("issue.monitor_triggered", 30, { attemptCount: 1, maxAttempts: 2 }),
      ev("issue.monitor_scheduled", 31, { nextCheckAt: at(60), maxAttempts: 2 }),
      ev("issue.monitor_triggered", 60, { attemptCount: 2, maxAttempts: 2 }),
      updated(69, { status: "blocked" }, "r3b"),
    ] }],
    ["i4", { issue: issue("i4"), events: [
      ev("issue.created", -10, { status: "todo" }),
      ev("issue.checked_out", 0, {}, "r4a"),
      ev("issue.monitor_scheduled", 19, { nextCheckAt: at(200), maxAttempts: 6 }, "r4b"),
    ] }],
    ["i5", { issue: issue("i5"), events: [
      ev("issue.created", -10, { status: "todo" }),
      ev("issue.checked_out", 0, {}, "r5a"),
      ev("issue.disposition_repair_escalated", 30, { terminalReason: "unchanged_source_state_exhausted" }),
    ] }],
    ["i6", { issue: issue("i6", { conversationAgentId: "chat" }), events: [ev("issue.created", -10, { status: "in_progress" })] }],
    ["i7", { issue: issue("i7"), events: [ev("issue.checked_out", 0), updated(4, { status: "done" }, "r7a")] }],
    ["i8", { issue: issue("i8"), events: [ev("issue.checked_out", 0), ev("issue.comment_added", 4, { resumeIntent: true }, "r8a")] }],
    ["i9", { issue: issue("i9", { assigneeAgentId: "someone-else" }), events: [ev("issue.checked_out", 0)] }],
  ]),
};

test("runs and repair wakes are counted by wake reason, with the run before each repair", () => {
  const result = census(BUNDLE, { hours: 24 });
  assert.equal(result.runs.total, 14, "the run from 50 hours earlier is outside the window");
  assert.equal(result.runs.repairRuns, 2);
  assert.equal(result.runs.repairCards, 2);
  assert.equal(result.runs.byWakeReason.issue_assigned, 6);
  assert.equal(result.runs.byWakeReason.issue_disposition_repair, 2);
  assert.equal(result.runs.repairShareOfRunsPct, 14.3);
  assert.deepEqual(result.repair.precedingWake, { issue_monitor_due: 2 });
  assert.deepEqual(result.coverage.truncatedAgents, [{ name: "B", oldest: at(100) }]);
});

test("card state at the repair wake uses the COO's classes", () => {
  const result = census(BUNDLE, { hours: 24 });
  assert.deepEqual(result.repair.cardStateAtRepairWake, {
    "in_progress + monitor fired, not re-armed": 1,
    "monitor exhausted (any status)": 1,
  });
});

test("the replay catches both repair-preceding runs and says which kind", () => {
  const { replay } = census(BUNDLE, { hours: 24 });
  assert.equal(replay.repairPreceding.runs, 2);
  assert.equal(replay.repairPreceding.caught, 2);
  assert.equal(replay.repairPreceding.caughtPct, 100);
  assert.deepEqual(replay.repairPreceding.caughtByKind, { "monitor-exhausted": 1, "no-live-path": 1 });
});

test("other runs: flagged, flagged-but-rescued-by-another-wake, and the unexplained wrong hand-back", () => {
  const { replay } = census(BUNDLE, { hours: 24 });
  // Guarded: the 14 runs in the window, minus the failed one.
  assert.equal(replay.guardedRuns, 13);
  assert.equal(replay.excluded.notSucceeded, 1);
  assert.equal(replay.others.runs, 11);
  // A resume write is not a live path, so r8a (which only resumed) is flagged too.
  assert.equal(replay.others.flagged, 3);
  assert.equal(replay.others.flaggedRecoveredLater, 1, "r4a is followed by issue_continuation_needed 10 minutes later");
  assert.equal(replay.others.flaggedEscalated, 1, "r5a is followed by a platform escalation 30 minutes later");
  assert.equal(replay.others.flaggedUnexplained, 1, "r8a is followed by nothing");
  assert.deepEqual(replay.others.flaggedByKind, { "no-live-path": 3 });
  assert.deepEqual(replay.others.flaggedByWake, { issue_assigned: 3 });
  assert.deepEqual(replay.flaggedUnexplainedSamples.map((s) => s.run), ["r8a"]);
  assert.deepEqual(replay.wrongHandbackPctOfGuardedRuns, { strict: 23.1, refined: 7.7 });
  assert.deepEqual(replay.verdictByWhy, {
    "monitor-scheduled": 4,
    "BLOCK no-live-path": 4,
    "BLOCK monitor-exhausted": 1,
    conversation: 1,
    "not-assignee": 1,
    "status-blocked": 1,
    "status-done": 1,
  });
});

test("the what-if where a resume write counts as a live path clears r8a", () => {
  const { replay } = census(BUNDLE, { hours: 24, resumeIsLivePath: true });
  assert.equal(replay.resumeIsLivePath, true);
  assert.equal(replay.others.flagged, 2);
  assert.equal(replay.others.flaggedUnexplained, 0);
  assert.equal(replay.verdictByWhy["continuation-queued"], 1);
  assert.deepEqual(replay.wrongHandbackPctOfGuardedRuns, { strict: 15.4, refined: 0 });
});

test("the signature table says which end-of-run states were repaired", () => {
  const { replay } = census(BUNDLE, { hours: 24 });
  const row = (signature) => replay.repairRateBySignature.find((r) => r.signature === signature);
  assert.deepEqual(row("in_progress | monitor:fired-or-cleared | blockers:no | interactions:no | resume:no"), {
    signature: "in_progress | monitor:fired-or-cleared | blockers:no | interactions:no | resume:no",
    runs: 1,
    repaired: 1,
    repairedPct: 100,
  });
  assert.equal(row("in_progress | monitor:exhausted | blockers:no | interactions:no | resume:no").repaired, 1);
  assert.equal(row("in_progress | monitor:armed | blockers:no | interactions:no | resume:no").repaired, 0);
  const resumed = row("in_progress | monitor:none | blockers:no | interactions:no | resume:yes");
  assert.deepEqual([resumed.runs, resumed.repaired], [1, 0]);
});

test("the text report names the window, the repair share and the wrong hand-back rate", () => {
  const text = renderText(census(BUNDLE, { hours: 24 }));
  assert.match(text, /issue_disposition_repair 2 \(14\.3%\)/);
  assert.match(text, /truncated agent pages/);
  assert.match(text, /wrong hand-back rate/);
});

test("stateAt follows the monitor from armed to fired to exhausted", () => {
  const events = BUNDLE.issues.get("i3").events;
  const armed = stateAt(events, T0 + 5 * 60_000);
  assert.equal(armed.monitor.armed, true);
  assert.equal(cardStateClass(armed), "in_progress + monitor armed");
  const fired = stateAt(events, T0 + 30.5 * 60_000);
  assert.equal(fired.monitor.armed, false);
  assert.equal(fired.monitor.attemptCount, 1);
  assert.equal(cardStateClass(fired), "in_progress + monitor fired, not re-armed");
  assert.equal(cardStateClass(stateAt(events, T0 + 61 * 60_000)), "monitor exhausted (any status)");
  assert.equal(cardStateClass(stateAt([], T0)), "unknown");
  assert.equal(cardStateClass(stateAt(BUNDLE.issues.get("i7").events, T0 + 10 * 60_000)), "done");
});

test("stateAt reads the monitor out of an issue.updated executionState and tracks blockers and interactions", () => {
  const events = [
    ev("issue.created", 0, { status: "todo" }),
    ev("issue.updated", 1, { status: "in_progress", executionStatePresent: true, monitor: monitorState(at(90), 0, 6, "scheduled"), monitorNextCheckAt: at(90) }),
    ev("issue.blockers_updated", 2, { blockedByIssueIds: ["x", "y"] }),
    ev("issue.thread_interaction_created", 3, { interactionId: "q1" }),
    ev("issue.thread_interaction_created", 4, { interactionId: "q2" }),
    ev("issue.thread_interaction_answered", 5, { interactionId: "q1" }),
  ];
  const state = stateAt(events, T0 + 10 * 60_000);
  assert.equal(state.status, "in_progress");
  assert.equal(state.monitor.armed, true);
  assert.equal(state.blockers, 2);
  assert.deepEqual([...state.pendingInteractions], ["q2"]);
  const cleared = stateAt([...events, ev("issue.blockers_resolved_wake_emitted", 6), ev("issue.updated", 7, { executionStatePresent: true, monitor: null, monitorNextCheckAt: null })], T0 + 10 * 60_000);
  assert.equal(cleared.blockers, 0);
  assert.equal(cleared.monitor.armed, false);
  assert.equal(pseudoCard(cleared, issue("z")).monitorNextCheckAt, null);
});

test("replayRun on a card with no recorded activity is not a block", () => {
  const verdict = replayRun(run("rx", "ix", "issue_assigned", 0, 5), issue("ix"), []);
  assert.equal(verdict.action, "allow");
  assert.equal(verdict.why, "no-activity");
});

test("the collector keeps only what the replay reads, and no text", () => {
  const raw = {
    action: "issue.updated",
    createdAt: at(1),
    runId: "r",
    actorType: "agent",
    details: {
      status: "in_progress",
      identifier: "case_8c5fe4cc",
      bodySnippet: "SECRET-COMMENT-TEXT",
      changes: { status: { to: "in_progress" } },
      executionState: { monitor: { status: "scheduled", nextCheckAt: at(60), attemptCount: 0, maxAttempts: 6, notes: "SECRET-NOTE", externalRef: "SECRET-REF" } },
    },
  };
  const slim = slimEvent(raw);
  assert.equal(slim.details.status, "in_progress");
  assert.equal(slim.details.executionStatePresent, true);
  assert.deepEqual(slim.details.monitor, { status: "scheduled", nextCheckAt: at(60), attemptCount: 0, maxAttempts: 6 });
  assert.ok(!JSON.stringify(slim).includes("SECRET"));
  assert.equal(slimEvent({ action: "tool_gateway.call_allowed", createdAt: at(1), details: {} }), null);
  assert.equal(slimEvent({ action: "issue.thread_interaction_created", createdAt: at(1), details: { interactionId: "q" } }).details.interactionId, "q");
  const slimmed = slimRun({ id: "r", agentId: "a", createdAt: at(1), status: "succeeded", resultJson: { summary: "SECRET-RESULT" }, contextSnapshot: { wakeReason: "issue_assigned", issueId: "i" } });
  assert.equal(slimmed.wakeReason, "issue_assigned");
  assert.ok(!JSON.stringify(slimmed).includes("SECRET"));
});

test("a monitor that was cleared is no longer armed, and a resume by another run is not this run's", () => {
  const events = [
    ev("issue.created", 0, { status: "todo" }),
    ev("issue.checked_out", 1, {}, "r"),
    ev("issue.monitor_scheduled", 2, { nextCheckAt: at(60), maxAttempts: 6 }, "r"),
    ev("issue.monitor_cleared", 3, { reason: "cancelled" }),
    ev("issue.comment_added", 4, { resumeIntent: true }, "other-run"),
  ];
  const state = stateAt(events, T0 + 10 * 60_000);
  assert.equal(state.monitor.armed, false);
  assert.equal(pseudoCard(state, issue("z")).monitorNextCheckAt, null);
  assert.equal(resumeQueuedBy(events, "r"), false);
  assert.equal(resumeQueuedBy(events, "other-run"), true);
  // The what-if "resume counts" must not rescue a run that did not resume.
  const verdict = replayRun({ id: "r", agentId: AGENT, finishedAt: at(10) }, issue("z"), events, { resumeIsLivePath: true });
  assert.equal(verdict.action, "block");
});
