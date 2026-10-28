"""Additional CLI regression coverage for deadline summary review findings."""

from test_garm_deadline_summary_acceptance import (
    DEFAULT_READY_TIMEOUT_MS,
    DeadlineSummaryBase,
    init_baseline,
    mature_baseline,
    parse_samples,
    record,
    samples_doc,
)


class DeadlineSummaryReviewTest(DeadlineSummaryBase):
    def test_max_excess_is_order_independent_for_three_exceedances(self):
        self.write_baseline(mature_baseline())
        for durations in ([650, 800, 700], [700, 800, 650],
                          [800, 650, 700]):
            with self.subTest(durations=durations):
                self.write_samples(list(durations) + [100] * 7)
                body, proc = self.run_both()
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(body["verdict"], "ok")
                self.assert_evaluated(body["deadline_summary"],
                                      DEFAULT_READY_TIMEOUT_MS, 10, 3, 200000)

    def test_provisional_baseline_preserves_supplied_deadline(self):
        base = init_baseline("synthetic fixture")
        base["thresholds"]["ready_timeout_ms"] = 150000
        self.write_baseline(base)
        self.write_samples([100] * 9 + [700])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(body["verdict"], "unknown")
        self.assert_not_evaluated(body["deadline_summary"], 150000)

    def test_nonprovisional_baseline_below_minimum_not_evaluated(self):
        base = init_baseline("synthetic fixture")
        base = record(base, parse_samples(samples_doc([120] * 5)),
                      "synthetic fixture", "tester", "2026-10-03T00:00:00Z")
        base["provisional"] = False
        base["thresholds"]["ready_timeout_ms"] = 150000
        self.write_baseline(base)
        self.write_samples([100] * 9 + [700])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(body["verdict"], "unknown")
        self.assert_not_evaluated(body["deadline_summary"], 150000)
