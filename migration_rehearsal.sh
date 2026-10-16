#!/usr/bin/env bash
# migration_rehearsal.sh — prod-clone rehearsal driver (shared migration
# safety gates).
#
# WHAT IT DOES. Before a migration chain may ride a production deploy, the
# exact pending chain must be proven on a throwaway clone of production:
# branch off prod (PlanetScale branch) or restore a point-in-time branch,
# apply the pending chain EXACTLY as production would (same files, same
# order, same timeout preamble), report, then delete the clone. Evidence
# fresher than 24h is required by the production preflight gate.
#
# Subcommands (all offline unless stated):
#   plan --migrations-dir DIR --provider planetscale|pitr --out evidence.json
#       [--only tag,tag...]
#       Enumerate the pending chain (sorted *.sql, or exactly the --only
#       tags when the caller computed the pending subset from the ledger),
#       digest each file (sha256), and write a `planned` evidence document.
#       No cloud calls.
#   run --migrations-dir DIR --provider P --out evidence.json [--branch NAME]
#       [--only tag,tag...]
#       Full rehearsal against the provider. Requires the provider CLI and
#       credentials in the environment (see below); refuses (exit 2) when
#       they are absent, so CI without secrets plans but never pretends.
#       Fail-fast: the first failed apply stops the chain and every later
#       migration is recorded `not_attempted`, never applied after a
#       failure.
#   verify --evidence FILE [--max-age-hours 24] [--sql-hashes FILE]
#       The production preflight check: evidence status must be `passed`,
#       younger than the max age, and — when --sql-hashes is given — bound
#       to exactly the candidate chain (same tags, same digests), so
#       evidence for an unrelated chain cannot pass. A finished_at in the
#       future (beyond 5 minutes of clock skew) fails. Exit 0
#       fresh-and-passed-and-bound, 1 stale/failed/unbound, 2 unreadable
#       evidence.
#   clean --branch NAME --provider P
#       Delete the rehearsal branch (also attempted automatically by `run`).
#
# Provider credentials are read from the environment only and are NEVER
# printed: `planetscale` needs REHEARSAL_PSCALE_ORG, REHEARSAL_PSCALE_DB and
# a token via REHEARSAL_PSCALE_TOKEN or the `pscale` CLI auth; `pitr` needs
# REHEARSAL_PITR_ENDPOINT plus REHEARSAL_RESTORE_CMD (the caller-owned
# restore command, run with the branch name appended). Unknown providers
# are refused.
#
# Evidence schema (JSON): {version, provider, branch, base, migrations:
# [{tag, sha256, result}], status, started_at, finished_at, cleanup}.
# Times are UTC ISO-8601. Only tags, digests, counts and statuses are
# ever printed — never credential values, connection strings, or SQL.
#
# Exit: 0 ok | 1 verification/apply failure | 2 bad input or missing
# capability (REFUSED).
set -euo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"
EVIDENCE_VERSION=1

die() { echo "REFUSED: $ME: $*" >&2; exit 2; }
redact() { sed -E 's/(token|secret|password|passwd|key)=[^[:space:]"]+/\\1=<redacted>/gi'; }

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

default_branch() { printf 'rehearsal/%s' "$(date -u +%Y%m%dT%H%M%SZ)"; }

# chain_json DIR [ONLY]: JSON array of {"tag","sha256"} sorted by file
# name. When ONLY (comma-separated tags) is given, exactly those tags form
# the chain and an unknown tag is refused — this is how a caller rehearses
# the ledger-computed pending subset instead of the whole directory.
chain_json() {
  local dir="$1" only="${2:-}"
  [[ -d "$dir" ]] || die "no such migrations dir: $dir"
  python3 - "$dir" "$only" <<'EOF'
import hashlib, json, sys
from pathlib import Path
want = None
if sys.argv[2]:
    want = sys.argv[2].split(",")
    if any(not t for t in want):
        sys.exit("--only holds an empty tag")
items = []
for p in sorted(Path(sys.argv[1]).glob("*.sql")):
    if not p.is_file():
        continue
    if want is not None and p.stem not in want:
        continue
    items.append({"tag": p.stem,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
if want is not None:
    missing = [t for t in want if t not in {m["tag"] for m in items}]
    if missing:
        sys.exit(f"--only names migrations not in the dir: {','.join(sorted(missing))}")
    items.sort(key=lambda m: want.index(m["tag"]))
if not items:
    sys.exit("no *.sql files in migrations dir (or --only matched none)")
print(json.dumps(items))
EOF
}

cmd="${1:-}"; shift || true
case "$cmd" in
  plan)
    DIR="migrations"; PROVIDER=""; OUT=""; ONLY=""
    while [[ $# -gt 0 ]]; do case "$1" in
      --migrations-dir) DIR="${2:-}"; shift 2 ;;
      --provider) PROVIDER="${2:-}"; shift 2 ;;
      --out) OUT="${2:-}"; shift 2 ;;
      --only) ONLY="${2:-}"; shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac; done
    [[ "$PROVIDER" == "planetscale" || "$PROVIDER" == "pitr" ]] \
      || die "--provider must be planetscale or pitr"
    [[ -n "$OUT" ]] || die "--out is required"
    CHAIN="$(chain_json "$DIR" "$ONLY")" || die "cannot enumerate $DIR"
    COUNT="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$CHAIN")" \
      || die "cannot digest the pending chain in $DIR"
    python3 - "$OUT" "$CHAIN" <<EOF
