# Offline profile matrix

`validate_profile_matrix.py` discovers every `*-image-profile.json` and
`*-pool-spec.json` in its directory, or in an explicitly supplied directory.
It validates the **complete** source matrix: role labels are eligible labels
minus `self-hosted`, disjoint between profiles and explicitly excluded by the
other profiles. Pool identifiers cannot be claimed across roles, and every
profile must retain `admission_authorized: false` and `installed: false`.

```sh
# Existing validation response and exits are unchanged.
python3 -B github-runner/garm/validate_profile_matrix.py
python3 -B github-runner/garm/validate_profile_matrix.py /path/to/source-matrix

# One exact source declaration, not a workflow selector or a live runner query.
python3 -B github-runner/garm/validate_profile_matrix.py --role-label=two-test
python3 -B github-runner/garm/validate_profile_matrix.py --help
```

## Literal role-label lookup

`--role-label LABEL` optionally queries the same validated source snapshot.
Matching is exact: no glob, regex, trimming, case folding, list splitting or
shell expansion is performed by the tool. Quote shell metacharacters when
invoking it from a shell; use `--role-label=VALUE` for a value beginning with `-`.
The directory may precede or follow the flag. New source profiles are discovered
without changing the lookup code.

A valid matrix produces one JSON object, schema `profile-role-lookup.v1`:

| Field | Meaning |
| --- | --- |
| `outcome` | `matched`, `shared_label`, or `unrecognized_label` |
| `role_label` | The exact queried string |
| `declaring_profile` | The uniquely declaring source filename, or `null` |
| `explicitly_excluding_profiles` | Sorted source filenames explicitly excluding that exact string |
| `explanation` | Bounded source-only/non-proof explanation |
| `admission_authorized`, `host_verified`, `migration_complete` | Always `false` |

Only the queried label and relevant source-file identities are returned, not
profile bodies, pool values or a list of other labels. Explicit exclusion is a
source declaration, not proof of where a runner can execute.

| Outcome | Exit | Non-match diagnostics and next action |
| --- | --- | --- |
| `matched` | 0 | Inspect the named source contract for the intended change. |
| `shared_label` | 2 | `code: shared_label`, `retryable: false`; choose an exact role-specific label from the intended profile. Literal `self-hosted` is never a unique role. |
| `unrecognized_label` | 2 | `code: unrecognized_label`, `retryable: false`; check the exact label and source directory; review any needed source change separately. |

Both non-matches include a bounded `next_action`. An excluded-only label remains
unrecognized, with its explicit excluders listed. Repeating the same query on the
same snapshot cannot change its outcome.

An invalid matrix takes precedence over **every** lookup, including shared and
unknown labels: exit 1 with the existing `profile_matrix_rejected` result and
field/filename violations, never a partial lookup result. Lookup rejections also
keep all three proof/authority flags false. Without the flag, the existing
validation JSON and exits remain unchanged.

**Declaration lookup does not prove live runner eligibility, selector
exclusivity, capacity, permission or host isolation.** It does not infer installed
state, inspect runners, contact a service, grant authority or modify files. The
lookup is inherently idempotent and dry-run; there is no mutating/live mode.
Source delivery is not runtime installation or operational activation.

## Local verification

From the repository root, using only local source/synthetic fixtures:

```sh
python3 -B -m unittest discover -s github-runner -p test_isolated_profile_matrix.py
python3 -B -m unittest -v test_profile_role_lookup_acceptance
```
