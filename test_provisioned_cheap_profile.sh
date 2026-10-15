#!/usr/bin/env bash
# case_521b73a1 regression suite: a freshly provisioned agent must be BORN with an
# explicitly falsy `effort` on its cheap model profile.
#
# WHY THIS EXISTS
# ---------------
# `effort` is a legitimate claude_local CLI-lane field and ships in that
# adapter's own default cheap profile (adapters/claude-local/src/index.ts).
# The ACP engine lane refuses it outright:
#
#   ACP session ... does not advertise config option 'effort'.
#   Supported config options: mode, model.
#
# An agent that downshifts to the cheap profile on the ACP lane therefore dies,
# and cannot self-heal, because self-repair requires it to be running. case_4656467a
# swept the 47 existing agents; this suite closes the hole for NEW ones.
#
# THE TRAP THIS SUITE EXISTS TO CATCH
# -----------------------------------
# Omitting the key does NOT work. resolveModelProfileApplication
# (server/src/services/heartbeat.ts) spreads the ADAPTER DEFAULT FIRST and the
# stored profile second, so any key we merely leave out is re-supplied as "low"
# at run time. That is why `absent` and `null` are asserted to be FAILURES here
# rather than passes -- a naive "no effort key present" assertion would go green
# on precisely the broken shape. Both lanes guard on TRUTHINESS, so "" is what
# suppresses the ACP option while leaving the CLI lane working.
#
# This runs fully offline: `pc` and `pcsql` are stubbed, so no database, API,
# credential or agent mutation is involved. It exercises the REAL create path
# in org_provisioner.sh and captures the payload actually sent.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROV="${PROV:-$HERE/org_provisioner.sh}"
command -v jq >/dev/null || { echo "ERROR: jq required" >&2; exit 1; }
[[ -f "$PROV" ]] || { echo "ERROR: missing $PROV" >&2; exit 1; }

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- stubs -----------------------------------------------------------------
# `pc` records every create payload it is called with (one line per call, so a
# case_26f23714 retry is distinguishable from a single call) and returns a
# plausible agent id. When case_3a7e5a0a_REJECT_MODELPROFILES=1 it simulates a host
# already on migration 0236: a create payload naming `modelProfiles` comes
# back as a validation rejection instead of an id, same as the real
# `shared/validators/agent.ts` would after the upstream migration lands.
# Every other subcommand is a no-op success, so the create path runs to
# completion.
mkdir -p "$WORK/bin"
cat >"$WORK/bin/paperclipai" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "agent" && "${2:-}" == "create" ]]; then
  payload=""
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "--payload-json" ]]; then
      j=$((i+1)); payload="${!j}"
    fi
  done
  printf '%s' "$payload" >"$case_642b01c6_PAYLOAD"
  printf '%s\n' "$payload" >>"$case_642b01c6_PAYLOAD_LOG"
  if [[ "${case_3a7e5a0a_REJECT_MODELPROFILES:-0}" == "1" ]] && grep -q '"modelProfiles"' <<<"$payload"; then
    echo '{"error":"Unrecognized key(s) in object: '"'"'modelProfiles'"'"'"}'
    exit 0
  fi
  echo '{"id":"00000000-0000-4000-8000-000000000180"}'
  exit 0
fi
echo '{}'
STUB
chmod +x "$WORK/bin/paperclipai"

# `psql` stands in for the pcsql backend: lookup_agent returns one caller row,
# every other query returns nothing.
cat >"$WORK/bin/psql" <<'STUB'
#!/usr/bin/env bash
sql="$(cat)"
if [[ "$sql" == *"FROM company_secrets"* ]]; then
  printf 't\n'
elif [[ "$sql" == *"permissionProfile"* ]]; then
  printf '%s\t%s\t%s\n' "00000000-0000-4000-8000-000000000003" "P1_PRESIDENT_COO" "President & COO"
fi
exit 0
STUB
chmod +x "$WORK/bin/psql"

