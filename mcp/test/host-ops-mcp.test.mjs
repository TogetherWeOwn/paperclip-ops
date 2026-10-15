// ===========================================================================
// host-ops-mcp regression suite — case_16723128.
//
// WHAT THIS SUITE IS FOR
//   Every non-negotiable in the issue is an assertion here: Phase-0 read
//   verbs only, identity (agent/run/issue) from the gateway and never from
//   arguments, corroboration against heartbeat_runs, signature verification
//   against the pinned key before any byte reaches the caller.
//
//   The suite runs fully offline EXCEPT for the two section-9 tests, which
//   drive the REAL host-ops/broker.py in a scratch spool with a generated
//   signing key and fake executor facts (HOST_OPS_TEST_MODE=1). No podman,
//   no database, no company. That is a deliberate design constraint on the
//   server, not a convenience: a transport whose refusals can only be tested
//   against production is a transport whose refusals are not tested.
//
//   The real-broker tests do NOT skip when broker.py is absent from a staging
//   copy — the CI mutation harness stages host-ops/broker.py alongside mcp/,
//   so the full suite (including both acceptance tests) judges every
//   mutation. A skipped assertion cannot reject a mutation.
//
// TEST SEAM DISCIPLINE
//   Where a control depends on a credential or an identity, the test supplies
//   a FAKE one rather than omitting it. Omitting the input tests a different
//   code path from the one that ships, and a suite that passes because the
//   input was missing passes against the bug too.
// ===========================================================================

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync, chmodSync, readFileSync, existsSync, readdirSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  TOOLS,
  advertisedTools,
  ALLOWLISTED_VERBS,
  normalizeConfig,
  assertConfigPermissions,
  assertBrokerScript,
  bearerMatches,
  readIdentityHeaders,
  buildBrokerArgs,
  buildBrokerEnv,
  makeBrokerRunner,
  makeBundleVerifier,
  makeRunCorroborator,
  containerEngineFor,
  createServer,
  createHandler,
  HttpError,
  _resetRunCheckCache,
} from "../host-ops-mcp.mjs";

// --- fixtures --------------------------------------------------------------

const BEARER = "test-bearer-not-a-real-credential";
const BEARER_SHA = createHash("sha256").update(BEARER, "utf8").digest("hex");

const COMPANY = "00000000-0000-4000-8000-000000000004";
const OTHER_COMPANY = "00000000-0000-4000-8000-000000000021";
const CALLER = "00000000-0000-4000-8000-000000000022"; // the agent actually calling
const RUN = "00000000-0000-4000-8000-000000000021"; // valid v4 shape: broker.py UUID_RE enforces the variant bits
const ISSUE = "00000000-0000-4000-8000-000000000023";

// Removed at process exit so the shared runner /tmp does not accumulate one dir
// per scratch() call per run (case_6ab2cd4d). Best effort: cleanup never fails a
// suite that otherwise passed.
const scratchDirs = [];
process.on("exit", () => {
  for (const dir of scratchDirs) {
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
});

function scratch() {
  const dir = mkdtempSync(path.join(tmpdir(), "host-ops-mcp-test-"));
  scratchDirs.push(dir);
  return dir;
}

// A stub broker.py. It must be NAMED broker.py: the server refuses to front
// anything else, and a stub called something else would test around it.
function stubBroker(dir, body) {
  const file = path.join(dir, "broker.py");
  writeFileSync(
    file,
    body ?? `#!/usr/bin/env python3
import json, sys
print(json.dumps({"requestId": "HRQ-stub", "canonicalHash": "x", "requestPath": "/tmp/x", "expiresAt": "t"}))
`,
    { mode: 0o700 },
  );
  return file;
}

function stubKey(dir) {
  const file = path.join(dir, "response-signing.pub.pem");
  writeFileSync(file, "not a real key, only a path\n");
  return file;
}

function configFor(dir, overrides = {}) {
  return normalizeConfig({
    companyId: COMPANY,
    bearerSha256: BEARER_SHA,
    brokerScript: stubBroker(dir),
    brokerSpool: path.join(dir, "spool"),
    brokerConfig: path.join(dir, "broker-config.json"),
    publicKeyPath: stubKey(dir),
    expectedPublicKeySha256: "a".repeat(64),
    requireLiveRun: false,
    ...overrides,
  });
}

function identityHeaders(overrides = {}) {
  return {
    authorization: `Bearer ${BEARER}`,
    "x-paperclip-agent-id": CALLER,
    "x-paperclip-company-id": COMPANY,
    "x-paperclip-run-id": RUN,
    "x-paperclip-issue-id": ISSUE,
    ...overrides,
  };
}

// Minimal HTTP client against a live server from createServer().
async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return server.address().port;
}

