#!/usr/bin/env bash
# evaluate-runner-disk.sh — pure evaluator for the platform_runner-disk Gatus
# module (case_71e7a4e9, thresholds from the case_a814f742 document).
#
# Reads ONE combined JSON document (the read-only `runner_disk_triage.sh
# --format json` output plus a top-level `vms` object mapping each CI VM name
# to its `lxc exec <vm> -- df -P /` capture) and prints one verdict JSON
# object on stdout. No host access, no network, no credentials: stdin/file
# in, verdict out. The cron wrapper `push-runner-disk.sh` builds the input
# and turns the verdict into a Gatus push; this file never pushes.
#
# Input shapes (all tolerant; anything unparseable is UNKNOWN, never green):
#   df / df_inode : `df -h` / `df -i` text; the row mounted on `/` decides.
#                   (Other mounts are ignored: snap squashfs rows sit at 100%
#                   by design, so a max-across-all-rows rule would page
#                   forever. The case_231b8224 killer was the host root.)
#   lxd_usage     : per-pool `lxc storage info` text (`space used:` /
#                   `total space:` per pool). Fallback: `size N used M`
#                   pairs in the `lxd` text. Only the exact triage sentinel
#                   `UNAVAILABLE: neither lxc nor zfs present` (host has no
#                   LXD) skips the pool check. Every `== pool X ==` block must
#                   parse or the verdict is UNKNOWN naming X: a failed
#                   `lxc storage info` is a blind spot, never a skip.
#   vms           : {"<name>": "<df -P / text>"} per VM, or
#                   {"<name>": {"error": "<short cause>"}} when `lxc exec`
#                   failed. A VM with no measurable root disk is UNKNOWN.
#
# Thresholds (case_a814f742 bounds, env-overridable constants):
#   RUNNER_DISK_WARN_PCT (default 80), RUNNER_DISK_CRIT_PCT (default 90),
#   or --warn-pct / --crit-pct flags (flags win). Ties trip the band (>=).
#
# Verdict JSON: {"verdict": "healthy|warning|critical|unknown",
#   "worst_pct": N|null, "warn_pct": 80, "crit_pct": 90, "reasons": [...]}.
# Exit: 0 healthy; 1 warning/critical/unknown (all push DOWN); 2 refusal
# (missing input, malformed JSON, bad thresholds) before any health claim.
set -uo pipefail

INPUT=""
WARN_PCT="${RUNNER_DISK_WARN_PCT:-80}"
CRIT_PCT="${RUNNER_DISK_CRIT_PCT:-90}"

