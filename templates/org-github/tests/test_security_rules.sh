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
# with Semgrep (verified at 1.138.0 and the gate pin 1.128.1). This repo's
# ci.yml does NOT install Semgrep or run this suite: run it with SEMGREP set
# to an existing binary (an absolute path, so the suite keeps system
# python3/PyYAML). When the suite is invoked with CI set, a missing binary
# fails hard instead of skipping. A local run without SEMGREP or semgrep on
# PATH SKIPs the behavioural section only. The drift-exemption regression
# does not depend on this section: the CI-wired test_staged_drift.py
# (SyntheticDrift) pins the PORT-ADDITIONS cases on every run.
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
for rid in ("tier0-ssrf-dynamic-fetch-js", "tier0-ssrf-generic-fetch-ts",
            "tier0-ssrf-dynamic-url-py",
            "tier0-open-redirect-js", "tier0-open-redirect-py"):
    check(rid in ssrf_ids, f"ssrf pack pins rule {rid}")

# Pin each rule's declared languages structurally. Semgrep's `javascript`
# target also parses .ts files, so dropping `typescript` from a JS rule
# changes no behavioural hit and only this declaration check notices; the
# pack header promises TypeScript coverage, so the declaration is part of
# the contract (the .ts fixtures below pin the behavioural side).
ssrf_by_id = {r.get("id"): r for r in ssrf.get("rules", [])}
for rid, langs in (("tier0-ssrf-dynamic-fetch-js", ["javascript", "typescript"]),
                   ("tier0-ssrf-generic-fetch-ts", ["typescript"]),
                   ("tier0-ssrf-dynamic-url-py", ["python"]),
                   ("tier0-open-redirect-js", ["javascript", "typescript"]),
                   ("tier0-open-redirect-py", ["python"])):
    check((ssrf_by_id.get(rid) or {}).get("languages") == langs,
          f"ssrf/{rid} declares languages {langs}")

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
        fail "semgrep unavailable with CI set — point SEMGREP at an installed Semgrep binary (this repo's CI does not install one)"
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
    # templated, concatenated, accessor, callback, new URL) fires. The
    # axios config forms fire on a dynamic url, baseURL or spread source
    # (bare and .request alike) and stay silent on literal ones. The
    # (url, config) spelling fires on a dynamic baseURL/spread in the
    # config argument even when the url itself is a literal, and the .ts
    # fixture pins the TypeScript language path plus the generic-call
    # regex rule (axios.get<T>(...)), which no call pattern can bind.
    # The $METHOD split is pinned too: post/put/patch payload objects in
    # second position stay silent (the config is third there), as do
    # axios.create/isAxiosError shapes the method regex excludes, and the
    # Python options-method lines stay silent for the same reason. The .ts
    # config url values run to the property boundary so comma-carrying
    # path.join/new-URL expressions fire, while payload and nested-spread
    # typed lines stay silent exactly like the untyped rule. Constant-url
    # typed lines with later req.* properties (BASE_URL + params/headers)
    # pin the boundary lookahead itself: without it they fire. Constant
    # first arguments with later req.* arguments (UPSTREAM_URL + body/
    # params) pin the first-argument top-level-comma boundary: without it
    # the direct/template/concat captures run past the comma and fire;
    # a.b(c) + req... and `${a.b(c)}/${req...}` pin the paren-level and
    # in-backtick spans. DEFAULTS/URL_CONST spread lines pin the spread
    # comma boundary and the get-style paren group: without them a spread
    # past a comma fires and path.join(a, b) with a spread config misses.
    # Nested
    # objects before url/baseURL (headers, method/data) and inline payload
    # objects in post/put/patch second position fire, while nested url/
    # baseURL/spread positions stay silent. Typed post/put/patch calls with
    # a config third argument fire; http.createServer/https.Agent lines pin
    # the $HTTP.$METHOD constraint (without it they fire).
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
  await axios.get(req.query.url);
  await axios.get("https://api.example.com/v1/query");
  await axios(req.query.url);
  await got(req.query.url);
  await https.request(req.query.url);
  await https.get("https://api.example.com/v1/query");
  await axios("https://api.example.com/v1/query");
  await got("https://api.example.com/v1/query");
  await axios({ method: "get", url: req.query.url });
  await axios({ method: "get", url: "https://x/a", params: req.query });
  await axios({ method: "get", url: "https://api.example.com/v1/query", params: req.query });
  await axios({ method: "get", url: API_URL, params: req.query });
  await axios({ baseURL: req.query.host, url: "/x" });
  await axios({ ...req.body, method: "get" });
  await axios({ baseURL: "https://api.example.com/v1/query", url: "/x" });
  await axios.request({ url: req.query.url });
  await axios.request({ url: "https://api.example.com/v1/query", params: req.query });
  await axios.request({ baseURL: req.query.host, url: "/x" });
  await axios.request({ baseURL: "https://api.example.com/v1/query", url: "/x" });
  await axios.request({ ...req.body });
  await axios.get("/x", { baseURL: req.query.host });
  await axios.get("/x", { ...req.body });
  await axios.get("/x", { baseURL: "https://api.example.com/v1/query" });
  await axios("/x", { baseURL: req.query.host });
  await axios("/x", { ...req.body });
  await axios("/x", { baseURL: "https://api.example.com/v1/query" });
  await axios.post("/x", data, { baseURL: req.query.host });
  await axios.post("/x", data, { ...req.body });
  await axios.post("/x", data, { baseURL: "https://api.example.com/v1/query" });
  await axios.post("/x", { ...req.body });
  await axios.put("/x", { ...req.body, id: 1 });
  await axios.post("/x", { baseURL: req.query.host });
  await axios.create(req.query.config);
  await axios.isAxiosError(req.query.err);
  await axios.create({ url: req.query.url });
  await axios.create({ baseURL: req.query.host, url: "/x" });
  await axios.create({ ...req.body });
  await axios.get("/x", data, { baseURL: req.query.host });
  await axios.get("/x", data, { ...req.body });
  await http.createServer(req.query.handler);
  await https.Agent(req.query.opts);
  await got.get(req.query.u);
  await got.post(req.body.url, { json: data });
  await got.stream(`${base}/${req.query.p}`);
  await got.get("https://api.example.com/v1/query");
  await got.get(API_URL);
  await got.extend(req.query.opts);
  await got.paginate(req.query.u);
}
EOF
    cat > "$FX/fx-ssrf.ts" <<'EOF'
