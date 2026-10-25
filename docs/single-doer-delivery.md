# Single-doer delivery contract

Use these fields when planning a delivery or reporting a defect. Native Paperclip
assignment is the source of truth for the current doer. A document records the
reason for the assignment; it is not a second assignment or an execution grant.

This is a planner-operated workflow, not an automatic ownership resolver. No
host script, timer, plugin, new catalog, or core patch is required. Native
assignment permits later reassignment; this contract does not make ownership
immutable or enforce it server-side.

## Planner card extension

Copy this section into a task's initial plan document. Save the initial plan
when creating the task, before execution can start. Keep real owner identities,
private catalog locations, and live delivery evidence on the private task, not
in this public repository.

```markdown
## Delivery contract
- Service and canonical repository: <service>; <owner/repository>
- Delivery owner: <one native assigneeAgentId OR assigneeUserId, never both>
- Assignment evidence: <existing task/PR ownership OR explicit authorized planner assignment OR verified catalog entry>
- Ownership metadata revision: <immutable revision, or not applicable with reason>
- Routing result and reason: <existing_delivery | planner_assignment | unique_owner | hold>; <evidence>
- Existing owning card: <link or none after duplicate check>
- Decision-changing questions: <none, or question + evidence + recommendation>
- Prerequisites: <native blockedByIssueIds, or []>
- Manifest: <one issue-document link and its saved revision>
```

Do not infer a delivery owner from a role title, project lead, GitHub author, or
repository name. A service's code-owning team and a task's named delivery doer
are distinct: for catalog-based routing, metadata must explicitly select one
eligible doer, not merely name a team. Preserve the doer through implementation,
review fixes, replies, and
delivery. Reviewers and authorized host executors retain their separate roles
and permission boundaries.

## Deterministic defect routing

Perform this lookup in the planner's current task. Do not create a parallel
router or mutate another agent's running task.

| Order | Evidence | Result |
| --- | --- | --- |
| 1 | Exactly one explicitly linked active delivery card owns the defective change and has one native assignee | `existing_delivery`: retain that card and assignee; no duplicate task or reassignment |
| 2 | More than one possible owning card, a missing assignee, or a closed/cancelled linked card | `hold`: resolve lifecycle/ownership ambiguity before creating or reopening work |
| 3 | No existing owning card; the designated work-supply planner has established an explicit authorized assignment on the actual task | `planner_assignment`: retain that native assignment and its evidence; do not create another task |
| 4 | No existing owning card or explicit planner assignment; verified revision-pinned service/repository metadata has exactly one matching eligible delivery owner | `unique_owner`: the designated work-supply planner creates one assigned defect task through the native API |
| 5 | Metadata is missing, stale, ambiguous, or identifies only a team or an ineligible owner | `hold`: the designated work-supply planner must establish the assignment on the actual task; do not guess or fan out |

Preserve existing native task and PR ownership. Only the designated work-supply
planner establishes assignments for unmapped new services or defects; a role
label is not assignment evidence. A failed lookup does not prove no catalog
exists. Do not build a duplicate registry or reroute a running sibling. Keep live
new-defect routing and canonical-catalog integration **UNVERIFIED** until a
revision-bound source or explicit authorized native assignment is evidenced.
Planner assignment does not itself prove catalog integration or delivery.

For step 4, use the canonical service and full repository identity from the
report and catalog. A repository match must not override a conflicting service
match. Use path rules or specificity precedence only if the authoritative
metadata explicitly defines them; do not invent a longest-prefix tie-breaker.
Pin the source and immutable revision, matched entry, selected owner, eligibility
evidence, and lookup outcome in the new task's initial plan. Recheck that the
source is current before task creation. If the revision or eligibility changed,
repeat the lookup instead of executing a stale decision.

Before creating anything, check for an existing task covering the same defect.
Use a caller-stable creation `idempotencyKey` derived from the canonical defect
identity and repository slice. Reuse it on retries; an idempotency key does not
replace the duplicate check. Do not generate a new key per attempt or owner.

An implementation/review defect on the active delivery goes back to its existing
delivery owner, not to a new task selected by a fresh team lookup. Record the
finding on the task you own. For a running sibling task, use an authorized native
coordination path carrying the existing card reference; do not comment into,
reassign, or start a competing implementation on that card.

