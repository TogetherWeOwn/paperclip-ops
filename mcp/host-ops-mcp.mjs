#!/usr/bin/env node
// ===========================================================================
// host-ops-mcp — the host-side MCP transport for host-ops/broker.py
// case_16723128.
//
// WHAT THIS IS
//   A thin HTTP MCP server that runs ON THE HOST as the operator user and
//   forwards a fixed, enumerated set of READ-ONLY tool calls into the
//   Paperclip host-ops broker (`host-ops/broker.py request`), waits for the
//   broker to publish its signed response bundle, verifies the bundle against
//   the independently pinned response-signing key, and returns the verified
//   payload to the caller. It exists because the broker's spool is a
//   filesystem path agents cannot reach: Paperclip's agent spawner resets
//   supplementary groups, so the gid-986 spool mount is a dead end from any
//   agent process (see case_16723128). A `local_stdio` MCP server would be spawned
//   from the Paperclip server process INSIDE the container and has no host
//   access either — hence `mcp_remote`, behind the established Caddy pattern,
//   exactly as org-request-mcp (mcp/org-request-mcp.mjs) already does for the
//   provisioning queue. See docs/transport-identity.md for the verification
//   behind that.
//
// WHAT THIS IS NOT
//   It is NOT an authorization boundary and NOT a second broker. Which verbs
//   may run, what they may read, request shape, replay protection, the audit
//   chain and the signatures all live in broker.py, and none of it is
//   duplicated here. If you are about to add a rule to this file that decides
//   whether a caller MAY do something, stop: it belongs in the broker, and
//   two copies of an authorization rule drift.
//
//   The distinction this file does draw, and the only one it draws:
//     * AUTHENTICATION  — who is calling. Established here, from headers the
//                         control plane stamps and the caller cannot forge,
//                         corroborated against heartbeat_runs.
//     * AUTHORIZATION   — what they may do. Established in broker.py.
//   Everything below is authentication, tenancy, transport allowlisting (the
//   Phase-0 verb set case_16723128 orders this transport to refuse anything
//   outside), and signature verification before a byte reaches the caller.
//
// THE IDENTITY CHAIN, END TO END
//   agent (model)
//     └─ cannot influence any of the following ───────────────────────────┐
//   Paperclip tool gateway (in container)                                 │
//     stamps x-paperclip-agent-id / -company-id / -run-id / -issue-id from │
//     a database row in tool_gateway_sessions. Caller-supplied             │
//     x-paperclip-* headers are classified sensitive and dropped before    │
//     dispatch, and the stamped values are written last regardless.       ─┘
//     (tool-gateway.ts: buildRemoteHeaders ← metadataHeadersForSession;
//     the session's issueId is resolved server-side from the run's
//     contextSnapshot by resolveRunContext — the caller cannot choose it.)
//     └─ https, Bearer credential resolved from Paperclip's secret store
//   Caddy on the host (TLS, bearer check, source restriction)
//     └─ 127.0.0.1
//   THIS SERVER
//     verifies the bearer again, requires the identity headers, and
//     corroborates (agent, run, company) against heartbeat_runs — AND that
//     the stamped issue matches the run's snapshot issue — before it will
//     run anything. See requireIdentity() and makeRunCorroborator().
//     └─ execFile, argv array, no shell
//   host-ops/broker.py request|verify  ← every authorization decision here
//
// WHY THE ISSUE IS PART OF IDENTITY HERE
//   The broker's requester contract REQUIRES all three of agentId, runId and
//   issueId (broker.py REQUESTER_KEYS), each a UUID, and records them in the
//   audit chain as attribution. A transport that let the model name the issue
//   would let any caller attribute host reads to any card. So the issue id is
//   taken from the gateway-stamped x-paperclip-issue-id header — which the
//   gateway derives from the run snapshot, never from caller input — and the
//   corroboration query checks the database agrees this run is on that issue.
//   A missing issue header is a hard 403, never a fallback and never an
//   anonymous submit.
//
// FAIL-CLOSED, AND WHY IT MATTERS MOST HERE
//   The gateway forwards metadata headers only when the connection opts in via
//   config.headerPolicy.metadata.forward. THE DEFAULT IS THE EMPTY LIST. A
//   connection registered the "obvious" way — url and nothing else — sends NO
//   identity header, with no error and no health degradation. That is silent
//   anonymity, which is the exact failure this transport exists to prevent.
//   So a missing header here is a hard 403 on tools/call, never a fallback
//   and never an anonymous request. If the connection is misconfigured, this
//   server is loud about it on the first call. The forward list MUST include
//   "issue_id" as well as "agent_id", "company_id" and "run_id" — without it
//   every call fails closed on the missing issue header.
//
// WHY tools/list IS ANONYMOUS AND tools/call IS NOT
//   The gateway's catalog refresh / health check (tool-access.ts remoteTools)
//   POSTs tools/list with CREDENTIAL HEADERS ONLY — no session, so no identity
//   headers. Requiring identity on tools/list would make the connection
//   permanently unhealthy and unregisterable. Discovery is public-with-bearer;
//   execution requires a principal. That asymmetry is deliberate, and it is
//   safe because tools/list reveals only the tool SCHEMAS. No request, no
//   broker call and no signed bundle is reachable without a corroborated
//   principal.
//
// NO SECRET IN argv (/proc/*/cmdline is world-readable on a shared host) and
// no secret in the config file either: the config stores the SHA-256 of the
// bearer, never the bearer. A host-file read does not yield a usable token.
// The response-signing PUBLIC key and its hash pin are verifiers, not secrets,
// and are safe to store.
//
// Zero dependencies, deliberately: this runs as the operator user with
// container-engine access (docker on node7b1ff338), and an npm tree next to that is a
// supply-chain surface we are not taking on for an HTTP server this small.
// ===========================================================================

import http from "node:http";
import { execFile } from "node:child_process";
import { createHash, timingSafeEqual, randomUUID } from "node:crypto";
import { readFileSync, appendFileSync, statSync, realpathSync, readdirSync } from "node:fs";
import path from "node:path";

const SERVER_NAME = "host-ops-mcp";
const SERVER_VERSION = "1.0.0";
const PROTOCOL_VERSION = "2025-06-18";
const SUPPORTED_PROTOCOL_VERSIONS = new Set(["2025-06-18", "2025-03-26", "2024-11-05"]);

// The bind address is NOT configurable. "Bind loopback only" is a stated
// non-negotiable, and a constraint that lives in a config file is a
// constraint one edit away from being gone. Caddy is the only thing that
// listens publicly; this process cannot be made to, short of editing source.
const BIND_ADDRESS = "127.0.0.1";

