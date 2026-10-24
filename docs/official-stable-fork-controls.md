# Official-stable cutover pack: historical fork controls

**State: preparation only; no live changes or deployment approval.**

This pack retires one fork-specific citation requirement from the fresh official
`v2026.1012.0` target and prepares one deferred monitor-timer hold. It does not
qualify an image, enable native SSH, remove an authorization control, or authorize
host execution. The target allows at most eight temporary upstream-bound fixes,
not carried fork features or migrations. No conditional ninth patch is allowed.

## 1. Citation audit applicability

`native_ssh_citation_gate.py` audits historical source anchors and the presence and
placement of three fork seam labels. Its pins describe the historical fork tree,
not the official target. Missing fork symbols in the fresh official tree must not
be treated as a request to restore the seam or carry a fork patch.

**Retirement decision:** omit this historical citation audit from the fresh
artifact's runtime installation and qualification requirements. Retain the public
audit, its fixture suite, and the historical ADR as legacy evidence. Do not change
the audit to return success on absent symbols, skip a failing security check, or
use a legacy green result as an official-stable verdict. If an existing live
consumer is found, stop and classify it before changing its invocation.

### Repository caller evidence

Inspected source: `TogetherWeOwn/paperclip-ops` commit
`5f8c9c4c521f0d2f17277bfc78dc114e6f8476a4`.

- The executable calls are fixture invocations in
  `test_native_ssh_citation_gate.sh`; the suite synthesizes its own source tree.
  The CI `native-ssh citation gate suite` step runs that suite, not a live audit;
  `.github/scripts/ci_changes.py` registers its change routing and
  `vendor/offline-ci-contract.json` retains its producer contract.
- `README.md` indexes the utility; `docs/native-ssh-os-authority-split.md` cites it
  as historical evidence. Neither is an execution path.
- No reference was found in `ops-tooling-runtime-manifest.txt`,
  `staged_root_scripts.txt`, the runtime installer, or a tracked systemd unit.
  This is repository evidence only, not a census of private or installed callers.
- The scanner itself says it is a lexical citation check, not proof that an
  authorization path executes. The ADR independently requires host preflight and
  hostile-agent acceptance; those requirements are not retired by this pack.

Reproduce the caller inventory on the exact reviewed revision:

```sh
git grep -n -F native_ssh_citation_gate -- .
git grep -n -E 'paperclip-fork-watch|TIMER HOLD RULE' -- .
```

Classify prose references separately from executable callers; a nonzero grep
exit for no matches is not a passing runtime security verdict. The source snapshot
excludes private runbooks and host configuration, as explained in `README.md`.

The gate and suite are deliberately unchanged:

| File | SHA-256 |
| --- | --- |
| `native_ssh_citation_gate.py` | `70d2c77dbe781249e89a294472b8b26b89d2ce04d6c6012b3784015bbdf65afd` |
| `test_native_ssh_citation_gate.sh` | `f104ebe93cec7b0f3438ada695bf76402976e47ed2f5c714b85c85c06cf3d6e8` |

Offline verification: `bash test_native_ssh_citation_gate.sh` passed 21 cases,
including absent/ambiguous anchors, missing seam symbols, forbidden-file symbols,
and lexical traps. This verifies the retained scanner, not a deployed tree.

## 2. Deferred host action: fork-watch timer hold

**Do not disable `paperclip-fork-watch.timer` now.** Its unit, service, command,
and installed callers are not in this public snapshot. No live timer state was
read or changed while preparing this pack.

The authorized host executor attaches this section to the existing cutover
contract as one step, not a second parallel request. Keep private host identity,
raw unit configuration, backups, and credentials in the private execution record.

### Entry conditions — all required

1. The exact official release commit, approved residual-patch list (zero to eight),
   final immutable image digest, ops revision, and rollback artifact are recorded.
   Unknown, unrecognized, or mismatched manifests are **NOT QUALIFIED**.
2. Two consecutive rehearsals passed, the second on that final image, with an
   explicit CEO go/no-go. Cutover then
   succeeded, including the contract's health checks. Changed artifacts invalidate
   favorable evidence; status alone is not approval.
3. Fresh private host inventory identifies the exact timer and triggered service,
   unit/drop-in hashes, effective command, dependencies, and any other callers.
   The executor proves this is only the obsolete read-only fork monitor, not an
   independent security, credential, readiness, backup, or recovery control. Mixed
   responsibilities or unavailable evidence mean **CHANGES REQUIRED**; leave it on.
   Source/configuration inspection must also show that expiry/rollback re-enable
   is safe on both the official and rollback artifacts, even if obsolete fork
   findings are expected. Do not run a production probe to establish this. If
   compatibility is unknown, prepare a separate reviewed monitor contract first.
