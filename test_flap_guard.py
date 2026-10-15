#!/usr/bin/env python3
# ===========================================================================
# test_flap_guard.py — offline proof for the Gatus flap-rate guard
# (case_437d0846, spec: ops/case_658bf6b9/flap-guard.md, impl: ops/case_658bf6b9/flap_guard.py).
#
# Hostile by design: every case targets a plausible-but-wrong guard, and the
# headline case (flapping endpoint digests instead of paging per flap) is the
# acceptance criterion itself. All time is pinned constants; no network, no
# clock read, no writes outside unittest's own asserts.
#
# Layout: §1 steady lifecycle pages (the guard must not eat real incidents) ·
# §2 flapping digests (headline: one page + one digest, never per-flap pages;
# window, hold-down and digest-interval boundaries pinned by a pair of cases
# each so the boundary lives between them) · §3 the guard only DELAYS pages:
# edges swallowed by a hold-down are reconciled when it ends (a flap storm
# that ends in a sustained outage still pages; a paged FIRE still resolves) ·
# §4 degenerate input fails toward PAGES, never toward silence · §5 summaries
# render flap state (the digest body carries the counts, the hold-down state
# and the paged-incident count) · §6 live mutation controls: each guard is
# mutated in-memory (source transform, never written to disk) and the
# matching invariant must go red, so a vacuous case cannot hide.
# ===========================================================================
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "ops", "case_658bf6b9"))
import flap_guard
from flap_guard import (DIGEST, FIRE, PAGE, RESOLVE, SILENT, FlapGuard,
                        FLAP_N, HOLD_DOWN_S, WINDOW_S)

STEP = 60  # Gatus pull interval: one evaluation per 60s
# Spec §2 default digest rate, stated here and deliberately NOT imported from
# the implementation: a test that reads its expectation from the code under
# test passes a weakened code (DIGEST_EVERY_S 1800 -> 1200) unchanged.
SPEC_DIGEST_EVERY_S = 30 * 60


def run(g, marks, t0=1_700_000_000, step=STEP):
    """Feed one bool per step starting at t0. Returns [(t, ev, route)]."""
    return [(t0 + i * step, *g.observe(t0 + i * step, ok))
            for i, ok in enumerate(marks)]


def flap():
    """One flap cycle (3 down, 3 up): opens and resolves one incident."""
    return [False] * 3 + [True] * 3


class SteadyLifecycleTest(unittest.TestCase):
    def test_one_outage_pages_once(self):
        g = FlapGuard()
        out = run(g, [True] * 5 + [False] * 3 + [False] * 10)
        pages = [(t, e) for (t, e, r) in out if r == PAGE]
        self.assertEqual([e for _, e in pages], [FIRE])   # open pages,
        self.assertEqual(g.open_paged, 1)                # one storm incident

    def test_outage_then_recovery_pages_twice(self):
        g = FlapGuard()
        out = run(g, [True] * 5 + [False] * 5 + [True] * 5)
        self.assertEqual([e for (_, e, r) in out if r == PAGE],
                         [FIRE, RESOLVE])
        self.assertEqual(g.open_paged, 0)

    def test_sub_threshold_blips_never_notify(self):
        g = FlapGuard()  # 2-down/1-up forever: never crosses either threshold
        out = run(g, ([False] * 2 + [True]) * 20)
        self.assertFalse(any(r != SILENT for (_, _, r) in out))

    def test_one_flap_still_pages(self):
        # One flap cycle = 2 transitions, below FLAP_N=4: real pages.
        g = FlapGuard()
        out = run(g, flap())
        self.assertEqual([e for (_, e, r) in out if r == PAGE],
                         [FIRE, RESOLVE])
        self.assertFalse(any(r == DIGEST for (_, _, r) in out))

    def test_three_transitions_still_page(self):
        # FIRE, RESOLVE, FIRE = 3 transitions: still below the trip wire.
        g = FlapGuard()
        out = run(g, flap() + [False] * 3)
        self.assertEqual([e for (_, e, r) in out if r == PAGE],
                         [FIRE, RESOLVE, FIRE])
        self.assertFalse(g.holding(1_700_000_000 + 9 * STEP))


