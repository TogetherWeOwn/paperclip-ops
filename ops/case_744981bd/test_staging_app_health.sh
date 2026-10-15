#!/usr/bin/env bash
# test_staging_app_health.sh — offline suite for the case_744981bd staging-app
# health module (Gatus endpoint renderer).
#
# Hermetic: no network, no credentials, no Coolify, no Gatus. The renderer
# emits ${VAR} references only; the suite renders with fake .invalid URLs
# and asserts on shape, thresholds, refusal and regen-sync. Gates on exit
# status, never on prose.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER="$HERE/render-staging-endpoints.sh"
EXAMPLE="$HERE/examples/staging-endpoints.example.yaml"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1 -- $2"; }

FULL_ENV=(env TWO_WEB_NEXT_STAGING_HEALTH_URL="https://health.invalid/two-web-next-staging"
  TWO_BOT_NEXT_STAGING_HEALTH_URL="https://health.invalid/two-bot-next-staging"
  TWO_STAGING_DASHBOARD_HEALTH_URL="https://health.invalid/staging-dashboard"
  TWO_STAGING_REDIRECT_HEALTH_URL="https://health.invalid/staging-redirect"
  STAGING_DB_TARGETS="staging-postgres=tcp://db.invalid:5432 staging-redis=tcp://db.invalid:6379")
rendered="$("${FULL_ENV[@]}" bash "$RENDER")"

# === 1. renderer emits 4 HTTP + 2 TCP + 4 restart blocks ====================
if grep -q "name: staging-two-web-next" <<<"$rendered" \
  && grep -q "name: staging-two-bot-next" <<<"$rendered" \
  && grep -q "name: staging-dashboard" <<<"$rendered" \
  && grep -q "name: staging-redirect" <<<"$rendered" \
  && grep -q "name: staging-db-staging-postgres" <<<"$rendered" \
  && grep -q "name: staging-db-staging-redis" <<<"$rendered" \
  && grep -q "name: staging-restart-two-web-next" <<<"$rendered" \
  && grep -q "name: staging-restart-redirect" <<<"$rendered"; then
  ok "1. renderer emits 4 HTTP + 2 TCP + 4 restart blocks"
else bad "1. renderer blocks" "an app, DB or restart block is missing"; fi
# Thresholds ride in the output where the operator will read them.
if grep -q 'interval: 300s' <<<"$rendered" \
  && grep -q '\[STATUS\] == 200' <<<"$rendered" \
  && grep -q '\[RESPONSE_TIME\] < 8000' <<<"$rendered" \
  && grep -q '\[CONNECTED\] == true' <<<"$rendered" \
  && grep -q 'interval: 30m' <<<"$rendered"; then
  ok "1b. renderer carries HTTP/TCP/heartbeat staging thresholds"
else bad "1b. renderer thresholds" "a threshold condition is missing from the output"; fi
# URLs and tokens stay ${VAR} references — nothing live in the fragment.
if grep -qE 'https?://[^$"]' <<<"$rendered"; then
  bad "1c. renderer URL hygiene" "a literal URL leaked into the fragment"
else ok "1c. renderer emits only \${VAR} references, no literal URLs"; fi
# Subset: one app alone renders one pull block + one restart block.
sub="$(TWO_BOT_NEXT_STAGING_HEALTH_URL="https://health.invalid/two-bot-next-staging" bash "$RENDER")"
if grep -q "name: staging-two-bot-next" <<<"$sub" \
  && grep -q "name: staging-restart-two-bot-next" <<<"$sub" \
  && ! grep -q "staging-two-web-next" <<<"$sub"; then
  ok "1d. single-app render carries exactly its two blocks"
else bad "1d. single-app render" "unexpected blocks present or missing"; fi
if bash "$RENDER" >/dev/null 2>&1; then
  bad "1e. renderer no app URLs" "empty env rendered instead of refusing"
