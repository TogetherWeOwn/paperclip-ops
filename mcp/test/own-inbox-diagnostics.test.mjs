// ===========================================================================
// Own-inbox diagnostics acceptance suite.
//
// WHAT THIS SUITE IS FOR
//   The read_my_requests tool currently returns TEXT ONLY: whatever the queue
//   script printed, plus isError. A caller cannot tell an empty inbox from a
//   backend failure, a refusal from a timeout, or one decision from another
//   without parsing prose. This suite pins the STRUCTURED contract the builder
//   implements: a versioned machine-readable result alongside the compatible
//   text, with stable request ids, a closed error taxonomy, and an actionable
//   next step on every row and every failure.
//
// CANONICAL RESULT SHAPE (version 1) — the builder implements exactly this:
//   result.structuredContent = {
//     version: 1,                  // number, always 1 for this contract
//     requests: [                  // array, [] when there is nothing to show
//       {
//         id: "REQ-001",           // /^REQ-[0-9]{3,}$/, stable across reads
//         decision: "approved",    // approved|rejected|expired|failed|pending
//         next_action: "...",      // non-empty, actionable, no secrets
//         // `status` may alias `decision`; when both are present they agree.
//         // `delivery`, `at` and other row fields are optional, plain strings.
//       }
//     ],
//     diagnostics: {
//       complete: true,            // false when anything was skipped or failed
//       parsed: 2,                 // non-negative integer rows accepted
//       skipped: 0,                // non-negative integer rows dropped
//       truncated: false,          // true when output was bounded mid-stream
//     },
//     // Present exactly when isError is true:
//     error: {
//       code: "inbox_parse_error", // stable machine code, see below
//       next_action: "...",        // non-empty, actionable, no secrets
//       exitCode: 2,               // retained CLI exit info when there is one
//     },
//   }
//
// STABLE MACHINE CODES — same failure, same code, every time:
//   inbox_refused       the CLI refused (exit 2, "REFUSED: ...")
//   inbox_timeout       the CLI did not answer in time
//   inbox_backend_error the CLI could not run or its output was unusable
//                       (spawn failure, buffer overflow, backend outage)
//   inbox_parse_error   success-shaped output that is not machine output:
//                       malformed JSONL, wrong row shapes, unparseable prose
//   invalid_argument | unknown_argument | identity_argument_refused
//                       invalid tool arguments; the structured code matches the
//                       code already named in the text refusal.
// Codes are distinct across failure modes: refusal, timeout, backend and
// parse failures never share a code.
//
// EMPTY SEMANTICS — one rule in both layers:
//   * The CLI's `inbox --for X --json` with a missing or empty queue emits
//     machine-parseable EMPTY (zero rows), never the prose "(no decisions)".
//   * The transport maps zero rows AND the legacy "(no decisions)" prose to
//     { requests: [], diagnostics: { complete: true, ... } } with
//     isError false. Any OTHER unparseable text is inbox_parse_error with
//     isError true: structured success is never inferred from prose.
//
// WHAT DOES NOT CHANGE (pinned as controls, green before and after):
//   * read_my_requests takes NO arguments; identity stays the authenticated
//     principal, corroborated exactly as today.
//   * The read reaches ONLY `inbox --for <caller> --json`: no `list`, no
//     company sweep, no capability read, no submit/review/grant/notify/drain.
//   * Text content and isError stay compatible; the structured result is
//     alongside, never instead.
//   * Reading never acks and never changes an authorization outcome. It MAY
//     materialize derived expiry (a fact the queue already computes), exactly
//     as `inbox` always has.
//   * No bearer, credential, private config, raw backend dump or control
//     character reaches the caller as a trusted instruction.
//
// OFFLINE BY CONSTRUCTION
//   Every test injects canned runner responses or stub scripts in a scratch
//   dir, with requireLiveRun off (one control stubs the corroborator exec).
//   No database, no network, no live queue, no credentials. The CLI check
//   executes only the extracted `cmd_inbox` function with stubbed helpers.
//   This suite needs a full checkout (it reads ../../org_request_queue.sh);
//   it never runs in the mcp-only staging copy.
// ===========================================================================

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  TOOLS,
  normalizeConfig,
  buildQueueArgs,
  makeScriptRunner,
  makeRunCorroborator,
  createServer,
} from "../org-request-mcp.mjs";

// --- fixtures --------------------------------------------------------------

const BEARER = "test-bearer-not-a-real-credential";
const BEARER_SHA = createHash("sha256").update(BEARER, "utf8").digest("hex");