type User = { id: number };
async function h(req: any, data: any, url: any) {
  await axios.get(req.query.url);
  await fetch(req.query.url);
  await got(req.query.url);
  await axios.get<User>(req.query.url);
  await axios.post<User>(req.body.url, data);
  await axios<User>(req.query.url);
  await axios<User>({ baseURL: req.query.host, url: "/x" });
  await axios<User>({ url: req.query.url });
  await axios<User>({ ...req.body, method: "get" });
  await axios.request<User>({ url: req.query.url });
  await got<User>(req.query.url);
  await got.get<User>(req.query.url);
  await axios.get<User>(url.searchParams.get("u"));
  await axios.get<User>("/x", { baseURL: req.query.host });
  await axios.get<User>("/x", { ...req.body });
  await axios.get<User>(`https://api.x/items/${req.query.url}`);
  await axios.get<User>("https://x/" + req.query.path);
  await axios<User>({ url: `https://api.x/items/${req.query.url}` });
  await axios<User>({ url: "https://x/" + req.query.path });
  await axios.get<User> (req.query.url);
  await axios.get<User>("https://api.example.com/v1/query");
  await axios<User>({ url: "https://api.example.com/v1/query", params: req.query });
  await axios<User>({ baseURL: "https://api.example.com/v1/query", url: "/x" });
  await axios.get<User>(API_URL);
  await axios.get<User>("/x", { baseURL: "https://api.example.com/v1/query" });
  await axios.get<User>(`https://api.example.com/v1/query`);
  await axios.get<User>("https://api.example.com/" + "v1/query");
  await axios.get("https://api.example.com/v1/query");
  await axios<User>({ url: path.join(base, req.query.p) });
  await axios<User>({ url: new URL(path, req.query.base).href });
  await axios.post<User>("/internal/users", { ...req.body });
  await axios.put<User>("/internal/users/1", { ...req.body, id: 1 });
  await axios.patch<User>("/internal/users/1", { ...req.query });
  await axios.post<User>("/internal/users", { baseURL: req.body.host });
  await axios.post<User>("/internal", data, { headers: { ...req.headers } });
  await axios.get<User>("/internal", { params: { ...req.query } });
  await axios<User>({ url: "/internal", headers: { ...req.headers } });
  await axios<User>({ method: "post", data: { url: req.body.target }, url: "/hook" });
  await axios<User>({ url: BASE_URL, params: req.query });
  await axios<User>({ url: BASE_URL, headers: req.headers });
  await axios.get<User>("/x", { baseURL: BASE_URL, params: req.query });
  await axios.post<User>("/x", data, { baseURL: BASE_URL, headers: req.headers });
  await axios<User>({ headers: { Authorization: tok }, url: req.query.u });
  await axios<User>({ method: "post", data: { a: 1 }, url: req.body.target });
  await axios<User>({ headers: { A: 1 }, baseURL: req.query.host, url: "/x" });
  await axios.post<User>("/x", { a: 1 }, { baseURL: req.query.host });
  await axios.post<User>("/x", { a: 1 }, { ...req.body });
  await axios.put<User>("/x", { id: req.body.id }, { baseURL: req.query.host });
  await axios.get<User>("/x", { params: { a: 1 }, baseURL: req.query.host });
  await axios.get<User>("/x", { headers: { ...req.headers }, baseURL: req.query.host });
  await axios.get<User>(path.join(a, b), { baseURL: req.query.host });
  await axios.post<User>("/x", data, { baseURL: req.query.host });
  await axios.post<User>("/x", data, { ...req.body });
  await axios.put<User>("/x", data, { baseURL: req.query.host });
  await axios.patch<User>("/x", data, { ...req.body });
  await axios<User>({ extra: { baseURL: req.query.host }, url: BASE_URL });
  await axios.get<User>("/x", { extra: { ...req.body }, baseURL: BASE_URL });
  await axios<User>({ headers: { A: 1 }, url: `/x/${req.query.p}` });
  await axios<User>({ headers: { A: 1 }, url: "/x/" + req.query.p });
  await axios<User>({ headers: { A: 1 }, ...req.body });
  await axios.get<User>("/x", { headers: { A: 1 }, ...req.body });
  await axios.post<User>("/x", data, { headers: { A: 1 }, baseURL: req.query.host });
  await axios.post<User>("/x", data, { headers: { A: 1 }, ...req.body });
  await axios<User>({ url: "/x/" + BASE_URL, params: req.query });
  await axios<User>({ url: `${BASE_URL}/users/${req.params.id}` });
  await axios<User>({ url: `${BASE_URL}/${req.query.p}`, timeout: 5 });
  await axios.post<User>(path.join(a, b), data, { baseURL: req.query.host });
  await axios.put<User>("/x", fn(a, b), { baseURL: req.query.host });
  await axios.put<User>("/x", [1, 2], { baseURL: req.query.host });
  await axios.post<User>("/x", JSON.stringify(a, b), { baseURL: req.query.host });
  await axios.post<User>(path.join(a, b), data, { ...req.body });
  await axios.post<User>("/x", fn(a, b), { ...req.body });
  await axios.put<User>("/x", [1, 2], { ...req.body });
  await axios.post<User>(UPSTREAM_URL, req.body);
  await axios.get<User>(UPSTREAM_URL, { params: req.query });
  await axios.get<User>(`${UPSTREAM}/items`, { params: req.query });
  await got.post<User>(UPSTREAM_URL, { json: req.body });
  await axios<User>(UPSTREAM_URL, { params: req.query });
  await axios.get<User>(a.b(c) + req.query.path);
  await axios.get<User>(`${a.b(c)}/${req.query.path}`);
  await axios<User>({ ...DEFAULTS, params: req.query });
  await axios.get<User>(URL_CONST, { ...DEFAULTS, params: req.query });
  await axios.post<User>(URL_CONST, data, { ...DEFAULTS, params: req.query });
  await axios.get<User>(path.join(a, b), { ...req.body });
  await axios.get<User>(encodeURI(String(req.query.u)));
  await axios.get<User>(buildUrl(base, encodeURIComponent(req.query.p)));
  await axios.get<User>("/" + a.b(c) + req.query.path);
  await axios.get<User>("https://up.example/" + UPSTREAM_PATH, { params: req.query });
  await axios.get<User>(`/items`, { headers: { h: `${req.headers.h}` } });
  await axios<User>({ callback_url: req.query.cb, url: BASE_URL });
  await axios.get<User>("/x", { mybaseURL: req.query.h, baseURL: BASE_URL });
  await axios.get<User>([base, req.query.p].join("/"));
  await axios.get<User>("/" + encodeURI(String(req.query.u)));
  await axios.get<User>("/" + [a, req.query.p].join("/"));
  await axios<User>({ callback_url: `${req.query.cb}`, url: BASE_URL });
  await axios<User>({ callback_url: "/x/" + req.query.cb, url: BASE_URL });
  await axios.post<User>("/x", data, { mybaseURL: req.query.h, baseURL: BASE_URL });
  await axios<User>({ url: BASE_URL, "X-Key": req.query.k });
  await axios<User>({ "url": req.query.u });
  await axios<User>({ "url": `${req.query.p}/x` });
  await axios<User>({ "url": "/x/" + req.query.p });
  await axios.get<User>("/x", { "baseURL": req.query.h });
  await axios.post<User>("/x", data, { "baseURL": req.query.h });
  await axios<User>({ url: "/x/" + BASE_URL, "X-Key": req.query.k });
  await axios.get<User>("/x", { baseURL: BASE_URL, "X-Key": req.query.k });
  await axios.post<User>("/x", data, { baseURL: BASE_URL, "X-Key": req.query.k });
  await axios<User>({ baseURL: API_BASE, url: req.query.path });
  await axios<User>({ url: BASE_URL, baseURL: req.query.host });
  await axios<User>({ ...DEFAULTS, ...req.body });
  await axios.get<User>(URL_CONST, { ...DEFAULTS, ...req.query });
  await axios.post<User>(URL_CONST, data, { ...DEFAULTS, ...req.query });
  await axios<User>({ "baseURL": req.query.host });
  await axios<User>({ baseURL: BASE_URL, "X-Key": req.query.k });
  await axios<User>({ "baseURL": `${req.query.h}/x` });
  await axios<User>({ "baseURL": "/x/" + req.query.p });
  await axios<User>({ baseURL: "/x/" + BASE_URL, "X-Key": req.query.k });
  await axios<User>({ 'x-url': req.query.u });
  await axios<User>({ 'x-baseURL': req.query.h });
  await axios<User>({ 'x-url': `${req.query.p}/x` });
  await axios<User>({ 'x-baseURL': `${req.query.h}/x` });
  await axios<User>({ 'x-url': "/x/" + req.query.p });
  await axios<User>({ 'x-baseURL': "/x/" + req.query.h });
  await axios.get<User>("/x", { 'x-baseURL': req.query.h });
  await axios.post<User>("/x", data, { 'x-baseURL': req.query.h });
  await axios<User>({ method: "get", url: ENDPOINT, params });
  await axios.get<User>("/x", { baseURL: API_BASE, params });
  await axios.post<User>("/x", data, { baseURL: API_BASE, params });
  await axios<User>({ url: "/api/" + id, query });
  await axios<User>({ ...merge(DEFAULTS, req.body) });
  await axios.get<User>(URL_CONST, { ...merge(DEFAULTS, req.query) });
  await axios.post<User>(URL_CONST, data, { ...merge(DEFAULTS, req.query) });
  await axios<User>({ ...ctx.Query });
  await axios.get<User>(URL_CONST, { ...ctx.Query });
  await axios.post<User>(URL_CONST, data, { ...ctx.Query });
}
EOF
    cat > "$FX/fx-ssrf.py" <<'EOF'
