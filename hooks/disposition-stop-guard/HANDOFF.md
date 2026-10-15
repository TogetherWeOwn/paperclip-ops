# Disposition Stop guard: install handoff

For the operator (main session). Reviewed package; this document is the whole install. Nothing in it
needs a credential, a restart or a model call. Card: [case_c40e2d63](/TOG/issues/case_c40e2d63), a slice of
[case_f15b04c6](/TOG/issues/case_f15b04c6). Pattern and install path follow the Muse Stop guard
(`hooks/muse-stop-guard/`, [case_11a4d3d9](/TOG/issues/case_11a4d3d9)).

## What it is

A Claude Code `Stop` hook for every agent. When a run that has a card (`PAPERCLIP_TASK_ID`) is about to
end and the card is `in_progress` or `todo` with nothing that will ever wake it, the hook hands the turn
back, in the same warm session, with a short reason that names the rule. The model arms a monitor,
keeps working, or opens a child card; it does not end the run. Without it the run ends, Paperclip queues
an `issue_disposition_repair` run, and after two failed repairs it escalates the card to `blocked`.

It reads two things with the run's own credential (the card, and its interactions) and writes nothing.

## Evidence

Method: `measure/collect-bundle.mjs` (per-agent `heartbeat-runs` plus the activity of every card those
runs touched) then `measure/census.mjs`. Bundle collected 2026-10-10 18:17Z, window 24 h, 29 agents,
1,707 cards. Three agents (DevOps & Reliability, Founding Engineer, TogetherWeOwn Reviewer) hit the
1,000-row page, so their windows are 21.6, 22.4 and 12.3 h. Card state at a past moment is rebuilt from activity
events, so treat every class as approximate (+-5%).

### The premise check reproduces

| | COO count (17:50Z) | This census (18:17Z) |
|---|---|---|
| `issue_disposition_repair` runs | 403 of 6,363 | 399 of 6,410 (6.2%), 268 cards |
| `in_progress`, monitor fired, not re-armed | 148 | 146 |
| `todo`, no monitor | 134 | 130 |
| `in_progress`, no monitor | 48 | 50 |
| monitor exhausted (any status) | 21 | 23 |
| `todo`, monitor fired | 23 | 22 |
| blocked / done | 11 / 8 | 11 / 8 |

Run before the repair wake: `issue_monitor_due` 115, `issue_assigned` 96, an earlier repair 59, no run
within 30 min 47, other 82.

### What counts as a live path (measured, not assumed)

Every finished run on a card (5,431 succeeded runs), by how the card looked at the end, and how many were
followed by a repair wake:

| End state | Runs | Followed by repair |
|---|---:|---:|
| `in_progress` or `todo`, nothing armed, no blocker, no interaction | 424 | **307 (72%)** |
| the same, after a `{"resume": true}` write by the run | 85 | **70 (82%)** |
| the same, no resume write | 339 | 237 (70%) |
| `in_progress` or `todo`, monitor armed | 2,062 | 3 (0.1%) |
| `in_progress` or `todo`, pending interaction | 209 | 6 (2.9%) |
| `in_progress` or `todo`, unresolved blocker | 74 | 6 (8.1%) |
| `in_review` | 677 | 0 |
| `blocked` | 544 | 10 (1.8%) |
| `done` | 1,395 | 7 (0.5%) |

**A resume write is not a live path.** The card (case_c40e2d63) and AGENTS.md both offer
`{"resume": true, "comment": ...}` as a way to end a run on `in_progress`; the data says Paperclip
repairs those cards anyway. The hand-back reason therefore does not offer it: it offers a monitor
(`maxAttempts` 100, checks at least 30 min apart), waiting in the run, or a child card with a blocker.
`census.mjs --resume-live` replays the other reading: catches fall from 278 to 208.

### Replay of the classifier over the 24 h bundle