usage() {
  cat <<'USAGE'
usage: evaluate-runner-disk.sh --input FILE [--warn-pct N] [--crit-pct N]

  --input FILE  combined triage+vms JSON document (required)
  --warn-pct N  WARNING at or above N percent (default: $RUNNER_DISK_WARN_PCT or 80)
  --crit-pct N  CRITICAL at or above N percent (default: $RUNNER_DISK_CRIT_PCT or 90)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input) shift; [[ $# -gt 0 ]] || { echo "ERROR: --input needs a value" >&2; exit 2; }; INPUT="$1" ;;
    --warn-pct) shift; [[ $# -gt 0 ]] || { echo "ERROR: --warn-pct needs a value" >&2; exit 2; }; WARN_PCT="$1" ;;
    --crit-pct) shift; [[ $# -gt 0 ]] || { echo "ERROR: --crit-pct needs a value" >&2; exit 2; }; CRIT_PCT="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$INPUT" ]] \
  || { echo "ERROR: --input is required" >&2; exit 2; }
[[ -f "$INPUT" ]] \
  || { echo "ERROR: input '$INPUT' is not a file (unmeasurable, never green)" >&2; exit 2; }
for n in "$WARN_PCT" "$CRIT_PCT"; do
  [[ "$n" =~ ^[1-9][0-9]?$ ]] || { echo "ERROR: thresholds must be integers 1-99" >&2; exit 2; }
done
[[ "$WARN_PCT" -lt "$CRIT_PCT" ]] || { echo "ERROR: warn ($WARN_PCT) must be below crit ($CRIT_PCT)" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "ERROR: python3 required" >&2; exit 2; }

# MUTATION-ANCHOR-START: threshold bands (the offline suite weakens each
# comparison to prove the 79/80 and 89/90 boundary cases are load-bearing:
# a mutant must mis-score the exact-boundary fixture healthy).
python3 - "$INPUT" "$WARN_PCT" "$CRIT_PCT" <<'PY'
import json, re, sys

input_path, warn, crit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

try:
    with open(input_path, encoding="utf-8") as fh:
        doc = json.load(fh)
except Exception as exc:
    print(json.dumps({"verdict": "unknown", "worst_pct": None,
                       "warn_pct": warn, "crit_pct": crit,
                       "reasons": [f"UNKNOWN input is not JSON ({exc})"]}))
    sys.exit(1)
if not isinstance(doc, dict):
    print(json.dumps({"verdict": "unknown", "worst_pct": None,
                       "warn_pct": warn, "crit_pct": crit,
                       "reasons": ["UNKNOWN input is not a JSON object"]}))
    sys.exit(1)

unknown, over_warn, over_crit, worst = [], [], [], None

def note_pct(p):
    global worst
    if p is not None and (worst is None or p > worst):
        worst = p

def root_pct(text):
    """Max Use% on rows mounted at `/`; None when no such row parses."""
    if not isinstance(text, str):
        return None
    found = None
    for line in text.splitlines():
        toks = line.split()
        if len(toks) < 2 or toks[-1] != "/":
            continue
        for tok in toks:
            m = re.fullmatch(r"(\d+)%", tok)
            if m:
                pct = int(m.group(1))
                found = pct if found is None else max(found, pct)
                break
    return found

def band(pct, label):
    if pct >= crit:
        over_crit.append(f"CRITICAL {label} at {pct}% (crit {crit})")
    elif pct >= warn:
        over_warn.append(f"WARNING {label} at {pct}% (warn {warn})")

# --- host root disk + inodes (the case_231b8224 signal) ---------------------------
host = root_pct(doc.get("df"))
if host is None:
    unknown.append("UNKNOWN host df has no measurable / row")
else:
    note_pct(host)
    band(host, "host /")

inode = root_pct(doc.get("df_inode"))
if inode is None:
    unknown.append("UNKNOWN host df_inode has no measurable / row")
else:
    note_pct(inode)
    band(inode, "host / inodes")

# --- LXD pool used/total ------------------------------------------------------
def to_bytes(num, unit):
    u = (unit or "B").upper().replace("IB", "B").replace("I", "")
    u = u[0] if u else "B"
    base = 1024 if "I" in (unit or "").upper() else 1000
    return float(num) * (base ** {"B": 0, "K": 1, "M": 2, "G": 3,
                                  "T": 4, "P": 5, "E": 6}[u])

SIZE_RE = r"([\d.]+)\s*([KMGTPE]?i?B|[KMGTPE])\b"

# The ONLY skip: triage prints exactly this sentinel when the host has neither
# lxc nor zfs. A bare "unavailable" elsewhere ("info unavailable", "lxc storage
# info unavailable") means lxc IS present and the probe failed: a blind spot,
# never a skip. Anchored to a line start so quoted prose cannot trigger it.
NO_LXD_RE = re.compile(r"^UNAVAILABLE: neither lxc nor zfs present", re.M)
POOL_HDR_RE = re.compile(r"== pool (.+?) ==")

def pool_pcts(usage_text, lxd_text):
    """-> (pcts, skip, problems). skip is True only for a host with no LXD;
    problems names every pool whose used/total could not be measured."""
    usage = usage_text if isinstance(usage_text, str) else ""
    pcts, problems = [], []
    # split() with one group: [preamble, name1, body1, name2, body2, ...].
    parts = POOL_HDR_RE.split(usage)
    for i in range(1, len(parts), 2):
        name, block = parts[i], parts[i + 1]
        used = re.search(r"space used:\s*" + SIZE_RE, block)
        total = re.search(r"total space:\s*" + SIZE_RE, block)
        tot = to_bytes(total.group(1), total.group(2)) if total else 0
        if used and tot > 0:
            pcts.append(round(to_bytes(used.group(1), used.group(2))
                              / tot * 100, 1))
        else:
            problems.append(f"UNKNOWN LXD pool {name} used/total unparseable")
    if pcts or problems:
        # Pool blocks exist, so lxc answered `storage list`: every block must
        # score. One unscored pool must not hide behind its healthy sibling.
        return pcts, False, problems
    if isinstance(lxd_text, str):
        # (used, total) pairs in document order for both word orders.
        pairs = []
        for m in re.finditer(r"size\s*" + SIZE_RE + r"\s*used\s*" + SIZE_RE,
                             lxd_text):
            pairs.append(((m.group(3), m.group(4)), (m.group(1), m.group(2))))
        for m in re.finditer(r"used\s*" + SIZE_RE + r".{0,40}?size\s*"
                             + SIZE_RE, lxd_text):
            pairs.append(((m.group(1), m.group(2)), (m.group(3), m.group(4))))
        for (un, uu), (tn, tu) in pairs:
            tot = to_bytes(tn, tu)
            if tot > 0:
                pcts.append(round(to_bytes(un, uu) / tot * 100, 1))
    if pcts:
        return pcts, False, []
    if NO_LXD_RE.search(usage):
        return [], True, []
    return [], False, ["UNKNOWN LXD pool used/total unparseable"]

pcts, no_lxd, pool_problems = pool_pcts(doc.get("lxd_usage"), doc.get("lxd"))
unknown.extend(pool_problems)
if not no_lxd:
    for p in pcts:
        note_pct(p)
        band(p, "LXD pool")

# --- per-VM root disks ---------------------------------------------------------
vms = doc.get("vms")
if not isinstance(vms, dict) or not vms:
    unknown.append("UNKNOWN no per-VM disk captures in input")
else:
    for name in sorted(vms):
        entry = vms[name]
        text = entry.get("df") if isinstance(entry, dict) else entry
        if isinstance(entry, dict) and entry.get("error"):
            unknown.append(f"UNKNOWN vm {name} unreachable ({entry['error']})")
            continue
        pct = root_pct(text)
        if pct is None:
            unknown.append(f"UNKNOWN vm {name} root disk unmeasurable")
        else:
            note_pct(pct)
            band(pct, f"vm {name} /")

if unknown:
    verdict = "unknown"
elif over_crit:
    verdict = "critical"
elif over_warn:
    verdict = "warning"
else:
    verdict = "healthy"
reasons = unknown + over_crit + over_warn
print(json.dumps({"verdict": verdict, "worst_pct": worst,
                  "warn_pct": warn, "crit_pct": crit, "reasons": reasons}))
sys.exit(0 if verdict == "healthy" else 1)
PY
# MUTATION-ANCHOR-END