import requests, httpx, urllib.request
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
    requests.get("https://x/request.html")
    requests.request("GET", request.args["u"])
    requests.request("GET", "https://x/request.html")
    requests.get(url=request.args["u"])
    requests.get(url="https://x/request.html")
    httpx.get(request.args["u"])
    httpx.request("GET", request.args["u"])
    httpx.get(url=request.args["u"])
    httpx.get("https://x/request.html")
    requests.Session().get(request.args["u"])
    httpx.AsyncClient().get(request.args["u"])
    httpx.Client().get(request.args["u"])
    httpx.request("GET", "https://x/request.html")
    urllib.request.urlopen("https://x/request.html")
    requests.Session().get("https://x/request.html")
    requests.post("https://x/request.html")
    requests.put("https://x/request.html")
    requests.patch("https://x/request.html")
    requests.delete("https://x/request.html")
    requests.head("https://x/request.html")
    httpx.post("https://x/request.html")
    httpx.put("https://x/request.html")
    httpx.patch("https://x/request.html")
    httpx.delete("https://x/request.html")
    httpx.head("https://x/request.html")
    httpx.get(url="https://x/request.html")
    httpx.Client().get("https://x/request.html")
    httpx.AsyncClient().get("https://x/request.html")
    requests.options(request.args["u"])
    httpx.options(request.args["u"])
    requests.options(url=request.args["u"])
    httpx.options(url=request.args["u"])
    requests.Session().options(request.args["u"])
    httpx.Client().options(request.args["u"])
    httpx.AsyncClient().options(request.args["u"])
