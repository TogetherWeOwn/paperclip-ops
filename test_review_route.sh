#!/usr/bin/env bash
# case_6bf2cca9. Hermetic suite for the Tier-2 review router:
#   1. security-boundary paths route expensive (authz, secrets, migrations, workflows)
#   2. size gate: over threshold routes expensive, at/under routes standard
#   3. Tier-0 / Tier-1 risk flags route expensive
#   4. audit sample is deterministic per PR and ~10% across PRs
#   5. --numstat-from parses totals, skips binary rows
#   6. weekly report metrics on a fixture ledger
#   7. bad option values exit 2; JSON/ledger rows survive hostile file names
#   8. weekly report: (repo, pr) join, one count per PR, 90-day promotion window
#   9. review-round fixes: missing/empty patterns file exits 2, binary numstat
#      rows keep their path, default patterns cover infra/CI paths and
#      credential-grant code, CI runs this suite
# No network, no credential, no model call.
set -u
cd "$(dirname "$0")"
FAIL=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; FAIL=1; }
T=$(mktemp -d "${TMPDIR:-/tmp}/review-route.XXXXXX"); trap 'rm -rf "$T"' EXIT
R=./review_route.sh

route_of() { $R "$@" | awk -F= '/^ROUTE=/{print $2}'; }

# --- 1. security boundary -------------------------------------------------------
# Assert the reason with auditing disabled: an audit-selected key used to make
# ROUTE-only checks pass even when the security pattern was deleted.
check_sec_reason() { # path -> expensive via security-boundary, never audit
  if $R --repo o/r --pr 12 --add 5 --del 3 --audit-pct 0 --file "$1" --format json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['route']=='expensive' and 'security-boundary' in d['reasons'] and d['audit_sample']=='no', d"; then
    ok "security-boundary reason: $1"
  else
    bad "security-boundary reason: $1"
  fi
}
check_sec_reason "src/authz/check.ts"
check_sec_reason "api/permissions/route.ts"
check_sec_reason "config/credentials.json"
check_sec_reason "deploy/migrate_production.sql"
check_sec_reason "db/migrations/0042_add_index.sql"
check_sec_reason ".github/workflows/ci.yml"
check_sec_reason "Dockerfile"
check_sec_reason "infra/main.tf"
check_sec_reason "GH_APP_PRIVATE_KEY.pem"
# Each router-only probe matches just its own pattern, not another security
# stem. permission, migration and provisioner are subsumed by permis, migrat
# and provision; the generated trigger probes in section 10 pin those stems.
for path in certs/server.pem certs/server.key src/private_key_xq7.py \
    infra/x.terraform src/workflow_xq7.py src/ruleset_xq7.py \
    src/protected_xq7.py src/acl_xq7.py src/security_xq7.py src/passwd_xq7.py \
    src/denylist_xq7.py src/capability_xq7.py src/disclosure_xq7.py \
    src/binding_xq7.py app/.env unit/x.service unit/x.timer; do
  check_sec_reason "$path"
done

# --- 2. size gate ----------------------------------------------------------------
[[ "$(route_of --repo o/r --pr 12 --add 900 --del 99 --file src/plain.ts)" == standard ]] \
  && ok "999 lines routes standard" || bad "999 lines routes standard"
[[ "$(route_of --repo o/r --pr 13 --add 900 --del 101 --file src/plain.ts)" == expensive ]] \
  && ok "1001 lines routes expensive" || bad "1001 lines routes expensive"
[[ "$(route_of --repo o/r --pr 14 --add 10 --del 5 --file src/plain.ts --threshold 20)" == standard ]] \
  && ok "custom threshold honored (standard)" || bad "custom threshold honored (standard)"
[[ "$(route_of --repo o/r --pr 15 --add 10 --del 11 --file src/plain.ts --threshold 20)" == expensive ]] \
  && ok "custom threshold honored (expensive)" || bad "custom threshold honored (expensive)"

# --- 3. risk flags ---------------------------------------------------------------
[[ "$(route_of --repo o/r --pr 16 --add 5 --del 2 --file src/plain.ts --tier0-risk 1)" == expensive ]] \
  && ok "tier0-risk routes expensive" || bad "tier0-risk routes expensive"