function post(port, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = (
      globalThis.__httpModule ?? (globalThis.__httpModule = import("node:http"))
    ).then(({ default: http }) => {
      const r = http.request(
        {
          host: "127.0.0.1",
          port,
          path: "/mcp",
          method: "POST",
          headers: { "content-type": "application/json", "content-length": Buffer.byteLength(payload), ...headers },
        },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () =>
            resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString("utf8") }),
          );
        },
      );
      r.on("error", reject);
      r.end(payload);
    });
    void req;
  });
}

function rpcCall(tool, args = {}) {
  return { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: tool, arguments: args } };
}

// assert.throws returns undefined even when it validates the class — capture
// the HttpError (with its .code) via try/catch instead.
function throwsHttp(fn, { code, messagePattern } = {}) {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof HttpError, `expected HttpError, got ${error?.constructor?.name}: ${error?.message}`);
    if (code !== undefined) assert.equal(error.code, code);
    if (messagePattern !== undefined) assert.match(error.message, messagePattern);
    return error;
  }
  assert.fail("expected function to throw HttpError, but it did not throw");
}

// --- 1. config -------------------------------------------------------------

test("config: accepts a complete config and pins operator-owned defaults", () => {
  const dir = scratch();
  const cfg = configFor(dir);
  assert.equal(cfg.companyId, COMPANY);
  assert.equal(cfg.brokerSpool, path.join(dir, "spool"));
  assert.equal(cfg.dbContainer, "paperclip-db");
  assert.equal(cfg.requireLiveRun, false);
  assert.equal(cfg.responseWaitMs, 60000);
  assert.equal(cfg.responsePollMs, 500);
  assert.deepEqual(cfg.brokerEnv, {});
});

test("config: refuses a non-uuid company, a non-digest bearer, and a relative broker path", () => {
  const dir = scratch();
  const good = {
    companyId: COMPANY,
    bearerSha256: BEARER_SHA,
    brokerScript: stubBroker(dir),
    brokerSpool: path.join(dir, "spool"),
    brokerConfig: path.join(dir, "broker-config.json"),
    publicKeyPath: stubKey(dir),
    expectedPublicKeySha256: "a".repeat(64),
  };
  assert.throws(() => normalizeConfig({ ...good, companyId: "not-a-uuid" }), /companyId/);
  assert.throws(() => normalizeConfig({ ...good, bearerSha256: "short" }), /bearerSha256/);
  assert.throws(() => normalizeConfig({ ...good, brokerScript: "relative/broker.py" }), /brokerScript/);
  assert.throws(() => normalizeConfig({ ...good, publicKeyPath: "relative/key.pem" }), /publicKeyPath/);
  assert.throws(() => normalizeConfig({ ...good, expectedPublicKeySha256: "xyz" }), /expectedPublicKeySha256/);
  assert.throws(() => normalizeConfig({ ...good, brokerSpool: "relative/spool" }), /brokerSpool/);
  assert.throws(() => normalizeConfig({ ...good, brokerConfig: "relative/config.json" }), /brokerConfig/);
});

test("config: refuses a brokerEnv that restates operator-owned names", () => {
  const dir = scratch();
  for (const key of ["PATH", "HOME", "PAPERCLIP_DB_CTR"]) {
    assert.throws(() => configFor(dir, { brokerEnv: { [key]: "/tmp/evil" } }), /brokerEnv/,
      `brokerEnv naming ${key} must be refused`);
  }
});

test("config: operator-owned env wins over brokerEnv even for a hand-built cfg", () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const env = buildBrokerEnv({ ...cfg, brokerEnv: { PAPERCLIP_DB_CTR: "evil", FOO: "bar" } });
  assert.equal(env.PAPERCLIP_DB_CTR, "paperclip-db");
  assert.equal(env.FOO, "bar");
});

test("assertConfigPermissions: refuses group/other-readable configs", () => {
  const dir = scratch();
  const file = path.join(dir, "config.json");
  writeFileSync(file, "{}");
  chmodSync(file, 0o600);
  assert.doesNotThrow(() => assertConfigPermissions(file));
  chmodSync(file, 0o640);
  assert.throws(() => assertConfigPermissions(file), /0600/);
});

test("assertBrokerScript: pins the basename to broker.py through symlinks", () => {
  const dir = scratch();
  const real = stubBroker(dir);
  assert.equal(assertBrokerScript(real), real);
  const wrong = path.join(dir, "something-else.py");
  writeFileSync(wrong, "#!/usr/bin/env python3\n");
  assert.throws(() => assertBrokerScript(wrong), /broker\.py/);
  assert.throws(() => assertBrokerScript(path.join(dir, "missing.py")), /does not exist/);
});

// --- 2. bearer -------------------------------------------------------------

test("bearer: matches the digest, rejects wrong and empty", () => {
  assert.equal(bearerMatches(BEARER, BEARER_SHA), true);
  assert.equal(bearerMatches("wrong-bearer", BEARER_SHA), false);
  assert.equal(bearerMatches("", BEARER_SHA), false);
  assert.equal(bearerMatches(null, BEARER_SHA), false);
});

