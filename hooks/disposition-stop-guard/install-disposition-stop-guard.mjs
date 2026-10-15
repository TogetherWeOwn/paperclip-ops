#!/usr/bin/env node
// Installer for the Disposition Stop guard. Operator-run; dry run by default.
//
//   node install-disposition-stop-guard.mjs status
//   node install-disposition-stop-guard.mjs install                         show the merged settings (dry run)
//   node install-disposition-stop-guard.mjs install --apply --i-understand-this-reaches-every-agent
//   node install-disposition-stop-guard.mjs uninstall --apply
//
// Flags: --settings <file>   default $HOME/.claude/settings.json (the file every agent on this box reads)
//        --script <file>     default disposition-stop-guard.mjs beside this installer (host filesystem
//                            path: existence + agent-owned checks run against it)
//        --hook-command <cmd> override for the command written into settings (container path the
//                            agents load, e.g. "node /paperclip/disposition-stop-guard/disposition-stop-guard.mjs").
//                            Default is hookCommandFor(scriptPath). An explicitly passed but empty or
//                            valueless --hook-command is refused; the default applies only when the flag is absent.
//        --container-prefix <p> + --host-prefix <p> map the container path to the host path for the
//                            existence check (e.g. --container-prefix /paperclip
//                            --host-prefix /home/ubuntu/stacks/paperclip/data). Must be passed together;
//                            both must be absolute. When absent the hook path is checked as-is.
//
// SCOPE. The default settings file is user scope: every claude_local agent reads it. That is
// intentional, because the guard is for every agent, and it is safe because the hook allows every
// stop unless the run has a task, its card is in_progress or todo, and that card has no live path.
// It is still a fleet-wide change, so --apply on that file needs the explicit acknowledgement flag,
// as the Muse guard installer does. Pass --settings to point at a pilot workspace's
// .claude/settings.json instead.
//
// WHAT IT TOUCHES. One thing: our entry under hooks.Stop. Every other hook event, every other Stop
// entry and every unrelated setting is left as it was; install proves that by comparing the file
// with our entry removed before and after. A settings file that is not valid JSON is refused, never
// overwritten.

import { copyFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

/** How we recognise our own entry on re-install and uninstall. */
export const HOOK_MARKER = "disposition-stop-guard.mjs";
/** Seconds. Larger than the hook's own 8 s deadline so the hook fails open on time, not the CLI. */
export const HOOK_TIMEOUT_SECONDS = 15;
export const ACK_FLAG = "i-understand-this-reaches-every-agent";

export function hookCommandFor(scriptPath) {
  return `node ${scriptPath}`;
}

/**
 * case_5d081dcf: the host filesystem path (--script) is not the command the agents load. A host
 * install copies the package to a shared DEST (host path) but the hook runs inside the
 * container, so the written command must be the container path
 * (`node /paperclip/disposition-stop-guard/disposition-stop-guard.mjs`). An explicit
 * --hook-command wins; otherwise the default stays hookCommandFor(scriptPath).
 *
 * Fail-closed on empty (second 4/5 review): only an absent flag (undefined) falls back to
 * the default. An explicitly passed empty string (or any non-string the CLI normalises,
 * e.g. a valueless `--hook-command --apply` parsed as `true`) is returned as-is so the
 * shape check refuses it; main() also refuses it up front with exit 2.
 */
export function resolveHookCommand({ scriptPath, hookCommand }) {
  if (hookCommand === undefined) return hookCommandFor(scriptPath);
  return hookCommand;
}

/**
 * Map a container hook path to the host path the installer can stat. Pure, so the host
 * install (case_5d081dcf: installer runs on the host, hook runs in the container) stays
 * testable. When both prefixes are non-empty strings and hookPath is the container prefix
 * or under it, the remainder is appended to the host prefix; otherwise hookPath is
 * returned unchanged. Trailing slashes on the prefixes are ignored (except root "/").
 */
export function mapContainerPathToHost(hookPath, { containerPrefix, hostPrefix } = {}) {
  const path = String(hookPath ?? "");
  if (typeof containerPrefix !== "string" || containerPrefix.length === 0) return path;
  if (typeof hostPrefix !== "string" || hostPrefix.length === 0) return path;
  const strip = (prefix) => (prefix.length > 1 ? prefix.replace(/\/+$/, "") : prefix);
  const container = strip(containerPrefix);
  const host = strip(hostPrefix);
  if (path === container) return host;
  if (path.startsWith(`${container}/`)) return `${host}${path.slice(container.length)}`;
  return path;
}

/** The host-visible path whose existence the installer checks (mapped when prefixes apply). */
export function hookExistencePathFor(hookPath, { containerPrefix, hostPrefix } = {}) {
  return mapContainerPathToHost(hookPath, { containerPrefix, hostPrefix });
}

/** The filesystem path embedded in a hook command (the token carrying HOOK_MARKER), else the raw command. */
export function hookScriptPathFromCommand(command) {
  const tokens = String(command ?? "")
    .split(/\s+/)
    .map((token) => token.replace(/^['"]+|['"]+$/g, ""))
    .filter(Boolean);
  return tokens.find((token) => token.includes(HOOK_MARKER)) ?? String(command ?? "");
}

/**
 * Shape problems for the command written into settings. Strict on purpose
 * (case_5d081dcf review): the path must be absolute and its file name exactly
 * HOOK_MARKER, so `disposition-stop-guard.mjs.bak` and relative paths are
 * refused instead of installing a fail-open hook.
 */
export function hookCommandPathProblems(command) {
  const problems = [];
  const hookPath = hookScriptPathFromCommand(command);
  if (!isAbsolute(hookPath)) {
    problems.push(`${command} must point at an absolute path; got ${hookPath}.`);
  }
  if (basename(hookPath) !== HOOK_MARKER) {
    problems.push(`${command} must end with ${HOOK_MARKER}; refusing to write a hook command the guard cannot recognise.`);
  }
  return { hookPath, problems };
}

const ownsEntry = (entry) =>
  (entry?.hooks ?? []).some((hook) => String(hook?.command ?? "").includes(HOOK_MARKER));

/** Merge our Stop entry in. Pure; re-installing replaces our entry instead of adding a second. */
export function mergeInstall(settings, command) {
  const next = structuredClone(settings ?? {});
  next.hooks ??= {};
  const current = Array.isArray(next.hooks.Stop) ? next.hooks.Stop : [];
  next.hooks.Stop = [
    ...current.filter((entry) => !ownsEntry(entry)),
    { hooks: [{ type: "command", command, timeout: HOOK_TIMEOUT_SECONDS }] },
  ];
  return next;
}

/** Remove our entry and any container it leaves empty, so uninstall is byte-comparable with the start. */
export function removeInstall(settings) {
  const next = structuredClone(settings ?? {});
  if (!next.hooks) return next;
  const foreign = (Array.isArray(next.hooks.Stop) ? next.hooks.Stop : []).filter((entry) => !ownsEntry(entry));
  if (foreign.length > 0) next.hooks.Stop = foreign;
  else delete next.hooks.Stop;
  if (Object.keys(next.hooks).length === 0) delete next.hooks;
  return next;
}

export function isInstalled(settings) {
  return Array.isArray(settings?.hooks?.Stop) && settings.hooks.Stop.some(ownsEntry);
}

/** A fleet-wide hook must not point into one agent's workspace; the gate would silently die with it. */
export function isAgentOwnedPath(scriptPath) {
  const path = resolve(scriptPath);
  return (
    /[/\\]instances[/\\][^/\\]+[/\\](workspaces|worktrees)[/\\]/.test(path) ||
    // Per-agent instruction and file-sync trees: .../instances/<i>/companies/<c>/(acp-engine/)agents/<id>/
    /[/\\]instances[/\\].*[/\\]agents[/\\][^/\\]+[/\\]/.test(path)
  );
}

export function isUserScope(settingsFile, home = homedir()) {
  return resolve(settingsFile) === resolve(join(home, ".claude", "settings.json"));
}

/**
 * True when this process looks like an agent run rather than the operator's
 * host shell. case_8ad2150d: a fleet-wide hook entry was written from an
 * agent-run context on 2026-10-03, so user-scope --apply refuses here.
 * Tripwire-grade, not a boundary: the real boundary is host file ownership
 * (operator follow-up). `env` is a parameter so the check stays testable.
 */
export function isAgentRunContext(env = process.env) {
  return Boolean(env.PAPERCLIP_RUN_ID || env.PAPERCLIP_AGENT_ID);
}

/** Refusal text when an agent run targets the user-scope file, else null. */
export function agentContextRefusal({ settingsFile, home, agentRun }) {
  if (agentRun && isUserScope(settingsFile, home)) {
    return (
      `${settingsFile} is read by every agent on this box, and this looks like an agent run ` +
      `(PAPERCLIP_RUN_ID/PAPERCLIP_AGENT_ID is set). Fleet-wide hook changes run from the ` +
      `operator/host shell, never from an agent run. Dry run and status still work here.`
    );
  }
  return null;
}

/** Every refusal that must clear before a write. Pure, so each is testable without a real file. */
export function checkApply({ settingsFile, scriptPath, hookCommand, acknowledged, scriptExists, hookExists, home, agentRun = false, containerPrefix, hostPrefix, hookExistencePath }) {
  const problems = [];
  const contextRefusal = agentContextRefusal({ settingsFile, home, agentRun });
  if (contextRefusal) problems.push(contextRefusal);
  if (isUserScope(settingsFile, home) && !acknowledged) {
    problems.push(`${settingsFile} is read by every agent on this box. Re-run with --${ACK_FLAG} if that is the intent.`);
  }
  if (isAgentOwnedPath(scriptPath)) {
    problems.push(`${scriptPath} lives inside one agent's workspace. Copy the package to a shared path and pass --script.`);
  }
  if (basename(resolve(scriptPath)) !== HOOK_MARKER) {
    problems.push(`${scriptPath} must end with ${HOOK_MARKER}; pass the host path of the installed package with --script.`);
  }
  const hasContainer = containerPrefix !== undefined;
  const hasHost = hostPrefix !== undefined;
  if (hasContainer !== hasHost) {
    problems.push(`--container-prefix and --host-prefix must be passed together; got only one.`);
  } else if (hasContainer && hasHost) {
    if (typeof containerPrefix !== "string" || containerPrefix.length === 0 || typeof hostPrefix !== "string" || hostPrefix.length === 0) {
      problems.push(`--container-prefix and --host-prefix need values; pass both absolute paths.`);
    } else if (!isAbsolute(containerPrefix) || !isAbsolute(hostPrefix)) {
      problems.push(`--container-prefix and --host-prefix must both be absolute paths; got ${containerPrefix} and ${hostPrefix}.`);
    }
  }
  const resolved = resolveHookCommand({ scriptPath, hookCommand });
  problems.push(...hookCommandPathProblems(resolved).problems);
  const hookPath = hookScriptPathFromCommand(resolved);
  if (isAgentOwnedPath(hookPath)) {
    problems.push(`${resolved} points inside one agent's workspace. Pass a shared container path with --hook-command.`);
  }
  if (!scriptExists) problems.push(`${scriptPath} does not exist; the hook would fail open and guard nothing.`);
  if (hookExists === false) {
    const checked = hookExistencePath ?? hookExistencePathFor(hookPath, { containerPrefix, hostPrefix });
    if (checked !== hookPath) problems.push(`${hookPath} does not exist (checked ${checked} via --container-prefix/--host-prefix); the hook would fail open and guard nothing.`);
    else problems.push(`${hookPath} does not exist; the hook would fail open and guard nothing.`);
  }
  return { ok: problems.length === 0, problems };
}

/** Everything except our own Stop entry, so install/uninstall can prove they touched nothing else. */
export function withoutOurEntry(settings) {
  return removeInstall(settings);
}

function readSettings(file) {
  if (!existsSync(file)) return {};
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    throw new Error(`${file} is not valid JSON (${error.message}); refusing to overwrite it`);
  }
}

function parseArgs(argv) {
  const [command = "status", ...rest] = argv;
  const valueFlags = new Set(["settings", "script", "hook-command", "container-prefix", "host-prefix"]);
  const booleanFlags = new Set(["apply", ACK_FLAG]);
  const refuse = (message) => {
    throw Object.assign(new Error(`refused: ${message}`), { exitCode: 2 });
  };
  const flags = {};
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i];
    if (!token.startsWith("--")) refuse(`unexpected argument ${token}`);
    const key = token.slice(2);
    if (!valueFlags.has(key) && !booleanFlags.has(key)) refuse(`unknown flag ${token}`);
    if (Object.hasOwn(flags, key)) refuse(`duplicate flag ${token}`);
    if (booleanFlags.has(key)) {
      flags[key] = true;
      continue;
    }
    const next = rest[i + 1];
    if (next === undefined || next.length === 0 || next.startsWith("--")) refuse(`${token} needs a value`);
    flags[key] = next;
    i += 1;
  }
  return { command, flags };
}

function writeAtomic(file, rendered) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  let backup = null;
  if (existsSync(file)) {
    backup = `${file}.pre-disposition-stop-guard-${stamp}.bak`;
    copyFileSync(file, backup);
  }
  const temporary = `${file}.disposition-stop-guard-tmp`;
  writeFileSync(temporary, rendered, { mode: 0o644 });
  JSON.parse(readFileSync(temporary, "utf8"));
  renameSync(temporary, file);
  return backup;
}

