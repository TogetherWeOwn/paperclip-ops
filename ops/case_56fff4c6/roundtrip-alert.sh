#!/usr/bin/env bash
# ===========================================================================
# roundtrip-alert.sh — staging-only alert round-trip harness (case_56fff4c6).
# ---------------------------------------------------------------------------
# THE GAP THIS EXISTS FOR. Sibling case_93aed7ff owns one live drill RUN on node7b1ff338
# (Operator card case_5e79fdfd); no persistent artifact lets anyone re-prove the
# alert path afterwards. This harness is that artifact: a staging-only
# trigger that pushes a canary alert through the Gatus external-endpoint API
# (or the Akmatori staging source) and verifies it lands, without ever
# touching a production incident path.
#
# WHAT IT DOES. (1) Builds a canary payload labelled staging + a unique
# roundtrip id; (2) POSTs it to ROUNDTRIP_URL (the staging sink the operator
# configures — a Gatus staging external endpoint or the Akmatori staging
# source, OPERATOR.md §3); (3) polls ROUNDTRIP_VERIFY_URL (or replays the
# Gatus API) until the canary id appears or ROUNDTRIP_TIMEOUT_S lapses;
# (4) prints ROUNDTRIP-OK with both timestamps, or ROUNDTRIP-FAIL.
#
# THREE RULES THIS FILE ENCODES, EACH BECAUSE THE OBVIOUS VERSION IS WRONG.
#
# 1. STAGING-ONLY IS ENFORCED, NOT DOCUMENTED. A "please only use staging"
#    comment does not stop a 3am copy-paste onto a prod URL. Any target that
#    smells like production (prod/staging-mismatch, the live Akmatori source
#    host, a non-localhost Gatus URL without an explicit staging path) is
#    refused at exit 2 before any byte is sent. The suite pins each refusal.
#
# 2. A TRIGGER WITHOUT A VERIFY IS A FIRE-AND-FORGET. POSTing 200 proves the
#    sink accepted bytes, not that the alert path works end to end. The
#    harness exits 0 ONLY after the canary id is observed back through the
#    verify channel (poll loop, bounded). Accepted-but-never-seen exits 1
#    (LOST), which is the finding this harness exists to surface.
#
# 3. SECRETS NEVER REACH ARGV. Bearer/CF tokens travel in 0600 curl config
#    files, never -H argv (same pattern as probe-gatus.sh and
#    test-akmatori-alert.sh). The suite pins this with a curl shim.
#
# EXIT CODES — distinct so a caller can tell the four apart.
#   0  ROUNDTRIP-OK: canary posted AND observed back (both timestamps printed)
#   1  ROUNDTRIP-FAIL/LOST: posted (or not) but never observed — A FINDING
#   2  refused (bad usage, missing env, production-like target refused)
#   5  UNMEASURED: curl/python3 absent, verify transport missing. NOT green.
#
# MACHINE CONTRACT. Stdout final line is one of:
#   ROUNDTRIP-OK   <id> posted=<ts> observed=<ts>
#   ROUNDTRIP-FAIL <id> posted=<ts|never> reason=<cause>
#   refused        <cause> <detail>
# Stderr is human prose and may drift. Tests pin stdout, not stderr.
# ===========================================================================
set -uo pipefail

ME="$(basename "${BASH_SOURCE[0]}")"

EXIT_OK=0; EXIT_FAIL=1; EXIT_REFUSED=2; EXIT_UNMEASURED=5

TIMEOUT_S="${ROUNDTRIP_TIMEOUT_S:-120}"
POLL_S="${ROUNDTRIP_POLL_S:-5}"

refuse() { printf 'refused\t%s\t%s\n' "$1" "${2:-}"; exit "$EXIT_REFUSED"; }

usage() {
  cat <<'EOF'
roundtrip-alert.sh — staging-only alert round-trip harness (case_56fff4c6)

  roundtrip-alert.sh [--id ID]

Env:
  ROUNDTRIP_URL            staging sink POST target (required)
  ROUNDTRIP_VERIFY_URL     verify channel GET target (required: no verify, no pass)
  ROUNDTRIP_BEARER         sink bearer (optional; travels in curl config only)
  ROUNDTRIP_TIMEOUT_S      give up after N seconds (default 120)
  ROUNDTRIP_POLL_S         poll every N seconds (default 5)

Exits: 0 round-trip proved · 1 lost (finding) · 2 refused · 5 unmeasured
EOF
}

RID="roundtrip-$(date -u +%Y%m%dT%H%M%SZ)-$$"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) [[ $# -ge 2 && -n "${2:-}" ]] || refuse bad_usage "--id needs a value"; RID="$2"; shift 2;;
    -h|--help|help) usage; exit "$EXIT_OK";;
    *) refuse bad_usage "unknown argument '$1'";;
  esac
