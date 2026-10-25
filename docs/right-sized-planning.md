# Right-sized slices and planning depth

Consumer: **agent**. Use when authoring or refining an implementation task.
Keep the existing acceptance criteria, anti-goals, requirement IDs and evidence
mappings; this planning fragment supplements them, not a second requirements
schema. It uses native Paperclip descriptions, `plan` documents, child tasks and
`blockedByIssueIds`. These are authoring guidelines, not new API fields or gates.

## Choose the smallest useful slice

One slice has one doer, one independently reviewable outcome, its verification
and rollback. Fold necessary setup, tests and documentation into that slice.
Split when a reviewer could meaningfully accept one deliverable and reject its
neighbor, or when ownership/execution boundaries differ. Do not split into
unusable scaffolding, separate “code” and “tests” cards, or arbitrary line/hour
quotas. Existing company slice limits still apply.

A coordination parent records integration acceptance. Its children carry the
actual deliverables and evidence. A table or `parentId` alone does not create a
dependency or wake path: use native blocker edges only for real prerequisites.
Never rewrite another doer's running task to improve its formatting.

## Route planning depth by risk and coupling

Choose the deepest applicable row; document why. Coupling means consumers,
interfaces or execution boundaries that must change together, not file count.
Small diffs can still have high risk. Depth changes preparation, **not authority**:
all existing security, approval, test, review and deployment gates remain.

| Work shape | Minimum useful preparation | Exit before implementation |
| --- | --- | --- |
| Bounded, reversible, known behavior; one local surface | Compact description: outcome, slice boundary, verification, rollback and one-line risk/coupling rationale | The doer can name an observable pass/fail result without guessing a critical requirement |
| Shared interface, multiple consumers, cross-component ordering or material failure impact | Native `plan` document: affected consumers, compatibility, ordered slices, failure/rollback path and integration check | Each dependency and slice has a concrete exit; unresolved critical requirements use the existing interaction/dependency path |
| Security/data boundary, irreversible effect, high uncertainty or broad coupling | Existing required design/risk process; explicit assumptions, failure cases, authorized executor and rollback limits; bounded prototype if it can answer an uncertainty safely | Required decisions are recorded and safety boundaries are resolved; a prototype is evidence, never permission |

Use the compact template below for small work; do not create a long design just
to fill headings. Add detail only for decisions a competent doer cannot safely
make alone. Preserve any stricter existing process. If risk or coupling grows,
record the changed premise and re-slice before continuing beyond the boundary.

## Resolve uncertainty with a disposable prototype

First name the question that could change the approach. Prefer inspecting
existing behavior/docs; prototype only if a small experiment is cheaper.
Record the hypothesis, a bounded time/resource budget, safe environment, evidence
and a stop condition. Stop when the question is answered or the budget is spent.
An inconclusive result stays inconclusive; do not silently extend the budget.

Use only authorized local fixtures or staging, never production data/mutations,
new spend or an unapproved external disclosure. Label the artifact throwaway and
record its disposition. Keeping prototype code requires an explicit decision
and the normal implementation, acceptance, security and review gates; “the spike
works” is not a merge or deployment exemption. Keep evidence without deleting
history. If no prototype is needed, write why rather than inventing one.

## Reassess remaining investment and order by risk

At a failed assumption, scope change, review milestone or material evidence
change, compare **remaining** cost with expected benefit and uncertainty. Past
effort is sunk cost, not a reason to continue. Record continue, narrow, replace
or stop the experiment, the evidence and the next decision trigger. Do not stop
a project or park an eligible card merely because it is lower priority.

Within the authorized project order, expose the highest-impact uncertainty or
failure boundary early, then ship the smallest useful outcome. State the risk
and ordering rationale; avoid false numeric precision. Required hardening,
failed checks and security fences cannot become optional followups. Native
readiness/dependency, capacity and explicit STOP rules still govern starts.

## Bound queued followups

A followup names one outcome, one doer, an observable exit/evidence path and a
reconsideration trigger (for example an integration milestone or new demand
evidence). Create a separate native task when authorized work is genuinely
needed; record its relation to this slice and real blocker edges if applicable.
Optional polish is not a blocker. No identified followup is a valid result.

Avoid speculative task trees. Reassess deferred work at its named trigger against
current benefit, cost and priority; never start it automatically just because
time passed. Use native assignment/dependencies/interactions for continuation;
a prose reminder creates no wake. Do not add host scripts, timers, hooks or
polling automation. A trigger is not permission and must not override a STOP.

## Copy-ready planning fragment

Put compact decisions in the existing task description; use its native `plan`
document when the selected depth needs detail. Replace all placeholders; an
explicit, justified “not needed” is better than a fictional experiment/followup.
Preserve existing requirement IDs, human decisions and approval boundaries.

```markdown
## Planning
- Depth and risk/coupling: <compact / coordinated / high-risk; concrete reason>.
- Slice: <one outcome + doer + observable exit/verification + rollback>.
  Excludes: <related work outside this deliverable>.
- Uncertainty/prototype: <question, hypothesis, budget, safe environment,
  stop condition, evidence and discard/retain decision; or not needed + why>.
- Investment review: <evidence/milestone trigger; remaining cost/benefit;
  continue/narrow/replace/stop decision, or initial decision + next trigger>.
- Risk order: <highest-impact uncertainty/failure boundary and why it goes first>.
- Followups: <task link, outcome/doer, exit evidence, reconsideration trigger,
  and real dependency; or none identified + why>.
```

For a compact documentation slice: reversible prose, no runtime coupling; one
doer publishes the guide and discovery link, checked by link inspection and
source review, reverted by reverting the PR. No prototype is needed because no
runtime uncertainty exists. Reassess if an executable mechanism becomes necessary;
prioritize preserving safety gates. No followups if the source is the complete
requested deliverable. This is an example, not evidence of execution or rollout.

## Adopt, measure and undo

The staged [agent seed](../templates/org-github/repo-template/AGENTS.md) includes
short rules. It is not the live company instruction bundle. Its authorized
maintainer applies reviewed source through the existing instruction/provisioning
path; no new skill assignment, runtime gate, plugin or core change is needed.
Inspect effective instructions and newly authored tasks before claiming rollout.

For a bounded pilot, retain baseline task IDs, selection rule, capture time,
original text/revision and the six filled decisions in a private task document.
Use only owned, non-conflicting tasks. Publish no private task text here. On the
**same cohort**, count tasks with concrete depth/risk rationale, slice boundary,
prototype disposition, investment review, risk order and bounded followup
disposition before and after. Report each component and complete-contract count
as `n/N`; placeholders and technique names do not count. An empty cohort is “no
data”, not 100%. Verify saved revisions, not merely successful command exits.

Label a self-owned one-task pilot a convenience sample. It measures explicit
planning adoption, **not** faster delivery, fewer defects or company-wide use.
Measure any delivery claim separately on completed work. Source PR merge is
publication; it does not prove effective instruction adoption. Closing evidence
states the metric, same-denominator before/after counts and unverified effects.

Undo by reverting the source PR. An authorized maintainer restores previously
adopted instructions through the same reviewed path. Supersede pilot text while
preserving intervening decisions and evidence; do not delete task history or
followup tasks. These prose changes introduce no runtime rollback obligation.
