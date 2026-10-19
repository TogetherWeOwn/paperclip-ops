# Runbook: queue-packet reject verb (receipt-only, non-executing)

The queue-packet reject verb (`op_queue_reject.sh`) records a terminal
non-execution disposition for a single operator-queue packet: it binds one
packet file to one rejection receipt without moving, deleting, relabeling,
replaying, or executing anything. The queue file is read-only input and its
bytes never change. There is no execution path, no approval handling, no
allowlist, and no caller-selected executable. A packet the approval
executor cannot parse is still rejectable here — that is the point.

SHADOW-independent by design: recording a terminal non-execution receipt is
not execution, so the shadow marker neither blocks this verb nor is changed
by it. The approval executor's SHADOW semantics are untouched.

This verb disposes exactly one packet per invocation. There is no queue
sweep mode and no batch flag; sweeping the queue through a shell loop is
outside this verb's contract and needs its own review.

## Source / install manifest

| File | Role |
| --- | --- |
| `op_queue_reject.sh` | The verb. Installed root-owned 755, run once as root. |
| `test_op_queue_reject.sh` | Offline refusal/regression suite (63 cases). |
| `docs/runbooks/op-queue-reject.md` | This file. |

Hashes below pin the reviewed revision. The approving reviewer re-checks
them at the exact head that merges; a re-push re-pins.

```
c2aa3672340316679dee7dcfe6b90f8ba71c6825c4515e197ee2d56e5877d20b  op_queue_reject.sh
92140d6b748e44958eee8dcad05cd8f38cb60107f5fc789ce115ed14b70049ff  test_op_queue_reject.sh
```

Trust lineage: the queue-dir jail (`within_dir_strict`) and the root-owned
file check (`check_root_owned_file`) are the same checks as the reviewed
`op_approval_executor.sh`, applied to the packet file itself since this
verb has no allowlist, approvers file, or runbook script. One deliberate
hardening over the executor: symlinks refuse even to inside targets
(`packet_is_symlink`), so the receipt name always equals the invoked name.

## Credential custody contract

- The verb takes no credential on argv, reads no credential file, and
  exports no credential variable.
- There is no free-text input: the only caller choice is a closed reason
  enum (`schema-probe | unknown-packet | stale-packet`), and the packet
  basename is charset-gated before any receipt path is built.
- Receipt and log lines are still passed through the same secret-shape
  scrub as the approval executor (defence in depth for future fields).
- Source work never mints, invites, grants, or reads live secrets. The
  suite asserts the non-execution structure directly (no eval, shell
  passthrough, exec, network, SHADOW/allowlist/approval coupling).

## Privilege and write-path design

No new principal and no sudoers change. The verb is installed root-owned
755 by the authorized host operator, who also runs the single one-shot
invocation as root inside the same bounded install window below. The
receipt and the log line inherit root ownership from the root invoker;
no agent account holds a write path to the processed dir or the log.
The helper validates the processed directory before trusting existing replay
or claim state, then again after directory creation: it must be a real,
non-symlink directory, root-owned under production rules, without group/other
write bits. Unsafe state refuses with exit 5 / `processed_dir_ownership`,
not replay or busy. `verify-only` also refuses an unsafe existing directory,
but does not create an absent one.

Output ancestors and the audit log remain host provisioning preconditions:
use protected root-owned ancestor directories and a root-owned regular audit
log that non-root users cannot write to (or an absent log in a protected
directory), never a symlink. The helper
does not validate every ancestor or the audit log's custody. It does not
repair permissions. Use a restrictive umask (for example `077`) during the
bounded invocation; a newly created group/other-writable processed directory
is refused before a claim or receipt can be written.

Re-verification is read-only through existing reads: the receipt under
`processed/` plus `sha256sum` over the preserved packet re-establishes
the binding on demand. (`verify-only` runs on the host only as the
install pre-flight in step 3 below; post-install re-verification uses
the receipt plus `sha256sum`, never the verb.)

Explicitly NOT chosen: a per-packet exact-argv sudoers grant for an
agent account. It would widen the standing sudo surface for a one-shot
disposition and is unnecessary while the host operator already runs a
bounded install window.

## Install (host operator only, bounded)

All paths are examples. Private host maps stay private.

1. Copy `op_queue_reject.sh` to `/usr/local/sbin/op-queue-reject`
   (or the private sbin path), `chown root:root`, `chmod 755`.
2. Confirm the file on disk hashes to the manifest above and `bash -n`
   passes. On any mismatch, stop: do not run it.
3. Pre-flight (writes nothing, proves the binding checks hold):
   `/usr/local/sbin/op-queue-reject verify-only --packet
   /var/lib/paperclip-operator/queue/<packet>.json --reason <enum>`
   Expect exit 0, `verified/would_reject`, no receipt file.
4. Run the single disposition exactly once:
   `/usr/local/sbin/op-queue-reject reject --packet
   /var/lib/paperclip-operator/queue/<packet>.json --reason <enum>`
   Expect exit 0, `rejected/ok`, one receipt at
   `processed/<packet>.json.rejected.json`.
5. Re-verify read-only: `cat` the receipt, confirm
   `packet_sha256` equals `sha256sum` of the queue file, and confirm
   the queue file's size and mtime are unchanged.

Time bound: under 15 minutes. Blast radius: the selected packet's terminal
workflow state only. Its persistent receipt and audit entry remain in place
if the command is uninstalled. Nothing is restarted, no service is reloaded,
no packet executes, and the queue file is untouched.

Publication uses an atomic, no-replace hard link from the acquired claim's
temporary file into the same processed directory. A delayed contender rechecks
the receipt under the claim and cannot overwrite it. Failed attempts remove
only their own temporary file and empty claim directory, never packet bytes
or a terminal receipt. The filesystem must support hard links; otherwise the
verb fails closed with `receipt_unwritten`.

A successful reject appends exactly one compact JSON object to the JSONL audit
log. If that append fails after receipt publication, exit 1 / `failed/log_unwritten`
includes the committed receipt path. The packet is already terminal: re-verify
the receipt read-only and investigate the missing audit trail; do not retry
the disposition or remove the receipt.

## Rollback (bounded)

1. Remove the installed verb binary.
2. Leave the receipt and the log line in place: they are the terminal
   record, not install state. Removing them would un-dispose the packet.
3. No restart is required. Total: under 10 minutes.

## Verification (non-destructive)

Run these from the repo checkout. None touches production, mints anything,
or restarts a service.

```bash
bash -n op_queue_reject.sh && bash -n test_op_queue_reject.sh
./test_op_queue_reject.sh          # expect: 63 passed, 0 failed
sha256sum op_queue_reject.sh test_op_queue_reject.sh
```

Expected: exit 0, `63 passed, 0 failed`, and hashes equal to the manifest
above. On the host (operator only, read-only after install):

```bash
ls -l /usr/local/sbin/op-queue-reject   # root-owned, no group/other write
cat /var/lib/paperclip-operator/processed/<packet>.json.rejected.json
sha256sum /var/lib/paperclip-operator/queue/<packet>.json   # must match receipt
stat -c '%s %y' /var/lib/paperclip-operator/queue/<packet>.json  # size/mtime unchanged
```

## Exit codes (pinned by the suite)

0 rejected / verified · 2 usage or bad invocation · 3 evidence/policy
refusal · 5 filesystem/ownership refusal · 6 replay or concurrent claim
(nothing written) · 1 I/O failure. There is no 4 (SHADOW does not apply
to a non-executing verb) and no 7 (nothing executes).