const MAX_BODY_BYTES = 256 * 1024;
const MAX_CONCURRENT_REQUESTS = 16;
const CLI_TIMEOUT_MS = 120_000;
const CLI_MAX_BUFFER = 4 * 1024 * 1024;
const RUN_CHECK_TIMEOUT_MS = 15_000;
// A short cache, not a long one. The gateway re-checks the run on every call
// anyway; this only stops a burst from becoming one container-exec per request.
const RUN_CHECK_TTL_MS = 10_000;
const RUN_CHECK_CACHE_MAX = 512;

// The container binary this host answers to. The node7b1ff338 install pins "docker"
// in config, so production corroboration never depends on ambient PATH;
// config wins, then the environment, then the historical default.
export function containerEngineFor(cfg) {
  return cfg.containerEngine || process.env.CONTAINER_ENGINE || "podman";
}

// Environment names this server supplies to broker.py itself. `brokerEnv` is
// an operator seam for the broker's own test hooks (HOST_OPS_TEST_MODE,
// HOST_OPS_FAKE_FACTS_DIR); it is not a way to restate any of these. See
// normalizeConfig and makeBrokerRunner — both refuse, in that order, because
// the second is what still holds if someone builds a cfg object by hand.
const RESERVED_BROKER_ENV = new Set(["PATH", "HOME", "PAPERCLIP_DB_CTR"]);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// broker.py REQUEST_ID_RE: ^HRQ-[A-Za-z0-9][A-Za-z0-9._-]{5,95}$
const BROKER_REQUEST_ID_RE = /^HRQ-[A-Za-z0-9][A-Za-z0-9._-]{5,95}$/;
const CANONICAL_HASH_RE = /^[0-9a-f]{64}$/;

// ---------------------------------------------------------------------------
// The Phase-0 transport allowlist (case_16723128).
//
// broker.py recognises a wider fixed READ_VERBS table, but THIS CARD narrows
// the transport to the original three verbs plus the CISO-reviewed logs.read
// (case_00a30085/case_5f163b24). Enabling anything else — including the other
// broker read verbs, and any write verb — is a separate, owner-approved
// change. A verb not on this list is refused here and never reaches broker.py
// (which would refuse an unknown verb anyway — defence in depth, not reliance).
//
// case_5248bcfa adds owner-approved write tiers (reason/approval/drainReceipt) to
// broker.py's own schema. This transport builds no write-tier request today —
// there is no verb argument, no reason argument, and no approval argument on
// any tool below — so it has nothing to authorize and nothing to change here.
// Wiring a write tier through this transport is the same "separate,
// owner-approved change" as enabling any other write verb above.
//
// case_802335d7 adds disabled coolify.app.deploy/coolify.app.restart specs to
// broker.py with no executor. They are intentionally unexposed here: no
// deploy/restart/env tool exists below, and adding one needs its own
// CISO privileged-boundary review plus activation gate, not a transport edit.
// ---------------------------------------------------------------------------
export const ALLOWLISTED_VERBS = new Set([
  "health.inspect",
  "logs.read",
  "service.inspect",
  "tooling.locate",
]);

// ---------------------------------------------------------------------------
// Identity is never a tool argument.
//
// The gateway does not sanitise tool arguments and SHOULD not — arguments are
// tool data, and a `requester` field would arrive here verbatim, exactly as the
// model wrote it. So the schemas below have no identity field, and this list
// makes the refusal LOUD rather than silent: an argument named `requester` is
// an error naming the rule, not a key quietly dropped. A silent drop would let
// a caller believe it had acted as someone else right up until it read the
// audit log.
//
// TWO CONTROLS, AND THE ORDER BETWEEN THEM MATTERS
//   The general control is assertKnownArguments(): any key absent from the
//   tool's own inputSchema.properties is refused. That is what
//   `additionalProperties: false` advertises to clients, and it is enforced
//   here because NOTHING ELSE ENFORCES IT — there is no JSON-Schema validator
//   in this process, and buildBrokerArgs reads named arguments explicitly, so
//   an unlisted key would otherwise be dropped in silence.
//
//   This list is the SPECIFIC control, checked first so the message names the
//   actual rule. `requester` and `verb_typo` are both unknown keys, but only
//   one of them is someone trying to be somebody else, and the operator
//   reading the audit log should be able to tell them apart at a glance.
// ---------------------------------------------------------------------------
const FORBIDDEN_ARGUMENT_NAMES = new Set([
  "requester", "requesterAgentId", "requester_agent_id",
  "reviewer", "reviewerAgentId", "reviewer_agent_id",
  "agent", "agent_id", "agentId",
  "company", "company_id", "companyId",
  "run", "run_id", "runId",
  "issue", "issue_id", "issueId",
  "reports_to", "reportsTo", "parent",
  "author", "caller", "principal", "identity", "as", "on_behalf_of", "onBehalfOf",
  "user", "user_id", "userId", "actor", "sub", "impersonate",
]);

// Exported so an injected dependency (a test's corroborator, a future one) can
// raise a refusal the handler treats as a refusal. Without this, a dependency
// throwing a plain Error becomes a 500 — an authentication failure reported as
// a server fault, which is the wrong signal in the wrong log.
export class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export function loadConfig(configPath) {
  const raw = readFileSync(configPath, "utf8");
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`config ${configPath} is not valid JSON: ${error.message}`);
  }
  return normalizeConfig(parsed, configPath);
}