EOF
    # Redirect fixtures: literal targets and safe builders stay silent
    # (both argument positions, literal-URL construction, url_for), while
    # user-influenced targets fire — including the SvelteKit (status, url)
    # form, the url-first (url, status/init) forms, Starlette
    # RedirectResponse in positional and url= keyword spellings, and the
    # TypeScript language path (.ts fixture). The fragment-carrying literals
    # ("/search?query=x", "/request.html", url_for with request.args) pin
    # each literal/safe-builder guard: without the guard the fragment
    # regex alone would fire on them. Constant non-literal targets
    # (HOME_URL, config.loginPath, settings.LOGIN_URL, reverse("home"),
    # HOME) pin the user-input fragment regex itself: without it they fire.
    cat > "$FX/fx-redirect.js" <<'EOF'
import { redirect } from "@sveltejs/kit";
import { NextResponse } from "next/server";
export function h(res, url, request, req) {
  res.redirect(302, "/home");
  throw redirect(302, url.searchParams.get("next"));
  throw redirect(302, "/home");
  NextResponse.redirect(new URL("/login", request.url));
  NextResponse.redirect(request.query.next);
  res.redirect(302, req.query.next);
  res.redirect(req.query.next);
  throw redirect(url.searchParams.get("next"));
  res.redirect("/search?query=x");
  res.redirect(302, "/search?query=x");
  throw redirect("/search?query=x");
  throw redirect(302, "/search?query=x");
  NextResponse.redirect(req.nextUrl.searchParams.get("next"), 302);
  res.redirect(req.query.next, 301);
  NextResponse.redirect("/search?query=x", 302);
  res.redirect("/search?query=x", 301);
  Response.redirect(request.query.next, 302);
  res.redirect(HOME_URL);
  res.redirect(302, HOME_URL);
  throw redirect(303, HOME_URL);
  Response.redirect(config.loginPath, 302);
}
EOF
    cat > "$FX/fx-redirect.ts" <<'EOF'