import json, sys
doc = {"version": $EVIDENCE_VERSION, "provider": "$PROVIDER",
       "branch": None, "base": "prod",
       "migrations": json.loads(sys.argv[2]),
       "status": "planned", "started_at": "$(now_utc)",
       "finished_at": None, "cleanup": None}
open(sys.argv[1], "w").write(json.dumps(doc, indent=2) + "\n")
EOF
    echo "ok: rehearsal planned: $COUNT migration(s), provider $PROVIDER -> $OUT"
    ;;
  run)
    DIR="migrations"; PROVIDER=""; OUT=""; BRANCH=""; ONLY=""
    while [[ $# -gt 0 ]]; do case "$1" in
      --migrations-dir) DIR="${2:-}"; shift 2 ;;
      --provider) PROVIDER="${2:-}"; shift 2 ;;
      --out) OUT="${2:-}"; shift 2 ;;
      --branch) BRANCH="${2:-}"; shift 2 ;;
      --only) ONLY="${2:-}"; shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac; done
    [[ "$PROVIDER" == "planetscale" || "$PROVIDER" == "pitr" ]] \
      || die "--provider must be planetscale or pitr"
    [[ -n "$OUT" ]] || die "--out is required"
    [[ -z "$BRANCH" ]] && BRANCH="$(default_branch)"
    CHAIN="$(chain_json "$DIR" "$ONLY")" || die "cannot enumerate $DIR"
    case "$PROVIDER" in
      planetscale)
        command -v pscale >/dev/null 2>&1 \
          || die "planetscale rehearsal needs the pscale CLI on PATH"
        [[ -n "${REHEARSAL_PSCALE_ORG:-}" && -n "${REHEARSAL_PSCALE_DB:-}" ]] \
          || die "planetscale rehearsal needs REHEARSAL_PSCALE_ORG and REHEARSAL_PSCALE_DB"
        [[ -n "${REHEARSAL_PSCALE_TOKEN:-}" ]] || pscale auth status >/dev/null 2>&1 \
          || die "planetscale rehearsal needs REHEARSAL_PSCALE_TOKEN or a logged-in pscale CLI"
        CREATE=(pscale branch create "$REHEARSAL_PSCALE_DB" "$BRANCH" --org "$REHEARSAL_PSCALE_ORG" --from prod)
        DELETE=(pscale branch delete "$REHEARSAL_PSCALE_DB" "$BRANCH" --org "$REHEARSAL_PSCALE_ORG" --force)
        ;;
      pitr)
        [[ -n "${REHEARSAL_PITR_ENDPOINT:-}" && -n "${REHEARSAL_RESTORE_CMD:-}" ]] \
          || die "pitr rehearsal needs REHEARSAL_PITR_ENDPOINT and REHEARSAL_RESTORE_CMD"
        # shellcheck disable=SC2206
        CREATE=(${REHEARSAL_RESTORE_CMD} "$BRANCH")
        DELETE=("true")
        ;;
    esac
    STARTED="$(now_utc)"
    echo "rehearsal: creating branch $BRANCH (provider $PROVIDER)"
    if ! "${CREATE[@]}" 2>&1 | redact >&2; then
      echo "FAIL: $ME: branch creation failed" >&2; exit 1
    fi
    # Apply the pending chain exactly as production would: same files, same
    # order, same timeout preamble. The caller-owned apply command reads
    # REHEARSAL_APPLY_CMD (run with one wrapped SQL file path appended);
    # without it the rehearsal refuses rather than simulating the apply.
    # Each file is wrapped with the canonical preamble through
    # migration_timeout_preamble.sh inject, so the rehearsal proves the
    # production injection path, not the bare files.
    if [[ -z "${REHEARSAL_APPLY_CMD:-}" ]]; then
      echo "REFUSED: $ME: REHEARSAL_APPLY_CMD is not set" >&2
      APPLY_RC=2
      RESULTS="[]"
    else
      HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      # shellcheck disable=SC2206
      APPLY=( ${REHEARSAL_APPLY_CMD} )
      WRAPDIR="$(mktemp -d)"; trap 'rm -rf "$WRAPDIR"' EXIT
      APPLY_RC=0; RESULTS="[]"
      # Fail-fast: apply the planned chain in order; the first failure
      # stops the chain and every later migration is recorded
      # `not_attempted` — a later migration must never be applied after an
      # earlier one failed, or the rehearsal would prove an order
      # production can never reproduce.
      mapfile -t CHAIN_FILES < <(python3 - "$DIR" "$CHAIN" <<'EOF'
import json, sys
from pathlib import Path
chain = json.loads(sys.argv[2])
by_tag = {p.stem: str(p) for p in Path(sys.argv[1]).glob("*.sql")}
for m in chain:
    print(by_tag[m["tag"]])
EOF
)
      [[ "${#CHAIN_FILES[@]}" -gt 0 ]] || { echo "FAIL: $ME: no *.sql files to apply in $DIR" >&2; APPLY_RC=1; }
      FAILED=0
      for f in "${CHAIN_FILES[@]}"; do
        tag="$(basename "$f" .sql)"
        if [[ "$FAILED" -eq 1 ]]; then
          res=not_attempted
        else
          wrapped="$WRAPDIR/$tag.sql"
          "$HERE/migration_timeout_preamble.sh" inject "$f" > "$wrapped"
          if timeout 600 "${APPLY[@]}" "$wrapped" 2>&1 | redact >&2; then
            res=applied
          else
            res=failed; APPLY_RC=1; FAILED=1
          fi
        fi
        RESULTS="$(python3 -c 'import json,sys; a=json.loads(sys.argv[1]); a.append({"tag":sys.argv[2],"result":sys.argv[3]}); print(json.dumps(a))' "$RESULTS" "$tag" "$res")"
        echo "rehearsal: $tag -> $res"
      done
    fi
    echo "rehearsal: deleting branch $BRANCH"
    if ! "${DELETE[@]}" 2>&1 | redact >&2; then
      echo "WARN: $ME: branch cleanup failed for $BRANCH — delete it manually" >&2
      CLEANUP="failed"
    else
      CLEANUP="deleted"
    fi
    STATUS="passed"; [[ "$APPLY_RC" -eq 0 ]] || STATUS="failed"
    python3 - "$OUT" "$CHAIN" "$RESULTS" <<EOF