export function normalizeConfig(input, configPath = "(inline)") {
  const cfg = { ...input };
  const problems = [];

  if (typeof cfg.companyId !== "string" || !UUID_RE.test(cfg.companyId)) {
    problems.push("companyId must be a uuid");
  }
  if (typeof cfg.bearerSha256 !== "string" || !/^[0-9a-f]{64}$/i.test(cfg.bearerSha256)) {
    // Deliberately the digest and not the token. See the header: a read of this
    // file must not hand anyone a working credential.
    problems.push("bearerSha256 must be the lowercase sha256 hex digest of the shared bearer");
  }
  if (typeof cfg.brokerScript !== "string" || !path.isAbsolute(cfg.brokerScript)) {
    problems.push("brokerScript must be an absolute path to host-ops/broker.py");
  }
  if (typeof cfg.brokerSpool !== "string" || !path.isAbsolute(cfg.brokerSpool)) {
    problems.push("brokerSpool must be an absolute path to the broker spool directory");
  }
  if (typeof cfg.brokerConfig !== "string" || !path.isAbsolute(cfg.brokerConfig)) {
    problems.push("brokerConfig must be an absolute path to the broker config.json");
  }
  if (typeof cfg.publicKeyPath !== "string" || !path.isAbsolute(cfg.publicKeyPath)) {
    // The PUBLIC key: a verifier, safe to name in a config. The private key
    // never leaves the broker's host-owned directory and is never named here.
    problems.push("publicKeyPath must be an absolute path to the response-signing public key");
  }
  if (typeof cfg.expectedPublicKeySha256 !== "string" || !/^[0-9a-f]{64}$/i.test(cfg.expectedPublicKeySha256)) {
    // The independent pin. broker.py refuses a key it cannot verify against;
    // this transport refuses a key whose bytes do not hash to the value the
    // operator recorded out of band. A swapped public key file is a swapped
    // trust root, and that must be loud.
    problems.push("expectedPublicKeySha256 must be the 64-hex sha256 of the pinned response-signing public key");
  }
  const port = Number(cfg.port ?? 8392);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    problems.push("port must be an integer 1-65535");
  }
  // node7b1ff338 is Docker-only, so the engine this host answers to is config, not
  // source (case_81816394, mirroring org-request-mcp containerEngine). Unset means
  // the historical default; anything but docker or podman is refused at load
  // rather than failing per-request in execFile.
  if (cfg.containerEngine !== undefined && cfg.containerEngine !== "docker" && cfg.containerEngine !== "podman") {
    problems.push('containerEngine, when set, must be "docker" or "podman"');
  }
  // brokerEnv is an operator-owned test seam (HOST_OPS_TEST_MODE,
  // HOST_OPS_FAKE_FACTS_DIR), but a config file that can set PATH chooses
  // which container engine and which `python3` run, and one that can set
  // PAPERCLIP_DB_CTR misdirects the run corroboration. Refuse at load, so it
  // is a unit that will not start rather than a request that quietly ran
  // against the wrong database.
  const reservedEnv = Object.keys(isPlainObject(cfg.brokerEnv) ? cfg.brokerEnv : {})
    .filter((key) => RESERVED_BROKER_ENV.has(key));
  if (reservedEnv.length > 0) {
    problems.push(
      `brokerEnv may not set ${reservedEnv.join(", ")}: ${[...RESERVED_BROKER_ENV].join(", ")} are supplied by this `
      + "server and a config that can override them can misdirect execution or tenancy",
    );
  }
  const responseWaitMs = Number(cfg.responseWaitMs ?? 60000);
  if (!Number.isInteger(responseWaitMs) || responseWaitMs < 1000 || responseWaitMs > 300000) {
    problems.push("responseWaitMs must be an integer 1000-300000");
  }
  const responsePollMs = Number(cfg.responsePollMs ?? 500);
  if (!Number.isInteger(responsePollMs) || responsePollMs < 50 || responsePollMs > 10000) {
    problems.push("responsePollMs must be an integer 50-10000");
  }
  if (problems.length > 0) {
    throw new Error(`invalid config ${configPath}:\n  - ${problems.join("\n  - ")}`);
  }

  return {
    companyId: cfg.companyId,
    bearerSha256: cfg.bearerSha256.toLowerCase(),
    brokerScript: cfg.brokerScript,
    brokerSpool: cfg.brokerSpool,
    brokerConfig: cfg.brokerConfig,
    publicKeyPath: cfg.publicKeyPath,
    expectedPublicKeySha256: cfg.expectedPublicKeySha256.toLowerCase(),
    port,
    dbContainer: typeof cfg.dbContainer === "string" ? cfg.dbContainer : "paperclip-db",
    // Verified node7b1ff338 topology (host operator, 2026-10-05): database service
    // "db", container "paperclip-db". Default-user exec is the only
    // compatible target — `docker exec -u node paperclip-db` fails (no node
    // user in the db image) — so the corroborator runs the default-user
    // `sh -c 'exec psql ...'` exactly as below, with no -u flag.
    // Unset stays unset here so containerEngineFor can still honour the
    // environment; the historical "podman" default applies at exec time.
    // A garbage env value fails closed per-request (exec error -> 503
    // run_check_unavailable), never open.
    containerEngine: typeof cfg.containerEngine === "string" ? cfg.containerEngine : undefined,
    // Corroborating (agent, run, company, issue) against heartbeat_runs is the
    // control that turns "a header says so" into "the database agrees". It is
    // on by default and the only sanctioned reason to disable it is an offline
    // test.
    requireLiveRun: cfg.requireLiveRun !== false,
    auditLog: typeof cfg.auditLog === "string" ? cfg.auditLog : null,
    responseWaitMs,
    responsePollMs,
    // Optional passthroughs to broker.py's own test seams. Never a secret.
    brokerEnv: isPlainObject(cfg.brokerEnv) ? { ...cfg.brokerEnv } : {},
  };
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function assertConfigPermissions(configPath, statFn = statSync) {
  const mode = statFn(configPath).mode & 0o777;
  if ((mode & 0o077) !== 0) {
    throw new Error(
      `config ${configPath} is mode ${mode.toString(8)}; it holds a credential verifier and must be 0600 (chmod 600 ${configPath})`,
    );
  }
}

