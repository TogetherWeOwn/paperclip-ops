# Red-main poll: read-only timer source contract

This public operator packet describes the checked-in wrapper and units. It is not
an installation receipt, a live configuration, or authorization to enable them.
Do not copy credentials, repository inventories or incident records into Git.

## 0. Delivery belongs to a runful routine

`red_main_poll_check.sh` has no board-write path. A runless timer cannot substitute
for an authorized agent run; `cross_issue_influence_run_context_required` is a
refusal, not a reason to retry with broader credentials. The timer reads its bound
rollup, writes local proposals, and exposes its result through unit status.

Set `RED_MAIN_HANDOFF_DIR` to a protected local directory. Each tick atomically
publishes `red-mains.json` with schema `watchdog.red-main-handoff.v1`, measured time,
snapshot entries and exit status. An authorized routine consumes it through
`watchdog/snapshot_builder.py --red-mains-handoff`. That routine, not the timer,
owns incident filing and `filed:` mirrors. A missing or stale handoff is not proof
that main is green.

## 1. Bound read access

Supply credentials only through a protected mode-0600 EnvironmentFile, never
command arguments or committed templates. `PAPERCLIP_AGENT_API_KEY` must be bound
to the rollup named by `RED_MAIN_ROLLUP_PARENT_ID`. The parent plus its comments is
the complete board read scope; the key's `parentIssueIds` binding must match that
parent. Do not replace this with a company-wide read or a broader write key.

GitHub observation needs `checks:read` and the reader's documented repository
metadata/status access, but no `actions:read`. `GH_TOKEN_CMD` supplies a credential
at execution time; do not print its output. The reader evaluates one immutable
commit rather than racing a moving branch. Unknown or inaccessible signals stay
unknown and are not converted into success.

## 2. State and cadence

Provide the reviewed repository list through `case_1a1b67a4_REPOS`. Set
`RED_MAIN_STATE_DIR` to a protected, writable local state directory. Its
`proposals/` child contains drafts, `[red-main-poll-gap]` coverage findings and
`[red-main-poll-error]` refusals. These are separate finding kinds so an existing
draft cannot suppress a gap or an error.

`RED_MAIN_REPOST_SECONDS` defaults to 86400; changed findings also obey
`RED_MAIN_CHANGE_FLOOR_SECONDS` (3600 by default). Both are nonnegative integer
seconds. Dedupe is based on incident keys, not changing prose or head hashes.
Clean ticks clear obsolete state; finding ticks sweep kinds no longer present.
Exit 0 means no outstanding proposals. Exit 1 means inspect the current proposal
and handoff evidence; it never claims that a board write succeeded.

## 3. Pin the public tooling before enabling anything

Set `RED_MAIN_TOOLKIT_DIR` to an explicitly reviewed immutable checkout. Run
`bash scripts/sync-toolkit.sh` there to restore checksum-pinned public dependencies,
including `gh_ci_status.sh`. Verify the poller and reader are executable and that
the offline suite and mutation gate pass. Do not reuse an unreviewed stale copy
from another install location. This source packet does not repoint a live host.

Review the checked-in service/timer and its environment bindings. Before any
separately approved activation, hand-run the service with the protected
configuration and inspect its journal, proposal files and handoff. A read-only
timer cannot repair a broken deployment or file its own incidents.

## Rollback

Retain proposals, handoffs and journal evidence. For an existing critical timer,
record an owned hold with an expiry before stopping it. Under that hold, disable
the timer before stopping its service; restore the previous reviewed release and
protected configuration, then verify it by hand before resuming its schedule.
Do not erase evidence, broaden key scope, bypass the run-context gate or recreate
a retired timer write path.
