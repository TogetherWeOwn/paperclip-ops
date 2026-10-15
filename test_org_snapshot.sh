#!/usr/bin/env bash
# Offline suite for the org-provisioner ORG_SNAPSHOT fixture path (case_1695ae38).
#
# NO DATABASE, NO CREDENTIALS, NO NETWORK, NO CONTAINER ENGINE. This container
# has none of podman, docker, psql, paperclipai, or `column`, and the suite is
# green here anyway — that absence IS the proof the snapshot path touches
# nothing live.
#
# Two seams, deliberately different shapes sharing the same columns:
#   provisioner ORG_SNAPSHOT  a DIRECTORY (tests/fixtures/org-snapshot/):
#                             org.tsv, grants.tsv — reads served, writes refuse
#   queue       ORG_SNAPSHOT  a FILE (a copy of that org.tsv in mktemp):
#                             the queue's pre-existing seam — the queue's
#                             approve execution still needs a PROV, stubbed or
#                             real (real refuses read-only, by design)
#
# What "both paths, diffed" means here. Every refusal verdict below is emitted
# by code SHARED with the live path (same die sites), so snapshot and live
# read byte-identical for the same data by construction, and the goldens pin
# those bytes. The diffs the suite actually runs: pure catalogs with the
# variable set vs unset-shaped values, and queue verdicts with a stub PROV vs
# the real provisioner. The ONE documented divergence is terminal approve
# execution, which is a WRITE and therefore read-only under the snapshot.
# The heavy-tier live suites stay authoritative for live-database behavior.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v jq >/dev/null || { echo "ERROR: jq required" >&2; exit 1; }
FIXTURE="$HERE/tests/fixtures/org-snapshot"
[[ -d "$FIXTURE" ]] || { echo "ERROR: missing fixture dir $FIXTURE" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export COMPANY_ID="offline-fixture-not-a-real-company"
# Exported: both the queue (PROV, ORG_SNAPSHOT) and the stub (CREATE_ARGV,
# ORG_SNAPSHOT) read these from the environment, not argv.
export PROV="$HERE/org_provisioner.sh"
Q="$HERE/org_request_queue.sh"
export CREATE_ARGV="$TMP/create.argv"

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }
note() { printf '        %s\n' "$1"; }

# refuses_because <desc> <reason-substring> <cmd...>: a refusal from the WRONG
# gate is a FAIL — see test_request_queue.sh:34 (case_39b2a64b).
refuses_because() {
  local d="$1" why="$2"; shift 2
  local o; o="$("$@" 2>&1)"; local rc=$?
  if [[ $rc -eq 0 ]]; then bad "$d — was ALLOWED (rc=0)"; return; fi
  if ! grep -q REFUSED <<<"$o"; then bad "$d — non-zero but not a refusal (rc=$rc)"; sed 's/^/        /' <<<"$o" | head -3; return; fi
  if grep -qF -- "$why" <<<"$o"; then ok "$d"
  else bad "$d — refused by the WRONG gate; wanted '$why'"; sed 's/^/        /' <<<"$o" | head -4; fi
}
must_allow()  { local d="$1"; shift; local o; o="$("$@" 2>&1)"; local rc=$?
  if [[ $rc -eq 0 ]]; then ok "$d"; else bad "$d (rc=$rc)"; sed 's/^/        /' <<<"$o" | head -4; fi; }
eq() { [[ "$2" == "$3" ]] && ok "$1" || { bad "$1"; diff <(printf '%s\n' "$2") <(printf '%s\n' "$3") | head -10 | sed 's/^/        /'; }; }

fixture_sha() { (cd "$FIXTURE" && sha256sum org.tsv grants.tsv README.md); }
SHA_BEFORE="$(fixture_sha)"