// broker.py is the only entry point. Pin the basename at startup so a
// misconfigured unit refuses to launch rather than executing whatever the
// path names.
export function assertBrokerScript(brokerScript, realpathFn = realpathSync) {
  let resolved;
  try {
    resolved = realpathFn(brokerScript);
  } catch {
    throw new Error(`brokerScript ${brokerScript} does not exist`);
  }
  const base = path.basename(resolved);
  if (base !== "broker.py") {
    throw new Error(
      `brokerScript resolves to ${base}; this transport fronts host-ops/broker.py and nothing else`,
    );
  }
  return resolved;
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

export function bearerMatches(presented, expectedSha256Hex) {
  if (typeof presented !== "string" || presented.length === 0) return false;
  // Hash both sides so the comparison is over two fixed-length buffers and
  // cannot leak the credential's length through timing.
  const presentedDigest = createHash("sha256").update(presented, "utf8").digest();
  const expectedDigest = Buffer.from(expectedSha256Hex, "hex");
  if (expectedDigest.length !== presentedDigest.length) return false;
  return timingSafeEqual(presentedDigest, expectedDigest);
}

function headerValue(headers, name) {
  const value = headers[name];
  if (Array.isArray(value)) return value.length === 1 ? value[0] : undefined;
  return typeof value === "string" ? value : undefined;
}

function requireBearer(req, cfg) {
  const authorization = headerValue(req.headers, "authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(authorization.trim());
  if (!match || !bearerMatches(match[1].trim(), cfg.bearerSha256)) {
    // NOTE: no WWW-Authenticate header on this 401, and that is not an
    // oversight. The gateway treats `401` + a WWW-Authenticate mentioning
    // bearer/oauth as a signal to go discover OAuth endpoints on us
    // (tool-access.ts remoteTools). We are not an OAuth server; sending it
    // would send the gateway down a discovery path that cannot succeed and
    // would bury the real cause — a wrong bearer — under a protocol error.
    throw new HttpError(401, "unauthorized", "invalid or missing bearer credential");
  }
}

/**
 * Establish WHO is calling, including WHICH CARD they are on. Everything
 * below the transport depends on this being right, so it is the one place
 * the server is deliberately strict.
 *
 * Returns { agentId, companyId, runId, issueId, correlationId }.
 */
export function readIdentityHeaders(headers, cfg) {
  const agentId = headerValue(headers, "x-paperclip-agent-id");
  const companyId = headerValue(headers, "x-paperclip-company-id");
  const runId = headerValue(headers, "x-paperclip-run-id");
  const issueId = headerValue(headers, "x-paperclip-issue-id");

  if (!agentId) {
    // The opt-in trap, made loud. See the file header.
    throw new HttpError(
      403, "identity_header_missing",
      "no x-paperclip-agent-id on this request. This transport never acts anonymously. "
      + "The tool_connection must set config.headerPolicy.metadata.forward to include "
      + '"agent_id", "company_id", "run_id" and "issue_id" — the gateway forwards NO metadata headers by default.',
    );
  }
  if (!UUID_RE.test(agentId)) {
    throw new HttpError(403, "identity_header_malformed", "x-paperclip-agent-id is not a uuid");
  }
  if (!companyId || !UUID_RE.test(companyId)) {
    throw new HttpError(403, "identity_header_missing", "no valid x-paperclip-company-id on this request");
  }
  // Tenancy, not authorization: this endpoint has a public hostname and every
  // company on this box shares the host, so another tenant's gateway can reach
  // it. This server fronts exactly one company's broker.
  if (companyId !== cfg.companyId) {
    throw new HttpError(
      403, "wrong_company",
      "this transport serves a single company and the caller belongs to another",
    );
  }
  if (!runId || !UUID_RE.test(runId)) {
    throw new HttpError(
      403, "identity_header_missing",
      'no valid x-paperclip-run-id on this request; add "run_id" to config.headerPolicy.metadata.forward',
    );
  }
  if (!issueId) {
    // The broker's requester contract REQUIRES issueId (broker.py
    // REQUESTER_KEYS) and records it in the audit chain as attribution. There
    // is no anonymous fallback: without a stamped issue there is no principal
    // to attribute the host read to.
    throw new HttpError(
      403, "issue_header_missing",
      'no x-paperclip-issue-id on this request; add "issue_id" to config.headerPolicy.metadata.forward. '
      + "The gateway resolves the session issue from the run snapshot — the caller cannot supply it.",
    );
  }
  if (!UUID_RE.test(issueId)) {
    throw new HttpError(403, "issue_header_malformed", "x-paperclip-issue-id is not a uuid");
  }
  return {
    agentId,
    companyId,
    runId,
    issueId,
    correlationId: headerValue(headers, "x-paperclip-correlation-id") ?? null,
  };
}

// ---------------------------------------------------------------------------
// Run corroboration — the control that makes the bearer non-sufficient.
//
// The bearer proves "this came from our gateway". The agent header proves
// "the gateway says it was this agent". Alone, that means anyone holding the
// bearer can name any agent. This query is the third leg: the database must
// agree that this run exists, is RUNNING, and belongs to this agent in this
// company — AND that the run's snapshot issue matches the stamped issue, so a
// caller cannot attribute host reads to another card by replaying a live run
// id alongside a foreign issue header.
//
// The status set mirrors the gateway's own ACTIVE_GATEWAY_RUN_STATUSES
// (`running`) deliberately. If upstream widens it, this narrows relative to
// the gateway — which fails closed, the right direction.
// ---------------------------------------------------------------------------

const runCheckCache = new Map();

export function makeRunCorroborator(cfg, exec = execFileAsync, now = () => Date.now()) {
  return async function corroborateRun(identity) {
    if (!cfg.requireLiveRun) return;
    const key = `${identity.runId}:${identity.agentId}:${identity.issueId}`;
    const cached = runCheckCache.get(key);
    if (cached && cached.expiresAt > now()) {
      if (cached.ok) return;
      throw new HttpError(403, "run_not_live", cached.message);
    }

    // heartbeat_runs carries NO issue_id column: the issue lives in
    // context_snapshot->>'issueId' (verified live 2026-09-22 against our own
    // run row). The query returns liveness AND the snapshot issue together so
    // the comparison below is against one atomic read.
    const sql =
      "SELECT CASE WHEN status = 'running' THEN 1 ELSE 0 END AS live, "
      + "COALESCE(context_snapshot->>'issueId', '') AS issue "
      + "FROM heartbeat_runs "
      + "WHERE id = :'run_id'::uuid AND agent_id = :'agent_id'::uuid "
      + "AND company_id = :'company_id'::uuid LIMIT 1;";

    let stdout = "";
    let failure = null;
    try {
      // Ids travel in the environment, not argv — the habit from
      // org-request-mcp: nothing this process runs puts a caller-influenced
      // value on a command line that /proc exposes to every account on the box.
      // Verified 2026-10-05: no node-user target exists (paperclip has no
      // psql/env, paperclip-db has no node user), so this is deliberately the
      // DEFAULT-user exec — no -u flag. A -u flag here would need its own
      // review; never add one to reach a different container user.
      const result = await exec(
        containerEngineFor(cfg),
        [
          "exec", "-i",
          "-e", "PGV_RUN_ID", "-e", "PGV_AGENT_ID", "-e", "PGV_COMPANY_ID",
          cfg.dbContainer,
          "sh", "-c",
          'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -F"|" -v ON_ERROR_STOP=1 '
          + '-v run_id="$PGV_RUN_ID" -v agent_id="$PGV_AGENT_ID" -v company_id="$PGV_COMPANY_ID" -f -',
        ],
        {
          input: sql,
          timeout: RUN_CHECK_TIMEOUT_MS,
          env: {
            PATH: process.env.PATH ?? "/usr/bin:/bin",
            HOME: process.env.HOME ?? "/",
            PGV_RUN_ID: identity.runId,
            PGV_AGENT_ID: identity.agentId,
            PGV_COMPANY_ID: identity.companyId,
          },
        },
      );
      stdout = result.stdout;
    } catch (error) {
      failure = error;
    }

    // A database we cannot reach is not a licence to accept the header. This
    // control fails closed like every other one here.
    if (failure) {
      throw new HttpError(
        503, "run_check_unavailable",
        "could not corroborate the calling agent against heartbeat_runs; refusing rather than trusting the header alone",
      );
    }

    const fields = stdout.trim().split("|");
    const live = fields.length === 2 && fields[0] === "1";
    const snapshotIssue = fields.length === 2 ? fields[1] : "";
    const message =
      "the calling agent has no RUNNING heartbeat run matching x-paperclip-run-id. "
      + "Either the run ended mid-call, or the identity headers did not come from this company's gateway.";
    if (!live) {
      rememberRunCheck(key, false, message, now());
      throw new HttpError(403, "run_not_live", message);
    }
    if (snapshotIssue !== identity.issueId) {
      // Do NOT cache a mismatch: the run may legitimately move cards between
      // calls, and a cached refusal would outlive the state it judged.
      throw new HttpError(
        403, "run_issue_mismatch",
        "the stamped x-paperclip-issue-id does not match the calling run's snapshot issue. "
        + "The gateway resolves the session issue from the run — a run cannot act on another card.",
      );
    }
    rememberRunCheck(key, true, message, now());
  };
}

function rememberRunCheck(key, ok, message, nowMs) {
  if (runCheckCache.size >= RUN_CHECK_CACHE_MAX) {
    // Cheap bound. Order is insertion order, so this drops the oldest.
    const oldest = runCheckCache.keys().next();
    if (!oldest.done) runCheckCache.delete(oldest.value);
  }
  runCheckCache.set(key, { ok, message, expiresAt: nowMs + RUN_CHECK_TTL_MS });
}

export function _resetRunCheckCache() {
  runCheckCache.clear();
}

// ---------------------------------------------------------------------------
// Tools — NONE takes an identity, and NONE takes any argument at all.
//
// All tools front host-ops/broker.py read verbs. The requester triple
// (agent, run, issue) is written by buildBrokerArgs from the authenticated
// principal; the schemas below declare NO properties, so there is no shape
// any call can take that names another principal or smuggles a broker
// argument (broker.py read verbs accept exactly {} — see validate_read_request).
//
// WHY NO VERB ARGUMENT
//   The tempting shape is one `host_ops_request` tool taking a `verb` string.
//   That makes the verb caller-influenced data, and every future verb the
//   broker ever gains — including deployment writes — becomes reachable the
//   moment the broker table grows, with no transport change and no review.
//   One tool per verb keeps the Phase-0 allowlist a CODE list (TOOLS), not a
//   runtime string comparison. Adding a verb is a diff to this file, its
//   suite, and the install review — which is exactly the review the card
//   demands before any write verb is enabled.
// ---------------------------------------------------------------------------

export const TOOLS = [
  {
    name: "read_logs",
    verb: "logs.read",
    description:
      "Read a metadata-only tail of the paperclip container: at most 200 entries from the last 15 minutes, "
      + "with all free text redacted by the broker BEFORE signing. Returns timestamps and a fixed "
      + "informational priority, not message text or credentials; the serialized signed response is "
      + "capped at 64 KiB. Identity comes from the Paperclip gateway. No arguments, container "
      + "selection or wider window.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: [],
      properties: {},
    },
  },
  {
    name: "inspect_health",
    verb: "health.inspect",
    description:
      "Read the fixed Paperclip loopback health endpoint through the host broker and return its allowlisted "
      + "field set (status, commit, version, uptime). You are identified automatically by the Paperclip tool "
      + "gateway; the request is attributed to you and your card in the host audit chain. This tool takes NO "
      + "arguments, so there is no way to act as another agent or on another card.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: [],
      // Deliberately empty, and asserted empty by the suite. A property here
      // is the entire distance between "reads for you on your card" and
      // "reads for anyone on any card".
      properties: {},
    },
  },
  {
    name: "inspect_service",
    verb: "service.inspect",
    description:
      "Read the fixed paperclip container state (Status, Running, Pid, start/finish timestamps, exit code) "
      + "through the host broker's exact sudoers command. You are identified automatically by the Paperclip "
      + "tool gateway; the request is attributed to you and your card in the host audit chain. "
      + "This tool takes NO arguments.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: [],
      properties: {},
    },
  },
  {
    name: "locate_tooling",
    verb: "tooling.locate",
    description:
      "Find the fixed ops-tooling marker set below the host's configured search roots. You are identified "
      + "automatically by the Paperclip tool gateway; the request is attributed to you and your card in the "
      + "host audit chain. This tool takes NO arguments.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: [],
      properties: {},
    },
  },
];

