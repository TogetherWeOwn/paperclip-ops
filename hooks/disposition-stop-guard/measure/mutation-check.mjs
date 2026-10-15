#!/usr/bin/env node
// Hand-applied mutation check for the Stop guard, scripted so it can be re-run:
//   node hooks/disposition-stop-guard/measure/mutation-check.mjs
//
// For each mutant it copies the guard directory to a temp dir, applies one textual change to the
// hook, runs the unit suite there, and requires the suite to FAIL. A mutant the suite lets through
// is a test the guard is missing. The mutated copies never touch the repository.

import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..");

const HOOK_FILE = "disposition-stop-guard.mjs";
const HOOK_SUITE = "disposition-stop-guard.test.mjs";
const REPLAY_FILE = join("measure", "reconstruct.mjs");
const REPLAY_SUITE = join("measure", "census.test.mjs");

/** [name, find, replace, file, suite]. `find` must occur exactly once in `file`. */
const HOOK_MUTANTS = [
  ["todo is no longer guarded", 'new Set(["in_progress", "todo"])', 'new Set(["in_progress"])'],
  ["in_review becomes guarded", 'new Set(["in_progress", "todo"])', 'new Set(["in_progress", "todo", "in_review"])'],
  ["conversations are guarded", 'if (card.conversationAgentId || card.originKind === "chat_channel" || card.externalChannelBinding)', "if (false)"],
  ["channel threads are guarded", 'card.originKind === "chat_channel" ||', "false ||"],
  ["bound channel threads are guarded", "|| card.externalChannelBinding)", "|| false)"],
  ["a card another agent owns is guarded", "card.assigneeAgentId !== ctx.agentId", "false"],
  ["a card another run holds is guarded", "card.executionRunId !== ctx.runId", "false"],
  ["a scheduled monitor is not a live path", "if (card.monitorNextCheckAt || monitor?.nextCheckAt)", "if (false)"],
  ["an unresolved blocker is not a live path", "if (unresolvedBlockers(card) > 0)", "if (false)"],
  ["a done blocker still counts as unresolved", 'blocker?.status !== "done"', "true"],
  ["a platform recovery is not a live path", "const recoveryIsLive =\n    (recoveryAction && recoveryAction.kind !== \"deliberate_wait_without_target\") ||", "const recoveryIsLive =\n    (recoveryAction) ||"],
  ["the repair's own wait counts as a live path", 'recoveryAction.kind !== "deliberate_wait_without_target"', 'recoveryAction.kind === "deliberate_wait_without_target"'],
  ["a retry for this run counts as a live path", "retry.runId !== ctx.runId", "true"],
  ["a resolved handoff counts as a live path", 'Boolean(handoff) && handoff.state === "required" && handoff.correctiveRunId !== ctx.runId', 'Boolean(handoff)'],
  ["an escalated or own-run required handoff counts as live", 'handoff.state === "required" && handoff.correctiveRunId !== ctx.runId', 'handoff.state !== "resolved"'],
  ["a required handoff for this run is live", 'handoff.state === "required" && handoff.correctiveRunId !== ctx.runId', 'handoff.state === "required"'],
  ["a required handoff with no corrective run is not live", 'handoff.state === "required" && handoff.correctiveRunId !== ctx.runId', 'handoff.state === "required" && handoff.correctiveRunId != null && handoff.correctiveRunId !== ctx.runId'],
  ["an unknown handoff state is live", 'handoff.state === "required" && handoff.correctiveRunId !== ctx.runId', 'handoff.state !== "resolved" && handoff.state !== "escalated"'],
  ["a review participant is not a live path", "if (execution?.currentParticipant || execution?.reviewRequest)", "if (false)"],
  ["a pending interaction is not a live path", "if (ctx.pendingInteractions > 0)", "if (false)"],
  ["a spent budget needs one attempt more", "attemptCount >= maxAttempts", "attemptCount > maxAttempts"],
  ["exhausted is reported as no-live-path", 'isMonitorExhausted(card) ? "monitor-exhausted" : "no-live-path"', '"no-live-path"'],
  ["the cap is off by one", "counter.handbacks >= cap", "counter.handbacks > cap"],
  ["the default cap is 3", "MAX_HANDBACKS_DEFAULT = 2", "MAX_HANDBACKS_DEFAULT = 3"],
  ["background work is ignored", "if (nonEmpty(input.background_tasks) || nonEmpty(input.session_crons))", "if (false)"],
  ["the kill switch is ignored", "if (killSwitch())", "if (false)"],
  ["a run without a task is read anyway", "if (!taskId)", "if (false)"],
  ["observe mode blocks", 'if (mode === "observe") {', "if (false) {"],
  ["mode off is ignored", 'if (mode === "off")', "if (false)"],
  ["an unwritable counter still blocks", "if (!writeCounter(env, key, counter.handbacks + 1))", "if (false)"],
  ["an unreadable counter with stop_hook_active still blocks", "if (input.stop_hook_active && !counter.ok)", "if (false)"],
  ["an unreadable card blocks", 'if (!cardRead.ok) return { action: "allow"', 'if (!cardRead.ok) return { action: "block"'],
  ["a throwing dependency escapes", "  } catch {\n    // Any failure, including", "  } finally {\n    // Any failure, including"],
  ["the deadline is not clamped", "Math.min(wanted, HARD_DEADLINE_MS)", "wanted"],
  ["the counter file collides with the Muse guard's", "`disposition-stop-guard.${String(key)", "`${String(key)"],
  ["the log keeps the model's text", "session: input?.session_id ?? null,", "session: input?.session_id ?? null, text: input?.last_assistant_message ?? null,"],
  ["the hook writes to the API", 'method: "GET"', 'method: "POST"'],
  ["the identifier is not sanitised", "? card.identifier : \"this card\"", "? card.identifier : card?.identifier"],
].map((m) => [...m, HOOK_FILE, HOOK_SUITE]);

