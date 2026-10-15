#!/usr/bin/env bash
# test_case_1372613a_invariants.sh — case_654dd2ec offline suite.
#
# No credential, no network, no database server: the Postgres views and
# fixtures are mechanically translated (rules in paperclip_invariants.sql)
# into sqlite and exercised with positive controls (rows each view MUST
# return) and negative controls (rows each view MUST NOT return).
# A static gate pins the views file to SELECT-only.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL="$HERE/ops/case_654dd2ec/paperclip_invariants.sql"
FIX="$HERE/ops/case_654dd2ec/fixtures.sql"
YAML="$HERE/ops/case_654dd2ec/gatus-endpoints.yaml"
PUSH="$HERE/ops/case_654dd2ec/push-paperclip-invariants.sh"
OPDOC="$HERE/ops/case_654dd2ec/OPERATOR.md"
PASS=0; FAIL=0
ok(){ echo "PASS $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL $1"; FAIL=$((FAIL+1)); }

# §1 — the views file is SELECT-only.
for f in "$SQL" "$FIX" "$YAML" "$PUSH" "$OPDOC"; do
  [[ -f "$f" ]] && ok "$(basename "$f") exists" || bad "$(basename "$f") missing"
done
[[ "$(grep -c '^CREATE OR REPLACE VIEW paperclip_inv_' "$SQL")" == 6 ]] \
  && ok "six invariant views" || bad "view count != 6"
for v in agents_error_10m failed_subtype_success cards_rewoken_6h missing_disposition_reparks unassigned_infocus_cards db_pool_waiters; do
  grep -q "CREATE OR REPLACE VIEW paperclip_inv_$v AS" "$SQL" \
    && ok "view $v defined" || bad "view $v missing"
done
# No remediation verbs anywhere in the views file (code or comments).
# Word-boundaried so updated_at / last_touched_at do not false-positive.
if grep -Ein '\b(insert|update|delete|merge|truncate|drop|alter|grant|revoke|copy|vacuum|into)\b|create +(table|index|extension)' "$SQL"; then
  bad "views file carries a non-SELECT verb"
else
  ok "views file is SELECT-only"
fi
# Nothing secret-shaped or statement-text-shaped may ride along.
if grep -Ein '\b(password|secret|token|bearer|query)\b' "$SQL"; then
  bad "views file names a secret or statement text"
else
  ok "views file names no secret or statement text"
fi
# Every view exposes its read time, so stale reads never look like healthy zeros.
[[ "$(grep -c 'now() AS checked_at' "$SQL")" == 6 ]] \
  && ok "all views expose checked_at" || bad "checked_at missing somewhere"
# Windows the card asked for.
for w in "INTERVAL '10 minutes'" "INTERVAL '1 hour'" "INTERVAL '6 hours'" "INTERVAL '15 minutes'"; do
  grep -qF "$w" "$SQL" && ok "window $w present" || bad "window $w missing"
done
grep -q 'HAVING count(\*) > 3' "$SQL" && ok "re-wake threshold >3 pinned" || bad "re-wake threshold missing"
# Only the five allowlisted source tables.
unexpected="$(grep -Eo 'FROM (agents|heartbeat_runs|activity_log|issues|pg_stat_activity)|FROM [a-z_]+' "$SQL" | sort -u | grep -v 'FROM \(agents\|heartbeat_runs\|activity_log\|issues\|pg_stat_activity\)$' || true)"
[[ -z "$unexpected" ]] && ok "only allowlisted source tables" || bad "unexpected source: $unexpected"

# §2 — Gatus definitions: six endpoints, each with threshold + owner + runbook.
[[ "$(grep -c '^  - name: ' "$YAML")" == 6 ]] \
  && ok "six gatus endpoints" || bad "endpoint count != 6"