| | |
|---|---|
| Repair wakes in the window | 399 |
| of which a preceding run by the same agent finished within 30 min | 347 |
| **caught by the guard** | **278 (80.1% of 347; 69.7% of all 399)** |
| &nbsp;&nbsp;`no-live-path` / `monitor-exhausted` | 257 / 21 |
| missed | 69: chat thread 27 (skipped on purpose), blocked/done at run end 17, pending interaction 6, no activity 6, not the assignee 5, blocker 3, monitor armed 3, unknown 2 |
| Other runs on a card | 5,084 |
| **handed back, no repair followed** | **68 = 1.34% of guarded runs (1.13% of all 6,011 other runs)** |
| &nbsp;&nbsp;followed by another recovery wake within 60 min | 15 |
| &nbsp;&nbsp;followed by a platform escalation within 60 min (33 are repair runs that failed) | 25 |
| &nbsp;&nbsp;nothing followed | 28 = 0.5% |

The rule from the card: ship `observe` if the wrong hand-back rate is above 2% of runs. It is 1.3% on the
strictest count, so the default is `enforce`. Hand-backs per day at this rate: about 346, up to two turns
each (about 690 extra warm-session turns a day, spread over the agents below).

| Agent | Guarded runs | Hand-backs | of which a repair followed | of which none followed |
|---|---:|---:|---:|---:|
| DevOps & Reliability Engineer | 850 | 86 | 62 | 24 |
| Founding Engineer | 903 | 75 | 70 | 5 |
| QA & Release Engineer | 433 | 39 | 34 | 5 |
| Automation Engineer | 294 | 33 | 24 | 9 |
| Security Engineer | 275 | 28 | 22 | 6 |
| Web Engineer | 524 | 24 | 19 | 5 |
| Chief Executive Officer | 494 | 14 | 13 | 1 |
| other 8 agents with hand-backs | 744 | 47 | 34 | 13 |

**Ceiling.** The guard can prevent at most the 278 repair wakes it catches (80.1% of the 347 with a
preceding run; 69.7% of all 399), attempt-1 and attempt-2+ alike. If every hand-back is obeyed, repair
wakes fall from 399 to about 121 per 24 h (30 per 6 h). The parent's done-test (under 50 per 6 h, under
200 per 24 h) needs about 72% of hand-backs to be obeyed. That rate is unknown until the log has live
data; the ~121 that remain are repair wakes with no run just before them (52), chat threads (27), cards
that were already `blocked` or `done` (17), and the other misses (25: pending interaction, not the
assignee, blocker, monitor armed, unknown), which this slice does not touch.

### Tests

| Check | Result |
|---|---|
| `node --test hooks/disposition-stop-guard/disposition-stop-guard.test.mjs hooks/disposition-stop-guard/measure/census.test.mjs` | 57 pass |
| `node hooks/disposition-stop-guard/measure/mutation-check.mjs` (hook 38, installer 3, replay 8) | 49/49 killed |
| Real Claude Code 2.1.284 against local stubs of the Messages API and the Paperclip API, `node hooks/disposition-stop-guard/smoke/run-smoke.mjs` | 4/4 pass |

What the smoke test proves about the harness: the hook process inherits `PAPERCLIP_API_URL`,
`PAPERCLIP_API_KEY`, `PAPERCLIP_AGENT_ID`, `PAPERCLIP_RUN_ID` and `PAPERCLIP_TASK_ID` from the claude
process (the one assumption the install depends on); it reads the card with that key and only with GET;
Claude Code honours `{"decision":"block"}` and shows the model `Stop hook feedback:` followed by our reason;
the second Stop arrives with `stop_hook_active: true`; the run ends at the guard's cap of 2. Loopback only,
no credential, throwaway HOME.

## Scope

- **Reach.** One entry under `hooks.Stop` in `/paperclip/.claude/settings.json`, the user-scope file every
  `claude_local` agent on this box reads. The Muse guard's entry is in the same array; the two do not
  share a counter or a log, and the installer proves it leaves the other entry byte-identical. The installer
  refuses to write the file without `--i-understand-this-reaches-every-agent`, and refuses from any
  agent run.