4. The timer's baseline is both `enabled` and `active`, with no masks or conflicting
   holds. Capture the service state and next trigger too. Any other baseline is
   **NOT QUALIFIED** for the commands below; prepare a separately reviewed restore
   contract instead of guessing. Do not stop an already running service.
5. A complete TIMER HOLD RULE record and a durable expiry action have been saved
   and verified before disablement. Use the existing authorized host scheduler;
   its re-enable action must survive a reboot and must not depend solely on the
   monitor being disabled. If that facility is unavailable, leave the timer on.
   No scheduler or expiry action is created by this document.

No production tests, migrations, model/configuration writes, credential changes,
new spend, or security-boundary changes are part of this action.

### TIMER HOLD RULE

This pack proposes a maximum 24-hour hold for review in the cutover contract;
that bound is not a recovered historical policy. Save these fields in the private
cutover record before executing the hold:

| Field | Required value |
| --- | --- |
| Owner | Authorized host executor; record their concrete accountable identity and the existing execution contract reference. |
| Target | Only `paperclip-fork-watch.timer` on the inventoried cutover host. |
| Reason | Official-stable cutover succeeded; fork-only drift monitoring no longer describes the running artifact. |
| Artifact binding | Exact release commit, patch manifest, final image digest, ops revision, and successful cutover evidence. |
| Start | Actual hold start time in UTC, recorded immediately before the command. |
| Expiry | An explicit UTC timestamp no later than 24 hours after Start; never blank, indefinite, or silently extended. |
| Expiry action | At Expiry, restore the enabled/active baseline using the command below and verify its result. Record the durable scheduler's receipt and due time before disabling. |
| Early re-enable | Re-enable immediately if cutover rolls back or an entry condition is invalidated; do not wait for Expiry. |
| Extension | A new reviewed, bounded hold record and verified expiry action before the old expiry; otherwise restore the timer. Permanent retirement requires a separate reviewed contract. |

A prepared record with unset timestamps or owner identity is not an active hold.
This document does not claim that an expiry callback has been scheduled.

### Execution and evidence

Only the authorized host executor runs these commands, after the entry conditions
and expiry action are verified. Inspect the effective units first; do not publish
raw `systemctl cat` output, environment, or credential-bearing command lines.
The following status checks are not a substitute for that private inspection:

```sh
systemctl is-enabled paperclip-fork-watch.timer
systemctl is-active paperclip-fork-watch.timer
systemctl list-timers --all paperclip-fork-watch.timer
```

Require the exact baseline `enabled` and `active`. Then record ACT / WHY / UNDO:

- **ACT:** `systemctl disable --now paperclip-fork-watch.timer`
- **WHY:** the recorded successful official-stable cutover makes only this
  fork-monitor trigger obsolete; all independent controls remain effective.
- **UNDO:** `systemctl enable --now paperclip-fork-watch.timer`

After ACT, require `systemctl is-enabled` to report `disabled` and
`systemctl is-active` to report `inactive` for that exact timer (these states
normally return nonzero; capture output and exit code rather than ignoring errors).
Verify the durable expiry action remains armed. The triggered service is not
stopped or disabled; no unit files, data, history, archives, or branches are deleted.

At expiry or earlier rollback, run UNDO and require both status checks to return
zero with `enabled` and `active`; capture the next trigger. After early restore,
cancel only this hold's identified expiry action, not an unrelated schedule.
On disablement/verification failure, attempt UNDO immediately and report
**CHANGES REQUIRED** with observed states. If restore fails, report the exact
error through the existing incident route; do not mask it or change credentials.

Capture a sanitized before/after receipt: exact timer and unit hashes, artifact
binding, hold owner/start/expiry, scheduler receipt, command exit codes, states,
health-check evidence, and restore evidence when it occurs. The executor records
**PASS**, **CHANGES REQUIRED**, or **NOT QUALIFIED** against those exact artifacts.
A coordinator must inspect the favorable verdict before proceeding. A completed
preparation task is not a release or host-action approval.

## 3. Limits and rollback of this documentation slice

No host inventory, official image, patch manifest, rehearsal, cutover, timer hold,
or expiry scheduler was qualified here. Those remain execution-time prerequisites.
The historical scanner and every independent live control remain unchanged.
Reverting this documentation restores its prior applicability wording; no runtime
rollback is needed for this slice. If the deployment rolls back to the legacy
fork, restore its original audit requirements as well as the timer baseline.