const COMPANY = "00000000-0000-4000-8000-000000000000";
const CALLER = "6a02a7ed-c2f9-4638-bd44-b453c589cbb1";
const OTHER_CALLER = "70f9e158-e8c6-4be4-bc0f-6ad5770a3f44";
const RUN = "11111111-2222-3333-4444-555555555555";

// Synthetic marker placed inside canned backend output. It must never cross
// into the structured result: the transport retains exit info, not dumps.
const LEAK_MARKER = "SHOULD-NOT-LEAK-MARKER-7f3a9c";

const REQ_RE = /^REQ-[0-9]{3,}$/;
const DECISIONS = new Set(["approved", "rejected", "expired", "failed", "pending"]);
const CODE_RE = /^[a-z][a-z0-9_]{2,40}$/;
// Control characters and ESC are never trusted instructions. Newline and tab
// are allowed (prose bodies); everything else in C0/C1 plus DEL and ESC is not.
const CONTROL_CHARS_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x1b]/;

const QUEUE_SCRIPT = fileURLToPath(new URL("../../org_request_queue.sh", import.meta.url));

const scratchDirs = [];
process.on("exit", () => {
  for (const dir of scratchDirs) {
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
});

function scratch() {
  const dir = mkdtempSync(path.join(tmpdir(), "own-inbox-diag-"));
  scratchDirs.push(dir);
  return dir;
}

// A stub org_request_queue.sh. It must be NAMED that: the server refuses to
// front anything else, and a stub called something else would test around it.
function stubQueue(dir, body) {
  const file = path.join(dir, "org_request_queue.sh");
  writeFileSync(
    file,
    body ?? `#!/bin/sh
: > "$(dirname "$0")/argv.txt"
for a in "$@"; do printf '%s\\n' "$a" >> "$(dirname "$0")/argv.txt"; done
printf 'COMPANY_ID=%s\\n' "$COMPANY_ID" > "$(dirname "$0")/env.txt"
env | grep -c BEARER >> "$(dirname "$0")/env.txt" 2>/dev/null || true
echo "(no decisions for test)"
`,
    { mode: 0o700 },
  );
  return file;
}

function recordedArgv(dir) {
  const file = path.join(dir, "argv.txt");
  if (!existsSync(file)) return null;
  return readFileSync(file, "utf8").split("\n").filter((line) => line.length > 0);
}

function configFor(dir, { queueBody, ...overrides } = {}) {
  return normalizeConfig({
    companyId: COMPANY,
    bearerSha256: BEARER_SHA,
    queueScript: stubQueue(dir, queueBody),
    requireLiveRun: false,
    ...overrides,
  });
}

function headersFor(agentId) {
  return {
    "x-paperclip-agent-id": agentId,
    "x-paperclip-company-id": COMPANY,
    "x-paperclip-run-id": RUN,
  };
}
const IDENTITY_HEADERS = headersFor(CALLER);

async function withServer(cfg, deps, fn) {
  const server = createServer(cfg, deps);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  const call = async (bodyObj, headers = {}, method = "POST") => {
    const response = await fetch(`http://127.0.0.1:${port}/mcp`, {
      method,
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        authorization: `Bearer ${BEARER}`,
        ...headers,
      },
      body: method === "POST" ? JSON.stringify(bodyObj) : undefined,
    });
    const text = await response.text();
    return { status: response.status, headers: response.headers, json: text ? JSON.parse(text) : null };
  };
  try {
    await fn(call);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

const rpc = (method, params, id = 1) => ({ jsonrpc: "2.0", id, method, params });
const readCall = (args = {}) => rpc("tools/call", { name: "read_my_requests", arguments: args });

// One notify_states-shaped JSONL row, as `inbox --json` emits it.
function inboxRow(overrides = {}) {
  return JSON.stringify({
    rid: "REQ-001",
    audience: "requester",
    status: "approved",
    role: "SYNTHETIC_ROLE",
    agent: CALLER,
    capability: "push",
    state: "delivered",
    at: "2026-10-21T00:00:00Z",
    body: "Request REQ-001 (SYNTHETIC — \"synthetic\") is now approved.",
    detail: "",
    ...overrides,
  });
}

// --- structured-contract assertions ----------------------------------------

function structuredOf(response, label) {
  const sc = response.json?.result?.structuredContent;
  assert.ok(
    sc && typeof sc === "object" && !Array.isArray(sc),
    `${label}: missing structuredContent — the read tool still returns text only`,
  );
  return sc;
}

function assertCleanString(value, label) {
  assert.equal(typeof value, "string", `${label} must be a string`);
  assert.doesNotMatch(value, CONTROL_CHARS_RE, `${label} carries control characters`);
  assert.ok(!value.includes(BEARER), `${label} leaks the bearer`);
  assert.ok(!value.includes(BEARER_SHA), `${label} leaks the bearer digest`);
  return value;
}

function assertNextAction(value, label) {
  assertCleanString(value, label);
  assert.ok(
    value.length >= 12,
    `${label} is not actionable (under 12 characters): ${JSON.stringify(value)}`,
  );
  assert.ok(
    !value.includes(LEAK_MARKER),
    `${label} echoes backend output that must stay behind the transport`,
  );
}

function assertRequestRow(row, label) {
  assert.ok(row && typeof row === "object" && !Array.isArray(row), `${label} must be an object`);
  assert.match(String(row.id ?? ""), REQ_RE, `${label} has no stable request id`);
  const decision = row.decision ?? row.status;
  assert.ok(
    typeof decision === "string" && DECISIONS.has(decision),
    `${label} has no closed decision (approved|rejected|expired|failed|pending)`,
  );
  if (row.decision !== undefined && row.status !== undefined) {
    assert.equal(row.status, row.decision, `${label}: status and decision disagree`);
  }
  assert.ok("next_action" in row, `${label} carries no next_action`);
  assertNextAction(row.next_action, `${label}.next_action`);
  for (const key of ["id", "decision", "status", "delivery", "at"]) {
    if (row[key] !== undefined) assertCleanString(String(row[key]), `${label}.${key}`);
  }
}

function assertDiagnostics(diag, label, { expectComplete } = {}) {
  assert.ok(diag && typeof diag === "object" && !Array.isArray(diag), `${label} must be an object`);
  assert.equal(typeof diag.complete, "boolean", `${label}.complete must be a boolean`);
  for (const key of ["parsed", "skipped"]) {
    assert.ok(
      Number.isInteger(diag[key]) && diag[key] >= 0,
      `${label}.${key} must be a non-negative integer`,
    );
  }
  assert.equal(typeof diag.truncated, "boolean", `${label}.truncated must be a boolean`);
  assert.ok(
    diag.parsed + diag.skipped <= 10000,
    `${label} is unbounded (parsed ${diag.parsed} + skipped ${diag.skipped})`,
  );
  if (diag.skipped > 0 || diag.truncated) {
    assert.equal(diag.complete, false, `${label}: skipped/truncated rows cannot report complete`);
  }
  if (expectComplete) {
    assert.equal(diag.complete, true, `${label} should be complete`);
    assert.equal(diag.skipped, 0, `${label} should skip nothing`);
  }
}

function assertCanonicalShape(sc, label) {
  assert.equal(sc.version, 1, `${label}: versioned result must carry version 1`);
  assert.ok(Array.isArray(sc.requests), `${label}: requests must be an array`);
  if (sc.tool !== undefined) {
    assert.equal(sc.tool, "read_my_requests", `${label}: tool must name this read`);
  }
  assertDiagnostics(sc.diagnostics, `${label}.diagnostics`);
  for (const [index, row] of sc.requests.entries()) {
    assertRequestRow(row, `${label}.requests[${index}]`);
  }
}

function assertErrorShape(error, expectedCode, label, { expectExit } = {}) {
  assert.ok(error && typeof error === "object", `${label} must be an object`);
  assert.match(String(error.code ?? ""), CODE_RE, `${label}.code is not a stable machine code`);
  assert.equal(error.code, expectedCode, `${label}.code should be ${expectedCode}`);
  assert.ok("next_action" in error, `${label} carries no next_action`);
  assertNextAction(error.next_action, `${label}.next_action`);
  if (expectExit !== undefined) {
    assert.equal(error.exitCode, expectExit, `${label} should retain exitCode ${expectExit}`);
  }
  if (error.detail !== undefined) {
    const detail = assertCleanString(String(error.detail), `${label}.detail`);
    assert.ok(detail.length <= 2000, `${label}.detail is an unbounded backend dump`);
    assert.ok(!detail.includes(LEAK_MARKER), `${label}.detail echoes backend output`);
  }
}

// --- controls: pinned behavior, green before and after ----------------------

test("control: the read tool still declares no properties at all", () => {
  const tool = TOOLS.find((candidate) => candidate.name === "read_my_requests");
  assert.ok(tool, "read_my_requests is gone; the transport is write-only again");
  assert.deepEqual(Object.keys(tool.inputSchema.properties), []);
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.deepEqual(tool.inputSchema.required ?? [], []);
});

test("control: naming another caller in the read arguments is refused, never honoured", async () => {
  const dir = scratch();
  await withServer(configFor(dir), {}, async (call) => {
    for (const args of [{ requester: OTHER_CALLER }, { for: OTHER_CALLER }, { json: true }]) {
      const response = await call(readCall(args), IDENTITY_HEADERS);
      assert.equal(response.json.result.isError, true, `read accepted ${JSON.stringify(args)}`);
      assert.match(
        response.json.result.content[0].text,
        /identity_argument_refused|unknown_argument/,
      );
      assert.equal(recordedArgv(dir), null, "the queue script ran for a refused read");
    }
  });
});

test("control: an unauthenticated read is refused exactly as an unauthenticated write is", async () => {
  const dir = scratch();
  await withServer(configFor(dir), {}, async (call) => {
    const response = await call(readCall(), {});
    assert.equal(response.status, 403);
    assert.equal(response.json.error.code, "identity_header_missing");
    assert.equal(recordedArgv(dir), null);
  });
});

test("control: the read reaches only the inbox subcommand, never a decision or delivery action", () => {
  const identity = { agentId: CALLER, companyId: COMPANY, runId: RUN };
  const argv = buildQueueArgs("read_my_requests", {}, identity);
  assert.equal(argv[0], "inbox");
  for (const forbidden of [
    "submit", "review", "countersign", "list", "ack", "ack-risk", "ack-override",
    "notify", "--drain", "--approve", "--reject", "disable-template", "enable-template",
  ]) {
    assert.ok(!argv.includes(forbidden), `the read tool reaches '${forbidden}', which is not a read`);
  }
});

test("control: text content and isError stay compatible on success", async () => {
  const dir = scratch();
  const text = "(no decisions for test)";
  await withServer(configFor(dir), { audit: () => {} }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
    assert.equal(typeof response.json.result.content[0].text, "string");
    assert.ok(response.json.result.content[0].text.length > 0);
    assert.equal(response.json.result.content[0].text, text);
  });
});

test("control: the bearer never reaches argv, env, text or structured output", async () => {
  const dir = scratch();
  await withServer(configFor(dir), { audit: () => {} }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    const argv = recordedArgv(dir);
    assert.ok(argv && !argv.some((a) => a.includes(BEARER)), "argv carries the bearer");
    const envText = readFileSync(path.join(dir, "env.txt"), "utf8");
    assert.ok(!envText.includes(BEARER), "script environment carries the bearer");
    assert.ok(!response.json.result.content[0].text.includes(BEARER), "text carries the bearer");
    const sc = response.json.result.structuredContent;
    if (sc !== undefined) {
      assert.ok(!JSON.stringify(sc).includes(BEARER), "structured output carries the bearer");
    }
  });
});

