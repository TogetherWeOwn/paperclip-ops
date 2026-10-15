// Imported by the existing Muse guard CI suite. No Docker or live settings reads.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { dockerArgs, ENTRYPOINT, fileMount, makeFixture, operatorGate, probeFixture,
  runRehearsal, snapshot, validateImage, validateWorkRoot, verifyProbe } from "./settings-bind-rehearsal.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const IMAGE = "sha256:" + "a".repeat(64);
const makeLocalFixture = (home) => makeFixture(home, undefined, { uid: process.getuid(), gid: process.getgid() });
const MOUNT = "351 350 0:85 /.claude/settings.json /paperclip/.claude/settings.json ro,relatime - ext4 /dev/fixture rw\n";
const config = { Entrypoint: ENTRYPOINT, User: "", Env: ["HOME=/paperclip", "USER_UID=1000", "USER_GID=1000"] };
function fakeReport(mode = "bound") {
  const before = { uid: 1000, gid: 1000, mode: "644", size: 42, device: 1, inode: 2, sha256: "fixture-hash" };
  const parent = { uid: 1000, gid: 1000, mode: "755", inode: 1 };
  const bound = mode === "bound";
  return { uid: 1000, gid: 1000, before, afterDirect: { ...before }, afterRename: { ...before, inode: bound ? 2 : 3 },
    after: { ...before, inode: bound ? 2 : 3 }, parentBefore: parent, parentAfter: { ...parent },
    guard: { installed: true, scriptExists: true, killSwitchPresent: false }, sessionWrite: true,
    killSwitchPresent: false, killSwitchBehavior: "not-muse", mountBefore: bound ? fileMount(MOUNT) : null,
    mountAfter: bound ? fileMount(MOUNT) : null, directWrite: { succeeded: !bound, errorCode: bound ? "EROFS" : null },
    renameOver: { succeeded: !bound, errorCode: bound ? "EBUSY" : null } };
}

