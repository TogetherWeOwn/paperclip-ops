#!/usr/bin/env bash
# migration_rehearsal.sh — prod-clone rehearsal driver (shared migration
# safety gates).
#
# WHAT IT DOES. Before a migration chain may ride a production deploy, the
# exact pending chain must be proven on a throwaway clone of production:
# branch off prod (PlanetScale branch) or restore a point-in-time branch,
# apply the pending chain EXACTLY as production would (same files, sorted
# migration order, same timeout preamble), report, then delete the clone. Evidence
# fresher than 24h is required by the production preflight gate.
#
# Subcommands (all offline unless stated):
#   plan --migrations-dir DIR --provider planetscale|pitr --out evidence.json
#       [--only tag,tag...]
#       Enumerate the pending chain in sorted filename order, optionally
#       filtered to the tags the caller computed from the production ledger.
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
# PLANETSCALE_SERVICE_TOKEN_ID/PLANETSCALE_SERVICE_TOKEN or pscale CLI auth; `pitr` needs
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
PSCALE_BIN="${PSCALE_BIN:-pscale}"

die() { echo "REFUSED: $ME: $*" >&2; exit 2; }
redact() { sed -E 's/(token|secret|password|passwd|key)=[^[:space:]"]+/\\1=<redacted>/gi'; }

delete_planetscale_branch() {
  local output rc lower_output
  if output="$(timeout 60 "$@" 2>&1)"; then
    printf '%s\n' "$output" | redact >&2
  else
    rc=$?
    lower_output="${output,,}"
    if [[ "$lower_output" =~ branch.*(not[[:space:]]+found|does[[:space:]]+not[[:space:]]+exist) ]]; then
      echo "ok: branch already absent"
    else
      printf '%s\n' "$output" | redact >&2
      return "$rc"
    fi
  fi
}

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

default_branch() {
  printf 'rehearsal-%s-%s' "$(date -u +%Y%m%dt%H%M%Sz)" \
    "$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
}

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
    [[ "$BRANCH" =~ ^[a-z][a-z0-9-]{0,47}$ ]] \
      || die "--branch must be lowercase letters, digits, and hyphens (max 48 characters)"
    CHAIN="$(chain_json "$DIR" "$ONLY")" || die "cannot enumerate $DIR"
    case "$PROVIDER" in
      planetscale)
        command -v "$PSCALE_BIN" >/dev/null 2>&1 \
          || die "planetscale rehearsal needs the pscale CLI on PATH (or PSCALE_BIN)"
        [[ -n "${REHEARSAL_PSCALE_ORG:-}" && -n "${REHEARSAL_PSCALE_DB:-}" ]] \
          || die "planetscale rehearsal needs REHEARSAL_PSCALE_ORG and REHEARSAL_PSCALE_DB"
        if [[ -z "${PLANETSCALE_SERVICE_TOKEN_ID:-}" || -z "${PLANETSCALE_SERVICE_TOKEN:-}" ]]; then
          "$PSCALE_BIN" auth check >/dev/null 2>&1 \
            || die "planetscale rehearsal needs PLANETSCALE_SERVICE_TOKEN_ID and PLANETSCALE_SERVICE_TOKEN or valid pscale CLI auth"
        fi
        # Branch-ownership guard: never reuse or delete a pre-existing
        # branch. Verified against planetscale/cli source
        # (internal/cmd/branch/show.go @ 89498e2, via
        # HandleNotFoundWithServiceTokenCheck in internal/cmdutil/auth.go):
        # genuine 404 prints `branch <B> does not exist in database <D>
        # (organization: <O>)` on stderr with cobra's `Error: ` prefix (plus
        # a service-token permission note when tokens are set; invalid
        # tokens report auth failure instead). The match below is a
        # substring on that verified core phrase so prefix/suffix
        # variations still confirm absence.
        if branch_output="$("$PSCALE_BIN" branch show "$REHEARSAL_PSCALE_DB" "$BRANCH" \
            --org "$REHEARSAL_PSCALE_ORG" 2>&1)"; then
          die "branch $BRANCH already exists; refusing to reuse or delete it"
        fi
        [[ "$branch_output" == *"branch "*" does not exist in database "* ]] \
          || die "could not confirm PlanetScale branch $BRANCH is absent"
        CREATE=("$PSCALE_BIN" branch create "$REHEARSAL_PSCALE_DB" "$BRANCH" --org "$REHEARSAL_PSCALE_ORG" --from prod --wait)
        DELETE=("$PSCALE_BIN" branch delete "$REHEARSAL_PSCALE_DB" "$BRANCH" --org "$REHEARSAL_PSCALE_ORG" --force)
        ;;
      pitr)
        [[ -n "${REHEARSAL_PITR_ENDPOINT:-}" && -n "${REHEARSAL_RESTORE_CMD:-}" ]] \
          || die "pitr rehearsal needs REHEARSAL_PITR_ENDPOINT and REHEARSAL_RESTORE_CMD"
        RESTORE_ARGS="$(python3 - "$REHEARSAL_RESTORE_CMD" <<'EOF'