for e in agents-error-10m failed-subtype-success cards-rewoken-6h missing-disposition-reparks unassigned-infocus-cards db-pool-waiters; do
  grep -q "name: $e" "$YAML" && ok "endpoint $e" || bad "endpoint $e missing"
  grep -q "OPERATOR.md#$e" "$YAML" && ok "runbook pointer $e" || bad "runbook pointer $e missing"
  grep -q "^### $e$" "$OPDOC" && ok "runbook anchor $e" || bad "runbook anchor $e missing"
done
[[ "$(grep -c THRESHOLD "$YAML")" -ge 6 ]] && ok "thresholds declared" || bad "thresholds missing"
[[ "$(grep -c 'OWNER director' "$YAML")" -ge 6 ]] && ok "owners declared" || bad "owners missing"
if grep -En '://' "$YAML"; then bad "yaml carries a URL literal"; else ok "yaml has no URL literals"; fi
[[ "$(grep -c 'GATUS_EXTERNAL_TOKEN' "$YAML")" -ge 6 ]] \
  && ok "token only via env reference" || bad "token reference missing"

# §3 — push script: parses, stays read-only, fails closed.
bash -n "$PUSH" && ok "push script parses" || bad "push script does not parse"
grep -q "default_transaction_read_only=on" "$PUSH" && ok "read-only session pinned" || bad "read-only session missing"
grep -q 'paperclip_inv_' "$PUSH" && ok "view allowlist present" || bad "view allowlist missing"
grep -q 'curl --config' "$PUSH" && ok "token via config file" || bad "token not via config file"
if grep -En '(^|[[:space:]])-H[[:space:]]' "$PUSH"; then bad "push script passes -H on argv"; else ok "no argv header"; fi
grep -q 'fail_closed=1' "$PUSH" && ok "fail-closed paths present" || bad "fail-closed missing"
[[ "$(grep -c '|paperclip_' "$PUSH")" == 6 ]] && ok "SPEC covers six endpoints" || bad "SPEC incomplete"
for e in agents-error-10m failed-subtype-success cards-rewoken-6h missing-disposition-reparks unassigned-infocus-cards db-pool-waiters; do
  grep -q "paperclip_$e" "$PUSH" && ok "SPEC pushes $e" || bad "SPEC misses $e"
done

# §4 — functional: translate to sqlite, check exact violating sets.
export SQL FIX
python3 <<'PYEOF'
import os, re, sqlite3, sys

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" — " + extra) if extra and not cond else ""))
    if not cond: fails.append(name)

def plural(n, unit):
    unit = unit.lower()
    base = {"minute": "minutes", "hour": "hours"}.get(unit.rstrip("s"), unit)
    return f"{n} {base}"

def translate(src):
    s = re.sub(r"now\(\)\s*-\s*INTERVAL\s*'(\d+)\s+(minutes?|hours?)'",
               lambda m: f"datetime('now','-{plural(m.group(1), m.group(2))}')", src)
    n_now = len(re.findall(r"now\(\)", s))
    s = s.replace("now()", "datetime('now')")
    s = re.sub(r"([\w.]+)\s*->>\s*'(\w+)'", r"json_extract(\1, '$.\2')", s)
    n_json = len(re.findall(r"json_extract\(", s))
    n_ilike = len(re.findall(r"\bILIKE\b", s))
    s = re.sub(r"\bILIKE\b", "LIKE", s)
    n_view = len(re.findall(r"^CREATE OR REPLACE VIEW", s, flags=re.M))
    s = re.sub(r"CREATE OR REPLACE VIEW (\w+) AS", r"DROP VIEW IF EXISTS \1; CREATE VIEW \1 AS", s)
    return s, {"now": n_now, "json": n_json, "ilike": n_ilike, "view": n_view}

sql = open(os.environ["SQL"]).read()
fix = open(os.environ["FIX"]).read()
tsql, tv = translate(sql)
tfix, tf = translate(fix)
n_win = len(re.findall(r"datetime\('now','-", tsql + tfix))
check("translation rewrites now() windows", n_win >= 50, f"window hits={n_win}")
check("translation keeps bare now()", tv["now"] + tf["now"] >= 6, f"bare now() hits={tv['now'] + tf['now']}")
check("translation rewrites ->>", tv["json"] + tf["json"] > 0, "no json_extract produced")
check("translation rewrites ILIKE", tv["ilike"] > 0, "no ILIKE found")
check("translation rewrites all six views", tv["view"] == 6, f"views={tv['view']}")

