# Upstream gate compatibility contract

The fail-closed contract for filing upstream PRs against repos whose CI does
not use this repo's default check names, and for upstream repos that publish
no PR template. This is the only public location of this contract.

Publication of upstream filings stays HOLD; this document changes no filing
behavior by itself. Enforcement lives in `upstream_gate.sh` (profile
dispatch in `eval_fork_ci` / `eval_upstream_ci`), `upstream_lint.py`
(absence memory + freshness), and `upstream_ci_profiles.json` (the registry).

## 1. Reviewed CI profiles (contract A)

### Registry

One reviewed source of truth, co-located with the gate so standalone tool
copies keep working: `upstream_ci_profiles.json` next to `upstream_gate.sh`
(resolved relative to the tool, never CWD). Schema v1, exact keys, no
fallback in fixture mode:

```json
{"schema_version": 1,
 "profiles": {"<owner/repo>": {
   "fork_required": ["<verbatim check_run.name>", "..."],
   "upstream_required": ["<verbatim check_run.name>", "..."],
   "status": "reviewed | pending-live-confirmation",
   "evidence": {"policy_ref": "<branch@sha>",
                "workflows_manifest": [{"file": "...", "blob_sha": "..."}],
                "check_run_names": ["verbatim rendered names"],
                "checked_at": "<date>"}}}}
```

- A repo with no entry, or an entry whose `status` is not `reviewed`,
  is `REFUSED unknown/unreviewed CI profile`, exit 2.
- A malformed registry (bad JSON, wrong `schema_version`, empty or
  non-string required list, bad evidence shape) is
  `REFUSED malformed CI profile`, exit 2.
- A review check smuggled into a required list to double-count a review
  as CI is malformed: `Paperclip Review` may appear only in the preserved
  `TogetherWeOwn/paperclip` entry; a check run named `Greptile Review` is
  only a legitimate `upstream_required` member. Both independent 5/5 review
  evaluators (`eval_our_review`, `eval_greptile`) stay independent: no
  profile edit or manual receipt can satisfy them.
- In live mode a repo with no entry keeps today's hardcoded defaults
  (`ci / verify`, `ci / e2e`, plus the review check); fixture mode never
  falls back. A missing registry file (old tool copies) also keeps today's
  behavior.

### Shipped entries (pinned 2026-10-18)

| Repo | Side | Required | Status |
|---|---|---|---|
| `TogetherWeOwn/paperclip` | fork / upstream | `ci / verify`, `ci / e2e`, `Paperclip Review` / `Greptile Review` | reviewed (defaults retained verbatim) |
| `paperclipai/paperclip` | fork / upstream | same lists as above | reviewed (alias world preserved, no changes) |
| `TogetherWeOwn/CLIProxyAPI` | fork | `build`, `ensure-no-translator-changes`, `close-when-agents-md-changed` | reviewed, `evidence.policy_ref = dev@e9af1ba…` |
| `router-for-me/CLIProxyAPI` | upstream | the three guard names plus `Greptile Review` | `pending-live-confirmation` (Greptile unconfirmed upstream) |

Evidence receipts (all read live 2026-10-18, no writes, no credentials):

- Mirror `TogetherWeOwn/CLIProxyAPI#1`: same-repo PR, head
  `159e43cea3c56aeaf51313651cce425a06256939`, base `dev` @
  `e9af1ba330858c09a3d6aace3669ba087455928c`, open draft; fork parent
  `router-for-me/CLIProxyAPI`. Exact-head check runs, latest per name:
  `build` success (id 111338758857),
  `ensure-no-translator-changes` success (id 111338758763),
  `close-when-agents-md-changed` success (id 111338761085), all app
  `github-actions`; `[code]smith` skipped (app `blacksmith-sh`, optional,
  never required); `Paperclip Review` is the independent review gate.
  `ci / verify` and `ci / e2e` appear nowhere: requiring them is the
  confirmed incompatibility. Workflow blobs at `dev@e9af1ba`:
  `pr-test-build.yml f1f0e28…`, `pr-path-guard.yml 4fe3d93…`,
  `agents-md-guard.yml c9ac0cb…` (full SHAs in the registry).
- True upstream `router-for-me/CLIProxyAPI` (default `main` @
  `911f49cbd49f…`): same six workflow files with identical guard blobs;
  upstream PR #6727 (@ `47211fe8…`) shows the three guard check runs green
  plus `retarget`. No Greptile check run and no Greptile summary comment
  were observed on that PR, so the upstream entry ships
  `pending-live-confirmation` with the guard names as expected-but-
  unconfirmed candidates plus `Greptile Review` retained: until live
  evidence lands, evaluation refuses instead of manufacturing green.

### Exact-head predicate (A.4)