import json, shlex, sys
try:
    argv = shlex.split(sys.argv[1])
except ValueError as exc:
    sys.exit(f"invalid REHEARSAL_RESTORE_CMD: {exc}")
if not argv:
    sys.exit("REHEARSAL_RESTORE_CMD is empty")
print(json.dumps(argv))
EOF
)" || die "invalid REHEARSAL_RESTORE_CMD"
        mapfile -d '' -t CREATE < <(python3 - "$RESTORE_ARGS" "$BRANCH" <<'EOF'
import json, sys
for arg in json.loads(sys.argv[1]) + [sys.argv[2]]:
    sys.stdout.buffer.write(arg.encode() + b"\0")
EOF
)
        DELETE=("true")
        ;;
    esac
    CLONE_CREATED=0
    CLONE_CREATE_IN_PROGRESS=0
    CLONE_CLEANED=0
    CLEANUP_MARKER=""
    [[ "$PROVIDER" != "planetscale" ]] || CLEANUP_MARKER="${REHEARSAL_CLEANUP_MARKER:-}"
    cleanup_on_exit() {
      local exit_code=$?
      trap - EXIT INT TERM HUP
      if [[ ("$CLONE_CREATED" -eq 1 || "$CLONE_CREATE_IN_PROGRESS" -eq 1) \
            && "$CLONE_CLEANED" -eq 0 && "$PROVIDER" == planetscale ]]; then
        if ! delete_planetscale_branch "${DELETE[@]}"; then
          echo "WARN: $ME: exit cleanup failed for $BRANCH — delete it manually" >&2
        else
          [[ -z "$CLEANUP_MARKER" ]] || rm -f "$CLEANUP_MARKER"
        fi
      fi
      if [[ -n "${WRAPDIR:-}" ]]; then rm -rf "$WRAPDIR"; fi
      exit "$exit_code"
    }
    trap cleanup_on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    STARTED="$(now_utc)"
    echo "rehearsal: creating branch $BRANCH (provider $PROVIDER)"
    # --wait can be interrupted after remote creation but before CLI success.
    CLONE_CREATE_IN_PROGRESS=1
    if [[ -n "$CLEANUP_MARKER" ]]; then
      : > "$CLEANUP_MARKER"
    fi
    if ! "${CREATE[@]}" 2>&1 | redact >&2; then
      CLONE_CREATE_IN_PROGRESS=0
      [[ -z "$CLEANUP_MARKER" ]] || rm -f "$CLEANUP_MARKER"
      echo "FAIL: $ME: branch creation failed" >&2; exit 1
    fi
    CLONE_CREATED=1
    CLONE_CREATE_IN_PROGRESS=0
    # The branch name is the execution target contract. The caller command
    # must include the literal {branch} placeholder so the exact clone created
    # above is passed to the apply adapter, rather than silently targeting prod.
    export REHEARSAL_BRANCH="$BRANCH"
    if [[ -z "${REHEARSAL_APPLY_CMD:-}" ]]; then
      echo "REFUSED: $ME: REHEARSAL_APPLY_CMD is not set" >&2
      APPLY_RC=2
      RESULTS="[]"
    else
      HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      WRAPDIR="$(mktemp -d)"
      if ! python3 - "$REHEARSAL_APPLY_CMD" "$BRANCH" > "$WRAPDIR/apply.argv0" <<'EOF'
