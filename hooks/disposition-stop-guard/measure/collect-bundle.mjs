#!/usr/bin/env node
// Collect the input bundle for census.mjs: per-agent heartbeat runs plus the
// activity of every card those runs touched. Read-only against the Paperclip API.
//
//   PAPERCLIP_API_URL=... PAPERCLIP_API_KEY=... PAPERCLIP_COMPANY_ID=... \
//     node collect-bundle.mjs --out <dir> [--hours 24] [--concurrency 3] [--resume true]
//
// It only reads. Keep the concurrency low: on a loaded control plane a single card read takes
// seconds, and a census must not make that worse. --resume skips a card fetched after its newest
// run was recorded, so an interrupted collection continues instead of restarting.
//
// WHY A PER-AGENT BUNDLE. The company-wide `heartbeat-runs?limit=1000` page covers about
// four hours. Each agent's own page of 1000 rows covers a day for all but the busiest agents,
// so the bundle asks every agent for its own page and records which agents were truncated.
// The window is therefore exact for most agents and short for a few; census.mjs prints that.
//
// The bundle is slim on purpose: run rows keep id, agent, times, status, wake reason and
// card; card activity keeps only the events that decide a card's disposition. No comment
// text, no tool-gateway events, no model text.

import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const KEEP_EVENTS = new Set([
  "issue.created",
  "issue.updated",
  "issue.checked_out",
  "issue.monitor_scheduled",
  "issue.monitor_triggered",
  "issue.monitor_cleared",
  "issue.blockers_updated",
  "issue.blockers_resolved_wake_emitted",
  "issue.comment_added",
  "issue.disposition_repair_escalated",
  "issue.successful_run_handoff_required",
]);
const INTERACTION_EVENT = /^issue\.thread_interaction_(created|accepted|answered|rejected|expired|withdrawn)$/;

const pick = (source, keys) => {
  const out = {};
  for (const key of keys) if (source && key in source) out[key] = source[key];
  return out;
};

const slimMonitor = (monitor) =>
  monitor && typeof monitor === "object"
    ? pick(monitor, ["status", "nextCheckAt", "attemptCount", "maxAttempts"])
    : monitor ?? null;

export function slimRun(row) {
  const context = row.contextSnapshot ?? {};
  return {
    id: row.id,
    agentId: row.agentId,
    createdAt: row.createdAt,
    startedAt: row.startedAt ?? null,
    finishedAt: row.finishedAt ?? null,
    status: row.status,
    invocationSource: row.invocationSource ?? null,
    wakeReason: context.wakeReason ?? null,
    issueId: context.issueId ?? null,
    retryOfRunId: row.retryOfRunId ?? null,
    errorCode: row.errorCode ?? null,
  };
}

export function slimEvent(event) {
  const action = event.action;
  if (!KEEP_EVENTS.has(action) && !INTERACTION_EVENT.test(action)) return null;
  const d = event.details && typeof event.details === "object" ? event.details : {};
  let details = {};
  if (action === "issue.created") details = pick(d, ["status"]);
  else if (action === "issue.updated") {
    details = {
      ...pick(d, ["status", "monitorNextCheckAt", "resumeIntent", "source", "recoveryCause"]),
      changesStatusTo: d.changes?.status?.to ?? null,
      executionStatePresent: "executionState" in d,
      monitor: "executionState" in d ? slimMonitor(d.executionState?.monitor) : undefined,
    };
  } else if (action === "issue.monitor_scheduled") details = pick(d, ["nextCheckAt", "maxAttempts"]);
  else if (action === "issue.monitor_triggered") details = pick(d, ["nextCheckAt", "attemptCount", "maxAttempts"]);
  else if (action === "issue.monitor_cleared") details = pick(d, ["reason"]);
  else if (action === "issue.blockers_updated") details = pick(d, ["blockedByIssueIds"]);
  else if (action === "issue.comment_added") details = pick(d, ["resumeIntent"]);
  else if (action === "issue.disposition_repair_escalated") details = pick(d, ["terminalReason", "attemptCount", "maxAttempts"]);
  else if (action === "issue.successful_run_handoff_required") details = pick(d, ["handoffReason"]);
  else if (INTERACTION_EVENT.test(action)) details = pick(d, ["interactionId"]);
  return {
    action,
    createdAt: event.createdAt,
    runId: event.runId ?? null,
    actorType: event.actorType ?? null,
    details,
  };
}

export function slimIssue(issue) {
  return {
    ...slimIssueFields(issue),
    hasExternalChannelBinding: Boolean(issue?.externalChannelBinding),
  };
}

function slimIssueFields(issue) {
  return pick(issue, [
    "id",
    "identifier",
    "status",
    "assigneeAgentId",
    "conversationAgentId",
    "originKind",
    "workMode",
    "createdAt",
    "parentId",
    "projectId",
  ]);
}

function parseArgs(argv) {
  const flags = { hours: 24, concurrency: 3 };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, "");
    if (!key) continue;
    flags[key] = argv[i + 1];
  }
  flags.hours = Number(flags.hours);
  flags.concurrency = Number(flags.concurrency);
  return flags;
}

