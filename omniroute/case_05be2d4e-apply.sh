#!/usr/bin/env bash
# case_05be2d4e-apply.sh — operator driver for the case_552a1655 pc/* combo + mapping deployment.
#
# Author: Chief Security & Trust Officer / CISO (agent 6a02a7ed), 2026-08-24.
# Reviewed proposal for operator execution. NOT run by an agent — agents are denied
# :20128/api/* by design and that boundary is correct.
#
# WHAT THIS IS: a THIN orchestrator over case_2963068b-omniroute_combo_cli.sh. Every mutation
# goes through that CLI's apply / map-apply, so its backup, containment gates, read-back
# assertions and audit log all still apply. This script adds sequencing, the before/after
# snapshots, the comboId resolution that cannot be done offline, and the post-write
# verification. It contains NO authorization logic of its own, on purpose: two places to
# get authorization right is two places for it to drift.
#
# INPUTS (must sit in the same directory):
#   case_05be2d4e-combo-specs.json      52 combo specs
#   case_05be2d4e-mapping-plan.json     52 mapping rows (comboId resolved at runtime)
#   case_05be2d4e-combo-allowlist.txt   91 leaf ids, deny-by-default allowlist
#   case_2963068b-claude-surface.json   351-id Claude corpus, for the regression audit
#
# CREDENTIALS — two, different scopes, do not conflate (the CLI says the same):
#   OMNIROUTE_MANAGEMENT_KEY  manage/admin scope. Needed by apply/list/map-*.
#   OMNIROUTE_API_KEY         ordinary READ key. Catalogue + the /v1 probes below.
# This script never echoes either. Both stay in the environment.
#
# EXIT: 0 all phases passed. Non-zero = stopped; read the last PHASE line.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="${COMBO_CLI:-$HERE/case_2963068b-omniroute_combo_cli.sh}"
OUT="${case_3531a34b_OUT:-$HERE/case_05be2d4e-run-$(date -u +%Y%m%dT%H%M%SZ)}"
# The /v1 base for the read-only probes in phases 4 and 9. Defaults to HOST loopback,
# matching the CLI's own OMNIROUTE_MODELS_URL default, because this script runs on the
# operator's host. The container-network alias 'omniroute' and 127.0.0.1 are NOT
# interchangeable: from inside the paperclip container only 'omniroute' resolves, from
# the host only 127.0.0.1 does. Derived from OMNIROUTE_MODELS_URL when that is set so the
# probes and the CLI's catalogue lookup can never end up pointed at different instances.
if [ -n "${OMNIROUTE_MODELS_URL:-}" ]; then
  API_BASE="${OMNIROUTE_API_BASE:-${OMNIROUTE_MODELS_URL%/v1/models}}"
else
  API_BASE="${OMNIROUTE_API_BASE:-http://127.0.0.1:20129}"
fi
REASON="case_05be2d4e: apply the case_552a1655 pc/* two-leg combo + exact-pattern mapping spec"
AGENT="00000000-0000-4000-8000-000000000022"
TEMPLATE="chief-security-and-trust-officer"

# ---- the namespace grant. THIS IS A DELIBERATE DECISION, NOT A DEFAULT. -----------
# The CLI ships COMBO_OWNED_PREFIX=two/ and refuses every pc/* name without this line.
# Setting it to pc/ grants this run write authority over the pc/ namespace and nothing
# else. It does NOT weaken PROTECTED_COMBO_GLOBS (hindsight/* auto/* qtSd/*) — those are
# checked first, have no override, and stay refused regardless of this value.
export COMBO_OWNED_PREFIX="pc/"
export COMBO_ALLOWLIST_FILE="${COMBO_ALLOWLIST_FILE:-$HERE/case_05be2d4e-combo-allowlist.txt}"
export COMBO_LOG_FILE="${COMBO_LOG_FILE:-$OUT/combo-audit.jsonl}"
export COMBO_BACKUP_DIR="${COMBO_BACKUP_DIR:-$OUT/backups}"

