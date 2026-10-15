#!/usr/bin/env node
// Operator-only kernel rehearsal. No app, database, live HOME, or inherited run credentials.
// Sources: https://docs.docker.com/engine/storage/bind-mounts/#use-a-read-only-bind-mount
//          https://docs.docker.com/reference/compose-file/services/#volumes
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chownSync, chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync,
  readFileSync, readdirSync, realpathSync, renameSync, rmSync, statSync, writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const TARGET = "/paperclip/.claude/settings.json";
const LABEL = "two.settings-bind-fixture";
export const ENTRYPOINT = ["/usr/bin/tini", "--", "docker-entrypoint.sh"];
const SAFE_IMAGE_ENV = new Set([
  "PATH", "NODE_VERSION", "YARN_VERSION", "NODE_ENV", "HOME", "HOST", "PORT", "SERVE_UI",
  "PAPERCLIP_HOME", "PAPERCLIP_INSTANCE_ID", "PAPERCLIP_BUILD_VERSION", "PAPERCLIP_BUILD_COMMIT",
  "USER_UID", "USER_GID", "PAPERCLIP_CONFIG", "PAPERCLIP_DEPLOYMENT_MODE",
  "PAPERCLIP_DEPLOYMENT_EXPOSURE", "OPENCODE_ALLOW_ALL_MODELS", "GEMINI_SANDBOX",
]);
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function snapshot(path) {
  const s = statSync(path);
  return { uid: s.uid, gid: s.gid, mode: (s.mode & 0o777).toString(8), size: s.size,
    device: s.dev, inode: s.ino, sha256: s.isFile() ? sha256(readFileSync(path)) : null };
}

export function fileMount(mountinfo, target = TARGET) {
  const decode = (s) => s.replace(/\\([0-7]{3})/g, (_, octal) => String.fromCharCode(parseInt(octal, 8)));
  const matches = mountinfo.trim().split("\n").map((line) => line.split(" "))
    .filter((fields) => decode(fields[4] ?? "") === target);
  assert(matches.length <= 1, "ambiguous settings mount");
  return matches.length ? { id: matches[0][0], root: decode(matches[0][3]),
    readOnly: matches[0][5].split(",").includes("ro") } : null;
}

export function validateImage(image, config) {
  assert.match(image, /^sha256:[a-f0-9]{64}$/, "use an already-local immutable image ID");
  assert(JSON.stringify(config.Entrypoint) === JSON.stringify(ENTRYPOINT), "unreviewed image entrypoint");
  assert(["", "root", "0", "0:0"].includes(config.User ?? ""), "root-to-node entrypoint must be exercised");
  for (const item of config.Env ?? []) {
    const name = item.split("=", 1)[0];
    assert(SAFE_IMAGE_ENV.has(name), `unreviewed image env name: ${name}`);
  }
}

export function operatorGate(env, acknowledged) {
  assert(!env.PAPERCLIP_RUN_ID && !env.PAPERCLIP_AGENT_ID, "agent context: Docker host access is forbidden");
  assert(acknowledged, "requires --operator-fixture-only");
  assert(!env.DOCKER_HOST && !env.DOCKER_CONTEXT, "remote/custom Docker endpoints are not accepted");
}

