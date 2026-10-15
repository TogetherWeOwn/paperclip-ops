#!/usr/bin/env python3
"""Durable alert queue with bounded retry and spill-never-drop overflow (case_56fff4c6).

Parent case_66436e78 deliverable 7: the on-call gate drops excess alerts with
HTTP 202 instead of queueing them — a silent loss on exactly the path that
must be loudest. This module is the specified replacement policy, as pure
stdlib-only logic with injectable clock and transport seams so the offline
suite proves it without network, credentials, or a broker.

POLICY (every terminal state is recorded; nothing vanishes quietly):

  * enqueue() appends one ``enqueued`` row to queue.jsonl through
    lib/durable_queue.append (fsync'd, 0600). The alert is pending.
  * drain() delivers each due alert via the injected deliver_fn. Success
    appends ``delivered``. Failure appends ``attempt_failed`` carrying
    next_attempt_at = now + backoff(attempts) — exponential base 60s,
    doubling, capped at 3600s, optional jitter. Not-due alerts are skipped,
    never retried early, never declared lost.
  * Exhaustion (attempts >= max_attempts) appends ``spilled`` to the queue
    log AND the full entry to spill.jsonl (durable, fsync'd) AND returns a
    page signal for the caller to send. The alert is terminal but accounted.
  * Overflow (pending depth > max_depth) spills oldest-first down to the
    bound with reason ``overflow``, one aggregated page signal listing every
    spilled id. Shedding load pages; it never deletes.
  * Identity: enqueueing the same id twice is refused (the index is the
    dedupe record). The expected set for audit() is the enqueue index,
    written before the queue row: a torn write fails loud, and a dropped
    queue row fails as ``unaccounted`` instead of reading as "never
    existed".
  * audit() replays the log and accounts every enqueued id as exactly one of
    pending / delivered / spilled. An id in none of the three (e.g. a row
    removed by a drop-path mutant) is reported under ``unaccounted`` — that
    is the gate the suite's drop-path fixture must fail.

The module never touches the network. deliver_fn and page_fn are injected;
probe-gatus.sh and roundtrip-alert.sh (same directory) are the transports.
A missing store raises FileNotFoundError — an unmeasured queue is a refusal,
never a clean bill (cf. queue_liveness.sh rule 3).
"""

from __future__ import annotations

import datetime
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from durable_queue import append as durable_append  # noqa: E402

QUEUE_FILE = "queue.jsonl"
SPILL_FILE = "spill.jsonl"
INDEX_FILE = "index.jsonl"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_DEPTH = 1000
BACKOFF_BASE_S = 60.0
BACKOFF_CAP_S = 3600.0


def backoff(attempts: int, base: float = BACKOFF_BASE_S,
            cap: float = BACKOFF_CAP_S, jitter: float = 0.0,
            rng: random.Random | None = None) -> float:
    """Seconds to wait after ``attempts`` failures (attempts >= 1).

    Exponential: base * 2**(attempts-1), capped. jitter=0 (the default) is
    exact and is what the suite pins; production passes jitter=0.2 with an
    rng so a fleet of queues does not retry in lockstep.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = min(base * (2.0 ** (attempts - 1)), cap)
    if jitter:
        rng = rng or random.SystemRandom()
        delay = delay * (1.0 + rng.uniform(-jitter, jitter))
    return delay


def _qpath(store: Path) -> Path:
    path = store / QUEUE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"alert queue store has no {QUEUE_FILE}: {store}")
    return path


def _read_log(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} line {lineno} is not JSON: {exc}")
    return rows


def enqueue(store: Path, alert_id: str, payload: dict,
            max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Queue one alert. Creates the store on first use; appends durably.

    Writes the enqueue INDEX row first, then the queue row. The index is
    audit()'s expected set: a torn write (index without queue row) fails
    loud as an unindexed-orphan case, and a dropped queue row fails as
    `unaccounted`. Enqueueing the same id twice is refused — the index is
    the dedupe record, and a double-enqueue would let two terminal rows
    fight over one id.
    """
    if not alert_id or not isinstance(alert_id, str):
        raise ValueError("alert_id must be a non-empty string")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    store.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if (store / INDEX_FILE).is_file():
        for row in _read_log(store / INDEX_FILE):
            if row.get("alert_id") == alert_id:
                raise ValueError(f"alert already enqueued: {alert_id}")
    durable_append(store / INDEX_FILE,
                   (json.dumps({"alert_id": alert_id, "at": now}) + "\n"
                    ).encode("utf-8"))
    row = {"event": "enqueued", "alert_id": alert_id, "payload": payload,
           "max_attempts": max_attempts, "at": now}
    durable_append(store / QUEUE_FILE,
                   (json.dumps(row) + "\n").encode("utf-8"))
    return row