import json, sys
chain = {m["tag"]: m["sha256"] for m in json.loads(sys.argv[2])}
results = {m["tag"]: m["result"] for m in json.loads(sys.argv[3])}
migs = [{"tag": t, "sha256": chain[t], "result": results.get(t, "not_attempted")}
        for t in sorted(chain)]
doc = {"version": $EVIDENCE_VERSION, "provider": "$PROVIDER",
       "branch": "$BRANCH", "base": "prod", "migrations": migs,
       "status": "$STATUS", "started_at": "$STARTED",
       "finished_at": "$(now_utc)", "cleanup": "$CLEANUP"}
open(sys.argv[1], "w").write(json.dumps(doc, indent=2) + "\n")
EOF
    echo "rehearsal: $STATUS, cleanup $CLEANUP -> $OUT"
    [[ "$STATUS" == "passed" ]] || exit 1
    ;;
  verify)
    EV=""; MAX_AGE=24; HASHES=""
    while [[ $# -gt 0 ]]; do case "$1" in
      --evidence) EV="${2:-}"; shift 2 ;;
      --max-age-hours) MAX_AGE="${2:-}"; shift 2 ;;
      --sql-hashes) HASHES="${2:-}"; shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac; done
    [[ -r "${EV:-}" ]] || die "--evidence must be a readable file"
    [[ "$MAX_AGE" =~ ^[0-9]+$ ]] || die "--max-age-hours must be an integer"
    if [[ -n "$HASHES" ]]; then
      [[ -r "$HASHES" ]] || die "--sql-hashes must be a readable file"
    fi
    python3 - "$EV" "$MAX_AGE" "$HASHES" <<'EOF'
