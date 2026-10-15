#!/usr/bin/env python3
"""Offline tests for ops/case_56fff4c6/alert_queue.py (case_56fff4c6).

No network, no credentials, no broker, no clock reads: every case runs on
a mktemp store with an injected `now` string, an injected deliver_fn, and a
recording page_fn. Style follows test_platform_watchdog.py (stdlib
unittest, fixture builders, one behavior per test).

WHAT THIS SUITE IS BUILT TO CATCH, beyond the happy path:

 * A SILENT DROP. The whole module exists to replace the on-call gate
   202-drop, so the drop-path positive control is the load-bearing test: a
   fixture with an enqueued id whose rows were deleted must fail audit()
   with that id under `unaccounted`, and a no-spill mutant (spill replaced
   by removal) must fail the exhaustion/overflow assertions. A suite that
   passes a queue that deletes is worse than no suite.
 * A THUNDERING RETRY. Backoff is pinned exact (60/120/240/480/960, then
   the 3600 cap): a flat-retry or uncapped mutant fails the table, and a
   retry-before-deadline case proves not-due alerts are skipped, not hammered.
 * AN UNBOUNDED QUEUE. Overflow spills oldest-first with ONE aggregated
   page: the case pins the spilled ids, the survivor set, and the page
   count, so a silent-trim mutant fails three ways.
 * AN UNMEASURED GREEN. A missing store and a malformed row raise — the
   drain/audit never return "all clear" on unreadable state.
"""

import json
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "ops" / "case_56fff4c6"))
sys.path.insert(0, str(HERE / "lib"))

from alert_queue import (  # noqa: E402
    audit,
    backoff,
    drain,
    enqueue,
    pending,
    record_failure,
    replay,
    spill,
)


def iso(hour, minute=0, second=0):
    return f"2026-10-03T{hour:02d}:{minute:02d}:{second:02d}+00:00"


class Recorder:
    def __init__(self):
        self.pages = []

    def __call__(self, signal):
        self.pages.append(signal)


def failing(errors):
    def deliver(alert_id, payload):
        err = errors.pop(0) if errors else "boom"
        return ("fail", err)
    return deliver


def succeeding(detail="sent"):
    def deliver(alert_id, payload):
        return ("ok", detail)
    return deliver