// --- 3. identity headers ---------------------------------------------------

test("identity: accepts the full stamped set", () => {
  const identity = readIdentityHeaders(identityHeaders(), configFor(scratch()));
  assert.equal(identity.agentId, CALLER);
  assert.equal(identity.companyId, COMPANY);
  assert.equal(identity.runId, RUN);
  assert.equal(identity.issueId, ISSUE);
});

test("identity: missing agent header fails closed with the forward-list remedy", () => {
  const headers = identityHeaders({ "x-paperclip-agent-id": undefined });
  delete headers["x-paperclip-agent-id"];
  const error = throwsHttp(() => readIdentityHeaders(headers, configFor(scratch())));
  assert.equal(error.code, "identity_header_missing");
  assert.match(error.message, /headerPolicy/);
});

test("identity: malformed agent, company and run headers are refused", () => {
  const dir = scratch();
  const cfg = configFor(dir);
  for (const [key, value] of [
    ["x-paperclip-agent-id", "not-a-uuid"],
    ["x-paperclip-company-id", "not-a-uuid"],
    ["x-paperclip-run-id", "not-a-uuid"],
  ]) {
    const headers = identityHeaders({ [key]: value });
    throwsHttp(() => readIdentityHeaders(headers, cfg));
  }
  const missingRun = identityHeaders();
  delete missingRun["x-paperclip-run-id"];
  throwsHttp(() => readIdentityHeaders(missingRun, cfg));
});

test("identity: another company's gateway cannot use this transport", () => {
  const headers = identityHeaders({ "x-paperclip-company-id": OTHER_COMPANY });
  const error = throwsHttp(() => readIdentityHeaders(headers, configFor(scratch())));
  assert.equal(error.code, "wrong_company");
});

test("identity: missing issue header fails closed — the broker's requester contract needs it", () => {
  const headers = identityHeaders();
  delete headers["x-paperclip-issue-id"];
  const error = throwsHttp(() => readIdentityHeaders(headers, configFor(scratch())));
  assert.equal(error.code, "issue_header_missing");
  assert.match(error.message, /issue_id/);
});

test("identity: malformed issue header is refused", () => {
  const headers = identityHeaders({ "x-paperclip-issue-id": "not-a-uuid" });
  throwsHttp(() => readIdentityHeaders(headers, configFor(scratch())));
});

// --- 4. tools surface ------------------------------------------------------

test("surface: exactly four read-only tools, and the allowlist covers them", () => {
  assert.deepEqual(TOOLS.map((t) => t.name).sort(), [
    "inspect_health",
    "inspect_service",
    "locate_tooling",
    "read_logs",
  ]);
  assert.deepEqual([...ALLOWLISTED_VERBS].sort(), ["health.inspect", "logs.read", "service.inspect", "tooling.locate"]);
  for (const tool of TOOLS) {
    assert.ok(ALLOWLISTED_VERBS.has(tool.verb), `${tool.name} maps to an allowlisted verb`);
    assert.deepEqual(Object.keys(tool.inputSchema.properties), [],
      `${tool.name} takes no arguments`);
    assert.equal(tool.inputSchema.additionalProperties, false);
  }
});

// --- 5. argument guards ----------------------------------------------------

test("args: any argument at all is refused — these tools take none", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  const error = throwsHttp(
    () => buildBrokerArgs("inspect_health", { verbose: true }, identity));
  assert.equal(error.code, "unknown_argument");
});

test("args: an identity-shaped argument is refused LOUDLY, naming the rule", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  for (const bad of ["requester", "agentId", "issueId", "run_id", "companyId", "principal"]) {
    const error = throwsHttp(
      () => buildBrokerArgs("inspect_service", { [bad]: CALLER }, identity));
    assert.equal(error.code, "identity_argument_refused", `${bad} must hit the specific control`);
  }
});

test("args: unknown verb never reaches an argv — refused before exec", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  const error = throwsHttp(
    () => buildBrokerArgs("do_something_else", {}, identity));
  assert.equal(error.code, "unknown_tool");
});

// --- 6. argv shape ---------------------------------------------------------

test("argv: request carries the verb and the AUTHENTICATED principal, nothing else", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  const expected = {
    inspect_health: "health.inspect",
    inspect_service: "service.inspect",
    locate_tooling: "tooling.locate",
    read_logs: "logs.read",
  };
  for (const [tool, verb] of Object.entries(expected)) {
    const argv = buildBrokerArgs(tool, {}, identity);
    assert.ok(argv.includes("--verb"), tool);
    assert.equal(argv[argv.indexOf("--verb") + 1], verb, tool);
    assert.equal(argv[argv.indexOf("--agent-id") + 1], CALLER, tool);
    assert.equal(argv[argv.indexOf("--run-id") + 1], RUN, tool);
    assert.equal(argv[argv.indexOf("--issue-id") + 1], ISSUE, tool);
    assert.ok(!argv.some((a) => a.includes("paperclip.deploy") || a.includes("restart")), tool);
  }
});