# --- the stub provisioner (approve execution only) -----------------------------
# Same construction as test_responsible_leader.sh: catalogs EXTRACTED from
# org_provisioner.sh, never copied. `create` records argv and echoes a fixed
# PROVISIONED line; it deliberately does NOT append to the fixture.
build_stub() {
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    sed -n "/^CEILING_JSON='{/,/^}'\$/p" "$HERE/org_provisioner.sh"
    sed -n "/^TEMPLATES_JSON='{/,/^}'\$/p" "$HERE/org_provisioner.sh"
    cat <<'STUB'
[[ -n "${CEILING_JSON:-}" ]]   || { echo "stub: failed to extract CEILING_JSON" >&2; exit 90; }
[[ -n "${TEMPLATES_JSON:-}" ]] || { echo "stub: failed to extract TEMPLATES_JSON" >&2; exit 90; }
case "${1:-}" in
  ceiling) jq -r 'to_entries[] | "\(.key)\t\(.value|join(", "))"' <<<"$CEILING_JSON" ;;
  template-keys) jq -r 'to_entries[] | "\(.key)\t\(.value|map(.permissionKey)|join(","))"' <<<"$TEMPLATES_JSON" ;;
  create)
    shift; printf '%s\n' "$*" >> "$CREATE_ARGV"
    echo "PROVISIONED E0_SPECIALIST -> 00000000-0000-4000-8000-000000000177 (stubbed execution)"
    ;;
  *) echo "stub: unsupported subcommand '${1:-}'" >&2; exit 91;;
esac
STUB
  } > "$TMP/stub_provisioner.sh"
  chmod +x "$TMP/stub_provisioner.sh"
}

hdr "0. Fixture shape"
field_check() {
  local f="$1" want="$2"
  if awk -F'\t' -v w="$want" 'NF!=w{bad=1} END{exit bad}' "$f"; then ok "$f: every line has $want fields"
  else bad "$f: a line does not have $want tab-separated fields"; fi
}
field_check "$FIXTURE/org.tsv" 6
# Grants rows are agent + key + optional subtree: a company-wide grant is
# `agent\tkey` with no trailing tab (trailing whitespace in a checked-in
# file would be stripped by editors anyway), so 2 or 3 fields both count.
if awk -F'\t' 'NF<2||NF>3{bad=1} END{exit bad}' "$FIXTURE/grants.tsv"; then
  ok "grants.tsv: every line has agent, key, and optional subtree"
else bad "grants.tsv: a line is malformed"; fi
[[ "$(wc -l < "$FIXTURE/org.tsv")" -ge 10 ]] && ok "org.tsv covers a company-shaped org" || bad "org.tsv is too small to cover approve/reject/ceiling-skip"
grep -q "terminated" "$FIXTURE/org.tsv" && ok "org.tsv carries a terminated row" || bad "no terminated row in org.tsv"

hdr "1. Pure catalogs never consult the variable (live behavior unchanged)"
export ORG_SNAPSHOT="$FIXTURE" GRANT_LOG="$TMP/grant.log"
must_allow "ceiling works under ORG_SNAPSHOT" "$PROV" ceiling
eq "template-keys C1 line is the live catalog" \
  "$("$PROV" template-keys | grep '^C1_DIRECTOR_BUILDER')" \
  "C1_DIRECTOR_BUILDER	agents:configure,tasks:assign_scope,tasks:manage_active_checkouts,skills:suggest-changes"
# The queue drives this tool with ITS file-shaped ORG_SNAPSHOT; pure commands
# must not trip on that shape (lazy validation — only a snapshot READ demands
# the directory).
eq "ceiling is byte-identical under a file-shaped var" \
  "$(ORG_SNAPSHOT="$FIXTURE/org.tsv" "$PROV" ceiling)" \
  "$("$PROV" ceiling)"
eq "template-keys is byte-identical under a file-shaped var" \
  "$(ORG_SNAPSHOT="$FIXTURE/org.tsv" "$PROV" template-keys)" \
  "$("$PROV" template-keys)"

hdr "2. Reads serve from the fixture with pinned verdicts"
eq "access DIR: placement, template, scopes" \
  "$("$PROV" access DIR)" \
  "$(cat <<'GOLD'
DIR  [C1_DIRECTOR_BUILDER]  parent=T0
  agents:configure               SELF-subtree
  skills:suggest-changes         company-wide
  tasks:assign                   company-wide
  tasks:assign_scope             SELF-subtree
  tasks:manage_active_checkouts  SELF-subtree
GOLD
)"
eq "access ENG: template with no governance grants, baseline only" \
  "$("$PROV" access ENG)" \
  "$(cat <<'GOLD'
ENG  [E0_SPECIALIST]  parent=MGR
  tasks:assign                   company-wide
GOLD
)"
eq "access T0: chief template, mixed scopes" \
  "$("$PROV" access T0)" \
  "$(cat <<'GOLD'
