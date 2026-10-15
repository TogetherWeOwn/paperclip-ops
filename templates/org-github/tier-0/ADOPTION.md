# Tier-0 adoption: one caller line per repo (case_4784a03d)

Destination: this whole `tier-0/` directory lands in `TogetherWeOwn/.github`
(`tier-0-quality-gate.yml` at `.github/workflows/`, `semgrep-rules/` at the
repo root, `ADOPTION.md` beside them). Private reusable workflows are
unreachable from public callers and private repos have no Actions minutes,
so the org `.github` repo — public — is the only home that serves every
repo.

## Caller snippet (copy-paste)

```yaml
jobs:
  tier-0:
    name: Tier-0 quality gate (report-only)
    uses: TogetherWeOwn/.github/.github/workflows/tier-0-quality-gate.yml@<pinned-sha>
    with:
      mode: report-only # flip to enforcing per gate once its FP rate is <5% over 20 PRs
    permissions:
      contents: read
```

Cross-repo `uses:` needs a pinned SHA, never a branch name. `timeout-minutes`
is rejected on `uses:` jobs by GitHub schema — the gate sets its own
per-job timeouts internally.

## Feed `ci-ok` (one context, exactly once)

Add the caller job name (`tier-0`) to the repo's required aggregator
`needs:` list. `tier-0-ok` is the gate's only aggregator — never wire an
individual gate job into a required set. In `report-only` mode `tier-0-ok`
always passes; findings land in the step summary and the
`tier-0-findings` artifact.

## Rollout: report-only 24h, then required

1. Land the caller in `report-only` (default). The workflow never fails the
   run for its own findings — every gate job sets `continue-on-error`.
2. After 24h, read the `tier-0-findings` artifacts across PRs. A gate whose
   false-positive rate over the last 20 PRs measures under 5% flips to
   `mode: enforcing` (whole gate first; per-gate inputs when the workflow
   grows them).
3. The org ruleset (`../rulesets/`) requires `ci-ok` on every active repo,
   so the gate rides the existing required check — no branch-protection
   edit per repo.

## Repo-owned extension points (adopt to enable, never required to land)

- `.tier0-generators` — one shell command per line, run in order before the
  drift check (schema:check, OpenAPI codegen, lockfile refresh). Lines
  starting with `#` are comments. Reviewed like code.
- `.tier0-tests` — executable entrypoint for Gate 6. Receives
  `TIER0_CHANGED_FILES` (newline-separated), `TIER0_COVERAGE_THRESHOLD`
  (percent), `TIER0_MUTATION_BUDGET_MIN`, `TIER0_ENABLE_MUTATION`. Fails
  when changed-line coverage is under threshold or a mutant on a changed
  line survives. Without it Gate 6 reports its absence and skips.
- `semgrep-rules/` — copy the org starter pack from this directory, then
  enable the two templates by naming the repo's own helpers/sinks. The
  templates ship disabled (stub callees match nothing) so they cannot burn
  the 5% false-positive budget on day one.

## Per-repo adoption checklist (one PR per repo)

- [ ] Caller job added with pinned SHA, `mode: report-only`
- [ ] Caller job in the repo's required-aggregator `needs:` list
- [ ] No `paths:`/`paths-ignore:` on the caller workflow (Gate 4 fails it)
- [ ] `semgrep-rules/` copied where the repo wants it (or default path kept)
- [ ] `.tier0-generators` / `.tier0-tests` added when the repo is ready
- [ ] After 24h green: flip to enforcing on measured evidence, then confirm
      the org ruleset shows the repo compliant
