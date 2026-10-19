#!/usr/bin/env bash
# test_tier0_org_template.sh — structural contract for the staged org template
# (case_4784a03d). Hermetic: python3 + PyYAML only, no network, no Docker.
#
# It pins the properties that make the TogetherWeOwn/.github drop safe to
# apply: the ported gate keeps every case_5789b2d5 invariant (report-only
# default, no path-filtered triggers, SHA-pinned actions, portable runners,
# one aggregator, valid Semgrep packs), the ADOPTION.md caller snippet is
# pinned and report-only, the ruleset payloads differ only in enforcement,
# the checklist covers all 10 buckets, and the destination map names every
# staged file.
set -euo pipefail
cd "$(dirname "$0")/../.."
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

BASE=templates/org-github
GATE=$BASE/tier-0/tier-0-quality-gate.yml
ADOPTION=$BASE/tier-0/ADOPTION.md
CHECKLIST=$BASE/pull-request-template/SELF-REVIEW-CHECKLIST.md
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[[ -f $GATE ]] || fail "staged gate missing: $GATE"
[[ -f $ADOPTION ]] || fail "adoption guide missing: $ADOPTION"
[[ -f $CHECKLIST ]] || fail "self-review checklist missing: $CHECKLIST"

python3 - "$GATE" "$ADOPTION" "$CHECKLIST" <<'PY'
import glob, json, re, sys, yaml