db = sqlite3.connect(":memory:")
db.executescript(tfix)
db.executescript(tsql)

def ids(view, col=0):
    return sorted(r[col] for r in db.execute(f"SELECT * FROM {view}"))

check("V1 stuck error agent trips",
      ids("paperclip_inv_agents_error_10m") == ["00000000-0000-4000-8000-000000000003"],
      f"got {ids('paperclip_inv_agents_error_10m')}")
check("V2 subtype-success 143 pair trips",
      ids("paperclip_inv_failed_subtype_success") == ["00000000-0000-4000-8000-000000000079",
                                                      "00000000-0000-4000-8000-000000000020"],
      f"got {ids('paperclip_inv_failed_subtype_success')}")
rows = db.execute("SELECT issue_id, wake_count FROM paperclip_inv_cards_rewoken_6h").fetchall()
check("V3 hot card trips with count 4",
      rows == [("00000000-0000-4000-8000-000000000085", 4)], f"got {rows}")
check("V4 three re-parks trip",
      ids("paperclip_inv_missing_disposition_reparks") == ["00000000-0000-4000-8000-000000000104",
                                                           "00000000-0000-4000-8000-000000000106",
                                                           "00000000-0000-4000-8000-000000000108"],
      f"got {ids('paperclip_inv_missing_disposition_reparks')}")
check("V5 orphaned focus card trips",
      ids("paperclip_inv_unassigned_infocus_cards") == ["00000000-0000-4000-8000-000000000116"],
      f"got {ids('paperclip_inv_unassigned_infocus_cards')}")
pids = sorted(r[0] for r in db.execute("SELECT pid FROM paperclip_inv_db_pool_waiters"))
check("V6 two pool waiters trip", pids == [42421, 42422], f"got {pids}")

# Vacuity guard: with only negative controls the views must go quiet.
db2 = sqlite3.connect(":memory:")
db2.executescript(tfix)
db2.executescript(tsql)
for bad_id, view in [("00000000-0000-4000-8000-000000000003", "paperclip_inv_agents_error_10m"),
                     ("00000000-0000-4000-8000-000000000079", "paperclip_inv_failed_subtype_success"),
                     ("00000000-0000-4000-8000-000000000020", "paperclip_inv_failed_subtype_success")]:
    tbl = "agents" if "agents" in view else "heartbeat_runs"
    db2.execute(f"DELETE FROM {tbl} WHERE id = '{bad_id}'")
for bad_id in ["00000000-0000-4000-8000-000000000084",
               "00000000-0000-4000-8000-000000000104",
               "00000000-0000-4000-8000-000000000106",
               "00000000-0000-4000-8000-000000000108",
               "00000000-0000-4000-8000-000000000116"]:
    for tbl in ["heartbeat_runs", "activity_log", "issues"]:
        db2.execute(f"DELETE FROM {tbl} WHERE id = '{bad_id}'")
db2.execute("DELETE FROM pg_stat_activity WHERE pid IN (42421, 42422)")
quiet = all(db2.execute(f"SELECT count(*) FROM {v}").fetchone()[0] == 0 for v in
            ["paperclip_inv_agents_error_10m", "paperclip_inv_failed_subtype_success",
             "paperclip_inv_cards_rewoken_6h", "paperclip_inv_missing_disposition_reparks",
             "paperclip_inv_unassigned_infocus_cards", "paperclip_inv_db_pool_waiters"])
check("vacuity guard: negatives alone trip nothing", quiet)

sys.exit(1 if fails else 0)
PYEOF
[[ $? -eq 0 ]] && ok "functional sqlite checks green" || bad "functional sqlite checks red"

echo "--- $PASS pass, $FAIL fail ---"
[[ "$FAIL" == 0 ]]