test("control: a read never reaches the capability runner", async () => {
  const dir = scratch();
  let queueCalls = 0;
  let capabilityCalls = 0;
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async (argv) => {
      queueCalls += 1;
      assert.equal(argv[0], "inbox");
      return { ok: true, text: "", exitCode: 0 };
    },
    runCapability: async () => {
      capabilityCalls += 1;
      return { ok: false, text: "must not be called", exitCode: null };
    },
  }, async (call) => {
    await call(readCall(), IDENTITY_HEADERS);
    assert.equal(queueCalls, 1, "the read did not reach the queue runner exactly once");
    assert.equal(capabilityCalls, 0, "the read reached the capability runner");
  });
});

test("control: corroboration still runs before the queue", async () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const corroborateRun = makeRunCorroborator(
    { ...cfg, requireLiveRun: true },
    async () => ({ stdout: "", stderr: "" }),
  );
  await withServer(cfg, { audit: () => {}, corroborateRun }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.status, 403);
    assert.equal(response.json.error.code, "run_not_live");
    assert.equal(recordedArgv(dir), null, "the queue ran for an uncorroborated read");
  });
});

test("control: no diagnostic or dry-run flag executes a mutation", () => {
  for (const tool of TOOLS) {
    if (!/dry|diagnos/i.test(tool.name)) continue;
    const identity = { agentId: CALLER, companyId: COMPANY, runId: RUN };
    const argv = buildQueueArgs(tool.name, {}, identity);
    assert.equal(argv[0], "inbox", `${tool.name} is a diagnostic that reaches '${argv[0]}'`);
  }
  const identity = { agentId: CALLER, companyId: COMPANY, runId: RUN };
  const argv = buildQueueArgs("read_my_requests", {}, identity);
  for (const flag of ["--dry-run", "--diagnose", "--diagnostic"]) {
    assert.ok(!argv.includes(flag), `the read carries ${flag}`);
  }
});

