# Capacity and trace documentation mapping verification

Synthetic, offline Git controls only. No live capacity or trace input was read,
and neither runtime was executed. This is accompaniment evidence, not semantic
correctness, capacity admission, deployment authorization or evidence freshness.

## Provenance

- Checker and unchanged tests at source commit `f4ec7ab15acdc4810cac9d6fe1b9138fa1a01023`.
- Checker SHA-256: `5bc8f3a21e0db4bae9fe330e436779f53f28f54dd91d881171922860a615a857`.
- Proposed `.docs-drift.json` SHA-256: `e1adc3d70eca8ab32e0203ad93a15c2475cafd6fe40cb48f37bd15be45a76fbc`.
- Common synthetic policy base: `b17d3c11266b16a5c84482c5eed585a821cc71e5`.
- Added-doc controls use a child base lacking that exact doc; all other controls use the common base.
- Fixture content is synthetic text; source files are never executed.
- All existing policy rules are included; no rules are replaced.

## Commands and outcomes

- `python3 -B -m unittest -v test_docs_drift`: 26 tests passed both before and after the policy/doc edits; the suite is unchanged.
- Each control invokes the real checker with `--repo <fixture-repo> --base <report.base_sha> --head <report.head_sha>`.
- All 22 controls matched expected exits, status, checked-rule counts and exact finding rule/path.
- Each of the three mapped source paths has seven controls: code-only refusal, modified and added canonical-doc passes, unrelated-doc refusal, deleted-doc refusal, chmod-only-doc refusal and head-rule-removal refusal.
- The unrelated-doc controls edit the other new contract's canonical page, proving it cannot satisfy this mapping.
- Removal controls also edit `docs/docs-drift.md`, satisfying the original policy rule while leaving the removed source rule unsatisfied. `checked_rules=2` and only the base-owned source rule finds.
- One unrelated-file control returns clean with zero checked rules; that is not a coverage claim.
- No checker, source, workflow or test file is modified by this change.

## Replaying immutable receipts

The review evidence includes `offline-doc-contract-controls.bundle` and the raw
`offline-doc-contract-controls.json` receipt on the delivery task. The bundle
SHA-256 is `09d14b4665f2f6045a2b6d5cefd75c0ba311dda25e1641e0ef2e7fba3f7f502c`.
To replay without creating new fixture commits:

```sh
git clone offline-doc-contract-controls.bundle fixture-repo
python3 scripts/quality-gates/docs_drift.py --repo fixture-repo \
  --base <base_sha-from-receipt> --head <head_sha-from-receipt>
```

The bundle contains `main` and `control-01` through `control-22`. Inspect exact
changes with `git -C fixture-repo diff <base_sha> <head_sha>`; use the full SHAs
in each JSON row below, not a moving branch ref.

## Exact JSON and exit receipts

### control-01: coolify_capacity_headroom.sh / code-only

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 1, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.sh"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "1e7cb209f1c8c8df8d640e6d26d473e2e4e80d75", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-02: coolify_capacity_headroom.sh / matching-doc-content-modified

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "179adab2aa45b5eb91faf2db7cb48506c7a083ad", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "clean"}
```

### control-03: coolify_capacity_headroom.sh / matching-doc-added

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "2540d690a5a831371eb3a48cde188f62c0cb4b23", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "10f2b0367640c6643931b79286a6e0382e5b0341", "merge_base_sha": "2540d690a5a831371eb3a48cde188f62c0cb4b23", "policy_sha": "2540d690a5a831371eb3a48cde188f62c0cb4b23", "schema": "docs-drift.v1", "status": "clean"}
```

### control-04: coolify_capacity_headroom.sh / unrelated-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.sh"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "d12acef8aff0b12d7c3dcc651dd9385b8c730264", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-05: coolify_capacity_headroom.sh / deleted-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.sh"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "55481f97140f5fc25b261347f7efb9f7819d55c9", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-06: coolify_capacity_headroom.sh / chmod-only-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.sh"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "c40402d9c103aa8b7c0910e027f1787340cf413c", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-07: coolify_capacity_headroom.sh / head-rule-removed

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 3, "checked_rules": 2, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.sh"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "afc2d93df13c17942562634de63e8f18513d49da", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-08: coolify_capacity_headroom.py / code-only

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 1, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.py"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "6fca941b0b045dc80e67c5845803050dd4948730", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-09: coolify_capacity_headroom.py / matching-doc-content-modified

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "8616f151403748624a4cc3c5056be0bbfe377843", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "clean"}
```

### control-10: coolify_capacity_headroom.py / matching-doc-added

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "e8ba5ea3651e76094c03167325eedd80b0ec7b01", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "eef984691db51efaec392c34eafe34afc81f4c23", "merge_base_sha": "e8ba5ea3651e76094c03167325eedd80b0ec7b01", "policy_sha": "e8ba5ea3651e76094c03167325eedd80b0ec7b01", "schema": "docs-drift.v1", "status": "clean"}
```