const INSTALLER_FILE = "install-disposition-stop-guard.mjs";
const INSTALLER_MUTANTS = [
  ["uninstall is allowed from an agent run on the user-scope file", "if (command === \"uninstall\") {", "if (false) {"],
  ["a per-agent agents/ tree counts as a shared path", "/[/\\\\]instances[/\\\\].*[/\\\\]agents[/\\\\][^/\\\\]+[/\\\\]/.test(path)", "false"],
  ["install is allowed from an agent run on the user-scope file", "agentRun: isAgentRunContext(),", "agentRun: false,"],
].map((m) => [...m, INSTALLER_FILE, HOOK_SUITE]);

// The replay reconstructs card state from activity; a wrong reconstruction would make the evidence wrong.
const REPLAY_MUTANTS = [
  ["a fired monitor stays armed", 'monitor.armed = false;\n        monitor.nextCheckAt = null;\n        if (Number.isInteger(d.attemptCount))', 'if (Number.isInteger(d.attemptCount))'],
  ["a cleared monitor stays armed", 'monitor.armed = false;\n        monitor.nextCheckAt = null;\n        monitor.status = "cleared";', 'monitor.status = "cleared";'],
  ["a scheduled monitor is not armed", "monitor.everScheduled = true;\n        monitor.armed = true;", "monitor.everScheduled = true;\n        monitor.armed = false;"],
  ["checkout does not set in_progress", 'case "issue.checked_out":\n        state.status = "in_progress";', 'case "issue.checked_out":\n        state.status = state.status;'],
  ["a closed interaction stays pending", "state.pendingInteractions.delete(d.interactionId)", "void d.interactionId"],
  ["blockers resolving does not clear them", 'case "issue.blockers_resolved_wake_emitted":\n        state.blockers = 0;', 'case "issue.blockers_resolved_wake_emitted":\n        state.blockers = state.blockers;'],
  ["events after the run ended are applied", "if (Date.parse(event.createdAt) > tMs) break;", "if (false) break;"],
  ["a resume by another run counts", "event.runId === runId && event.details?.resumeIntent === true", "event.details?.resumeIntent === true"],
].map((m) => [...m, REPLAY_FILE, REPLAY_SUITE]);

export const MUTANTS = [...HOOK_MUTANTS, ...INSTALLER_MUTANTS, ...REPLAY_MUTANTS];

function run(mutant) {
  const dir = mkdtempSync(join(tmpdir(), "disposition-mutant-"));
  try {
    cpSync(SRC, join(dir, "guard"), { recursive: true });
    const [, find, replace, file, suite] = mutant;
    const hook = join(dir, "guard", file);
    const text = readFileSync(hook, "utf8");
    const count = text.split(find).length - 1;
    if (count !== 1) return { applied: false, reason: `pattern occurs ${count} times` };
    writeFileSync(hook, text.replace(find, () => replace));
    const result = spawnSync("node", ["--test", join(dir, "guard", suite)], {
      encoding: "utf8",
      env: { ...process.env, PAPERCLIP_RUN_ID: "", PAPERCLIP_AGENT_ID: "", PAPERCLIP_TASK_ID: "" },
      timeout: 180000,
    });
    return { applied: true, killed: result.status !== 0 };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

let survivors = 0;
let unapplied = 0;
for (const mutant of MUTANTS) {
  const outcome = run(mutant);
  if (!outcome.applied) unapplied += 1;
  else if (!outcome.killed) survivors += 1;
  process.stdout.write(`${!outcome.applied ? "SKIP" : outcome.killed ? "killed" : "SURVIVED"}  ${mutant[0]}${outcome.reason ? ` (${outcome.reason})` : ""}\n`);
}
process.stdout.write(`\n${MUTANTS.length - survivors - unapplied}/${MUTANTS.length} killed, ${survivors} survived, ${unapplied} not applied\n`);
process.exit(survivors + unapplied === 0 ? 0 : 1);