// --- new behavior: red until the builder lands the structured contract ------

test("argv is the authenticated inbox plus the fixed machine-output flag", async () => {
  const dir = scratch();
  await withServer(configFor(dir), { audit: () => {} }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
    const argv = recordedArgv(dir);
    assert.ok(argv, "the queue script never ran");
    assert.equal(argv[0], "inbox", "the read must reach `inbox`, never `list`");
    const forIndex = argv.indexOf("--for");
    assert.ok(forIndex >= 0, "argv carries no --for selector");
    assert.equal(argv[forIndex + 1], CALLER, "--for must carry the authenticated caller");
    assert.ok(argv.includes("--json"), "argv carries no fixed machine-output flag");
    assert.equal(argv.length, 4, `argv must be exactly inbox + --for + caller + --json, got ${JSON.stringify(argv)}`);
    assert.ok(!argv.includes(OTHER_CALLER), "another caller's id appears in the argv");
    assert.ok(!argv.includes("list"), "the read reaches `list`, a company-wide sweep");
  });
});

test("decided rows return a versioned structured result with stable ids", async () => {
  const dir = scratch();
  const first = inboxRow({ rid: "REQ-001", status: "approved", agent: CALLER });
  const second = inboxRow({ rid: "REQ-002", status: "rejected", agent: CALLER, state: "failed" });
  const seen = [];
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: `${first}\n${second}`, exitCode: 0 }),
  }, async (call) => {
    for (let round = 0; round < 2; round += 1) {
      const response = await call(readCall(), IDENTITY_HEADERS);
      assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
      const sc = structuredOf(response, `round ${round}`);
      assertCanonicalShape(sc, `round ${round}`);
      assertDiagnostics(sc.diagnostics, `round ${round}.diagnostics`, { expectComplete: true });
      assert.equal(sc.requests.length, 2, "both decided rows must be present");
      assert.deepEqual(sc.requests.map((row) => row.id), ["REQ-001", "REQ-002"]);
      assert.deepEqual(sc.requests.map((row) => row.decision ?? row.status), ["approved", "rejected"]);
      seen.push(JSON.stringify(sc.requests.map((row) => [row.id, row.decision ?? row.status])));
    }
    assert.equal(seen[0], seen[1], "stable input must yield stable request ids");
  });
});