# Run the real `create` and echo the LAST captured payload on stdout (every
# attempt, in order, also lands in $WORK/payload.log). $WORK/rc.txt carries the
# provisioner's own exit status, since it is lost once this function's stdout
# is captured via command substitution.
provision() {
  local payload_file="$WORK/payload.json" payload_log="$WORK/payload.log" rc_file="$WORK/rc.txt"
  : >"$payload_file"; : >"$payload_log"
  PATH="$WORK/bin:$PATH" \
  case_642b01c6_PAYLOAD="$payload_file" \
  case_642b01c6_PAYLOAD_LOG="$payload_log" \
  case_3a7e5a0a_REJECT_MODELPROFILES="${2:-0}" \
  COMPANY_ID="00000000-0000-4000-8000-000000000004" \
  PROVISIONER_CLAUDE_TRANSPORT_JSON='{"companyId":"00000000-0000-4000-8000-000000000004","baseUrl":"http://cliproxy:8317","secretId":"00000000-0000-4000-8000-000000000020","assignedModel":"test-assigned-model","smallFastModel":"test-small-model","apiTimeoutMs":"600000","maxContextTokens":"200000"}' \
  PAPERCLIP_CLI="$WORK/bin/paperclipai" \
  PAPERCLIP_SQL_BACKEND="psql" \
  PGHOST="stub-offline" \
  ADAPTER_TYPE="${1:-claude_local}" \
    bash "$PROV" create --caller P1_PRESIDENT_COO --template B1_FUNCTION_CHIEF \
      --title "case_521b73a1 probe" >/dev/null 2>"$WORK/err.txt"
  echo $? >"$rc_file"
  cat "$payload_file"
}

cheap_of() { jq -c '.runtimeConfig.modelProfiles.cheap // null' <<<"$1"; }

printf '\n\033[1mTOG-689: a provisioned claude_local agent is born ACP-safe\033[0m\n'

PAYLOAD="$(provision claude_local)"
if [[ -z "$PAYLOAD" ]]; then
  bad "provisioner produced a create payload (stderr: $(tr '\n' ' ' <"$WORK/err.txt" | cut -c1-200))"
else
  ok "provisioner produced a create payload"

  CHEAP="$(cheap_of "$PAYLOAD")"
  # Distinguish "key present and empty" from "key missing". A `// "ABSENT"`
  # default would collapse exactly the two cases this suite must tell apart,
  # because jq's `//` fires on an empty string too.
  EFFORT="$(jq -r 'if ((.adapterConfig // {}) | has("effort"))
                   then (.adapterConfig.effort | tostring)
                   else "ABSENT" end' <<<"$CHEAP")"

  # The load-bearing assertion. `absent` is a FAILURE, not a pass: the adapter
  # default refills an omitted key with "low" at run time.
  case "$EFFORT" in
    "")     ok  "cheap.adapterConfig.effort is pinned to the empty string" ;;
    ABSENT) bad "effort is ABSENT -- the adapter default refills it with \"low\" at run time" ;;
    *)      bad "effort is '"'"'$EFFORT'"'"' -- any truthy value is fatal on the ACP lane" ;;
  esac

  # Guard the key exists rather than merely being falsy-by-absence.
  if jq -e 'has("adapterConfig") and (.adapterConfig|has("effort"))' <<<"$CHEAP" >/dev/null 2>&1; then
    ok "the effort key is explicitly present, not merely omitted"
  else
    bad "the effort key is not explicitly present"
  fi

  # The heartbeat block must survive: runtimeConfig is replaced WHOLESALE on
  # PATCH, and an earlier sweep clobbered a live agent's heartbeat this way.
  if jq -e '.runtimeConfig.heartbeat.enabled == false
            and .runtimeConfig.heartbeat.wakeOnDemand == false' <<<"$PAYLOAD" >/dev/null; then
    ok "the heartbeat block is preserved alongside the new modelProfiles block"
  else
    bad "the heartbeat block was lost: $(jq -c '.runtimeConfig.heartbeat' <<<"$PAYLOAD")"
  fi

  # Born disabled AND pinned. Disabled alone short-circuits before the merge and
  # is safe today, but is one `enabled:true` edit away from fatal; the pin makes
  # the agent safe in both positions.
  if jq -e '.enabled == false' <<<"$CHEAP" >/dev/null; then
    ok "the cheap profile is born disabled (safe even before the merge is reached)"
  else
    bad "the cheap profile is not born disabled: $CHEAP"
  fi

  # Invariants that predate this change and must not regress.
  #
  # The assignment mode expectation was INVERTED by the case_effd74c7 owner decision
  # (2026-09-05): agents are now born `company_default` with canAssignTasks
  # true, because the old born-protected posture made an agent's first hand-back
  # up its own chain fail with 403 deny_policy_restricted (case_b344a149/69/586).
  # `canCreateAgents == false` is untouched by that decision and still holds —
  # the creator ceiling is a separate invariant from the assignment baseline.
  if jq -e '.permissions.canCreateAgents == false
            and .permissions.canAssignTasks == true
            and .permissions.authorizationPolicy.assignmentPolicy.mode == "company_default"' \
       <<<"$PAYLOAD" >/dev/null; then
    ok "provisioning invariants hold: creator ceiling off, case_effd74c7 assignment baseline on"
  else
    bad "provisioning invariants regressed: $(jq -c '.permissions' <<<"$PAYLOAD")"
  fi
