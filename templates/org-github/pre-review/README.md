# Pre-review gate: staged source for the org template repo

Author self-review before a PR is marked ready: a deterministic script plus
a 10-bucket rubric. No model calls, network, or credentials; Cargo metadata
runs offline only when a Cargo manifest changes, using the operator's Cargo
cache. A tracked lockfile is checked with `--locked`; projects without a
tracked lockfile are checked for offline dependency resolution without
creating or committing a lockfile. Submitted `.cargo` configuration is removed
from the snapshot and compiler wrappers are overridden. Other checks use bash/git.

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
Strong credential families (private-key headers, `AKIA`, `ghp_`,
`github_pat_`, `xox[bap]-`, `api_key` assignments) are checked first against
the original added bytes in every file, so nothing below can suppress them.

- **Quoted and bare values.** For `token`, `secret` and `password`
  assignments, quoted values with 12 or more consecutive non-space,
  non-quote characters fail. Bare values also exclude dots and parentheses so
  method calls and long function names are not mistaken for credentials. A
  bare value ends at a space, comma, semicolon, comment hash, either quote, or
  end of line. Quotes count as boundaries even when the whole key/value pair
  is inside a string, such as a generated config line.
- **`api_key` values.** Unquoted values keep dots (a SendGrid-style
  `SG.<segment>.<segment>` key fails). Only an unquoted value that starts with
  an environment/config lookup (`process.env.`, `os.environ.`,
  `import.meta.env.`, `ENV.`, `settings.`, `config.`) passes; the same text in
  quotes is a string literal and fails. A lookup exempts only its own value: a
  quoted 12+ literal behind it (`|| "..."`, `?? "..."`, `or "..."`) fails, as
  does a second argument to an environment method that takes a default
  (`os.environ.get`, `os.environ.setdefault`, `os.environ.pop`, `os.getenv`,
  `ENV.fetch`), whether positional or passed as `default=`. The fallback scan
  tracks quotes and delimiters, including
  whitespace and nested default calls; operators inside key-name strings do
  not count. A second argument to `config.get` or `settings.get` is a key
  name, not a default, and passes. Unrelated sibling properties/statements
  are outside the lookup expression. An unquoted fallback is not examined.
- **Call and parenthesis forms.** `token(...)`/`token = (...)` values fail
  only when literal-shaped: a quoted 12+ value, or an unquoted run that is not
  a bare identifier (underscore) or a CamelCase type reference. Base64 padding
  (`=`) after the run is part of the value.
- **Rust.** Rust strings and comments (raw, escaped, byte, nested block) are
  never exempt. In plain code, a match wholly inside a recognized type span
  (`token: Option<Token>,`, `password: impl AsRef<[u8]>`, fn parameters,
  inline struct fields) is a declaration, not a credential. After a credential
  word, every argument of a generic list closed on the same line is checked
  (an unclosed list checks its first argument), after `a::b::` module path
  prefixes are dropped. An argument fails only when it is literal-shaped: an
  identifier run of 12+ characters that is lowercase-initial or carries a
  digit or dash, or any character outside type syntax. Type syntax includes
  macro and const-generic characters (`!`, `#`, `$`, `{`, `}`), so
  `Option<Token![:]>,`, `Result<String, !>` and `MyBuffer<{ 32 }>` pass.
  CamelCase type names without digits pass. Ambiguous input keeps detection.
- **Collection.** Paths are NUL-delimited and read with literal pathspecs;
  any read, diff or binary-detection failure fails the bucket closed.

## Local verification (no credentials)

```sh
./templates/org-github/tests/test_pre_review.sh
```

Covers all 10 buckets (FAIL and PASS paths), exit codes, the pass/fail
table shape, `--public` / `--pr-body` / `--pr-title` modes, stale-base and
dirty-tree warnings, hermetic runs in throwaway repos, and the public-safe
self-check on the staged files.
