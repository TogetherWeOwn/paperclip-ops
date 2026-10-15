#!/usr/bin/env node
// Disposition-repair census and Stop-guard replay over a bundle from collect-bundle.mjs.
//
//   node census.mjs --bundle <dir> [--hours 24] [--now <iso>] [--resume-live] [--json]
//
// One method for the before and after of case_f15b04c6: the same script, run on a bundle collected
// before the hook is installed and again 24 h after. It prints:
//   1. runs by wake reason, and the share that are `issue_disposition_repair`;
//   2. what ran just before each repair wake, and the card state at the repair wake;
//   3. the Stop guard's classifier replayed over every finished run in the window:
//      how many repair-preceding runs it would have caught, and how many other runs it
//      would have handed back (the wrong hand-back rate that decides observe or enforce).
//
// The replay classifies reconstructed card state (reconstruct.mjs), not the live card, so it is
// an estimate. Card state at repair time is inferred from activity events, +-5%.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { cardStateClass, replayRun, stateAt } from "./reconstruct.mjs";

export const REPAIR = "issue_disposition_repair";
/** Other platform wakes that exist to rescue a card a run left without a path. */
export const RECOVERY_WAKES = new Set([
  REPAIR,
  "issue_continuation_needed",
  "issue_assignment_recovery",
  "idle_assignee_executable_card",
  "issue_review_path_lost",
]);
/** A repair wake follows the run that caused it by a median of 39 s; 30 min covers the 95th percentile. */
/** Events the platform writes when it gives up on a card a run left without a path. */
export const ESCALATIONS = new Set(["issue.disposition_repair_escalated", "issue.successful_run_handoff_required"]);
export const REPAIR_FOLLOW_MS = 30 * 60 * 1000;
export const RECOVERED_LATER_MS = 60 * 60 * 1000;

const bump = (map, key, by = 1) => {
  map[key] = (map[key] ?? 0) + by;
};
const sortCounts = (map) => Object.fromEntries(Object.entries(map).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])));
const pct = (n, d) => (d ? Math.round((n / d) * 1000) / 10 : 0);

export function loadBundle(dir) {
  const read = (path) => JSON.parse(readFileSync(path, "utf8"));
  const runs = [];
  for (const name of readdirSync(join(dir, "runs"))) runs.push(...read(join(dir, "runs", name)));
  const issues = new Map();
  for (const name of readdirSync(join(dir, "issues"))) {
    const { issue, events } = read(join(dir, "issues", name));
    issues.set(name.replace(/\.json$/, ""), { issue, events });
  }
  let meta = {};
  try {
    meta = read(join(dir, "meta.json"));
  } catch {
    /* a hand-built bundle has no meta */
  }
  return { runs, issues, meta };
}