- **When it acts.** Only the main-thread `Stop` event, only when `PAPERCLIP_TASK_ID` is set, only when the
  card is `in_progress` or `todo`, is assigned to the run's agent, is not held by another run, is not a chat
  thread (`conversationAgentId`, `originKind: chat_channel`, or an external channel binding), has no
  monitor scheduled, no unresolved blocker, no pending interaction, no recovery the platform
  already owns for another run (except the repair's own wait, kind `deliberate_wait_without_target`,
  which is never a live path: it stays active through the repair run whose ending triggers the next
  repair or the escalation), no scheduled retry for another run (a retry row for this very run is this
  run, not a future wake), and no review participant. Not while `background_tasks` or `session_crons`
  is non-empty.
  Repair runs are therefore handed back, not excluded, attempt-1 and attempt-2+ alike: a
  `scheduled_retry` row for this very run is this run, not a future wake. Repair attempts 2+
  (delays 60s, 120s, 240s, 480s) park such a row with reason `issue_disposition_repair` while they
  execute, and the card carries that row, pointing at the very run, until the next repair or the
  escalation; the hook hands those runs back exactly as the replay assumes. The replay never
  reconstructs `activeRecoveryAction` or `scheduledRetry` (activity records neither), but the repair's
  own wait (kind `deliberate_wait_without_target`) and an own-run retry row read the same in replay
  as live for this dimension, so the 278 catch count describes the hook as it runs. The 68 flagged
  runs / 1.3% therefore estimates the live wrong-hand-back rate; it stays
  under the 2% line, so the default stays `enforce`.
- **Cost bound.** At most 2 hand-backs per run, counted in a file outside the transcript
  (`$PAPERCLIP_RUN_SCRATCH_DIR`, deleted with the run). Claude Code adds its own cap of 8. If the counter
  cannot be written the hook allows the stop. No new spend path: a hand-back is one more turn in a warm
  session on the lane the agent already uses.
- **Latency.** One card read and one interactions read, started together, per task run end, each on
  a 900 ms budget (`DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS` / `requestTimeoutMs`,
  clamp 50–8,000 ms). A shared card cache covers the tail: fresh allow entries (< 30 s,
  `cacheFreshMs`) skip the network (single-digit ms); stale allow entries (< 5 min, `cacheStaleMs`)
  decide when the network is slow, marked `cached: true`. A block (or would-block) is never
  served from cache and never written: the Stop after a hand-back always re-reads, so a card
  the agent just fixed cannot be re-blocked from the pre-fix card. Slow reads without usable
  cache fail open on their own classes (`card-timeout` / `interactions-timeout`, never
  `card-unreadable`); a slow read with only a stale block cached fails open the same way.
  Measured 2026-10-10: card read p50 2.6 s, p90 4.6 s, interactions p50 0.9 s. Observe
  23:37Z→05:41Z (624 stops): Stop p50 533 ms, p95 3.1 s, max 6.5 s. After: the slow path costs
  the ~900 ms budget (CLI hang test: 928 ms empty-cache, 30 ms fresh-cache) and the 8 s deadline
  stays as backstop. Cached cards are validated (same assignee, same or no holding run) before
  use; monitor/blocker/interaction drift inside the TTL is the accepted staleness tradeoff.
  The cache stores only the classifier shape (no title, description or other card text) and
  every cache write prunes entries older than the stale TTL (backstop 48 h, bounded 50).
- **Fails open.** Any error, missing credential, unreadable card, bad input, or the 8 s deadline: allow, exit 0.
- **Read only.** It issues GETs against `PAPERCLIP_API_URL` with the run's own key. It never writes a card,
  comment or status, and never prints the key.
- **Log.** `decisions.jsonl` beside the script: time, action, reason, card status, hand-back count, length of
  the reason, agent id, run id, milliseconds. No model text, no card text.
- **Not covered.** Agents that do not run Claude Code (`codex_local`, OpenCode, Hermes) have no Stop hook.
  Chat threads are skipped on purpose. Counting them would catch 24 more repair-preceding runs and hand
  back 45 more runs no repair followed (wrong rate 2.1% instead of 1.3%), so a hand-back there would be wrong
  almost twice as often as right.

## Install

Operator-owned, host-executed, from the operator's host shell, never from an agent run. `SRC` is a
checkout of `paperclip-ops` at the merge commit of the PR that added this directory.