[[ "$(route_of --repo o/r --pr 17 --add 5 --del 2 --file src/plain.ts --tier1-risk 1)" == expensive ]] \
  && ok "tier1-risk routes expensive" || bad "tier1-risk routes expensive"

# --- 4. audit sample: deterministic and ~10% -------------------------------------
a1=$(route_of --repo o/r --pr 42 --add 5 --del 2 --file src/plain.ts)
a2=$(route_of --repo o/r --pr 42 --add 5 --del 2 --file src/plain.ts)
[[ "$a1" == "$a2" ]] && ok "audit sample stable per PR" || bad "audit sample stable per PR"
hits=0
for n in $(seq 1 200); do
  [[ "$(route_of --repo o/r --pr "$n" --add 5 --del 2 --file src/plain.ts)" == expensive ]] && hits=$((hits+1))
done
# Only audit-sampled PRs can be expensive here (tiny clean diff, no flags).
if [[ "$hits" -ge 10 && "$hits" -le 32 ]]; then ok "audit sample ~10% across 200 PRs ($hits)";
else bad "audit sample ~10% across 200 PRs ($hits)"; fi

# --- 5. numstat parsing -----------------------------------------------------------
printf '10\t4\tsrc/a.ts\n-\t-\tbin/blob\n3\t1\t.github/workflows/x.yml\n' >"$T/ns.txt"
out=$($R --repo o/r --pr 18 --numstat-from "$T/ns.txt")
echo "$out" | grep -q '^TOTAL=18$' && ok "numstat totals skip binary rows" || bad "numstat totals skip binary rows"
echo "$out" | grep -q '^ROUTE=expensive$' && ok "numstat workflow path routes expensive" || bad "numstat workflow path routes expensive"

# --- json + ledger ----------------------------------------------------------------
$R --repo o/r --pr 19 --add 5 --del 2 --file src/plain.ts --format json --ledger "$T/led.json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['route'] in ('expensive','standard'), d" \
  && ok "json output parses" || bad "json output parses"
[[ -s "$T/led.json" ]] && ok "ledger row appended" || bad "ledger row appended"

# --- bad input exits 2 --------------------------------------------------------------
$R --repo o/r --add 1 --del 1 >/dev/null 2>&1 && bad "missing --pr exits 2" || \
  [[ $? -eq 2 ]] && ok "missing --pr exits 2" || bad "missing --pr exits 2"

# --- 6. buckets taxonomy + weekly report -------------------------------------------
python3 - review-finding-buckets.json <<'PY' || bad "buckets taxonomy valid"
import json, sys
d = json.load(open(sys.argv[1]))
ids = {b["id"] for b in d["buckets"]}
assert {"tier0-semgrep","tier1-docs-diff","security-boundary","size-complexity"} <= ids, ids
assert "3" in d["promotion_rule"], d["promotion_rule"]
PY
[[ $? -eq 0 ]] && ok "buckets taxonomy valid"