T0  [B2_TECH_CHIEF]  parent=O1
  agents:configure               SELF-subtree
  environments:manage            company-wide
  skills:create                  company-wide
  tasks:assign                   company-wide
  tasks:assign_scope             SELF-subtree
  tasks:manage_active_checkouts  SELF-subtree
  tools:manage_connections       company-wide
  tools:manage_runtime           company-wide
  tools:view_audit               company-wide
GOLD
)"
refuses_because "access to a terminated agent reads as absent" \
  "no such agent: OLD" \
  "$PROV" access OLD
refuses_because "access to an unknown ref reads as absent" \
  "no such agent: NOPE" \
  "$PROV" access NOPE
mkdir -p "$TMP/nogrants" && cp "$FIXTURE/org.tsv" "$TMP/nogrants/org.tsv"
eq "a snapshot without grants.tsv reads as grantless, not as an error" \
  "$(env ORG_SNAPSHOT="$TMP/nogrants" "$PROV" access ENG)" \
  "$(cat <<'GOLD'
ENG  [E0_SPECIALIST]  parent=MGR
  (no governance grants)         company-wide
GOLD
)"

hdr "3. Policy refusals read byte-identical (shared emitters with live)"
out="$("$PROV" create --caller T0 --template P1_PRESIDENT_COO --title "SNAP Shadow President" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "ceiling refusal exits 2" || bad "ceiling refusal exits $rc, wanted 2"
eq "ceiling refusal text is pinned" "$out" "$(cat <<'GOLD'
  caller template : B2_TECH_CHIEF
  requested       : P1_PRESIDENT_COO
  permitted       : C1_DIRECTOR_BUILDER C2_PLATFORM_DIRECTOR D1_MANAGER D2_ORCHESTRATION_MANAGER E0_SPECIALIST E1_REVIEWER_COACH E2_TOOLING_ADMIN E3_PIPELINE_BUILDER
REFUSED: template 'P1_PRESIDENT_COO' exceeds the delegation ceiling of 'B2_TECH_CHIEF'.
GOLD
)"
grep -q '"reason":"template_above_ceiling"' "$GRANT_LOG" \
  && ok "ceiling refusal is audit-logged (create.refused)" \
  || bad "ceiling refusal left no create.refused audit row"
refuses_because "unknown caller refuses before the ceiling" \
  "caller not found in company: NOPE" \
  "$PROV" create --caller NOPE --template E0_SPECIALIST --title "SNAP Ghost"
refuses_because "caller-supplied reportsTo refuses (service-set placement)" \
  "reportsTo cannot be supplied by the caller" \
  "$PROV" create --caller T0 --template E0_SPECIALIST --title "SNAP Escapee" --reports-to O2
refuses_because "kill switch refuses before merit" \
  "kill switch is engaged" \
  env PROVISIONER_DISABLED=1 "$PROV" create --caller T0 --template E0_SPECIALIST --title "SNAP Killswitch"
refuses_because "deactivation outside the caller's subtree refuses" \
  "is not inside" \
  "$PROV" deactivate --caller MGR --target DIR
refuses_because "deactivation of an unknown target refuses" \
  "target not found" \
  "$PROV" deactivate --caller T0 --target NOPE

hdr "4. Writes refuse read-only before their first live touch"
out="$("$PROV" create --caller T0 --template C1_DIRECTOR_BUILDER --title "SNAP Would-be director" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "would-be provision exits 2, not 0" || bad "would-be provision exits $rc"
# The policy gates passed (no ceiling/caller complaint) and ONLY the
# read-only guard fired — that is what makes this the approve-path proof.
if grep -q "read-only" <<<"$out" && ! grep -q "ceiling\|not found" <<<"$out"; then
  ok "would-be provision refuses read-only after passing policy"
else bad "would-be provision refused for the wrong reason"; sed 's/^/        /' <<<"$out" | head -4; fi
refuses_because "would-be deactivation refuses at the grant write" \
  "is read-only; refusing grant write" \
  "$PROV" deactivate --caller T0 --target DIR
refuses_because "verify-transport refuses (no secrets/bindings/runs offline)" \
  "is read-only; verify-transport" \
  "$PROV" verify-transport --target 00000000-0000-4000-8000-000000000178 --smoke-run 00000000-0000-4000-8000-000000000177
