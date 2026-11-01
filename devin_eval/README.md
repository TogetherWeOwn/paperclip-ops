# Devin Review eval harness

`eval_devin.py` measures Devin Review, a merge-gate reviewer, against adjudicated
findings. Collection makes only `gh api --method GET` calls under `/repos/`; scoring
is offline and deterministic. Two scoring runs on the same inputs write
byte-identical files. Metrics are computed by code. Judgment (is this finding real,
is this escape a miss) lives in the adjudication files the round owner writes.

## Commands

The work directory must be outside this repository. Snapshots, adjudications and
outputs go on the round card, never into git.

```sh
cd devin_eval
python3 -I eval_devin.py collect --workdir "$W" --round r2 --since "$PREVIOUS_ROUND_STARTED_AT"
python3 -I eval_devin.py score   --workdir "$W" --round r2 --prior-metrics "$W/out/r1/metrics.json"
python3 -I eval_devin.py golden  --workdir "$W" --round r2
# A snapshot collected before version manifests existed:
python3 -I eval_devin.py freeze-manifest --workdir "$W" --round r1-early
```

`--since` must be in the past. Use the previous round's `snapshot_started_at`
(from its `metrics.json`) so consecutive rounds leave no gap. The cutoff is
inclusive, and reviews posted while the previous round was collecting can appear
in both rounds: compare rounds by finding key, never by adding their counts. For
the first round, use the moment the change under test landed on main. A cutoff
later than every review collects an empty round.

Layout of `$W`:

- inputs: `snapshots/<round>/`, `adjudications.json`, `reference.json`,
  `reference-audit.json`;
- outputs: `out/<round>/metrics.json`, `normalized.json`, `adjudication-queue.json`
  and `golden-score.json`.

Tests: `python3 -B -m unittest -v test_eval_devin` (stubbed client, no network).

## What the metrics mean

- **REVIEW.md version.** At collect time the harness reads
  `GET /repos/{r}/commits?path=REVIEW.md&sha=main` and the root blob at each
  commit. It freezes every version, text included, in
  `snapshots/<round>/review-md-versions.json`.
  - Every review and finding gets a `review_md_version` label, such as
    `two-web-next@v3:bd8e33f6`.
  - A head *qualifies* when its blob is a main version at or after the pinned
    v2 merge (`V2_MERGES`), and its PR does not edit any `REVIEW.md`.
  - A repository without a pin never qualifies.
  - `post_v2_quality` scores only qualifying heads, overall and per version.
- **Escapes.** The collector looks back 28 days and finds candidate PRs from
  three sources:
  - revert commits on main (`This reverts commit X`, resolved through
    `/commits/X/pulls`);
  - `Regression-of: owner/repo#N` lines in PR bodies;
  - issues labelled `regression` that link a PR.

  Each candidate reports whether Devin posted a `Completed analysis` status on
  the merge head before the merge. A candidate is not an escape until someone
  adjudicates it.
- **Freshness at merge.** For PRs merged since the cutoff, this checks whether
  Devin reviewed the final head before the merge, and lists the misses.
- **Thread answers.** A Devin inline thread counts as answered when a non-bot
  reply, posted before merge, starts with `Fixed in <sha>`,
  `Rejected: <proof>` or `Duplicate of <link>`. `loose_frd_secondary` keeps
  the older, looser match.
- **Rule ids.** Each REVIEW.md version is parsed into stable ids:
  - `<section>` for each heading below the title;
  - `<section>/<bold-lead-in>` for each bold lead-in.

  Adjudications may set `rule_id`, and an unknown id is refused. The output
  counts Devin true findings per rule at qualifying heads. It also lists rules
  with 0 true findings in this round and the prior one, but only for
  repositories that had qualifying heads in both rounds.
- **Golden set.** `golden.json` holds 8 adjudicated blocking true defects from
  r0 and r1-early: 2 each of behavior, test_gap, gate and safety, across 4
  repositories, with 2 caught and 6 missed.
  - `golden` matches Devin findings at the exact golden head by file, with a
    line window (default ±5).
  - Score it on a snapshot that contains the golden heads: the baseline
    snapshots, or a replay round collected with a `--since` earlier than the
    golden PRs' Devin reviews.
  - A case whose head has no Devin review in the snapshot is reported as
    `unobserved`, never `missed`. `caught_by_kind` is `[caught, observed]`.
  - Unmatched Devin findings on golden heads go to the adjudication queue.
  - Mechanisms are summaries only, with no exploit detail.
- **Status signals.** The harness flags every `Devin Review` status whose
  description does not start with `Completed analysis`, as a cost or health
  signal. A pending status on a SHA that also has a terminal status is counted
  but not flagged.

## Golden PRs and PR automation

Golden PRs are opened only when someone proposes a REVIEW.md change. Each one
replays a case's base and head and carries the `eval-golden` label.

Neither PR plugin reads labels. Both act only through an owning card:

- `paperclip-plugin-pr-doer-wake` (reviewed at branch head `5d25d998`, still
  unmerged) resolves the owner from three places:
  - a branch name matching `^[A-Z][A-Z0-9]*-[0-9]+-`;
  - a card comment containing the PR's full URL;
  - an open card's description containing the PR's full URL.
- `paperclip-plugin-pr-tracker` (main `22781be1`) wakes only a resolved
  tracking task. Otherwise the PR is `untracked`.

Until a plugin skips `eval-golden` itself, follow this workaround:

- name golden branches `eval-golden/<case-id>`;
- keep card identifiers out of the PR title and body;
- refer to a golden PR on cards as `owner/repo#N` text, never its full URL;
- close golden PRs unmerged.
