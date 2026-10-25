# Task scope: acceptance criteria and anti-goals

A task should say what passes, what must not happen, and where the slice ends.
Use this contract when authoring or refining implementation tasks. It uses
Paperclip's existing issue description, plan document, child tasks and blockers;
these headings are prose, **not new API fields or an admission gate**.

This is reusable source and a company-adapter instruction fragment, not an
installed skill or proof of company-wide adoption. No host automation, plugin,
core change or additional approval step is required by this document.

## Copy-ready description

Replace every angle-bracket placeholder before handing off the task. Preserve
existing requirement IDs and their verification mappings; use local `AC1`,
`AC2`, etc. only when no requirement IDs exist. A heading, placeholder, or an
untestable promise such as “works correctly” is not an acceptance criterion.

```markdown
## Outcome
<Who benefits, what changes, and why this slice matters.>

## Acceptance criteria
- AC1: Given <input/state>, <observable pass/fail outcome>.
  Verify: <command or repeatable inspection>; evidence: <artifact/location>.
- AC2: Given <boundary/failure state>, <observable safe outcome>.
  Verify: <command or repeatable inspection>; evidence: <artifact/location>.

## Anti-goals
- <An unwanted outcome that would make this change unacceptable even if AC1 passes.>
  Check: <how the reviewer can detect that outcome>.

## Out of scope
- <Related work deliberately not delivered in this slice.>

## Scope and milestones
Scope: <single reviewable slice, with reason; or oversized coordination task>.
<For a single slice: "Milestones: not applicable —" plus the bounded deliverable.>
<For oversized work: replace the line above with one row per milestone.>

| Milestone / child task | Doer | Depends on | Observable exit criterion | Verification / evidence |
| --- | --- | --- | --- | --- |
| <independently reviewable deliverable / child link> | <one doer> | <child link or none> | <pass/fail outcome> | <command/inspection and artifact> |

## Rollback
<Exact reversible action and retained evidence; otherwise name the irreversible
boundary and its required decision before executing it.>
```

One criterion is enough for a small task; add boundary criteria when relevant.
Do not invent requirements to fill the example's two bullets. If there is no
related deferred work, write “Out of scope: none identified” with a reason.
Anti-goals remain explicit: name a concrete unwanted behavior or boundary.

Acceptance criteria describe **required outcomes**. Anti-goals describe
**unacceptable outcomes**, not merely deferred features. For example, a preview
may render correctly but still be unacceptable if it sends member data to an
external service. A new dashboard can be out of scope without being forbidden.
That distinction does not authorize data access or change an existing fence.

### Filled small-task example

For a documentation-navigation slice:

- **Outcome:** readers can find this task-authoring contract from the repository
  entry point.
- **AC1:** the README task-authoring link resolves to `docs/task-scope.md`.
  **Verify/evidence:** inspect the relative link and its tracked target in the
  source PR; retain the inspected commit with the review.
- **Anti-goal:** do not install runtime automation or change skill assignments to
  make a documentation link available. **Check:** the PR changes prose only.
- **Out of scope:** migrating existing tasks or rolling out adapter instructions.
- **Scope:** one reviewable documentation slice; **milestones:** not applicable,
  one README link and its document. **Rollback:** revert that source change.

This example specifies checks; it does not claim they have been executed.

## Right-size without building a workflow engine

A single slice has one independently reviewable deliverable and one doer. Treat
work as oversized when it contains independently shippable changes, distinct
doers or execution boundaries, or cannot fit the existing one-PR-sized-slice
policy. Do not invent an hours/line-count threshold or override company policy.

For oversized work, keep the coordination task and put each implementation
slice on its own native child task with acceptance criteria and anti-goals.
Every milestone needs an observable exit criterion and a verification/evidence
location; “implement”, “test” and “deploy” alone are not verifiable milestones.
The parent records integration acceptance, not just child completion. Use
`blockedByIssueIds` for real dependencies; a table or `parentId` alone does not
create a blocker or a wake path. Keep existing ownership and execution gates.