fi

printf '\n\033[1mAdapters are opted in explicitly, never by default\033[0m\n'
OTHER="$(provision codex_local)"
if [[ -z "$OTHER" ]]; then
  bad "provisioner produced a payload for a non-claude_local adapter"
elif jq -e '.runtimeConfig | has("modelProfiles") | not' <<<"$OTHER" >/dev/null; then
  ok "a non-claude_local adapter gets no invented cheap profile"
else
  bad "a cheap profile was invented for an adapter that may not declare one: \
$(jq -c '.runtimeConfig.modelProfiles' <<<"$OTHER")"
fi

printf '\n\033[1mwakeOnDemand: born off for every hire, opt-in per create for event-driven agents\033[0m\n'
# An agent whose policy has wakeOnDemand=false never dispatches on a mention,
# PR event or assignment (server heartbeat.ts drops every non-timer wake), so
# the review-bot reviewer needs it on while every other hire must stay off.
DEFAULT_WAKE="$(provision codex_local)"
if jq -e '.runtimeConfig.heartbeat == {enabled:false, wakeOnDemand:false}' <<<"$DEFAULT_WAKE" >/dev/null 2>&1; then
  ok "default create keeps heartbeat {enabled:false, wakeOnDemand:false}"
else
  bad "default heartbeat changed: $(jq -c '.runtimeConfig.heartbeat' <<<"$DEFAULT_WAKE")"
fi
WAKE_ON="$(HEARTBEAT_WAKE_ON_DEMAND=true provision codex_local)"
if jq -e '.runtimeConfig.heartbeat == {enabled:false, wakeOnDemand:true}' <<<"$WAKE_ON" >/dev/null 2>&1; then
  ok "HEARTBEAT_WAKE_ON_DEMAND=true sets wakeOnDemand only; the timer heartbeat stays off"
else
  bad "opt-in heartbeat wrong: $(jq -c '.runtimeConfig.heartbeat' <<<"$WAKE_ON")"
fi
WAKE_OFF="$(HEARTBEAT_WAKE_ON_DEMAND=false provision codex_local)"
if jq -e '.runtimeConfig.heartbeat.wakeOnDemand == false' <<<"$WAKE_OFF" >/dev/null 2>&1; then
  ok "an explicit HEARTBEAT_WAKE_ON_DEMAND=false matches the default"
else
  bad "explicit false produced: $(jq -c '.runtimeConfig.heartbeat' <<<"$WAKE_OFF")"