class FlapDigestTest(unittest.TestCase):
    def test_flapping_endpoint_digests_not_pages(self):
        g = FlapGuard()
        out = run(g, flap() * 4)   # 8 transitions at idx 2,4,8,10,14..22
        pages = [e for (_, e, r) in out if r == PAGE]
        digests = [e for (_, e, r) in out if r == DIGEST]
        silenced = [e for (_, e, r) in out if r == SILENT and e is not None]
        # The trip wire is the 4th transition (RESOLVE at idx 10): it
        # digests, and the hold-down absorbs everything after. Pages freeze
        # at 3 no matter how many more flaps arrive — never a page per flap.
        self.assertEqual(pages, [FIRE, RESOLVE, FIRE])
        self.assertEqual(digests, [RESOLVE])
        self.assertEqual(silenced, [FIRE, RESOLVE, FIRE, RESOLVE])
        self.assertTrue(g.holding(1_700_000_000 + 23 * STEP))

    def test_hold_down_expires_pages_resume(self):
        g = FlapGuard()
        t0 = 1_700_000_000
        run(g, flap() * 4)
        t_trip = t0 + 10 * STEP
        self.assertTrue(g.holding(t_trip + HOLD_DOWN_S - 1))
        self.assertFalse(g.holding(t_trip + HOLD_DOWN_S))
        # The first observation after expiry reconciles the swallowed
        # RESOLVE (the last paged state was FIRE): a PAGE, not silence.
        t1 = t_trip + HOLD_DOWN_S + WINDOW_S + STEP
        self.assertEqual(g.observe(t1, True), (RESOLVE, PAGE))
        # A fresh outage on the SAME guard then pages normally: old
        # transitions aged out, the wire is clear, thresholds re-arm.
        routes = [g.observe(t1 + (i + 1) * STEP, False)[1] for i in range(3)]
        self.assertEqual(routes, [SILENT, SILENT, PAGE])

    def test_old_transitions_age_out_of_window(self):
        # One flap, a full quiet window, one more flap: at most 2
        # transitions are ever in-window together, so the wire never trips
        # and every transition pages.
        g = FlapGuard()
        gap = [True] * (WINDOW_S // STEP + 2)
        out = run(g, flap() + gap + flap())
        self.assertEqual([e for (_, e, r) in out if r == PAGE],
                         [FIRE, RESOLVE, FIRE, RESOLVE])
        self.assertFalse(any(r == DIGEST for (_, _, r) in out))

    def test_digest_interval_absorbs_mid_cycle_flaps(self):
        g = FlapGuard()
        out = run(g, flap() * 4)   # trip wire at idx 10
        by_idx = {(t - 1_700_000_000) // STEP: (e, r)
                  for (t, e, r) in out if e is not None}
        self.assertEqual(by_idx[10], (RESOLVE, DIGEST))  # trip digests once
        for i in (14, 16, 20, 22):                      # inside the 30m rate
            self.assertEqual(by_idx[i][1], SILENT)       # ...rest absorb

def edge_guard():
    """Guard whose every observation is a lifecycle edge (thresholds 1/1).
    Digest rate left at the shipped default: that is what is under test."""
    return FlapGuard(failure_threshold=1, success_threshold=1)


# Four edges at t=0..3 trip the wire on the 4th (a digest at t=3). Two more
# edges then land at exactly 1799s and 1800s after that digest.
TRIP_AT = 3
EDGES_BEFORE_TRIP = [(0, False), (1, True), (2, False), (3, True)]


class DigestRateDefaultTest(unittest.TestCase):
    """The shipped 30m digest rate, pinned by literals the spec states and
    a just-before / at-the-boundary pair (spec §2/§3)."""

    def test_shipped_default_is_30_minutes(self):
        self.assertEqual(SPEC_DIGEST_EVERY_S, 1800)
        self.assertEqual(flap_guard.DIGEST_EVERY_S, 1800)
        self.assertEqual(FlapGuard().digest_every_s, 1800)

    def _edge_after_trip(self, gap):
        g = edge_guard()
        out = [g.observe(t, ok) for t, ok in EDGES_BEFORE_TRIP]
        self.assertEqual(out[-1], (RESOLVE, DIGEST))   # the trip digests
        return g.observe(TRIP_AT + gap, False)

    def test_edge_1799s_after_a_digest_is_absorbed(self):
        self.assertEqual(self._edge_after_trip(1799), (FIRE, SILENT))

    def test_edge_exactly_1800s_after_a_digest_digests_again(self):
        self.assertEqual(self._edge_after_trip(1800), (FIRE, DIGEST))

    def test_boundary_pair_straddles_one_second(self):
        # Back-to-back edges 1799s and 1800s after the trip digest: only the
        # second may digest, so the boundary sits between them.
        g = edge_guard()
        for t, ok in EDGES_BEFORE_TRIP:
            g.observe(t, ok)
        before = g.observe(TRIP_AT + 1799, False)
        at = g.observe(TRIP_AT + 1800, True)
        self.assertEqual(before, (FIRE, SILENT))
        self.assertEqual(at, (RESOLVE, DIGEST))

    def test_configured_interval_is_still_honoured(self):
        # Configurable stays supported: a 600s guard digests at 600s, and
        # not at 599s. Constrains the parameter, not the shipped default.
        for gap, route in ((599, SILENT), (600, DIGEST)):
            g = FlapGuard(failure_threshold=1, success_threshold=1,
                          digest_every_s=600)
            for t, ok in EDGES_BEFORE_TRIP:
                g.observe(t, ok)
            self.assertEqual(g.observe(TRIP_AT + gap, False), (FIRE, route))


# Repro A: a 40-minute outage, then a flap storm that trips the wire on a
# FIRE, then a sustained outage. Pre-fix, the guard digested the trip FIRE
# and then never paged the endpoint that stayed down.
STORM_THEN_OUTAGE = ([False] * 40 + [True] * 3 + [False] * 3 + [True] * 3
                     + [False] * 3 + [False] * 300)
# Repro B: a flap storm whose last paged state is FIRE (RESOLVE digested),
# then a sustained recovery. Pre-fix, the paged FIRE was never resolved.
STORM_THEN_RECOVERY = flap() * 4 + [True] * 300
# Repro C: a storm outlives the first hold and ends just before renewal.
# The old renewal delayed the final unpaged FIRE from tick 105 to 171 (66m).
RENEWED_THEN_OUTAGE = (STORM_THEN_OUTAGE[:52]
                       + ([True] * 3 + [False] * 3) * 9 + [False] * 150)
# Mirror the final state while retaining enough edges to renew the hold.
RENEWED_THEN_RECOVERY = flap() * 21 + [True] * 150


class ReconcileAfterHoldDownTest(unittest.TestCase):
    """The guard only delays pages. Edges swallowed by a hold-down are
    delivered as a PAGE on the tick the hold-down ends (spec §3)."""

    def test_storm_then_sustained_outage_pages_after_hold_down(self):
        g = FlapGuard()
        out = run(g, STORM_THEN_OUTAGE)
        trip = next(i for i, (_, _, r) in enumerate(out) if r == DIGEST)
        self.assertEqual(trip, 51)                 # trip lands on a FIRE
        self.assertEqual(out[trip][1], FIRE)
        later = [(i, e) for i, (_, e, r) in enumerate(out)
                 if r == PAGE and i > trip]
        self.assertEqual(later, [(trip + HOLD_DOWN_S // STEP, FIRE)])
        self.assertTrue(g.incident_open)
        self.assertEqual(g.open_paged, 1)          # Akmatori holds it open

    def test_storm_then_sustained_recovery_resolves_the_page(self):
        g = FlapGuard()
        out = run(g, STORM_THEN_RECOVERY)
        pages = [e for (_, e, r) in out if r == PAGE]
        self.assertEqual(pages, [FIRE, RESOLVE, FIRE, RESOLVE])
        self.assertFalse(g.incident_open)
        self.assertEqual(g.open_paged, 0)          # nothing left open

    def test_reconcile_waits_for_the_hold_down_to_end(self):
        # Pages stay held for the whole hold-down: the reconcile is not an
        # early exit. Last silent tick is one before the expiry tick.
        g = FlapGuard()
        out = run(g, STORM_THEN_OUTAGE)
        t_exp = out[51][0] + HOLD_DOWN_S
        self.assertFalse(any(r == PAGE for (t, _, r) in out
                             if out[51][0] < t < t_exp))
        self.assertEqual([r for (t, _, r) in out if t == t_exp], [PAGE])

    def test_continuous_flapping_stays_digested_for_hours(self):
        # 3h of 6-minute flap cycles: the wire re-trips at every expiry, so
        # the reconcile never fires and pages stay frozen at 3.
        g = FlapGuard()
        out = run(g, flap() * 30)
        self.assertEqual([e for (_, e, r) in out if r == PAGE],
                         [FIRE, RESOLVE, FIRE])
        times = [t for (t, _, r) in out if r == DIGEST]
        self.assertTrue(all(b - a >= SPEC_DIGEST_EVERY_S
                            for a, b in zip(times, times[1:])))
        self.assertGreaterEqual(len(times), 5)

    def test_refire_after_hold_down_does_not_double_count_open(self):
        # FIRE paged, RESOLVE swallowed, then a fresh FIRE on the very tick
        # the hold-down ends: it pages again (fail toward paging) but the
        # open-incident count stays 1, never 2.
        g = FlapGuard()
        out = run(g, flap() * 4 + [True] * 44 + [False] * 3)
        self.assertEqual(out[70][1:], (FIRE, PAGE))
        self.assertEqual(g.open_paged, 1)


class RenewalDeadlineTest(unittest.TestCase):
    def test_renewal_cannot_delay_a_sustained_fire_beyond_60m(self):
        g = FlapGuard()
        out = run(g, RENEWED_THEN_OUTAGE[:166])
        self.assertEqual(out[105][1], FIRE)
        self.assertTrue(all(r != PAGE for _, _, r in out[52:165]))
        self.assertEqual(out[164][2], SILENT)
        self.assertEqual(out[165][1:], (FIRE, PAGE))  # exactly 60m later
        self.assertTrue(g.holding(out[165][0]))      # renewal still active
        self.assertEqual(g.open_paged, 1)

    def test_renewal_cannot_delay_a_sustained_resolve_beyond_60m(self):
        g = FlapGuard()
        out = run(g, RENEWED_THEN_RECOVERY[:185])
        self.assertEqual(out[124][1], RESOLVE)
        self.assertEqual(out[183][2], SILENT)
        self.assertEqual(out[184][1:], (RESOLVE, PAGE))
        self.assertTrue(g.holding(out[184][0]))
        self.assertEqual(g.open_paged, 0)

    def test_sparse_ticks_reconcile_on_first_tick_after_deadline(self):
        g = FlapGuard()
        out = run(g, RENEWED_THEN_OUTAGE[:165])
        deadline = out[105][0] + HOLD_DOWN_S
        self.assertEqual(g.observe(deadline - 1, False), (None, SILENT))
        self.assertEqual(g.observe(deadline + 17, False), (FIRE, PAGE))
        self.assertIsNone(g.pending_since)

    def test_state_returning_to_delivered_state_cancels_pending(self):
        g = FlapGuard()
        run(g, STORM_THEN_OUTAGE[:52])
        self.assertIsNotNone(g.pending_since)
        g.observe(1_700_000_000 + 52 * STEP, True)
        g.observe(1_700_000_000 + 53 * STEP, True)
        self.assertFalse(g.incident_open)
        self.assertIsNone(g.pending_since)


class HeartbeatCadenceTest(unittest.TestCase):
    def test_pull_window_does_not_protect_10m_heartbeats(self):
        out = run(FlapGuard(), flap() * 10, step=600)
        self.assertEqual(sum(r == PAGE for _, _, r in out), 20)
        self.assertFalse(any(r == DIGEST for _, _, r in out))

    def test_10m_profile_counts_lifecycle_edges_in_120m_window(self):
        g = FlapGuard(window_s=7200)
        out = run(g, flap() * 10, step=600)
        self.assertEqual([e for _, e, r in out if r == PAGE],
                         [FIRE, RESOLVE, FIRE])
        self.assertEqual(out[10][1:], (RESOLVE, DIGEST))
        digests = [t for t, _, r in out if r == DIGEST]
        self.assertGreater(len(digests), 1)
        self.assertTrue(all(b - a >= SPEC_DIGEST_EVERY_S
                            for a, b in zip(digests, digests[1:])))

    def test_consecutive_heartbeat_misses_are_one_fire_not_four_edges(self):
        g = FlapGuard(window_s=7200)
        out = run(g, [False] * 20, step=600)
        self.assertEqual([e for _, e, r in out if r == PAGE], [FIRE])
        self.assertFalse(any(r == DIGEST for _, _, r in out))


class FailTowardPagingTest(unittest.TestCase):
    def test_backward_clock_raises_instead_of_silencing(self):
        g = FlapGuard()
        g.observe(100, True)
        with self.assertRaises(ValueError):
            g.observe(99, False)

    def test_invalid_parameters_are_rejected_before_state_is_initialized(self):
        for name in ("window_s", "hold_down_s", "digest_every_s"):
            for value in (0, -1, float("nan"), float("inf"),
                          -float("inf"), True, "60", None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        FlapGuard(**{name: value})
        for name in ("flap_n", "failure_threshold", "success_threshold"):
            for value in (0, -1, 1.5, 3.0, float("nan"), float("inf"),
                          True, "3", None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        FlapGuard(**{name: value})

    def test_non_boolean_observations_cannot_resolve_an_outage(self):
        g = FlapGuard()
        run(g, [False] * 3, t0=0)
        before = repr(vars(g))
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    g.observe(180, value)
                self.assertEqual(repr(vars(g)), before)
        self.assertTrue(g.incident_open)
        self.assertEqual(g.observe(180, True), (None, SILENT))
        self.assertEqual(g.observe(240, True), (RESOLVE, PAGE))

    def test_invalid_timestamps_do_not_mutate_state(self):
        g = FlapGuard()
        for value in (float("nan"), float("inf"), -float("inf"),
                      True, "60", None):
            with self.subTest(value=value):
                before = repr(vars(g))
                with self.assertRaises(ValueError):
                    g.observe(value, False)
                self.assertEqual(repr(vars(g)), before)

    def test_single_transition_never_digests(self):
        self.assertEqual(run(FlapGuard(), [False] * 3)[-1][2], PAGE)

    def test_resolve_without_prior_page_still_pages(self):
        # Degenerate feed: resolves with nothing open still resolve-pages.
        g = FlapGuard()
        g.incident_open = True  # forced: no FIRE was ever paged for it
        g.consec_ok = 1
        self.assertEqual(g.observe(1_700_000_001, True), (RESOLVE, PAGE))


class DigestSummaryTest(unittest.TestCase):
    def test_summary_carries_counts_and_hold_state(self):
        g = FlapGuard()
        run(g, flap() * 4)   # 8 transitions in-window: 4 firings, 4 resolves
        s = g.digest_summary("two-web-prod", 1_700_000_000 + 24 * STEP)
        for token in ("two-web-prod", "8 transitions", "4 firings",
                      "4 resolves", "pages held"):
            self.assertIn(token, s)

    def test_trip_fire_names_actual_unpaged_outage(self):
        g = FlapGuard()
        out = run(g, STORM_THEN_OUTAGE[:52])
        self.assertEqual(out[51][1:], (FIRE, DIGEST))
        s = g.digest_summary("x", out[51][0])
        for token in ("current=firing", "pending=fire",
                      "0 storm incident(s) open downstream"):
            self.assertIn(token, s)

    def test_trip_resolve_names_actual_recovery_and_pending_close(self):
        g = FlapGuard()
        out = run(g, (flap() * 2)[:11])
        self.assertEqual(out[10][1:], (RESOLVE, DIGEST))
        s = g.digest_summary("x", out[10][0])
        for token in ("current=resolved", "pending=resolve",
                      "1 storm incident(s) open downstream"):
            self.assertIn(token, s)

    def test_paged_state_has_no_pending_notification(self):
        g = FlapGuard()
        out = run(g, [False] * 3)
        s = g.digest_summary("x", out[-1][0])
        self.assertIn("current=firing", s)
        self.assertIn("pending=none", s)

    def test_summary_after_expiry_reports_clear(self):
        g = FlapGuard()
        run(g, flap() * 4)
        s = g.digest_summary("x", 1_700_000_000 + 10 * HOLD_DOWN_S)
        self.assertIn("hold-down clear", s)


# §6 live mutation controls. Each control rewrites ONE guard in memory (the
# file on disk is never touched), and the invariant that guard protects must
# go red. A stale anchor fails loudly, so a refactor cannot void a control.
SRC = open(flap_guard.__file__, encoding="utf-8").read()


def mutant(old, new):
    assert SRC.count(old) == 1, "stale mutant anchor: %r" % old
    m = types.ModuleType("flap_guard_mutant")
    exec(compile(SRC.replace(old, new), "<mutant>", "exec"), m.__dict__)
    return m


def _pages(m, marks):
    g = m.FlapGuard()
    out = run(g, marks)
    return g, out, [e for (_, e, r) in out if r == m.PAGE]


def inv_flap_digests(m):
    g, out, pages = _pages(m, flap() * 4)
    return (pages == [FIRE, RESOLVE, FIRE]
            and [e for (_, e, r) in out if r == m.DIGEST] == [RESOLVE])


def inv_hold_down_holds(m):
    return len(_pages(m, flap() * 8)[2]) == 3


def inv_window_prunes(m):
    gap = [True] * (WINDOW_S // STEP + 2)
    g, out, pages = _pages(m, flap() + gap + flap())
    return (pages == [FIRE, RESOLVE, FIRE, RESOLVE]
            and not any(r == m.DIGEST for (_, _, r) in out))


def inv_reconcile_fire(m):
    g, out, pages = _pages(m, STORM_THEN_OUTAGE)
    return pages[-1] == FIRE and pages.count(FIRE) == 3 and g.open_paged == 1


def inv_reconcile_resolve(m):
    g, out, pages = _pages(m, STORM_THEN_RECOVERY)
    return pages[-1] == RESOLVE and g.open_paged == 0


def inv_renewal_deadline(m):
    g, out, pages = _pages(m, RENEWED_THEN_OUTAGE)
    return out[165][1:] == (FIRE, m.PAGE) and g.open_paged == 1


def inv_digest_state(m):
    g, out, pages = _pages(m, STORM_THEN_OUTAGE[:52])
    return "current=firing; pending=fire" in g.digest_summary("x", out[-1][0])


def inv_digest_rate_default(m):
    # Shipped-default spacing on a 3h continuous storm, against the literal.
    _, out, _ = _pages(m, flap() * 30)
    times = [t for (t, _, r) in out if r == m.DIGEST]
    return (m.DIGEST_EVERY_S == SPEC_DIGEST_EVERY_S and len(times) >= 5
            and all(b - a >= SPEC_DIGEST_EVERY_S
                    for a, b in zip(times, times[1:])))


def inv_digest_boundary(m):
    g = m.FlapGuard(failure_threshold=1, success_threshold=1)
    for t, ok in EDGES_BEFORE_TRIP:
        g.observe(t, ok)
    return (g.observe(TRIP_AT + 1799, False) == (FIRE, m.SILENT)
            and g.observe(TRIP_AT + 1800, True) == (RESOLVE, m.DIGEST))


def inv_parameters_rejected(m):
    for args in ({"flap_n": 0}, {"failure_threshold": float("nan")},
                 {"hold_down_s": float("inf")}):
        try:
            m.FlapGuard(**args)
        except ValueError:
            continue
        return False
    return True


def inv_boolean_rejected(m):
    g = m.FlapGuard()
    run(g, [False] * 3, t0=0)
    try:
        g.observe(180, "false")
    except ValueError:
        return g.incident_open
    return False


CONTROLS = [
    ("FLAP_N = 4 ", "FLAP_N = 1000000 ", inv_flap_digests),
    ("DIGEST_EVERY_S = 1800 ", "DIGEST_EVERY_S = 1200 ",
     inv_digest_rate_default),
    ("t - self.last_digest_at >= self.digest_every_s", "True",
     inv_digest_rate_default),
    ("t - self.last_digest_at >= self.digest_every_s",
     "t - self.last_digest_at > self.digest_every_s", inv_digest_boundary),
    ("        if self.holding(t):\n            if (self.last_digest_at",
     "        if False:\n            if (self.last_digest_at", inv_hold_down_holds),
    ("if tt >= cutoff]", "if True]", inv_window_prunes),
    ("if (self.incident_open != self.notified_open\n", "if (False\n",
     inv_reconcile_fire),
    ("if (self.incident_open != self.notified_open\n", "if (False\n",
     inv_reconcile_resolve),
    ("or t - self.pending_since >= self.hold_down_s", "or False",
     inv_renewal_deadline),
    ('current = "firing" if self.incident_open else "resolved"',
     'current = "resolved"', inv_digest_state),
    ('raise ValueError("%s must be a positive integer" % name)',
     'pass', inv_parameters_rejected),
    ('raise ValueError("%s must be finite and positive" % name)',
     'pass', inv_parameters_rejected),
    ('raise ValueError("evaluation result must be boolean")',
     'pass', inv_boolean_rejected),
]


class MutationControlTest(unittest.TestCase):
    def test_every_invariant_holds_on_the_real_guard(self):
        for _, _, inv in CONTROLS:
            self.assertTrue(inv(flap_guard), inv.__name__)

    def test_every_mutant_goes_red(self):
        for old, new, inv in CONTROLS:
            self.assertFalse(inv(mutant(old, new)),
                             "mutant survived: %s" % inv.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
