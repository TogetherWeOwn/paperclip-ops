# PR standards and native squash groups

The existing required `pr-lint` job runs the
[standards checker](../.github/scripts/pr_standards.py). Pull requests validate
the PR title and body; pushes validate their supplied commit messages. A native
merge group has neither a PR object nor the push `commits` array. Treating it as
an empty push would silently skip convention validation.

## Native merge-group input

For `EVENT=merge_group`, the checker reads the runner's `GITHUB_EVENT_PATH` JSON
file. It requires `action=checks_requested`, a `merge_group` object, and raw full
nonzero lowercase `base_sha` and `head_sha` values. The event head must equal both
`GITHUB_SHA` and the checked-out `HEAD`. Missing or malformed evidence is always
an error, including in warning mode; `COMMITS` cannot replace native evidence.

This repository uses a **SQUASH** merge queue. The checker resolves every queued
squash on the local first-parent chain from the native base, exclusive, to the
native head, inclusive. The chain must be nonempty, complete, linear and bounded
by that exact base. Missing objects, unavailable history, unrelated bases and
merge-parent topology fail closed. Git reads disable replacement objects and
have a ten-second timeout per command; the checker makes no network requests.
The [workflow](../.github/workflows/pr-lint.yml) fetches complete history and keeps
checkout credentials unpersisted.

Each queued squash is checked for its Conventional Commits subject and the
existing full-message public-reference policy. Generated `Merge`/`Revert`
subjects are not exempt in this queue-specific path. Original feature-branch
commits that were squashed away are not checked. Existing PR and push behavior,
including push exemptions and warning/error severity, remains unchanged.

This is a repository-specific squash-history contract, not support for arbitrary
merge/rebase queues. A queue strategy change needs a corresponding reviewed
checker change; it must not turn unsupported history into successful empty work.
No required-check, review, permission or ruleset change is implied.

## Focused offline verification

```sh
python3 -B -m unittest discover -s .github/scripts -p 'test_pr_standards*.py'
actionlint .github/workflows/pr-lint.yml
```

The native-group acceptance suite builds isolated local Git histories and event
files, including multi-entry groups, invalid non-tip messages, shallow/missing
objects and malformed native data. These are offline fixtures, not manufactured
GitHub events or runtime queue acceptance. Record genuine `merge_group` run IDs,
full head SHAs, step conclusions and remaining limitations separately.

Rollback is a reviewed revert of the checker, workflow and documentation change.
Do not bypass a failed required check or change the queue rules to admit it.