test("argv: a distinct request id per call — request ids are single-use", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  const first = buildBrokerArgs("inspect_health", {}, identity);
  const second = buildBrokerArgs("inspect_health", {}, identity);
  const idOf = (argv) => argv[argv.indexOf("--request-id") + 1];
  assert.notEqual(idOf(first), idOf(second));
  assert.match(idOf(first), /^HRQ-[A-Za-z0-9][A-Za-z0-9._-]{5,95}$/);
});

// --- 7. HTTP behaviour -----------------------------------------------------

test("http: tools/list is anonymous — no identity required", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    const res = await post(port,
      { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
      { authorization: `Bearer ${BEARER}` });
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.deepEqual(payload.result.tools.map((t) => t.name).sort(), [
      "inspect_health", "inspect_service", "locate_tooling", "read_logs",
    ]);
  } finally {
    server.close();
  }
});

test("http: tools/call without a bearer is 401 with NO WWW-Authenticate", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    const res = await post(port, rpcCall("inspect_health"));
    assert.equal(res.status, 401);
    assert.ok(!("www-authenticate" in res.headers), "no WWW-Authenticate — the gateway would chase OAuth discovery");
  } finally {
    server.close();
  }
});

test("http: tools/call without identity headers fails closed at HTTP level", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    const res = await post(port, rpcCall("inspect_health"),
      { authorization: `Bearer ${BEARER}` });
    assert.equal(res.status, 403);
    assert.match(JSON.parse(res.body).error.message, /x-paperclip-agent-id/);
  } finally {
    server.close();
  }
});

test("http: there is no tool that runs a write — paperclip.deploy is unknown", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    const res = await post(port, rpcCall("paperclip.deploy"),
      identityHeaders());
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.equal(payload.result.isError, true);
    assert.match(payload.result.content[0].text, /unknown tool 'paperclip\.deploy'/);
    assert.match(payload.result.content[0].text, /inspect_health, inspect_service, locate_tooling/);
  } finally {
    server.close();
  }
});

// case_802335d7: the disabled staging-app specs (coolify.app.deploy,
// coolify.app.restart) and the Phase-1 env verb (coolify.env.set) must not
// gain an executable transport path: every name refuses as unknown_tool,
// and the advertised surface stays exactly the four read-only tools.
test("http: case_802335d7 staging-app verbs have no transport tool", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    for (const name of ["coolify.app.deploy", "coolify.app.restart", "coolify.env.set", "script.run"]) {
      const res = await post(port, rpcCall(name), identityHeaders());
      assert.equal(res.status, 200, name);
      const payload = JSON.parse(res.body);
      assert.equal(payload.result.isError, true, name);
      assert.match(payload.result.content[0].text, new RegExp(`unknown tool '${name.replace(/\./g, "\\.")}'`), name);
    }
    const list = await post(port,
      { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
      { authorization: `Bearer ${BEARER}` });
    assert.deepEqual(JSON.parse(list.body).result.tools.map((t) => t.name).sort(), [
      "inspect_health", "inspect_service", "locate_tooling", "read_logs",
    ]);
  } finally {
    server.close();
  }
});

test("http: identity argument over the wire is refused as a tool error", async () => {
  const dir = scratch();
  const server = createServer(configFor(dir));
  const port = await listen(server);
  try {
    const res = await post(port, rpcCall("inspect_service", { requester: CALLER }),
      identityHeaders());
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.equal(payload.result.isError, true);
    assert.match(payload.result.content[0].text, /identity_argument_refused/);
  } finally {
    server.close();
  }
});

// --- 8. run corroboration ---------------------------------------------------
// case_81816394: node7b1ff338 is Docker-only, so the engine the corroborator execs is
// config, not source. The node7b1ff338 install pins "docker"; unset keeps the
// historical default.
test("corroboration: configured engine wins, then env, then the default", async () => {
  const dir = scratch();
  const seen = [];
  const cfg = configFor(dir, { requireLiveRun: true, containerEngine: "docker" });
  const corroborate = makeRunCorroborator(cfg, async (...args) => {
    seen.push(args);
    return { stdout: `1|${ISSUE}`, stderr: "" };
  });
  await corroborate({ agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE });
  assert.equal(seen[0][0], "docker");
  assert.deepEqual(seen[0][1].slice(0, 3), ["exec", "-i", "-e"]);
  assert.ok(!seen[0][1].includes("-u"),
    "default-user exec carries no -u flag — paperclip-db has no node user");
  _resetRunCheckCache();
  // Unset stays unset through normalization so the environment still wins
  // over the default; the "podman" historical default applies at exec time.
  const saved = process.env.CONTAINER_ENGINE;
  try {
    process.env.CONTAINER_ENGINE = "docker";
    assert.equal(containerEngineFor(configFor(dir, { containerEngine: undefined })), "docker");
    delete process.env.CONTAINER_ENGINE;
    assert.equal(containerEngineFor(configFor(dir, { containerEngine: undefined })), "podman");
  } finally {
    if (saved === undefined) delete process.env.CONTAINER_ENGINE;
    else process.env.CONTAINER_ENGINE = saved;
  }
});

