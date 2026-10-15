# Pre-review gate: staged source for the org template repo

Author self-review before a PR is marked ready: a deterministic script plus
a 10-bucket rubric. No model calls, no network, no credentials — bash and
git only — so it runs anywhere, on any subscription.

## Destination map

| This file | Goes to (in the org `.github` repo) |
|---|---|
| `pre-review.sh` | `pre-review/pre-review.sh` (authors run it on the diff) |
| `RUBRIC.md` | `pre-review/RUBRIC.md` (the 10-bucket judgment self-check) |
| `AGENT-RULE.md` | `pre-review/AGENT-RULE.md` (the one-line bundle rule + attestation) |
| `MEASURE.md` | reference only (stays here; cites internal evidence, never ships) |
| `tests/test_pre_review.sh` | reference only (`templates/org-github/tests/`; hermetic fixture suite) |

## Adoption wiring (required, not delivered by copying files)

1. Copy the three shipping files to `pre-review/` in the org repo from a
   public-safe branch (this staging copy is already public-safe: no
   internal IDs, hosts, or agent names — the suite asserts that with no
   exclusion list). Set `PRE_REVIEW_PRIVATE_PATTERNS` to the org's
   internal tracker prefixes (space-separated EREs) wherever `--public`
   runs, so private IDs fail the scan on public repos.
2. Add the rubric checklist to the org PR template as a self-review section
   (checkboxes for the 10 buckets + the attestation line).
3. Point every engineering agent bundle at `AGENT-RULE.md` (one line by
   reference; do not paste the rubric into bundles).
4. Request the independent review only on ready heads after required CI is
   green; reviewers then sample, not re-scan.

## Secret-pattern boundaries

The `authz` bucket is a heuristic over added lines, not a language parser.
For `token`, `secret` and `password` assignments, it flags quoted values with
12 or more consecutive non-space, non-quote characters. Bare values also
exclude dots and parentheses so method calls and long function names are not
mistaken for credentials. A bare value ends at a space, comma, semicolon,
comment hash, either quote, or end of line. Quotes count as boundaries even
when the whole key/value pair is inside a string, such as a generated config
line. Provider-specific patterns remain independent of this check.

## Local verification (no credentials)

```sh
./templates/org-github/tests/test_pre_review.sh
```

Covers all 10 buckets (FAIL and PASS paths), exit codes, the pass/fail
table shape, `--public` / `--pr-body` / `--pr-title` modes, stale-base and
dirty-tree warnings, hermetic runs in throwaway repos, and the public-safe
self-check on the staged files.
