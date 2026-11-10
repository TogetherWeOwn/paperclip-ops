# Portable plugin transaction and checked rollback

`cliproxy_plugin_deploy.sh` is a source-only Bash entry point backed by the sibling
Python module. It ships no credentials, operational adapter, service names,
network probes, restart commands, installation hook or service integration.
Source review and synthetic CI are not deployment qualification or permission to
run it against a service. Requirements: Linux, Bash, Python 3.11+, `cp` and `chmod`
and `rm` with GNU-compatible options. No Python packages are installed.

## Entry points and outcomes

| Command | Meaning | Exit |
| --- | --- | --- |
| `STATE_DIR=... bash cliproxy_plugin_deploy.sh run` | Prepare a new deployment and apply it through a separately reviewed adapter | 0 verified; 3 preflight refusal before service data changes; 1 failure |
| `STATE_DIR=... bash cliproxy_plugin_deploy.sh rollback` | Restore the deployment recorded in that private state directory | 0 checked restoration; 1 refused/incomplete |
| `STATE_DIR=... bash cliproxy_plugin_deploy.sh watchdog` | One delayed check of the recorded transaction | 0 checked terminal success; 1 failure; 75 busy |
| invalid arguments | Usage only | 2 |

All three entry points return **75** if another transaction holds either the state
or service lock. Contention is not completion. Retry only after the holder exits;
the new invocation acquires both locks and checks the actual disk state. No
invocation claims the other process completed successfully.

`run` starts one detached watchdog before applying changes (`WD_SECS`, default
600; integer range 0–86400). This is a per-invocation safety check, not an installed
scheduler or controller. The child receives only `PATH`, `STATE_DIR` and
`WD_SECS`; its output goes to private `watchdog.log`. There is intentionally no
process-group kill based on a stale PID file. A watchdog cannot take over a held
lock: it exits 75 rather than terminating unrelated processes or racing a live
writer. A stuck writer therefore requires separately authorized diagnosis; this
wrapper does not promise recovery from a hung process. External invocation must
retain and inspect the watchdog exit/log, not treat launch as a successful check.

## Inputs and adapter boundary

`STATE_DIR` must be an existing, private mode-0700 directory owned by the invoking
user, outside the service tree. Use a fresh directory for each `run`; reusing a
prepared directory is refused without rewriting its state or backup. No default
service or state location is guessed.

The forward path additionally requires these environment values:

- `S`: absolute canonical service directory, containing `config.yaml`.
- `AUTH_REL`, `ARTIFACT_REL`: distinct relative destination files below `S`;
  parents must already exist. Neither may be the config file or use the reserved
  top-level `.cliproxy-plugin-owner` or `.cliproxy-plugin.lock` paths.
- `ARTIFACT_SRC`, `SO_SHA`: release file and its lowercase 64-character SHA-256.
- `DEPLOY_ADAPTER`: absolute executable path to a separately reviewed adapter.

Paths must be canonical, without symlink components. Directories must not be
writable by other users. Only root-owned sticky ancestors are allowed for
fixtures; service/state directories and target parents themselves must never be
group- or world-writable, even with the sticky bit. Ancestors must be owned by
root or the invoking user. The service/state directories and target parents must
be owned by the invoking user. Run under the already-authorized service identity: this wrapper
does not call `sudo` or change permissions on a host to gain access. It assumes
other writers cooperate with the service lock; it is not a sandbox against a
malicious same-user process or a privileged administrator.

The adapter receives **`<action> <absolute-state-directory>`** as separate argv
values, without a shell. Each action has a 200-second timeout. It must be trusted,
immutable for the transaction lifetime and independently qualified. No production
adapter is included, and the fixture adapter must never be used operationally.

| Action | Required adapter behavior |
| --- | --- |
| `preflight` | Read-only checks: service available, plugin config absent, credential accessible, baseline counts captured to private state; return nonzero on any missing prerequisite |
| `render` | Write `config.yaml.new` in state, mode 0600; require exactly one insertion anchor and YAML equivalence to the saved original after removing the one new plugin key |
| `auth` | Return one JSON object on stdout via a private pipe; never print its credential to stderr, argv, environment, or logs |
| `models-baseline` | Require baseline model listing, with bounded probes |
| `route` | Require baseline route stream and multiturn probes |
| `verify` | Bounded plugin-loaded/registered/no-panic log checks, required model listing, plugin plain/stream/tool/multiturn probes, and auth total-before+1/disabled-count-unchanged checks |
| `restart` | Separately authorized bounded recovery operation; return nonzero if restart fails |

All actions except `render` and the explicitly authorized `restart` must avoid
mutating service data. `verify`, baseline, and route checks return only an exit
status; the wrapper discards helper output. The wrapper does not implement or
claim YAML semantic validation, service readiness, model identity, count parsing,
or restart qualification itself. Missing helper implementations are a real
operational prerequisite, not a reason to use a permissive adapter. It has no
management-API client. Credentials returned by `auth` stay in process memory
until exclusively written to the designated mode-0600 auth file, never a state
backup or a command argument. The environment used to invoke the forward path
must itself be clean; helpers are trusted code, not untrusted plugins.

## Transaction identity and private state

Preparation saves a mode-0600 byte copy as `config.yaml.orig`, asks the adapter for
`config.yaml.new`, and saves `deployment.json` containing schema version 1, a
random deployment ID, canonical root/destination/adapter paths and hashes of the
original/rendered configs, auth record and artifact. The wrapper fsyncs the
validated rendered file before committing preparation. Manifest writing and
loading share a 16-KiB serialized-byte limit: oversized records are refused before
committing a manifest or changing service data, including paths expanded by JSON
Unicode escaping. The service's
`.cliproxy-plugin-owner` binds that ID to the manifest digest. It prevents an old
state directory from rolling back a newer deployment. These private records are
not public artifacts: paths, hashes and config backups may be sensitive. Do not
attach them to a public issue or commit them.