test("bind: operator packet heredoc parses without executing any live probe", () => {
  const packet = readFileSync(join(HERE, "SETTINGS-BIND.md"), "utf8");
  const snippets = [...packet.matchAll(/^docker exec -u 1000:1000 -i paperclip node <<'NODE'\r?\n([\s\S]*?)^NODE\r?$/gm)];
  assert.equal(snippets.length, 1);
  const source = snippets[0][1];
  const dir = mkdtempSync(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? process.env.PAPERCLIP_SCRATCH_DIR ?? tmpdir(), "settings-bind-syntax-"));
  try {
    const file = join(dir, "post-install.cjs");
    const check = (body) => {
      writeFileSync(file, body);
      return spawnSync(process.execPath, ["--check", file], { encoding: "utf8", env: {} });
    };
    const parsed = check(source);
    assert.equal(parsed.status, 0, parsed.stderr);
    assert.equal(parsed.stdout, "");
    assert.equal(check(source.replaceAll("\\n", "\n")).status, 0);
    for (const [token, brokenToken] of [
      ["split(lf)", "split('\n')"],
      ["'session-fixture'+lf", "'session-fixture\n'"],
    ]) {
      assert(source.includes(token));
      assert.notEqual(check(source.replaceAll(token, brokenToken)).status, 0);
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("bind: exact FILE mount required; parent RO or missing mount is not evidence", () => {
  assert.equal(fileMount(MOUNT).readOnly, true);
  assert.equal(fileMount(MOUNT.replace("ro,relatime", "rw,relatime")).readOnly, false);
  assert.equal(fileMount(MOUNT.replace("/paperclip/.claude/settings.json", "/paperclip/.claude")), null);
  assert.equal(fileMount(""), null);
  assert.throws(() => fileMount(MOUNT + MOUNT));
});
test("bind: escaped mount paths parsed, not prefix matched", () => {
  assert.equal(fileMount(MOUNT.replace("/paperclip/.claude/settings.json", "/fixture\\040home/settings.json"), "/fixture home/settings.json").readOnly, true);
});
test("bind: baseline, bound and rollback reports must all prove their controls", () => {
  for (const mode of ["baseline", "bound", "rollback"]) verifyProbe(fakeReport(mode), mode);
});
for (const [name, change] of [
  ["wrong uid", (r) => { r.uid = 0; }], ["wrong gid", (r) => { r.gid = 0; }],
  ["direct write succeeds", (r) => { r.directWrite.succeeded = true; }],
  ["rename succeeds", (r) => { r.renameOver.succeeded = true; }],
  ["missing file mount", (r) => { r.mountBefore = null; }],
  ["writable file mount", (r) => { r.mountBefore.readOnly = false; }],
  ["inode replaced", (r) => { r.afterRename.inode++; }],
  ["intermediate bytes changed", (r) => { r.afterDirect.sha256 = "changed"; }],
  ["mount lost", (r) => { r.mountAfter = null; }],
  ["unexpected denial", (r) => { r.renameOver.errorCode = "ENOENT"; }],
  ["session storage unwritable", (r) => { r.sessionWrite = false; }],
  ["guard absent", (r) => { r.guard.installed = false; }],
  ["guard script absent", (r) => { r.guard.scriptExists = false; }],
  ["kill switch broken", (r) => { r.killSwitchPresent = true; r.guard.killSwitchPresent = true; }],
  ["parent permissions changed", (r) => { r.parentAfter.mode = "555"; }],
]) test(`bind: rejects ${name}`, () => { const r = fakeReport(); change(r); assert.throws(() => verifyProbe(r, "bound")); });
test("bind: DISABLED state accepted only when the real guard observed it", () => {
  const r = fakeReport(); r.killSwitchPresent = true; r.guard.killSwitchPresent = true; r.killSwitchBehavior = "kill-switch";
  verifyProbe(r, "bound");
});
test("bind: rollback requires removing even an rw file mount and both writable positive controls", () => {
  for (const change of [
    (r) => { r.mountBefore = { readOnly: false }; }, (r) => { r.renameOver.succeeded = false; },
    (r) => { r.directWrite.succeeded = false; }, (r) => { r.afterRename.inode = r.before.inode; },
  ]) { const r = fakeReport("rollback"); change(r); assert.throws(() => verifyProbe(r, "rollback")); }
});
test("bind: Docker command preserves entrypoint and has no app, DB, egress, live home or socket", () => {
  const args = dockerArgs({ name: "fixture", id: "test", home: "/fixture/home", image: IMAGE, mode: "bound" });
  assert.equal(args[args.indexOf("--network") + 1], "none");
  assert(args.includes("--pull=never")); assert(args.includes("--read-only"));
  assert(!args.includes("--entrypoint")); assert(!args.includes("--privileged"));
  const mounts = args.filter((_, i) => args[i - 1] === "--mount");
  assert.equal(mounts.length, 3);
  assert(mounts.includes("type=bind,src=/fixture/home/.claude/settings.json,dst=/paperclip/.claude/settings.json,readonly"));
  assert.equal(args.slice(args.indexOf(IMAGE) + 1).join(" "), "node /rehearsal/settings-bind-rehearsal.mjs probe bound");
  assert(!args.some((arg) => /docker\.sock|--env-file|DATABASE_URL|server\/dist/.test(arg)));
  for (const mode of ["baseline", "rollback"]) {
    const unbound = dockerArgs({ name: "fixture", id: "test", home: "/fixture/home", image: IMAGE, mode });
    assert(!unbound.some((arg) => arg.includes("dst=/paperclip/.claude/settings.json")));
  }
});
test("bind: coordinated umask wrapper still execs Tini and original entrypoint", () => {
  const args = dockerArgs({ name: "fixture", id: "test", home: "/fixture/home", image: IMAGE, mode: "bound", wrapper: true });
  assert.equal(args[args.indexOf("--entrypoint") + 1], "/bin/sh");
  assert(args.includes('umask 0077 && exec /usr/bin/tini -- docker-entrypoint.sh "$@"'));
});
test("bind: immutable image and reviewed image env/entrypoint required; rejection does not print values", () => {
  validateImage(IMAGE, config);
  assert.throws(() => validateImage("paperclip:latest", config));
  assert.throws(() => validateImage(IMAGE, { ...config, Entrypoint: ["secret-canary"] }), (e) => !e.message.includes("secret-canary"));
  assert.throws(() => validateImage(IMAGE, { ...config, Env: ["UNREVIEWED=secret-canary"] }), (e) => !e.message.includes("secret-canary"));
});
test("bind: operator-only context fails before accessing Docker, no marker scrubbing", () => {
  assert.throws(() => operatorGate({ PAPERCLIP_RUN_ID: "run" }, true));
  assert.throws(() => operatorGate({ PAPERCLIP_AGENT_ID: "agent" }, true));
  assert.throws(() => operatorGate({}, false));
  assert.throws(() => operatorGate({ DOCKER_HOST: "remote" }, true));
  assert.throws(() => operatorGate({ DOCKER_CONTEXT: "remote" }, true));
  assert.throws(() => runRehearsal({ image: IMAGE, workRoot: "/does-not-exist", acknowledged: true }, { PAPERCLIP_RUN_ID: "run" }));
  operatorGate({}, true);
});
test("bind: live work roots refused", () => {
  assert.throws(() => validateWorkRoot("/paperclip"));
});
test("bind: synthetic baseline and rollback exercise actual installer/guard, preserve bytes and session writes", async () => {
  const dir = mkdtempSync(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? tmpdir(), "settings-bind-test-"));
  try {
    const home = join(dir, "home"); makeLocalFixture(home);
    const settings = join(home, ".claude", "settings.json"); const before = snapshot(settings);
    assert.throws(() => makeFixture(home));
    for (const mode of ["baseline", "rollback"]) {
      const r = await probeFixture(home, mode);
      assert.equal(r.before.sha256, before.sha256); assert.equal(r.after.sha256, before.sha256);
    }
    writeFileSync(join(home, "muse-stop-guard", "DISABLED"), "fixture only\n");
    const r = await probeFixture(home, "baseline");
    assert.equal(r.killSwitchBehavior, "kill-switch");
    assert.equal(r.after.sha256, before.sha256);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("bind: Docker-emulated orchestration runs four phases and only deletes its labeled containers", () => {
  const dir = mkdtempSync(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? tmpdir(), "settings-bind-test-"));
  const calls = []; let label;
  const spawn = (_, args, options) => {
    calls.push(args);
    assert.deepEqual(Object.keys(options.env).sort(), ["HOME", "PATH"]);
    if (args[0] === "image") return { status: 0, stdout: JSON.stringify(config) };
    if (args[0] === "inspect") return { status: 0, stdout: label + "\n" };
    if (args[0] === "rm") return { status: 0, stdout: "" };
    const mode = args.at(-1); label = args[args.indexOf("--label") + 1].split("=")[1];
    const home = args[args.indexOf("--mount") + 1].split(",")[1].slice(4);
    const r = fakeReport(mode); r.before = snapshot(join(home, ".claude", "settings.json"));
    r.afterDirect = { ...r.before }; r.afterRename = { ...r.before, inode: r.before.inode + (mode === "bound" ? 0 : 1) }; r.after = { ...r.afterRename };
    r.killSwitchPresent = args[args.indexOf("--name") + 1].endsWith("-2");
    r.guard.killSwitchPresent = r.killSwitchPresent; r.killSwitchBehavior = r.killSwitchPresent ? "kill-switch" : "not-muse";
    return { status: 0, stdout: JSON.stringify(r) };
  };
  try {
    const result = runRehearsal({ image: IMAGE, workRoot: dir, acknowledged: true }, {}, spawn, makeLocalFixture);
    assert.equal(result.passed, true);
    const report = JSON.parse(readFileSync(result.resultPath));
    assert.equal(report.results.length, 4); assert.equal(report.commands.length, 4);
    assert.deepEqual(calls.filter((a) => a[0] === "run").map((a) => a.at(-1)), ["baseline", "bound", "bound", "rollback"]);
    assert.equal(calls.filter((a) => a[0] === "rm").length, 4);
    assert.equal(report.baseline.sha256, report.rollback.sha256);
    assert.equal(Object.keys(report.sourceHashes).length, 3);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("bind: Docker failures retain NON-PASS evidence, hide raw logs, and never delete a foreign label", () => {
  const dir = mkdtempSync(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? tmpdir(), "settings-bind-test-"));
  const calls = [];
  const spawn = (_, args) => {
    calls.push(args);
    if (args[0] === "image") return { status: 0, stdout: JSON.stringify(config) };
    if (args[0] === "inspect") return { status: 0, stdout: "someone-else\n" };
    return { status: 1, stdout: "secret-canary", stderr: "secret-canary" };
  };
  try {
    assert.throws(() => runRehearsal({ image: IMAGE, workRoot: dir, acknowledged: true }, {}, spawn, makeLocalFixture), (e) => {
      assert(!e.message.includes("secret-canary"));
      const match = e.message.match(/evidence: (.*?);/); assert(match);
      const report = JSON.parse(readFileSync(match[1])); assert.equal(report.passed, false); assert.equal(report.results.length, 0);
      assert(!JSON.stringify(report).includes("secret-canary")); return true;
    });
    assert(!calls.some((a) => a[0] === "rm"));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("bind: arbitrary live-like home without fixture marker refuses before settings access", async () => {
  const dir = mkdtempSync(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR ?? tmpdir(), "settings-bind-test-"));
  try { await assert.rejects(probeFixture(dir, "bound")); }
  finally { rmSync(dir, { recursive: true, force: true }); }
});