test("corroboration: disabled requireLiveRun corroborates nothing", async () => {
  _resetRunCheckCache();
  const dir = scratch();
  const cfg = configFor(dir, { requireLiveRun: false });
  const corroborate = makeRunCorroborator(cfg, () => {
    throw new Error("must not exec when disabled");
  });
  await corroborate({ agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE });
});

test("corroboration: an unreachable database fails CLOSED, never open", async () => {
  _resetRunCheckCache();
  const dir = scratch();
  const cfg = configFor(dir, { requireLiveRun: true });
  const corroborate = makeRunCorroborator(cfg, async () => {
    const error = new Error("podman exec failed");
    error.code = 125;
    throw error;
  });
  const error = await corroborate(
    { agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE }).catch((e) => e);
  assert.ok(error instanceof HttpError);
  assert.equal(error.status, 503);
  assert.equal(error.code, "run_check_unavailable");
});

test("corroboration: a non-live run is refused", async () => {
  _resetRunCheckCache();
  const dir = scratch();
  const cfg = configFor(dir, { requireLiveRun: true });
  const corroborate = makeRunCorroborator(cfg, async () => ({ stdout: "", stderr: "" }));
  const error = await corroborate(
    { agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE }).catch((e) => e);
  assert.ok(error instanceof HttpError);
  assert.equal(error.code, "run_not_live");
});

test("corroboration: the stamped issue must match the run's snapshot issue", async () => {
  _resetRunCheckCache();
  const dir = scratch();
  const cfg = configFor(dir, { requireLiveRun: true });
  // Run row exists and is live, but on ANOTHER issue. The stub speaks the
  // real psql -F"|" dialect: `1|<snapshot-issue-uuid>`.
  const corroborate = makeRunCorroborator(cfg, async () => ({
    stdout: `1|${OTHER_COMPANY}`,
    stderr: "",
  }));
  const error = await corroborate(
    { agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE }).catch((e) => e);
  assert.ok(error instanceof HttpError);
  assert.equal(error.code, "run_issue_mismatch");
});

test("corroboration: live run on the stamped issue passes", async () => {
  _resetRunCheckCache();
  const dir = scratch();
  const cfg = configFor(dir, { requireLiveRun: true });
  const corroborate = makeRunCorroborator(cfg, async () => ({
    stdout: `1|${ISSUE}`,
    stderr: "",
  }));
  await corroborate({ agentId: CALLER, runId: RUN, companyId: COMPANY, issueId: ISSUE });
});

// --- 9. real broker end to end ----------------------------------------------
//
// The REAL host-ops/broker.py in a scratch spool: generated RSA signing key,
// fake executor facts (HOST_OPS_TEST_MODE=1), service.inspect. The transport
// must return the broker's verified result with its audit linkage.
//
// These tests do NOT skip when broker.py is absent — the CI mutation harness
// stages host-ops/broker.py next to mcp/, so the full suite judges every
// mutation. If the relative path below ever resolves nowhere, that is a
// hard failure, not a skip: a skipped assertion cannot reject a mutation.

const REAL_BROKER = fileURLToPath(new URL("../../host-ops/broker.py", import.meta.url));

function brokerAvailable() {
  return existsSync(REAL_BROKER);
}