export function makeFixture(home, guardSource = join(REPO, "hooks", "muse-stop-guard"), owner = { uid: 1000, gid: 1000 }) {
  assert(!existsSync(home), "fixture must be new");
  mkdirSync(join(home, ".claude", "sessions"), { recursive: true });
  mkdirSync(join(home, "muse-stop-guard"));
  writeFileSync(join(home, ".settings-bind-fixture"), "synthetic-settings-bind-v1\n");
  for (const name of ["muse-stop-guard.mjs", "install-muse-stop-guard.mjs"]) {
    copyFileSync(join(guardSource, name), join(home, "muse-stop-guard", name));
  }
  const settings = {
    model: "fixture-only-model", env: { FIXTURE_PIN: "preserve-this-value" },
    hooks: {
      PreToolUse: [{ hooks: [{ type: "command", command: "fixture-model-guard" }] }],
      UserPromptSubmit: [{ hooks: [{ type: "command", command: "fixture-pacer" }] }],
      Stop: [{ hooks: [{ type: "command", command: "node /paperclip/muse-stop-guard/muse-stop-guard.mjs", timeout: 15 }] }],
    },
  };
  writeFileSync(join(home, ".claude", "settings.json"), JSON.stringify(settings, null, 2) + "\n");
  function own(path) {
    const s = statSync(path);
    if (s.isDirectory()) for (const name of readdirSync(path)) own(join(path, name));
    chmodSync(path, s.isDirectory() ? 0o755 : 0o644);
    if (s.uid !== owner.uid || s.gid !== owner.gid) chownSync(path, owner.uid, owner.gid);
  }
  own(home); // Only this freshly-created fixture; never a Paperclip mount.
}

function attempt(fn) {
  try { fn(); return { succeeded: true, errorCode: null }; }
  catch (e) { return { succeeded: false, errorCode: e.code ?? "UNKNOWN" }; }
}

export function verifyProbe(report, mode) {
  assert.equal(report.uid, 1000); assert.equal(report.gid, 1000);
  assert.equal(report.guard.installed, true); assert.equal(report.guard.scriptExists, true);
  assert.equal(report.sessionWrite, true);
  assert.equal(report.killSwitchBehavior, report.killSwitchPresent ? "kill-switch" : "not-muse");
  assert.equal(report.guard.killSwitchPresent, report.killSwitchPresent);
  assert.equal(report.before.sha256, report.afterDirect.sha256);
  assert.equal(report.before.sha256, report.afterRename.sha256);
  assert.equal(report.before.sha256, report.after.sha256);
  assert.deepEqual(report.parentBefore, report.parentAfter);
  if (mode === "bound") {
    assert.equal(report.mountBefore?.readOnly, true, "missing kernel RO file mount");
    for (const operation of [report.directWrite, report.renameOver]) {
      assert.equal(operation.succeeded, false, "write vector remained open");
      assert(["EROFS", "EBUSY", "EACCES", "EPERM"].includes(operation.errorCode), "unexpected failure is not containment");
    }
    for (const value of [report.afterDirect, report.afterRename, report.after]) assert.deepEqual(value, report.before);
    assert.deepEqual(report.mountAfter, report.mountBefore);
  } else {
    assert.equal(report.mountBefore, null, "rollback must REMOVE the file bind, not make it rw");
    assert.equal(report.mountAfter, null);
    assert.equal(report.directWrite.succeeded, true, "unbound positive control must be writable");
    assert.equal(report.renameOver.succeeded, true, "unbound positive control must permit atomic replacement");
    assert.notEqual(report.afterRename.inode, report.before.inode);
  }
}

