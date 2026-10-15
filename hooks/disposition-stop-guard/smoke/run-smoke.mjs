#!/usr/bin/env node
// Real-harness smoke test for the Disposition Stop guard. Run it by hand before an install and
// after a Claude Code upgrade:  node hooks/disposition-stop-guard/smoke/run-smoke.mjs
//
// It runs the real `claude` CLI against a local stub of the Messages API (stub-anthropic.mjs) and a
// local stub of the Paperclip API, with the hook configured through --settings, in a throwaway
// HOME. Nothing leaves loopback, no credential is used, and nothing under the real ~/.claude is
// read or written.
//
// What it proves that the unit tests cannot: that the hook process inherits PAPERCLIP_TASK_ID,
// PAPERCLIP_API_URL, PAPERCLIP_API_KEY and PAPERCLIP_AGENT_ID from the claude process (the one
// assumption the install depends on), that it reads the card with that key, that Claude Code honours
// {"decision":"block"} and feeds the reason to the model, and that the run ends at the guard's cap.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOOK = join(HERE, "..", "disposition-stop-guard.mjs");
const STUB = join(HERE, "stub-anthropic.mjs");

const AGENT = "smoke-agent";
const RUN = "smoke-run";
const KEY = "smoke-run-jwt-not-a-credential";

const card = (extra = {}) => ({
  id: "smoke-task",
  identifier: "case_f052904a",
  status: "in_progress",
  assigneeAgentId: AGENT,
  conversationAgentId: null,
  executionRunId: RUN,
  monitorNextCheckAt: null,
  executionState: null,
  blockedBy: [],
  blockerAttention: { unresolvedBlockerCount: 0 },
  ...extra,
});

function startAnthropicStub(logPath) {
  return new Promise((resolve, reject) => {
    const child = spawn("node", [STUB], { env: { ...process.env, STUB_LOG: logPath }, stdio: ["ignore", "pipe", "inherit"] });
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += chunk;
      const match = /PORT (\d+)/.exec(out);
      if (match) resolve({ child, port: Number(match[1]) });
    });
    child.on("error", reject);
    setTimeout(() => reject(new Error("stub did not start")), 5000).unref();
  });
}

async function startPaperclipStub(cardBody) {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push({ method: req.method, url: req.url, auth: req.headers.authorization });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(req.url.endsWith("/interactions") ? [] : cardBody));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, seen, port: server.address().port };
}

async function runScenario({ name, cardBody, withTask = true, expect }) {
  const dir = mkdtempSync(join(tmpdir(), "disposition-stop-smoke-"));
  const stubLog = join(dir, "stub.jsonl");
  const guardLog = join(dir, "decisions.jsonl");
  const home = join(dir, "home");
  const cwd = join(dir, "cwd");
  mkdirSync(join(home, ".claude"), { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const settings = join(dir, "settings.json");
  writeFileSync(settings, JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: "command", command: `node ${HOOK}`, timeout: 15 }] }] } }));
  const anthropic = await startAnthropicStub(stubLog);
  const paperclip = await startPaperclipStub(cardBody);
  try {
    // Async spawn, not spawnSync: the Paperclip stub lives in this process and must keep answering.
    const run = await new Promise((resolve) => {
      const child = spawn("claude", ["-p", "Finish the wake.", "--settings", settings, "--output-format", "json"], {
        cwd,
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          PATH: process.env.PATH,
          HOME: home,
          CLAUDE_CONFIG_DIR: join(home, ".claude"),
          ANTHROPIC_BASE_URL: `http://127.0.0.1:${anthropic.port}`,
          ANTHROPIC_API_KEY: "stub-key-not-a-credential",
          DISABLE_TELEMETRY: "1",
          DISABLE_AUTOUPDATER: "1",
          CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
          PAPERCLIP_API_URL: `http://127.0.0.1:${paperclip.port}`,
          PAPERCLIP_API_KEY: KEY,
          PAPERCLIP_AGENT_ID: AGENT,
          PAPERCLIP_RUN_ID: RUN,
          ...(withTask ? { PAPERCLIP_TASK_ID: "smoke-task" } : {}),
          PAPERCLIP_RUN_SCRATCH_DIR: join(dir, "scratch"),
          DISPOSITION_STOP_GUARD_MODE: "enforce",
          DISPOSITION_STOP_GUARD_LOG: guardLog,
        },
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => (stdout += chunk));
      child.stderr.on("data", (chunk) => (stderr += chunk));
      const timer = setTimeout(() => child.kill("SIGKILL"), 120000);
      child.on("close", (status) => {
        clearTimeout(timer);
        resolve({ status, stdout, stderr });
      });
    });
    const read = (path) => (existsSync(path) ? readFileSync(path, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);
    const stubLines = read(stubLog);
    const mainCalls = stubLines.filter((l) => l.main);
    const guardLines = read(guardLog);
    const result = { name, exit: run.status, mainCalls: mainCalls.length, guard: guardLines.map((l) => `${l.action}:${l.why}`), requests: paperclip.seen.length, secondRequestLastUser: mainCalls[1]?.lastUser ?? null };
    try {
      expect({ run, mainCalls, guardLines, seen: paperclip.seen });
      return { ...result, ok: true };
    } catch (error) {
      return { ...result, ok: false, error: error.message, stderr: String(run.stderr).slice(0, 600), stdout: String(run.stdout).slice(0, 600) };
    }
  } finally {
    anthropic.child.kill();
    paperclip.server.closeAllConnections?.();
    await new Promise((resolve) => paperclip.server.close(resolve));
    rmSync(dir, { recursive: true, force: true });
  }
}