const TOOLS_BY_NAME = new Map(TOOLS.map((tool) => [tool.name, tool]));

export function advertisedTools() {
  return TOOLS.map((tool) => ({
    name: tool.name,
    description: tool.description,
    inputSchema: tool.inputSchema,
  }));
}

/**
 * Enforce the tool's own inputSchema: every key must be one it declares.
 *
 * The schemas say `additionalProperties: false`, but a schema is a CLAIM made
 * to the client — there is no JSON-Schema validator in this process and adding
 * one would mean an npm tree next to a container-capable operator account. So the
 * claim is enforced here instead, against the very same schema object that
 * tools/list hands out. The two cannot drift.
 *
 * Refusing rather than dropping is the point. An unlisted key could never
 * reach broker.py either way — buildBrokerArgs reads named arguments and
 * nothing else — but a silent drop lets a caller believe it said something
 * this transport never heard.
 */
function assertKnownArguments(tool, args) {
  const declared = tool.inputSchema.properties;
  // Object.hasOwn, not `in`: `constructor` and `toString` are `in` every plain
  // object, so `in` would quietly admit exactly the keys a prototype-pollution
  // probe reaches for first.
  const unknown = Object.keys(args).find((key) => !Object.hasOwn(declared, key));
  if (unknown === undefined) return;
  throw new HttpError(
    400, "unknown_argument",
    `'${unknown}' is not an argument of ${tool.name}. This tool takes no arguments at all: `
    + "identity comes from the authenticated Paperclip principal and broker arguments are fixed by the verb. "
    + "An unrecognised key is refused rather than dropped, so a caller is never left believing it said "
    + "something this transport never heard.",
  );
}

/**
 * Refuse any argument that names a principal.
 *
 * Specific before general — assertKnownArguments would also refuse these as
 * undeclared keys, but only this refusal names the rule that makes them
 * interesting, so the audit log can tell `requester` (someone trying to be
 * somebody else) apart from a typo.
 */