export async function probeFixture(home, mode, mountinfo = readFileSync("/proc/self/mountinfo", "utf8")) {
  assert.equal(readFileSync(join(home, ".settings-bind-fixture"), "utf8"), "synthetic-settings-bind-v1\n");
  assert(["baseline", "bound", "rollback"].includes(mode));
  const settings = join(home, ".claude", "settings.json");
  const bytes = readFileSync(settings);
  const report = { mode, hostname: readFileSync("/etc/hostname", "utf8").trim(), uid: process.getuid(), gid: process.getgid(),
    before: snapshot(settings), parentBefore: snapshot(dirname(settings)), mountBefore: fileMount(mountinfo, settings) };
  report.directWrite = attempt(() => writeFileSync(settings, bytes, { flag: "w" }));
  report.afterDirect = snapshot(settings);
  const temp = join(dirname(settings), ".rename-fixture-" + randomUUID());
  writeFileSync(temp, bytes, { mode: 0o644 });
  report.renameOver = attempt(() => renameSync(temp, settings));
  report.afterRename = snapshot(settings);
  if (existsSync(temp)) rmSync(temp);
  const session = join(dirname(settings), "sessions", "fixture-session-" + randomUUID());
  writeFileSync(session, "fixture session\n");
  report.sessionWrite = readFileSync(session, "utf8") === "fixture session\n";
  rmSync(session);
  const guardDir = join(home, "muse-stop-guard");
  const status = spawnSync(process.execPath, [join(guardDir, "install-muse-stop-guard.mjs"), "status", "--settings", settings],
    { encoding: "utf8", timeout: 10000, env: { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: home } });
  assert.equal(status.status, 0, "guard status command failed");
  const result = JSON.parse(status.stdout);
  report.guard = { installed: result.installed, scriptExists: result.scriptExists, killSwitchPresent: result.killSwitchPresent };
  report.killSwitchPresent = existsSync(join(guardDir, "DISABLED"));
  const { decide } = await import(pathToFileURL(join(guardDir, "muse-stop-guard.mjs")));
  report.killSwitchBehavior = decide({ hook_event_name: "Stop" }, { ANTHROPIC_MODEL: "fixture-only-model" }, { config: {} }).why;
  report.after = snapshot(settings);
  report.parentAfter = snapshot(dirname(settings));
  report.mountAfter = fileMount(readFileSync("/proc/self/mountinfo", "utf8"), settings);
  return report;
}

export function dockerArgs({ name, id, home, image, mode, wrapper = false, source = HERE }) {
  const args = ["run", "--pull=never", "--name", name, "--label", `${LABEL}=${id}`, "--network", "none",
    "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
    "--memory", "256m", "--cpus", "0.5", "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
    "--mount", `type=bind,src=${home},dst=/paperclip`, "--mount", `type=bind,src=${source},dst=/rehearsal,readonly`,
    "--env", "HOME=/paperclip", "--env", "PAPERCLIP_HOME=/paperclip", "--env", "USER_UID=1000", "--env", "USER_GID=1000"];
  if (mode === "bound") args.push("--mount", `type=bind,src=${join(home, ".claude", "settings.json")},dst=${TARGET},readonly`);
  if (wrapper) args.push("--entrypoint", "/bin/sh");
  args.push(image);
  if (wrapper) args.push("-c", 'umask 0077 && exec /usr/bin/tini -- docker-entrypoint.sh "$@"', "paperclip-umask");
  args.push("node", "/rehearsal/settings-bind-rehearsal.mjs", "probe", mode);
  return args;
}

export function validateWorkRoot(path) {
  const root = realpathSync(path);
  assert(!["/paperclip", "/app", "/var/run", "/run"].some((p) => root === p || root.startsWith(p + "/")), "not a permitted fixture root");
  assert(statSync(root).isDirectory());
  assert(!root.includes(","), "Docker --mount path must not contain comma");
  return root;
}