export function census(
  { runs, issues, meta = {} },
  { hours = 24, now = meta.collectedAt ?? new Date().toISOString(), resumeIsLivePath = false } = {},
) {
  const toMs = Date.parse(now);
  const fromMs = toMs - hours * 3600 * 1000;
  const inWindow = (run) => {
    const created = Date.parse(run.createdAt);
    return created >= fromMs && created <= toMs;
  };

  // Runs per card, oldest first, so "the run before this one" is a lookup.
  const byIssue = new Map();
  for (const run of runs) {
    if (!run.issueId) continue;
    if (!byIssue.has(run.issueId)) byIssue.set(run.issueId, []);
    byIssue.get(run.issueId).push(run);
  }
  for (const list of byIssue.values()) list.sort((a, b) => a.createdAt.localeCompare(b.createdAt));

  const windowRuns = runs.filter(inWindow);
  const byWake = {};
  const byStatus = {};
  for (const run of windowRuns) {
    bump(byWake, run.wakeReason ?? "(none)");
    bump(byStatus, run.status);
  }

  // 1. Repair wakes and what preceded them.
  const repairRuns = windowRuns.filter((run) => run.wakeReason === REPAIR);
  const repairCards = new Set(repairRuns.map((run) => run.issueId));
  const precedingWake = {};
  const stateAtRepair = {};
  const precedingOf = new Map(); // repair run id -> the run whose ending caused it
  for (const repair of repairRuns) {
    const list = byIssue.get(repair.issueId) ?? [];
    const repairAt = Date.parse(repair.createdAt);
    let preceding = null;
    for (const candidate of list) {
      if (candidate.id === repair.id) break;
      if (candidate.agentId === repair.agentId && candidate.finishedAt && Date.parse(candidate.finishedAt) <= repairAt + 1000) {
        preceding = candidate;
      }
    }
    if (preceding && repairAt - Date.parse(preceding.finishedAt) <= REPAIR_FOLLOW_MS) {
      precedingOf.set(repair.id, preceding);
      bump(precedingWake, preceding.wakeReason ?? "(none)");
    } else {
      bump(precedingWake, "(no run within 30 min)");
    }
    const card = issues.get(repair.issueId);
    bump(stateAtRepair, card ? cardStateClass(stateAt(card.events, repairAt)) : "unknown");
  }

  // 2. Replay the guard over every finished run it could have acted on.
  const causes = new Set([...precedingOf.values()].map((run) => run.id));
  const replay = {
    guardedRuns: 0,
    excluded: { notSucceeded: 0, noCard: 0 },
    repairPreceding: { runs: 0, caught: 0, missed: 0, caughtByKind: {}, missedBy: {} },
    others: { runs: 0, flagged: 0, flaggedByKind: {}, flaggedRecoveredLater: 0, flaggedEscalated: 0, flaggedUnexplained: 0, flaggedByWake: {} },
    verdictByWhy: {},
  };
  // How each end-of-run card signature fared: of the runs that ended like this, how many were
  // followed by a repair wake. This is what says which signals are live paths in practice.
  const signatures = {};
  const agentNames = new Map((meta.agents ?? []).map((agent) => [agent.id, agent.name]));
  const byAgent = {};
  const flaggedSamples = [];
  for (const run of windowRuns) {
    if (!run.issueId) continue;
    if (run.status !== "succeeded" || !run.finishedAt) {
      replay.excluded.notSucceeded += 1;
      continue;
    }
    const card = issues.get(run.issueId);
    if (!card) {
      replay.excluded.noCard += 1;
      continue;
    }
    replay.guardedRuns += 1;
    const agentRow = (byAgent[agentNames.get(run.agentId) ?? run.agentId] ??= { guarded: 0, blocks: 0, repairPrecedingCaught: 0, otherFlagged: 0 });
    agentRow.guarded += 1;
    const verdict = replayRun(run, card.issue, card.events, { resumeIsLivePath });
    bump(replay.verdictByWhy, verdict.action === "block" ? `BLOCK ${verdict.kind}` : verdict.why);
    const row = (signatures[verdict.signature] ??= { runs: 0, repaired: 0 });
    row.runs += 1;
    if (causes.has(run.id)) row.repaired += 1;
    if (verdict.action === "block") agentRow.blocks += 1;
    if (causes.has(run.id)) {
      replay.repairPreceding.runs += 1;
      if (verdict.action === "block") {
        agentRow.repairPrecedingCaught += 1;
        replay.repairPreceding.caught += 1;
        bump(replay.repairPreceding.caughtByKind, verdict.kind);
      } else {
        replay.repairPreceding.missed += 1;
        bump(replay.repairPreceding.missedBy, verdict.why);
      }
      continue;
    }
    replay.others.runs += 1;
    if (verdict.action !== "block") continue;
    replay.others.flagged += 1;
    agentRow.otherFlagged += 1;
    bump(replay.others.flaggedByKind, verdict.kind);
    bump(replay.others.flaggedByWake, run.wakeReason ?? "(none)");
    const finishedAt = Date.parse(run.finishedAt);
    const later = (byIssue.get(run.issueId) ?? []).find(
      (next) =>
        next.id !== run.id &&
        Date.parse(next.createdAt) >= finishedAt - 1000 &&
        Date.parse(next.createdAt) - finishedAt <= RECOVERED_LATER_MS &&
        RECOVERY_WAKES.has(next.wakeReason),
    );
    // The platform's own record that the card was left in trouble: an escalation to the board.
    const escalated = card.events.some(
      (event) =>
        ESCALATIONS.has(event.action) &&
        Date.parse(event.createdAt) >= finishedAt - 1000 &&
        Date.parse(event.createdAt) - finishedAt <= RECOVERED_LATER_MS,
    );
    if (later) replay.others.flaggedRecoveredLater += 1;
    else if (escalated) replay.others.flaggedEscalated += 1;
    else {
      replay.others.flaggedUnexplained += 1;
      if (flaggedSamples.length < 40) flaggedSamples.push({ run: run.id, issue: card.issue?.identifier ?? run.issueId, wake: run.wakeReason, status: verdict.state.status, kind: verdict.kind });
    }
  }
  const strictWrong = replay.others.flagged;
  const refinedWrong = replay.others.flaggedUnexplained;
  const guarded = replay.guardedRuns;

  return {
    window: { from: new Date(fromMs).toISOString(), to: new Date(toMs).toISOString(), hours },
    coverage: {
      agents: (meta.agents ?? []).length,
      truncatedAgents: (meta.agents ?? []).filter((agent) => agent.truncated).map((agent) => ({ name: agent.name, oldest: agent.oldest })),
    },
    runs: {
      total: windowRuns.length,
      byWakeReason: sortCounts(byWake),
      byStatus: sortCounts(byStatus),
      repairRuns: repairRuns.length,
      repairShareOfRunsPct: pct(repairRuns.length, windowRuns.length),
      repairCards: repairCards.size,
    },
    repair: { precedingWake: sortCounts(precedingWake), cardStateAtRepairWake: sortCounts(stateAtRepair) },
    replay: {
      ...replay,
      verdictByWhy: sortCounts(replay.verdictByWhy),
      repairPreceding: {
        ...replay.repairPreceding,
        caughtPct: pct(replay.repairPreceding.caught, replay.repairPreceding.runs),
        caughtByKind: sortCounts(replay.repairPreceding.caughtByKind),
        missedBy: sortCounts(replay.repairPreceding.missedBy),
      },
      others: {
        ...replay.others,
        flaggedByKind: sortCounts(replay.others.flaggedByKind),
        flaggedByWake: sortCounts(replay.others.flaggedByWake),
      },
      wrongHandbackPctOfGuardedRuns: { strict: pct(strictWrong, guarded), refined: pct(refinedWrong, guarded) },
      resumeIsLivePath,
      byAgent: Object.entries(byAgent)
        .map(([agent, row]) => ({ agent, ...row }))
        .sort((a, b) => b.blocks - a.blocks || a.agent.localeCompare(b.agent)),
      repairRateBySignature: Object.entries(signatures)
        .map(([signature, row]) => ({ signature, runs: row.runs, repaired: row.repaired, repairedPct: pct(row.repaired, row.runs) }))
        .sort((a, b) => b.repaired - a.repaired || b.runs - a.runs),
      flaggedUnexplainedSamples: flaggedSamples,
    },
  };
}