function assertNoIdentityArgument(args) {
  const bad = Object.keys(args).find((key) => FORBIDDEN_ARGUMENT_NAMES.has(key));
  if (bad) {
    throw new HttpError(
      400, "identity_argument_refused",
      `'${bad}' is not an argument of this tool. Identity is taken from the authenticated Paperclip principal `
      + "and can never be supplied as tool input; an identity the model can fill in voids every authorization "
      + "check below this transport.",
    );
  }
}

/**
 * Translate a validated tool call into an argv array for broker.py request.
 *
 * The requester triple (--agent-id, --run-id, --issue-id) is supplied HERE,
 * from the authenticated principal, and is structurally unreachable from tool
 * input. The request id is fresh per call: broker.py request ids are
 * single-use, and reusing one is a replay refusal.
 */
export function buildBrokerArgs(toolName, args, identity) {
  const tool = TOOLS_BY_NAME.get(toolName);
  if (!tool) {
    throw new HttpError(400, "unknown_tool", `unknown tool: ${toolName}`);
  }

  assertNoIdentityArgument(args);
  assertKnownArguments(tool, args);

  const requestId = `HRQ-${randomUUID().replace(/-/g, "").slice(0, 16)}`;
  return [
    "request",
    "--verb", tool.verb,
    "--request-id", requestId,
    "--agent-id", identity.agentId,
    "--run-id", identity.runId,
    "--issue-id", identity.issueId,
  ];
}

// ---------------------------------------------------------------------------
// Execution — broker.py request, then wait, then broker.py verify.
// ---------------------------------------------------------------------------

function execFileAsync(file, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, { ...options, encoding: "utf8" }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout, stderr });
    });
    if (options.input !== undefined) {
      child.stdin.end(options.input);
    }
  });
}

// broker.py invocations are serialised: two concurrent `request` calls are
// harmless (unique ids), but serialisation keeps the unit's child-process
// count bounded and matches the template's transport hygiene.
function makeSerializer() {
  let tail = Promise.resolve();
  return function serialize(fn) {
    const result = tail.then(fn, fn);
    tail = result.then(() => undefined, () => undefined);
    return result;
  };
}

// The environment broker.py runs in. Built ONCE here so there is a single
// place a config could ever misdirect execution: brokerEnv is spread FIRST,
// so the values this server owns are written last and win. normalizeConfig
// already refuses a config that names one of them; this ordering is what
// still holds for a cfg object built by hand.
export function buildBrokerEnv(cfg) {
  return {
    ...cfg.brokerEnv,
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    HOME: process.env.HOME ?? "/",
    PAPERCLIP_DB_CTR: cfg.dbContainer,
  };
}

// The broker's response bundle is a DIRECTORY published atomically
// (persist_signed → publish_directory_noreplace → rename). A glob of
// responses/*.json would race that publication and could read a half-written
// pair. So the bundle directory — `<basename>.bundle` containing exactly the
// two siblings — is the atomic publication unit, and ONLY a complete one is
// ever opened.
function findCompleteBundle(responsesDir, bundleBase) {
  const bundleDir = path.join(responsesDir, bundleBase + ".bundle");
  let entries;
  try {
    entries = readdirSync(bundleDir);
  } catch {
    return null;
  }
  const jsonFile = entries.find((e) => e === bundleBase + ".json");
  const sigFile = entries.find((e) => e === bundleBase + ".sig");
  if (!jsonFile || !sigFile) return null;
  return { bundleDir, jsonPath: path.join(bundleDir, jsonFile), sigPath: path.join(bundleDir, sigFile) };
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function makeBrokerRunner(cfg, exec = execFileAsync) {
  const serialize = makeSerializer();
  return function runBroker(argv) {
    return serialize(async () => {
      // argv[0] is "request". --spool is appended here, never taken from the
      // caller, so a tool call cannot redirect the broker at a different
      // spool. NOTE: `request` takes NO --config flag — the broker config is
      // only read by the host-side `run`/`run-once` loop. cfg.brokerConfig
      // exists so the OPERATOR's installed config path is recorded next to the
      // transport config, and so the install runbook can assert the two agree;
      // it is never passed to the request subcommand.
      const requestArgv = [...argv, "--spool", cfg.brokerSpool];
      let stdout;
      try {
        ({ stdout } = await exec(cfg.brokerScript, requestArgv, {
          timeout: CLI_TIMEOUT_MS,
          maxBuffer: CLI_MAX_BUFFER,
          env: buildBrokerEnv(cfg),
        }));
      } catch (error) {
        // broker.py refusals are exit 2 with "REFUSED: ..." on stderr. Those
        // are a legitimate ANSWER (bad verb, bad shape — but the shape is
        // fixed here, so this should not happen), not a transport failure.
        const text = [(error.stdout ?? ""), (error.stderr ?? "")].join("").trim();
        return { ok: false, text: text || `the broker request failed: ${error.message}`, exitCode: null };
      }
      let receipt;
      try {
        receipt = JSON.parse(stdout.trim());
      } catch {
        return { ok: false, text: "the broker request did not return a JSON receipt", exitCode: null };
      }
      if (typeof receipt.requestId !== "string" || !BROKER_REQUEST_ID_RE.test(receipt.requestId)) {
        return { ok: false, text: "the broker request returned an invalid requestId", exitCode: null };
      }
      const bundleBase = path.basename(String(receipt.requestPath ?? ""), ".json");
      if (!bundleBase || bundleBase.includes("/") || bundleBase.includes("..")) {
        return { ok: false, text: "the broker request returned an unusable request path", exitCode: null };
      }
      return { ok: true, text: stdout.trim(), exitCode: 0, request: receipt, bundleBase };
    });
  };
}

// The PUBLIC key bytes must hash to the independently pinned value before
// they verify anything. broker.py verify compares the same way
// (--expected-key-sha256); this transport checks the pin itself first so a
// swapped key FILE is refused even before openssl runs, and the key bytes
// that verified the bundle are the bytes that matched the pin.
export function pinnedPublicKey(cfg) {
  let keyBytes;
  try {
    keyBytes = readFileSync(cfg.publicKeyPath);
  } catch (error) {
    throw new HttpError(502, "signing_key_unreadable", `the pinned response-signing public key cannot be read: ${error.message}`);
  }
  const actual = createHash("sha256").update(keyBytes).digest("hex");
  if (actual !== cfg.expectedPublicKeySha256) {
    throw new HttpError(
      502, "signing_key_mismatch",
      "the response-signing public key does not match the independently pinned sha256. "
      + "Refusing to verify against a swapped trust root.",
    );
  }
  return keyBytes;
}

export function makeBundleWaiter(cfg, exec = execFileAsync) {
  return async function waitForBundle(bundleBase) {
    const responsesDir = path.join(cfg.brokerSpool, "responses");
    const deadline = Date.now() + cfg.responseWaitMs;
    for (;;) {
      const found = findCompleteBundle(responsesDir, bundleBase);
      if (found) return found;
      if (Date.now() >= deadline) {
        throw new HttpError(
          504, "broker_response_timeout",
          `the broker published no signed response bundle for this request within ${Math.round(cfg.responseWaitMs / 1000)}s. `
          + "The request may still be queued on the host; retry with a fresh call (request ids are single-use).",
        );
      }
      await sleepMs(cfg.responsePollMs);
    }
  };
}

// Verify through broker.py's OWN verify subcommand — the same code path the
// operator uses — rather than re-implementing openssl verification here. One
// implementation of the signature contract, not two.
export function makeBundleVerifier(cfg, exec = execFileAsync) {
  return async function verifyBundle(bundle) {
    pinnedPublicKey(cfg);
    let stdout;
    try {
      ({ stdout } = await exec(cfg.brokerScript, [
        "verify",
        "--public-key", cfg.publicKeyPath,
        "--expected-key-sha256", cfg.expectedPublicKeySha256,
        "--response", bundle.jsonPath,
        "--signature", bundle.sigPath,
      ], {
        timeout: CLI_TIMEOUT_MS,
        maxBuffer: CLI_MAX_BUFFER,
        env: buildBrokerEnv(cfg),
      }));
    } catch (error) {
      const text = [(error.stdout ?? ""), (error.stderr ?? "")].join("").trim();
      throw new HttpError(
        502, "invalid_signature",
        `the broker's signed response bundle FAILED verification: ${text.slice(0, 300)}. `
        + "No byte of it is returned to the caller.",
      );
    }
    let payload;
    try {
      payload = JSON.parse(stdout);
    } catch {
      throw new HttpError(502, "invalid_signature", "the verified bundle is not valid JSON");
    }
    return { payload, bundleBase: path.basename(bundle.jsonPath, ".json") };
  };
}

// ---------------------------------------------------------------------------
// Audit — transport-level only. broker.py keeps the authoritative host audit
// chain; this records who reached the transport and what happened, and NEVER
// the bearer, and never argument values.
// ---------------------------------------------------------------------------

function makeAuditor(cfg) {
  return function audit(entry) {
    const line = JSON.stringify({ ts: new Date().toISOString(), ...entry });
    if (cfg.auditLog) {
      try {
        appendFileSync(cfg.auditLog, line + "\n", { mode: 0o600 });
      } catch {
        // Never let an audit write failure take the transport down; the line
        // still reaches the journal below.
      }
    }
    process.stdout.write(line + "\n");
  };
}

// ---------------------------------------------------------------------------
// JSON-RPC / MCP
// ---------------------------------------------------------------------------

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id: id ?? null, result };
}