```bash
SRC=<checkout>/hooks/disposition-stop-guard
DEST=/paperclip/disposition-stop-guard

# 1. Copy the three runtime files. Tests, fixtures, the smoke harness and the measure/ scripts stay in the repo.
install -d -m 0755 "$DEST"
install -m 0644 "$SRC/disposition-stop-guard.mjs" "$SRC/install-disposition-stop-guard.mjs" "$SRC/summarize-decisions.mjs" "$DEST/"
: > "$DEST/decisions.jsonl" && chmod 0664 "$DEST/decisions.jsonl"
chown -R --reference=/paperclip/model-guard "$DEST"     # the hook runs as the agents' uid and appends here

# 2. Dry run. Nothing is written. Expect installed:false.
node "$DEST/install-disposition-stop-guard.mjs" status

# 3. Review the diff. Expect ONE added block under "Stop", and nothing else.
diff <(jq -S . /paperclip/.claude/settings.json) \
  <(node "$DEST/install-disposition-stop-guard.mjs" install 2>/dev/null | jq -S .)

# 4. Apply. It prints the backup path; keep it.
env | grep -E '^PAPERCLIP_(RUN|AGENT)_ID=' && echo "STOP: operator shell only" && exit 1
node "$DEST/install-disposition-stop-guard.mjs" install --apply --i-understand-this-reaches-every-agent
```

When the host checkout and the container see different paths (case_5d081dcf: the host-installed
command briefly carried the host path `/home/ubuntu/stacks/paperclip/data/...`, which agents
inside the container cannot load), pass both explicitly. `--script` is the host filesystem
path (existence + agent-owned checks); `--hook-command` is the command written into settings
(the container path the agents load). The installer refuses a hook command whose path is not
absolute, whose file name is not exactly `disposition-stop-guard.mjs` (so `.bak` is refused),
or that points into one agent's workspace; it also refuses a `--script` whose file name is
not exactly `disposition-stop-guard.mjs`. An explicitly passed but empty or valueless
`--hook-command` is refused (exit 2); the host-path default applies only when the flag is
absent, so quote the variable (`--hook-command "$CONTAINER_CMD"`) only when it is set.
Use the space-separated form `--flag value`: equals-form options (`--flag=value`), unknown
or misspelled flags, duplicate flags, missing option values and stray positional arguments
are refused with exit 2 before reading or writing settings. `--apply` and
`--i-understand-this-reaches-every-agent` are valueless switches.
For existence, the installer stats the container path mapped through
`--container-prefix/--host-prefix` (both required together, both absolute): the container
path `/paperclip/...` is checked as its host counterpart under the data dir. `status` shows
the mapped `hookExistencePath` it checked. Then run the in-container check under Verify below.

```bash
DATA=/home/ubuntu/stacks/paperclip/data                     # host path agents see as /paperclip
SETTINGS="$DATA/.claude/settings.json"                      # host path of the file agents read as /paperclip/.claude/settings.json
DEST="$DATA/disposition-stop-guard"                          # host path of the installed package (agents see /paperclip/disposition-stop-guard)
node "$DEST/install-disposition-stop-guard.mjs" install \
  --settings "$SETTINGS" --script "$DEST/disposition-stop-guard.mjs" \
  --hook-command "node /paperclip/disposition-stop-guard/disposition-stop-guard.mjs" \
  --container-prefix /paperclip --host-prefix "$DATA" \
  --apply --i-understand-this-reaches-every-agent
```

Note: the op executor script `/home/paperclip-operator/op-req-case_b7d04dd0-install.sh` lives
outside this repo (host-owned). It needs the same fix: pass `--script` (host path) together
with `--hook-command "node /paperclip/disposition-stop-guard/disposition-stop-guard.mjs"`
and `--container-prefix /paperclip --host-prefix /home/ubuntu/stacks/paperclip/data`.

## Verify (no model spend)

