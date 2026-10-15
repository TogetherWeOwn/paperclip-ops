# Operator runbook: org rulesets for the Tier-0 gate (case_4784a03d)

Ruleset writes are 403 for the GitHub App — a human org admin runs the
commands below (or the Operator pastes them into an admin session). Nothing
here runs from CI.

## 0. Read the live state first (never blind-write)

```bash
gh api orgs/TogetherWeOwn/rulesets --jq '.[] | "\(.id) \(.name) \(.enforcement)"'
```

If a `tier-0-quality-gate` ruleset already exists, update it (`PATCH
orgs/TogetherWeOwn/rulesets/{id}`) instead of creating a duplicate.

## 1. Fill the two placeholders

```bash
REPO_ID=$(gh api repos/TogetherWeOwn/.github --jq .id)
PINNED_SHA=$(gh api repos/TogetherWeOwn/.github/commits/main --jq .sha)
sed -e "s/\"REPO_ID_OF_DOT_GITHUB\"/$REPO_ID/" -e "s/PINNED_SHA/$PINNED_SHA/" \
  tier0-report-only.json > /tmp/tier0-stage1.json
python3 -c "import json; d=json.load(open('/tmp/tier0-stage1.json')); assert isinstance([w for r in d['rules'] if r['type']=='required_workflows' for w in r['parameters']['workflows']][0]['repository_id'], int), 'repository_id must be an integer'" && echo payload-valid
```

## 2. Stage 1 — evaluate (advisory) for 24h

```bash
gh api orgs/TogetherWeOwn/rulesets -X POST --input /tmp/tier0-stage1.json \
  --jq '{id: .id, name: .name, enforcement: .enforcement}'
```

Confirm: open any active repo PR — `ci-ok` still gates the merge, the
Tier-0 required-workflow run appears but cannot block (evaluate mode).

## 3. Stage 2 — active, no earlier than 24h later

Only when no gate's false-positive rate over the last 20 PRs exceeds 5%:

```bash
RULESET_ID=<id-from-step-2>
sed -e "s/\"REPO_ID_OF_DOT_GITHUB\"/$REPO_ID/" -e "s/PINNED_SHA/$PINNED_SHA/" \
  tier0-required.json > /tmp/tier0-stage2.json
gh api orgs/TogetherWeOwn/rulesets/$RULESET_ID -X PATCH --input /tmp/tier0-stage2.json \
  --jq '{id: .id, name: .name, enforcement: .enforcement}'
```

## Rollback (either stage, one command)

```bash
gh api orgs/TogetherWeOwn/rulesets/$RULESET_ID -X PATCH \
  -f enforcement=disabled --jq '{name: .name, enforcement: .enforcement}'
```

Disabling restores the pre-template gate (repo-level `ci-ok`/`pr-lint`
branch protection is untouched by these payloads). Delete only when a
replacement ruleset is already active.

## Why Paperclip Review is not in these payloads

Review is requested once per ready head after `ci-ok` green by the trigger
workflow (case_4a48e6a3). A required review context in branch protection would
block merges of low-risk PRs where review is intentionally skipped, and
would re-request review on every push — the exact cost the epic removes.