A `hold` is a lookup result, not a Paperclip task status. Continue independent
work. If missing evidence truly prevents delivery, persist a native interaction
or a first-class prerequisite blocker and the corresponding waiting status.
Never use a prose-only question as the task's liveness path. A defect report or
successful lookup never bypasses STOPs, security gates, capability limits,
exact-head checks/review, or staging and rollback requirements.

## Builder card extension

```markdown
## Builder handoff and quality accountability
- Native delivery assignee: <same doer as the planner contract>
- Scope: <this repository slice; one card/branch/worktree>
- Owning card and manifest entry: <links>
- Acceptance evidence: <test-author contract or acceptance criteria link>
- Verification: <commands, exact head, observed outcomes; mark not run honestly>
- Review fixes and defect replies: <owned by the delivery doer>
- Delivery: <PR work product; merged/deployed evidence when applicable>
- Rollback: <revert or existing approved rollback procedure>
```

Do not modify acceptance tests owned by a separate test author. If acceptance is
ambiguous, ask only a question whose answer changes scope, implementation,
acceptance, authority, or a real dependency. Include the current evidence and a
recommendation. Do not add a routine approval hop to a pre-approved deployment.

Use native `blockedByIssueIds` for real prerequisite work, including separate
repository slices when one actually requires another to finish. A parent link,
manifest row, question, or “remaining” bullet is not a blocker edge. Preserve
existing edges when updating the array; it replaces the whole blocker set.

## One revision-linked manifest

Use one keyed native issue document for the delivery manifest. Link it from each
slice's initial plan instead of maintaining independent copies in comments.
Each actual repository slice still has its own owning card, branch, and worktree.
Do not create empty slices solely to demonstrate multi-repository delivery.

```markdown
# Delivery manifest
- Delivery coordination card: <link; does not replace slice owners>
- Ownership source and revision: <authoritative reference, or unresolved>

| Repository / service | Owning card / native assignee | Base / head SHA | PR work product | Verification / delivery evidence | Prerequisites | Rollback |
| --- | --- | --- | --- | --- | --- | --- |
| <canonical repository / service> | <card / one assignee> | <base / head> | <link or not created> | <observed result or not verified> | <native blocker IDs or []> | <undo> |
```

Create/update with `PUT /api/issues/{issueId}/documents/{key}`. For creation,
send `baseRevisionId: null`. Before updating, read the document's
`latestRevisionId` and use it as `baseRevisionId`; on a revision conflict,
read and reconcile the current document rather than overwriting another slice's
evidence. Confirm the saved revision. Record that revision with downstream
handoffs; a link to “latest” alone does not pin what the builder received.

Keep repository code and work products separate from private delivery data. Do
not copy credentials, private host inventory, or real ownership records into a
public example. A manifest lists actual delivery state, not intended success:
“not created”, “not run”, and “not verified” are valid values.

## Verify on live work

Use a bounded, named live sample, with its time window and denominator. Capture
the baseline before changing records, then apply the same measurements after:

- Tasks with exactly one native delivery assignee: count / eligible tasks.
- Tasks with a filled contract and revision-linked manifest: count / sampled tasks.
- Existing-delivery defects retaining the same owning card and doer: count /
  observed existing-delivery defects.
- New-service defects: record separate counts for verified catalog mappings,
  explicit authorized planner assignments, and held cases with reasons, over all
  observed new-service defects. A planner assignment is not catalog integration.
- Real repository slices with complete verification/delivery/rollback evidence:
  count / actual slices, not planned or synthetic slices.

Report zero observations as **not measured**, not a 100% routing success rate.
A preserved assignee or added document is not evidence that a new defect was
routed successfully. Do not fabricate defects, touch running sibling cards, or
claim multi-repository delivery from a single-repository sample. Keep private
before/after evidence on the native task. Done requires the requested live effect,
not just a merged documentation PR.

Rollback: revert the documentation PR to remove these reusable templates. Any
native task changes require their own recorded, authorized undo; reverting prose
does not reverse assignments, blockers, or saved documents.