```bash
# Hook keys: expect PreToolUse, Stop, UserPromptSubmit (and whatever else was there). Two Stop entries.
jq '.hooks | keys, (.hooks.Stop | length)' /paperclip/.claude/settings.json

# Nothing else changed: this must print nothing.
diff <(jq -S 'del(.hooks.Stop)' /paperclip/.claude/settings.json) <(jq -S 'del(.hooks.Stop)' <BACKUP_PATH>)

# Behaviour, unreachable API: expect no output, exit=0, and one "card-unreadable" line with agent op-check.
echo '{"hook_event_name":"Stop","session_id":"op-check"}' \
  | PAPERCLIP_TASK_ID=op-check PAPERCLIP_AGENT_ID=op-check PAPERCLIP_RUN_ID=op-check-$(date +%s) \
    PAPERCLIP_API_URL=http://127.0.0.1:9 PAPERCLIP_API_KEY=x node "$DEST/disposition-stop-guard.mjs"; echo "exit=$?"

# Behaviour, no task: expect no output, exit=0, and no log line.
echo '{"hook_event_name":"Stop","session_id":"op-check"}' | node "$DEST/disposition-stop-guard.mjs"; echo "exit=$?"
```

In-container check of the WRITTEN command (case_5d081dcf: catches a host path written into
settings, which agents cannot load). Run from an agent-container shell, where `/paperclip`
is the container path:

```bash
CMD=$(jq -r '[.hooks.Stop[].hooks[].command] | map(select(contains("disposition-stop-guard.mjs"))) | first // empty' /paperclip/.claude/settings.json)
test -n "$CMD" || { echo "STOP: no disposition-stop-guard entry in container settings"; exit 1; }
HOOK_PATH=$(echo "$CMD" | tr ' ' '\n' | grep 'disposition-stop-guard.mjs' | tr -d "'\"" | head -n 1)
test -f "$HOOK_PATH" || { echo "STOP: container hook path missing: $HOOK_PATH (got $CMD)"; exit 1; }
echo '{"hook_event_name":"Stop","session_id":"op-check"}' \
  | PAPERCLIP_TASK_ID=op-check PAPERCLIP_AGENT_ID=op-check PAPERCLIP_RUN_ID=op-check-$(date +%s) \
    PAPERCLIP_API_URL=http://127.0.0.1:9 PAPERCLIP_API_KEY=x sh -c "$CMD"; echo "exit=$?"
```

Then confirm live agents reach the hook, the one assumption the repo cannot prove from a host shell
(the smoke test proves it for the Claude Code CLI):

```bash
# Within ~30 minutes of the next task runs ending: stopsSeen above the op-check line, and blocks above 0.
node "$DEST/summarize-decisions.mjs" --since <install time>
```

If `stopsSeen` stays at the single `op-check` line while task runs are ending, those agents do not read
`/paperclip/.claude/settings.json` (a per-agent `CLAUDE_CONFIG_DIR`). Nothing is harmed. Report it on
[case_c40e2d63](/TOG/issues/case_c40e2d63).

## Rollback

Fastest first. Each is independent.

| Step | Command | Effect |
|---|---|---|
| 0. Instant, no settings edit, also covers sessions that already loaded the hook | `touch /paperclip/disposition-stop-guard/DISABLED` | Every stop is allowed. Undo with `rm`. |
| 0b. Keep it installed, stop blocking, keep logging what it would do | write `{"mode":"observe"}` to `/paperclip/disposition-stop-guard/disposition-stop-guard.config.json` | Logs `wouldBlock: true`, blocks nothing. |
| 0c. Same, smaller | write `{"maxHandbacks":1}` there | One hand-back per run instead of two. |
| 1. Remove our entry only | `node /paperclip/disposition-stop-guard/install-disposition-stop-guard.mjs uninstall --apply` | Removes our `Stop` entry; the Muse entry stays. Backs up first. |
| 2. Restore the pre-install file | `cp <BACKUP_PATH> /paperclip/.claude/settings.json` | Only if nobody else has edited the settings since. Otherwise use step 1. |

Prove it: `node /paperclip/disposition-stop-guard/install-disposition-stop-guard.mjs status` shows
`installed:false` (steps 1, 2), and `jq '.hooks | keys'` matches the pre-install list.

