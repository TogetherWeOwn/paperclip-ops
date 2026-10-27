# op-request wrapper source

Versioned production source for the host wrapper installed at
`/usr/local/sbin/op-request` (approval-queue publication, Part B groundwork B3).
The source carries generic example paths (`LOG`, `QUEUE`) and is installed byte-for-byte against a pinned digest. Never commit host-specific paths to this repository, and never edit the source or its pin on a host. A host whose real audit-log or queue location differs from the source stops at the preflight below until a reviewed source revision for that layout exists.

## Provenance

- Negative detector (`tests/fixtures/op-request-known-bad.sh`): SHA256
  `30b9f2ff45d2cc54fa8cb52da7da7436ef49029e789d690c528a847035f30e6d`.
  Sanitized fixture preserving the old false-success chain for the red
  truthful suite below; it is never installed.
- Fixed source (`op-request` in this directory): SHA256
  `450aa7d7e569c9df96f28ea4facc4309910ff57cb2935da2eb16c0a55b9708c9`.
  Publication and its success signals are one fail-closed unit with
  post-publication no-retry diagnostics plus an incomplete-publication
  diagnostic when DEST already exists after a `chown`/`chmod` (or partial
  `cp`) failure (see below). SRC is resolved exactly once (single
  `O_NOFOLLOW|O_NONBLOCK` open; type, size, owner and JSON taken from that
  fd); the validated bytes are written to a private staging snapshot next to
  SRC and DEST is copied from the already-open staging fd, then read back
  against the snapshot with a bounded read, so bytes rewritten after
  validation can never substitute unvalidated bytes. Every validation refusal is preserved (not-a-file,
  too-large, not-operator-owned, invalid JSON and key checks, all exit 3).
  A symlinked or non-object-JSON SRC is refused with exit 3, refusals raised
  in the python step append an `op-request REFUSED` audit line (see below),
  and `queue-missing` is checked before SRC validation. The strict manifest
  `op-request.sha256` in this directory pins this digest (`64 lowercase hex,
  two spaces, op-request, newline`); the fixed suite refuses a
  missing/malformed/mismatched manifest before any execution, so no test-file
  edit is needed when the source hash changes.

## Behavior contract

- Validation refusals keep exit 3, including a symlinked SRC, a non-regular
  SRC (a FIFO is refused immediately because the open is non-blocking, a
  directory or missing path is `not-a-file`), an oversized SRC, an SRC not
  owned by `paperclip-operator` (or when that account is unknown), and a
  JSON body that is not an object; usage keeps exit 2. Every refusal
  appends one `op-request REFUSED <reason>` line to the audit log; if that
  append itself fails the refusal still exits 3 and stderr reports the lost
  audit line. A failed copy exits 10 into the publication-failure branch
  below. Any other python-step exit (a crash) exits 1 as an internal error
  with its own diagnostic, never as a publication failure; if a DEST was
  left behind, stderr adds the unknown-state no-retry instruction.
- Any publication-step failure (`cp`/`chown`/`chmod`) exits **1** without
  printing `queued ` or attempting the `op-request ok` log append.
  When DEST already exists (full bytes after `chown`/`chmod` failure,
  truncated bytes after a partial `cp`, or bytes that do not match the
  validated snapshot), it is kept for operator inspection: a `FAILED` line
  is appended to the audit log and stderr adds
  `publication incomplete (metadata not applied) dest=$DEST` with the full
  path, the audit outcome (`recorded`/`unconfirmed`), and explicit no-retry
  plus operator inspect/reconcile instructions. A `cp` failure with no DEST
  keeps the single `FAILED` line only, with no `publication incomplete`
  text and no audit append.
- Only the validated snapshot is ever published: SRC is opened once with
  `O_NOFOLLOW|O_NONBLOCK`, type/size/owner/JSON are taken from that fd (the
  owner is compared with `id -u paperclip-operator` before any byte is
  read), the validated bytes are written to a private staging snapshot next
  to SRC, the copy reads from the already-open staging fd (the SRC inode is
  never re-read, and staging is unlinked once the copy returns), and DEST is
  read back against the snapshot with a bounded read before `chown`/`chmod`.
  Anything else is a publication failure, never a `queued` claim.
- An ok-log append failure also exits **1** without printing `queued `.
  Its stderr states publication is complete, names the published DEST,
  states audit unconfirmed, and instructs no-retry plus operator
  inspection/reconciliation.
- `queued $DEST` prints only after the append command for the `ok` line
  succeeds. This is not an `fsync` or a crash-durability guarantee. A failed
  append can also leave partial log bytes; it does not undo publication.
- A lost final stdout (EPIPE) after publication and append exits nonzero
  (SIGPIPE is ignored so the failure is catchable). Its stderr states
  publication complete and audit complete, names the DEST, and instructs
  no-retry plus operator inspection/reconciliation. The `request-ok`
  validation line already emitted stays the only stdout.