import shlex, sys
try:
    argv = shlex.split(sys.argv[1])
except ValueError as exc:
    sys.exit(f"invalid REHEARSAL_APPLY_CMD: {exc}")
if not argv or not any("{branch}" in arg for arg in argv):
    sys.exit("REHEARSAL_APPLY_CMD must include {branch} to bind apply to the clone")
for arg in argv:
    sys.stdout.buffer.write(arg.replace("{branch}", sys.argv[2]).encode() + b"\0")
EOF
      then
        echo "REFUSED: $ME: apply command is not bound to the rehearsal branch" >&2
        APPLY_RC=2; RESULTS="[]"
      else
        mapfile -d '' -t APPLY < "$WRAPDIR/apply.argv0"
        APPLY_RC=0; RESULTS="[]"
        # Fail-fast: later migrations are never applied after a failure.
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
    fi
    echo "rehearsal: deleting branch $BRANCH"
    if ! "${DELETE[@]}" 2>&1 | redact >&2; then
      echo "WARN: $ME: branch cleanup failed for $BRANCH — delete it manually" >&2
      CLEANUP="failed"
    else
      CLONE_CLEANED=1
      [[ -z "$CLEANUP_MARKER" ]] || rm -f "$CLEANUP_MARKER"
      if [[ "$PROVIDER" == "pitr" ]]; then
        CLEANUP="delegated"
      else
        CLEANUP="deleted"
      fi
    fi
    STATUS="passed"
    [[ "$APPLY_RC" -eq 0 && "$CLEANUP" != "failed" ]] || STATUS="failed"
    python3 - "$OUT" "$CHAIN" "$RESULTS" <<EOF
import json, sys
chain = json.loads(sys.argv[2])
results = {m["tag"]: m["result"] for m in json.loads(sys.argv[3])}
migs = [{"tag": m["tag"], "sha256": m["sha256"],
         "result": results.get(m["tag"], "not_attempted")}
        for m in chain]
doc = {"version": $EVIDENCE_VERSION, "provider": "$PROVIDER",
       "branch": "$BRANCH", "apply_target": "$BRANCH", "base": "prod", "migrations": migs,
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
provider, cleanup = doc.get("provider"), doc.get("cleanup")
if provider == "planetscale" and cleanup != "deleted":
    sys.exit("FAIL: PlanetScale rehearsal branch cleanup is not confirmed deleted")
if provider == "pitr" and cleanup != "delegated":
    sys.exit("FAIL: PITR cleanup must be recorded as delegated expiration")
if provider not in {"planetscale", "pitr"}:
    sys.exit("FAIL: rehearsal evidence has an unknown provider")
if not doc.get("branch") or doc.get("apply_target") != doc.get("branch"):
    sys.exit("FAIL: rehearsal evidence target does not match its created branch")
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
    if [m.get("tag") for m in migs] != sorted(candidate):
        sys.exit("FAIL: rehearsal evidence migration order does not match filename order")
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
        command -v "$PSCALE_BIN" >/dev/null 2>&1 || die "needs the pscale CLI on PATH (or PSCALE_BIN)"
        [[ -n "${REHEARSAL_PSCALE_ORG:-}" && -n "${REHEARSAL_PSCALE_DB:-}" ]] \
          || die "needs REHEARSAL_PSCALE_ORG and REHEARSAL_PSCALE_DB"
        delete_planetscale_branch "$PSCALE_BIN" branch delete "$REHEARSAL_PSCALE_DB" "$BRANCH" \
          --org "$REHEARSAL_PSCALE_ORG" --force \
          || exit $?
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
