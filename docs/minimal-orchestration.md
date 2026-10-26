# Minimal native handoff convention

The smallest useful way to hand work between agents: one owner, a short
packet, a short result, and the native task lifecycle that already exists.
This guide adds no orchestrator, endpoint, state store, dependency, or
running process. It is pilot guidance, not a fleet rollout or a blocking
platform gate, and it does not replace native typed review stages: a design
acceptance is never a substitute for the required pull request review.

Keep real task, run, and ownership identifiers on the private native task.
The public examples use neutral fictitious identifiers.

## Default topology: one owner

One owner executes end to end: implementation, fixes, replies, and closeout.
Delegate only a separable output with a clear benefit the owner can name.
Independent review stays mandatory either way; delegating production never
delegates verification.

Pick the smallest topology that fits:

| Need | Topology |
| --- | --- |
| The owner can do it in this session | Single owner, no delegate |
| Stateless extraction or drafting from supplied text | The existing model router, with the source text in the packet |
| Tool-dependent discovery over a bounded, readable surface | One read-only helper |
| Two genuinely independent read-only questions | At most two helpers, one level deep, disjoint work (pilot cap) |
| Long-lived or durable work | A native assigned child task with an initial plan |

Never use recursive delegation, shared-branch writers, or a helper that
repeats the owner's whole assignment. For a map/reduce split, partition only
independently useful questions; the owner verifies and combines the answers
without another reducer agent, and surfaces conflicts instead of averaging
them away. A short packet does not erase inherited harness or system
context, so delegation is not automatically cheaper: say what it buys.

## The packet and the result

Fillable examples live in
[`templates/orchestration/task-packet.json`](../templates/orchestration/task-packet.json)
and
[`templates/orchestration/work-result.json`](../templates/orchestration/work-result.json).
They are content for a prompt, a native task description, or an initial
plan — not new API envelopes and not directly executable API payloads.

A task packet carries exactly these fields: `version`, `objective`,
`source` (task identity and exact revision or SHA), `scope`, `constraints`,
`inputs`, `acceptance`, `limits`, `result_location`, and `continuation`.

A work result carries exactly these fields: `version`, `source`, `outcome`
(`complete`, `needs_input`, or `failed`), `summary`, `evidence`,
`unknowns`, `changes`, and `next_action`. The summary holds at most five
findings. Full evidence lives in a referenced native document or work
product with a stable ID and revision or SHA, not pasted into the result.
A complete investigation may report adverse findings; `complete` means the
work finished and is checkable, not that the answer is favourable. The
owner verifies before combining or closing anything.

Packet rules:

- Include the necessary safe inline facts and every applicable STOP and
  allowed action. A reference the recipient cannot read is not an input.
- Never copy a full thread, environment values, or credentials into a
  packet, and never phrase a packet as if it grants a permission.
- Budgets, excluding full artifacts and inherited harness or system
  context: packet at most 8192 UTF-8 bytes, result at most 4096 UTF-8
  bytes. These are conventions, not runtime enforcement. Never silently
  truncate evidence or constraints to fit: narrow or split the scope first.

## Durable handoffs use native tasks

Long work goes in a native assigned child task, created with its initial
plan saved at creation and a caller-stable idempotency key derived from the
work's canonical identity; reuse the key on retries. Blocker edges express
real dependencies only. The owner resumes from native completion and
dependency events — never polling and never wake-only comments. A cancelled
blocker does not count as resolved: reconcile it explicitly before expecting
a dependency wake. A child's result stays on the child's own task; the owner
links to it instead of copying it upward.

## The outer loop

Run every assignment, delegated or not, through the same loop:

1. Validate the current scope and revision.
2. Choose the smallest topology above.
3. Execute.
4. Verify the evidence and the current state.
5. Persist the result where the packet said it belongs.
6. Complete, do one bounded rework pass, or enter a real native wait.

Allow at most one scope replan per assignment. Never redispatch an unchanged
objective and revision to get a preferred answer. After an ambiguous write,
read the state back before deciding it failed or succeeded. Honour
denied-call and conflict (409) STOPs and each tool's own retry contract;
there is no generic mutation retry loop.

## Worked examples

**Single-owner execution.** A bounded fix with its checks fits one session.
No packet is written for a delegate; the owner implements, verifies, and
reports a work result on their own task. Expected outcome: zero handoffs,
review unchanged.

**Justified two-helper split.** A question splits into two independent
read-only lookups over disjoint surfaces, each useful alone. Each helper
gets its own packet naming only its question, inputs, and STOPs. The owner
verifies both results, combines them personally, and records any conflict.
Expected outcome: two results, one owner synthesis, no reducer agent.

**Durable native child handoff.** Work that must outlive the session becomes
an assigned child task with the packet as its initial plan, a stable
idempotency key, and a blocker edge only if the parent truly depends on it.
The child persists its work result on its own task. Expected outcome: the
owner resumes from the native completion or dependency wake, with nothing
polled.

**Explicit STOP.** The only readable input contradicts the packet's scope,
or the allowed actions cannot produce the acceptance evidence. The
recipient stops, reports `outcome: needs_input` (or `failed`) with the exact
evidence, and does not widen its own scope, retry with different authority,
or delegate onward. Expected outcome: the owner replans once or closes the
slice; nobody improvises.

## Review cases

| Case | Expected handling |
| --- | --- |
| Inaccessible inputs | The recipient names the unreadable reference and stops with `needs_input`; guessing from titles or summaries is a defect, and the owner fixes the packet or inlines the safe facts |
| Stale or duplicate completion | The result echoes the packet revision, so the owner rejects or reconciles a result written against an older revision, and treats a duplicate delivery as already-satisfied rather than re-executing it |
| Shared writes or dependent questions | Not a valid split: collapse to one owner, or sequence the dependent question after the first result; shared-branch writers are never merged after the fact |
| Oversized, conflicting, or failed outputs | Oversized work is narrowed or split before dispatch, never truncated; conflicts are surfaced to the owner verbatim; a `failed` result with evidence is a completed report, and rework is bounded to one pass |
| Hidden inherited-context, downstream-read, and retry costs | Count inherited harness context, the owner's read-and-verify time, and retry traffic as real costs when judging a split; if they erase the benefit, stay single-owner and say so |

## Verify the examples

```sh
python3 -m json.tool templates/orchestration/task-packet.json >/dev/null
python3 -m json.tool templates/orchestration/work-result.json >/dev/null
wc -c templates/orchestration/task-packet.json templates/orchestration/work-result.json
```

Both parse commands must exit 0, and the byte counts must stay within the
8192 / 4096 budgets above. Check every required field and every review case
by reading, not by parser alone: syntax and byte checks prove the examples
parse and fit, not that any runtime enforces this convention.

Rollback: revert the change that added this guide, the two examples, the
template pointer, and the CI parse step. Nothing runs because of this
document, so there is no job, timer, or service to disable.