import { NextResponse } from "next/server";
export function h(res: any, req: any) {
  res.redirect(req.query.next);
  NextResponse.redirect(req.nextUrl.searchParams.get("next"), 302);
  res.redirect("/search?query=x");
  res.redirect("/search?query=x", 301);
}
EOF
    # Import-free copy: with the SvelteKit import present, Semgrep's
    # qualified-name matching also lets the $RES.redirect guards silence
    # the bare redirect() literals above, so this copy is what pins the
    # bare redirect("...") / redirect($S, "...") guards themselves.
    cat > "$FX/fx-redirect-bare.js" <<'EOF'
export function h(url) {
  throw redirect(url.searchParams.get("next"));
  throw redirect("/search?query=x");
  throw redirect(302, "/search?query=x");
  throw redirect(url.searchParams.get("next"), { headers });
  throw redirect("/search?query=x", { headers });
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
    HttpResponseRedirect(request.GET["next"])
    HttpResponseRedirect("/home")
    redirect(request.args["next"], code=302)
    RedirectResponse(request.query_params["next"], status_code=302)
    redirect("/request.html")
    redirect(url_for("login", next=request.args["next"]))
    HttpResponseRedirect("/request.html")
    RedirectResponse("/request.html")
    redirect("/request.html", "/home")
    RedirectResponse(url=request.query_params["next"])
    RedirectResponse(url=request.query_params["next"], status_code=302)
    RedirectResponse(url="/request.html")
    redirect(location=request.args["next"])
    redirect(to=request.args["next"])
    redirect(location="/request.html")
    redirect(to="/request.html")
    redirect(location=url_for("login", next=request.args["next"]))
    redirect(settings.LOGIN_URL)
    HttpResponseRedirect(reverse("home"))
    redirect(HOME)
EOF
    # Secrets fixtures: credential positives are assembled from split
    # fragments (never committed as literals); kebab-case words and
    # EXAMPLE-marked placeholders stay silent.
    SK_P="sk-"
    SK_PROJ="proj-"
    SK_SVC="svcacct-"
    SK_BODY="AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMn"
    SK_SHORT="AbCdEfGh-12345678-IjKlMnOpQrStUv"
    SK_EX_BODY="EXAMPLEAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    SK_REP_BODY="AAAAAAAAAABcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
    SL_P="xoxb-"
    SL_BODY="4827364927364-5827364927364-2hWdFy7ejxVuN3P8F6aZbCdE9"
    SL_CANARY="4827shy2-abcdefghijklmnop"
    SL_EX_BODY="12345678-12345678-example123456789012"
    AWS_A="AK"
    AWS_B="IA"
    AWS_BODY="1A2B3C4D5E6F7G8H"
    AWS_EX="IOSFODNN7EXAMPLE"
    GH_A="gh"
    GH_B="p_"
    GH_BODY="AbCdEfGh1234567890IjKl"
    GH_PAT_A="github_"
    GH_PAT_B="pat_"
    GH_PAT_BODY="AbCdEfGh_1234567890_IjKlMn"
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
        printf 'j=%s%s%s\n' "$AWS_A" "$AWS_B" "$AWS_BODY"
        printf 'k=%s%s%s\n' "$AWS_A" "$AWS_B" "$AWS_EX"
        printf 'l=%s%s%s\n' "$GH_A" "$GH_B" "$GH_BODY"
        printf 'm=%s%s%s\n' "$GH_PAT_A" "$GH_PAT_B" "$GH_PAT_BODY"
        printf 'n=key = "%s%s%s"\n' "$STRIPE_R" "$STRIPE_B" "$STRIPE_C"
        printf 'o=url = "https://%s%s%s%s%s/%s%s"\n' "$DISC_D" "$DISC_B" "$DISC_C" "$CHAN_A" "$CHAN_B" "$TOK_A" "$TOK_B"
        printf 'p=%s%s\n' "$SL_P" "$SL_CANARY"
        printf 'q=%s%s\n' "$SK_P" "$SK_SHORT"
        printf 'r=%s%s\n' "$SL_P" "$SL_EX_BODY"
        printf 's=%s%s\n' "$SK_P" "$SK_EX_BODY"
        printf 't=%s%s\n' "$SK_P" "$SK_REP_BODY"
    } > "$FX/fx-secrets.txt"
    "$SEMGREP" scan --config "$ROOT/$SSRF" --json "$FX/fx-ssrf.js" "$FX/fx-ssrf.ts" "$FX/fx-ssrf.py" "$FX/fx-redirect.js" "$FX/fx-redirect.ts" "$FX/fx-redirect-bare.js" "$FX/fx-redirect.py" > "$FX/ssrf.json" 2>/dev/null \
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