async function api(base, key, path, { retries = 3 } = {}) {
  let lastError;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    try {
      const response = await fetch(`${base}${path}`, {
        headers: { authorization: `Bearer ${key}` },
        signal: AbortSignal.timeout(120_000),
      });
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
    }
  }
  throw lastError;
}

async function pool(items, size, work) {
  let next = 0;
  const workers = Array.from({ length: Math.min(size, items.length) }, async () => {
    while (next < items.length) {
      const item = items[next];
      next += 1;
      await work(item);
    }
  });
  await Promise.all(workers);
}

async function main() {
  const flags = parseArgs(process.argv.slice(2));
  const base = (process.env.PAPERCLIP_API_URL ?? "").replace(/\/+$/, "").replace(/\/api$/, "");
  const key = process.env.PAPERCLIP_API_KEY;
  const company = process.env.PAPERCLIP_COMPANY_ID;
  if (!flags.out || !base || !key || !company) {
    process.stderr.write("need --out and PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID\n");
    process.exit(2);
  }
  mkdirSync(join(flags.out, "runs"), { recursive: true });
  mkdirSync(join(flags.out, "issues"), { recursive: true });

  const latestRun = new Map(); // card -> newest run timestamp seen, for --resume
  const repaired = new Set();
  const noteRun = (run) => {
    if (!run.issueId) return;
    if (run.wakeReason === "issue_disposition_repair") repaired.add(run.issueId);
    const at = Math.max(Date.parse(run.createdAt), run.finishedAt ? Date.parse(run.finishedAt) : 0);
    latestRun.set(run.issueId, Math.max(latestRun.get(run.issueId) ?? 0, at));
  };
  const issueIds = new Set();
  let meta;
  if (flags.resume === "true" && existsSync(join(flags.out, "meta.json"))) {
    meta = JSON.parse(readFileSync(join(flags.out, "meta.json"), "utf8"));
    for (const name of readdirSync(join(flags.out, "runs"))) {
      for (const run of JSON.parse(readFileSync(join(flags.out, "runs", name), "utf8"))) {
        noteRun(run);
        if (run.issueId) issueIds.add(run.issueId);
      }
    }
  } else {
    const collectedAt = new Date();
    // Two extra hours so the run before the first repair wake in the window is present.
    const since = collectedAt.getTime() - (flags.hours + 2) * 3600 * 1000;
    const agents = await api(base, key, `/api/companies/${company}/agents`);
    meta = { collectedAt: collectedAt.toISOString(), hours: flags.hours, agents: [] };
    await pool(agents, flags.concurrency, async (agent) => {
      // Ask for a small page first and widen only while the page still reaches back into the
      // window: a busy agent's 1000 rows can be hundreds of megabytes.
      let rows = [];
      let limit = 250;
      for (;;) {
        rows = (await api(base, key, `/api/companies/${company}/heartbeat-runs?agentId=${agent.id}&limit=${limit}`)) ?? [];
        const oldest = rows.length ? Date.parse(rows[rows.length - 1].createdAt) : Infinity;
        if (rows.length < limit || oldest < since || limit >= 1000) break;
        limit = Math.min(1000, limit * 2);
      }
      const oldest = rows.length ? Date.parse(rows[rows.length - 1].createdAt) : null;
      const slim = rows.filter((row) => Date.parse(row.createdAt) >= since).map(slimRun);
      writeFileSync(join(flags.out, "runs", `${agent.id}.json`), `${JSON.stringify(slim)}\n`);
      for (const run of slim) {
        noteRun(run);
        if (run.issueId) issueIds.add(run.issueId);
      }
      meta.agents.push({
        id: agent.id,
        name: agent.name,
        status: agent.status,
        rows: rows.length,
        kept: slim.length,
        oldest: oldest ? new Date(oldest).toISOString() : null,
        truncated: rows.length >= limit && oldest !== null && oldest > since,
      });
    });
    meta.agents.sort((a, b) => a.name.localeCompare(b.name));
    meta.issues = issueIds.size;
    writeFileSync(join(flags.out, "meta.json"), `${JSON.stringify(meta, null, 2)}\n`);
  }

  const fresh = (id) => {
    const file = join(flags.out, "issues", `${id}.json`);
    return flags.resume === "true" && existsSync(file) && statSync(file).mtimeMs > (latestRun.get(id) ?? 0) + 60_000;
  };
  // Cards that were repaired first, so an interrupted collection still answers the repair census.
  const todo = [...issueIds].filter((id) => !fresh(id)).sort((a, b) => Number(repaired.has(b)) - Number(repaired.has(a)));
  let done = 0;
  await pool(todo, flags.concurrency, async (issueId) => {
    const [issue, activity] = await Promise.all([
      api(base, key, `/api/issues/${issueId}`),
      api(base, key, `/api/issues/${issueId}/activity`),
    ]);
    const events = (activity ?? []).map(slimEvent).filter(Boolean).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    writeFileSync(join(flags.out, "issues", `${issueId}.json`), `${JSON.stringify({ issue: slimIssue(issue ?? {}), events })}\n`);
    done += 1;
    if (done % 200 === 0) process.stderr.write(`issues ${done}/${todo.length}\n`);
  });

  process.stderr.write(`bundle written: ${flags.out} (${meta.agents.length} agents, ${issueIds.size} cards)\n`);
}

import { fileURLToPath } from "node:url";
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  });
}
