#!/usr/bin/env node
// Summarise the guard's decision log: how often it handed a run back, why it let one go, and how
// long the card read took.
//   node summarize-decisions.mjs [decisions.jsonl] [--since 2026-10-10T00:00:00Z]
// The log holds reasons and lengths only, never model text or card text.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const sinceIndex = args.indexOf("--since");
const since = sinceIndex >= 0 ? Date.parse(args.splice(sinceIndex, 2)[1]) : 0;
const file = args[0] ?? join(HERE, "decisions.jsonl");
if (!existsSync(file)) {
  process.stderr.write(`no decision log at ${file}\n`);
  process.exit(1);
}
const rows = readFileSync(file, "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => {
    try {
      return JSON.parse(line);
    } catch {
      return null;
    }
  })
  .filter((row) => row && Date.parse(row.ts) >= since);

const count = (items, key) =>
  Object.fromEntries(
    Object.entries(items.reduce((acc, row) => ({ ...acc, [row[key] ?? "null"]: (acc[row[key] ?? "null"] ?? 0) + 1 }), {})).sort((a, b) => b[1] - a[1]),
  );
const blocks = rows.filter((row) => row.action === "block");
const wouldBlock = rows.filter((row) => row.wouldBlock);
const runs = new Set(rows.map((row) => row.run ?? row.session));
const blockedRuns = new Set(blocks.map((row) => row.run ?? row.session));
const perRun = {};
for (const row of blocks) perRun[row.run ?? row.session] = (perRun[row.run ?? row.session] ?? 0) + 1;
const histogram = {};
for (const n of Object.values(perRun)) histogram[n] = (histogram[n] ?? 0) + 1;
const ms = rows.map((row) => row.ms).filter(Number.isFinite).sort((a, b) => a - b);
const quantile = (q) => (ms.length ? ms[Math.min(ms.length - 1, Math.floor(ms.length * q))] : null);

process.stdout.write(
  `${JSON.stringify(
    {
      from: rows[0]?.ts ?? null,
      to: rows.at(-1)?.ts ?? null,
      stopsSeen: rows.length,
      runsSeen: runs.size,
      blocks: blocks.length,
      runsHandedBack: blockedRuns.size,
      handbacksPerHandedBackRun: histogram,
      blockedBy: count(blocks, "kind"),
      blocksByAgent: count(blocks, "agent"),
      wouldBlockInObserve: wouldBlock.length,
      wouldBlockByKind: count(wouldBlock, "kind"),
      allowedBecause: count(rows.filter((row) => row.action === "allow" && !row.wouldBlock), "why"),
      capReached: rows.filter((row) => row.why === "handback-cap-reached").length,
      failedOpen: rows.filter((row) => ["card-unreadable", "interactions-unreadable", "error", "counter-unwritable"].includes(row.why)).length,
      msP50: quantile(0.5),
      msP95: quantile(0.95),
      msMax: ms.at(-1) ?? null,
    },
    null,
    2,
  )}\n`,
);