expect_hit("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js", (3, 4, 5, 6, 7, 8, 9, 10, 13, 15, 16, 17, 21, 25, 26, 28, 30, 32, 33, 34, 36, 37, 39, 40, 54, 55, 56))
check(2 not in hits.get(("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js"), set()),
      "behavioural fx-ssrf.js:2 literal fetch stays silent")
for ln in (11, 12, 14, 18, 19, 20, 22, 23, 24, 27, 29, 31, 35, 38, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 57, 58, 59, 60):
    check(ln not in hits.get(("fx-ssrf.js", "tier0-ssrf-dynamic-fetch-js"), set()),
          f"behavioural fx-ssrf.js:{ln} literal/constant URL stays silent")
expect_silent("fx-ssrf.js", "tier0-open-redirect-js")
expect_hit("fx-ssrf.ts", "tier0-ssrf-dynamic-fetch-js", (3, 4, 5))
expect_hit("fx-ssrf.ts", "tier0-ssrf-generic-fetch-ts", (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 31, 32, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 60, 61, 62, 63, 64, 65, 67, 68, 69, 70, 71, 72, 73, 74, 75, 81, 82, 86, 87, 88, 89, 94, 95, 96, 101, 102, 103, 104, 105, 109, 110, 111, 112, 113, 114, 116, 117, 131, 132, 133, 134, 135, 136))
for ln in (23, 24, 25, 26, 27, 28, 29, 30, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 58, 59, 66, 76, 77, 78, 79, 80, 83, 84, 85, 90, 91, 92, 93, 97, 98, 99, 100, 106, 107, 108, 115, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130):
    check(ln not in hits.get(("fx-ssrf.ts", "tier0-ssrf-generic-fetch-ts"), set())
          and ln not in hits.get(("fx-ssrf.ts", "tier0-ssrf-dynamic-fetch-js"), set()),
          f"behavioural fx-ssrf.ts:{ln} literal/constant URL stays silent")
