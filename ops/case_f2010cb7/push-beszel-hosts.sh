#!/usr/bin/env bash
# push-beszel-hosts.sh — cron: per-host Beszel health → Gatus external endpoints.
#
# case_f2010cb7 (child of case_1ec443c8; slice of case_66436e78 deliverable 1).
# Source-only: no host deploy rides this change; the operator installs the
# cron line separately. Sibling leaves own the GARM pool module (case_b24794c2)
# and prod-app endpoints (case_73259b15); this leaf is the per-host Beszel-API
# bridge only.
#
# What it does, once per run (cron: */5 * * * *):
#   1. Loads one systems-JSON payload — either the file named as $1, the
#      operator's BESZEL_SOURCE_CMD, or a GET of BESZEL_API_URL (the full
#      URL that returns the systems JSON; no path is invented here).
#   2. Evaluates every enrolled host on disk %, inode %, CPU %, memory %,
#      load15-per-cpu, failed systemd units, container restarts, and a
#      linear disk-growth forecast to full.
#   3. Pushes one Gatus external endpoint per host (`beszel_<host>`,
#      success=true|false) via the shared _gatus_push.sh helper, so the
#      existing Gatus custom provider carries host alerts into Akmatori.
#      The key is the Gatus external key for the emitted stanza below
#      (`beszel` + `_` + sanitized host, case_6d874ecd): a key without the
#      underscore separator can never match a Gatus endpoint (404).
#
# NORMALIZED INPUT CONTRACT (what the payload must look like, however it
# was fetched — an array, or {"systems": [...]}):
#   [{"name":"node7b1ff338","disk_used_percent":42.5,"inode_used_percent":12.0,
#     "cpu_used_percent":25.0,"mem_used_percent":60.0,"load15_per_cpu":0.8,
#     "failed_units":0,"container_restarts_24h":0,
#     "disk_total_bytes":107374182400,
#     "disk_history":[{"t":1696000000,"used_bytes":40000000000}, ...]}, ...]
# `failed_units` takes a count or a list of unit names. `disk_history` plus
# `disk_total_bytes` enable the forecast; without them the forecast is
# skipped (never scored healthy-or-not on missing history).
# `inode_used_percent` and `container_restarts_24h` are OPTIONAL the same
# way: Beszel records neither (no inode field; container stats carry no
# restart count — see beszel_adapter.py provenance), so an adapter-fed
# payload can never supply them honestly. Absent = skipped, never scored;
# present-but-not-a-number still fails CLOSED like any malformed field.
# Every other field is required: a host missing one is pushed
# success=false naming the field — missing data fails CLOSED, never healthy.
#
# Env:
#   push mode:  GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh),
#               BESZEL_HOSTS (optional whitespace list; when set, every
#               enrolled host must appear in the payload or it is pushed
#               false as a silent host — a host Beszel stops reporting is
#               itself an incident),
#               BESZEL_SOURCE_CMD | BESZEL_API_URL + BESZEL_API_KEY,
#               thresholds below.
#   thresholds: BESZEL_DISK_CRIT=90 BESZEL_INODE_CRIT=90 BESZEL_CPU_CRIT=90
#               BESZEL_MEM_CRIT=90 BESZEL_LOAD_PER_CPU_CRIT=2.0
#               BESZEL_FORECAST_DAYS=14 BESZEL_RESTARTS_CRIT=5
#
# THRESHOLD TABLE (defaults; env overrides above win):
#   disk>=90% | inode>=90% | cpu>=90% | mem>=90% | load15-per-cpu>=2.0 |
#   failed_units>0 | container restarts>5/24h | disk-full forecast<14d.
#   Missing field or silent enrolled host fails CLOSED (never healthy).
# OWNER devops (host/disk/fleet saturation; host action via an Operator:
# card to the CEO — triage never touches the host).
# RUNBOOK docs/runbooks/garm-ci-oncall-alert-to-action.md (§3 names the
# Beszel disk alert on a runner host; escalation table names DevOps).
# case_7ec9e924 wires OWNER + RUNBOOK into every --emit-gatus-yaml description,
# mirroring the case_9b48cd0a gatus-config.yaml convention.
# NON-DISCLOSURE: BESZEL_API_KEY and GATUS_EXTERNAL_TOKEN travel in 0600
# curl config files, never argv (/proc/<pid>/cmdline is world-readable).
# UNTRUSTED INPUT: host names are sanitized exactly the way Gatus derives
# an external endpoint key (v5.37.0 config/key/key.go: lowercase, then each
# of / _ . , space # + & becomes '-'), prefixed with `beszel_`;
# reason strings are truncated by the shared helper.
#
# Exit: 0 every evaluated host healthy and pushed; 1 at least one host
# unhealthy, silent, or unpushable; 2 refusal (bad env, unreadable payload,
# missing tooling) before any health claim.
#   push-beszel-hosts.sh [--emit-gatus-yaml] [payload.json]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-beszel-hosts: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
. "$GATUS_HELPER"

