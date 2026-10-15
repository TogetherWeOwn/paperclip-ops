# Selector vs bridge once MUSE is withdrawn

Refs: case_bd95ab6f, residual of case_ec9827b5 (`docs/lane-withdrawal.md`), finding from
[case_adc03405](/TOG/issues/case_adc03405#document-shadow-agreement-v2).

## What this corrects

`docs/lane-withdrawal.md` recorded a residual: with MUSE withdrawn, in `claude_5h_below_98` the
selector picks `gpt-6.1-sol` by cost ($1.90 against $2.41) while the bridge sets
`claude-sonnet-5-5`, so a "Claude over Codex" preference rule was missing.

That reading was an artifact of the case_ec9827b5 replay, not of the engine. The replay ledgered only
`cliproxy-meta`. Claude, Codex and Z.ai then had no pace verdict, ranked `unknown`, tied, and the
engine fell through to cost order. With every lane that hosts a candidate in the ledger, the
engine never reaches cost order between lanes: pace state and deviation decide first. Cost puts
sol ahead of nothing, and the gap that remains is not a missing preference rule (below).

## What orders the candidates

`orderCandidatesByPace` (`src/engine/pacing.ts`), within one tier: preferred-near-reset, then pace
state (`behind-urgent` < `behind` < `on` < `unknown` < `free` < `ahead` < `exhausted`), then lane
deviation (lower first), then cost, then release date. In the recorded T1 stream:

- Z.ai trails pace (deviation -0.33 to -0.36, state `behind`): it ranks first among the non-MUSE rows.
- Claude and Codex are both `ahead`. Through 22:01Z Claude was the less-ahead lane (0.17 to 0.38
  against Codex's 0.40), so deviation put Claude first. Cost never gets a say.
- The slot throttle does not change the winner. Both lanes carry slot factor 0.25 and the throttle
  compares one per-issue hash with each lane's factor, so an issue clears both or neither; when
  neither clears, the walk takes index 0. Two `ahead` lanes share one coin and cannot split load.
- `lane-avoid` matters in one place only: it is the reason `glm-5.3` is sometimes not a candidate
  (next section).

## Where the selector and the bridge actually part

Replay of every recorded T1 `plugin-shadow` decision that overlaps the bridge log
(n=633, 2026-10-04 00:23Z to 22:29Z), all lanes ledgered, MUSE withdrawn at the ceiling the bridge
was using in each regime. Baseline fidelity first: with no ceiling the replay reproduces the
selector's own recorded pace-ordering trace on 633/633 records and its recorded pick on 633/633.

| Bridge regime | Bridge sets | n | Selector picks | Agree |
|---|---|---|---|---|
| `muse_headroom_expires_first` | MUSE | 60 | MUSE 60 | 60 |
| `meta_weekly_below_98` | MUSE | 277 | MUSE 277 | 277 |
| `claude_5h_below_98` | claude-sonnet-5-5 | 276 | glm-5.3 164, claude-sonnet-5-5 110, gpt-6.1-sol 2 | 110 |
| `muse_collective_95` | gpt-6.1-sol | 20 | glm-5.3 9, claude-sonnet-5-5 9, gpt-6.1-sol 2 | 2 |

Agreement in the withdrawal regimes is 37.8% (112 of 296). It splits three ways.

**1. Z.ai, by agent role (`claude_5h_below_98`, 164 of 276).** `glm-5.3` is a candidate exactly when
the card's agent is not in `ZAI_LONG_RUN_AGENTS` (the 2026-09-08 owner rule: Z.ai rejects very long
engineering turns, error 1214, so it stays on reviewer, QA and mechanical roles). Checked against the
current assignee of 60 records sampled from this regime: 30 of 30 with `glm-5.3` were non-engineering
agents, 30 of 30 without it were Founding, Automation, DevOps, Director or CTO. For the non-engineers the
selector picks `glm-5.3`, because the Z.ai lane trails pace and the gas pedal prefers it. The bridge
manages three models and has no Z.ai.

**2. Claude against Codex, for the long-run engineers (112 records, 110 agree).** Until 22:27Z the
selector picked sonnet every time, because Claude was the less-ahead lane. The last two records are the
first after the order flipped: Claude's lane deviation reads 0.428 against Codex's 0.401 (22:30Z), pace
now orders `glm-5.3 > gpt-6.1-sol > claude-sonnet-5-5`, and the engineers get sol while the bridge still
sets sonnet (its `claude_5h_combined` reads 49). So the sonnet-over-sol agreement was incidental and has
just ended; `tests/case_bd95ab6f-full-lane-replay.spec.ts` pins the flip.

**3. The `muse_collective_95` era (9 of 20 pick sonnet while the bridge is on sol).** Both Claude
accounts read healthy and `ahead` in the selector's lane document, the bridge was on FALLBACK. This is
the only divergence whose cause is not in the data held here: the bridge log carries no Claude fields
before 2026-10-04T17:28Z, and the host mirror of `fleet_quota_balancer.py` is stale (case_e64c9469 is the
Operator card for the live script). The 2 sol picks are long-run engineers while both Claude accounts are
tripped, which is the bridge's choice too.

## Decision

CTO decision (case_2503e8f9): **A, the bridge follows the selector.** No PRIMARY-first rule is built. The
reasoning and the amended case_adc03405 bar follow; the `muse_collective_95` 9 of 20 stays an open
classification tied to case_e64c9469, not a closed one.

Reproducing the bridge would mean a static PRIMARY-first rule (Claude, then Codex, then the rest). The
evidence argues against building it:

- Sonnet over sol held only while Claude was the less-ahead lane, and stopped at 22:27Z. A static
  rule would now override the pace order for the engineers: it would keep feeding Claude, the lane
  further ahead of its weekly allowance (deviation 0.428 against 0.401), which is the over-draw the
  supply-paced goal (case_91cf972c) exists to prevent.
- The bridge's own 5h rule (`claude_5h_below_98`) is a burst rule on a window that resets every five
  hours; the selector's rule is the weekly pace. They answer different questions, and the weekly
  window is the one that runs out.
- The bridge retires at enforce.

The case_adc03405 bar therefore changes from raw T1 agreement to: zero MUSE picks while the bridge withdraws
MUSE (met by case_ec9827b5); zero picks of a lane the bridge holds; and every other disagreement attributed to a named rule (Z.ai role rule,
Claude/Codex deviation order) with its count. The second clause is measurable for `claude_5h_below_98`
(no pick of a held lane) and not yet for the `muse_collective_95` era.

The alternative, a per-lane preference in `pacing.lanes[]` read ahead of pace state inside
`orderCandidatesByPace`, was not built: it contradicts the pace rule above.

## The replay

`tests/case_bd95ab6f-full-lane-replay.spec.ts` replays the 23 records of the case_ec9827b5 replay with the
companion lanes in `tests/fixtures/withdrawal-replay/t1-other-lanes.json`. It pins: the rebuilt lane
read matches the recorded lane label and slot factor; the baseline reproduces the recorded pace
ordering and pick on 23/23; the Meta-only ledger reproduces the old "sol" reading; with every lane the
pick is the head of the pace order (in this cut never sol while Claude or Z.ai can serve); every
`claude_5h_below_98` disagreement with the bridge is a Z.ai pick; sonnet over sol flips when Claude is
the more-ahead lane; and the two-`ahead`-lanes throttle case. Two hand-applied mutants (behind/ahead rank swapped, deviation tiebreak removed) are each killed
by several of these.

Two stand-ins, both stated in the spec: the live lane margin is not in the repo, and any margin in
[0.121, 0.174) reproduces all 66 recorded lane labels (0.15 is used for the non-Meta lanes); and the
snapshot carries only the governing window, so the one Claude account recorded unserviceable by a
five-hour trip gets a stand-in five-hour window at the trip ceiling.

The population figures above come from the same replay run over every record (the stream is bounded,
about 100 records per hour per writer, so the hourly shards rotate). The export and replay scripts are
documents `population-replay-export` and `population-replay-spec` on case_bd95ab6f. Re-run them across the
2026-10-05T00:00Z Meta reset, which is the re-run case_adc03405's GO condition names.
