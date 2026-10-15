#!/usr/bin/env python3
"""Durable atomic reservation proof for case_9ae341a0 — agent-testdb/CI only.

Validates the case_24119081 aggregate CAS/fencing/idempotency contract against a
REAL PostgreSQL (agent-testdb, card-scoped database ``ops_test_case_2e0d60cb``),
using the same single-statement conditional-UPDATE shape as
``plugins/model-selection/src/capacity-ledger.ts``:

* multi-window all-or-none under real concurrent writers (exactly one wins,
  no partial debit);
* ambiguous-response recovery by attempt-fingerprint read (never a fresh key);
* fencing-epoch rejection of a superseded writer;
* cross-tenant isolation on the same domain key;
* restart recovery through a fresh connection (no process memory);
* charged outcomes: finish keeps the debit, trusted watermarked
  reconciliation reduces it once, replays do not double-apply;
* rowCount-only driver behavior (every conditional UPDATE returns 0 or 1).

The database is created on entry and DROPPED in a finally block, so a green
run leaves no residue. Any failure exits non-zero. No live GARM, no
credentials, no enforcement, no production access.
"""

import json
import sys
import threading
import traceback

import psycopg2

HOST = "agent-testdb"
PORT = 5432
USER = "agent_test"
ADMIN_DB = "postgres"
CARD_DB = "ops_test_case_2e0d60cb"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
DOMAIN = "domain-1"
EPOCH = 7
NOW = 1_000

WEEKLY = json.dumps(["prov", "pool", "weekly", 0, 10000])
FIVE_HOUR = json.dumps(["prov", "pool", "five-hour", 0, 5000])

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def single_statement(sql: str) -> str:
    assert ";" not in sql, "multi-statement SQL rejected"
    return sql


def connect(db: str):
    return psycopg2.connect(host=HOST, port=PORT, user=USER, dbname=db, connect_timeout=10)


def seed_row(cur, tenant: str, epoch: int = EPOCH, slot_max: int = 3) -> None:
    windows = {
        WEEKLY: {"unit": "allowance", "quota": 100, "consumed": 90,
                 "reserved": 0, "safety_headroom": 0, "reset_at": 10000},
        FIVE_HOUR: {"unit": "allowance", "quota": 50, "consumed": 10,
                    "reserved": 0, "safety_headroom": 5, "reset_at": 5000},
    }
    cur.execute(single_statement(
        "INSERT INTO capacity_ledger (tenant_id, domain_key, fencing_epoch,"
        " slot_max, windows, attempts, watermarks)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"),
        [tenant, DOMAIN, epoch, slot_max, json.dumps(windows), "{}", "{}"])


def read_row(cur, tenant: str):
    cur.execute(single_statement(
        "SELECT version, fencing_epoch, slot_max, slots_held,"
        " windows, attempts, watermarks FROM capacity_ledger"
        " WHERE tenant_id = %s AND domain_key = %s"), [tenant, DOMAIN])
    r = cur.fetchone()
    if r is None:
        return None
    return {"version": r[0], "epoch": r[1], "slot_max": r[2], "slots_held": r[3],
            "windows": r[4], "attempts": r[5], "watermarks": r[6]}


