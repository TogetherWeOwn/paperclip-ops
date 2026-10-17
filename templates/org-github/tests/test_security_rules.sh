#!/usr/bin/env bash
# test_security_rules.sh — deterministic security-checks contract (case_f3ff4554).
#
# Blocker-ledger 'security' class (18% of Claude blockers): semgrep/CodeQL
# custom rules, secret-scan gaps, SSRF/redirect patterns. This suite pins the
# org-level (durable-by-default) security pack staged under
# templates/org-github/ plus the local secret-scan rollout, so a later slice
# cannot silently drop a rule file, a rule id, or a scan pattern.
#
# Hermetic: bash + python3 + PyYAML + git only. No network (the tier-0
# Gate 5 folder scan is the engine in prod; here we pin the pack structure,
# the rule ids, the FP guards, and the scan behaviour).
# The behavioural section at the end additionally EXECUTES the staged rules
# with the pinned Semgrep 1.138.0: the CI suites step installs it from a
# hash-locked wheel closure in a venv and sets SEMGREP to that binary (without
# shadowing system python3/PyYAML). A missing binary fails hard under CI.
# A local run without SEMGREP or semgrep on PATH SKIPs
# the behavioural section only, so a dead rule shape fails in CI instead of
# nowhere.
set -euo pipefail
cd "$(dirname "$0")/../.."
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ROOT="$PWD"

BASE=templates/org-github
RULES=$BASE/tier-0/semgrep-rules
SSRF=$RULES/security-ssrf-redirect.yaml
SECRETS=$RULES/security-secrets-extended.yaml
MANIFEST=$BASE/tier-0/PORT-ADDITIONS.md
CODEQL=$BASE/codeql/codeql-config.yml
SCAN=verification/secret-scan.sh

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[[ -f $SSRF ]] || fail "staged SSRF/redirect rules missing: $SSRF"
pass "staged SSRF/redirect rules present"
[[ -f $SECRETS ]] || fail "staged extended-secrets rules missing: $SECRETS"
pass "staged extended-secrets rules present"
[[ -f $MANIFEST ]] || fail "port-additions manifest missing: $MANIFEST"
pass "port-additions manifest present"
[[ -f $CODEQL ]] || fail "staged CodeQL config missing: $CODEQL"
pass "staged CodeQL config present"

python3 - "$SSRF" "$SECRETS" <<'PY'
import sys, yaml

