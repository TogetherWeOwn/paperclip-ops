# Orphan-suite check outcomes

`verification/orphan_suites.py` is an offline, read-only CI-wiring checker. It
uses bounded Git inspection and reads workflow/script text; it never executes
checked-tree code, uses the network, writes to the checked tree, wires suites,
or adds exemptions. Running it is already its dry-run. Detection and exemption
rules are the same in both output modes.

```sh
python3 -B verification/orphan_suites.py --root . --json
```

Options: `--root DIR` selects the repository directory; `--allowlist PATH`
overrides `verification/orphan_suites_allowlist.json` under that root. Relative
allowlist paths are relative to the selected root, not the caller's directory.
Omit `--json` for the existing `PASS:`/`FAIL:` text output. `check(root, allowlist)`
continues to return a list of error strings in the existing order.

## JSON contract: `orphan-suites.v1`

A check with `--json` prints exactly one deterministic JSON object to stdout,
with no additional prose or stderr:

- `schema`: `"orphan-suites.v1"`.
- `outcome`: `"clean"`, `"findings"`, or `"unmeasurable"`.
- `check_complete`: true for clean/findings; false for unmeasurable.
- `suite_count`, `exemption_count`, `finding_count`: integers on completion;
  **all null** if analysis could not finish. Exemptions count allowlist entries,
  including invalid or stale entries; findings count diagnostics, not unique
  paths (one path can have multiple findings).
- `findings`: entries sorted by `(path, code)`; empty for clean/unmeasurable.
- `diagnostic`: null on completion; one failure entry for unmeasurable.

Every finding/failure entry has exactly `code`, `path`, `retryable`, and
`next_action`. Finding paths are repository-relative strings. Failure paths name
the allowlist when it resolves within the selected root, otherwise null. Unknown
paths are null. JSON escaping preserves spaces, Unicode and control characters
in filenames; machine identity never depends on parsing prose.

| Finding code | Next action |
| --- | --- |
| `orphan-suite` | wire the suite into CI or add an exemption with a one-line reason |
| `exemption-invalid-reason` | give the exemption a one-line reason |
| `exemption-stale-not-a-suite` | remove the exemption |
| `exemption-stale-now-wired` | remove the exemption |

| Failure code | Next action |
| --- | --- |
| `allowlist-missing` | create the allowlist file or pass --allowlist |
| `allowlist-unreadable` | make the allowlist file readable |
| `allowlist-malformed` | fix the allowlist to be a JSON object of path to reason |
| `root-invalid` | pass an existing repository directory as --root |
| `git-failed` | fix git access for the root and retry |
| `analysis-failed` | fix the input the checker names and rerun |

Only `git-failed` is retryable. `root-invalid` covers a missing/non-directory root
or a directory outside a Git work tree; failed Git inspection of an apparent
work tree is `git-failed`. Git commands time out after 30 seconds. A failed scan
never publishes a complete clean result or unsupported counts.

## Exit and usage boundaries

- **0:** completed, clean check.
- **1:** policy findings **or** an unmeasurable check. Inspect `outcome` and
  `check_complete`; exit 1 alone does not prove a wiring defect.
- **2:** usage error.

`--help`/`-h` always print text, including with `--json`. Invalid options,
unexpected operands and missing option arguments also print text usage (exit 2),
never JSON. Parse stdout as JSON only for an actual `--json` check, not a help or
usage response. Keep text output for troubleshooting input errors.

## Offline verification

```sh
python3 -B -m unittest -v test_orphan_suites test_orphan_suite_json_acceptance
```

These miniature-repository fixtures cover the shared detector, text compatibility,
outcome/error separation, counts, deterministic codes and paths, special filenames,
help/usage boundaries and unchanged checked-tree files. They run in both the local
fast verification tier and CI; the acceptance file is maintained independently.
This source contract is not an installation or deployment claim.