import datetime, json, re, sys
try:
    doc = json.load(open(sys.argv[1]))
except (OSError, ValueError) as exc:
    sys.exit(f"REFUSED: unreadable evidence: {exc.__class__.__name__}")
if doc.get("status") != "passed":
    sys.exit(f"FAIL: rehearsal evidence status is {doc.get('status')!r}, want 'passed'")
try:
    finished = datetime.datetime.strptime(doc["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
except (KeyError, TypeError, ValueError):
    sys.exit("FAIL: rehearsal evidence has no parseable finished_at")
age = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds() / 3600
if age < -5 / 60:
    sys.exit(f"FAIL: rehearsal evidence finished_at is in the future ({-age:.1f}h ahead)")
limit = int(sys.argv[2])
if age > limit:
    sys.exit(f"FAIL: rehearsal evidence is {age:.1f}h old, older than {limit}h")
migs = doc.get("migrations", [])
not_applied = [m.get("tag") for m in migs if m.get("result") != "applied"]
if not_applied:
    sys.exit(f"FAIL: rehearsal evidence claims passed but {len(not_applied)} migration(s) "
             f"were not applied; first: {sorted(not_applied)[0]}")
if sys.argv[3]:
    pat = re.compile(r"([0-9a-f]{64})\s+\*?(?:.*/)?([^/\s]+)\.sql")
    candidate = {}
    for raw in open(sys.argv[3], encoding="utf-8"):
        line = raw.strip()
        if not line:
            continue
        m = pat.fullmatch(line)
        if not m:
            sys.exit(f"REFUSED: unparseable --sql-hashes line: {line[:80]}")
        candidate[m.group(2)] = m.group(1)
    evidenced = {m["tag"]: m.get("sha256") for m in migs}
    if evidenced != candidate:
        only_ev = sorted(set(evidenced) - set(candidate))
        only_cand = sorted(set(candidate) - set(evidenced))
        mism = sorted(t for t in set(evidenced) & set(candidate) if evidenced[t] != candidate[t])
        detail = only_ev[:1] + only_cand[:1] + mism[:1]
        sys.exit("FAIL: rehearsal evidence is not bound to this candidate chain "
                 f"(first divergence: {detail[0] if detail else 'empty evidence'})")
    print(f"ok: rehearsal evidence bound to the candidate chain ({len(migs)} migration(s))")
print(f"ok: rehearsal evidence fresh ({age:.1f}h < {limit}h), {len(migs)} migration(s) passed")
EOF
    ;;
  clean)
    PROVIDER=""; BRANCH=""
    while [[ $# -gt 0 ]]; do case "$1" in
      --provider) PROVIDER="${2:-}"; shift 2 ;;
      --branch) BRANCH="${2:-}"; shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac; done
    [[ -n "$BRANCH" ]] || die "--branch is required"
    case "$PROVIDER" in
      planetscale)
        command -v pscale >/dev/null 2>&1 || die "needs the pscale CLI on PATH"
        [[ -n "${REHEARSAL_PSCALE_ORG:-}" && -n "${REHEARSAL_PSCALE_DB:-}" ]] \
          || die "needs REHEARSAL_PSCALE_ORG and REHEARSAL_PSCALE_DB"
        pscale branch delete "$REHEARSAL_PSCALE_DB" "$BRANCH" \
          --org "$REHEARSAL_PSCALE_ORG" --force 2>&1 | redact >&2
        ;;
      *) die "--provider must be planetscale or pitr (pitr branches expire with the restore)" ;;
    esac
    echo "ok: branch cleanup requested: $BRANCH"
    ;;
  *) cat >&2 <<EOF
Usage: $ME {plan|run|verify|clean} [options]
  plan   --migrations-dir DIR --provider planetscale|pitr --out evidence.json [--only tag,...]
  run    --migrations-dir DIR --provider P --out evidence.json [--branch NAME] [--only tag,...]
  verify --evidence FILE [--max-age-hours 24] [--sql-hashes FILE]
  clean  --provider planetscale --branch NAME
EOF
    exit 2
    ;;
esac
