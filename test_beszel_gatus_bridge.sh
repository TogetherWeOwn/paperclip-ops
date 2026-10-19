#!/usr/bin/env bash
# test_beszel_gatus_bridge.sh — offline suite for the case_f2010cb7 Beszel bridge.
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY pushes true — the healthy fixture exits 0 with success=true
#      per host. Without this, a suite that only asserts failures could pass
#      against a script that fails everything.
#   2. BREACH pushes false naming the cause — disk breach + failed units on
#      node7b1ff338, cpu + restarts on ovh-1; exit 1.
#   3. MISSING FIELD fails closed — the missing-field fixture pushes false
#      naming the absent field, never healthy. The positive control is §1:
#      the same shape with the field present exits 0.
#   4. FORECAST fires — fast growth with days-to-full under threshold pushes
#      false naming the ETA; slow growth (§1 node7b1ff338) stays healthy.
#   5. SILENT HOST fails closed — an enrolled host absent from the payload
#      pushes false; full enrollment (§1 hosts) exits 0.
#   6. REFUSALS — missing GATUS_URL, empty host list, non-JSON payload each
#      exit 2 with zero pushes.
#   7. NON-DISCLOSURE — Bearer tokens never reach curl argv; they travel in
#      the 0600 curl config only.
#   8. ROUTING config — beszel-discord-routing.json parses, reuses only the
#      existing webhook env, and carries no secret-shaped string. A planted
#      webhook trips the same grep (positive control).
#   9. EMIT-YAML — --emit-gatus-yaml names every payload host with the
#      shared-token heartbeat stanza.
#  10. NOT VACUOUS — a mutant with the fail-closed gate neutered scores the
#      missing-field fixture healthy, proving §3 asserts something.
#  11. OWNER+RUNBOOK (case_7ec9e924) — every --emit-gatus-yaml description names
#      OWNER devops and the garm-ci-oncall runbook; every routing route plus
#      the default carries a non-empty owner + runbook. The positive control
#      is §9: emit still names every host with the shared-token stanza.
#  13. OPTIONAL FIELDS (adapter provenance) — a healthy payload WITHOUT
#      inode_used_percent/container_restarts_24h still pushes true (Beszel
#      records neither; an adapter-fed payload can never carry them), while
#      a present-but-non-numeric value fails closed, and the adapter's own
#      output over fixtures/native pushes the up host true and the down
#      host false. §1/§2 are the positive controls that present values
#      still score (inode/restarts present there; restarts breach names it).
#  12. HOSTILE HOSTNAMES (case_6d874ecd follow-up) — names with punctuation or
#      non-ASCII letters (`Hetzner (main)`, `db:1`, `[edge]`, `*alias`,
#      `Ünïcode-1`) still push true under the gatus_push charset and emit
#      plain-YAML-safe names. Paperclip Review on PR #809 caught the
#      narrower sanitizer refusing all five (exit 1).
#
# Hermetic: no network, no credentials. A stub curl records argv + config
# contents and answers 200; BESZEL payloads come from fixture files.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ops/case_f2010cb7/push-beszel-hosts.sh"
ROUTING="$HERE/ops/case_f2010cb7/beszel-discord-routing.json"
FIX="$HERE/ops/case_f2010cb7/fixtures"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_beszel_bridge.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" CFG_LOG="$WORK/cfg.log"
: > "$ARGV_LOG"; : > "$CFG_LOG"

# Stub curl: records argv and every --config file's contents, answers 200.
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$CFG_LOG"; fi
done
printf '200'
exit 0
STUB
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"