fi
# Anything but a JSON boolean must be refused BEFORE any create call: jq
# --argjson would otherwise ship a string, number or object into runtimeConfig.
BAD_WAKE="$(HEARTBEAT_WAKE_ON_DEMAND=yes provision codex_local)"
BAD_RC="$(cat "$WORK/rc.txt")"
if [[ -z "$BAD_WAKE" && "$BAD_RC" != "0" ]] && grep -q "HEARTBEAT_WAKE_ON_DEMAND must be" "$WORK/err.txt"; then
  ok "a non-boolean HEARTBEAT_WAKE_ON_DEMAND is refused before any create call"
else
  bad "non-boolean value not refused (rc=$BAD_RC, payload='$BAD_WAKE', err: $(tr '\n' ' ' <"$WORK/err.txt" | cut -c1-160))"
fi

printf '\n\033[1mTOG-3348: a host that already rejects modelProfiles gets retried without it\033[0m\n'
provision claude_local 1 >/dev/null
ATTEMPTS="$(wc -l <"$WORK/payload.log" | tr -d ' ')"
# Checked against stderr, not the script's overall exit status: the offline
# psql stub answers only the caller lookup this suite needs, so the later,
# unrelated grant-attribution write (case_f909ca1f) legitimately fails here on every
# path including the pre-existing case_521b73a1 case above. The assertion this test
# owns is narrower and precise -- did id resolution survive the retry.
if ! grep -q "agent creation failed" "$WORK/err.txt"; then
  ok "creation still succeeds when the first attempt is rejected for carrying modelProfiles"
else
  bad "creation failed even after the retry: $(tr '\n' ' ' <"$WORK/err.txt" | cut -c1-200)"
fi
if [[ "$ATTEMPTS" == "2" ]]; then
  ok "the rejection triggered exactly one retry (2 create calls total)"
else
  bad "expected 2 create calls (reject + retry), saw $ATTEMPTS"
fi
FIRST_ATTEMPT="$(sed -n '1p' "$WORK/payload.log")"
SECOND_ATTEMPT="$(sed -n '2p' "$WORK/payload.log")"
if jq -e '.runtimeConfig | has("modelProfiles")' <<<"$FIRST_ATTEMPT" >/dev/null 2>&1; then
  ok "the first attempt still carries the legacy field (pre-upgrade case_521b73a1 protection is tried first)"
else
  bad "the first attempt did not carry runtimeConfig.modelProfiles at all: $FIRST_ATTEMPT"
fi
if jq -e '.runtimeConfig | has("modelProfiles") | not' <<<"$SECOND_ATTEMPT" >/dev/null 2>&1; then
  ok "the retry dropped runtimeConfig.modelProfiles once the host named it as the rejection"
else
  bad "the retry still carried runtimeConfig.modelProfiles: $SECOND_ATTEMPT"
fi

printf '\n\033[1mTOG-3348: an unrelated create failure is not retried\033[0m\n'
# The retry is gated on the host's response actually NAMING modelProfiles, so a
# rejection for any other reason must surface immediately rather than loop.
cat >"$WORK/bin/paperclipai" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "agent" && "${2:-}" == "create" ]]; then
  payload=""
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "--payload-json" ]]; then
      j=$((i+1)); payload="${!j}"
    fi
  done
  printf '%s' "$payload" >"$case_642b01c6_PAYLOAD"
  printf '%s\n' "$payload" >>"$case_642b01c6_PAYLOAD_LOG"
  echo '{"error":"title already in use"}'
  exit 0
fi
echo '{}'
STUB
chmod +x "$WORK/bin/paperclipai"
provision claude_local >/dev/null
RC="$(cat "$WORK/rc.txt")"
ATTEMPTS="$(wc -l <"$WORK/payload.log" | tr -d ' ')"
if [[ "$RC" != "0" ]]; then
  ok "an unrelated rejection still fails the provisioner (no silent retry loop)"
else
  bad "an unrelated rejection was swallowed instead of failing"
fi
if [[ "$ATTEMPTS" == "1" ]]; then
  ok "an unrelated rejection is not retried"
else
  bad "expected exactly 1 create call for an unrelated rejection, saw $ATTEMPTS"
fi

printf '\n\033[1mRESULT: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