test("an empty inbox is an empty collection, not a failure", async () => {
  const dir = scratch();
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: "", exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
    const sc = structuredOf(response, "empty inbox");
    assertCanonicalShape(sc, "empty inbox");
    assert.deepEqual(sc.requests, [], "an empty inbox must be an empty collection");
    assertDiagnostics(sc.diagnostics, "empty inbox.diagnostics", { expectComplete: true });
    assert.equal(sc.error, undefined, "an empty inbox must not carry an error");
  });
});

test("a missing queue reads as empty, never as inferred success from prose", async () => {
  const dir = scratch();
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: "(no decisions)", exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
    assert.match(response.json.result.content[0].text, /no decisions/);
    const sc = structuredOf(response, "missing queue");
    assertCanonicalShape(sc, "missing queue");
    assert.deepEqual(sc.requests, [], "a missing queue must read as an empty collection");
    assertDiagnostics(sc.diagnostics, "missing queue.diagnostics", { expectComplete: true });
  });
});

test("malformed JSONL is a parse failure with a stable code and next action", async () => {
  const dir = scratch();
  const good = inboxRow({ rid: "REQ-003", status: "expired", agent: CALLER });
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: `${good}\n{not json\nplain prose`, exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "partially unparseable output read as success");
    const sc = structuredOf(response, "malformed JSONL");
    assertCanonicalShape(sc, "malformed JSONL");
    assert.equal(sc.requests.length, 1, "only the well-formed row survives");
    assert.equal(sc.requests[0].id, "REQ-003");
    assert.equal(sc.diagnostics.parsed, 1);
    assert.equal(sc.diagnostics.skipped, 2);
    assert.equal(sc.diagnostics.complete, false);
    assertErrorShape(sc.error, "inbox_parse_error", "malformed JSONL.error");
  });
});