- One successful invocation publishes one DEST file, identical bytes, owner
  `root:paperclip-operator`, mode `0640`, one `queued` line, one `ok` log line.
  This is not an exactly-once or idempotent retry protocol.
- No hidden cleanup: a partially or fully published DEST is left in place for
  operator inspection (cleanup was explicitly not authorized). Log failure
  happens **after** publication; lost stdout can happen after publication and
  the log append. Nonzero exit, missing `queued`, interruption, or lost output
  is therefore **not proof of an absent request**. Never automatically retry,
  replay, or remove a request on that evidence. The existing authorized host
  Operator must reconcile its identity and disposition first, without changing
  queue permissions. This repair does not add deduplication or an executor.

## Tests

- `test_op_request_publication_truthful.sh` — Test Author red suite on the
  sanitized known-bad fixture (pass=3, fail=4 by design). Offline evidence
  only; never a CI gate.
- `test_op_request_publication_fixed.sh` — thin wrapper over
  `tests/op_request_publication.py` against this fixed source plus its
  strict manifest; all 20 pass (P0/P1/P2, F1–F4, post-publication F5/F6,
  F7 partial-cp, F8 corrupt-cp, F9/F10 metadata failure plus unwritable
  audit, F11 symlink-swap loop, F12 operator-ownership, F13 FIFO, F14
  non-object JSON, F15 python-step refusal audit lines, F16 refusal with
  unwritable audit, F17 python-step crash). Runs in CI (`suites` job, core
  area). Run locally with
  `env -u BASH_ENV bash test_op_request_publication_fixed.sh`
  (the `env -u BASH_ENV` prefix is load-bearing: without it the container
  PATH reset voids the fake-command harness). Provide a private writable
  `TMPDIR` when the environment has no run scratch directory.
- `tests/test_op_request_harness.py` — 13 fail-closed harness regressions
  (mktemp/provenance/rewrite containment). Runs in CI.
- `tests/test_op_request_swap.py` — 9 dummy-file atomic install/rollback
  regressions extracted from the marked runbook block below. Runs in CI.
  Both entrypoints share `tests/op_request_harness.py`; fake privileged
  commands live in `tests/op_request_fake_command.py`. Suites refuse real
  root execution.
- `tests/test_op_request_snapshot_acceptance.py` — 5 validated-snapshot
  regressions: the happy-path control, same-inode rewrite and
  post-validation growth publishing only validated bytes, a bounded
  destination read-back, and the nested executable README selecting core CI.
  Runs in CI with `--artifact-root` pointing at the checkout; the
  `snapshot_probe` read observer is confined to that suite's step
  environment. Suites refuse real root execution.

## Install manifest (for the host Operator; agents must not install)

| Item | Value |
|---|---|
| Canonical source | `ops/operator-safety/wrappers/op-request` at the reviewed merge commit |
| Source SHA256 | `450aa7d7e569c9df96f28ea4facc4309910ff57cb2935da2eb16c0a55b9708c9` |
| Installed target | `/usr/local/sbin/op-request` |
| Replaces | digest of the installed wrapper, supplied by the Operator; the recipe refuses on mismatch |

## Install / rollback (root Operator only, after all merge gates)

**Do not run this from an agent or against a live host during source review.**
The existing host Operator owns one serialized replacement window: no other
writer may change the target, its directories, or the backup during the window.
Use a fresh Bash process with `BASH_ENV` unset, not a sourced or conditional
shell fragment. The target parent and `/root` must be real, root-owned,
non-group/world-writable directories with trusted ancestors.

Preflight before install: confirm the `LOG=` and `QUEUE=` lines in the reviewed source match the target host and that the `LOG` directory is writable by the wrapper. If either differs, stop. Do not edit the source and do not re-pin. A wrong `LOG` path makes publication succeed while the wrapper reports a failed audit append (the header documents that a failed append does not undo publication).

For install, run the **entire** block with `ACTION=install` and set `SOURCE` to
the clean, reviewed merge-commit clone. For rollback, run the **same entire
block** with `ACTION=rollback`; it uses the preserved baseline backup, not the
clone. All hash comparisons are mandatory predicates. A repeated install
refuses when the target is already fixed; it cannot replace the baseline backup.
A missing, symlinked, corrupted, or writable backup refuses before replacement.

The only truncating write below is to a new private staging file. Both directions
verify staged bytes, syntax and preserved uid/gid/mode, then rename on the
target's filesystem. Failures before rename leave the installed inode untouched.
This provides atomic visibility, not power-loss durability. A post-rename
verification failure is a STOP requiring Operator inspection, not proof that no
replacement occurred. The backup is never overwritten or removed.