function makeBrokerWorld() {
  assert.ok(brokerAvailable(), `real broker.py missing at ${REAL_BROKER} — refusing to skip`);
  const dir = scratch();
  const spool = path.join(dir, "spool");
  const state = path.join(dir, "state");
  const audit = path.join(dir, "audit.jsonl");
  const keys = path.join(dir, "keys");
  mkdirSync(keys, { recursive: true, mode: 0o700 });
  const privateKey = path.join(keys, "response-signing.pem");
  const publicKey = path.join(keys, "response-signing.pub.pem");
  execFileSync("openssl", ["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", privateKey]);
  chmodSync(privateKey, 0o600);
  execFileSync("openssl", ["pkey", "-in", privateKey, "-pubout", "-out", publicKey]);
  const publicKeySha256 = createHash("sha256").update(readFileSync(publicKey)).digest("hex");
  const facts = path.join(dir, "facts");
  mkdirSync(facts);
  // node7b1ff338 Docker shape (verified 2026-10-05): the fixed subset of
  // `docker inspect --format '{{json .State}}'` broker.py exports. The full
  // eight-key set matches EXPECTED_RESULT_KEYS in test_host_ops_broker.py.
  writeFileSync(path.join(facts, "service.json"), JSON.stringify({
    Status: "running",
    Running: true,
    Pid: 1234,
    StartedAt: "2026-10-04T22:00:00Z",
    FinishedAt: "0001-01-01T00:00:00Z",
    ExitCode: 0,
    OOMKilled: false,
    Restarting: false,
  }));
  const brokerConfigPath = path.join(dir, "broker-config.json");
  // Mirrors host-ops/config.example.json: paperclipImageRef is REQUIRED since
  // the sudoers-mirror binding (testMode skips the mirror comparison, so any
  // well-formed reference works here).
  writeFileSync(brokerConfigPath, JSON.stringify({
    version: 1,
    spoolPath: spool,
    statePath: state,
    auditPath: audit,
    privateKeyPath: privateKey,
    healthUrl: "http://127.0.0.1:3100/api/health",
    paperclipDataPath: dir,
    paperclipImageRef: "localhost/paperclip-local:test",
    opsToolingPath: dir,
    toolingSearchRoots: [dir],
    toolingMarkers: ["marker-a.txt", "marker-b.txt"],
    pollSeconds: 5,
    testMode: true,
  }));
  return { dir, spool, state, audit, privateKey, publicKey, publicKeySha256, facts, brokerConfigPath };
}

function transportConfigForWorld(world, overrides = {}) {
  return normalizeConfig({
    companyId: COMPANY,
    bearerSha256: BEARER_SHA,
    brokerScript: REAL_BROKER,
    brokerSpool: world.spool,
    brokerConfig: world.brokerConfigPath,
    publicKeyPath: world.publicKey,
    expectedPublicKeySha256: world.publicKeySha256,
    requireLiveRun: false,
    responseWaitMs: 30000,
    responsePollMs: 200,
    brokerEnv: {
      HOST_OPS_TEST_MODE: "1",
      HOST_OPS_FAKE_FACTS_DIR: world.facts,
    },
    ...overrides,
  });
}

// In production the broker SERVICE (systemd, polling every pollSeconds) picks
// the request up; there is no service in this test, so the test plays it by
// running the broker's own `run-once` — the exact code path the service loop
// calls — once the request file lands. The transport itself never processes;
// it only submits and waits, which is what this exercises.
async function pumpBrokerOnce(world) {
  execFileSync("python3", [REAL_BROKER, "run-once", "--config", world.brokerConfigPath], {
    env: {
      ...process.env,
      HOST_OPS_TEST_MODE: "1",
      HOST_OPS_FAKE_FACTS_DIR: world.facts,
    },
  });
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Async, never execFileSync: the test thread shares its event loop with the
// transport server under test, so a blocking sleep would starve the very POST
// this is waiting for — a self-inflicted deadlock that reads as a timeout.
async function waitForRequestFile(world, timeoutMs = 10000) {
  const dir = path.join(world.spool, "requests");
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const entries = readdirSync(dir).filter((e) => e.endsWith(".json"));
      if (entries.length > 0) return entries[0];
    } catch { /* spool dir not yet created — keep waiting */ }
    if (Date.now() >= deadline) throw new Error("timed out waiting for the broker request file");
    await sleepMs(100);
  }
}