mkdir -p "$OUT" "$COMBO_BACKUP_DIR" || exit 1
red(){ printf '\033[31m%s\033[0m\n' "$*" >&2; }
grn(){ printf '\033[32m%s\033[0m\n' "$*" >&2; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*" >&2; }
phase(){ printf '\n\033[1m=== PHASE %s\033[0m\n' "$*" >&2; }
stop(){ red "STOPPED in phase $1: $2"; exit "${3:-1}"; }

cli(){ bash "$CLI" "$@"; }

need(){ command -v "$1" >/dev/null 2>&1 || stop 0 "missing required tool: $1"; }
need jq; need curl; need python3

# ============================================================ PHASE 0  preflight
phase "0  preflight"
[ -f "$CLI" ] || stop 0 "combo CLI not found at $CLI"
for f in case_05be2d4e-combo-specs.json case_05be2d4e-mapping-plan.json case_05be2d4e-combo-allowlist.txt; do
  [ -s "$HERE/$f" ] || stop 0 "missing input $f"
done
echo "input sha256:" | tee "$OUT/input-manifest.txt"
( cd "$HERE" && sha256sum case_05be2d4e-combo-specs.json case_05be2d4e-mapping-plan.json \
    case_05be2d4e-combo-allowlist.txt case_05be2d4e-apply.sh ) | tee -a "$OUT/input-manifest.txt"
sha256sum "$CLI" | tee -a "$OUT/input-manifest.txt"
ylw "Compare these against the sha256 published in the case_05be2d4e issue thread BEFORE continuing."
ylw "The drop channel is agent-writable (1777, same uid for every agent). The issue thread"
ylw "is the only link in this chain an agent on that box cannot rewrite. This is a stop"
ylw "condition, not advice."
if [ "${case_3531a34b_SHA_CONFIRMED:-}" != "yes" ]; then
  stop 0 "sha256 not confirmed. Re-run with case_3531a34b_SHA_CONFIRMED=yes once you have compared." 3
fi

# selftest runs under the SAME COMBO_OWNED_PREFIX this script is about to mutate with.
#
# It used to run with the prefix UNSET (`env -u COMBO_OWNED_PREFIX`), because the CLI's
# fixtures hardcoded the default 'two/' namespace while its guard read the ambient value,
# so any other prefix turned 3 of 181 assertions red — case_05be2d4e finding F-1. That was a
# real defect in the selftest, and unsetting the prefix was the only way to get a
# trustworthy proof out of it, but it bought that trust by proving the tool under a
# configuration this script does not use.
#
# case_3075398f fixed the fixtures: they now build their names from $COMBO_OWNED_PREFIX, and a
# CI step asserts the pass/fail counts are identical across five prefixes. So the prefix
# is left set, and the proof is now of the tool AS CONFIGURED — which is strictly
# stronger. If this ever goes red under pc/ while passing unset, that is no longer a
# known fixture bug to be worked around; it is a regression, and it must stop the run.
bash "$CLI" selftest >"$OUT/selftest.txt" 2>&1 \
  || stop 0 "CLI selftest failed under COMBO_OWNED_PREFIX=$COMBO_OWNED_PREFIX — see $OUT/selftest.txt"
grn "CLI selftest passed ($(grep -c PASS "$OUT/selftest.txt") assertions, under COMBO_OWNED_PREFIX=$COMBO_OWNED_PREFIX)"
# A HARD stop. check-endpoints sends no credential — its whole purpose is to run before
# a manage-scoped key goes on the wire at a URL you have not confirmed. A 401 is a PASS.
if ! cli check-endpoints >"$OUT/check-endpoints.txt" 2>&1; then
  cat "$OUT/check-endpoints.txt" >&2
  stop 0 "check-endpoints says at least one URL is wrong. Fix it before any credential is sent. If you are running this INSIDE a container, set OMNIROUTE_BASE_URL=http://omniroute:20128 and OMNIROUTE_MODELS_URL=http://omniroute:20129/v1/models — the host loopback does not resolve there." 3
fi
grn "both endpoints reachable and enforcing auth (probe sent no credential)"
grn "read-only /v1 probes will use API_BASE=$API_BASE"

[ -n "${OMNIROUTE_MANAGEMENT_KEY:-}" ] || stop 0 "OMNIROUTE_MANAGEMENT_KEY unset" 3
[ -n "${OMNIROUTE_API_KEY:-}" ]        || stop 0 "OMNIROUTE_API_KEY (read scope) unset — needed for the catalogue and the /v1 probes" 3

# ================================================ PHASE 1  BEFORE snapshot (mgmt-side)
# Taken through the CLI's list/map-list, i.e. the MANAGEMENT API. /v1/combos on an agent
# key shows 3 of 41+ callable combos (case_3955ba49) and would prove nothing about the 19 agent
# bindings in other companies that the additive-only constraint exists to protect.
phase "1  BEFORE snapshot (management-side)"
cli list      >"$OUT/before-combos.tsv"   2>"$OUT/before-combos.err"   || stop 1 "combo list failed — see before-combos.err"
cli map-list  >"$OUT/before-mappings.tsv" 2>"$OUT/before-mappings.err" || stop 1 "map-list failed — see before-mappings.err"
grn "before: $(wc -l <"$OUT/before-combos.tsv") combos, $(wc -l <"$OUT/before-mappings.tsv") mappings"
if [ "$(wc -l <"$OUT/before-mappings.tsv")" -ne 0 ]; then
  ylw "model_combo_mappings was 0 rows when case_552a1655 measured it. It is not 0 now."
  ylw "The 'any Claude id resolving to pc/* is a regression introduced here' argument in"
  ylw "phase 8 assumes a 0-row baseline. Re-read before-mappings.tsv and decide whether"
  ylw "the pre-existing rows are yours before continuing."
  [ "${case_3531a34b_NONEMPTY_MAPPINGS_OK:-}" = "yes" ] || stop 1 "non-empty mapping baseline not acknowledged" 3
fi
# Additive-only tripwire: record the protected namespaces so phase 7 can prove they moved.
grep -E '(^|[[:space:]])(hindsight|auto|qtSd)/' "$OUT/before-combos.tsv" | sort >"$OUT/before-protected.tsv" || true
grn "protected-namespace rows recorded: $(wc -l <"$OUT/before-protected.tsv")"

# ============================================== PHASE 2  offline validate (all 52)
phase "2  offline validate — schema + containment, no network, no DB"
mkdir -p "$OUT/specs"
jq -c '.[]' "$HERE/case_05be2d4e-combo-specs.json" | while read -r s; do
  n="$(jq -r .name <<<"$s" | tr '/' '_')"
  jq -S . <<<"$s" >"$OUT/specs/$n.json"
done
vp=0; vf=0
for f in "$OUT"/specs/*.json; do
  if cli validate "$f" >>"$OUT/validate.txt" 2>&1; then vp=$((vp+1)); else vf=$((vf+1)); echo "FAILED: $f" >>"$OUT/validate.txt"; fi
done
echo "validate: $vp pass / $vf fail" >&2
[ "$vf" -eq 0 ] || stop 2 "$vf specs failed offline validation — see $OUT/validate.txt" 3
grn "all 52 specs pass schema + allowlist + Claude tripwire offline"

# ================================== PHASE 3  live catalogue check (dry-run, all 52)
# This is the step that catches an id withdrawn or renamed upstream since 2026-08-23,
# and any id that is a BLENDED route. Nothing is written.
phase "3  dry-run — live catalogue check for all 91 leaf ids"
dp=0; df=0
for f in "$OUT"/specs/*.json; do
  if cli dry-run "$f" --reason "$REASON" --agent "$AGENT" --template "$TEMPLATE" >>"$OUT/dry-run.txt" 2>&1
  then dp=$((dp+1)); else df=$((df+1)); echo "FAILED: $f" >>"$OUT/dry-run.txt"; fi
done
echo "dry-run: $dp pass / $df fail" >&2
[ "$df" -eq 0 ] || stop 3 "$df specs failed the live catalogue check — see $OUT/dry-run.txt. Do NOT hand-edit the spec; fix the leg table and regenerate." 3
grn "every leaf id is present in the live catalogue and is not a blended route"

# =========================== PHASE 4  terminal-leg callability probe (READ key only)
# The issue is explicit: presence in /v1/models is NOT proof of callability. Only 4 of
# the 52 terminal legs were ever called. This probes each UNIQUE terminal leg once,
# streaming, max_tokens 16. Read-only; writes nothing to OmniRoute.
#
# case_be6c5238 CAVEAT — CORRECTED 2026-09-03 (case_156388b9). This block previously read "non-streaming
# OpenRouter returns 502 on every request". That was measured and found FALSE; do not act on
# it. Corrected statement:
#   * The 502 is OmniRoute's, not OpenRouter's — upstream returns a well-formed 200 and the
#     non-streaming translation path discards it (root cause: diagnostics.ts:176).
#   * It is a property of the individual CALL, not of the provider and not of the model. It
#     fires when a non-streaming response arrives with empty `content` because `max_tokens`
#     truncated a reasoning model before its first content token.
#   * There is no stable list of affected models. case_be6c5238 §3b measured one model at 502 twice
#     and 200 once on identical input; §3c measured every model from the 502 column returning
#     0/3 502 once `max_tokens` was raised to 600.
# These probes are streaming, so they still establish each leg for STREAMING callers only. A
# stream:false caller is exposed only when its own `max_tokens` can truncate a reasoning model
# mid-reasoning — the mitigation is raising `max_tokens` on the caller, never avoiding a model
# name. Full evidence: case_be6c5238-nonstream-502-root-cause.md §3a–§4.
phase "4  terminal-leg callability probe (streaming, max_tokens 16)"
jq -r '[.[].models[1].model] | unique | .[]' "$HERE/case_05be2d4e-combo-specs.json" >"$OUT/terminal-legs.txt"
: >"$OUT/leg-probe.tsv"
while read -r leg; do
  [ -n "$leg" ] || continue
  code="$(curl -s -o "$OUT/.legbody" -w '%{http_code}' --max-time 60 \
      -H "Authorization: Bearer $OMNIROUTE_API_KEY" -H 'Content-Type: application/json' \
      -H 'Accept: text/event-stream' \
      -d "$(jq -cn --arg m "$leg" '{model:$m,stream:true,max_tokens:16,messages:[{role:"user",content:"hi"}]}')" \
      "$API_BASE/v1/chat/completions")"
  bytes=$(wc -c <"$OUT/.legbody" 2>/dev/null || echo 0)
  printf '%s\t%s\t%s\n' "$leg" "$code" "$bytes" >>"$OUT/leg-probe.tsv"
done <"$OUT/terminal-legs.txt"
rm -f "$OUT/.legbody"
bad=$(awk -F'\t' '$2!=200' "$OUT/leg-probe.tsv" | wc -l)
grn "probed $(wc -l <"$OUT/terminal-legs.txt") unique terminal legs; $bad non-200"
if [ "$bad" -ne 0 ]; then
  awk -F'\t' '$2!=200 {print "  NOT CALLABLE: "$1" -> HTTP "$2}' "$OUT/leg-probe.tsv" >&2
  ylw "A non-callable terminal leg means that combo has NO working fallback. Its Go leg"
  ylw "still serves, so the combo is not broken — it is unprotected. That is a different"
  ylw "risk and it should be an explicit decision, not a silent one."
  [ "${case_3531a34b_DEAD_LEGS_OK:-}" = "yes" ] || stop 4 "$bad terminal legs are not callable. Re-run with case_3531a34b_DEAD_LEGS_OK=yes to proceed with unprotected combos, or fix the leg table." 3
fi

# ================================================== PHASE 5  apply the 52 combos
phase "5  apply 52 combos (backup + read-back asserted per combo by the CLI)"
ap=0
for f in "$OUT"/specs/*.json; do
  n="$(jq -r .name "$f")"
  if cli apply "$f" --reason "$REASON" --agent "$AGENT" --template "$TEMPLATE" >>"$OUT/apply.txt" 2>&1; then
    ap=$((ap+1)); echo "  applied $n" >&2
  else
    rc=$?
    red "apply FAILED for $n (rc=$rc)"
    [ "$rc" -eq 4 ] && red "rc=4 is a READ-BACK MISMATCH: the mutation MAY STAND. Check $OUT/apply.txt and $COMBO_BACKUP_DIR before rerunning."
    stop 5 "stopped after $ap successful applies; $n failed. Earlier combos are live. Roll back with 'delete' or leave them — they are additive and inert until a mapping points at them." "$rc"
  fi
done
grn "all 52 combos applied and read-back verified"

# ============================ PHASE 6  resolve comboIds, then create the 52 mappings
# comboId is SERVER-GENERATED. It cannot be authored offline, which is why the mapping
# plan carries comboName and this phase resolves it. Resolving by name against a fresh
# list is also what makes a partial re-run safe.
phase "6  resolve comboIds and create 52 mappings"
cli list >"$OUT/after-apply-combos.tsv" 2>/dev/null || stop 6 "list failed while resolving comboIds"
mkdir -p "$OUT/mapspecs"
python3 - "$HERE/case_05be2d4e-mapping-plan.json" "$OUT/after-apply-combos.tsv" "$OUT/mapspecs" <<'PY' || stop 6 "comboId resolution failed"
import json,sys,os
plan=json.load(open(sys.argv[1])); out=sys.argv[3]
byname={}
for line in open(sys.argv[2]):
    p=line.rstrip('\n').split('\t')
    if len(p)>=2: byname[p[1]]=p[0]
missing=[m['comboName'] for m in plan if m['comboName'] not in byname]
if missing:
    print("comboId resolution FAILED, %d combos not found by name: %s" % (len(missing), missing[:5]), file=sys.stderr)
    sys.exit(1)
for m in plan:
    spec={"pattern":m["pattern"],"comboId":byname[m["comboName"]],
          "priority":m["priority"],"enabled":True,
          "description":"case_05be2d4e exact-pattern mapping -> %s" % m["comboName"]}
    json.dump(spec, open(os.path.join(out, m["pattern"].replace('/','_')+".json"),"w"), indent=1)
print("resolved %d comboIds" % len(plan), file=sys.stderr)
PY
mp=0
for f in "$OUT"/mapspecs/*.json; do
  if cli map-apply "$f" --reason "$REASON" --agent "$AGENT" --template "$TEMPLATE" >>"$OUT/map-apply.txt" 2>&1; then
    mp=$((mp+1))
  else
    rc=$?
    stop 6 "map-apply failed after $mp mappings ($(jq -r .pattern "$f")). Combos are live; mappings are partial. See $OUT/map-apply.txt" "$rc"
  fi
done
grn "all 52 mappings created and read-back verified"

# ============================================= PHASE 7  AFTER snapshot + additivity
phase "7  AFTER snapshot and additive-only proof"
cli list     >"$OUT/after-combos.tsv"   2>/dev/null || stop 7 "list failed"
cli map-list >"$OUT/after-mappings.tsv" 2>/dev/null || stop 7 "map-list failed"
grep -E '(^|[[:space:]])(hindsight|auto|qtSd)/' "$OUT/after-combos.tsv" | sort >"$OUT/after-protected.tsv" || true
if ! diff -q "$OUT/before-protected.tsv" "$OUT/after-protected.tsv" >/dev/null; then
  diff "$OUT/before-protected.tsv" "$OUT/after-protected.tsv" >"$OUT/PROTECTED-DIFF.txt"
  stop 7 "PROTECTED NAMESPACE CHANGED. hindsight/*, auto/* or qtSd/* differs before vs after. See $OUT/PROTECTED-DIFF.txt. 19 agent bindings in other companies route through these." 4
fi
grn "additive-only holds: hindsight/*, auto/* and qtSd/* are byte-identical before and after"
comm -13 <(sort "$OUT/before-combos.tsv") <(sort "$OUT/after-combos.tsv") >"$OUT/added-combos.tsv"
comm -23 <(sort "$OUT/before-combos.tsv") <(sort "$OUT/after-combos.tsv") >"$OUT/removed-combos.tsv"
if [ -s "$OUT/removed-combos.tsv" ]; then
  stop 7 "$(wc -l <"$OUT/removed-combos.tsv") combos DISAPPEARED. This run was supposed to be additive. See $OUT/removed-combos.tsv" 4
fi
grn "added $(wc -l <"$OUT/added-combos.tsv") combos, removed 0"

# ================================== PHASE 8  Claude containment regression audit
# Not a regex over names — an exact-match audit against the 351-id corpus, because the
# family-word regex has a known false negative (aug/prism-a carries Claude and contains
# no family word at all). See the runbook, finding F-2.
phase "8  Claude containment regression audit (351-id corpus x stored mappings)"
python3 - "$OUT/after-mappings.tsv" /paperclip/operator-handoff/case_2963068b-claude-surface.json "$OUT" <<'PY' || stop 8 "CLAUDE CONTAINMENT REGRESSION — see claude-audit.json" 4
import json,sys,fnmatch
rows=[l.rstrip('\n').split('\t') for l in open(sys.argv[1]) if l.strip()]
claude=json.load(open(sys.argv[2]))['all_claude_ids']
# map-list emits: priority, pattern, comboId, enabled — in resolution order.
pats=[(r[1], r[0], r[2]) for r in rows if len(r)>=3]
hits=[]
for pat,prio,cid in pats:
    for c in claude:
        if fnmatch.fnmatchcase(c,pat): hits.append({"pattern":pat,"priority":prio,"comboId":cid,"claude_id":c})
res={"stored_mappings":len(pats),"claude_corpus":len(claude),"matches":hits,
     "verdict":"PASS" if not hits else "FAIL"}
json.dump(res,open(sys.argv[3]+"/claude-audit.json","w"),indent=1)
print("claude audit: %d stored mappings x %d Claude ids -> %d matches" % (len(pats),len(claude),len(hits)), file=sys.stderr)
sys.exit(0 if not hits else 1)
PY
grn "no stored mapping captures any of the 351 Claude-bearing ids"

# =========================== PHASE 9  per-combo verification (SSE TRAILER, not headers)
# F-9: the x-omniroute-* HEADER block is absent on ~29% of streaming 200s and the loss is
# concentrated on the SLOW leg (0/7 Go vs 4/7 OpenRouter). Header-based verification
# therefore UNDER-REPORTS fallthrough — it would silently pass a combo that fell through
# to OpenRouter. Parse the trailer.
phase "9  per-combo verification — HTTP 200 + SSE trailer provider=opencode-go"
: >"$OUT/verify.tsv"
jq -r '.[].name' "$HERE/case_05be2d4e-combo-specs.json" | while read -r combo; do
  body="$OUT/.v"
  code="$(curl -s -o "$body" -w '%{http_code}' --max-time 90 \
      -H "Authorization: Bearer $OMNIROUTE_API_KEY" -H 'Content-Type: application/json' \
      -H 'Accept: text/event-stream' \
      -d "$(jq -cn --arg m "$combo" '{model:$m,stream:true,max_tokens:16,messages:[{role:"user",content:"hi"}]}')" \
      "$API_BASE/v1/chat/completions")"
  trailer="$(grep -a '^: *x-omniroute-decision=' "$body" | tail -1 || true)"
  if   [ -z "$trailer" ];                             then verdict="NO_TRAILER"
  elif grep -q 'provider=opencode-go' <<<"$trailer";  then verdict="GO"
  else                                                     verdict="FELL_THROUGH"; fi
  printf '%s\t%s\t%s\t%s\n' "$combo" "$code" "$verdict" "${trailer:0:180}" >>"$OUT/verify.tsv"
done
rm -f "$OUT/.v"
n200=$(awk -F'\t' '$2==200' "$OUT/verify.tsv" | wc -l)
ngo=$(awk -F'\t' '$3=="GO"' "$OUT/verify.tsv" | wc -l)
nnt=$(awk -F'\t' '$3=="NO_TRAILER"' "$OUT/verify.tsv" | wc -l)
nft=$(awk -F'\t' '$3=="FELL_THROUGH"' "$OUT/verify.tsv" | wc -l)
echo "verify: 200=$n200/52  go-leg-won=$ngo  fell-through=$nft  no-trailer=$nnt" >&2
[ "$nft" -eq 0 ] || { awk -F'\t' '$3=="FELL_THROUGH"{print "  FELL THROUGH: "$1}' "$OUT/verify.tsv" >&2; ylw "fallthrough means the Go leg did not serve. The combo works, but not as designed."; }
[ "$nnt" -eq 0 ] || ylw "$nnt combos returned no trailer — UNVERIFIED, not passed. Re-probe those individually."

# ======================================= PHASE 10  stored-strategy read-back (guard 1)
phase "10  stored strategy reads back as 'priority' for all 52"
awk -F'\t' '$2 ~ /^pc\// {print $2"\t"$3}' "$OUT/after-combos.tsv" >"$OUT/stored-strategies.tsv"
nbad=$(awk -F'\t' '$2!="priority"' "$OUT/stored-strategies.tsv" | wc -l)
if [ "$nbad" -ne 0 ]; then
  awk -F'\t' '$2!="priority"{print "  "$1" stored as "$2}' "$OUT/stored-strategies.tsv" >&2
  stop 10 "$nbad combos did not store strategy='priority'. normalizeRoutingStrategy() coerces silently — the combo is not what was requested." 4
fi
grn "all $(wc -l <"$OUT/stored-strategies.tsv") pc/* combos store strategy=priority"

phase "DONE"
grn "artifacts in $OUT"
grn "attach to case_05be2d4e: input-manifest.txt before-*.tsv after-*.tsv added-combos.tsv"
grn "                  leg-probe.tsv verify.tsv claude-audit.json combo-audit.jsonl"
ylw "REMINDER: strategy=priority is INTERIM with a defined expiry. Two opencode-go"
ylw "connections are now active (main, main-2) — the case_552a1655 bake-off precondition of"
ylw ">=2 connections may already be met. Re-open case_552a1655 before this setting is forgotten."