const scenarios = [
  {
    name: "card-without-a-path-is-handed-back-twice",
    cardBody: card(),
    expect: ({ run, mainCalls, guardLines, seen }) => {
      assert.equal(run.status, 0, "the run still ends");
      assert.equal(mainCalls.length, 3, "initial call plus exactly two hand-backs");
      assert.match(mainCalls[1].lastUser, /no live path/, "the hook's reason reached the model");
      assert.deepEqual(guardLines.map((l) => `${l.action}:${l.why}`), ["block:no-live-path", "block:no-live-path", "allow:handback-cap-reached"]);
      assert.equal(guardLines[0].stopHookActive, false);
      assert.equal(guardLines[1].stopHookActive, true, "Claude Code reports the continuation as stop_hook_active");
      assert.ok(seen.length >= 2, "the hook read the card through the inherited PAPERCLIP_API_URL");
      assert.ok(seen.some((s) => s.url === "/api/issues/smoke-task"), "and it is the run's own card");
      assert.ok(seen.every((s) => s.method === "GET" && s.auth === `Bearer ${KEY}`), "only GETs, with the inherited run key");
    },
  },
  {
    name: "card-with-a-scheduled-monitor-is-untouched",
    cardBody: card({ monitorNextCheckAt: "2026-10-10T23:00:00.000Z" }),
    expect: ({ run, mainCalls, guardLines }) => {
      assert.equal(run.status, 0);
      assert.equal(mainCalls.length, 1);
      assert.deepEqual(guardLines.map((l) => `${l.action}:${l.why}`), ["allow:monitor-scheduled"]);
    },
  },
  {
    name: "done-card-is-untouched",
    cardBody: card({ status: "done" }),
    expect: ({ mainCalls, guardLines }) => {
      assert.equal(mainCalls.length, 1);
      assert.deepEqual(guardLines.map((l) => l.why), ["status-done"]);
    },
  },
  {
    name: "run-without-a-task-does-nothing",
    cardBody: card(),
    withTask: false,
    expect: ({ mainCalls, guardLines, seen }) => {
      assert.equal(mainCalls.length, 1);
      assert.equal(guardLines.length, 0, "not even a log line");
      assert.equal(seen.length, 0, "and no request");
    },
  },
];

const results = [];
for (const scenario of scenarios) results.push(await runScenario(scenario));
for (const r of results) process.stdout.write(`${r.ok ? "PASS" : "FAIL"} ${r.name} ${JSON.stringify({ exit: r.exit, mainCalls: r.mainCalls, requests: r.requests, guard: r.guard })}\n`);
for (const r of results.filter((x) => !x.ok)) process.stderr.write(`\n${r.name}: ${r.error}\nstdout: ${r.stdout}\nstderr: ${r.stderr}\n`);
const second = results.find((r) => r.secondRequestLastUser);
if (second) process.stdout.write(`\nwhat the model saw after the block:\n${second.secondRequestLastUser}\n`);
process.exit(results.every((r) => r.ok) ? 0 : 1);