export function runRehearsal({ image, workRoot, wrapper = false, acknowledged = false }, env = process.env, spawn = spawnSync, createHome = makeFixture) {
  operatorGate(env, acknowledged);
  assert.match(image, /^sha256:[a-f0-9]{64}$/);
  const root = validateWorkRoot(workRoot);
  // Minimal client environment. Never --env-file, host HOME binds, or inherited agent secrets.
  const dockerEnv = { PATH: env.PATH ?? "/usr/local/bin:/usr/bin:/bin", HOME: root };
  const docker = (args) => {
    const r = spawn("docker", args, { encoding: "utf8", timeout: 60000, maxBuffer: 1024 * 1024, env: dockerEnv });
    assert(!r.error && r.status === 0, `Docker ${args[0]} failed: ${r.error?.code ?? r.status}; raw output withheld`);
    return r.stdout;
  };
  const config = JSON.parse(docker(["image", "inspect", image, "--format", "{{json .Config}}"]));
  validateImage(image, config);
  const dir = mkdtempSync(join(root, "settings-bind-"));
  const home = join(dir, "home");
  createHome(home);
  const baseline = snapshot(join(home, ".claude", "settings.json"));
  const id = randomUUID();
  const report = { version: 1, kind: "isolated-kernel-rehearsal", startedAt: new Date().toISOString(), image, fixtureRoot: dir,
    entrypoint: config.Entrypoint, umaskWrapper: wrapper, baseline,
    sourceHashes: Object.fromEntries([join(HERE, "settings-bind-rehearsal.mjs"),
      join(home, "muse-stop-guard", "muse-stop-guard.mjs"), join(home, "muse-stop-guard", "install-muse-stop-guard.mjs")]
      .map((path) => [path, sha256(readFileSync(path))])),
    commands: [], results: [], passed: false };
  for (const [mode, disabled] of [["baseline", false], ["bound", false], ["bound", true], ["rollback", false]]) {
    const kill = join(home, "muse-stop-guard", "DISABLED");
    if (disabled) {
      writeFileSync(kill, "fixture only\n");
      const owner = statSync(home); const current = statSync(kill);
      if (current.uid !== owner.uid || current.gid !== owner.gid) chownSync(kill, owner.uid, owner.gid);
    }
    else if (existsSync(kill)) rmSync(kill);
    const name = "settings-bind-" + id + "-" + report.results.length;
    const args = dockerArgs({ name, id, home, image, mode, wrapper });
    report.commands.push(["docker", ...args]);
    try {
      const output = docker(args);
      const result = JSON.parse(output);
      verifyProbe(result, mode);
      assert.equal(result.killSwitchPresent, disabled);
      assert.equal(result.before.sha256, baseline.sha256);
      assert.equal(result.after.sha256, baseline.sha256);
      report.results.push(result);
    } catch (e) {
      report.failure = e.message;
      writeFileSync(join(dir, "RESULT.json"), JSON.stringify(report, null, 2) + "\n");
      throw new Error(`rehearsal failed; evidence: ${join(dir, "RESULT.json")}; ${e.message}`);
    } finally {
      // Delete only the exactly-named container carrying this fresh fixture's label.
      const check = spawn("docker", ["inspect", "--format", `{{index .Config.Labels "${LABEL}"}}`, name],
        { encoding: "utf8", timeout: 10000, env: dockerEnv });
      if (check.status === 0 && check.stdout.trim() === id) docker(["rm", "-f", name]);
    }
  }
  report.rollback = snapshot(join(home, ".claude", "settings.json"));
  for (const key of ["uid", "gid", "mode", "size", "sha256"]) assert.equal(report.rollback[key], baseline[key]);
  assert(!existsSync(join(home, "muse-stop-guard", "DISABLED")));
  report.passed = true;
  const resultPath = join(dir, "RESULT.json");
  writeFileSync(resultPath, JSON.stringify(report, null, 2) + "\n");
  return { passed: true, resultPath, image, fixtureRoot: dir };
}

async function main(argv) {
  if (argv[0] === "probe" && argv.length === 2) {
    const report = await probeFixture("/paperclip", argv[1]);
    verifyProbe(report, argv[1]);
    console.log(JSON.stringify(report));
    return;
  }
  if (argv[0] !== "run") throw new Error("use run --image sha256:... --work-root <isolated-dir> --operator-fixture-only [--umask-wrapper]");
  const options = {};
  for (let i = 1; i < argv.length; i++) {
    const key = argv[i];
    if (key === "--operator-fixture-only") options.acknowledged = true;
    else if (key === "--umask-wrapper") options.wrapper = true;
    else if (key === "--image" || key === "--work-root") {
      assert(argv[i + 1] && !argv[i + 1].startsWith("--"), "missing argument");
      options[key === "--image" ? "image" : "workRoot"] = argv[++i];
    } else throw new Error("unknown argument");
  }
  console.log(JSON.stringify(runRehearsal(options)));
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((e) => { console.error(e.message); process.exitCode = 1; });
}