### control-11: coolify_capacity_headroom.py / unrelated-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.py"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "ff1ffbe67296e1fc687353e0a5c6ba0fd153dd11", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-12: coolify_capacity_headroom.py / deleted-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.py"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "6e305ff46c2c09f2b2a2e13be671625c16a2ee6a", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-13: coolify_capacity_headroom.py / chmod-only-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.py"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "3064570ebc0cf2a8747e9ee5b4c3f255f2d8f29f", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-14: coolify_capacity_headroom.py / head-rule-removed

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 3, "checked_rules": 2, "findings": [{"code": "docs_required", "docs": ["docs/capacity-evidence.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["coolify_capacity_headroom.py"], "retryable": false, "rule": "capacity-evidence-contract"}], "head_sha": "946b75ae46e8139065ef9121de02b3bad74cf266", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-15: ops/paperclip/trace_correlator.py / code-only

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 1, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/trace-correlation.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["ops/paperclip/trace_correlator.py"], "retryable": false, "rule": "trace-correlation-contract"}], "head_sha": "bad127196cb3b92fff0016e17fefcca2df4df62b", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-16: ops/paperclip/trace_correlator.py / matching-doc-content-modified

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "06ada6c843333278600ec44aaa620d58820c52ce", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "clean"}
```

### control-17: ops/paperclip/trace_correlator.py / matching-doc-added

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "35fade0d10419b37d83157537925cdf693433ae6", "changed_paths": 2, "checked_rules": 1, "findings": [], "head_sha": "55acc6b2c2b785d8e38244c6a0686b38b50bfa2d", "merge_base_sha": "35fade0d10419b37d83157537925cdf693433ae6", "policy_sha": "35fade0d10419b37d83157537925cdf693433ae6", "schema": "docs-drift.v1", "status": "clean"}
```

### control-18: ops/paperclip/trace_correlator.py / unrelated-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/trace-correlation.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["ops/paperclip/trace_correlator.py"], "retryable": false, "rule": "trace-correlation-contract"}], "head_sha": "5ba304533a09a977c879cabc344cedcfe4e3c4e8", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-19: ops/paperclip/trace_correlator.py / deleted-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/trace-correlation.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["ops/paperclip/trace_correlator.py"], "retryable": false, "rule": "trace-correlation-contract"}], "head_sha": "aa70e78eb3844efc484006d6aab26f6f6754d12c", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-20: ops/paperclip/trace_correlator.py / chmod-only-doc

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 2, "checked_rules": 1, "findings": [{"code": "docs_required", "docs": ["docs/trace-correlation.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["ops/paperclip/trace_correlator.py"], "retryable": false, "rule": "trace-correlation-contract"}], "head_sha": "b6974a2f4e51440f96f3e36d32edade129d3e958", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-21: ops/paperclip/trace_correlator.py / head-rule-removed

Expected and observed exit: **1**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 3, "checked_rules": 2, "findings": [{"code": "docs_required", "docs": ["docs/trace-correlation.md"], "next_action": "Add or edit a separate mapped documentation file in this patch.", "paths": ["ops/paperclip/trace_correlator.py"], "retryable": false, "rule": "trace-correlation-contract"}], "head_sha": "ffad96ea9b14cfb80a5adbb5b8006cf9fbffc3df", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "finding"}
```

### control-22: unmapped file / unrelated-file

Expected and observed exit: **0**. Stderr: empty.

```json
{"base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "changed_paths": 1, "checked_rules": 0, "findings": [], "head_sha": "e982bd4cbfd419b2da77aea39370dcc758145bc0", "merge_base_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "policy_sha": "b17d3c11266b16a5c84482c5eed585a821cc71e5", "schema": "docs-drift.v1", "status": "clean"}
```

## Scope, undo and follow-up

ACT: add only the capacity and trace exact-path rules and canonical documentation.
WHY: require the documented contract to accompany changes using the existing gate,
without broad wildcards or a parallel registry. UNDO: revert this mapping/docs
change through normal review; do not disable the checker or turn errors into passes.

Mappings begin enforcing only after merge into the target policy. The current
PR's normal check uses the old target policy; the synthetic policy bases above
are the evidence for the new mappings, not a claim of installation.

Within seven days of merge, measure applicable PRs that touch any of the three
mapped sources and record accompaniment findings/corrections separately from
these synthetic controls. Zero applicable PRs means no real-world observations.