For each required name the latest run by numeric-id ordering wins
(unchanged), then ALL of: run `head_sha` equals the PR head sha (a
missing/malformed sha in `pr.json` or in a required run is REFUSED,
exit 2); `status == completed` (else FAIL `[pending]`); `conclusion ==
success` (`skipped`, `neutral`, empty or unknown conclusions on a
required check FAIL, never pass); a success on a stale head FAILs
`stale [pending]`. Extra non-required runs never fail the gate, with
one exception: the preserved `TogetherWeOwn/paperclip` and
`paperclipai/paperclip` entries keep the legacy any-run rule for extras
(any completed run with a `failure`/`timed_out`/`cancelled`/
`action_required`/`startup_failure` conclusion fails, any unfinished
run pends, required or not) *plus* the exact-head predicate above
(stale success FAILs, missing/malformed head REFUSEs) — a strict
superset of the pre-profile evaluators, which compare no heads.
Completed `skipped`/`neutral` extras never block anywhere.
Unreadable payloads and zero observed runs are REFUSED. Provenance
(run id, head, app) is quoted in the output. The check-runs API always
returns `head_sha`; a payload where no run carries one predates
head-pinning (legacy fixtures/stubs) and skips only the head comparison.

Drift enforcement is executable, not manual: fixture mode compares the
entry manifest against `workflows.json` in the layout dir; live
`check`/`ready` re-resolves the manifest blob SHAs against the policy
branch head. Any mismatch is `REFUSED profile-stale: <file> changed;
refresh profile + fixtures`, exit 2, until profile, fixtures and
evidence are refreshed in one reviewed PR.

## 2. Template-less maintainer memory (contract B)

`memory_init` probes the four file candidates in order
(`<recorded path>`, `.github/pull_request_template.md`,
`pull_request_template.md`, `docs/pull_request_template.md`), then sweeps
the further locations GitHub honors: uppercase `PULL_REQUEST_TEMPLATE.md`
in root and `docs/`, and the multi-template `PULL_REQUEST_TEMPLATE/`
directory in root, `docs/` and `.github/`:

- Absence is proven ONLY when every file candidate is a verifiable
  HTTP 404 and every template directory reads absent (HTTP 404 or empty).
  A real template later in any list is still adopted (fallback preserved);
  any entry inside a template directory refuses as cannot-verify (a
  directory is never proof of absence).
- Any non-file/ambiguous signal (directory, symlink, 403, 5xx, timeout,
  non-JSON) on any candidate is `REFUSED cannot verify PR template
  <cand> (<signal>); memory unchanged`: no write, no weaker fallback, and
  never relabeled as 404. An unresolvable repo also refuses with memory
  untouched.
- On proven absence, memory records the internal minimum, honestly
  labeled (`origin: internal-minimum — NOT upstream maintainer input`):
  7 sections (Thinking Path, Linked Issues or Issue Description, What
  Changed, Verification, Risks, Model Used, Checklist), the 6 verbatim
  checklist boxes from this repo's own template,
  `pending_ok_at_first_push: ["greptile", "ci gates are green"]`, empty
  path/blob, `absent: true`, per-candidate 404 `absence_evidence` for the
  four file candidates plus the extended-sweep outcomes,
  with the resolved `default_branch@commit-sha` revision. Signoff
  discovery is preserved (`dco/cla false` with null evidence when
  nothing is found). `checkboxes: []` is forbidden on this path.

Freshness: `lint --live` re-probes the four file candidates plus the
extended uppercase-name and template-directory sweep when memory says
absent. A newly present template, or new DCO/CLA markers versus
`detected_from`, is `REFUSED template-fresh: upstream gained a
<template|signoff policy>; run memory refresh --repo <upstream>`,
exit 2, until a human runs `memory refresh --repo <upstream>` and
reviews the new maintainer text. A refresh on the absence path preserves
a hand-recorded CLA, exactly like the template-present path. Gate output on absence-memory lines is
tagged `template(internal-minimum)` so a pass never implies maintainer
authorship; empty or placeholder sections still FAIL.

Rollback is `git revert` of the implementation PR plus a suite re-run.
Runtime memory under `$UPSTREAM_MEMORY_DIR` is never deleted by
rollback; a stale absence-memory is neutralized by the freshness
REFUSED, not by deletion.

## 3. Verification, rollback, availability

- `make verify-fast` plus `./test_upstream_gate.sh` plus
  `python3 -m unittest test_upstream_lint test_upstream_reputation`;
  exact-head CI green (`ci-ok`, `pr-lint`, the `ci.yml` upstream
  selectors); independent Paperclip Review 5/5; independent CISO
  comment-gate before landing. Live proof is fixture-driven
  `ready --dry-run` plus one `audit` pass, quoted in the PR body; no
  upstream mutation is ever used as proof.
- Availability: a `paperclip-ops` checkout at the merged commit
  (`git rev-parse HEAD`), with the `check --help` surface smoke and one
  read-only `check` receipt.
