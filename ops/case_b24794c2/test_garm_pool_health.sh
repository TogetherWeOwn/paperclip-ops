#!/usr/bin/env bash
# test_garm_pool_health.sh — offline suite for the case_b24794c2 GARM pool-health
# module (strict per-pool evaluator + endpoint renderer).
#
# Hermetic: synthetic fixtures only, no network, no credentials, no garm-cli,
# no Gatus. Gates on exit status, never on prose or counts.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL="$HERE/garm_pool_health.sh"
RENDER="$HERE/render-garm-pool-endpoints.sh"
FIX="$HERE/fixtures"
EXAMPLE="$HERE/examples/garm-pool-endpoints.example.yaml"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1 -- $2"; }

# expect <rc-want> <out-needle> <name> -- <command...>
expect() {
  local want="$1" needle="$2" name="$3"; shift 3
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if (( rc == want )) && grep -q "$needle" <<<"$out"; then ok "$name"
  else bad "$name" "rc=$rc want=$want needle='$needle' out='$out'"; fi
}

WORK="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/gph_test.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# === 1. healthy payloads report green ======================================
expect 0 "HEALTHY pool=aa101234" "1. healthy pool green" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/healthy.json"
expect 0 "HEALTHY pool=bb202345" "1b. all-busy pool green (no demand, warm floor 0)" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/busy-ok.json"
# stdin path (no --pool-json) reads the same payload. expect captures first
# and checks the producer's exit status before matching a here-string.
expect 0 "HEALTHY pool=aa101234" "1c. stdin payload green" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" < "$FIX/healthy.json"
# runner buckets ride the verdict line: 1 idle + 1 busy on the healthy pool.
expect 0 "idle=1 busy=1" "1d. verdict carries idle/busy counts" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/healthy.json"

# === 2. missing fields fail closed (the omitempty lesson) ===================
expect 1 "min_idle_runners.*fail closed" "2. absent min_idle_runners UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/missing-min-idle.json"
expect 1 "'instances' missing (fail closed)" "2b. absent instances UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/missing-instances.json"
expect 1 "min_idle_runners.*fail closed" "2c. null min_idle_runners UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/null-min-idle.json"
expect 1 "fail closed" "2d. live omitempty shape UNHEALTHY under strict semantics" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/live-omitempty.json"
expect 1 "disabled" "2e. disabled pool UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/disabled.json"
expect 1 "over max" "2f. over-max instances UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/over-max.json"

# Mis-typed fields fail closed: a quoted string must not read as the typed
# value. An earlier `| tostring` probe erased the JSON type and passed all
# three of these as HEALTHY.
expect 1 "'enabled' missing or not boolean" "2h. string-typed enabled UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/string-enabled.json"
expect 1 "min_idle_runners.*fail closed" "2i. string-typed min_idle_runners UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/string-min-idle.json"
expect 1 "max_runners.*fail closed" "2j. string-typed max_runners UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/string-max.json"
# An instance entry that is null or lacks a string status cannot be bucketed.
expect 1 "instance entry lacks a string 'status'" "2k. null instance entry UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/null-instance.json"
expect 1 "instance entry lacks a string 'status'" "2l. empty-object instance entry UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/empty-instance.json"