done

[[ "$RID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
  || refuse bad_usage "roundtrip id '$RID' is not a safe token"
command -v curl >/dev/null 2>&1 \
  || { printf 'refused\tcurl_missing\tcurl is required\n'; exit "$EXIT_UNMEASURED"; }
command -v python3 >/dev/null 2>&1 \
  || { printf 'refused\tpython_missing\tpython3 is required\n'; exit "$EXIT_UNMEASURED"; }
[[ -n "${ROUNDTRIP_URL:-}" ]] || refuse bad_usage "ROUNDTRIP_URL is unset"
[[ -n "${ROUNDTRIP_VERIFY_URL:-}" ]] || refuse bad_usage "ROUNDTRIP_VERIFY_URL is unset (a trigger without a verify is fire-and-forget)"
[[ "$ROUNDTRIP_URL" =~ ^https?:// ]] || refuse bad_usage "ROUNDTRIP_URL must be an http(s) URL"
[[ "$ROUNDTRIP_VERIFY_URL" =~ ^https?:// ]] || refuse bad_usage "ROUNDTRIP_VERIFY_URL must be an http(s) URL"
[[ "${ROUNDTRIP_URL,,}" == *prod* ]] \
  && refuse production_target "ROUNDTRIP_URL smells like production"
[[ "${ROUNDTRIP_VERIFY_URL,,}" == *prod* ]] \
  && refuse production_target "ROUNDTRIP_VERIFY_URL smells like production"

CFG_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/roundtrip.XXXXXXXX")" \
  || { printf 'refused\ttmp_unavailable\tcould not create a private temp dir\n'; exit "$EXIT_UNMEASURED"; }
trap 'rm -rf "$CFG_DIR"' EXIT

post_cfg="$CFG_DIR/post.cfg"; get_cfg="$CFG_DIR/get.cfg"
{
  printf 'url = "%s"\n' "$ROUNDTRIP_URL"
  printf 'request = "POST"\n'
  printf 'header = "Content-Type: application/json"\n'
  [[ -n "${ROUNDTRIP_BEARER:-}" ]] \
    && printf 'header = "Authorization: Bearer %s"\n' "$ROUNDTRIP_BEARER"
  printf 'max-time = "%s"\n' "${ROUNDTRIP_TIMEOUT_S:-15}"
  printf 'silent\nshow-error\n'
} > "$post_cfg"
{
  printf 'url = "%s"\n' "$ROUNDTRIP_VERIFY_URL"
  printf 'request = "GET"\n'
  [[ -n "${ROUNDTRIP_BEARER:-}" ]] \
    && printf 'header = "Authorization: Bearer %s"\n' "$ROUNDTRIP_BEARER"
  printf 'max-time = "%s"\n' "${ROUNDTRIP_TIMEOUT_S:-15}"
  printf 'silent\nshow-error\n'
} > "$get_cfg"
chmod 0600 "$post_cfg" "$get_cfg"

BODY="$(python3 -c 'import json,sys; print(json.dumps({"alertname": "RoundtripCanary", "severity": "warning", "instance": "staging", "roundtrip_id": sys.argv[1], "summary": "case_56fff4c6 staging round-trip canary (never prod)"}))' "$RID")"
printf '%s' "$BODY" > "$CFG_DIR/body.json"
chmod 0600 "$CFG_DIR/body.json"

# NOTE: --config and --data-binary take FILE PATHS only — the bearer and the
# URLs never appear in this argv. Do not rewrite this as bare-URL curl.
posted_at=""
if http="$(curl --config "$post_cfg" --data-binary "@$CFG_DIR/body.json" -o /dev/null -w '%{http_code}' 2>"$CFG_DIR/post.err")"; then
  case "$http" in
    2*) posted_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" ;;
    *) printf 'ROUNDTRIP-FAIL\t%s\tposted=never\treason=sink_http_%s\n' "$RID" "$http"; exit "$EXIT_FAIL" ;;
  esac
else
  printf 'ROUNDTRIP-FAIL\t%s\tposted=never\treason=sink_unreachable\n' "$RID"; exit "$EXIT_FAIL"
fi

deadline=$(( $(date -u +%s) + TIMEOUT_S ))
while (( $(date -u +%s) < deadline )); do
  if body="$(curl --config "$get_cfg" 2>/dev/null)" && grep -qF "$RID" <<<"$body"; then
    printf 'ROUNDTRIP-OK\t%s\tposted=%s\tobserved=%s\n' \
      "$RID" "$posted_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit "$EXIT_OK"
  fi
  sleep "$POLL_S"
done
printf 'ROUNDTRIP-FAIL\t%s\tposted=%s\treason=never_observed\n' "$RID" "$posted_at"
exit "$EXIT_FAIL"