def reserve(conn, tenant: str, key: str, payload: dict,
            debits: list[tuple[str, str, float]],
            duration: int = 100, now: int = NOW,
            epoch: int = EPOCH) -> tuple[str, dict | None]:
    """Full driver-equivalent flow: read, evaluate, single UPDATE, classify.

    Returns (status, row-after). Statuses mirror capacity-ledger.ts:
    held / deferred / replayed-identical / payload-mismatch / fenced / lost /
    won-after-ambiguity / unknown-retry-same-key.
    """
    fp = json.dumps(payload, sort_keys=True)
    cur = conn.cursor()
    row = read_row(cur, tenant)
    if row is None:
        return "deferred:domain-unknown", None
    if row["epoch"] != epoch:
        return "fenced", row
    prior = row["attempts"].get(key)
    if prior is not None:
        if prior["fingerprint"] == fp:
            return "replayed-identical", row
        return "payload-mismatch", row
    # Advisory pre-check (the UPDATE re-checks atomically server-side).
    for wid, unit, amount in debits:
        w = row["windows"].get(wid)
        if w is None or w["unit"] != unit or not (
                now + duration < w["reset_at"] and
                w["reserved"] + amount <= w["quota"] - w["consumed"] - w["safety_headroom"]):
            return "deferred:insufficient", row
    if row["slots_held"] + 1 > row["slot_max"]:
        return "deferred:slot-limit", row

    next_windows = json.loads(json.dumps(row["windows"]))
    for wid, _unit, amount in debits:
        next_windows[wid]["reserved"] += amount
    next_attempts = json.loads(json.dumps(row["attempts"]))
    next_attempts[key] = {"fingerprint": fp, "outcome": "held",
                          "reservation_version": row["version"] + 1,
                          "debits": {wid: {"unit": u, "amount": a, "remaining": a}
                                     for wid, u, a in debits},
                          "duration_ms": duration, "reconciliation_ids": [],
                          "slot_released": False}
    clauses = ["tenant_id = %s", "domain_key = %s", "version = %s",
               "fencing_epoch = %s", "NOT (attempts ? %s)",
               "slots_held + 1 <= slot_max"]
    where_params: list = [tenant, DOMAIN, row["version"], epoch, key]
    for wid, unit, amount in debits:
        clauses += [
            "(windows -> %s IS NOT NULL)",
            "(windows -> %s ->> 'unit') = %s",
            "(%s + %s < ((windows -> %s ->> 'reset_at')::bigint))",
            "(((windows -> %s ->> 'reserved')::numeric) + (%s)::numeric <="
            " ((windows -> %s ->> 'quota')::numeric)"
            " - ((windows -> %s ->> 'consumed')::numeric)"
            " - ((windows -> %s ->> 'safety_headroom')::numeric))",
        ]
        where_params += [wid, wid, unit, now, duration, wid,
                         wid, amount, wid, wid, wid]
    sql = single_statement(
        "UPDATE capacity_ledger SET windows = %s::jsonb,"
        " attempts = %s::jsonb, slots_held = %s, version = %s"
        f" WHERE {' AND '.join(clauses)}")
    # SET placeholders precede WHERE placeholders in the text, so their
    # values lead the positional parameter list.
    params = [json.dumps(next_windows), json.dumps(next_attempts),
              row["slots_held"] + 1, row["version"] + 1] + where_params
    cur.execute(sql, params)
    assert cur.rowcount in (0, 1), f"rowCount {cur.rowcount} is ambiguous"
    conn.commit()
    if cur.rowcount == 1:
        return "held", read_row(cur, tenant)
    fresh = read_row(cur, tenant)
    if fresh is None:
        return "lost:domain-unknown", None
    if fresh["epoch"] != epoch:
        return "fenced", fresh
    entry = fresh["attempts"].get(key)
    if entry is not None:
        return ("won-after-ambiguity" if entry["fingerprint"] == fp
                else "payload-mismatch"), fresh
    return "lost:write-conflict", fresh


