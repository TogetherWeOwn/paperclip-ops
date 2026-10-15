#!/usr/bin/env bash
# review_route.sh — deterministic Tier-2 (expensive reviewer) router (case_6bf2cca9).
#
# Decides whether a PR needs the expensive review model (Claude, bake-off
# winner — unchanged) or merges on Tier-0 + Tier-1 green plus PR-lint.
# Expensive review runs when at least one holds:
#   1. the diff touches a security boundary (authz/permission routes, secrets,
#      production migrations, infra/workflows);
#   2. Tier-0 or Tier-1 flagged risk (caller-supplied flags);
#   3. changed lines (additions + deletions) exceed REVIEW_ROUTE_THRESHOLD
#      (default 1000: ~p66 of recent source-line PR sizes on origin/main,
#      so the size gate alone routes about one third — see docs/review-routing.md);
#   4. the PR falls in the deterministic 10% audit sample
#      (cksum of "repo#pr" mod 100 < pct: stable per PR, ~10% across PRs).
#
# Pure bash + coreutils, offline, no credentials, no model call. Exits 0 on a
# routing decision, 2 on any bad input (missing/non-numeric value, unknown
# option). False-expensive is the safe error: security matching is
# deliberately broad.
set -euo pipefail

REPO=""
PR=""
ADD=""
DEL=""
NUMSTAT_FROM=""
FILES_FROM=()
TIER0_RISK=0
TIER1_RISK=0
THRESHOLD=1000
AUDIT_PCT=10
SECURITY_PATTERNS_FILE=""
LEDGER=""
FORMAT="text"

usage() {
  cat <<'EOF'
usage: review_route.sh --repo OWNER/REPO --pr NUMBER [--add A --del D]
       [--numstat-from FILE] [--file PATH ...]
       [--tier0-risk 0|1] [--tier1-risk 0|1]
       [--threshold N] [--audit-pct P] [--security-patterns FILE]
       [--ledger FILE] [--format text|json]
EOF
}

# Security-boundary substrings matched (lowercased) against repo-relative paths.
# Broad on purpose: authz/permission routes, secrets, production migrations,
# infra/workflows. Override with --security-patterns FILE (one substring per
# line, "#" comments allowed).
# The live gate is .github/scripts/review_trigger.py (RISKY_PATH_PATTERNS);
# these defaults are a deliberate SUPERSET of that list (same stems, plus the
# credential-grant and CI paths this repo owns), so a path the live gate calls
# risky never routes `standard` here. N and the audit key intentionally differ
# (see docs/review-routing.md "Enforcement wiring").
default_security_patterns() {
  printf '%s\n' \
    auth permis permission secret credential token private_key .pem .key \
    migrat migration workflow dockerfile deploy terraform .tf broker provision \
    provisioner protected protection-rule ruleset rbac acl sudo passwd \
    security policy allowlist denylist \
    grant gh_access capability disclosure binding \
    .github/ .env systemd .service .timer wrangler \
    runner garm coolify backup quota billing host-ops
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# JSON string body (no surrounding quotes): escape \ and ", map \n \r \t, and
# replace any other control byte, so a hostile file name cannot corrupt a row.
json_str() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  s=${s//[[:cntrl:]]/?}
  printf '%s' "$s"
}

# die_unless_uint NAME VALUE [MAX]: exit 2 unless VALUE is 1-9 digits (<= MAX).
die_unless_uint() {
  [[ "$2" =~ ^[0-9]{1,9}$ ]] || { echo "review_route: $1 must be a non-negative integer (max 9 digits)" >&2; exit 2; }
  if [[ -n "${3:-}" && $((10#$2)) -gt "$3" ]]; then
    echo "review_route: $1 must be <= $3" >&2; exit 2
  fi
}

audit_hit() {
  # Deterministic ~AUDIT_PCT% sample, stable for a given repo+PR.
  local key="${REPO}#${PR}" h
  h=$(printf '%s' "$key" | cksum | awk '{print $1 % 100}')
  [[ "$h" -lt "$AUDIT_PCT" ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    --repo|--pr|--add|--del|--numstat-from|--file|--tier0-risk|--tier1-risk|--threshold|--audit-pct|--security-patterns|--ledger|--format)
      [[ $# -ge 2 ]] || { echo "review_route: $1 needs a value" >&2; usage >&2; exit 2; };;
    *) echo "review_route: unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
  case "$1" in
    --repo) REPO="$2";;
    --pr) PR="$2";;
    --add) ADD="$2";;
    --del) DEL="$2";;
    --numstat-from) NUMSTAT_FROM="$2";;
    --file) FILES_FROM+=("$2");;
    --tier0-risk) TIER0_RISK="$2";;
    --tier1-risk) TIER1_RISK="$2";;
    --threshold) THRESHOLD="$2";;
    --audit-pct) AUDIT_PCT="$2";;
    --security-patterns) SECURITY_PATTERNS_FILE="$2";;
    --ledger) LEDGER="$2";;
    --format) FORMAT="$2";;
  esac
  shift 2
done

