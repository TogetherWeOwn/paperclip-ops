# Confirmed reconciler outcomes

Use `ops/productivity/confirmed_outcomes.py` for the outcome vocabulary shared by
`connection_intent_sweeper.py`, `backlog_promoter.py` and `flow_guard.py`. This is a
source contract, not evidence of an installed or deployed runtime. The fixed
runtime release manifest currently contains none of these reconcilers. Any later
authorized packaging must carry the shared sibling module with all its consumers;
this change does not alter the installer or expand that manifest.

## Modes and verification

```sh
# Offline: no stores, subprocesses, network or credentials.
PYTHONDONTWRITEBYTECODE=1 python3 ops/productivity/confirmed_outcomes.py --selftest

# Offline discovery; each caller supports --help.
PYTHONDONTWRITEBYTECODE=1 python3 ops/productivity/flow_guard.py --help

# Flow guard dry-run: no DB/API reads, PATCH, child steps, state directory or log writes.
PYTHONDONTWRITEBYTECODE=1 python3 ops/productivity/flow_guard.py --dry-run --json-summary

# Offline acceptance: all effects replaced with fixtures/spies.
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/productivity -p 'test_*outcomes.py'
```

The sweeper and promoter default to dry-run: candidate reads still occur, but no
mutations or action-log writes. `--apply` enables only their existing eligible
actions. `flow_guard.py` **mutates without flags** (also with `--apply`): it arms
monitors, resets eligible transient-error agents and runs its existing mutating
child steps. Its explicit `--dry-run` is a no-I/O preview of that scope, not a
measurement of candidate counts. All three refuse `--apply --dry-run` before
reading stores or writing anything. Unknown CLI options are rejected.

No change authorizes a live decline, promotion, monitor/reset, installation,
scheduler change or broader admission policy. Existing pressure, STOP, readiness,
eligibility and subprocess timeout gates remain in force.

## Response contract, version 1

The existing write endpoints signal completion with HTTP **200**. Each caller
keeps `curl -o /dev/null -w '%{http_code}'`: bodies are discarded, not interpreted
as a success status. Confirmation requires **both** process exit `0` and an exact
three-digit `200` status after trimming surrounding whitespace. This proves the
endpoint response, not a later runtime or deployment state.

| Evidence | Outcome | Code | Next action |
|---|---|---|---|
| Exit 0, HTTP 200 | `confirmed` | `http_200` | `none` |
| Exit 0, HTTP 4xx/5xx | `definite_failure` | `http_<status>` | `inspect_refusal_before_next_tick` |
| Curl exits 5/6/7 before connecting | `definite_failure` | `curl_exit_<exit>` | `check_transport_before_next_tick` |
| Executable cannot start | `definite_failure` | `curl_unavailable` | `check_transport_before_next_tick` |
| Curl exit 28 / subprocess timeout | `unknown` | `curl_timeout` / `process_timeout` | `reconcile_target_before_retry` |
| Empty, malformed or 000 status | `unknown` | `invalid_http_status` | `reconcile_target_before_retry` |
| Other transport error / unexpected HTTP | `unknown` | `curl_transport_error` / `unexpected_http_<status>` | `reconcile_target_before_retry` |

A nonzero transport exit never confirms a write, even if stdout says `200`.
Unknowns are visible and never automatically retried within a tick. Duplicate
candidate targets are attempted at most once per action per tick.

### Counts and bounce suppression

Only confirmed actions produce completed-action text/logs. The promoter writes
`backlog->todo ... HTTP 200 outcome=confirmed code=http_200` only on confirmation.
Only those explicit confirmation records feed the existing 24-hour bounce window;
old unclassified records, failures, unknowns and future timestamps do not. A
rejection does not spend successful-promotion capacity. Unknowns reserve a slot
**for the current tick only**, preventing uncertain writes from exceeding the
runnable target. All attempts remain capped at `MAX_PER_RUN`.

The next promoter tick rereads backlog/eligibility before a new attempt. Unknowns
do not masquerade as a confirmed promotion or create a 24-hour hold. This is not
a retry engine or an exactly-once guarantee across concurrent processes.

## JSON and exits

`--json-summary` retains ordinary human text and appends **one JSON object as the
final stdout line**. Without it, no JSON summary is printed.

```json
{"version":"1","caller":"backlog_promoter","actions":[{"action":"promote","target":"case_example:card-example","outcome":"definite_failure","code":"http_403","next_action":"inspect_refusal_before_next_tick","http_status":"403"}],"counts":{"attempted":1,"confirmed":0,"definite_failure":1,"unknown":0},"planned":0,"errors":[],"next_action":"inspect_action_results"}
```

- `caller`: `connection_intent_sweeper`, `backlog_promoter` or `flow_guard`.
- `action`: `decline`, `promote`, `arm_monitor` or `reset_agent`.
- `target`: stable issue identifier plus intent/issue ID, or agent ID for resets.
- `outcome`, `code`, `next_action`, `http_status`: sanitized classification;
  `http_status` is null unless a valid HTTP status was measured.
- `counts`: attempted partitions into confirmed + definite_failure + unknown.
  Dry-run plans are excluded and reported separately as `planned`.
- `errors`: stable codes for read/log/child-step errors or contradictory flags.
  Child exit 0 is not claimed as a confirmed control-plane mutation; child
  failures/timeouts make the enclosing invocation fail.
- Top-level `next_action`: `inspect_action_results` if failures/errors exist,
  otherwise `none`.

Exit **0** means no observed failures (including no eligible work, STOP or dry-run),
not that an action occurred. Exit **1** reports unconfirmed actions or operational
read/log/child errors. Exit **2** refuses contradictory/invalid CLI arguments.
Never blindly retry from exit status alone: read the per-target classification.
Raw response bodies, child output, run error text and credential material are not
echoed into outcomes. Existing credential files and endpoints are unchanged.