Each entry point first locks `STATE_DIR/.cliproxy-plugin.lock`, before reading or
writing transaction records, then locks the service-wide `.cliproxy-plugin.lock`.
This serializes both shared-state/different-service and shared-service/different-
state invocations. Lock acquisition is nonblocking and consistently state-first;
existing lock inodes must be regular, invoking-user-owned, mode 0600 and have only
one link. No lock file is deleted to steal ownership. Preparation checks the actual
saved backup against the initial config digest before committing its manifest.
The new owner marker supersedes prior deployments only after preparation. `run`
rechecks ownership before any forward data write. It writes and fsyncs temporary
auth/artifact files, then publishes them with exclusive hard links (never replacing
an existing destination). A write/flush/fsync failure before publication leaves no
partial destination. This requires same-filesystem hard-link support; unsupported
publication fails rather than falling back to an unsafe copy. It then atomically
replaces config with the rendered bytes, checks adapter verification and verifies
all recorded hashes before `verified`.

Rollback ignores forward-path environment overrides and loads the saved manifest.
Before any rollback mutation it requires:

1. Owner ID and manifest digest still match; saved original/rendered bytes match
   their recorded digests.
2. Live config is exactly the recorded original or rendered config, not a later
   unrelated edit.
3. Each auth/artifact is either absent or a regular, non-symlink file with the
   deployment's exact content hash. A changed file is not deleted.

Identity here is content plus the service ownership record, not inode identity.
A same-content replacement is deliberately treated as the same material. It does
not establish a cryptographic chain of trust for an untrusted manifest or adapter.
Do not edit records or backups to force a refusal through.

## What `rolledback` proves

The rollback checks every mutation return code, independently of health:

1. Remove auth and require absence, including absence of dangling symlinks.
2. Copy the saved config into a same-filesystem temporary file; require successful
   copy and chmod, byte equality and exact mode 0600 before replacing live config.
3. Remove the artifact; require both files absent and live config equal/mode 0600.
4. Require baseline models and routes. If unhealthy, require adapter restart
   success and then both health checks. There is no swallowed restart failure.
5. Recheck ownership and all disk postconditions after helpers, fsync both removal
   parent directories, then durably write `rolledback`. A directory-sync failure
   is a rollback failure. Repeated rollback does the same checks: a marker alone proves
   nothing and cannot hide drift or an incorrect mode.

`state` contains one of `preflight`, `aborted-unchanged`, `applying`, `loading`,
`verified`, `rolling-back`, `rolledback`, `rollback-FAILED`. State replacement uses
private temporary files, file fsync, atomic replace and directory fsync. Mutation,
postcondition, helper, or state-write failure returns nonzero; the wrapper tries
to persist `rollback-FAILED`. **Partial failure is not automatically undone.**
For example, auth may already be absent while config restoration failed and the
artifact remains. Retain private state and reconcile that exact deployment via
an authorized recovery procedure. Abrupt termination can leave transaction
temporaries; these are not published auth/artifact destinations and require
private reconciliation. Identity drift still refuses rollback rather than guessing
ownership or deleting unrelated material.
If storage cannot persist even the failure state, the previous word may remain;
state text is never sufficient evidence without the process exit and fresh disk
checks. Forward failure returns 1 even if its rollback subsequently succeeds.
The watchdog propagates rollback failure and reports `rollback-FAILED` as failure,
not a successful terminal state. A `verified` watchdog also rechecks identities.
A `rolledback` watchdog returns 0 for historical completion without rechecking or
mutating service data, invoking helpers, or rewriting the state. That result is
not a fresh disk/health qualification; use an explicitly authorized manual rollback
for a current recheck, which still refuses superseded ownership.

## Verification and limits

GitHub Actions runs `bash test_cliproxy_plugin_deploy.sh`, which delegates to
`test_cliproxy_plugin_deploy.py`. Fixtures use only dummy files and injected
commands/helpers in a disposable private directory; detached watchdog launch is
stubbed. No container, credential, live probe or network fixture is needed.

Coverage includes failing and successful-no-op copy/chmod/removals, equality and
mode checks, state persistence/replace failures, lock contention followed by
retry, repeat rollback, identity drift, symlink refusal, health failure, forward
failure, watchdog exit propagation and successful restoration. Additional fixtures
cover state-directory contention across service roots, state-locked recovery,
sticky target and insecure lock refusal, removal-directory sync ordering/failure,
rendered-file fsync failure, reserved destinations, oversized Unicode manifests,
a nonzero restart action, completed-rollback watchdog isolation, preflight/preapply
config drift, and forward-copy persistence failure before publication. These injected persistence checks are not a real
power-loss/storage qualification, and detached watchdog lifecycle/environment
remains untested end to end. The unsafe control
is a clearly labeled **synthetic reconstruction**, not execution of any original
operational script: green routes and ignored mutations produce a success marker
that the corrected filesystem oracle rejects. There is no claimed pre-fix Git
execution or production rollback drill.

For a single failing-test reproduction only, the entry point accepts unittest
names, for example:

```sh
bash test_cliproxy_plugin_deploy.sh RollbackTests.test_success_and_repeated_rollback
```

Full suites and CI mirrors belong in GitHub Actions, not on a shared host.
Publishing this source does not install or authorize an adapter, a watchdog,
a deployment, a rollback, a restart or any service operation.