else (( $? == 2 )) && ok "1e. renderer refuses empty env exit 2" || bad "1e. renderer no app URLs" "wrong exit"; fi
if STAGING_DB_TARGETS="bad pair!" TWO_WEB_NEXT_STAGING_HEALTH_URL="https://health.invalid/x" bash "$RENDER" >/dev/null 2>&1; then
  bad "1f. renderer bad DB pair" "bad DB pair rendered instead of refusing"
else (( $? == 2 )) && ok "1f. renderer refuses bad DB pair exit 2" || bad "1f. renderer bad DB pair" "wrong exit"; fi
# Regen-sync: the checked-in example is byte-identical to renderer output —
# the example cannot silently drift from the tool.
if diff <("${FULL_ENV[@]}" bash "$RENDER") "$EXAMPLE" >/dev/null; then
  ok "1g. checked-in example byte-identical to renderer output"
else bad "1g. example sync" "example drifted from renderer — re-generate it"; fi
# Mutant: an example with a restart block deleted must fail the same diff.
if grep -v "name: staging-restart-dashboard" "$EXAMPLE" | diff - <("${FULL_ENV[@]}" bash "$RENDER") >/dev/null; then
  bad "1h. example-sync mutant" "block-deleted example still matches — check vacuous?"
else ok "1h. example-sync mutant goes red"; fi

# === 2. staging stays looser than prod and never pages ======================
# No custom (Akmatori pager) alert may ride a staging block: a red staging
# endpoint notifies on Discord only and never opens an incident.
if grep -q "type: custom" <<<"$rendered"; then
  bad "2. no pager on staging" "a custom/Akmatori alert block is present"
else ok "2. staging fragment carries no custom pager alert"; fi
# NON-VACUITY: a copy with a pager alert planted must trip assertion 2.
WORK="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/stg_test.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
if ! python3 - "$RENDER" "$WORK/paging.sh" <<'PYEOF'
import sys
src = open(sys.argv[1]).read()
old = '''    alerts:
      - type: discord
        description: "Staging app $app unhealthy (non-paging; see docs/runbooks/staging-app-health.md)"'''
assert old in src, "anchor for staging alert block not found"
src = src.replace(old, old + '''
      - type: custom
        description: "Staging app $app unhealthy (MUTANT pager)"''')
open(sys.argv[2], "w").write(src)
PYEOF
then
  bad "2b. pager mutant" "could not build paging copy (anchor moved?)"
else
  mut_out="$("${FULL_ENV[@]}" bash "$WORK/paging.sh" 2>/dev/null)"
  if grep -q "type: custom" <<<"$mut_out"; then
    ok "2b. pager mutant plants a custom alert (no-pager assertion is non-vacuous)"
  else bad "2b. pager mutant" "planted pager did not render — check vacuous?"; fi
fi
# Prod budgets must not leak into the staging fragment: no 60s pull, no
# 3000ms budget, no 10m heartbeat, no coolify- prod key prefix.
if grep -q 'interval: 60s' <<<"$rendered" \
  || grep -q '\[RESPONSE_TIME\] < 3000' <<<"$rendered" \
  || grep -q 'interval: 10m' <<<"$rendered" \
  || grep -q 'name: coolify-' <<<"$rendered"; then
  bad "2c. no prod budget" "a prod interval, budget, heartbeat or key leaked in"
else ok "2c. no prod interval/budget/heartbeat/key in staging fragment"; fi
# Staging keys live under group staging, distinct from the prod coolify group
# and from the two-web-staging pull check already in ops/case_658bf6b9.
if grep -q 'group: staging' <<<"$rendered" && ! grep -q 'group: coolify' <<<"$rendered"; then
  ok "2d. all blocks grouped staging, none grouped coolify"
else bad "2d. staging group" "a block misses group staging or carries group coolify"; fi