eq "dir and file shapes read byte-identical rows" \
  "$(env ORG_SNAPSHOT="$FIXTURE/org.tsv" "$PROV" access DIR)" \
  "$("$PROV" access DIR)"
out="$(env ORG_SNAPSHOT="$TMP/no-such-dir" "$PROV" access DIR 2>&1)"; rc=$?
if [[ $rc -eq 1 ]] && grep -q "is neither a fixture directory nor an org.tsv file" <<<"$out"; then
  ok "a missing snapshot path fails closed with a clear error (rc=1)"
else bad "missing snapshot path misbehaves (rc=$rc)"; sed 's/^/        /' <<<"$out" | head -3; fi
out="$(env ORG_SNAPSHOT="$TMP" "$PROV" access DIR 2>&1)"; rc=$?
if [[ $rc -eq 1 ]] && grep -q "has no readable org snapshot" <<<"$out"; then
  ok "a dir without org.tsv fails closed with a clear error (rc=1)"
else bad "snapshot-less dir misbehaves (rc=$rc)"; sed 's/^/        /' <<<"$out" | head -3; fi

hdr "5. Tree serves fixture content (rows, not psql framing)"
tree_out="$("$PROV" tree)"
# [[:space:]] spans the tab-vs-column rendering gap: without util-linux
# `column` (this container) the columns are tab-separated, with it they are
# space-aligned. Either way the rows, order, and counts must match.
for want in "DIR Director of Engineering[[:space:]]+C1_DIRECTOR_BUILDER[[:space:]]+5" \
            "MGR Engineering Manager[[:space:]]+D1_MANAGER[[:space:]]+3" \
            "ENG Web Engineer[[:space:]]+E0_SPECIALIST[[:space:]]+1" \
            "T0 Technology Chief[[:space:]]+B2_TECH_CHIEF[[:space:]]+9"; do
  if grep -qE "^[[:space:]]*${want}\$" <<<"$tree_out"; then ok "tree row: ${want%%\[*}"
  else bad "tree row missing: $want"; fi
done
if ! grep -q "00000000" <<<"$tree_out" && ! grep -q "terminated\|Retired" <<<"$tree_out"; then
  ok "tree shows roles, not UUIDs, and hides terminated rows"
else bad "tree leaks UUIDs or terminated rows"; fi
dir_ln="$(grep -n "DIR Director" <<<"$tree_out" | cut -d: -f1)"
eng_ln="$(grep -n "ENG Web Engineer" <<<"$tree_out" | cut -d: -f1)"
o3_ln="$(grep -n "O3 Chief Audit" <<<"$tree_out" | cut -d: -f1)"
[[ "$dir_ln" -lt "$eng_ln" && "$eng_ln" -lt "$o3_ln" ]] \
  && ok "tree orders by lineage path (DIR < MGR < ENG < O3)" \
  || bad "tree order wrong (DIR=$dir_ln ENG=$eng_ln O3=$o3_ln)"

hdr "6. Queue matrix on the same org: approve / reject / ceiling-skip"
# The queue reads a FILE, so it runs on a mktemp COPY — the checked-in fixture
# is never written (the stub appends nothing, but the queue creates lock files
# next to QUEUE, and a future stub may append).
build_stub
qreset() { rm -f "$TMP/queue.jsonl" "$TMP/qgrant.log" "$TMP/disabled" "$CREATE_ARGV"; cp "$FIXTURE/org.tsv" "$TMP/org.tsv"; }
export QUEUE="$TMP/queue.jsonl" DISABLED_TEMPLATES="$TMP/disabled"
export ORG_SNAPSHOT="$TMP/org.tsv"

# --- ceiling refusal at submit: stub PROV vs real PROV must agree -----------
qreset; export GRANT_LOG="$TMP/qgrant.log"
a_out="$(PROV="$HERE/org_provisioner.sh" "$Q" submit --requester T0 --template P1_PRESIDENT_COO --title "SNAP Shadow President" 2>&1)"; a_rc=$?
qreset
b_out="$(PROV="$TMP/stub_provisioner.sh" "$Q" submit --requester T0 --template P1_PRESIDENT_COO --title "SNAP Shadow President" 2>&1)"; b_rc=$?
if [[ "$a_rc" -ne 0 && "$a_rc" == "$b_rc" && "$a_out" == "$b_out" ]] && grep -q "above the request ceiling" <<<"$a_out"; then
  ok "submit ceiling refusal is identical under real and stub PROV (rc=$a_rc)"