<!-- op-request-swap:start -->
```bash
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
ACTION=install                         # change only to rollback for the inverse
SOURCE='/absolute/path/to/reviewed-clone/ops/operator-safety/wrappers/op-request'
TARGET=/usr/local/sbin/op-request
BACKUP=/root/op-request.bak-previous
OLD=0000000000000000000000000000000000000000000000000000000000000000  # Operator: set to the installed wrapper digest
NEW=450aa7d7e569c9df96f28ea4facc4309910ff57cb2935da2eb16c0a55b9708c9
swap_dir=''; backup_dir=''
stop() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
cleanup() {
  [[ -z "$swap_dir" ]] || rm -rf -- "$swap_dir"
  [[ -z "$backup_dir" ]] || rm -rf -- "$backup_dir"
}
trap cleanup EXIT
require_hash() {
  local got
  [[ -f "$1" && ! -L "$1" ]] || stop "not a regular non-symlink file: $1"
  got="$(sha256sum -- "$1")" || stop "cannot hash: $1"
  [[ "${got%% *}" == "$2" ]] || stop "hash mismatch: $1"
}
require_root_safe() {
  local uid mode
  [[ ! -L "$1" ]] || stop "symlink: $1"
  uid="$(stat -c %u -- "$1")" || stop "cannot stat owner: $1"
  mode="$(stat -c %a -- "$1")" || stop "cannot stat mode: $1"
  [[ "$uid" == 0 ]] || stop "not root-owned: $1"
  (( (8#$mode & 0022) == 0 )) || stop "group/world writable: $1"
}
[[ $EUID -eq 0 ]] || stop 'root Operator required'
for dir in "${TARGET%/*}" "${BACKUP%/*}"; do
  [[ -d "$dir" ]] || stop "missing directory: $dir"
  require_root_safe "$dir"
done
case "$ACTION" in
  install) FROM="$OLD"; TO="$NEW" ;;
  rollback) FROM="$NEW"; TO="$OLD"; SOURCE="$BACKUP" ;;
  *) stop 'ACTION must be install or rollback' ;;
esac
require_hash "$TARGET" "$FROM"
require_root_safe "$TARGET"
[[ "$(stat -c %h -- "$TARGET")" == 1 ]] || stop 'target has hard-link aliases'
metadata="$(stat -c '%u:%g:%a' -- "$TARGET")"
require_hash "$SOURCE" "$TO"

if [[ "$ACTION" == install && ! -e "$BACKUP" && ! -L "$BACKUP" ]]; then
  backup_dir="$(mktemp -d -- "${BACKUP%/*}/.op-request.backup.XXXXXXXX")" || stop 'backup staging failed'
  [[ -n "$backup_dir" ]] || stop 'empty backup staging path'
  cp -a -- "$TARGET" "$backup_dir/original"
  require_hash "$backup_dir/original" "$OLD"
  # Exclusive publication: ln fails if a backup appeared; never overwrite it.
  ln -T -- "$backup_dir/original" "$BACKUP"
fi
require_hash "$BACKUP" "$OLD"
require_root_safe "$BACKUP"

swap_dir="$(mktemp -d -- "${TARGET%/*}/.op-request.swap.XXXXXXXX")" || stop 'swap staging failed'
[[ -n "$swap_dir" ]] || stop 'empty swap staging path'
cp -a -- "$TARGET" "$swap_dir/op-request"
cat -- "$SOURCE" > "$swap_dir/op-request"
require_hash "$swap_dir/op-request" "$TO"
bash -n "$swap_dir/op-request"
[[ "$(stat -c '%u:%g:%a' -- "$swap_dir/op-request")" == "$metadata" ]] || stop 'staged metadata changed'
# Refuse drift immediately before the single same-filesystem replacement.
require_hash "$TARGET" "$FROM"
[[ "$(stat -c '%u:%g:%a' -- "$TARGET")" == "$metadata" ]] || stop 'target metadata drift'
mv -T -- "$swap_dir/op-request" "$TARGET"
require_hash "$TARGET" "$TO"
[[ "$(stat -c '%u:%g:%a' -- "$TARGET")" == "$metadata" ]] || stop 'installed metadata changed'
bash -n "$TARGET"
printf 'Verified %s; baseline backup retained at %s\n' "$ACTION" "$BACKUP"
```
<!-- op-request-swap:end -->

Preserve owner, group, mode and copied attributes; change only wrapper bytes and
its inode by rename. No sudoers, SHADOW, allowlist, queue, credential or service
change. Cleanup removes only this invocation's private temporary directories;
it never removes a queue request or the rollback backup.

## Non-destructive verification (no live queue traffic)

Never invoke the installed wrapper on the host to "test" it: every successful
call files a real queue request. Verification is:

1. `sha256sum` of the installed file equals the intended hash (fixed after
   install, baseline after rollback).
2. `bash -n` parses clean.
3. The fixed suite is green in CI on the exact merged SHA (no host run needed;
   the suite touches only fake queue/log fixtures).