# === 3. credential hygiene: this module mints, stores and sends nothing =====
# The push side (operator cron + _gatus_push.sh) owns the Bearer. The emitted
# fragment must carry no credential material: no curl, no literal Bearer
# secret, no hard-coded token value, no webhook URL with an ID. ${VAR}
# references (e.g. token: "${GATUS_EXTERNAL_TOKEN}") are allowed — they
# resolve host-side from the root-only 0600 env file, never from this repo.
# The check scans the RENDERED OUTPUT (the artifact that could leak), so a
# renderer mutation that hard-codes a secret trips it too.
surface_pat='curl|Bearer [A-Za-z0-9_.~-]|token: *"[^$"]|discord\.com/api/webhooks/[0-9]'
credential_surface() { grep -E "$surface_pat" "$1" >/dev/null; } # rc 0 = surface found
printf '%s\n' "$rendered" > "$WORK/rendered.yaml"
hits="$(grep -nE "$surface_pat" "$WORK/rendered.yaml" || true)"
if [[ -n "$hits" ]]; then
  bad "3. credential surface" "${hits:0:240}"
else ok "3. rendered fragment carries no credential material (only \${VAR} refs)"; fi
# Positive control THROUGH THE SAME CHECK: a mutant fragment with a
# hard-coded token and a literal Bearer line must trip credential_surface.
# (The previous control tested a different regex than the assertion, so a
# hard-coded token passed both — see Paperclip Review on be7ef665.)
{ printf '%s\n' "$rendered"
  printf '%s\n' '    token: "hardcoded-planted-secret"' '    header = "Authorization: Bearer LIVESECRETXYZ"'
} > "$WORK/mutant-cred.yaml"
if credential_surface "$WORK/mutant-cred.yaml"; then
  ok "3b. credential check fires on planted token + Bearer literals"
else bad "3b. credential control" "planted secrets did not trip the check — control disconnected?"; fi

# === 4. operator pushes the group-qualified key ============================
# Gatus addresses external endpoints as <group>_<name>: the key goes straight
# into /api/v1/endpoints/<key>/external (see ops/case_658bf6b9/_gatus_push.sh),
# and every existing pusher uses that form (platform_garm-reaper,
# platform_ci-queue-age, coolify_coolify-restart-<app>). A bare
# staging-restart-<app> push is rejected, so the renderer header, README and
# runbook must name the full key staging_staging-restart-<app> wherever a
# push is described.
README="$HERE/README.md"
RUNBOOK="$HERE/../../docs/runbooks/staging-app-health.md"
missing="$(for doc in "$RENDER" "$README" "$RUNBOOK"; do
  grep -qF 'staging_staging-restart-<app>' "$doc" || echo "group-qualified key missing in $doc"
done)"
if [[ -n "$missing" ]]; then
  bad "4. push key" "${missing:0:240}"
else ok "4. group-qualified push key staging_staging-restart-<app> named in renderer, README, runbook"; fi
if grep -rn "gatus_push staging-restart-<app>\|per staging-restart-<app> key" "$RENDER" "$EXAMPLE" "$README" "$RUNBOOK" >/dev/null; then
  bad "4b. bare push key" "bare-name push phrasing survives — those pushes would be rejected"
else ok "4b. no bare-name push phrasing"; fi

# === 5. restart pushes are gated on the case_73259b15 evaluator =================
# The restart blocks are fed by ops/case_73259b15/coolify_restart_signal.sh (PR
# #551, unmerged at time of writing). The docs must name that dependency and
# the deploy fallback (pull + TCP checks only until it lands), so the
# fragment cannot be deployed with no evaluator through stale docs.
if grep -q "coolify_restart_signal.sh" "$README" \
  && grep -q "coolify_restart_signal.sh" "$RUNBOOK" \
  && grep -q "skip the restart pushes" "$RUNBOOK"; then
  ok "5. restart pushes gated on the case_73259b15 evaluator in README + runbook"
else bad "5. evaluator gate" "README/runbook lost the evaluator prerequisite or the skip fallback"; fi

echo "---"
echo "pass=$pass fail=$fail"
(( fail == 0 ))