gate_path, adoption_path, checklist_path = sys.argv[1:3] + [sys.argv[3]]
gate = yaml.safe_load(open(gate_path))
adoption = open(adoption_path).read()
checklist = open(checklist_path).read()
errors = []
def check(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        errors.append(msg)

# Port freshness: the staged gate names its source commit so the Operator
# re-syncs after PR #824 merges instead of applying a stale copy.
header = "\n".join(open(gate_path).read().splitlines()[:12])
m = re.search(r'paperclip-ops@([0-9a-f]{40})', header)
check(bool(m), "staged gate header pins its source commit (re-sync after #824 merges)")

def triggers(wf):
    on = wf.get('on', wf.get(True, {}))
    return on or {}
wc = triggers(gate).get('workflow_call', {})
check(isinstance(wc, dict) and wc.get('inputs', {}).get('mode', {}).get('default') == 'report-only',
      "workflow_call exists with mode default report-only")

on = triggers(gate)
if isinstance(on, dict):
    for trig, cfg in on.items():
        if isinstance(cfg, dict):
            check('paths' not in cfg and 'paths-ignore' not in cfg,
                  f"gate: trigger '{trig}' has no path filter")

jobs = gate.get('jobs', {})
expected_gates = {'lint-types', 'unused-code', 'generator-drift',
                  'workflow-checks', 'security', 'tests'}
check(expected_gates <= set(jobs), f"all six gates present ({sorted(expected_gates)})")
for j, spec in jobs.items():
    if j in ('detect', 'tier-0-ok'):
        continue
    check(spec.get('continue-on-error') == "${{ inputs.mode == 'report-only' }}",
          f"job {j} continue-on-error tied to report-only mode")

agg = jobs.get('tier-0-ok', {})
check(agg.get('if') == '${{ !cancelled() }}', "tier-0-ok uses if: !cancelled()")
check(set(agg.get('needs', [])) == {'detect'} | expected_gates,
      "tier-0-ok needs detect plus all six gates")
aggs = [j for j, s in jobs.items() if 'tier-0' in j and 'needs' in s and j != 'tier-0-ok']
check(not aggs, "no second aggregator")

sha = re.compile(r'^[^@]+@[0-9a-f]{40}$')
def uses_entries(obj):
    found = []
    if isinstance(obj, dict):
        if 'uses' in obj and isinstance(obj['uses'], str):
            found.append(obj['uses'])
        for v in obj.values():
            found += uses_entries(v)
    elif isinstance(obj, list):
        for v in obj:
            found += uses_entries(v)
    return found
for u in uses_entries(gate):
    if u.startswith('./'):
        continue
    check(bool(sha.match(u.split('#')[0].strip())), f"action pinned by SHA: {u}")
for j, spec in jobs.items():
    if 'runs-on' in spec:
        check(spec['runs-on'] == 'ubuntu-latest', f"job {j} runs on ubuntu-latest (portable)")
    check('timeout-minutes' in spec, f"job {j} has timeout-minutes")

def steps(obj):
    found = []
    if isinstance(obj, dict):
        if isinstance(obj.get('steps'), list):
            found += obj['steps']
        for v in obj.values():
            found += steps(v)
    elif isinstance(obj, list):
        for v in obj:
            found += steps(v)
    return found
for s in steps(gate):
    uses = s.get('uses', '')
    if 'actions/checkout' in uses:
        check(s.get('with', {}).get('persist-credentials') is False,
              f"checkout step drops credentials ({s.get('name', uses)})")
    run = s.get('run', '')
    if isinstance(run, str) and run:
        check('${{' not in run,
              f"run: block uses env indirection, no inline expression ({s.get('name', '?')})")

det_out = (jobs.get('detect', {}).get('outputs', {})) or {}
for o in ('changed', 'has-ts', 'has-php', 'has-rust', 'has-go',
          'has-python', 'has-workflows', 'has-migrations'):
    check(o in det_out, f"detect publishes output {o}")

live = 0
for path in sorted(glob.glob('templates/org-github/tier-0/semgrep-rules/*.yaml')):
    doc = yaml.safe_load(open(path))
    check(isinstance(doc, dict) and isinstance(doc.get('rules'), list),
          f"{path} has a rules list")
    for r in doc['rules']:
        tier = (r.get('metadata') or {}).get('tier0')
        if tier == 'live':
            live += 1
            check(all(k in r for k in ('id', 'languages', 'message')) and
                  ('patterns' in r or 'pattern' in r),
                  f"{path}/{r.get('id')} live rule is complete")
        elif tier == 'template-disabled':
            pat = r.get('pattern', '')
            check('__tier0_template' in pat, f"{path}/{r.get('id')} template matches nothing")
        else:
            check(False, f"{path}/{r.get('id')} has tier0 metadata (live|template-disabled)")
check(live >= 2, f"at least two live rules ({live} found)")

# Caller snippet: pinned SHA (never a branch), report-only, single context.
check('TogetherWeOwn/.github/.github/workflows/tier-0-quality-gate.yml@<pinned-sha>' in adoption,
      "adoption caller uses the org path with a pinned SHA placeholder")
check('mode: report-only' in adoption, "adoption caller starts report-only")
check('tier-0-ok' in adoption and 'needs:' in adoption,
      "adoption wires the single tier-0-ok context into the required aggregator")
check('timeout-minutes' in adoption and 'rejected on `uses:`' in adoption,
      "adoption warns timeout-minutes is rejected on uses: jobs")

# Ruleset payloads: valid JSON, same rules, differ only in enforcement.
base = 'templates/org-github/rulesets/'
stage1 = json.load(open(base + 'tier0-report-only.json'))
stage2 = json.load(open(base + 'tier0-required.json'))
check(stage1.get('enforcement') == 'evaluate', "stage-1 ruleset is evaluate (advisory)")
check(stage2.get('enforcement') == 'active', "stage-2 ruleset is active")
s1 = {k: v for k, v in stage1.items() if k not in ('enforcement', '_comment')}
s2 = {k: v for k, v in stage2.items() if k not in ('enforcement', '_comment')}
check(s1 == s2, "stage-1 and stage-2 payloads differ only in enforcement")
required = [r for r in stage1.get('rules', []) if r.get('type') == 'required_status_checks']
check(any(rc.get('context') == 'ci-ok'
          for r in required
          for rc in r.get('parameters', {}).get('required_status_checks', [])),
      "ruleset requires the ci-ok context")
check(not any('review' in json.dumps(r).lower() for r in stage1.get('rules', [])),
      "ruleset keeps the review check out (case_4a48e6a3 trigger owns it)")

# Regression guards (Paperclip Review 3/5 on PR #829): the caller snippet
# must not ship timeout-minutes on a uses: job, and the runbook must sed
# the QUOTED placeholder so repository_id applies as an integer.
snippet = adoption.split('```yaml', 1)[1].split('```', 1)[0] if '```yaml' in adoption else ''
check('tier-0-quality-gate.yml@<pinned-sha>' in snippet,
      "regression guard targets the caller snippet")
check('timeout-minutes' not in snippet,
      "adoption caller snippet ships no timeout-minutes (rejected on uses: jobs)")
runbook = open('templates/org-github/rulesets/OPERATOR-RUNBOOK.md').read()
check(runbook.count('s/\\"REPO_ID_OF_DOT_GITHUB\\"/') == 2,
      "runbook seds the shell-escaped quoted placeholder so repository_id applies as an integer")
wfs = [w for r in stage1.get('rules', []) if r.get('type') == 'required_workflows'
       for w in r.get('parameters', {}).get('workflows', [])]
check(any(w.get('repository_id') == 'REPO_ID_OF_DOT_GITHUB' for w in wfs),
      "staged repository_id stays a string placeholder until the runbook fills it")

# Checklist: all 10 buckets, one question each.
for n in range(1, 11):
    check(re.search(rf'^- \[ \] {n} ', checklist, re.M) is not None,
          f"checklist covers bucket {n}")

# Destination map: every staged file named in the org README.
readme = open('templates/org-github/README.md').read()
for f in (['tier-0/tier-0-quality-gate.yml', 'tier-0/ADOPTION.md']
          + [f'tier-0/semgrep-rules/{p}' for p in
             ('README.md', 'generic-dangerous-functions.yaml',
              'generic-secrets-in-code.yaml', 'template-authz-helper.yaml',
              'template-check-then-write.yaml', 'security-ssrf-redirect.yaml',
              'security-secrets-extended.yaml')]
          + ['tier-0/PORT-ADDITIONS.md', 'codeql/codeql-config.yml',
             'pull-request-template/SELF-REVIEW-CHECKLIST.md',
             'rulesets/tier0-report-only.json', 'rulesets/tier0-required.json',
             'rulesets/OPERATOR-RUNBOOK.md']):
    check(f in readme, f"destination map names {f}")

sys.exit(1 if errors else 0)
PY
pass "tier-0 org-template structural contract holds"