cat >"$T/ledger.jsonl" <<'EOF'
{"ts":"2026-10-01T00:00:00Z","repo":"o/r","pr":1,"add":5,"del":2,"total":7,"tier0_risk":0,"tier1_risk":0,"security_hit":"","audit_sample":"no","route":"standard","reasons":""}
{"ts":"2026-10-01T00:00:00Z","repo":"o/r","pr":2,"add":5,"del":2,"total":7,"tier0_risk":0,"tier1_risk":0,"security_hit":"","audit_sample":"yes","route":"expensive","reasons":"audit-sample"}
{"ts":"2026-10-01T00:00:00Z","repo":"o/r","pr":3,"add":900,"del":200,"total":1100,"tier0_risk":0,"tier1_risk":0,"security_hit":"","audit_sample":"no","route":"expensive","reasons":"size>1000"}
{"ts":"2026-10-01T00:00:00Z","repo":"o/r","pr":4,"add":5,"del":2,"total":7,"tier0_risk":0,"tier1_risk":0,"security_hit":"","audit_sample":"no","route":"standard","reasons":""}
EOF
cat >"$T/findings.jsonl" <<'EOF'
{"repo":"o/r","pr":2,"bucket":"tier1-docs-diff","audit_only":true}
{"repo":"o/r","pr":3,"bucket":"tier0-semgrep","audit_only":false}
{"repo":"o/r","pr":3,"bucket":"tier0-semgrep","audit_only":false}
{"repo":"o/r","pr":3,"bucket":"tier0-semgrep","audit_only":false}
EOF
rep=$(python3 review_routing_weekly.py --ledger "$T/ledger.jsonl" --findings "$T/findings.jsonl")
echo "$rep" | grep -q 'expensive_review_share: 0.500' && ok "weekly: expensive share 2/4" || bad "weekly: expensive share 2/4"
echo "$rep" | grep -q 'findings_per_expensive_review: 2.00' && ok "weekly: findings per review" || bad "weekly: findings per review"
echo "$rep" | grep -q 'audit_sample_defects: 1' && ok "weekly: audit-sample defects" || bad "weekly: audit-sample defects"
echo "$rep" | grep -q 'tier0-semgrep: 3' && echo "$rep" | grep -q 'promotion_candidates' \
  && ok "weekly: promotion candidate surfaced" || bad "weekly: promotion candidate surfaced"

# --- 7. bad option values exit 2 (header promise); hostile names stay valid JSON ----
rc_of() { "$@" >/dev/null 2>&1; echo $?; }
base=(--repo o/r --pr 7 --add 1 --del 1)
[[ "$(rc_of $R "${base[@]}" --threshold abc)" == 2 ]] && ok "--threshold abc exits 2" || bad "--threshold abc exits 2"
[[ "$(rc_of $R "${base[@]}" --audit-pct abc)" == 2 ]] && ok "--audit-pct abc exits 2" || bad "--audit-pct abc exits 2"
[[ "$(rc_of $R "${base[@]}" --audit-pct 101)" == 2 ]] && ok "--audit-pct 101 exits 2" || bad "--audit-pct 101 exits 2"
[[ "$(rc_of $R "${base[@]}" --threshold)" == 2 ]] && ok "option without value exits 2" || bad "option without value exits 2"
[[ "$(route_of "${base[@]}" --threshold 09 --file src/plain.ts)" =~ ^(standard|expensive)$ ]] \
  && ok "--threshold 09 is decimal, not octal" || bad "--threshold 09 is decimal, not octal"
$R --repo o/r --pr 007 --add 1 --del 1 --file src/plain.ts --format json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['pr']==7, d" \
  && ok "leading-zero --pr stays valid JSON" || bad "leading-zero --pr stays valid JSON"
cat >"$T/hostile_check.py" <<'PY'
import json, sys
out, ledger = (open(p).read().splitlines() for p in sys.argv[1:3])
assert len(out) == 1 and len(ledger) == 1, (out, ledger)
a, b = json.loads(out[0]), json.loads(ledger[0])
assert a["repo"] == b["repo"] == 'o/"r', (a, b)
assert "auth" in b["security_hit"], b
PY
for name in 'src/auth\x.ts' 'src/auth"x.ts' $'src/auth\tx.ts' $'src/auth\001x.ts'; do
  : >"$T/hostile.jsonl"
  $R --repo 'o/"r' --pr 20 --add 1 --del 1 --file "$name" --format json --ledger "$T/hostile.jsonl" >"$T/hostile.out"
  python3 "$T/hostile_check.py" "$T/hostile.out" "$T/hostile.jsonl" \
    && ok "valid JSON for file name: $(printf '%q' "$name")" || bad "valid JSON for file name: $(printf '%q' "$name")"
done

# --- 8. weekly report: repo-scoped join, one count per PR, 90-day window ------------
NOW=2026-10-09T00:00:00Z
weekly() { python3 review_routing_weekly.py --now "$NOW" "$@"; }
cat >"$T/l2.jsonl" <<'EOF'
{"ts":"2026-10-08T00:00:00Z","repo":"a/x","pr":5,"route":"expensive","reasons":"size>1000"}
{"ts":"2026-10-08T00:00:00Z","repo":"b/y","pr":5,"route":"standard","reasons":""}
EOF
cat >"$T/f2.jsonl" <<'EOF'
{"repo":"b/y","pr":5,"bucket":"tier0-lint","audit_only":true}
EOF
rep=$(weekly --ledger "$T/l2.jsonl" --findings "$T/f2.jsonl")
echo "$rep" | grep -q 'findings_per_expensive_review: 0.00' \
  && ok "finding on standard b/y#5 does not join expensive a/x#5" || bad "finding on standard b/y#5 does not join expensive a/x#5"
