#!/usr/bin/env node
/*
 * shadow-read-bounded.cjs — case_e6a23bc2 bounded shadow-reader for the
 * cliproxy-meta lane-withdrawn evidence (prerequisite for case_f8d3db0f,
 * which blocks case_5a395882).
 *
 * Why this exists: reader request req-20261008T015703Z-2957254 ended
 * REFUSED_BOUND_LIMIT — the previous reader scanned all 48 retained hourly
 * shards (236,446,446 bytes) against the approved 134,217,728-byte
 * (128 MiB) aggregate cap. Zero records read is NOT zero matches. This
 * reader stays inside the cap by construction:
 *
 *   1. Binding re-verify first: the shadow-decisions directory's realpath
 *      must equal the expected registered path. Refuse otherwise.
 *   2. Newest-first byte-budget selection: candidate shards (hour window +
 *      filename pattern) are stat'ed BEFORE reading; shards are included
 *      newest-first only while the running total stays within the cap.
 *      A single newest shard over budget refuses (fail-closed, no partial
 *      read); over-budget older shards are listed as excluded, never read.
 *   3. Streaming scan: line-by-line, bounded match retention
 *      (--max-matches, default 50; totals still exact), bounded line
 *      length, per-shard open-once + fstat of the open handle against
 *      the budgeted size (TOCTOU guard: the streamed inode is the
 *      budgeted inode, so the atomic-replace writer cannot swap the
 *      file between the size check and the read).
 *
 * Preserved checks: retention (never deletes/trims/rotates shards),
 * schema (only schema paired-decision-v1 records can match; other schemas
 * are counted, never parsed as matches), freshness (newest record ts and
 * age are reported; record timestamps ARE examined).
 *
 * Done-test predicate (from the card): a paired-decision-v1 /
 * plugin-shadow / T1 record with explanations[].gate = lane-withdrawn and
 * operand.laneId = cliproxy-meta. Outcome MATCHES_FOUND vs NO_MATCHES is
 * only published with recordsScanned > 0, so a negative is proven work,
 * not an empty read. Zero in-schema records (other-schema-only,
 * schema-less, or scalar shards) refuse with REFUSED_NO_SCANNED_RECORDS
 * (exit 2); only in-schema paired-decision-v1 records count as scanned.
 *
 * Sanitized receipt only: match descriptors carry ts/tier/pickedModel plus
 * the gate operand numbers (utilization, ceiling, accounts). Account-level
 * detail (laneSnapshot.accounts and everything under it) is never copied
 * to the receipt. No /advise invocation, no manufactured quota, no config
 * change, no read outside the shadow-decisions binding.
 *
 * Usage:
 *   node shadow-read-bounded.cjs --dir DIR --expected-realpath PATH
 *     [--cap-bytes N] [--lane ID] [--ceiling F] [--since HOUR]
 *     [--until HOUR] [--max-matches N] [--receipt FILE] [--now ISO]
 *   HOUR form: 2026-10-08-08 (UTC hour, matches the shard filename stamp).
 *
 * Exit codes: 0 scan complete (read outcome in the receipt); 2 refused
 * (binding/scope/budget/IO — receipt explains, nothing claimed); 1 usage.
 *
 * Stdlib only. No network, no credentials, no writes except the receipt.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");

const TOOL = "case_a38f4d21-shadow-read-bounded-v1";
const DEFAULT_CAP_BYTES = 134217728; // 128 MiB — the approved aggregate bound.
const DEFAULT_LANE = "cliproxy-meta";
const DEFAULT_MAX_MATCHES = 50;
const MAX_LINE_BYTES = 4 * 1024 * 1024; // live records are ~16 KB; bound, not trust.
const MAX_SCHEMA_OTHER_KEYS = 20;
const SHARD_PATTERN = /^decisions-(\d{4}-\d{2}-\d{2}-\d{2})Z\.jsonl$/;
const EXPECTED_SCHEMA = "paired-decision-v1";
const EXPECTED_WRITER = "plugin-shadow";
const EXPECTED_TIER = "T1";
const EXPECTED_GATE = "lane-withdrawn";

function usageError(msg) {
  process.stderr.write(`usage error: ${msg}\n`);
  process.exit(1);
}

function parseArgs(argv) {
  const o = {
    dir: null,
    expectedRealpath: null,
    capBytes: DEFAULT_CAP_BYTES,
    lane: DEFAULT_LANE,
    ceiling: 0.98,
    since: null,
    until: null,
    maxMatches: DEFAULT_MAX_MATCHES,
    receipt: null,
    now: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const take = (name) => {
      const v = argv[++i];
      if (v === undefined) usageError(`${name} needs a value`);
      return v;
    };
    if (a === "--dir") o.dir = take(a);
    else if (a === "--expected-realpath") o.expectedRealpath = take(a);
    else if (a === "--cap-bytes") o.capBytes = Number(take(a));
    else if (a === "--lane") o.lane = take(a);
    else if (a === "--ceiling") o.ceiling = Number(take(a));
    else if (a === "--since") o.since = take(a);
    else if (a === "--until") o.until = take(a);
    else if (a === "--max-matches") o.maxMatches = Number(take(a));
    else if (a === "--receipt") o.receipt = take(a);
    else if (a === "--now") o.now = take(a);
    else usageError(`unknown flag ${a}`);
  }
  if (!o.dir) usageError("--dir is required");
  if (!o.expectedRealpath) usageError("--expected-realpath is required");
  if (!Number.isInteger(o.capBytes) || o.capBytes <= 0) usageError("--cap-bytes must be a positive integer");
  if (!Number.isInteger(o.maxMatches) || o.maxMatches <= 0) usageError("--max-matches must be a positive integer");
  if (!Number.isFinite(o.ceiling) || o.ceiling <= 0 || o.ceiling > 1) usageError("--ceiling must be in (0, 1]");
  for (const h of [o.since, o.until]) {
    if (h !== null && !/^\d{4}-\d{2}-\d{2}-\d{2}$/.test(h)) usageError("--since/--until hour form is YYYY-MM-DD-HH");
  }
  return o;
}

function refuse(code, detail, extra) {
  const receipt = {
    tool: TOOL,
    outcome: "REFUSED_" + code,
    detail,
    ...(extra || {}),
  };
  return { refused: true, receipt };
}

function isMatch(rec, lane) {
  if (!rec || typeof rec !== "object") return null;
  if (rec.schema !== EXPECTED_SCHEMA) return null;
  if (rec.writer !== EXPECTED_WRITER) return null;
  if (rec.tier !== EXPECTED_TIER) return null;
  const ex = rec.explanations;
  if (!Array.isArray(ex)) return null;
  for (const e of ex) {
    if (e && typeof e === "object" && e.gate === EXPECTED_GATE && e.operand && typeof e.operand === "object") {
      if (e.operand.laneId === lane) {
        return {
          ts: typeof rec.ts === "string" ? rec.ts : null,
          tier: rec.tier,
          pickedModel: typeof rec.pickedModel === "string" ? rec.pickedModel : rec.pickedModel ?? null,
          gate: e.gate,
          laneId: e.operand.laneId,
          utilization: typeof e.operand.utilization === "number" ? e.operand.utilization : null,
          ceiling: typeof e.operand.ceiling === "number" ? e.operand.ceiling : null,
          accounts: typeof e.operand.accounts === "number" ? e.operand.accounts : null,
        };
      }
    }
  }
  return null;
}

// Numbers/state only — never the accounts array (keys, health, weights).
function metaContext(rec, lane) {
  try {
    const snap = rec && rec.laneSnapshot && rec.laneSnapshot.lanes && rec.laneSnapshot.lanes[lane];
    if (!snap || typeof snap !== "object") return null;
    const pick = (v) => (typeof v === "number" ? v : null);
    return {
      weekly: pick(snap.weekly),
      fiveHour: pick(snap.fiveHour),
      state: typeof snap.state === "string" ? snap.state : null,
      atRecordTs: typeof rec.ts === "string" ? rec.ts : null,
    };
  } catch {
    return null;
  }
}

async function main() {
  const o = parseArgs(process.argv.slice(2));
  const nowMs = o.now ? Date.parse(o.now) : Date.now();
  if (!Number.isFinite(nowMs)) usageError("--now is not a parseable timestamp");

  const base = {
    tool: TOOL,
    lane: o.lane,
    ceiling: o.ceiling,
    boundBytes: o.capBytes,
    window: { since: o.since, until: o.until },
  };

  // 1. Binding re-verify first: realpath equality, directory, not a symlink.
  let dirReal;
  try {
    dirReal = fs.realpathSync(o.dir);
  } catch (err) {
    const r = refuse("BINDING", `shadow-decisions dir not resolvable: ${String(err.message || err)}`, base);
    return finish(r.receipt, o, 2);
  }
  let expectedReal;
  try {
    expectedReal = fs.realpathSync(o.expectedRealpath);
  } catch (err) {
    const r = refuse("BINDING", `expected binding path not resolvable: ${String(err.message || err)}`, base);
    return finish(r.receipt, o, 2);
  }
  if (dirReal !== expectedReal) {
    const r = refuse("BINDING", "dir realpath does not equal the registered shadow-decisions binding", {
      ...base, dirRealpath: dirReal,
    });
    return finish(r.receipt, o, 2);
  }
  let st;
  try {
    st = fs.statSync(dirReal);
  } catch (err) {
    const r = refuse("BINDING", `binding stat failed: ${String(err.message || err)}`, { ...base, dirRealpath: dirReal });
    return finish(r.receipt, o, 2);
  }
  if (!st.isDirectory()) {
    const r = refuse("BINDING", "binding path is not a directory", { ...base, dirRealpath: dirReal });
    return finish(r.receipt, o, 2);
  }
  let lst;
  try {
    lst = fs.lstatSync(o.dir);
  } catch (err) {
    const r = refuse("BINDING", `dir lstat failed: ${String(err.message || err)}`, { ...base, dirRealpath: dirReal });
    return finish(r.receipt, o, 2);
  }
  if (lst.isSymbolicLink()) {
    const r = refuse("BINDING", "dir itself is a symlink; following it is not allowed", { ...base, dirRealpath: dirReal });
    return finish(r.receipt, o, 2);
  }

  // 2. Candidate shards: filename pattern + hour window, regular files only.
  let entries;
  try {
    entries = fs.readdirSync(dirReal, { withFileTypes: true });
  } catch (err) {
    const r = refuse("LIST", `shard listing failed: ${String(err.message || err)}`, { ...base, dirRealpath: dirReal });
    return finish(r.receipt, o, 2);
  }
  const candidates = [];
  let symlinksSkipped = 0;
  for (const e of entries) {
    const m = SHARD_PATTERN.exec(e.name);
    if (!m) continue;
    const hour = m[1];
    if (o.since !== null && hour < o.since) continue;
    if (o.until !== null && hour > o.until) continue;
    if (!e.isFile()) {
      // A shard-named non-file (symlink, dir, socket) is never followed.
      try {
        if (fs.lstatSync(path.join(dirReal, e.name)).isSymbolicLink()) symlinksSkipped++;
      } catch {
        // Vanished mid-listing; nothing to count.
      }
      continue;
    }
    const full = path.join(dirReal, e.name);
    let fst;
    try {
      fst = fs.statSync(full);
    } catch {
      continue; // vanished mid-listing; the read phase re-stats and refuses on error.
    }
    if (!fst.isFile()) continue;
    // Belt and braces: re-lstat to catch a symlink swap between readdir and stat.
    try {
      if (fs.lstatSync(full).isSymbolicLink()) {
        symlinksSkipped++;
        continue;
      }
    } catch {
      continue;
    }
    candidates.push({ name: e.name, hour, size: fst.size });
  }
  // Newest-first: lexical order is chronological order (zero-padded UTC hour).
  candidates.sort((a, b) => (a.hour < b.hour ? 1 : a.hour > b.hour ? -1 : 0));

  if (candidates.length === 0) {
    const r = refuse("NO_SHARDS", "no retained shards in scope; zero records read is not zero matches", {
      ...base, dirRealpath: dirReal, symlinksSkipped,
    });
    return finish(r.receipt, o, 2);
  }

  // 3. Byte-budget selection, newest-first, on stat sizes before reading.
  const included = [];
  const excluded = [];
  let aggregate = 0;
  for (const c of candidates) {
    if (aggregate + c.size <= o.capBytes) {
      included.push(c);
      aggregate += c.size;
    } else {
      excluded.push(c);
    }
  }
  if (included.length === 0) {
    const r = refuse("SINGLE_SHARD_OVER_BUDGET",
      `newest in-scope shard ${candidates[0].name} alone (${candidates[0].size} bytes) exceeds the ${o.capBytes}-byte aggregate bound; no partial read`,
      { ...base, dirRealpath: dirReal, newestShard: candidates[0], symlinksSkipped });
    return finish(r.receipt, o, 2);
  }
  // Chronological scan order (oldest included first).
  included.sort((a, b) => (a.hour < b.hour ? -1 : a.hour > b.hour ? 1 : 0));

  // 4. Streaming scan.
  let recordsScanned = 0;
  let malformedLines = 0;
  let overlongLines = 0;
  let nonObjectLines = 0;
  let schemaLessLines = 0;
  let schemaOther = {};
  let schemaOtherCollapsed = 0;
  let matchesTotal = 0;
  const matchesRetained = [];
  let minTs = null;
  let maxTs = null;
  let maxTsCtx = null;
  const shardsRead = [];
  for (const c of included) {
    const full = path.join(dirReal, c.name);
    // Open once, then fstat the open handle: the writer atomically
    // replaces the current-hour shard, so stat-by-path followed by a
    // second open-by-path could examine one inode and stream another
    // (spurious refusal, or a read past the byte budget). fstat pins the
    // budgeted inode for the whole streaming read below.
    let fd = null;
    let rst;
    try {
      fd = fs.openSync(full, "r");
      rst = fs.fstatSync(fd);
    } catch (err) {
      if (fd !== null) { try { fs.closeSync(fd); } catch { /* already gone */ } }
      const r = refuse("READ", `selected shard ${c.name} became unreadable mid-run: ${String(err.message || err)}; refusing rather than silently truncating history`, {
        ...base, dirRealpath: dirReal, shardsBudgeted: included.map((x) => x.name), aggregateBudgetedBytes: aggregate,
      });
      return finish(r.receipt, o, 2);
    }
    if (!rst.isFile() || rst.size !== c.size) {
      try { fs.closeSync(fd); } catch { /* already gone */ }
      const r = refuse("SHARD_CHANGED", `selected shard ${c.name} changed between budget and read (budgeted ${c.size}, now ${rst.size}); refusing rather than exceeding the bound or truncating`, {
        ...base, dirRealpath: dirReal, shardsBudgeted: included.map((x) => x.name), aggregateBudgetedBytes: aggregate,
      });
      return finish(r.receipt, o, 2);
    }
    let lines = 0;
    let parsed = 0;
    let malformed = 0;
    let overlong = 0;
    try {
      // Stream the already-open handle (path is ignored for opening when
      // fd is given); the stream owns the fd and closes it (autoClose).
      const stream = fs.createReadStream(full, { fd, encoding: "utf8", autoClose: true });
      const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
      for await (const line of rl) {
        if (line.length === 0) continue;
        lines++;
        if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
          overlong++;
          continue;
        }
        let rec;
        try {
          rec = JSON.parse(line);
        } catch {
          malformed++;
          continue;
        }
        parsed++;
        // Only plain-object records can carry a schema tag or the match
        // predicate. Valid-JSON scalars/arrays are counted, never scanned:
        // counting them would let a shard with no real records publish a
        // proven-looking negative.
        if (!rec || typeof rec !== "object" || Array.isArray(rec)) {
          nonObjectLines++;
          continue;
        }
        // Freshness examines every record object's timestamp, whatever
        // its schema; only schema paired-decision-v1 records can match.
        if (rec && typeof rec === "object" && typeof rec.ts === "string") {
          if (minTs === null || rec.ts < minTs) minTs = rec.ts;
          if (maxTs === null || rec.ts > maxTs) {
            maxTs = rec.ts;
            maxTsCtx = metaContext(rec, o.lane);
          }
        }
        if (rec && typeof rec === "object" && typeof rec.schema === "string" && rec.schema !== EXPECTED_SCHEMA) {
          if (Object.prototype.hasOwnProperty.call(schemaOther, rec.schema)) schemaOther[rec.schema]++;
          else if (Object.keys(schemaOther).length < MAX_SCHEMA_OTHER_KEYS) schemaOther[rec.schema] = 1;
          else schemaOtherCollapsed++;
          continue;
        }
        // Schema-less objects can never match (the predicate requires the
        // paired-decision-v1 tag). Counting them as scanned would let a
        // shard of `{}` lines publish a proven-looking negative over zero
        // evidence, so they are counted, never scanned.
        if (typeof rec.schema !== "string") {
          schemaLessLines++;
          continue;
        }
        recordsScanned++;
        const m = isMatch(rec, o.lane);
        if (m) {
          matchesTotal++;
          if (matchesRetained.length < o.maxMatches) matchesRetained.push({ shard: c.name, ...m });
        }
      }
    } catch (err) {
      const r = refuse("READ", `streaming shard ${c.name} failed: ${String(err.message || err)}; refusing rather than silently truncating history`, {
        ...base, dirRealpath: dirReal, shardsBudgeted: included.map((x) => x.name), aggregateBudgetedBytes: aggregate,
      });
      return finish(r.receipt, o, 2);
    }
    malformedLines += malformed;
    overlongLines += overlong;
    shardsRead.push({ name: c.name, hour: c.hour, bytes: c.size, lines, parsed });
  }

  // A negative is only proven work when at least one in-schema record was
  // examined. Other-schema-only, schema-less-only, or scalar-only shards
  // refuse instead of publishing a clean-looking NO_MATCHES over nothing
  // examined.
  if (recordsScanned === 0) {
    const r = refuse("NO_SCANNED_RECORDS",
      "zero in-schema records scanned across the included shards; zero records read is not zero matches",
      {
        ...base, dirRealpath: dirReal, symlinksSkipped,
        shardsBudgeted: included.map((x) => x.name), aggregateBudgetedBytes: aggregate,
        shardsRead, schemaOther, schemaOtherCollapsed,
        malformedLines, overlongLinesSkipped: overlongLines, nonObjectLines,
        schemaLessLines,
      });
    return finish(r.receipt, o, 2);
  }

  let maxAgeSec = null;
  if (maxTs !== null) {
    const t = Date.parse(maxTs);
    if (Number.isFinite(t)) maxAgeSec = Math.max(0, Math.round((nowMs - t) / 1000));
  }
  const receipt = {
    ...base,
    outcome: matchesTotal > 0 ? "MATCHES_FOUND" : "NO_MATCHES",
    dirRealpath: dirReal,
    symlinksSkipped,
    shardsIncluded: included.map((x) => ({ name: x.name, hour: x.hour, bytes: x.size })),
    shardsExcludedOverBudget: excluded.map((x) => ({ name: x.name, hour: x.hour, bytes: x.size })),
    aggregateBytes: aggregate,
    shardsRead,
    recordsScanned,
    malformedLines,
    overlongLinesSkipped: overlongLines,
    nonObjectLines,
    schemaLessLines,
    schemaOther,
    schemaOtherCollapsed,
    minRecordTs: minTs,
    maxRecordTs: maxTs,
    maxRecordAgeSec: maxAgeSec,
    freshestMetaContext: maxTsCtx,
    matchesTotal,
    matchesTruncated: Math.max(0, matchesTotal - matchesRetained.length),
    matches: matchesRetained,
  };
  return finish(receipt, o, 0);
}

function finish(receipt, o, code) {
  const text = JSON.stringify(receipt, null, 2) + "\n";
  if (o.receipt) {
    try {
      fs.writeFileSync(o.receipt, text, { encoding: "utf8", mode: 0o600 });
    } catch (err) {
      process.stderr.write(`receipt write failed: ${String(err.message || err)}\n`);
      process.exit(2);
    }
  } else {
    process.stdout.write(text);
  }
  process.exit(code);
}

main();