test("wrong row shapes are skipped, never trusted", async () => {
  const dir = scratch();
  const good = inboxRow({ rid: "REQ-004", status: "failed", agent: CALLER });
  const missingId = JSON.stringify({ status: "approved", agent: CALLER });
  const missingDecision = JSON.stringify({ rid: "REQ-005", agent: CALLER });
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: `${good}\n${missingId}\n${missingDecision}`, exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "wrong-shaped rows read as success");
    const sc = structuredOf(response, "wrong row shapes");
    assertCanonicalShape(sc, "wrong row shapes");
    assert.deepEqual(sc.requests.map((row) => row.id), ["REQ-004"]);
    assert.ok(sc.diagnostics.skipped >= 2, "misshapen rows must be counted as skipped");
    assert.equal(sc.diagnostics.complete, false);
    assertErrorShape(sc.error, "inbox_parse_error", "wrong row shapes.error");
  });
});

test("unparseable success text is never inferred as success", async () => {
  const dir = scratch();
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: "@@@garbage@@@\nmore garbage", exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "unparseable text read as success");
    const sc = structuredOf(response, "unparseable text");
    assertCanonicalShape(sc, "unparseable text");
    assert.deepEqual(sc.requests, [], "nothing parsed means nothing listed");
    assert.equal(sc.diagnostics.complete, false);
    assert.ok(sc.diagnostics.skipped >= 1, "unparseable lines must be counted");
    assertErrorShape(sc.error, "inbox_parse_error", "unparseable text.error");
  });
});

test("a refusal keeps its exit info and gains a stable code", async () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const stubExec = async () => {
    const error = new Error("Command failed");
    error.code = 2;
    error.stdout = "";
    error.stderr = "REFUSED: the caller may not read this queue";
    throw error;
  };
  await withServer(cfg, {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: makeScriptRunner(cfg.queueScript, cfg, stubExec),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "a refusal read as success");
    assert.match(response.json.result.content[0].text, /REFUSED/);
    const sc = structuredOf(response, "refusal");
    assertCanonicalShape(sc, "refusal");
    assert.deepEqual(sc.requests, [], "a refusal lists no decisions");
    assert.equal(sc.diagnostics.complete, false);
    assertErrorShape(sc.error, "inbox_refused", "refusal.error", { expectExit: 2 });
  });
});

test("a timeout is distinct from a refusal and from a backend failure", async () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const stubExec = async () => {
    const error = new Error("the request script timed out");
    error.killed = true;
    error.signal = "SIGTERM";
    error.code = null;
    error.stdout = "";
    error.stderr = "";
    throw error;
  };
  await withServer(cfg, {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: makeScriptRunner(cfg.queueScript, cfg, stubExec),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "a timeout read as success");
    const sc = structuredOf(response, "timeout");
    assertCanonicalShape(sc, "timeout");
    assert.deepEqual(sc.requests, [], "a timeout lists no decisions");
    assertErrorShape(sc.error, "inbox_timeout", "timeout.error");
  });
});

test("a backend or buffer failure is distinct with an actionable next step", async () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const stubExec = async () => {
    const error = new Error("spawn ENOBUFS");
    error.code = "ENOBUFS";
    error.stdout = "";
    error.stderr = `backend overflow ${LEAK_MARKER}`;
    throw error;
  };
  await withServer(cfg, {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: makeScriptRunner(cfg.queueScript, cfg, stubExec),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    assert.equal(response.json.result.isError, true, "a backend failure read as success");
    const sc = structuredOf(response, "backend failure");
    assertCanonicalShape(sc, "backend failure");
    assertErrorShape(sc.error, "inbox_backend_error", "backend failure.error");
    assert.ok(
      !JSON.stringify(sc).includes(LEAK_MARKER),
      "structured output echoes raw backend output",
    );
  });
});

test("invalid arguments carry a structured error matching the refusal code", async () => {
  const dir = scratch();
  await withServer(configFor(dir), { audit: () => {} }, async (call) => {
    for (const args of [{ requester: OTHER_CALLER }, { for: OTHER_CALLER }]) {
      const response = await call(readCall(args), IDENTITY_HEADERS);
      assert.equal(response.json.result.isError, true, `read accepted ${JSON.stringify(args)}`);
      const text = response.json.result.content[0].text;
      const code = (/REFUSED \((\w+)\):/.exec(text) || [])[1];
      assert.ok(code, `refusal names no machine code: ${JSON.stringify(text)}`);
      assert.ok(
        ["invalid_argument", "unknown_argument", "identity_argument_refused"].includes(code),
        `unexpected argument refusal code: ${code}`,
      );
      const sc = structuredOf(response, `invalid args ${JSON.stringify(args)}`);
      assertCanonicalShape(sc, `invalid args ${JSON.stringify(args)}`);
      assert.deepEqual(sc.requests, [], "a refused read lists no decisions");
      assertErrorShape(sc.error, code, `invalid args ${JSON.stringify(args)}.error`);
      assert.equal(recordedArgv(dir), null, "the queue script ran for a refused read");
    }
  });
});