def _expected_ids(store: Path) -> set[str]:
    """Ids the queue must account for: the enqueue index, when present.

    Stores created before the index (or with it deleted) fall back to the
    queue log's own enqueued rows — a weaker gate, and audit() says so via
    ``index_present: false`` rather than claiming index strength.
    """
    index = store / INDEX_FILE
    if not index.is_file():
        return set()
    return {r["alert_id"] for r in _read_log(index) if r.get("alert_id")}


def replay(store: Path) -> dict[str, dict]:
    """Fold the queue log into per-alert state. Pure read; raises on a
    missing store or a malformed row (never a silent empty)."""
    states: dict[str, dict] = {}
    for row in _read_log(_qpath(store)):
        aid = row.get("alert_id")
        event = row.get("event")
        if not aid or event not in ("enqueued", "attempt_failed",
                                    "delivered", "spilled"):
            raise ValueError(f"queue log holds an unknown row: {row!r:.120}")
        st = states.setdefault(aid, {"attempts": 0, "terminal": None,
                                     "next_attempt_at": None,
                                     "max_attempts": DEFAULT_MAX_ATTEMPTS,
                                     "payload": None})
        if event == "enqueued":
            st["max_attempts"] = int(row.get("max_attempts",
                                             DEFAULT_MAX_ATTEMPTS))
            st["payload"] = row.get("payload")
        elif event == "attempt_failed":
            st["attempts"] += 1
            st["next_attempt_at"] = row.get("next_attempt_at")
        elif event in ("delivered", "spilled"):
            st["terminal"] = event
    return states


def pending(store: Path, now: str | None = None) -> list[str]:
    """Enqueued, non-terminal alert ids, oldest first. ``now`` (ISO-8601)
    restricts to due alerts; None returns all pending regardless of backoff."""
    states = replay(store)
    ids = [aid for aid, st in states.items() if st["terminal"] is None]
    if now is not None:
        ids = [aid for aid in ids
               if (states[aid]["next_attempt_at"] or "") <= now]
    return ids


def record_failure(store: Path, alert_id: str, error: str,
                   now: str, jitter: float = 0.0,
                   rng: random.Random | None = None) -> dict:
    """Append an attempt_failed row with the next backoff deadline."""
    states = replay(store)
    if alert_id not in states:
        raise KeyError(f"no such alert: {alert_id}")
    attempts = states[alert_id]["attempts"] + 1
    at = datetime.datetime.fromisoformat(now)
    nxt = at + datetime.timedelta(
        seconds=backoff(attempts, jitter=jitter, rng=rng))
    row = {"event": "attempt_failed", "alert_id": alert_id, "error": error,
           "attempts": attempts,
           "next_attempt_at": nxt.isoformat(), "at": now}
    durable_append(store / QUEUE_FILE,
                   (json.dumps(row) + "\n").encode("utf-8"))
    return row


def record_delivery(store: Path, alert_id: str, now: str,
                    detail: str = "") -> dict:
    row = {"event": "delivered", "alert_id": alert_id, "detail": detail,
           "at": now}
    durable_append(_qpath(store),
                   (json.dumps(row) + "\n").encode("utf-8"))
    return row


def spill(store: Path, alert_id: str, reason: str, now: str) -> dict:
    """Terminal overflow/exhaustion path. Appends ``spilled`` to the queue
    log AND the full entry to spill.jsonl, and returns the page signal the
    caller must send. Deleting without both appends is the drop path this
    module exists to replace — the suite fails any mutant that does it."""
    states = replay(store)
    if alert_id not in states:
        raise KeyError(f"no such alert: {alert_id}")
    st = states[alert_id]
    entry = {"alert_id": alert_id, "payload": st["payload"],
             "attempts": st["attempts"], "reason": reason, "at": now}
    durable_append(store / SPILL_FILE,
                   (json.dumps(entry) + "\n").encode("utf-8"))
    row = {"event": "spilled", "alert_id": alert_id, "reason": reason,
           "attempts": st["attempts"], "at": now}
    durable_append(store / QUEUE_FILE,
                   (json.dumps(row) + "\n").encode("utf-8"))
    return {"page": True, "alert_id": alert_id, "reason": reason,
            "attempts": st["attempts"], "at": now}