EMIT_YAML=0
PAYLOAD_FILE=""
for arg in "$@"; do
  case "$arg" in
    --emit-gatus-yaml) EMIT_YAML=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) [[ -z "$PAYLOAD_FILE" ]] || { echo "push-beszel-hosts: refusing — one payload file at most" >&2; exit 2; }
       PAYLOAD_FILE="$arg" ;;
  esac
done

command -v jq >/dev/null 2>&1 \
  || { echo "push-beszel-hosts: refusing — jq required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-beszel-hosts: refusing — python3 required" >&2; exit 2; }

for var in BESZEL_DISK_CRIT BESZEL_INODE_CRIT BESZEL_CPU_CRIT BESZEL_MEM_CRIT; do
  [[ "${!var:-90}" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
    || { echo "push-beszel-hosts: refusing — $var must be numeric" >&2; exit 2; }
done
DISK_CRIT="${BESZEL_DISK_CRIT:-90}"
INODE_CRIT="${BESZEL_INODE_CRIT:-90}"
CPU_CRIT="${BESZEL_CPU_CRIT:-90}"
MEM_CRIT="${BESZEL_MEM_CRIT:-90}"
LOAD_CRIT="${BESZEL_LOAD_PER_CPU_CRIT:-2.0}"
[[ "$LOAD_CRIT" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { echo "push-beszel-hosts: refusing — BESZEL_LOAD_PER_CPU_CRIT must be numeric" >&2; exit 2; }
FORECAST_DAYS="${BESZEL_FORECAST_DAYS:-14}"
[[ "$FORECAST_DAYS" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { echo "push-beszel-hosts: refusing — BESZEL_FORECAST_DAYS must be numeric" >&2; exit 2; }
RESTARTS_CRIT="${BESZEL_RESTARTS_CRIT:-5}"
[[ "$RESTARTS_CRIT" =~ ^[0-9]+$ ]] \
  || { echo "push-beszel-hosts: refusing — BESZEL_RESTARTS_CRIT must be a whole number" >&2; exit 2; }

# --- load payload -----------------------------------------------------------
PAYLOAD=""
if [[ -n "$PAYLOAD_FILE" ]]; then
  [[ -r "$PAYLOAD_FILE" ]] \
    || { echo "push-beszel-hosts: refusing — cannot read $PAYLOAD_FILE" >&2; exit 2; }
  PAYLOAD="$(cat "$PAYLOAD_FILE")"
elif [[ -n "${BESZEL_SOURCE_CMD:-}" ]]; then
  PAYLOAD="$(bash -c "$BESZEL_SOURCE_CMD" 2>&1)" \
    || { echo "push-beszel-hosts: refusing — BESZEL_SOURCE_CMD failed" >&2; exit 2; }
elif [[ -n "${BESZEL_API_URL:-}" ]]; then
  [[ -n "${BESZEL_API_KEY:-}" ]] \
    || { echo "push-beszel-hosts: refusing — BESZEL_API_KEY is unset" >&2; exit 2; }
  cfg_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/beszel_fetch.XXXXXXXX")" \
    || { echo "push-beszel-hosts: refusing — no private temp dir" >&2; exit 2; }
  # Top level: RETURN fires only in functions, so EXIT removes the dir
  # holding the Bearer curl config on every path, including refusals.
  # shellcheck disable=SC2064
  trap "rm -rf '$cfg_dir'" EXIT
  cfg="$(umask 077; mktemp "$cfg_dir/curlcfg.XXXXXXXX")" \
    || { echo "push-beszel-hosts: refusing — no curl config" >&2; exit 2; }
  {
    printf 'url = "%s"\n' "$BESZEL_API_URL"
    printf 'header = "Authorization: Bearer %s"\n' "$BESZEL_API_KEY"
    printf 'max-time = "%s"\n' "${BESZEL_FETCH_TIMEOUT:-15}"
    printf 'silent\nshow-error\n'
  } > "$cfg"
  chmod 0600 "$cfg"
  PAYLOAD="$(curl --config "$cfg" 2>&1)" \
    || { echo "push-beszel-hosts: refusing — Beszel fetch failed" >&2; exit 2; }
else
  echo "push-beszel-hosts: refusing — payload file, BESZEL_SOURCE_CMD, or BESZEL_API_URL required" >&2
  exit 2
fi

SYSTEMS="$(jq -c 'if type == "array" then . else (.systems // empty) end' <<<"$PAYLOAD" 2>&1)" \
  || { echo "push-beszel-hosts: refusing — payload is not JSON" >&2; exit 2; }
[[ -n "$SYSTEMS" && "$SYSTEMS" != "null" && "$SYSTEMS" != "" ]] \
  || { echo "push-beszel-hosts: refusing — payload holds no host array (want [...] or {\"systems\": [...]})" >&2; exit 2; }
COUNT="$(jq 'length' <<<"$SYSTEMS" 2>/dev/null)" \
  || { echo "push-beszel-hosts: refusing — host list unreadable" >&2; exit 2; }
(( COUNT > 0 )) \
  || { echo "push-beszel-hosts: refusing — empty host list proves nothing" >&2; exit 2; }

# --- host enrollment --------------------------------------------------------
read -r -a ENROLLED <<<"${BESZEL_HOSTS:-}" || true
have_host() {
  local want="$1"
  jq -e --arg n "$want" 'any(.name == $n)' <<<"$SYSTEMS" >/dev/null 2>&1
}

# --- per-host evaluation ----------------------------------------------------
# Prints unhealthy reasons, one per line; empty output = healthy.
# A host missing a required field reports it (fail closed).
eval_host() {
  local host_json="$1"
  local name reasons missing
  name="$(jq -r '.name // empty' <<<"$host_json")"
  [[ -n "$name" ]] || { echo "host record without a name"; return; }
  reasons=(); missing=()
  local f
  for f in disk_used_percent cpu_used_percent mem_used_percent load15_per_cpu failed_units; do
    jq -e --arg f "$f" '
      .[$f] != null and
      (if $f == "failed_units" then ((.[$f] | type) == "number" or ((.[$f] | type) == "array"))
       else ((.[$f] | type) == "number") end)' <<<"$host_json" >/dev/null 2>&1 \
      || missing+=("$f")
  done
  # Optional fields (no Beszel-native provenance): absent = skipped below,
  # present-but-not-a-number = malformed, fails closed like a missing field.
  for f in inode_used_percent container_restarts_24h; do
    jq -e --arg f "$f" 'if .[$f] == null then true else ((.[$f] | type) == "number") end' \
      <<<"$host_json" >/dev/null 2>&1 || missing+=("$f")
  done
  if (( ${#missing[@]} > 0 )); then
    printf 'missing field(s): %s\n' "${missing[*]}"
    return
  fi
  local disk inode cpu mem load restarts
  disk="$(jq -r '.disk_used_percent' <<<"$host_json")"
  inode="$(jq -r '.inode_used_percent // "-"' <<<"$host_json")"
  cpu="$(jq -r '.cpu_used_percent' <<<"$host_json")"
  mem="$(jq -r '.mem_used_percent' <<<"$host_json")"
  load="$(jq -r '.load15_per_cpu' <<<"$host_json")"
  restarts="$(jq -r '.container_restarts_24h // "-"' <<<"$host_json")"
  python3 - "$disk" "$DISK_CRIT" "$inode" "$INODE_CRIT" "$cpu" "$CPU_CRIT" \
            "$mem" "$MEM_CRIT" "$load" "$LOAD_CRIT" <<'PY'
import sys
def num(s):  # "-" = optional field absent: skip, never score
    return None if s == "-" else float(s)
disk, ino, cpu, mem, load = (num(sys.argv[i]) for i in (1, 3, 5, 7, 9))
dc, ic, cc, mc, lc = (float(sys.argv[i]) for i in (2, 4, 6, 8, 10))
if disk >= dc: print(f"disk {disk:g}% >= {dc:g}%")
if ino is not None and ino >= ic: print(f"inode {ino:g}% >= {ic:g}%")
if cpu >= cc: print(f"cpu {cpu:g}% >= {cc:g}%")
if mem >= mc: print(f"mem {mem:g}% >= {mc:g}%")
if load >= lc: print(f"load15/cpu {load:g} >= {lc:g}")
PY
  local units_count units_names
  if [[ "$(jq -r '.failed_units | type' <<<"$host_json")" == "array" ]]; then
    units_count="$(jq -r '.failed_units | length' <<<"$host_json")"
    units_names="$(jq -r '.failed_units | map(tostring) | join(",")' <<<"$host_json")"
  else
    units_count="$(jq -r '.failed_units' <<<"$host_json")"
    units_names=""
  fi
  if [[ "$units_count" =~ ^[0-9]+$ ]] && (( units_count > 0 )); then
    if [[ -n "$units_names" ]]; then
      echo "failed units ($units_count): $units_names"
    else
      echo "failed units: $units_count"
    fi
  fi
  if [[ "$restarts" =~ ^[0-9]+$ ]] && (( restarts > RESTARTS_CRIT )); then
    echo "container restarts $restarts/24h > $RESTARTS_CRIT"
  fi
  # Disk-growth forecast: linear fit over disk_history; needs total to
  # project days-to-full. Absent/short history skips, never scores.
  if jq -e '.disk_history != null and .disk_total_bytes != null' <<<"$host_json" >/dev/null 2>&1; then
    local hist total
    hist="$(jq -c '.disk_history' <<<"$host_json")"
    total="$(jq -r '.disk_total_bytes' <<<"$host_json")"
    FORECAST_DAYS="$FORECAST_DAYS" python3 - "$hist" "$total" <<'PY'
import json, os, sys
try:
    pts = json.loads(sys.argv[1]); total = float(sys.argv[2])
    days = float(os.environ["FORECAST_DAYS"])
except (ValueError, TypeError):
    print("unreadable disk_history/disk_total_bytes"); sys.exit(0)
if not isinstance(pts, list) or len(pts) < 2:
    print("disk_history too short for a forecast"); sys.exit(0)
try:
    xy = [(float(p["t"]), float(p["used_bytes"])) for p in pts]
except (KeyError, TypeError, ValueError):
    print("unreadable disk_history/disk_total_bytes"); sys.exit(0)
n = len(xy); sx = sum(x for x, _ in xy); sy = sum(y for _, y in xy)
den = sum((x - sx / n) ** 2 for x, _ in xy)
if den <= 0:
    sys.exit(0)  # identical timestamps: no slope to project
slope = sum((x - sx / n) * (y - sy / n) for x, y in xy) / den
if slope <= 0:
    sys.exit(0)  # not growing: nothing to forecast
left = total - xy[-1][1]
if left <= 0:
    print("disk already at or over total")
else:
    eta = left / slope / 86400
    if eta < days:
        print(f"disk full in ~{eta:.1f}d at current growth (< {days:g}d)")
PY
  fi
}

sanitize_key() {
  # The Gatus external key for group `beszel` and this host: lowercase, then
  # each of / _ . , space # + & becomes '-' (v5.37.0 config/key/key.go), then
  # anything still outside [a-z0-9-] becomes '-' so the key always satisfies
  # the gatus_push charset and the emitted `name:` stays plain-YAML-safe.
  # Leading/trailing dashes are trimmed and an empty result refuses rather
  # than pushing under a bare prefix. LC_ALL=C is function-local so the
  # mapping is byte-deterministic in any runner locale.
  local s LC_ALL=C
  s="$(tr '[:upper:]' '[:lower:]' <<<"$1" | tr '/_., #+&' '--------')"
  s="$(sed 's/[^a-z0-9-]/-/g; s/^-\+//; s/-\+$//' <<<"$s")"
  [[ -n "$s" ]] || return 1
  printf 'beszel_%s' "$s"
}

# --- emit mode: Gatus external-endpoint stanzas, no pushes ------------------
if (( EMIT_YAML )); then
  NAMES="$(jq -r '.[].name // empty' <<<"$SYSTEMS")"
  [[ -n "$NAMES" ]] || { echo "push-beszel-hosts: refusing — no host names to emit" >&2; exit 2; }
  while read -r h; do
    [[ -n "$h" ]] || continue
    key="$(sanitize_key "$h")" || { echo "push-beszel-hosts: refusing — hostname '$h' sanitizes to empty" >&2; exit 2; }
    short="${key#beszel_}"
    cat <<EOF
  - name: $short
    group: beszel
    token: "\${GATUS_EXTERNAL_TOKEN}"
    heartbeat:
      interval: 10m
    alerts:
      - type: discord
        description: "Beszel host $h unhealthy (see push-beszel-hosts log). THRESHOLD disk/inode/cpu/mem>=90%, load15/cpu>=2.0, restarts>5/24h, disk-full<14d. OWNER devops. RUNBOOK docs/runbooks/garm-ci-oncall-alert-to-action.md"
      - type: custom
        description: "Beszel host $h unhealthy (see push-beszel-hosts log). THRESHOLD disk/inode/cpu/mem>=90%, load15/cpu>=2.0, restarts>5/24h, disk-full<14d. OWNER devops. RUNBOOK docs/runbooks/garm-ci-oncall-alert-to-action.md"
EOF
  done <<<"$NAMES"
  exit 0
fi

# --- push mode --------------------------------------------------------------
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-beszel-hosts: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-beszel-hosts: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

overall=0
push_one() {
  local host="$1" ok="$2" reason="$3"
  local key
  key="$(sanitize_key "$host")" \
    || { echo "push-beszel-hosts: $host: cannot derive an endpoint key" >&2; overall=1; return; }
  if [[ "$ok" == true ]]; then
    gatus_push "$key" true || overall=1
  else
    gatus_push "$key" false "$reason" || overall=1
    overall=1
  fi
}

while read -r host_json; do
  [[ -n "$host_json" ]] || continue
  name="$(jq -r '.name // empty' <<<"$host_json")"
  [[ -n "$name" ]] || { echo "push-beszel-hosts: nameless record pushed false" >&2; overall=1; continue; }
  mapfile -t reasons < <(eval_host "$host_json")
  if (( ${#reasons[@]} == 0 )); then
    push_one "$name" true ""
  else
    push_one "$name" false "$name: $(IFS='; '; echo "${reasons[*]}")"
  fi
done < <(jq -c '.[]' <<<"$SYSTEMS")

for want in ${ENROLLED[@]+"${ENROLLED[@]}"}; do
  [[ -n "$want" ]] || continue
  if ! have_host "$want"; then
    # Silent host: Beszel stopped reporting it. Fail closed.
    key="$(sanitize_key "$want")" || key="beszel_unknown"
    gatus_push "$key" false "$want: silent — absent from Beszel payload" || true
    overall=1
  fi
done

exit "$overall"