function main() {
  const { command, flags } = parseArgs(process.argv.slice(2));
  // Fail-closed on an explicitly passed but empty or valueless --hook-command: the default
  // applies only when the flag is absent. Without this, `--hook-command ""` or a bare
  // `--hook-command --apply` (parsed as `true`) silently fell back to hookCommandFor(scriptPath),
  // the host path this PR exists to stop writing.
  if (Object.hasOwn(flags, "hook-command")) {
    const raw = flags["hook-command"];
    if (typeof raw !== "string" || raw.length === 0) {
      process.stderr.write(`refused: --hook-command needs a value; pass the container command, e.g. "node /paperclip/disposition-stop-guard/disposition-stop-guard.mjs"\n`);
      process.exit(2);
    }
  }
  const hasContainerPrefix = Object.hasOwn(flags, "container-prefix");
  const hasHostPrefix = Object.hasOwn(flags, "host-prefix");
  if (hasContainerPrefix !== hasHostPrefix) {
    process.stderr.write(`refused: --container-prefix and --host-prefix must be passed together\n`);
    process.exit(2);
  }
  let containerPrefix;
  let hostPrefix;
  if (hasContainerPrefix && hasHostPrefix) {
    containerPrefix = flags["container-prefix"];
    hostPrefix = flags["host-prefix"];
    if (typeof containerPrefix !== "string" || containerPrefix.length === 0 || typeof hostPrefix !== "string" || hostPrefix.length === 0) {
      process.stderr.write(`refused: --container-prefix and --host-prefix need values; pass both absolute paths\n`);
      process.exit(2);
    }
    if (!isAbsolute(containerPrefix) || !isAbsolute(hostPrefix)) {
      process.stderr.write(`refused: --container-prefix and --host-prefix must both be absolute paths; got ${containerPrefix} and ${hostPrefix}\n`);
      process.exit(2);
    }
  }
  const settingsFile = resolve(
    typeof flags.settings === "string" ? flags.settings : join(homedir(), ".claude", "settings.json"),
  );
  const scriptPath = typeof flags.script === "string" ? resolve(flags.script) : join(HERE, "disposition-stop-guard.mjs");
  const hookCommand = resolveHookCommand({
    scriptPath,
    hookCommand: Object.hasOwn(flags, "hook-command") ? flags["hook-command"] : undefined,
  });
  const apply = flags.apply === true;
  const current = readSettings(settingsFile);
  const out = (value) => process.stdout.write(`${typeof value === "string" ? value : JSON.stringify(value, null, 2)}\n`);

  const hookPath = hookScriptPathFromCommand(hookCommand);
  const hookExistencePath = hookExistencePathFor(hookPath, { containerPrefix, hostPrefix });
  const hookExists = existsSync(hookExistencePath);

  if (command === "status") {
    out({
      settingsFile,
      scope: isUserScope(settingsFile) ? "user" : "project",
      installed: isInstalled(current),
      stopEntries: (current.hooks?.Stop ?? []).length,
      otherHookEvents: Object.keys(current.hooks ?? {}).filter((event) => event !== "Stop"),
      script: scriptPath,
      hookCommand,
      hookScript: hookPath,
      hookExistencePath,
      containerPrefix: containerPrefix ?? null,
      hostPrefix: hostPrefix ?? null,
      scriptExists: existsSync(scriptPath),
      hookExists,
      killSwitchPresent: existsSync(join(dirname(scriptPath), "DISABLED")),
    });
    return;
  }

  if (command !== "install" && command !== "uninstall") {
    process.stderr.write(`unknown command ${command}; use status, install or uninstall\n`);
    process.exit(2);
  }

  if (command === "install") {
    const shape = hookCommandPathProblems(hookCommand);
    if (shape.problems.length > 0) {
      for (const problem of shape.problems) process.stderr.write(`refused: ${problem}\n`);
      process.exit(1);
    }
  }

  const next = command === "install" ? mergeInstall(current, hookCommand) : removeInstall(current);
  // Prove we changed nothing but our own entry.
  if (JSON.stringify(withoutOurEntry(next)) !== JSON.stringify(withoutOurEntry(current))) {
    throw new Error("refusing to write: the merge would change something other than our own Stop entry");
  }
  const rendered = `${JSON.stringify(next, null, 2)}\n`;

  if (!apply) {
    out(rendered);
    process.stderr.write(`dry run: nothing written to ${settingsFile}; add --apply to write it\n`);
    return;
  }

  if (command === "uninstall") {
    // Removing the fleet-wide guard is as much a fleet-wide change as adding it: not from an agent run.
    const refusal = agentContextRefusal({ settingsFile, home: homedir(), agentRun: isAgentRunContext() });
    if (refusal) {
      process.stderr.write(`refused: ${refusal}\n`);
      process.exit(1);
    }
  }

  if (command === "install") {
    const verdict = checkApply({
      settingsFile,
      scriptPath,
      hookCommand,
      acknowledged: flags[ACK_FLAG] === true,
      scriptExists: existsSync(scriptPath),
      hookExists,
      home: homedir(),
      agentRun: isAgentRunContext(),
      containerPrefix,
      hostPrefix,
      hookExistencePath,
    });
    if (!verdict.ok) {
      for (const problem of verdict.problems) process.stderr.write(`refused: ${problem}\n`);
      process.exit(1);
    }
  }

  const backup = writeAtomic(settingsFile, rendered);
  const verified = readSettings(settingsFile);
  if (isInstalled(verified) !== (command === "install")) {
    throw new Error(`${command} did not survive settings read-back; restore ${backup ?? "the original"}`);
  }
  out({ [command === "install" ? "installed" : "uninstalled"]: settingsFile, backup });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exit(error.exitCode ?? 1);
  }
}