echo "$rep" | grep -q 'audit_sample_defects: 0' \
  && ok "audit finding on a standard-routed PR is not an audit catch" || bad "audit finding on a standard-routed PR is not an audit catch"

cat >"$T/l3.jsonl" <<'EOF'
{"ts":"2026-10-07T00:00:00Z","repo":"a/x","pr":5,"route":"expensive","reasons":"audit-sample"}
{"ts":"2026-10-08T00:00:00Z","repo":"a/x","pr":5,"route":"expensive","reasons":"audit-sample"}
{"ts":"2026-10-07T00:00:00Z","repo":"a/x","pr":6,"route":"expensive","reasons":"size>1000"}
{"ts":"2026-10-08T00:00:00Z","repo":"a/x","pr":6,"route":"standard","reasons":""}
{"ts":"2026-10-08T00:00:00Z","repo":"a/x","pr":7,"route":"standard","reasons":""}
EOF
rep=$(weekly --ledger "$T/l3.jsonl")
echo "$rep" | grep -q '^routed_prs: 3$' && echo "$rep" | grep -q '^expensive_reviews: 2$' \
  && ok "a PR routed twice counts once (3 PRs, 2 expensive)" || bad "a PR routed twice counts once (3 PRs, 2 expensive)"
echo "$rep" | grep -q 'expensive_review_share: 0.667' \
  && ok "share uses distinct PRs; expensive-then-standard stays expensive" || bad "share uses distinct PRs; expensive-then-standard stays expensive"

printf '%s\n' '{"ts":"2026-10-08T00:00:00Z","repo":"a/x","pr":1,"route":"expensive","reasons":"size>1000"}' >"$T/l4.jsonl"
mkfind() { # ts... -> one tier0-lint finding per ts ("-" = undated)
  : >"$T/f4.jsonl"
  for t in "$@"; do
    if [[ "$t" == "-" ]]; then echo '{"repo":"a/x","pr":1,"bucket":"tier0-lint"}' >>"$T/f4.jsonl"
    else printf '{"repo":"a/x","pr":1,"bucket":"tier0-lint","ts":"%s"}\n' "$t" >>"$T/f4.jsonl"; fi
  done
}
cand() { weekly --ledger "$T/l4.jsonl" --findings "$T/f4.jsonl" "$@"; }
mkfind 2026-10-01T00:00:00Z 2026-10-02T00:00:00Z 2026-10-03T00:00:00Z
cand | grep -q '  tier0-lint: 3' && ok "3 findings inside 90 days promote" || bad "3 findings inside 90 days promote"
mkfind 2026-06-01T00:00:00Z 2026-06-02T00:00:00Z 2026-06-03T00:00:00Z
cand | grep -q '^promotion_candidates: none' && ok "3 findings older than 90 days do not promote" || bad "3 findings older than 90 days do not promote"
mkfind 2026-06-01T00:00:00Z 2026-06-02T00:00:00Z 2026-10-03T00:00:00Z
cand | grep -q '^promotion_candidates: none' && ok "2 old + 1 recent finding do not promote" || bad "2 old + 1 recent finding do not promote"
mkfind - - -
cand | grep -q '  tier0-lint: 3' && ok "undated findings fall back to the ledger row date" || bad "undated findings fall back to the ledger row date"
mkfind 2026-06-01T00:00:00Z 2026-06-02T00:00:00Z 2026-06-03T00:00:00Z
cand --promotion-days 200 | grep -q '  tier0-lint: 3' && ok "--promotion-days widens the window" || bad "--promotion-days widens the window"