test("broker: service.inspect end to end returns a verified signed result", async () => {
  const world = makeBrokerWorld();
  const cfg = transportConfigForWorld(world);
  const server = createServer(cfg);
  const port = await listen(server);
  try {
    const pending = post(port, rpcCall("inspect_service"), identityHeaders());
    await waitForRequestFile(world);
    pumpBrokerOnce(world);
    const res = await pending;
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.equal(payload.result.isError, false, payload.result.content[0].text.slice(0, 500));
    const bundle = JSON.parse(payload.result.content[0].text);
    assert.equal(bundle.response.status, "succeeded");
    assert.equal(bundle.response.verb, "service.inspect");
    assert.equal(bundle.response.result.Status, "running");
    assert.equal(bundle.response.result.Pid, 1234);
    assert.ok(bundle.response.audit.sequence >= 1);
    assert.match(bundle.response.audit.entryHash, /^[0-9a-f]{64}$/);
    assert.equal(bundle.bundle, bundle.response.requestId + "." + bundle.response.canonicalHash.slice(0, 12));
    // The request is visible in the host audit chain.
    const auditLines = readFileSync(world.audit, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    const accepted = auditLines.find((e) => e.event === "request.accepted");
    assert.ok(accepted, "audit chain contains request.accepted");
    assert.equal(accepted.details.requester.agentId, CALLER);
    assert.equal(accepted.details.requester.issueId, ISSUE);
  } finally {
    server.close();
  }
});

test("args: log unit, window, cap and identity cannot be caller-selected", () => {
  const identity = { agentId: CALLER, runId: RUN, issueId: ISSUE };
  for (const args of [{ unit: "ssh.service" }, { since: "-1day" }, { lines: 201 }, { path: "/etc/shadow" }]) {
    assert.equal(throwsHttp(() => buildBrokerArgs("read_logs", args, identity)).code, "unknown_argument");
  }
  assert.equal(throwsHttp(() => buildBrokerArgs("read_logs", { agentId: CALLER }, identity)).code,
    "identity_argument_refused");
});

test("broker: logs.read returns a verified bounded redacted tail", async () => {
  const world = makeBrokerWorld();
  // Raw `docker logs --timestamps` line, like the broker seam carries: the
  // executor parses and redacts it, so the secret never reaches the bundle.
  writeFileSync(path.join(world.facts, "logs.json"), JSON.stringify([
    "2026-10-04T23:00:00.123456789Z Bearer fixture-log-secret",
  ]));
  const server = createServer(transportConfigForWorld(world));
  const port = await listen(server);
  try {
    const pending = post(port, rpcCall("read_logs"), identityHeaders());
    await waitForRequestFile(world);
    await pumpBrokerOnce(world);
    const res = await pending;
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.equal(payload.result.isError, false, payload.result.content[0].text.slice(0, 500));
    const bundle = JSON.parse(payload.result.content[0].text);
    assert.equal(bundle.response.status, "succeeded");
    assert.equal(bundle.response.verb, "logs.read");
    assert.deepEqual(bundle.response.result, {
      unit: "paperclip", windowSeconds: 900, lineLimit: 200, redaction: "all-free-text",
      entries: [{ timestamp: "1791154800123456", priority: 6, message: "[REDACTED]" }],
    });
    assert.ok(Buffer.byteLength(JSON.stringify(bundle.response)) <= 65536);
    assert.ok(!res.body.includes("fixture-log-secret"));
    assert.ok(!readFileSync(world.audit, "utf8").includes("fixture-log-secret"));
    assert.ok(bundle.response.audit.sequence >= 1);
  } finally {
    server.close();
  }
});

test("broker: a tampered signature is refused before any byte reaches the caller", async () => {
  const world = makeBrokerWorld();
  const cfg = transportConfigForWorld(world);
  // First, a real submit + a real broker processing pass to produce a bundle.
  const bundleBase = await (async () => {
    const runBroker = makeBrokerRunner(cfg);
    const argv = buildBrokerArgs("inspect_service", {}, {
      agentId: CALLER, runId: RUN, issueId: ISSUE,
    });
    const first = await runBroker(argv);
    assert.equal(first.ok, true, first.text.slice(0, 300));
    pumpBrokerOnce(world);
    return first.bundleBase;
  })();
  // Tamper with the signature in place.
  const bundleDir = path.join(world.spool, "responses", bundleBase + ".bundle");
  const entries = readdirSync(bundleDir);
  const sigFile = entries.find((e) => e.endsWith(".sig"));
  assert.ok(sigFile, "bundle carries a signature file");
  const sigPath = path.join(bundleDir, sigFile);
  const sig = readFileSync(sigPath);
  const tampered = Buffer.from(sig);
  tampered[tampered.length - 1] ^= 0xff;
  writeFileSync(sigPath, tampered);
  // Now route a call at the tampered bundle through a stubbed runner.
  const tamperedHandler = createHandler(cfg, {
    corroborateRun: async () => {},
    runBroker: async () => ({ ok: true, text: "", exitCode: 0, request: { requestId: "HRQ-x" }, bundleBase }),
  });
  const req = { method: "POST", headers: identityHeaders() };
  const { status, payload } = await tamperedHandler(
    req, JSON.stringify(rpcCall("inspect_service")));
  assert.equal(status, 200);
  assert.equal(payload.result.isError, true);
  assert.match(payload.result.content[0].text, /invalid_signature/);
});

test("verifier: a key file that does not match the pin is refused BEFORE openssl runs", async () => {
  // The pin is this transport's own control, independent of broker.py's
  // --expected-key-sha256 check. Prove the refusal comes from HERE by
  // stubbing exec to SUCCEED: if verifyBundle skipped its pin check, the
  // stubbed openssl would "verify" anything and this test would see a payload.
  const dir = scratch();
  const cfg = configFor(dir, { expectedPublicKeySha256: "b".repeat(64) });
  let execCalled = false;
  const verifyBundle = makeBundleVerifier(cfg, async () => {
    execCalled = true;
    return { stdout: JSON.stringify({ status: "succeeded" }), stderr: "" };
  });
  const error = await verifyBundle({
    bundleDir: dir, jsonPath: path.join(dir, "x.json"), sigPath: path.join(dir, "x.sig"),
  }).catch((e) => e);
  assert.ok(error instanceof HttpError);
  assert.equal(error.code, "signing_key_mismatch");
  assert.equal(execCalled, false, "openssl must not run against an unpinned key");
});

test("broker: no signed bundle within the wait is a timeout, not a hang", async () => {
  const dir = scratch();
  const cfg = configFor(dir, { responseWaitMs: 1200, responsePollMs: 100 });
  const handler = createHandler(cfg, {
    corroborateRun: async () => {},
    runBroker: async () => ({
      ok: true, text: "", exitCode: 0,
      request: { requestId: "HRQ-never" }, bundleBase: "HRQ-never.deadbeefcafe",
    }),
  });
  const req = { method: "POST", headers: identityHeaders() };
  const started = Date.now();
  const { payload } = await handler(req, JSON.stringify(rpcCall("inspect_health")));
  assert.ok(Date.now() - started < 15000, "bounded wait");
  assert.equal(payload.result.isError, true);
  assert.match(payload.result.content[0].text, /broker_response_timeout/);
});

// ===========================================================================
// 10. Discovery matches what the transport can actually run (case_50df2efc —
// case_31e66fd9 D1c, audit priority 1: verification for the upstream gateway fix
// prepared in case_35cc7d69, "advertised-but-denied tools" half).
//
// This is the host-ops twin of the case_b22b58a0 block in org-request-mcp.test.mjs.
// The defect class is the same: tools/list advertises a tool the transport
// will never run, so an agent spends its attempt on a call that cannot
// succeed. Here the surface is fixed (three Phase-0 read verbs, no optional
// script binding), which makes the invariant statable in both directions:
//
//   a tool is advertised  <=>  a tool is dispatchable
//
// The wire half goes through the real HTTP server: for every advertised name,
// tools/call with valid (empty) arguments must NOT come back unknown_tool.
// The runner is stubbed to succeed so the assertion stops at dispatch — what
// broker.py would answer is the broker suite's business, not this one's.
// ===========================================================================

test("case_50df2efc: every advertised tool dispatches — none is advertised-but-denied", async () => {
  const dir = scratch();
  const cfg = configFor(dir);
  const handler = createHandler(cfg, {
    corroborateRun: async () => {},
    runBroker: async () => ({
      ok: true, text: "", exitCode: 0,
      request: { requestId: "HRQ-advertised" }, bundleBase: "HRQ-advertised.deadbeefcafe",
    }),
    waitForBundle: async () => ({ bundleDir: ".", jsonPath: "x.json", sigPath: "x.sig" }),
    verifyBundle: async () => ({ payload: { status: "succeeded" }, bundleBase: "HRQ-advertised" }),
  });
  for (const tool of advertisedTools()) {
    const req = { method: "POST", headers: identityHeaders() };
    const { status, payload } = await handler(req, JSON.stringify(rpcCall(tool.name, {})));
    assert.equal(status, 200, `${tool.name}: dispatch must reach the runner, not the HTTP layer`);
    assert.doesNotMatch(
      payload.result.content[0].text, /unknown_tool/,
      `${tool.name} is advertised by tools/list and must therefore be dispatchable`,
    );
  }
});

test("case_50df2efc: the dispatchable set and the advertised set are the same set", async () => {
  // The unit-level half of the invariant: TOOLS_BY_NAME (what tools/call can
  // reach) and advertisedTools() (what tools/list offers) are derived from
  // the same TOOLS array, so a tool added to one surface without the other
  // fails here rather than dangling in front of an agent. The wire half
  // (tools/list names exactly these) is pinned by the anonymous-list test
  // in section 7; this pins the dispatch half.
  const dir = scratch();
  const cfg = configFor(dir);
  const handler = createHandler(cfg, {
    corroborateRun: async () => {},
    runBroker: async () => ({
      ok: true, text: "", exitCode: 0,
      request: { requestId: "HRQ-dispatched" }, bundleBase: "HRQ-dispatched.deadbeefcafe",
    }),
    waitForBundle: async () => ({ bundleDir: ".", jsonPath: "x.json", sigPath: "x.sig" }),
    verifyBundle: async () => ({ payload: { status: "succeeded" }, bundleBase: "HRQ-dispatched" }),
  });
  for (const tool of TOOLS) {
    // buildBrokerArgs throws unknown_tool for anything outside TOOLS_BY_NAME —
    // every TOOLS entry must survive it with valid (empty) arguments.
    const argv = buildBrokerArgs(tool.name, {}, { agentId: CALLER, runId: RUN, issueId: ISSUE });
    assert.ok(argv.includes(tool.verb), `${tool.name} builds an argv for its own verb`);
    const req = { method: "POST", headers: identityHeaders() };
    const { payload } = await handler(req, JSON.stringify(rpcCall(tool.name, {})));
    assert.doesNotMatch(
      payload.result.content[0].text, /unknown_tool/,
      `${tool.name} is in TOOLS and must be callable, not merely listed`,
    );
  }
  assert.deepEqual(
    advertisedTools().map((t) => t.name).sort(),
    TOOLS.map((t) => t.name).sort(),
    "advertisedTools() and TOOLS must name the same tools",
  );
});