## Measurement (owner: Prompt & Model Engineer; the COO records the parent's done-test on case_f15b04c6)

Same method before and after:

```bash
export PAPERCLIP_API_URL=... PAPERCLIP_API_KEY=... PAPERCLIP_COMPANY_ID=...
node hooks/disposition-stop-guard/measure/collect-bundle.mjs --out "$PAPERCLIP_SCRATCH_DIR/bundle" --hours 24 --concurrency 4
node hooks/disposition-stop-guard/measure/census.mjs --bundle "$PAPERCLIP_SCRATCH_DIR/bundle"
```

1. **Before** is the 24 h census above: 399 repair wakes per 24 h (112 in the last 6 h at the card's time).
2. **After**, 24 h post install: `runs.repairRuns` under 200 and the 6 h figure under 50; the
   `missing-disposition` finding of the churn detector gone for `max_attempts_exhausted`.
3. `node /paperclip/disposition-stop-guard/summarize-decisions.mjs --since <install time>`: hand-backs,
   runs handed back, `capReached`, `failedOpen`, `msP95`.
4. Read it as follows. If `capReached` is above 5% of handed-back runs, the reason is not being obeyed and
   its wording needs work, not the cap. If `failedOpen` is above 10% of stops, the card read is slower than the
   read budget and the guard is missing its target; `timedOut` splits budget timeouts out of
   `cardUnreadable` (a slow plane must move `timedOut`, never `cardUnreadable`), and `decidedFromCache`
   shows how much of the tail the cache absorbed. If the hand-backs fall mostly on runs that no repair would
   have followed, flip to `observe` (step 0b) and report the rate here.
5. The collector is read only. Keep `--concurrency` at 4 or below: a card read takes seconds on a loaded
   control plane, and a census must not make that worse.

## Tuning without touching settings

`/paperclip/disposition-stop-guard/disposition-stop-guard.config.json`, environment variables win over it:

| Key | Env | Default | Meaning |
|---|---|---|---|
| `mode` | `DISPOSITION_STOP_GUARD_MODE` | `enforce` | `off`, `observe` (log only) or `enforce`. |
| `maxHandbacks` | `DISPOSITION_STOP_GUARD_MAX_HANDBACKS` | 2 | Hand-backs per run. 0 turns blocking off. |
| `requestTimeoutMs` | `DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS` | 900 | Per-read budget in ms (clamp 50–8,000). Lower bounds the Stop tail; higher catches more on a slow plane. |
| `cacheFreshMs` | `DISPOSITION_STOP_GUARD_CACHE_FRESH_MS` | 30,000 | Fresh allow-cache skips the network (blocks always re-read). 0 disables the fast path. |
| `cacheStaleMs` | `DISPOSITION_STOP_GUARD_CACHE_STALE_MS` | 300,000 | Stale allow-cache decides when the network is slow (marked `cached`); stale blocks fail open. At least the fresh window. |
| cache dir | `DISPOSITION_STOP_GUARD_CACHE_DIR` | `$TMPDIR/disposition-stop-guard` | Shared card cache (no credential, no title/description in it; pruned on every write). Per-test isolation sets this per dir. |

## Known limits

- The replay is an estimate. It classifies card state rebuilt from activity events; the live card also has
  `executionRunId`, scheduled retries, recovery actions and `successfulRunHandoff` that activity does
  not record. The repair's own `deliberate_wait_without_target` wait and a repair run's own
  `scheduled_retry` row are both deliberately not live paths, so their absence from the reconstruction
  does not overstate the catches: hook and replay agree on those runs (see Scope). Required handoffs for
  another corrective run remain live; the replay does not reconstruct their run IDs, so it can miss those
  live paths. Any other recovery kind would also allow the stop live and is absent from the replay.
- Cards with unresolved blockers count as live whatever the blocker's state. A card whose blockers are all
  `done` is repaired but not handed back (3 of the 69 misses).
- A hand-back that the model answers with a bare "done" restarts the same problem one level up; the cap is what
  ends it.
- The 52 repair wakes with no run just before them (no run by the same agent finished within 30 min) are
  outside this guard. They are cards the platform finds stuck later, for example after a restart.