expect_silent("fx-ssrf.ts", "tier0-open-redirect-js")
expect_hit("fx-ssrf.py", "tier0-ssrf-dynamic-url-py", (4, 5, 6, 7, 8, 13, 15, 17, 18, 19, 21, 22, 23))
check(3 not in hits.get(("fx-ssrf.py", "tier0-ssrf-dynamic-url-py"), set()),
      "behavioural fx-ssrf.py:3 literal request stays silent")
for ln in (9, 10, 11, 12, 14, 16, 20, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46):
    check(ln not in hits.get(("fx-ssrf.py", "tier0-ssrf-dynamic-url-py"), set()),
          f"behavioural fx-ssrf.py:{ln} literal/constant URL stays silent")
expect_silent("fx-ssrf.py", "tier0-open-redirect-py")
expect_hit("fx-redirect.js", "tier0-open-redirect-js", (5, 8, 9, 10, 11, 16, 17, 20))
for ln in (4, 6, 7, 12, 13, 14, 15, 18, 19, 21, 22, 23, 24):
    check(ln not in hits.get(("fx-redirect.js", "tier0-open-redirect-js"), set()),
          f"behavioural fx-redirect.js:{ln} literal target stays silent")
expect_hit("fx-redirect.ts", "tier0-open-redirect-js", (3, 4))
for ln in (5, 6):
    check(ln not in hits.get(("fx-redirect.ts", "tier0-open-redirect-js"), set()),
          f"behavioural fx-redirect.ts:{ln} literal target stays silent")
expect_hit("fx-redirect-bare.js", "tier0-open-redirect-js", (2, 5))
for ln in (3, 4, 6):
    check(ln not in hits.get(("fx-redirect-bare.js", "tier0-open-redirect-js"), set()),
          f"behavioural fx-redirect-bare.js:{ln} literal target stays silent")
expect_silent("fx-redirect-bare.js", "tier0-ssrf-dynamic-fetch-js")
expect_hit("fx-redirect.py", "tier0-open-redirect-py", (5, 6, 8, 10, 11, 17, 18, 20, 21))
for ln in (4, 7, 9, 12, 13, 14, 15, 16, 19, 22, 23, 24, 25, 26, 27):
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
check(10 in shits.get("tier0-secret-aws-key", set()),
      "behavioural fx-secrets.txt:10 aws key fires")
check(11 not in shits.get("tier0-secret-aws-key", set()),
      "behavioural fx-secrets.txt:11 aws EXAMPLE placeholder stays silent")
check(12 in shits.get("tier0-secret-github-token", set()),
      "behavioural fx-secrets.txt:12 ghp_ token fires")
check(13 in shits.get("tier0-secret-github-token", set()),
      "behavioural fx-secrets.txt:13 github_pat_ token fires")
check(14 in shits.get("tier0-secret-stripe-live-key", set()),
      "behavioural fx-secrets.txt:14 stripe rk_live key fires")
check(15 in shits.get("tier0-secret-discord-webhook", set()),
      "behavioural fx-secrets.txt:15 discordapp webhook fires")
check(16 not in shits.get("tier0-secret-slack-token", set()),
      "behavioural fx-secrets.txt:16 slack short canary stays silent")
check(17 not in shits.get("tier0-secret-openai-key", set()),
      "behavioural fx-secrets.txt:17 openai short-tail canary stays silent")
check(18 not in shits.get("tier0-secret-slack-token", set()),
      "behavioural fx-secrets.txt:18 slack real-shape token with example marker stays silent")
check(19 not in shits.get("tier0-secret-openai-key", set()),
      "behavioural fx-secrets.txt:19 openai real-shape key with EXAMPLE marker stays silent")
check(20 not in shits.get("tier0-secret-openai-key", set()),
      "behavioural fx-secrets.txt:20 openai repeated-character placeholder stays silent")

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
