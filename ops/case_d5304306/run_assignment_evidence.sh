#!/usr/bin/env bash
# run_assignment_evidence.sh — case_d5304306 manual run: read-only source -> bundle -> snapshot -> verdict.
#
# Runs assignment_evidence.sql through a read-only session, builds the
# watchdog.assignment-evidence-input.v1 bundle, normalises it and runs the
# detector, printing the detector's JSON lines. Scratch files live in a fresh
# 0700 directory that is removed on exit. No cron, no board write, no credential.
#
# Env: PAPERCLIP_RO_DB_URL (read-only role URL, mandatory).
# Exit: 0 all three sources complete (individual rows may still read unknown —
#       count them before acting); 1 a source incomplete or the read failed;
#       2 refused before touching the database. Detector output is filtered to
#       the watchdog/assignment family plus the summary line: the snapshot only
#       carries assignment evidence, so every other family reads unknown.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
COMPANY_ID="00000000-0000-4000-8000-000000000004"

# shellcheck source=../../lib/pcsql.sh
. "$ROOT/lib/pcsql.sh" \
  || { echo "run-assignment-evidence: refusing — lib/pcsql.sh missing" >&2; exit 2; }
[[ -n "${PAPERCLIP_RO_DB_URL:-}" ]] \
  || { echo "run-assignment-evidence: refusing — PAPERCLIP_RO_DB_URL is unset" >&2; exit 2; }

umask 077
work="$(mktemp -d "${PAPERCLIP_SCRATCH_DIR:-${TMPDIR:-/tmp}}/case_dcf07273.XXXXXX")" \
  || { echo "run-assignment-evidence: refusing — cannot create scratch directory" >&2; exit 2; }
trap 'rm -rf "$work"' EXIT

# libpq reads the connection from PG* variables derived from the URL, never argv.
export PAPERCLIP_SQL_BACKEND=psql
export DATABASE_URL="$PAPERCLIP_RO_DB_URL"
export PGOPTIONS='-c default_transaction_read_only=on'

pcsql_run -tA -v ON_ERROR_STOP=1 -v "company_id=$COMPANY_ID" \
  < "$HERE/assignment_evidence.sql" > "$work/rows.jsonl" \
  || { echo "run-assignment-evidence: database read failed" >&2; exit 1; }

python3 "$HERE/build_assignment_bundle.py" \
  --rows "$work/rows.jsonl" --sql "$HERE/assignment_evidence.sql" \
  --bundle "$work/bundle.json" --report "$work/report.json"
built=$?
(( built <= 1 )) || { echo "run-assignment-evidence: bundle builder failed (exit $built)" >&2; exit 2; }
if [[ -n "${case_dcf07273_REPORT_OUT:-}" ]]; then
  ( set -C; cat "$work/report.json" > "$case_dcf07273_REPORT_OUT" ) \
    || { echo "run-assignment-evidence: cannot write case_dcf07273_REPORT_OUT (must not exist)" >&2; exit 2; }
fi

python3 "$ROOT/watchdog/assignment_evidence.py" \
  --input "$work/bundle.json" --out "$work/snapshot.json" \
  || { echo "run-assignment-evidence: normaliser failed" >&2; exit 2; }
python3 "$ROOT/watchdog/detectors.py" --snapshot "$work/snapshot.json" \
  > "$work/detected.jsonl"
detected=$?
# The snapshot only carries assignment evidence, so every other detector family
# reads unknown by construction. Print this slice's family and the summary.
python3 - "$work/detected.jsonl" <<'EOF'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("detector") in (
                "watchdog/assignment", "watchdog/cycle"):
            print(json.dumps(record, sort_keys=True))
EOF

(( built == 0 && detected == 0 ))