Separate necessary hardening from optional follow-ups. A security fence or a
failed required check is not optional. Unrelated polish can become a separate
task; it must not silently expand the current slice or block it without a real
dependency. Missing facts should be recorded as questions, not guessed answers.

## Company-adapter instruction fragment

The authorized maintainer can merge the following into the **existing**
company-adapter authoring skill after reviewing the source revision. It is not
a new skill to install or an instruction to change any agent's skill assignments.
Retain the adapter's existing requirement format, IDs and coverage mechanism.
If their canonical source is unavailable, record that integration as unverified
rather than inventing a second schema or claiming compatibility.

> When authoring or refining an implementation task, include explicit Acceptance
> criteria, Anti-goals, Out of scope, Scope and milestones, and Rollback content
> in its native description. Use the copy-ready contract above; preserve the
> existing requirement IDs and evidence mappings. Each acceptance criterion has
> an observable pass/fail outcome and a repeatable verification/evidence path.
> Each anti-goal names an unacceptable outcome and how to check it. Classify the
> scope: one independently reviewable slice, or oversized coordination with
> evidence-bearing milestones, one doer per child slice, and native blockers
> where needed. Never count placeholders as coverage. Do not rewrite another
> doer's running task, waive a safety/review gate, or infer missing authority.
> Record unresolved requirements and use the existing interaction/dependency
> mechanism only when they actually prevent progress. Optional polish does not
> justify a new approval hop or an unbounded hardening task.

## Native adoption and verification

1. Retain the original description and its revision/hash in a private task
   document before editing. Do not publish live task text or internal evidence
   in this public repository.
2. Fill the template in the current owned task's `description`. Preserve the
   original objective, decisions and approval boundaries. Put a detailed plan
   in the native `plan` document, not a duplicate repository plan.
3. Verify the update response echoes the intended description. API writes need
   the normal run audit header. Preserve concurrent human changes; reconcile a
   changed description rather than overwriting it with an older copy.
4. Have the existing adapter's authorized maintainer integrate the fragment in
   its canonical source through its normal reviewed release path. Inspect the
   effective authoring instructions and a newly authored task before claiming
   adapter rollout. Source publication alone does not change those instructions.
5. Continue normal execution and review. Specification completeness does not
   prove acceptance outcomes, deployment, or that the task is done.

No company-specific requirements schema is defined here. Until the referenced
requirements-format/coverage source is located and inspected, integration with
it remains unverified. This contract is deliberately Markdown over native fields,
not a replacement requirements manifest, validator, scheduler or dashboard.

### Measure live work honestly

Before changing a cohort, record its fixed task IDs, selection rule, capture
time and original descriptions/revisions in a private task document. Avoid tasks
another doer is running. For a self-owned pilot, label the cohort as a convenience
sample; do not generalize it to the company.

Report counts before and after using the **same denominator**:

- **Specification completeness:** tasks with concrete acceptance outcomes plus
  verification/evidence, explicit checkable anti-goals, and a scope classification
  with verifiable milestones when oversized, divided by cohort size.
- Report the three component counts as well, so a missing anti-goal cannot be
  hidden by an acceptance heading. A placeholder or unresolved critical
  requirement does not count as complete. For an empty cohort, report “no data”,
  not 100%.
- Keep **adoption** separate: effective adapter instruction revision inspected,
  and newly authored tasks that used it. Keep **outcome verification** separate:
  criteria actually demonstrated with evidence. Do not mark future PRs, tests,
  milestone exits or deployments as already passed.

Closing evidence must state the cohort, before/after counts, what was actually
adopted, and what remains unverified. Formatting improvements alone do not
establish reduced guessing, faster delivery, or requirements-test coverage.

### Undo

Revert the source PR to withdraw this contract. For an adopted adapter fragment,
restore its prior reviewed source revision using the same authorized maintenance
path. For a pilot description, restore only the added scope-contract content
from the retained original, preserving any intervening human decisions and work
evidence. Reverting prose never deletes child tasks, history or completed work.