def drain(store: Path, deliver_fn, page_fn, now: str,
          max_depth: int = DEFAULT_MAX_DEPTH,
          jitter: float = 0.0,
          rng: random.Random | None = None) -> dict:
    """Deliver every due alert; spill exhausted and overflowed ones.

    deliver_fn(alert_id, payload) returns ("ok", detail) or raises / returns
    ("fail", error). page_fn(signal) sends the page (Discord/hook) and may be
    a recorder in tests. Returns a summary; ``undelivered`` > 0 means work
    remains (same cron contract as org_request_queue.sh notify --drain).
    """
    summary = {"delivered": [], "failed": [], "spilled": [],
               "pages": 0, "undelivered": 0}
    due = pending(store, now)
    states = replay(store)

    # Overflow first: shed oldest-first down to the bound, loud, never
    # silent. `due` is oldest-first (pending() preserves log order), so the
    # shed set is the TAIL: the newest arrivals beyond the bound survive and
    # the oldest are spilled with their ids in the aggregated page.
    overflow = due[:-max_depth] if len(due) > max_depth else []
    if overflow:
        signal = {"page": True, "reason": "overflow",
                  "alert_ids": overflow, "at": now}
        page_fn(signal)
        summary["pages"] += 1
        for aid in overflow:
            spill(store, aid, "overflow", now)
            summary["spilled"].append(aid)
        due = [aid for aid in due if aid not in set(overflow)]
        states = replay(store)

    for aid in due:
        st = states[aid]
        try:
            outcome = deliver_fn(aid, st["payload"])
        except Exception as exc:  # noqa: BLE001 — transport errors are data
            outcome = ("fail", f"{type(exc).__name__}: {exc}")
        status, detail = outcome
        if status == "ok":
            record_delivery(store, aid, now, str(detail))
            summary["delivered"].append(aid)
        else:
            attempts = st["attempts"] + 1
            if attempts >= st["max_attempts"]:
                record_failure(store, aid, str(detail), now,
                               jitter=jitter, rng=rng)
                signal = spill(store, aid, "exhausted", now)
                page_fn(signal)
                summary["pages"] += 1
                summary["spilled"].append(aid)
            else:
                record_failure(store, aid, str(detail), now,
                               jitter=jitter, rng=rng)
                summary["failed"].append(aid)
    summary["undelivered"] = len(pending(store))
    return summary


def audit(store: Path) -> dict:
    """Account every expected id as pending, delivered, or spilled.

    The expected set is the enqueue INDEX (_expected_ids), not the queue
    log itself: deriving "expected" from the same log under audit is the
    self-grading gate that reads a dropped row as "never existed". With
    the index, a drop-path mutant (queue row removed, index intact)
    surfaces under ``unaccounted`` — the gate the suite's drop-path
    fixture asserts. A second impossibility lands there too: attempts at
    or past max_attempts with no terminal row means exhaustion owed a
    spill that never landed (torn write between record_failure and spill,
    or a no-spill mutant) — drain() always spills in the same pass it
    exhausts, so "exhausted but pending" cannot occur honestly.
    ``orphan_terminal`` flags terminal rows with no enqueue (log
    tampering, same family); ``index_present: false`` marks the weaker
    fallback for pre-index stores.
    """
    rows = _read_log(_qpath(store))
    indexed = _expected_ids(store)
    logged = {r["alert_id"] for r in rows if r.get("event") == "enqueued"}
    expected = indexed if indexed else logged
    states = replay(store)
    out = {"enqueued": len(expected), "pending": [], "delivered": [],
           "spilled": [], "unaccounted": [], "orphan_terminal": [],
           "index_present": bool(indexed)}
    for aid in sorted(expected):
        st = states.get(aid)
        if st is None:
            out["unaccounted"].append(aid)
        elif st["terminal"] == "delivered":
            out["delivered"].append(aid)
        elif st["terminal"] == "spilled":
            out["spilled"].append(aid)
        elif st["attempts"] >= st["max_attempts"]:
            out["unaccounted"].append(aid)
        else:
            out["pending"].append(aid)
    terminal_ids = {r["alert_id"] for r in rows
                    if r.get("event") in ("delivered", "spilled")}
    out["orphan_terminal"] = sorted(terminal_ids - logged)
    return out


if __name__ == "__main__":
    print("alert_queue: library module (see test_alert_queue.py); "
          "no CLI. Refusing.", file=sys.stderr)
    raise SystemExit(2)