test("codes are stable: the same failure repeats, different failures differ", async () => {
  const dir = scratch();
  const refusal = { ok: false, text: "REFUSED: synthetic refusal", exitCode: 2 };
  const codes = [];
  await withServer(configFor(dir), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => refusal,
  }, async (call) => {
    for (let round = 0; round < 2; round += 1) {
      const response = await call(readCall(), IDENTITY_HEADERS);
      codes.push(structuredOf(response, `refusal round ${round}`).error.code);
    }
  });
  assert.equal(codes[0], codes[1], "the same refusal produced different codes");

  const cfg = configFor(scratch());
  const timeoutExec = async () => {
    const error = new Error("the request script timed out");
    error.killed = true;
    error.signal = "SIGTERM";
    error.stdout = "";
    error.stderr = "";
    throw error;
  };
  await withServer(cfg, {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: makeScriptRunner(cfg.queueScript, cfg, timeoutExec),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    codes.push(structuredOf(response, "timeout").error.code);
  });

  await withServer(configFor(scratch()), {
    audit: () => {},
    corroborateRun: async () => {},
    runQueue: async () => ({ ok: true, text: "@@@garbage@@@", exitCode: 0 }),
  }, async (call) => {
    const response = await call(readCall(), IDENTITY_HEADERS);
    codes.push(structuredOf(response, "parse failure").error.code);
  });

  assert.equal(new Set(codes).size, 3, `refusal, timeout and parse failures must use distinct codes, got ${JSON.stringify(codes)}`);
});

test("every failure has an actionable next step and leaks nothing", async () => {
  const cases = [
    { label: "refusal", run: { ok: false, text: "REFUSED: synthetic refusal", exitCode: 2 } },
    { label: "parse", run: { ok: true, text: "@@@garbage@@@", exitCode: 0 } },
    { label: "backend", run: { ok: false, text: `backend boom ${LEAK_MARKER}`, exitCode: null } },
  ];
  for (const { label, run } of cases) {
    const dir = scratch();
    await withServer(configFor(dir), {
      audit: () => {},
      corroborateRun: async () => {},
      runQueue: async () => run,
    }, async (call) => {
      const response = await call(readCall(), IDENTITY_HEADERS);
      assert.equal(response.json.result.isError, true, `${label} read as success`);
      assertCleanString(response.json.result.content[0].text, `${label} text`);
      const sc = structuredOf(response, label);
      assertCanonicalShape(sc, label);
      assert.match(String(sc.error?.code ?? ""), CODE_RE, `${label} error code is not stable`);
      assertNextAction(sc.error.next_action, `${label}.error.next_action`);
      const flat = JSON.stringify(sc);
      assert.ok(!flat.includes(BEARER), `${label} structured output leaks the bearer`);
      assert.ok(!flat.includes(LEAK_MARKER), `${label} structured output echoes backend output`);
      assert.doesNotMatch(flat, CONTROL_CHARS_RE, `${label} structured output carries control characters`);
    });
  }
});

test("no foreign inbox: structured rows belong to the caller only", async () => {
  const callerRow = inboxRow({ rid: "REQ-010", status: "approved", agent: CALLER });
  const otherRow = inboxRow({ rid: "REQ-011", status: "approved", agent: OTHER_CALLER });
  const seenArgv = [];
  const runFor = (text) => async (argv) => {
    seenArgv.push(argv);
    return { ok: true, text, exitCode: 0 };
  };
  const dir = scratch();
  const cfg = configFor(dir);
  const idsFor = async (agentId, text) => {
    let ids = null;
    await withServer(cfg, {
      audit: () => {},
      corroborateRun: async () => {},
      runQueue: runFor(text),
    }, async (call) => {
      const response = await call(readCall(), headersFor(agentId));
      assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
      ids = structuredOf(response, `inbox for ${agentId}`).requests.map((row) => row.id);
    });
    return ids;
  };
  assert.deepEqual(await idsFor(CALLER, callerRow), ["REQ-010"]);
  assert.deepEqual(await idsFor(OTHER_CALLER, otherRow), ["REQ-011"]);
  assert.equal(seenArgv.length, 2);
  for (const [index, agentId] of [CALLER, OTHER_CALLER].entries()) {
    const at = seenArgv[index].indexOf("--for");
    assert.equal(seenArgv[index][at + 1], agentId, "argv --for does not match the caller");
  }
});