class BackoffTest(unittest.TestCase):
    def test_backoff_table_is_exact(self):
        self.assertEqual(
            [backoff(n) for n in (1, 2, 3, 4, 5)],
            [60.0, 120.0, 240.0, 480.0, 960.0])

    def test_backoff_caps_at_one_hour(self):
        self.assertEqual(backoff(7), 3600.0)
        self.assertEqual(backoff(20), 3600.0)
        self.assertEqual(backoff(6), 1920.0)  # last uncapped step

    def test_backoff_rejects_zero_attempts(self):
        with self.assertRaises(ValueError):
            backoff(0)

    def test_zero_jitter_is_deterministic(self):
        rng = random.Random(7)
        first = [backoff(3, jitter=0.2, rng=rng) for _ in range(5)]
        rng = random.Random(7)
        second = [backoff(3, jitter=0.2, rng=rng) for _ in range(5)]
        self.assertNotEqual(first[0], 240.0)  # jitter actually perturbs
        self.assertEqual(first, second)  # same seed, same delays
        for _ in range(100):
            d = backoff(1)
            self.assertEqual(d, 60.0)  # jitter=0 default is exact


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case_df6ed188-q-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store"
        self.pages = Recorder()

    def qlog(self):
        return (self.store / "queue.jsonl").read_text()

    def test_missing_store_raises_never_green(self):
        with self.assertRaises(FileNotFoundError):
            replay(self.tmp / "no-such-store")

    def test_malformed_row_raises(self):
        self.store.mkdir()
        (self.store / "queue.jsonl").write_text("not json\n")
        with self.assertRaises(ValueError):
            replay(self.store)

    def test_unknown_row_shape_raises(self):
        self.store.mkdir()
        (self.store / "queue.jsonl").write_text('{"alert_id":"a"}\n')
        with self.assertRaises(ValueError):
            replay(self.store)

    def test_enqueue_validates(self):
        with self.assertRaises(ValueError):
            enqueue(self.store, "", {})
        with self.assertRaises(ValueError):
            enqueue(self.store, "a", {}, max_attempts=0)

    def test_happy_path(self):
        enqueue(self.store, "a1", {"alertname": "x"})
        self.assertEqual(pending(self.store), ["a1"])
        summary = drain(self.store, succeeding(), self.pages, iso(12))
        self.assertEqual(summary["delivered"], ["a1"])
        self.assertEqual(summary["failed"], [])
        self.assertEqual(summary["spilled"], [])
        self.assertEqual(self.pages.pages, [])
        self.assertEqual(summary["undelivered"], 0)
        states = replay(self.store)
        self.assertEqual(states["a1"]["terminal"], "delivered")

    def test_retry_then_recover_with_growing_deadlines(self):
        enqueue(self.store, "a1", {"alertname": "x"}, max_attempts=5)
        errs = ["t1", "t2"]
        summary = drain(self.store, failing(errs), self.pages, iso(12))
        self.assertEqual(summary["failed"], ["a1"])
        self.assertEqual(summary["undelivered"], 1)
        st = replay(self.store)["a1"]
        self.assertEqual(st["attempts"], 1)
        first_deadline = st["next_attempt_at"]
        # Not due yet (first deadline is 12:01): skipped at 12:00:30,
        # never retried early, never declared lost.
        self.assertEqual(pending(self.store, iso(12, 0, 30)), [])
        self.assertEqual(pending(self.store), ["a1"])
        # Past the first deadline: due, fails again with a later deadline.
        summary = drain(self.store, failing(errs), self.pages, iso(13))
        st = replay(self.store)["a1"]
        self.assertEqual(st["attempts"], 2)
        self.assertGreater(st["next_attempt_at"], first_deadline)
        # Third attempt succeeds: delivered, no page ever sent.
        summary = drain(self.store, succeeding(), self.pages, iso(14))
        self.assertEqual(summary["delivered"], ["a1"])
        self.assertEqual(self.pages.pages, [])

    def test_exhaustion_spills_and_pages_exactly_once(self):
        enqueue(self.store, "a1", {"alertname": "x"}, max_attempts=3)
        summary = drain(self.store, failing(["e1"]), self.pages, iso(12))
        self.assertEqual(summary["failed"], ["a1"])
        summary = drain(self.store, failing(["e2"]), self.pages, iso(13))
        self.assertEqual(summary["failed"], ["a1"])
        summary = drain(self.store, failing(["e3"]), self.pages, iso(14))
        self.assertEqual(summary["spilled"], ["a1"])
        self.assertEqual(len(self.pages.pages), 1)
        sig = self.pages.pages[0]
        self.assertTrue(sig["page"])
        self.assertEqual(sig["reason"], "exhausted")
        self.assertEqual(sig["alert_id"], "a1")
        # The spill record is durable and complete.
        spill_rows = (self.store / "spill.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(spill_rows), 1)
        entry = json.loads(spill_rows[0])
        self.assertEqual(entry["alert_id"], "a1")
        self.assertEqual(entry["reason"], "exhausted")
        self.assertEqual(entry["attempts"], 3)
        self.assertIn("alertname", entry["payload"])
        # Terminal: no further retry, no second page.
        self.assertEqual(pending(self.store), [])
        self.assertEqual(audit(self.store)["spilled"], ["a1"])

    def test_no_spill_mutant_goes_red(self):
        # Positive control: a drop-path mutant that DELETES the exhausted
        # entry instead of spilling must fail audit — silently losing the
        # alert is the 202-drop this module replaces. The enqueue index is
        # intact, so the vanished queue row reads as `unaccounted`, not as
        # "never existed".
        enqueue(self.store, "a1", {"alertname": "x"}, max_attempts=1)
        drain(self.store, failing(["boom"]), self.pages, iso(12))
        (self.store / "spill.jsonl").write_text("")  # mutant: no spill file
        lines = [json.loads(x) for x in
                 (self.store / "queue.jsonl").read_text().splitlines()
                 if x.strip()]
        kept = [x for x in lines if x.get("event") != "spilled"]
        (self.store / "queue.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in kept))
        rep = audit(self.store)
        self.assertEqual(rep["unaccounted"], ["a1"])
        self.assertEqual(rep["pending"], [])
        self.assertEqual((self.store / "spill.jsonl").read_text().strip(), "")

    def test_double_enqueue_is_refused(self):
        enqueue(self.store, "a1", {"x": 1})
        with self.assertRaises(ValueError):
            enqueue(self.store, "a1", {"x": 2})
        # The first enqueue still accounts cleanly; the refusal wrote nothing.
        self.assertEqual(audit(self.store)["pending"], ["a1"])

    def test_audit_reports_index_strength(self):
        enqueue(self.store, "a1", {"x": 1})
        self.assertTrue(audit(self.store)["index_present"])

    def test_overflow_spills_oldest_first_with_one_aggregated_page(self):
        for i in range(5):
            enqueue(self.store, f"a{i}", {"n": i}, max_attempts=9)
        summary = drain(self.store, failing(["x"] * 9), self.pages, iso(12),
                        max_depth=3)
        self.assertEqual(summary["spilled"], ["a0", "a1"])
        self.assertEqual(len(self.pages.pages), 1)
        self.assertEqual(self.pages.pages[0]["reason"], "overflow")
        self.assertEqual(self.pages.pages[0]["alert_ids"], ["a0", "a1"])
        spilled = audit(self.store)["spilled"]
        self.assertEqual(sorted(spilled), ["a0", "a1"])
        # Survivors keep their order and keep retrying, not lost.
        self.assertEqual(sorted(pending(self.store)), ["a2", "a3", "a4"])
        # At the bound exactly: no spill, no page.
        store2 = self.tmp / "store2"
        for i in range(3):
            enqueue(store2, f"b{i}", {}, max_attempts=9)
        pages2 = Recorder()
        summary = drain(store2, failing(["x"] * 9), pages2, iso(12),
                        max_depth=3)
        self.assertEqual(summary["spilled"], [])
        self.assertEqual(pages2.pages, [])

    def test_audit_clean_on_mixed_store(self):
        enqueue(self.store, "ok", {"a": 1})
        enqueue(self.store, "retry", {"a": 2})
        enqueue(self.store, "gone", {"a": 3}, max_attempts=1)
        drain(self.store, succeeding(), self.pages, iso(12))
        # retry fails once (still pending), gone exhausts on max_attempts=1...
        # drain delivered all three above; redo on fresh ids for clarity:
        store2 = self.tmp / "mixed"
        enqueue(store2, "d", {"a": 1})
        enqueue(store2, "f", {"a": 2}, max_attempts=9)
        enqueue(store2, "s", {"a": 3}, max_attempts=1)
        pages2 = Recorder()
        drain(store2, succeeding(), pages2, iso(12))  # delivers d only? no:
        # succeeding delivers everything due — build the mix explicitly:
        store3 = self.tmp / "mixed3"
        enqueue(store3, "d", {"a": 1})
        drain(store3, succeeding(), Recorder(), iso(12))
        enqueue(store3, "f", {"a": 2}, max_attempts=9)
        drain(store3, failing(["e"]), Recorder(), iso(12))
        enqueue(store3, "s", {"a": 3}, max_attempts=1)
        drain(store3, failing(["e"]), Recorder(), iso(13))
        rep = audit(store3)
        self.assertEqual(rep["delivered"], ["d"])
        self.assertEqual(rep["pending"], ["f"])
        self.assertEqual(rep["spilled"], ["s"])
        self.assertEqual(rep["unaccounted"], [])
        self.assertEqual(rep["orphan_terminal"], [])

    def test_drop_path_fixture_fails_the_gate(self):
        # THE load-bearing positive control: an enqueued id whose rows were
        # removed (the 202-drop shape: accepted, then vanished) must surface
        # under `unaccounted`. If this ever passes on a dropped id, the gate
        # is vacuous and the suite has failed its purpose.
        enqueue(self.store, "kept", {"a": 1})
        enqueue(self.store, "dropped", {"a": 2})
        lines = (self.store / "queue.jsonl").read_text().splitlines(keepends=True)
        (self.store / "queue.jsonl").write_text(
            "".join(x for x in lines if '"dropped"' not in x))
        rep = audit(self.store)
        self.assertEqual(rep["unaccounted"], ["dropped"])
        self.assertEqual(rep["pending"], ["kept"])

    def test_orphan_terminal_flags_tampering(self):
        self.store.mkdir()
        (self.store / "queue.jsonl").write_text(
            '{"event": "enqueued", "alert_id": "a", "payload": {}, "at": "x"}\n'
            '{"event": "delivered", "alert_id": "ghost", "at": "x"}\n')
        self.assertEqual(audit(self.store)["orphan_terminal"], ["ghost"])

    def test_spill_rejects_unknown_id(self):
        self.store.mkdir()
        (self.store / "queue.jsonl").write_text("")
        with self.assertRaises(KeyError):
            spill(self.store, "nope", "overflow", iso(12))
        with self.assertRaises(KeyError):
            record_failure(self.store, "nope", "e", iso(12))

    def test_deliver_exception_is_a_failure_not_a_crash(self):
        enqueue(self.store, "a1", {}, max_attempts=9)

        def explodes(aid, payload):
            raise ConnectionError("sink down")

        summary = drain(self.store, explodes, self.pages, iso(12))
        self.assertEqual(summary["failed"], ["a1"])
        self.assertIn("ConnectionError", self.qlog())


if __name__ == "__main__":
    unittest.main()