function rpcError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) error.data = data;
  return { jsonrpc: "2.0", id: id ?? null, error };
}

export function createHandler(cfg, deps = {}) {
  const runBroker = deps.runBroker ?? makeBrokerRunner(cfg);
  const waitForBundle = deps.waitForBundle ?? makeBundleWaiter(cfg);
  const verifyBundle = deps.verifyBundle ?? makeBundleVerifier(cfg);
  const corroborateRun = deps.corroborateRun ?? makeRunCorroborator(cfg);
  const audit = deps.audit ?? makeAuditor(cfg);

  async function handleRpc(message, req) {
    const { id, method, params } = message;

    if (method === "initialize") {
      const requested = params?.protocolVersion;
      return rpcResult(id, {
        protocolVersion: SUPPORTED_PROTOCOL_VERSIONS.has(requested) ? requested : PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        instructions:
          "Every tool acts as YOU on YOUR card. Identity comes from the Paperclip tool gateway and is never "
          + "an argument. Only Phase-0 read verbs are exposed; there is no tool that writes, deploys, "
          + "restarts, or effects any host change.",
      });
    }

    if (method === "ping") return rpcResult(id, {});

    // Anonymous by necessity — this is the gateway's catalog refresh and health
    // check, which carries credentials but no session. See the file header.
    if (method === "tools/list") return rpcResult(id, { tools: advertisedTools() });

    if (method === "tools/call") {
      const toolName = params?.name;
      const args = params?.arguments ?? {};
      if (!isPlainObject(args)) {
        throw new HttpError(400, "invalid_argument", "params.arguments must be an object");
      }
      const tool = TOOLS_BY_NAME.get(toolName);
      if (!tool) {
        // Named explicitly so nobody reads a typo as a missing capability, and
        // so an attempt to reach anything else — deployment writes above all —
        // is recorded rather than guessed.
        throw new HttpError(
          400, "unknown_tool",
          `unknown tool '${toolName}'. This transport exposes: `
          + advertisedTools().map((t) => t.name).join(", ")
          + ". There is no tool that writes, deploys, restarts, or effects any host change; "
          + "the broker is read-only and this transport allowlists four read verbs.",
        );
      }

      // Identity is established AFTER the tool is known and BEFORE anything
      // runs. Ordering matters: an unknown tool must not consume a database
      // round trip, and a known tool must never execute without a principal.
      const identity = readIdentityHeaders(req.headers, cfg);
      await corroborateRun(identity);

      const argv = buildBrokerArgs(toolName, args, identity);
      const started = Date.now();
      const submitted = await runBroker(argv);
      if (!submitted.ok) {
        audit({
          event: "tool.call",
          tool: toolName,
          agentId: identity.agentId,
          runId: identity.runId,
          issueId: identity.issueId,
          correlationId: identity.correlationId,
          outcome: "broker_refused",
          exitCode: submitted.exitCode,
          durationMs: Date.now() - started,
        });
        return rpcResult(id, {
          content: [{ type: "text", text: `REFUSED (broker_refused): ${submitted.text}` }],
          isError: true,
        });
      }
      const bundle = await waitForBundle(submitted.bundleBase);
      const verified = await verifyBundle(bundle);
      audit({
        event: "tool.call",
        tool: toolName,
        agentId: identity.agentId,
        runId: identity.runId,
        issueId: identity.issueId,
        correlationId: identity.correlationId,
        // Keys only. Values live in the broker's own audit chain, which is authoritative.
        argumentKeys: Object.keys(args).sort(),
        outcome: verified.payload.status ?? "unknown",
        requestId: verified.payload.requestId ?? null,
        auditSequence: verified.payload.audit?.sequence ?? null,
        durationMs: Date.now() - started,
      });
      return rpcResult(id, {
        content: [{
          type: "text",
          text: JSON.stringify({
            response: verified.payload,
            bundle: verified.bundleBase,
          }),
        }],
        isError: verified.payload.status !== "succeeded",
      });
    }

    return rpcError(id, -32601, `method not found: ${method}`);
  }

  return async function handle(req, body) {
    if (req.method !== "POST") {
      // No SSE stream to open, so a GET has nothing to return. 405 is the
      // spec's answer for a server that does not offer the streaming leg.
      throw new HttpError(405, "method_not_allowed", "this endpoint accepts POST only");
    }

    requireBearer(req, cfg);

    let message;
    try {
      message = JSON.parse(body);
    } catch {
      return { status: 400, payload: rpcError(null, -32700, "parse error") };
    }
    if (Array.isArray(message)) {
      // Batching was removed from MCP in 2025-06-18 and supporting it here
      // would mean deciding identity once for several calls.
      return { status: 400, payload: rpcError(null, -32600, "JSON-RPC batching is not supported") };
    }
    if (!isPlainObject(message) || typeof message.method !== "string") {
      return { status: 400, payload: rpcError(null, -32600, "invalid request") };
    }

    // A notification (no id) gets no body — 202 per the Streamable HTTP spec.
    const isNotification = message.id === undefined || message.id === null;
    if (isNotification) {
      return { status: 202, payload: null };
    }

    try {
      return { status: 200, payload: await handleRpc(message, req) };
    } catch (error) {
      if (error instanceof HttpError) {
        // An IDENTITY refusal (401/403) is an HTTP status, so the gateway
        // records it as a connection-level failure rather than a tool answer
        // the model might paper over. Everything else — bad arguments (400),
        // a broker wait that timed out (504), a key that will not verify
        // (502) — is the CALLER's answer to read: it names what to fix or
        // retry, and an HTTP status would hide it in the connection health
        // instead of the tool result.
        if (error.status === 401 || error.status === 403) {
          audit({
            event: "request.refused",
            code: error.code,
            status: error.status,
            tool: message.params?.name ?? null,
            // Recorded so a misconfigured connection is diagnosable, but never
            // trusted: this is the unverified claim, which is why it is refused.
            claimedAgentId: headerValue(req.headers, "x-paperclip-agent-id") ?? null,
          });
          throw error;
        }
        // A refusal about the ARGUMENTS or the BROKER WAIT is the caller's answer to read.
        audit({
          event: "tool.rejected",
          code: error.code,
          tool: message.params?.name ?? null,
          argumentKeys: Object.keys(message.params?.arguments ?? {}).sort(),
        });
        return {
          status: 200,
          payload: rpcResult(message.id, {
            content: [{ type: "text", text: `REFUSED (${error.code}): ${error.message}` }],
            isError: true,
          }),
        };
      }
      audit({ event: "request.error", message: String(error?.message ?? error) });
      return { status: 500, payload: rpcError(message.id, -32603, "internal error") };
    }
  };
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

export function createServer(cfg, deps = {}) {
  const handle = deps.handle ?? createHandler(cfg, deps);
  let inFlight = 0;

  return http.createServer((req, res) => {
    const requestId = randomUUID();
    res.setHeader("x-request-id", requestId);

    const url = new URL(req.url ?? "/", "http://localhost");
    if (url.pathname !== "/mcp") {
      // No health endpoint, no index, no error detail. The hostname is public;
      // an unauthenticated probe learns only that something answers on 404.
      sendJson(res, 404, { error: "not found" });
      return;
    }

    // The endpoint is public-facing through Caddy, so an unbounded number of
    // concurrent container-exec children is a real failure mode even with a
    // valid bearer.
    if (inFlight >= MAX_CONCURRENT_REQUESTS) {
      sendJson(res, 503, { error: "busy" });
      return;
    }
    inFlight += 1;

    readBody(req, MAX_BODY_BYTES)
      .then(async (body) => {
        const { status, payload } = await handle(req, body);
        if (payload === null) {
          res.writeHead(status);
          res.end();
          return;
        }
        sendJson(res, status, payload);
      })
      .catch((error) => {
        const status = error instanceof HttpError ? error.status : 500;
        const code = error instanceof HttpError ? error.code : "internal_error";
        const message = error instanceof HttpError ? error.message : "internal error";
        // See requireBearer: no WWW-Authenticate on a 401, ever.
        sendJson(res, status, { error: { code, message } });
        // Drain whatever the caller is still sending. A rejection decided from
        // Content-Length alone leaves an unread request stream, and node holds
        // the socket open until it ends — the caller sees the 413 only after a
        // multi-second stall, which reads like a hang rather than a refusal.
        req.resume();
      })
      .finally(() => {
        inFlight -= 1;
      });
  });
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    // Reject on the declared length FIRST, before a byte is read. Destroying a
    // socket mid-upload makes the peer see ECONNRESET rather than the 413, and
    // the gateway then records an opaque network failure instead of the actual
    // reason. The gateway always sends a fixed JSON body, so this is the path
    // an oversized request really takes.
    const declared = Number(req.headers["content-length"]);
    if (Number.isFinite(declared) && declared > limit) {
      reject(new HttpError(413, "payload_too_large", "request body too large"));
      return;
    }

    const chunks = [];
    let size = 0;
    let overflowed = false;
    req.on("data", (chunk) => {
      if (overflowed) return;
      size += chunk.length;
      if (size > limit) {
        // Backstop for a chunked request with no declared length. Keep draining
        // rather than destroying, so the response still reaches the caller.
        overflowed = true;
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (overflowed) {
        reject(new HttpError(413, "payload_too_large", "request body too large"));
        return;
      }
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", reject);
  });
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export async function main(argv) {
  // The config PATH on the command line is fine; the config CONTENTS are not
  // on the command line, and the file itself holds digests rather than tokens.
  // /proc/*/cmdline stays boring.
  const configPath = argv[0] ?? process.env.HOST_OPS_MCP_CONFIG ?? "/etc/host-ops-mcp/config.json";
  assertConfigPermissions(configPath);
  const cfg = loadConfig(configPath);
  assertBrokerScript(cfg.brokerScript);

  const server = createServer(cfg);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(cfg.port, BIND_ADDRESS, resolve);
  });
  process.stdout.write(JSON.stringify({
    ts: new Date().toISOString(),
    event: "listening",
    address: `${BIND_ADDRESS}:${cfg.port}`,
    companyId: cfg.companyId,
    brokerScript: cfg.brokerScript,
    brokerSpool: cfg.brokerSpool,
    requireLiveRun: cfg.requireLiveRun,
    tools: advertisedTools().map((tool) => tool.name),
    allowlistedVerbs: [...ALLOWLISTED_VERBS],
  }) + "\n");

  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
  return server;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`host-ops-mcp: ${error.message}\n`);
    process.exit(1);
  });
}