else bad "submit ceiling verdict differs by PROV"; note "real rc=$a_rc stub rc=$b_rc"; fi

# --- review authority refusal: a chief cannot review --------------------------
qreset
must_allow "MGR submits a specialist request" \
  "$Q" submit --requester MGR --template E0_SPECIALIST --title "SNAP Reviewable"
REQ_MGR="$(jq -r 'select(.event=="request.submitted")|.requestId' "$TMP/queue.jsonl" | tail -1)"
real_out="$(PROV="$HERE/org_provisioner.sh" "$Q" review --reviewer T0 --request "$REQ_MGR" --approve --reason "reason supplied so this asserts authority, not arity" 2>&1)"; real_rc=$?
stub_out="$(PROV="$TMP/stub_provisioner.sh" "$Q" review --reviewer T0 --request "$REQ_MGR" --approve --reason "reason supplied so this asserts authority, not arity" 2>&1)"; stub_rc=$?
if [[ "$real_rc" -ne 0 && "$real_rc" == "$stub_rc" && "$real_out" == "$stub_out" ]] \
  && grep -q "is not the responsible leader for" <<<"$real_out"; then
  ok "review authority refusal is identical under real and stub PROV (rc=$real_rc)"
else bad "review authority verdict differs by PROV"; note "real rc=$real_rc stub rc=$stub_rc"; fi

# --- ceiling-skip derivation ---------------------------------------------------
qreset
who_out="$("$Q" who --requester ENG --template D1_MANAGER 2>/dev/null)"
[[ "$(cut -f1 <<<"$who_out")" == "leader" && "$(cut -f2 <<<"$who_out")" == "DIR" ]] \
  && ok "ENG requesting a manager derives DIR (MGR skipped)" \
  || bad "wrong derivation: $who_out"
grep -q "ceiling_insufficient" <<<"$who_out" \
  && ok "the skip reason names the ceiling, not dormancy or termination" \
  || bad "skip reason missing: $who_out"

# --- approve execution: stub says APPROVED; real refuses read-only ------------
qreset; PROV="$TMP/stub_provisioner.sh"
must_allow "MGR submits an approvable specialist request" \
  "$Q" submit --requester MGR --template E0_SPECIALIST --title "SNAP Approvable"
REQ_OK="$(jq -r 'select(.event=="request.submitted")|.requestId' "$TMP/queue.jsonl" | tail -1)"
rev_out="$("$Q" review --reviewer DIR --request "$REQ_OK" --approve --reason "headcount plan signed off" 2>&1)"; rev_rc=$?
if [[ $rev_rc -eq 0 ]] && grep -q "APPROVED $REQ_OK" <<<"$rev_out"; then
  ok "stub execution approves the fixture request (approve case covered)"
else bad "stub execution did not approve (rc=$rev_rc REQ=$REQ_OK)"; sed 's/^/        /' <<<"$rev_out" | head -6; fi
if grep -q -- "--caller MGR" "$CREATE_ARGV" 2>/dev/null && grep -q -- "--template E0_SPECIALIST" "$CREATE_ARGV" 2>/dev/null; then
  ok "stub recorded the original requester as caller with the exact template"
else bad "stub argv wrong: $(cat "$CREATE_ARGV" 2>/dev/null)"; fi
qreset; PROV="$HERE/org_provisioner.sh"
must_allow "MGR submits the same request under the real provisioner" \
  "$Q" submit --requester MGR --template E0_SPECIALIST --title "SNAP Approvable"
REQ_OK2="$(jq -r 'select(.event=="request.submitted")|.requestId' "$TMP/queue.jsonl" | tail -1)"
div_out="$("$Q" review --reviewer DIR --request "$REQ_OK2" --approve --reason "headcount plan signed off" 2>&1)"; div_rc=$?
if [[ $div_rc -ne 0 ]] && grep -q "read-only" <<<"$div_out" && grep -q "provisioner rejected the approved request" <<<"$div_out"; then
  ok "real execution diverges ONLY at the write (read-only), as designed"
else bad "real execution diverged somewhere other than the write"; sed 's/^/        /' <<<"$div_out" | head -5; fi

hdr "7. The checked-in fixture was not written"
eq "fixture checksums unchanged" "$(fixture_sha)" "$SHA_BEFORE"

printf '\n\033[1mRESULT: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