run_tool() { # env-assignment... -- fixture [extra args]; echoes exit code
  reset_logs
  local rc
  set +e
  "$@" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
reset_logs() { : > "$ARGV_LOG"; : > "$CFG_LOG"; : > "$WORK/out.txt"; : > "$WORK/err.txt"; }
pushes_for() { # <key> prints success= values pushed for that endpoint key
  grep -o "endpoints/$1/external?success=[a-z]*" "$CFG_LOG" | sed 's/.*success=//'
}
cfg_has() { # <pattern> greps the URL-decoded curl configs (reasons are encoded)
  local decoded
  decoded="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open("'"$CFG_LOG"'").read()))')"
  grep -q "$1" <<<"$decoded"
}
push_count() { grep -c "endpoints/" "$CFG_LOG" || true; }
err_has() { grep -q "$1" "$WORK/out.txt" "$WORK/err.txt"; }

export GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-token-not-a-secret"

# --- §1 healthy -------------------------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/healthy.json")"
[[ "$rc" == "0" ]] && ok "1. healthy fixture exits 0" || bad "1. healthy exit" "got $rc"
[[ "$(pushes_for beszel_node7b1ff338)" == "true" ]] && ok "1b. node7b1ff338 pushed true" || bad "1b. node7b1ff338 push" "$(cat "$CFG_LOG")"
[[ "$(pushes_for beszel_ovh-1)" == "true" ]] && ok "1c. ovh-1 pushed true" || bad "1c. ovh-1 push" "$(cat "$CFG_LOG")"

# --- §2 breach --------------------------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/breach.json")"
[[ "$rc" == "1" ]] && ok "2. breach fixture exits 1" || bad "2. breach exit" "got $rc"
[[ "$(pushes_for beszel_node7b1ff338)" == "false" ]] && ok "2b. node7b1ff338 pushed false" || bad "2b. node7b1ff338 push" "$(cat "$CFG_LOG")"
cfg_has "disk" && ok "2c. reason names disk" || bad "2c. disk named" "$(cat "$CFG_LOG")"
cfg_has "cron.service" && ok "2d. reason names failed units" || bad "2d. units named" "$(cat "$CFG_LOG")"
[[ "$(pushes_for beszel_ovh-1)" == "false" ]] && ok "2e. ovh-1 pushed false" || bad "2e. ovh-1 push" "$(cat "$CFG_LOG")"
cfg_has "restarts" && ok "2f. reason names restarts" || bad "2f. restarts named" "$(cat "$CFG_LOG")"

# --- §3 missing field fails closed ------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/missing-field.json")"
[[ "$rc" == "1" ]] && ok "3. missing-field fixture exits 1" || bad "3. missing-field exit" "got $rc"
[[ "$(pushes_for beszel_node7b1ff338)" == "false" ]] && ok "3b. pushed false, never healthy" || bad "3b. fail-closed" "$(cat "$CFG_LOG")"
cfg_has "failed_units" && ok "3c. reason names the absent field" || bad "3c. field named" "$(cat "$CFG_LOG")"

# --- §4 forecast ------------------------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/forecast-critical.json")"
[[ "$rc" == "1" ]] && ok "4. forecast-critical exits 1" || bad "4. forecast exit" "got $rc"
cfg_has "full in" && ok "4b. reason carries the ETA" || bad "4b. ETA named" "$(cat "$CFG_LOG")"

# --- §5 silent host ---------------------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" BESZEL_HOSTS="node7b1ff338 ghost" "$TOOL" "$FIX/healthy.json")"
[[ "$rc" == "1" ]] && ok "5. silent enrolled host exits 1" || bad "5. silent exit" "got $rc"
[[ "$(pushes_for beszel_ghost)" == "false" ]] && ok "5b. ghost pushed false as silent" || bad "5b. ghost push" "$(cat "$CFG_LOG")"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" BESZEL_HOSTS="node7b1ff338 ovh-1" "$TOOL" "$FIX/healthy.json")"
[[ "$rc" == "0" ]] && ok "5c. full enrollment exits 0 (positive control)" || bad "5c. enrollment control" "got $rc"

# --- §6 refusals, zero pushes -----------------------------------------------
reset_logs
set +e; (unset GATUS_URL; GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/healthy.json" >/dev/null 2>&1); rc=$?; set -e
[[ "$rc" == "2" ]] && ok "6. missing GATUS_URL refuses (exit 2)" || bad "6. GATUS_URL refusal" "got $rc"
[[ "$(push_count)" == "0" ]] && ok "6b. refusal makes zero pushes" || bad "6b. zero pushes" "$(cat "$CFG_LOG")"
echo '[]' > "$WORK/empty.json"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/empty.json")"
[[ "$rc" == "2" ]] && ok "6c. empty host list refuses" || bad "6c. empty refusal" "got $rc"
echo 'not json{{{' > "$WORK/bad.json"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/bad.json")"
[[ "$rc" == "2" ]] && ok "6d. non-JSON refuses" || bad "6d. non-JSON refusal" "got $rc"

# --- §7 non-disclosure ------------------------------------------------------
reset_logs
set +e; GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$FIX/healthy.json" >/dev/null 2>&1; set -e
if grep -q "Bearer" "$ARGV_LOG"; then bad "7. token in argv" "$(cat "$ARGV_LOG")"; else ok "7. no Bearer in curl argv"; fi
grep -q "Authorization: Bearer" "$CFG_LOG" && ok "7b. token travels in curl config" || bad "7b. config transport" "$(cat "$CFG_LOG")"
reset_logs
# Drive the curl-fetch branch (stub answers 200 with a non-JSON body, so the
# run refuses AFTER the fetch — argv and config are still recorded).
set +e; BESZEL_API_KEY="fetch-key" BESZEL_API_URL="http://127.0.0.1:9/systems" GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" >/dev/null 2>&1; set -e
if grep -q "fetch-key" "$ARGV_LOG"; then bad "7c. Beszel key in argv" "$(cat "$ARGV_LOG")"; else ok "7c. no Beszel key on argv"; fi
if grep -q "Authorization: Bearer" "$CFG_LOG"; then ok "7d. Beszel key travels in curl config"; else bad "7d. fetch transport" "$(cat "$CFG_LOG")"; fi
# The fetch branch holds the Beszel Bearer in a 0600 temp dir: it must not
# survive the run (case_1937450c: trap RETURN is a no-op at top level).
if ls -d "${TMPDIR:-/tmp}"/beszel_fetch.* >/dev/null 2>&1; then bad "7e. fetch temp dir leaks Bearer config" "$(ls -d "${TMPDIR:-/tmp}"/beszel_fetch.*)"; else ok "7e. fetch temp dir removed"; fi

# --- §8 routing config ------------------------------------------------------
python3 -c 'import json; json.load(open("'"$ROUTING"'"))' \
  && ok "8. routing JSON parses" || bad "8. routing parses" ""
[[ "$(jq -r '.webhook_env' "$ROUTING")" == "DISCORD_WEBHOOK_URL" ]] \
  && ok "8b. reuses the existing webhook env" || bad "8b. webhook env" ""
if grep -qE 'discord\.com/api/webhooks|discord://[^$"]*@' "$ROUTING"; then
  bad "8c. live secret in routing" "webhook-shaped string committed"
else ok "8c. no secret-shaped strings in routing"; fi
printf 'x discord.com/api/webhooks/123/abc\n' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks' "$WORK/planted.txt"; then ok "8d. secret-scan pattern fires (positive control)"; else bad "8d. scan pattern" "pattern missed a planted webhook"; fi

# --- §9 emit-yaml -----------------------------------------------------------
EMIT="$(GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" --emit-gatus-yaml "$FIX/healthy.json")"
{ grep -q "name: node7b1ff338" <<<"$EMIT" && grep -q "name: ovh-1" <<<"$EMIT"; } \
  && ok "9. emit names every payload host" || bad "9. emit hosts" "$EMIT"
grep -q 'token: "${GATUS_EXTERNAL_TOKEN}"' <<<"$EMIT" \
  && ok "9b. emit uses the shared-token indirection" || bad "9b. emit token" "$EMIT"
{ grep -q "type: discord" <<<"$EMIT" && grep -q "type: custom" <<<"$EMIT"; } \
  && ok "9c. emit carries discord+akmatori alerts" || bad "9c. emit alerts" "$EMIT"

# --- §10 mutation gate ------------------------------------------------------
cp "$TOOL" "$WORK/mutant.sh"
# Neuter the fail-closed gate: a missing field no longer records anything.
# The copy resolves its helper relatively, so repoint it at the real one.
python3 - "$WORK/mutant.sh" "$HERE/ops/case_658bf6b9/_gatus_push.sh" <<'PY'
import sys
p, helper = sys.argv[1], sys.argv[2]
s = open(p).read()
old = '|| missing+=("$f")'
assert old in s, "anchor for the fail-closed gate moved — update this gate"
s = s.replace(old, '|| true  # MUTANT: fail-closed gate neutered')
s = s.replace('GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"',
              f'GATUS_HELPER="{helper}"')
open(p, "w").write(s)
PY
chmod +x "$WORK/mutant.sh"
set +e
GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$WORK/mutant.sh" "$FIX/missing-field.json" >/dev/null 2>&1; mrc=$?
set -e
if [[ "$mrc" == "0" ]]; then ok "10. mutant scores missing-field healthy (gate is load-bearing)"; else bad "10. mutation gate" "neutered script still exits $mrc — §3 asserts nothing"; fi

# --- §11 owner+runbook wiring (case_7ec9e924) ------------------------------------
{ grep -q "OWNER devops" <<<"$EMIT" && grep -q "RUNBOOK docs/runbooks/garm-ci-oncall-alert-to-action.md" <<<"$EMIT"; } \
  && ok "11. emit descriptions carry OWNER + RUNBOOK" || bad "11. emit owner/runbook" "$EMIT"
{ grep -q "THRESHOLD" <<<"$EMIT"; } \
  && ok "11b. emit descriptions carry the threshold table" || bad "11b. emit thresholds" "$EMIT"
OWNERS_OK="$(jq -r '([.routes[] | select(.owner == null or .owner == "")] | length == 0) and (.default_route.owner != null and .default_route.owner != "")' "$ROUTING")"
[[ "$OWNERS_OK" == "true" ]] \
  && ok "11c. every route + default names an owner" || bad "11c. route owners" "$(jq -c '.routes, .default_route' "$ROUTING")"
BOOKS_OK="$(jq -r '([.routes[] | select(.runbook == null or .runbook == "")] | length == 0) and (.default_route.runbook != null and .default_route.runbook != "")' "$ROUTING")"
[[ "$BOOKS_OK" == "true" ]] \
  && ok "11d. every route + default links a runbook" || bad "11d. route runbooks" "$(jq -c '.routes, .default_route' "$ROUTING")"
[[ "$(jq -r '.webhook_env' "$ROUTING")" == "DISCORD_WEBHOOK_URL" ]] \
  && ok "11e. still no new credential (existing webhook indirection)" || bad "11e. webhook env" ""
if grep -qE 'discord\.com/api/webhooks|discord://[^$"]*@' "$ROUTING"; then
  bad "11f. live secret in routing" "webhook-shaped string committed"
else ok "11f. no secret-shaped strings in routing"; fi

# --- §12 hostile hostnames stay pushable (case_6d874ecd follow-up) ---------------
python3 - "$FIX/healthy.json" "$WORK/hostile.json" <<'PY'
import json, sys
template = json.load(open(sys.argv[1]))[0]
names = ["node7b1ff338", "Hetzner (main)", "db:1", "[edge]", "*alias", "Ünïcode-1"]
json.dump([dict(template, name=n) for n in names],
          open(sys.argv[2], "w", encoding="utf-8"))
PY
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/hostile.json")"
[[ "$rc" == "0" ]] && ok "12. hostile hostnames exit 0" || bad "12. hostile exit" "got $rc"
for key in beszel_node7b1ff338 beszel_hetzner--main beszel_db-1 beszel_edge beszel_alias; do
  [[ "$(pushes_for "$key")" == "true" ]] && ok "12b. $key pushed true" || bad "12b. $key push" "$(cat "$CFG_LOG")"
done
[[ "$(push_count)" == "6" ]] && ok "12c. all six hosts pushed" || bad "12c. push count" "$(cat "$CFG_LOG")"
BADKEYS="$(grep -o 'endpoints/[^/]*/external' "$CFG_LOG" | grep -vE 'endpoints/[A-Za-z0-9][A-Za-z0-9_-]*/external' || true)"
[[ -z "$BADKEYS" ]] && ok "12d. every pushed key satisfies the gatus_push charset" || bad "12d. key charset" "$BADKEYS"
EMIT12="$(GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" --emit-gatus-yaml "$WORK/hostile.json")"
[[ "$(grep -cE '^  - name: [a-z0-9-]+$' <<<"$EMIT12")" == "6" ]] \
  && ok "12e. emit names are plain-YAML-safe" || bad "12e. emit names" "$EMIT12"