# --- 9. review-round fixes (Paperclip Review 3/5 on 41e7a46e) --------------------
# 9a. A missing patterns file must exit 2 from the main shell. It used to
# exit only the process substitution, so the router carried on with no
# patterns and routed an auth change `standard`.
[[ "$(rc_of $R "${base[@]}" --security-patterns "$T/does-not-exist.txt")" == 2 ]] \
  && ok "missing patterns file exits 2" || bad "missing patterns file exits 2"
# An empty (or comments-only) patterns file matches nothing, which would fail
# every diff open: refuse it too.
: >"$T/empty-patterns.txt"
printf '# just a comment\n\n' >"$T/comments-only-patterns.txt"
[[ "$(rc_of $R "${base[@]}" --security-patterns "$T/empty-patterns.txt")" == 2 ]] \
  && ok "empty patterns file exits 2" || bad "empty patterns file exits 2"
[[ "$(rc_of $R "${base[@]}" --security-patterns "$T/comments-only-patterns.txt")" == 2 ]] \
  && ok "comments-only patterns file exits 2" || bad "comments-only patterns file exits 2"
# 9b. Binary numstat rows keep their path (only line counting skips them), so
# a binary secret still routes expensive via the security boundary.
printf '10\t4\tsrc/a.ts\n-\t-\tsecrets/prod-secrets.enc\n' >"$T/ns-bin.txt"
out=$($R --repo o/r --pr 21 --numstat-from "$T/ns-bin.txt")
echo "$out" | grep -q '^TOTAL=14$' && ok "binary rows skip line counts" || bad "binary rows skip line counts"
echo "$out" | grep -q '^ROUTE=expensive$' && ok "binary secret path routes expensive" || bad "binary secret path routes expensive"
echo "$out" | grep -q 'security-boundary' && ok "binary secret matched by security patterns" || bad "binary secret matched by security patterns"
# 9c. Default patterns cover the infra/CI paths from the review. Assert on the
# `security-boundary` reason (not just ROUTE) so the audit sample cannot make
# these pass vacuously.
check_sec_reason ".github/scripts/ci_changes.py"
check_sec_reason ".github/dependabot.yml"
check_sec_reason "github-runner/actions-runner.env"
check_sec_reason "systemd/runner-provision.service"
check_sec_reason "ops/provision.timer"
check_sec_reason "wrangler.toml"
# 9d. CI must actually run this suite: verify-fast.sh --parse-only (all CI
# calls on this path) never reaches it, so it needs its own ci.yml step.
grep -q 'test_review_route.sh' .github/workflows/ci.yml \
  && ok "ci.yml runs the review-route suite" || bad "ci.yml runs the review-route suite"
# 9e. Default patterns cover the credential-grant code from the review: a
# change to grant logic must route expensive via `security-boundary`, not
# ride the cheap path 90% of the time once the platform calls the router.
check_sec_reason "gh_access.sh"
check_sec_reason "tool_grant_expectations.json"
check_sec_reason "docs/external-disclosure-grants.md"

# --- 10. single source of truth: review_trigger.py owns the live gate ---------
# 10a. Router defaults are a superset of the trigger's RISKY_PATH_PATTERNS, so
# a path the live gate calls risky routes expensive here via
# `security-boundary` (assert on the reason, not just ROUTE, so the audit
# sample cannot make these pass vacuously).
# The probes are GENERATED from the live list: one probe per
# RISKY_PATH_PATTERNS entry, read from review_trigger.py at test time. An
# entry with no branch below fails the suite, so a new trigger entry cannot
# pass silently. Each probe matches exactly one default router pattern (so
# deleting that pattern fails this suite), except the workflows probe, which
# unavoidably also matches the router's `workflow` stem — `.github/`
# deletion is pinned by the `.github/scripts/ci_changes.py` check above.
read_trigger_entries() {
  python3 - "$1" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
entries = []
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'RISKY_PATH_PATTERNS' for t in node.targets):
        entries.extend(ast.literal_eval(e) for e in node.value.elts)
if any(not isinstance(e, str) or not e or '\n' in e or '\r' in e for e in entries):
    raise ValueError('trigger entries must be nonempty single-line literal strings')