# Inline shapes: fractional, negative, boolean-where-number, non-array
# instances and a non-boolean enabled all stay red; a whole-valued float is a
# whole number. Payloads are derived from one healthy base with jq.
base='{"id":"00000000-0000-4000-8000-000000000076","enabled":true,"max_runners":2,"min_idle_runners":0,"instances":[]}'
inline_rc() { # <key> <json-value> -> evaluator exit code for base with key=value
  jq -c --argjson v "$2" ".$1 = \$v" <<<"$base" \
    | env -i PATH="/usr/bin:/bin" bash "$EVAL" >/dev/null 2>&1
  echo $?
}
shapes_ok=1
for spec in min_idle_runners=1.5 min_idle_runners=-1 min_idle_runners=true \
            max_runners=2.5 max_runners=false instances='{}' enabled=1 enabled=null; do
  rc="$(inline_rc "${spec%%=*}" "${spec#*=}")"
  (( rc == 1 )) || { shapes_ok=0; echo "  inline shape not UNHEALTHY (rc=$rc): $spec"; }
done
if (( shapes_ok == 1 )); then ok "2m. fractional/negative/boolean/non-array/non-boolean-enabled shapes all UNHEALTHY"
else bad "2m. inline mis-typed shapes" "a mis-typed shape was not UNHEALTHY"; fi
if (( $(inline_rc max_runners 2.0) == 0 )); then ok "2n. whole-valued float (2.0) accepted as a whole number"
else bad "2n. whole float" "2.0 refused"; fi

# NON-VACUITY mutants. Each copy of the evaluator carries one defect and must
# turn its assertion red; every substitution must match exactly once.
#   lenient.sh  — the aggregate push's absent-means-zero fallback.
#   tostring.sh — the pre-fix probes that erased the JSON type.
if ! python3 - "$EVAL" "$WORK" <<'PYEOF'
import re, sys
src = open(sys.argv[1]).read()
work = sys.argv[2]

anchor = '[[ "$min_idle_raw" =~ ^[0-9]+$ ]] \\\n'
assert src.count(anchor) == 1, "anchor for strict min_idle check not found"
lenient = src.replace(anchor, '[[ "$min_idle_raw" == "invalid" ]] && min_idle_raw="0"\n' + anchor)
open(work + "/lenient.sh", "w").write(lenient)

old = {
    "enabled": r'''enabled="$(jq -r '.enabled | tostring' <<<"$payload" 2>/dev/null)"''' + " \\",
    "min_idle_raw": r'''min_idle_raw="$(jq -r '.min_idle_runners | tostring' <<<"$payload" 2>/dev/null)"''',
    "max_raw": r'''max_raw="$(jq -r '.max_runners | tostring' <<<"$payload" 2>/dev/null)"''',
}
tostr = src
for var, line in old.items():
    pat = re.compile(r"^" + var + r"=.*$", re.M)
    assert len(pat.findall(tostr)) == 1, "probe line for " + var + " not found exactly once"
    tostr = pat.sub(lambda m, line=line: line, tostr, count=1)
open(work + "/tostring.sh", "w").write(tostr)
PYEOF
then
  bad "2g. mutants" "could not build mutant copies (anchor moved?)"
else
  # The relaxed copy must actually succeed, not merely lack the refusal text.
  expect 0 "HEALTHY pool=" "2g. leniency mutant goes red (fail-closed assertion is non-vacuous)" \
    env -i PATH="/usr/bin:/bin" bash "$WORK/lenient.sh" --pool-json "$FIX/missing-min-idle.json"
  # The pre-fix probe must wrongly pass every string-typed fixture.
  ts_green=0
  for f in string-enabled string-min-idle string-max; do
    env -i PATH="/usr/bin:/bin" bash "$WORK/tostring.sh" --pool-json "$FIX/$f.json" >/dev/null 2>&1 && ts_green=$((ts_green + 1))
  done
  if (( ts_green == 3 )); then ok "2o. tostring mutant reads all 3 string fixtures HEALTHY (2h-2j are non-vacuous)"
  else bad "2o. tostring mutant" "only $ts_green/3 string fixtures went HEALTHY under the pre-fix probe"; fi
fi

# === 3. demand-coupled rules ================================================
# Broken VMs with NO waiting demand stay info (GARM self-heals idle breakage).
expect 0 "HEALTHY.*prov_fail=1" "3. prov-fail without demand is info, stays green" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/prov-fail.json"
# Same breakage WITH demand waiting is the stall signature: red.
expect 1 "prov-fail instances" "3b. prov-fail with demand UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/prov-fail.json" --waiting
# Warm floor unmet with demand waiting and nothing provisioning: red.
expect 1 "idle 0 < min-idle 2" "3c. warm gap with demand UNHEALTHY" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/warm-gap.json" --waiting
# Same gap with no demand: green (nothing to serve, nothing broken).
expect 0 "HEALTHY pool=cd909012" "3d. warm gap without demand green" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/warm-gap.json"

# === 4. refusals (exit 2, never a verdict) ===================================
expect 2 "not a JSON object" "4. garbage input refused exit 2" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/garbage.txt"
expect 2 "unknown argument" "4b. unknown flag refused exit 2" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --bogus
expect 2 "cannot read" "4c. missing file refused exit 2" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/no-such.json"
expect 2 "bad label" "4d. bad label refused exit 2" \
  env -i PATH="/usr/bin:/bin" bash "$EVAL" --pool-json "$FIX/healthy.json" --label 'a b'

# === 5. renderer: shape, refusal, regen-sync ================================
rendered="$(GARM_POOLS="aa101234-1111-4222-8333-444444444444 bb202345-1111-4222-8333-444444444444" GARM_ELIGIBLE_LABEL=two-ephemeral bash "$RENDER")"
if grep -q "name: garm-pool-aa101234" <<<"$rendered" \
  && grep -q "name: garm-pool-bb202345" <<<"$rendered" \
  && grep -q "name: garm-runners-two-ephemeral" <<<"$rendered" \
  && grep -q "interval: 10m" <<<"$rendered"; then
  ok "5. renderer emits per-pool + per-label blocks with 10m heartbeat"
else bad "5. renderer blocks" "per-pool or per-label block missing"; fi
if GARM_POOLS="" bash "$RENDER" >/dev/null 2>&1; then
  bad "5b. renderer empty pools" "empty GARM_POOLS rendered instead of refusing"
else (( $? == 2 )) && ok "5b. renderer refuses empty GARM_POOLS exit 2" || bad "5b. renderer empty pools" "wrong exit";
fi
if GARM_POOLS="bad id!" bash "$RENDER" >/dev/null 2>&1; then
  bad "5c. renderer bad pool id" "bad pool ID rendered instead of refusing"
else (( $? == 2 )) && ok "5c. renderer refuses bad pool ID exit 2" || bad "5c. renderer bad pool id" "wrong exit";
fi
# Regen-sync: the checked-in example is byte-identical to renderer output for
# the documented fake pools — the example cannot silently drift from the tool.
if diff <(GARM_POOLS="aa101234-1111-4222-8333-444444444444 bb202345-1111-4222-8333-444444444444" GARM_ELIGIBLE_LABEL=two-ephemeral bash "$RENDER") "$EXAMPLE" >/dev/null; then
  ok "5d. checked-in example byte-identical to renderer output"
else bad "5d. example sync" "example drifted from renderer — re-generate it"; fi
# Mutant: an example with a pool block deleted must fail the same diff.
if grep -v "name: garm-pool-bb202345" "$EXAMPLE" | diff - <(GARM_POOLS="aa101234-1111-4222-8333-444444444444 bb202345-1111-4222-8333-444444444444" GARM_ELIGIBLE_LABEL=two-ephemeral bash "$RENDER") >/dev/null; then
  bad "5e. example-sync mutant" "block-deleted example still matches — check vacuous?"
else ok "5e. example-sync mutant goes red"; fi

# === 6. credential hygiene: this module mints, stores and sends nothing ======
# The push side (operator cron + _gatus_push.sh) owns the Bearer. These two
# scripts must never touch it: no curl, no token env reads.
# Match the bare names (so `$GATUS_EXTERNAL_TOKEN` and `${GATUS_URL}` both hit)
# and drop only whole-line comments, never a code line that carries a `#`.
cred_hits() { grep -nE 'curl|GATUS_EXTERNAL_TOKEN|GATUS_URL' "$1" | grep -vE '^[0-9]+:[[:space:]]*#'; }
if cred_hits "$EVAL" >/dev/null; then
  bad "6. evaluator credential surface" "$(cred_hits "$EVAL" | head -3)"
else ok "6. evaluator touches no credential, makes no network call"; fi
# Mutant: a copy that echoes the token with a trailing comment (the shape the
# old `grep -v '#'` filter dropped) must trip the same scan.
{ cat "$EVAL"; printf '%s\n' 'echo "$GATUS_EXTERNAL_TOKEN" # leak'; } > "$WORK/leaky.sh"
if cred_hits "$WORK/leaky.sh" >/dev/null; then ok "6d. credential scan catches an appended token echo"
else bad "6d. credential scan" "appended \$GATUS_EXTERNAL_TOKEN echo not detected"; fi
if grep -nE '^\s*curl|Bearer \\$\{|GATUS_URL:-' "$RENDER" >/dev/null; then
  bad "6b. renderer credential surface" "renderer handles a live credential"
else ok "6b. renderer emits only a \${ENV} token placeholder"; fi
# Positive control: the hygiene grep fires on a planted credential line.
if grep -qE 'Bearer [A-Za-z0-9]{4,}' <<<'header = "Authorization: Bearer LIVESECRET"'; then
  ok "6c. credential-scan pattern fires on a planted Bearer"
else bad "6c. credential-scan pattern" "pattern missed a planted Bearer"; fi

echo "---"
echo "pass=$pass fail=$fail"
(( fail == 0 ))