test("reading is idempotent but may materialize derived expiry; it never acks", async () => {
  const dir = scratch();
  const queueFile = path.join(dir, "queue.jsonl");
  writeFileSync(queueFile, `${JSON.stringify({ event: "request.submitted", requestId: "REQ-009", status: "pending" })}\n`);
  const row = inboxRow({ rid: "REQ-009", status: "expired", agent: CALLER });
  // The stub plays the queue's part: a read may append the DERIVED expiry
  // fact, exactly as `inbox` reaps, but never a decision row of any kind.
  const queueBody = `#!/bin/sh
: > "$(dirname "$0")/argv.txt"
for a in "$@"; do printf '%s\\n' "$a" >> "$(dirname "$0")/argv.txt"; done
if ! grep -q '"event":"request.expired"' "$FAKE_QUEUE" 2>/dev/null; then
  printf '%s\\n' '{"event":"request.expired","requestId":"REQ-009","status":"expired"}' >> "$FAKE_QUEUE"
fi
printf '%s\\n' '${row.replace(/'/g, "'\\''")}'
`;
  const cfg = configFor(dir, { queueBody, queueEnv: { FAKE_QUEUE: queueFile } });
  const decisions = [];
  await withServer(cfg, { audit: () => {} }, async (call) => {
    for (let round = 0; round < 2; round += 1) {
      const response = await call(readCall(), IDENTITY_HEADERS);
      assert.equal(response.json.result.isError, false, response.json.result.content?.[0]?.text);
      const sc = structuredOf(response, `read ${round}`);
      assertCanonicalShape(sc, `read ${round}`);
      decisions.push(sc.requests.map((entry) => [entry.id, entry.decision ?? entry.status]));
    }
  });
  assert.deepEqual(decisions[0], decisions[1], "two reads of one inbox disagree on decisions");
  assert.deepEqual(decisions[0], [["REQ-009", "expired"]]);
  const queueText = readFileSync(queueFile, "utf8");
  assert.ok(queueText.includes('"event":"request.expired"'), "derived expiry was not materialized");
  assert.ok(!queueText.includes("request.reviewed"), "a read wrote a decision row");
  assert.ok(!queueText.includes("request.approved"), "a read wrote a decision row");
  assert.ok(!queueText.includes("request.rejected"), "a read wrote a decision row");
});

test("CLI: a missing queue with --json emits machine-parseable empty", () => {
  assert.ok(existsSync(QUEUE_SCRIPT), "this suite needs a full checkout with org_request_queue.sh");
  const source = readFileSync(QUEUE_SCRIPT, "utf8");
  const start = source.indexOf("cmd_inbox() {");
  assert.ok(start >= 0, "cmd_inbox is gone from org_request_queue.sh");
  const end = source.indexOf("\n}\n", start);
  assert.ok(end > start, "cmd_inbox has no closing brace to extract");
  const fnSource = source.slice(start, end + 3);
  const dir = scratch();
  const harness = path.join(dir, "harness.sh");
  writeFileSync(harness, `#!/bin/bash
die() { echo "REFUSED: $*" >&2; return 2; }
reap_expired() { return 0; }
notify_states() { return 0; }
tabulate() { cat; }
${fnSource}
cmd_inbox --for "${CALLER}" $1
`, { mode: 0o700 });
  const missing = path.join(dir, "no-such-queue.jsonl");
  const env = { ...process.env, QUEUE: missing, REQUEST_NOTIFY_CMD: "" };

  const textOut = execFileSync("bash", [harness, ""], { env, encoding: "utf8" });
  assert.match(textOut, /no decisions/, "text mode lost its empty marker");

  const jsonOut = execFileSync("bash", [harness, "--json"], { env, encoding: "utf8" });
  assert.doesNotMatch(jsonOut, /no decisions/, "--json mode emits prose instead of machine output");
  const nonEmpty = jsonOut.split("\n").filter((line) => line.trim() !== "");
  if (nonEmpty.length > 0) {
    let parsed = null;
    assert.doesNotThrow(() => { parsed = JSON.parse(jsonOut); }, "--json mode emits unparseable output");
    const rows = Array.isArray(parsed) ? parsed : parsed.requests ?? parsed.rows ?? null;
    assert.deepEqual(rows, [], "--json mode for a missing queue must be an empty collection");
  }
});

test("discovery advertises the structured inbox contract", () => {
  const tool = TOOLS.find((candidate) => candidate.name === "read_my_requests");
  assert.ok(tool, "read_my_requests is gone; the transport is write-only again");
  assert.match(
    tool.description,
    /structured/i,
    "discovery never mentions the structured result the caller is supposed to read",
  );
});