# Emit only after the whole list has been validated: no partial coverage.
print('\n'.join(entries))
PY
}
# Command substitution preserves the Python status; process substitution did
# not, so a computed entry after valid entries used to leave the suite green.
TRIGGER_ENTRIES=()
if entries=$(read_trigger_entries .github/scripts/review_trigger.py); then
  [[ -n "$entries" ]] && mapfile -t TRIGGER_ENTRIES <<<"$entries"
else
  bad "drift pin reader failed"
fi
# Negative controls: a computed element after valid entries and invalid syntax
# must return nonzero with no partial entries on stdout.
printf '%s\n' 'RISKY_PATH_PATTERNS = (r"auth", r"kms" + r"key", r"newstem",)' >"$T/computed-trigger.py"
printf '%s\n' 'RISKY_PATH_PATTERNS = (' >"$T/invalid-trigger.py"
for fixture in computed invalid; do
  if read_trigger_entries "$T/$fixture-trigger.py" >"$T/entries.out" 2>"$T/entries.err"; then
    bad "drift reader refuses $fixture input"
  elif [[ -s "$T/entries.out" ]]; then
    bad "drift reader emitted partial entries for $fixture input"
  else
    ok "drift reader refuses $fixture input without partial entries"
  fi
done
[[ ${#TRIGGER_ENTRIES[@]} -gt 0 ]] \
  && ok "drift pin read ${#TRIGGER_ENTRIES[@]} trigger entries" || bad "drift pin read zero trigger entries"
for entry in "${TRIGGER_ENTRIES[@]}"; do
  case "$entry" in # quoted = literal match, so regex entries map exactly
    'auth')                 probe='src/auth_xq7.py' ;;
    'permis')               probe='src/permis_xq7.py' ;;
    'secret')               probe='src/secret_xq7.py' ;;
    'credential')           probe='src/credential_xq7.py' ;;
    'token')                probe='src/token_xq7.py' ;;
    'migrat')               probe='src/migrat_xq7.py' ;;
    'sudoers')              probe='etc/sudoers_xq7' ;;
    'rbac')                 probe='src/rbac_xq7.py' ;;
    'allowlist')            probe='src/allowlist_xq7.py' ;;
    '\.github/workflows/') probe='.github/workflows/drift_xq7.yml' ;;
    'protection-rule/')     probe='protection-rule/drift_xq7.json' ;;
    'host-ops/')            probe='host-ops/drift_xq7.sh' ;;
    'systemd')              probe='src/systemd_xq7.py' ;;
    'provision')            probe='src/provision_xq7.py' ;;
    'broker')               probe='src/broker_xq7.py' ;;
    'deploy')               probe='src/deploy_xq7.py' ;;
    'runner')               probe='src/runner_xq7.py' ;;
    'garm')                 probe='src/garm_xq7.py' ;;
    'coolify')              probe='src/coolify_xq7.py' ;;
    'backup')               probe='src/backup_xq7.py' ;;
    'quota')                probe='src/quota_xq7.py' ;;
    'billing')              probe='src/billing_xq7.py' ;;
    'policy')               probe='src/policy_xq7.py' ;;
    *) bad "no drift probe for trigger entry: $entry"; continue ;;
  esac
  check_sec_reason "$probe"
done
# 10b. The docs name the live gate authoritative and pin the intended
# divergences (size N, audit key), so the two routers cannot silently drift.
grep -q 'authoritative: review_trigger.py' docs/review-routing.md \
  && ok "docs name review_trigger.py authoritative" || bad "docs name review_trigger.py authoritative"
grep -q 'LARGE_DIFF_LINES' docs/review-routing.md \
  && ok "docs pin the live size-gate value" || bad "docs pin the live size-gate value"
grep -q 'sha256(head)' docs/review-routing.md \
  && ok "docs pin the live audit-sample key" || bad "docs pin the live audit-sample key"
grep -q 'Standard heads' docs/review-routing.md \
  && ok "docs pin the standard-heads divergence" || bad "docs pin the standard-heads divergence"
! grep -q 'Platform handoff' docs/review-routing.md \
  && ok "docs carry no stale section reference" || bad "docs carry no stale section reference"

if [[ "$FAIL" -ne 0 ]]; then echo "review-route: FAILED"; exit 1; fi
echo "review-route: PASS"
