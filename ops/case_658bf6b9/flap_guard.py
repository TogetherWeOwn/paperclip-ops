#!/usr/bin/env python3
# ===========================================================================
# flap_guard.py — reference classifier for the Gatus flap-rate guard
# (case_437d0846, spec: ops/case_658bf6b9/flap-guard.md).
#
# Gatus v5.37.0 has no native flap detection: failure-threshold /
# success-threshold give hysteresis, minimum-reminder-interval damps repeats
# only while one incident stays open. A flapping endpoint — alternating
# bursts that each cross both thresholds — opens a NEW incident per flap,
# i.e. one page per flap. This module implements the spec's guard:
#
#   count incident transitions (firing <-> resolved) in a sliding window;
#   at FLAP_N transitions the endpoint is flapping: hold down pages for
#   HOLD_DOWN_S and emit at most one digest per DIGEST_EVERY_S instead.
#   A persistent unpaged state reconciles when the hold ends OR its own
#   HOLD_DOWN_S deadline arrives: renewal cannot extend that deadline.
#   Returning to the already-delivered state cancels the pending edge.
#
# Pure and deterministic: no I/O, no network, no clock read — the caller
# passes every timestamp. Unknown/degenerate input fails toward PAGING
# (never suppress a page the guard has no evidence for).
# ===========================================================================

import math

FAILURE_THRESHOLD = 3   # consecutive failures to open an incident (live default)
SUCCESS_THRESHOLD = 2   # consecutive successes to close one (live default)
WINDOW_S = 1800         # sliding evaluation window: 30m (~30 samples at 60s)
FLAP_N = 4              # incident transitions in-window that declare flapping
HOLD_DOWN_S = 3600      # page suppression after a flap declaration: 60m
DIGEST_EVERY_S = 1800   # at most one digest per 30m inside a hold-down

FIRE = "fire"
RESOLVE = "resolve"

PAGE = "page"        # steady lifecycle: Discord + Akmatori incident
DIGEST = "digest"    # flap noise: Discord-only summary, no new incident
SILENT = "silent"    # absorbed: no notification decision on this tick


class FlapGuard:
    """Per-endpoint guard. Feed Gatus evaluation results oldest-first."""

    def __init__(self, window_s=WINDOW_S, flap_n=FLAP_N,
                 hold_down_s=HOLD_DOWN_S, digest_every_s=DIGEST_EVERY_S,
                 failure_threshold=FAILURE_THRESHOLD,
                 success_threshold=SUCCESS_THRESHOLD):
        for name, value in (("window_s", window_s),
                            ("hold_down_s", hold_down_s),
                            ("digest_every_s", digest_every_s)):
            if (type(value) not in (int, float)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError("%s must be finite and positive" % name)
        for name, value in (("flap_n", flap_n),
                            ("failure_threshold", failure_threshold),
                            ("success_threshold", success_threshold)):
            if type(value) is not int or value <= 0:
                raise ValueError("%s must be a positive integer" % name)
        self.window_s = window_s
        self.flap_n = flap_n
        self.hold_down_s = hold_down_s
        self.digest_every_s = digest_every_s
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.reset()

    def reset(self):
        self.incident_open = False
        self.consec_fail = 0
        self.consec_ok = 0
        self.transitions = []      # [(epoch, FIRE|RESOLVE)] inside the window
        self.flap_until = None     # epoch: pages held while now < flap_until
        self.last_digest_at = None
        self.notified_open = False # incident state last delivered as a PAGE
        self.pending_since = None  # current unpaged state began at this epoch
        self._last_t = None

    @property
    def open_paged(self):
        """Storm incidents Akmatori holds open for this endpoint (0 or 1)."""
        return 1 if self.notified_open else 0

    def holding(self, t):
        """True while a hold-down covers t."""
        return self.flap_until is not None and t < self.flap_until

    def observe(self, t, ok):
        """Consume one evaluation result. Returns (event|None, route)."""
        if type(ok) is not bool:
            raise ValueError("evaluation result must be boolean")
        if type(t) not in (int, float) or not math.isfinite(t):
            raise ValueError("tick must be finite")
        if self._last_t is not None and t < self._last_t:
            raise ValueError("ticks must be non-decreasing")
        self._last_t = t

        # 1. Incident lifecycle (mirrors the Gatus threshold semantics).
        if ok:
            self.consec_ok += 1
            self.consec_fail = 0
        else:
            self.consec_fail += 1
            self.consec_ok = 0
        event = None
        if not self.incident_open and self.consec_fail >= self.failure_threshold:
            self.incident_open = True
            event = FIRE
        elif self.incident_open and self.consec_ok >= self.success_threshold:
            self.incident_open = False
            event = RESOLVE
        if event is not None:
            self.transitions.append((t, event))

        # Age the outstanding state, not the renewable flap hold. Recovery
        # or re-fire back to the delivered state cancels this pending edge.
        if self.incident_open == self.notified_open:
            self.pending_since = None
        elif self.pending_since is None:
            self.pending_since = t

        # 2. Window prune, hold-down expiry, flap trip.
        cutoff = t - self.window_s
        self.transitions = [(tt, e) for (tt, e) in self.transitions
                            if tt >= cutoff]
        if self.flap_until is not None and t >= self.flap_until:
            self.flap_until = None
        if self.flap_until is None and len(self.transitions) >= self.flap_n:
            self.flap_until = t + self.hold_down_s

        # 3. Reconcile at expiry OR the pending state's own deadline. A
        # renewal cannot defer a sustained unpaged FIRE/RESOLVE forever;
        # continuous flaps cancel/reset the pending edge and stay digested.
        if (self.incident_open != self.notified_open
                and (not self.holding(t)
                     or t - self.pending_since >= self.hold_down_s)):
            event = FIRE if self.incident_open else RESOLVE
            return (event, self._page(event))
        if event is None:
            return (None, SILENT)
        if self.holding(t):
            if (self.last_digest_at is None
                    or t - self.last_digest_at >= self.digest_every_s):
                self.last_digest_at = t
                return (event, DIGEST)
            return (event, SILENT)
        return (event, self._page(event))

    def _page(self, event):
        """Record a delivered PAGE. A FIRE while one is already open
        re-notifies (fails toward paging) without inflating the count."""
        self.notified_open = event == FIRE
        self.pending_since = None
        return PAGE

    def digest_summary(self, endpoint, t):
        """One-line hold-down summary: the digest body (see spec §4)."""
        fires = sum(1 for _, e in self.transitions if e == FIRE)
        resolves = sum(1 for _, e in self.transitions if e == RESOLVE)
        held = "pages held" if self.holding(t) else "hold-down clear"
        current = "firing" if self.incident_open else "resolved"
        pending = (FIRE if self.incident_open else RESOLVE) if (
            self.incident_open != self.notified_open) else "none"
        return (
            "flap digest %s last-%dm: %d transitions "
            "(%d firings, %d resolves); %s; current=%s; pending=%s; "
            "%d storm incident(s) open downstream"
            % (endpoint, self.window_s // 60, len(self.transitions),
               fires, resolves, held, current, pending, self.open_paged)
        )