export function renderText(result) {
  const lines = [];
  const add = (text = "") => lines.push(text);
  const table = (title, map) => {
    add(title);
    for (const [key, value] of Object.entries(map)) add(`  ${String(value).padStart(6)}  ${key}`);
  };
  add(`window ${result.window.from} .. ${result.window.to} (${result.window.hours} h)`);
  if (result.coverage.truncatedAgents.length > 0) {
    add(`truncated agent pages (window short for these): ${result.coverage.truncatedAgents.map((a) => `${a.name} from ${a.oldest}`).join("; ")}`);
  }
  add(`runs ${result.runs.total}; ${REPAIR} ${result.runs.repairRuns} (${result.runs.repairShareOfRunsPct}%) over ${result.runs.repairCards} cards`);
  add();
  table("runs by wake reason", result.runs.byWakeReason);
  add();
  table("run before each repair wake", result.repair.precedingWake);
  add();
  table("card state at the repair wake", result.repair.cardStateAtRepairWake);
  add();
  const r = result.replay;
  add(`replay over ${r.guardedRuns} finished runs on a card (excluded: ${r.excluded.notSucceeded} not succeeded, ${r.excluded.noCard} no card)`);
  add(`  repair-preceding runs ${r.repairPreceding.runs}: caught ${r.repairPreceding.caught} (${r.repairPreceding.caughtPct}%), missed ${r.repairPreceding.missed}`);
  for (const [why, n] of Object.entries(r.repairPreceding.missedBy)) add(`      missed: ${String(n).padStart(5)}  ${why}`);
  add(
    `  other runs ${r.others.runs}: flagged ${r.others.flagged}; then another platform recovery wake within 60 min ${r.others.flaggedRecoveredLater}; ` +
      `then a platform escalation ${r.others.flaggedEscalated}; nothing followed ${r.others.flaggedUnexplained}`,
  );
  add(`  wrong hand-back rate, % of guarded runs: strict ${r.wrongHandbackPctOfGuardedRuns.strict}, refined ${r.wrongHandbackPctOfGuardedRuns.refined}`);
  add();
  table("guard verdict, all guarded runs", r.verdictByWhy);
  add();
  add("guard verdicts by agent: guarded runs, hand-backs, of which caught a repair-preceding run, of which on a run no repair followed");
  for (const row of r.byAgent.filter((entry) => entry.blocks > 0)) {
    add(`  ${String(row.guarded).padStart(5)}  ${String(row.blocks).padStart(4)}  ${String(row.repairPrecedingCaught).padStart(4)}  ${String(row.otherFlagged).padStart(4)}  ${row.agent}`);
  }
  add();
  add(`repair rate by end-of-run card signature (resume counted as a live path: ${r.resumeIsLivePath})`);
  add("  runs  repaired  repaired%  signature");
  for (const row of r.repairRateBySignature.slice(0, 30)) {
    add(`  ${String(row.runs).padStart(4)}  ${String(row.repaired).padStart(8)}  ${String(row.repairedPct).padStart(8)}%  ${row.signature}`);
  }
  return lines.join("\n");
}

function parseArgs(argv) {
  const flags = { hours: 24 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--json") flags.json = true;
    else if (argv[i] === "--resume-live") flags.resumeLive = true;
    else if (argv[i].startsWith("--")) {
      flags[argv[i].slice(2)] = argv[i + 1];
      i += 1;
    }
  }
  flags.hours = Number(flags.hours);
  return flags;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const flags = parseArgs(process.argv.slice(2));
  if (!flags.bundle) {
    process.stderr.write("usage: census.mjs --bundle <dir> [--hours 24] [--now <iso>] [--resume-live] [--json]\n");
    process.exit(2);
  }
  const result = census(loadBundle(flags.bundle), {
    hours: flags.hours,
    resumeIsLivePath: flags.resumeLive === true,
    ...(flags.now ? { now: flags.now } : {}),
  });
  process.stdout.write(flags.json ? `${JSON.stringify(result, null, 2)}\n` : `${renderText(result)}\n`);
}