errors = []
def check(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        errors.append(msg)

ssrf = yaml.safe_load(open(sys.argv[1]))
secrets = yaml.safe_load(open(sys.argv[2]))

for name, doc in (("ssrf", ssrf), ("secrets", secrets)):
    check(isinstance(doc, dict) and isinstance(doc.get("rules"), list),
          f"{name} pack has a rules list")

ssrf_ids = {r.get("id") for r in ssrf.get("rules", [])}
for rid in ("tier0-ssrf-dynamic-fetch-js", "tier0-ssrf-dynamic-url-py",
            "tier0-open-redirect-js", "tier0-open-redirect-py"):
    check(rid in ssrf_ids, f"ssrf pack pins rule {rid}")

secret_ids = {r.get("id") for r in secrets.get("rules", [])}
for rid in ("tier0-secret-discord-webhook", "tier0-secret-slack-token",
            "tier0-secret-aws-key", "tier0-secret-openai-key",
            "tier0-secret-stripe-live-key", "tier0-secret-github-token"):
    check(rid in secret_ids, f"secrets pack pins rule {rid}")

for path, doc in (("ssrf", ssrf), ("secrets", secrets)):
    for r in doc.get("rules", []):
        rid = r.get("id", "?")
        check((r.get("metadata") or {}).get("tier0") == "live",
              f"{path}/{rid} is tier0: live")
        check(all(k in r for k in ("id", "languages", "message")) and
              ("patterns" in r or "pattern" in r or "pattern-regex" in r),
              f"{path}/{rid} rule is complete")
        # FP guard: every live rule stays out of test/fixture/vendor paths.
        blob = str(r.get("paths", ""))
        check("test" in blob and "vendor" in blob,
              f"{path}/{rid} excludes test/fixture/vendor paths")
        check(r.get("severity") in ("ERROR", "WARNING"),
              f"{path}/{rid} carries a severity")

if errors:
    raise SystemExit(1)
PY

# The rules themselves must not contain real-shaped secrets outside their
# own pattern-regex definitions: examples stay truncated or EXAMPLE-marked
# so the repo's own secret scan stays clean. Covers the current key
# families (legacy sk-, sk-proj-/sk-svcacct-/sk-admin-) so a pasted example
# of any advertised shape is caught here, not in a gate.
if grep -nE 'discord(app)?\.com/api/webhooks/[0-9]{10,}/[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|sk_live_[A-Za-z0-9]{10,}|rk_live_[A-Za-z0-9]{10,}|gh[pousr]_[A-Za-z0-9]{16,}|sk-(proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}|sk-[A-Za-z0-9]{20,}' "$SSRF" "$SECRETS" | grep -v 'pattern-regex:'; then
    fail "staged rules embed a real-shaped secret outside pattern-regex"
fi
pass "staged rules carry no real-shaped secret outside pattern-regex"

# Manifest lists exactly the post-pin additions (the drift gate exempts these).
for f in security-ssrf-redirect.yaml security-secrets-extended.yaml; do
    grep -qF "semgrep-rules/$f" "$MANIFEST" || fail "manifest omits $f"
done
pass "manifest lists both new rule files"

# CodeQL: shared security-extended config, staged for the org rollout.
grep -q "security-extended" "$CODEQL" || fail "codeql config lacks the security-extended suite"
pass "codeql config selects security-extended"

# Local rollout: the always-run scan covers the two zero-FP shapes.
grep -q "api/webhooks" "$SCAN" || fail "secret-scan.sh lacks the discord-webhook shape"
grep -q "sk_live_" "$SCAN" || fail "secret-scan.sh lacks the stripe-live shape"
pass "secret-scan.sh covers discord-webhook + stripe-live"

# Behavioural: each planted real-shape canary trips the scan on its own and
# the failure names its own shape, so either shape going dead is caught
# instead of masked by the other. The discordapp.com and rk_live_ variants
# trip too, while the short test-fixture canary (used across suites) stays
# clean.
# NOTE: real-shape canaries are assembled from split non-secret fragments at
# runtime so this suite file itself never contains a scannable secret shape
# (the repo's own secret scan stays strict; no test-path exemptions).
SCRATCH="${PAPERCLIP_RUN_SCRATCH_DIR:-${RUNNER_TEMP:-/tmp}}"
WORK="$(mktemp -d "$SCRATCH/secrules.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/repo" && cd "$WORK/repo"
git init -q .
printf '%s\n' '*.env' '*.pem' '*.key' '*.jsonl' '__pycache__/' '*.pyc' > .gitignore
git add .gitignore
DISC_A="discord"
DISC_B=".com/api/"
DISC_C="webhooks/"
DISC_D="discordapp"
CHAN_A="123456"
CHAN_B="789012345678"
TOK_A="abcdefghijklmnop"
TOK_B="qrstUVWXYZ012345"
STRIPE_A="sk_"
STRIPE_R="rk_"
STRIPE_B="live_"
STRIPE_C="abcDEF1234567890"
trip_canary() { # $1: file, $2: expected ::error:: substring, $3: file content
    local file="$1" expect="$2" content="$3" out
    printf '%s\n' "$content" > "$file"
    git add -A
    if out="$(cd "$WORK/repo" && bash "$ROOT/$SCAN" 2>&1)"; then
        fail "secret-scan passes with $file committed (expected trip: $expect)"
    fi
    grep -qF "$expect" <<<"$out" \
        || fail "secret-scan tripped on $file but did not name it (expected '$expect')"
    pass "secret-scan trips on $file ($expect)"
    rm "$file" && git add -A
}
trip_canary real-discord.txt "Discord-webhook" "url = \"https://$DISC_A$DISC_B$DISC_C$CHAN_A$CHAN_B/$TOK_A$TOK_B\""
trip_canary real-discordapp.txt "Discord-webhook" "url = \"https://$DISC_D$DISC_B$DISC_C$CHAN_A$CHAN_B/$TOK_A$TOK_B\""
trip_canary real-stripe-sk.txt "Stripe-live-key" "key = \"$STRIPE_A$STRIPE_B$STRIPE_C\""
trip_canary real-stripe-rk.txt "Stripe-live-key" "key = \"$STRIPE_R$STRIPE_B$STRIPE_C\""
printf 'x="https://discord.com/api/webhooks/123/abc"\n' > short-canary.txt
git add -A
if ! (cd "$WORK/repo" && bash "$ROOT/$SCAN" >/dev/null 2>&1); then
    fail "secret-scan trips on the short test-fixture canary"
fi
pass "secret-scan stays clean on the short test-fixture canary"

# Behavioural: the staged semgrep rules are EXECUTED, not just listed.
# Fixtures are generated from split fragments at runtime — committing
# real-shaped positives would trip the repo's own scans — then scanned with
# the pinned semgrep when it is already on PATH. Without semgrep this
# reports SKIP (the Tier-0 Gate 5 folder scan stays the engine there).
SEMGREP="${SEMGREP:-semgrep}"
if ! command -v "$SEMGREP" >/dev/null 2>&1; then
    if [[ -n "${CI:-}${GITHUB_ACTIONS:-}" ]]; then
        fail "semgrep unavailable under CI — the suites step must install Semgrep 1.138.0 first"
    fi
    echo "SKIP: semgrep unavailable — behavioural rule fixtures skipped"
else
    SEMGREP_VERSION="$("$SEMGREP" --version)" || fail "semgrep failed to start"
    echo "semgrep behavioural fixtures: $SEMGREP_VERSION"
    FX="$WORK/fx"
    mkdir -p "$FX"
    # SSRF fixtures: literals stay silent even when their path contains a
    # fragment word (/v1/query, /api/params, webhook), and configured
    # constants (SLACK_WEBHOOK_URL, settings.EXTERNAL_API_URL) stay silent;
    # every dynamically built shape (bare, String()-wrapped, decoded,
    # templated, concatenated, accessor, callback, new URL) fires.
    cat > "$FX/fx-ssrf.js" <<'EOF'
async function h(req, searchParams, url, body) {
  await fetch("https://api.example.com/health");
  await fetch(req.query.url);
  await fetch(String(req.query.url));
  await fetch(decodeURIComponent(req.query.url));
  await fetch(`https://api.x/items/${req.query.url}`);
  await fetch("https://x/" + req.query.path);
  await fetch(url.searchParams.get("u"));
  await fetch(body.callbackUrl);
  await fetch(new URL(req.query.u));
  await fetch("https://api.example.com/v1/query");
  await fetch("/api/params");
}
EOF
    cat > "$FX/fx-ssrf.py" <<'EOF'
import requests, urllib.request
def h(request):
    requests.get("https://api.example.com/health")
    requests.get(request.args["u"])
    requests.get(f"https://x/{request.args['u']}")
    requests.get("https://x/" + request.args["u"])
    requests.get(str(request.args["u"]))
    urllib.request.urlopen("https://x/" + request.args["u"])
    requests.post("https://hooks.slack.com/services/webhook")
    requests.post(SLACK_WEBHOOK_URL, json=p)
    requests.get(settings.EXTERNAL_API_URL)
EOF
    # Redirect fixtures: literal targets and safe builders stay silent
    # (both argument positions, literal-URL construction, url_for), while
    # user-influenced targets fire — including the SvelteKit two-arg form
    # and Starlette RedirectResponse.
    cat > "$FX/fx-redirect.js" <<'EOF'
import { redirect } from "@sveltejs/kit";
import { NextResponse } from "next/server";
export function h(res, url, request) {
  res.redirect(302, "/home");
  throw redirect(302, url.searchParams.get("next"));
  throw redirect(302, "/home");
  NextResponse.redirect(new URL("/login", request.url));
  NextResponse.redirect(request.query.next);
}
EOF
    cat > "$FX/fx-redirect.py" <<'EOF'
from flask import redirect, url_for
from starlette.responses import RedirectResponse
def h(request):
    redirect(url_for("index"))
    redirect(request.args["next"])
    RedirectResponse(request.query_params["next"])
    RedirectResponse("/home")
EOF
    # Secrets fixtures: credential positives are assembled from split
    # fragments (never committed as literals); kebab-case words and
    # EXAMPLE-marked placeholders stay silent.
    SK_P="sk-"
    SK_PROJ="proj-"
    SK_SVC="svcacct-"
    SK_BODY="AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMn"
    SL_P="xoxb-"
    SL_BODY="4827364927364-5827364927364-2hWdFy7ejxVuN3P8F6aZbCdE9"
    {
        printf 'a=%s%s\n' "$SK_P" "$SK_BODY"
        printf 'b=%s%s%s\n' "$SK_P" "$SK_PROJ" "$SK_BODY"
        printf 'c=%s%s%s\n' "$SK_P" "$SK_SVC" "$SK_BODY"
        printf 'd=risk-assessmentconfigurationmanagerservice-extra-long-word-here\n'
        printf 'e=disk-encryptionkeymanagementservice-with-more-words-appended\n'
        printf 'f=%s%s\n' "$SL_P" "$SL_BODY"
        printf 'g=xoxb-EXAMPLE-TOKEN-FOR-DOCS-12345\n'
        printf 'h=url = "https://%s%s%s%s%s/%s%s"\n' "$DISC_A" "$DISC_B" "$DISC_C" "$CHAN_A" "$CHAN_B" "$TOK_A" "$TOK_B"
        printf 'i=key = "%s%s%s"\n' "$STRIPE_A" "$STRIPE_B" "$STRIPE_C"
    } > "$FX/fx-secrets.txt"
    "$SEMGREP" scan --config "$ROOT/$SSRF" --json "$FX/fx-ssrf.js" "$FX/fx-ssrf.py" "$FX/fx-redirect.js" "$FX/fx-redirect.py" > "$FX/ssrf.json" 2>/dev/null \
        || fail "semgrep scan of SSRF/redirect fixtures failed to run"
    "$SEMGREP" scan --config "$ROOT/$SECRETS" --json "$FX/fx-secrets.txt" > "$FX/secrets.json" 2>/dev/null \
        || fail "semgrep scan of secrets fixtures failed to run"
    python3 - "$FX/ssrf.json" "$FX/secrets.json" <<'PY'
import json, sys

errors = []
def check(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        errors.append(msg)

ssrf = json.load(open(sys.argv[1]))
hits = {}
for r in ssrf["results"]:
    rid = r["check_id"].split(".")[-1]
    hits.setdefault((r["path"].split("/")[-1], rid), set()).add(r["start"]["line"])

def expect_hit(fname, rid, lines):
    got = hits.get((fname, rid), set())
    for ln in lines:
        check(ln in got, f"behavioural {fname}:{ln} fires {rid}")

def expect_silent(fname, rid):
    got = hits.get((fname, rid), set())
    check(not got, f"behavioural {fname} stays silent for {rid} (got lines {sorted(got)})")

expect_hit("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js", (3, 4, 5, 6, 7, 8, 9, 10))
check(2 not in hits.get(("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js"), set()),
      "behavioural fx-ssrf.js:2 literal fetch stays silent")
for ln in (11, 12):
    check(ln not in hits.get(("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js"), set()),
          f"behavioural fx-ssrf.js:{ln} literal URL with a fragment word stays silent")
expect_silent("fx-ssrf.js", "tier0-open-redirect-js")
expect_hit("fx-ssrf.py", "tier0-ssrf-dynamic-url-py", (4, 5, 6, 7, 8))
check(3 not in hits.get(("fx-ssrf.py", "tier0-ssrf-dynamic-url-py"), set()),
      "behavioural fx-ssrf.py:3 literal request stays silent")
for ln in (9, 10, 11):
    check(ln not in hits.get(("fx-ssrf.py", "tier0-ssrf-dynamic-url-py"), set()),
          f"behavioural fx-ssrf.py:{ln} literal/constant URL stays silent")
expect_silent("fx-ssrf.py", "tier0-open-redirect-py")
expect_hit("fx-redirect.js", "tier0-open-redirect-js", (5, 8))
for ln in (4, 6, 7):
    check(ln not in hits.get(("fx-redirect.js", "tier0-open-redirect-js"), set()),
          f"behavioural fx-redirect.js:{ln} literal target stays silent")
expect_hit("fx-redirect.py", "tier0-open-redirect-py", (5, 6))
for ln in (4, 7):
    check(ln not in hits.get(("fx-redirect.py", "tier0-open-redirect-py"), set()),
          f"behavioural fx-redirect.py:{ln} safe/literal target stays silent")

sec = json.load(open(sys.argv[2]))
shits = {}
for r in sec["results"]:
    rid = r["check_id"].split(".")[-1]
    shits.setdefault(rid, set()).add(r["start"]["line"])

for ln in (1, 2, 3):
    check(ln in shits.get("tier0-secret-openai-key", set()),
          f"behavioural fx-secrets.txt:{ln} openai shape fires")
for ln in (4, 5):
    check(ln not in shits.get("tier0-secret-openai-key", set()),
          f"behavioural fx-secrets.txt:{ln} kebab word stays silent")
check(6 in shits.get("tier0-secret-slack-token", set()),
      "behavioural fx-secrets.txt:6 slack token fires")
check(7 not in shits.get("tier0-secret-slack-token", set()),
      "behavioural fx-secrets.txt:7 EXAMPLE placeholder stays silent")
check(8 in shits.get("tier0-secret-discord-webhook", set()),
      "behavioural fx-secrets.txt:8 discord webhook fires")
check(9 in shits.get("tier0-secret-stripe-live-key", set()),
      "behavioural fx-secrets.txt:9 stripe live key fires")

if errors:
    raise SystemExit(1)
PY
    pass "semgrep behavioural fixtures pin every rule"
fi

# Regression: the drift gate visits PORT-ADDITIONS.md-exempt names even
# when they exist on neither side, so a listed file deleted from staging
# fails (missing-staged) and a listed file since merged upstream fails
# (present-in-source). Exercised in a disposable worktree, so the checkout
# itself is never touched.
# Public-snapshot adaptation: the drift regression needs the recorded
# source pin as a local git object. Fresh public snapshots exclude that
# private history (see test_staged_drift.py), so skip only this section
# there — loudly, never as a silent pass. Everywhere the pin is present
# the regression below runs unchanged.
SOURCE_SHA="$(sed -n 's/^SOURCE_SHA=//p' "$ROOT/$BASE/tier-0/SOURCE" | head -1)"
if ! git -C "$ROOT" cat-file -e "${SOURCE_SHA}^{commit}" 2>/dev/null; then
    echo "SKIP: drift regression needs the recorded source pin object, absent from this public snapshot"
    echo "security-rules: ALL GREEN"
    exit 0
fi
DWT="$(mktemp -d "$SCRATCH/driftwt.XXXXXX")"
rmdir "$DWT"
git -C "$ROOT" worktree add --detach "$DWT" HEAD >/dev/null 2>&1 \
    || fail "could not create a disposable worktree for the drift regression"
trap 'git -C "$ROOT" worktree remove --force "$DWT" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT
WT_STAGE="$DWT/templates/org-github"
# Overlay the working tree under test: the worktree pins HEAD, which lacks
# uncommitted changes, and the drift check must see exactly these files.
# (check-staged-drift.sh resolves its repo root from the caller's cwd, so
# every invocation below runs with cwd inside the worktree.)
cp -r "$ROOT/templates/org-github/." "$WT_STAGE/"
(cd "$DWT" && "$WT_STAGE/check-staged-drift.sh") >/dev/null 2>&1 \
    || fail "drift gate fails on an intact worktree"
pass "drift gate passes on an intact port"
rm "$WT_STAGE/tier-0/semgrep-rules/security-ssrf-redirect.yaml"
if (cd "$DWT" && "$WT_STAGE/check-staged-drift.sh") >/dev/null 2>&1; then
    fail "drift gate passes with a listed staged file deleted"
fi
pass "drift gate fails when a listed staged file goes missing"
cp "$ROOT/templates/org-github/tier-0/semgrep-rules/security-ssrf-redirect.yaml" \
    "$WT_STAGE/tier-0/semgrep-rules/security-ssrf-redirect.yaml"
mkdir -p "$DWT/semgrep-rules"
cp "$WT_STAGE/tier-0/semgrep-rules/security-secrets-extended.yaml" \
    "$DWT/semgrep-rules/security-secrets-extended.yaml"
git -C "$DWT" add -A
git -C "$DWT" -c user.email=tier0-drift-test -c user.name=tier0-drift-test \
    commit -qm "drift regression probe: listed file merged upstream"
if (cd "$DWT" && "$WT_STAGE/check-staged-drift.sh" HEAD) >/dev/null 2>&1; then
    fail "drift gate passes with a listed file present in source"
fi
pass "drift gate fails when a listed file is merged upstream"

echo "security-rules: ALL GREEN"