def main() -> int:
    try:
        admin = connect(ADMIN_DB)
    except Exception as exc:
        print(f"FAIL cannot reach agent-testdb: {exc}")
        return 2
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [CARD_DB])
    if cur.fetchone() is None:
        cur.execute(f"CREATE DATABASE {CARD_DB}")
    admin.close()

    db = connect(CARD_DB)
    db.autocommit = True
    try:
        cur = db.cursor()
        cur.execute("DROP TABLE IF EXISTS capacity_ledger")
        cur.execute(single_statement(
            "CREATE TABLE capacity_ledger (tenant_id TEXT NOT NULL,"
            " domain_key TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 0,"
            " fencing_epoch BIGINT NOT NULL DEFAULT 0,"
            " slot_max INTEGER NOT NULL DEFAULT 0,"
            " slots_held INTEGER NOT NULL DEFAULT 0,"
            " windows JSONB NOT NULL DEFAULT '{}',"
            " attempts JSONB NOT NULL DEFAULT '{}',"
            " watermarks JSONB NOT NULL DEFAULT '{}',"
            " PRIMARY KEY (tenant_id, domain_key))"))
        seed_row(cur, TENANT_A)
        seed_row(cur, TENANT_B)

        payload = {"binding": "eligible", "burn": [[WEEKLY, 10], [FIVE_HOUR, 5]]}
        debits = [(WEEKLY, "allowance", 10), (FIVE_HOUR, "allowance", 5)]

        # 1. Basic hold is all-or-none across both windows.
        status, row = reserve(db, TENANT_A, "k-basic", payload, debits)
        check("basic-hold", status == "held", status)
        check("basic-hold-debits",
              row is not None and row["windows"][WEEKLY]["reserved"] == 10
              and row["windows"][FIVE_HOUR]["reserved"] == 5
              and row["slots_held"] == 1 and row["version"] == 1,
              json.dumps(row["windows"]) if row else "no-row")

        # 2. Insufficient second window: zero rows, nothing debited anywhere.
        big = [(WEEKLY, "allowance", 1), (FIVE_HOUR, "allowance", 1000)]
        status2, row2 = reserve(db, TENANT_A, "k-short", dict(payload), big)
        check("all-or-none-defer", status2.startswith("deferred"), status2)
        check("all-or-none-untouched",
              row2 is not None and row2["windows"][WEEKLY]["reserved"] == 10
              and row2["windows"][FIVE_HOUR]["reserved"] == 5
              and "k-short" not in row2["attempts"], "partial debit detected")

        # 3. Real concurrent contention: two writers, one version, one winner.
        cur.execute("DELETE FROM capacity_ledger WHERE tenant_id = %s", [TENANT_A])
        seed_row(cur, TENANT_A)
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def contender(name: str, key: str) -> None:
            c = connect(CARD_DB)
            c.autocommit = True
            barrier.wait(timeout=30)
            st, _ = reserve(c, TENANT_A, key, payload, debits)
            outcomes[name] = st
            c.close()

        t1 = threading.Thread(target=contender, args=("w1", "k-race-a"))
        t2 = threading.Thread(target=contender, args=("w2", "k-race-b"))
        t1.start()
        t2.start()
        t1.join(60)
        t2.join(60)
        final = read_row(cur, TENANT_A)
        wins = sorted(outcomes.values())
        check("race-exactly-one-wins", wins == ["held", "lost:write-conflict"], str(wins))
        check("race-no-partial",
              final is not None and final["windows"][WEEKLY]["reserved"] == 10
              and final["windows"][FIVE_HOUR]["reserved"] == 5
              and len(final["attempts"]) == 1 and final["slots_held"] == 1,
              json.dumps(final["windows"]) if final else "no-row")

        # 4. Ambiguous response: reseed so the attempt can hold, then
        #    discard the rowcount and resolve by fingerprint on a FRESH
        #    connection (restart-equivalent), then continue there.
        cur.execute("DELETE FROM capacity_ledger WHERE tenant_id = %s", [TENANT_A])
        seed_row(cur, TENANT_A)
        c2 = connect(CARD_DB)
        c2.autocommit = True
        st4, _ = reserve(c2, TENANT_A, "k-amb", payload, debits)
        c2.close()  # response "lost": a new worker takes over by reading
        c3 = connect(CARD_DB)
        c3.autocommit = True
        after = read_row(c3.cursor(), TENANT_A)
        entry = (after["attempts"].get("k-amb") if after else None)
        check("ambiguity-resolves-won",
              st4 == "held" and entry is not None
              and entry["fingerprint"] == json.dumps(payload, sort_keys=True),
              st4)
        st4b, _ = reserve(c3, TENANT_A, "k-amb", payload, debits)
        check("ambiguity-replay-identical", st4b == "replayed-identical", st4b)
        v_before = read_row(c3.cursor(), TENANT_A)["version"]
        st4c, _ = reserve(c3, TENANT_A, "k-amb", dict(payload, burn="other"), debits)
        v_after = read_row(c3.cursor(), TENANT_A)["version"]
        check("ambiguity-payload-mismatch",
              st4c == "payload-mismatch" and v_before == v_after, st4c)
        c3.close()

        # 5. Fencing: epoch bump defeats the stale writer, admits the new one.
        cur.execute("UPDATE capacity_ledger SET fencing_epoch = %s"
                    " WHERE tenant_id = %s", [EPOCH + 1, TENANT_A])
        db.commit()
        st5, _ = reserve(db, TENANT_A, "k-stale", payload, debits, epoch=EPOCH)
        check("fenced-stale-epoch", st5 == "fenced", st5)

        # 6. Cross-tenant isolation on the same domain key.
        st6, row6 = reserve(db, TENANT_B, "k-b1", payload, debits, epoch=EPOCH)
        check("tenant-b-holds-own-row", st6 == "held", st6)
        rowa = read_row(cur, TENANT_A)
        check("tenant-a-untouched",
              rowa is not None and "k-b1" not in rowa["attempts"], "cross-tenant leak")
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET slots_held = 99"
            " WHERE tenant_id = %s AND domain_key = %s AND version = %s"),
            [TENANT_A, DOMAIN, rowa["version"] if rowa else -1])
        check("tenant-predicate-binds", cur.rowcount == 1, f"rowcount={cur.rowcount}")
        hostile = read_row(cur, TENANT_B)
        check("tenant-rows-independent",
              hostile is not None and hostile["slots_held"] == 1, "tenant-B row moved")

        # 7. Charged outcomes on tenant B: commit, finish keeps debit,
        #    watermarked reconcile reduces exactly once.
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET attempts = attempts || %s::jsonb,"
            " version = version + 1 WHERE tenant_id = %s AND domain_key = %s"
            " AND (attempts -> %s ->> 'outcome') = 'held'"),
            [json.dumps({"k-b1": {**hostile["attempts"]["k-b1"], "outcome": "committed"}}),
             TENANT_B, DOMAIN, "k-b1"])
        check("commit", cur.rowcount == 1, f"rowcount={cur.rowcount}")
        pre = read_row(cur, TENANT_B)
        assert pre is not None
        released = {**pre["attempts"]["k-b1"], "slot_released": True}
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET slots_held = slots_held - 1,"
            " attempts = %s::jsonb, version = version + 1"
            " WHERE tenant_id = %s AND domain_key = %s AND version = %s"
            " AND (attempts -> %s ->> 'outcome') = 'committed'"
            " AND COALESCE(((attempts -> %s ->> 'slot_released')::boolean), false) = false"
            " AND slots_held > 0"),
            [json.dumps({"k-b1": released}), TENANT_B, DOMAIN,
             pre["version"], "k-b1", "k-b1"])
        check("finish-frees-slot", cur.rowcount == 1, f"rowcount={cur.rowcount}")
        mid = read_row(cur, TENANT_B)
        check("finish-keeps-debit-charged",
              mid is not None and mid["windows"][WEEKLY]["reserved"] == 10
              and mid["slots_held"] == 0, "debit refunded by finish")
        # Hold another slot so a retried second finish cannot hide behind
        # slots_held = 0: the journal predicate alone must block it.
        stb, _ = reserve(db, TENANT_B, "k-b2",
                         {"binding": "eligible", "burn": [[FIVE_HOUR, 5]]},
                         [(FIVE_HOUR, "allowance", 5)], epoch=EPOCH)
        check("finish-setup-second-holder", stb == "held", stb)
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET slots_held = slots_held - 1,"
            " version = version + 1 WHERE tenant_id = %s AND domain_key = %s"
            " AND (attempts -> %s ->> 'outcome') = 'committed'"
            " AND COALESCE(((attempts -> %s ->> 'slot_released')::boolean), false) = false"
            " AND slots_held > 0"),
            [TENANT_B, DOMAIN, "k-b1", "k-b1"])
        check("finish-no-double-free", cur.rowcount == 0, f"rowcount={cur.rowcount}")
        after_second = read_row(cur, TENANT_B)
        check("finish-slot-count-exact",
              after_second is not None and after_second["slots_held"] == 1,
              "second finish moved the counter")
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET windows = %s::jsonb,"
            " attempts = %s::jsonb, watermarks = %s::jsonb, version = version + 1"
            " WHERE tenant_id = %s AND domain_key = %s"
            " AND (attempts -> %s ->> 'outcome') = 'committed'"
            " AND NOT (COALESCE((attempts -> %s -> 'reconciliation_ids'), '[]'::jsonb) ? %s)"
            " AND (%s)::numeric <="
            " ((attempts -> %s -> 'debits' -> %s ->> 'remaining')::numeric)"
            " AND (%s)::int > COALESCE(((watermarks -> %s)::int), 0)"),
            [json.dumps({**mid["windows"], WEEKLY: {**mid["windows"][WEEKLY],
                                                    "consumed": 100, "reserved": 0}}),
             json.dumps({"k-b1": {**mid["attempts"]["k-b1"],
                                  "outcome": "reconciled",
                                  "debits": {WEEKLY: {"unit": "allowance", "amount": 10,
                                                       "remaining": 0}},
                                  "reconciliation_ids": ["rec-1"]}}),
             json.dumps({WEEKLY: 1}),
             TENANT_B, DOMAIN, "k-b1", "k-b1", "rec-1", 10, "k-b1", WEEKLY, 1, WEEKLY])
        check("reconcile-applies-once", cur.rowcount == 1, f"rowcount={cur.rowcount}")
        # Replay the same reconciliation id: must match zero rows.
        cur.execute(single_statement(
            "UPDATE capacity_ledger SET version = version + 1"
            " WHERE tenant_id = %s AND domain_key = %s"
            " AND NOT (COALESCE((attempts -> %s -> 'reconciliation_ids'), '[]'::jsonb) ? %s)"),
            [TENANT_B, DOMAIN, "k-b1", "rec-1"])
        check("reconcile-replay-noop", cur.rowcount == 0, f"rowcount={cur.rowcount}")
        done = read_row(cur, TENANT_B)
        check("reconcile-final",
              done is not None and done["windows"][WEEKLY]["reserved"] == 0
              and done["windows"][WEEKLY]["consumed"] == 100
              and done["attempts"]["k-b1"]["outcome"] == "reconciled",
              "double-apply or lost update")
    finally:
        try:
            db.close()
        except Exception:
            pass
        admin2 = connect(ADMIN_DB)
        admin2.autocommit = True
        admin2.cursor().execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()", [CARD_DB])
        admin2.cursor().execute(f"DROP DATABASE IF EXISTS {CARD_DB}")
        admin2.close()

    print(f"--- {len(FAILURES)} failures ---")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