# --- §13 optional fields + adapter end-to-end --------------------------------
jq 'map(del(.inode_used_percent, .container_restarts_24h))' "$FIX/healthy.json" > "$WORK/no-optionals.json"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/no-optionals.json")"
[[ "$rc" == "0" ]] && ok "13. healthy without optional fields exits 0" || bad "13. no-optionals exit" "got $rc: $(cat "$WORK/err.txt")"
[[ "$(pushes_for beszel_node7b1ff338)" == "true" ]] && ok "13b. pushed true without optionals" || bad "13b. no-optionals push" "$(cat "$CFG_LOG")"
jq 'map(.inode_used_percent = "many")' "$WORK/no-optionals.json" > "$WORK/bad-optional.json"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/bad-optional.json")"
[[ "$rc" == "1" && "$(pushes_for beszel_node7b1ff338)" == "false" ]] \
  && ok "13c. non-numeric optional fails closed" || bad "13c. bad-optional" "rc=$rc $(cat "$CFG_LOG")"
if python3 "$HERE/ops/case_f2010cb7/beszel_adapter.py" --now 1792238400 "$FIX/native" > "$WORK/normalized.json" 2>"$WORK/adapter-err.txt"; then
  rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" "$TOOL" "$WORK/normalized.json")"
  [[ "$rc" == "1" ]] && ok "13d. adapter output exits 1 (one host down)" || bad "13d. adapter-output exit" "got $rc"
  [[ "$(pushes_for beszel_node7b1ff338)" == "true" ]] && ok "13e. adapter up host pushed true" || bad "13e. adapter up push" "$(cat "$CFG_LOG")"
  [[ "$(pushes_for beszel_ovh-1)" == "false" ]] && ok "13f. adapter down host pushed false" || bad "13f. adapter down push" "$(cat "$CFG_LOG")"
else
  bad "13d. adapter ran" "$(cat "$WORK/adapter-err.txt")"; bad "13e. adapter up push" "adapter failed"; bad "13f. adapter down push" "adapter failed"
fi

printf '\nbeszel bridge: %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