[[ -n "$REPO" ]] || { echo "review_route: --repo is required" >&2; exit 2; }
[[ -n "$PR" ]] || { echo "review_route: --pr is required" >&2; exit 2; }
die_unless_uint --pr "$PR"
PR=$((10#$PR))  # canonical: no leading zeros (JSON numbers, stable audit key)
die_unless_uint --threshold "$THRESHOLD"
THRESHOLD=$((10#$THRESHOLD))
die_unless_uint --audit-pct "$AUDIT_PCT" 100
AUDIT_PCT=$((10#$AUDIT_PCT))
[[ "$TIER0_RISK" =~ ^[01]$ ]] || { echo "review_route: --tier0-risk must be 0|1" >&2; exit 2; }
[[ "$TIER1_RISK" =~ ^[01]$ ]] || { echo "review_route: --tier1-risk must be 0|1" >&2; exit 2; }
[[ "$FORMAT" == text || "$FORMAT" == json ]] || { echo "review_route: --format must be text|json" >&2; exit 2; }

# Collect changed files and line counts from --numstat-from (git diff --numstat).
if [[ -n "$NUMSTAT_FROM" ]]; then
  [[ -f "$NUMSTAT_FROM" ]] || { echo "review_route: numstat file not found: $NUMSTAT_FROM" >&2; exit 2; }
  auto_add=0; auto_del=0
  while IFS=$'\t' read -r a d path; do
    [[ -n "${path:-}" ]] || continue
    FILES_FROM+=("$path")  # record every path first: binary rows skip only line counting
    [[ "$a$d" =~ ^[0-9]+$ ]] || continue
    auto_add=$((auto_add + a)); auto_del=$((auto_del + d))
  done < "$NUMSTAT_FROM"
  [[ -z "$ADD" ]] && ADD="$auto_add"
  [[ -z "$DEL" ]] && DEL="$auto_del"
fi
[[ -n "$ADD" && -n "$DEL" ]] || { echo "review_route: need --add/--del or --numstat-from" >&2; exit 2; }
die_unless_uint --add "$ADD"
die_unless_uint --del "$DEL"
ADD=$((10#$ADD)); DEL=$((10#$DEL))
TOTAL=$((ADD + DEL))

# The patterns-file check runs in the main shell: an `exit 2` inside the
# process substitution below would only leave that subshell, and the router
# would then carry on with no patterns and route an auth change `standard`.
if [[ -n "$SECURITY_PATTERNS_FILE" ]]; then
  [[ -f "$SECURITY_PATTERNS_FILE" ]] || { echo "review_route: patterns file not found: $SECURITY_PATTERNS_FILE" >&2; exit 2; }
  mapfile -t PATTERNS < <(grep -v '^[[:space:]]*#' "$SECURITY_PATTERNS_FILE" | grep -v '^[[:space:]]*$' || true)
else
  mapfile -t PATTERNS < <(default_security_patterns)
fi
# An empty pattern set matches nothing, so every diff would route `standard`.
# That silent fail-open is never a valid configuration: refuse it.
[[ ${#PATTERNS[@]} -gt 0 ]] || { echo "review_route: no security patterns loaded" >&2; exit 2; }

REASONS=()
SECURITY_HIT=""

if [[ ${#FILES_FROM[@]} -gt 0 && ${#PATTERNS[@]} -gt 0 ]]; then
  for f in "${FILES_FROM[@]}"; do
    lf=$(lower "$f")
    for p in "${PATTERNS[@]}"; do
      if [[ "$lf" == *"$(lower "$p")"* ]]; then
        SECURITY_HIT="$p :: $f"
        break 2
      fi
    done
  done
fi
[[ -n "$SECURITY_HIT" ]] && REASONS+=("security-boundary")
[[ "$TOTAL" -gt "$THRESHOLD" ]] && REASONS+=("size>${THRESHOLD}")
[[ "$TIER0_RISK" == 1 ]] && REASONS+=("tier0-risk")
[[ "$TIER1_RISK" == 1 ]] && REASONS+=("tier1-risk")

AUDIT="no"
if audit_hit; then
  AUDIT="yes"
  REASONS+=("audit-sample")
fi

ROUTE="standard"
[[ ${#REASONS[@]} -gt 0 ]] && ROUTE="expensive"

reasons_csv=$(IFS=,; echo "${REASONS[*]}")
repo_j=$(json_str "$REPO")

if [[ "$FORMAT" == json ]]; then
  if [[ ${#REASONS[@]} -eq 0 ]]; then
    reasons_json="[]"
  else
    reasons_json=$(printf '"%s",' "${REASONS[@]}")
    reasons_json="[${reasons_json%,}]"
  fi
  printf '{"repo":"%s","pr":%s,"add":%s,"del":%s,"total":%s,"route":"%s","reasons":%s,"audit_sample":"%s"}\n' \
    "$repo_j" "$PR" "$ADD" "$DEL" "$TOTAL" "$ROUTE" "$reasons_json" "$AUDIT"
else
  printf 'ROUTE=%s\nREASONS=%s\nTOTAL=%s\nAUDIT_SAMPLE=%s\n' \
    "$ROUTE" "$reasons_csv" "$TOTAL" "$AUDIT"
fi

if [[ -n "$LEDGER" ]]; then
  ts=$(date -u +%FT%TZ)
  printf '{"ts":"%s","repo":"%s","pr":%s,"add":%s,"del":%s,"total":%s,"tier0_risk":%s,"tier1_risk":%s,"security_hit":"%s","audit_sample":"%s","route":"%s","reasons":"%s"}\n' \
    "$ts" "$repo_j" "$PR" "$ADD" "$DEL" "$TOTAL" "$TIER0_RISK" "$TIER1_RISK" \
    "$(json_str "$SECURITY_HIT")" "$AUDIT" "$ROUTE" "$reasons_csv" >>"$LEDGER"
fi
