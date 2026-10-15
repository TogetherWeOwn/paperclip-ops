import subprocess
import unittest
from unittest import mock

import live_run_guard

ISSUE = "00000000-0000-4000-8000-000000000030"


def psql(stdout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class LiveWorkGuardTest(unittest.TestCase):
    def test_identifier_that_is_not_a_uuid_counts_as_live(self):
        with mock.patch.object(live_run_guard.subprocess, "run") as run:
            self.assertTrue(live_run_guard.issue_has_live_work("case_f3bda97c"))
            run.assert_not_called()

    def test_lookup_failure_counts_as_live(self):
        with mock.patch.object(live_run_guard.subprocess, "run", side_effect=OSError("no docker")):
            self.assertTrue(live_run_guard.issue_has_live_work(ISSUE))

    def test_empty_lookup_output_counts_as_live(self):
        with mock.patch.object(live_run_guard.subprocess, "run", return_value=psql("")):
            self.assertTrue(live_run_guard.issue_has_live_work(ISSUE))

    def test_no_running_queued_or_retry_row_is_not_live(self):
        with mock.patch.object(live_run_guard.subprocess, "run", return_value=psql("f\n")):
            self.assertFalse(live_run_guard.issue_has_live_work(ISSUE))

    def test_running_queued_or_retry_row_is_live(self):
        with mock.patch.object(live_run_guard.subprocess, "run", return_value=psql("t\n")):
            self.assertTrue(live_run_guard.issue_has_live_work(ISSUE))

    def test_query_matches_task_keyed_runs_and_counts_scheduled_retries(self):
        with mock.patch.object(live_run_guard.subprocess, "run", return_value=psql("f\n")) as run:
            live_run_guard.issue_has_live_work(ISSUE)
        query = run.call_args.args[0][-1]
        self.assertIn("coalesce(h.context_snapshot->>'issueId', h.context_snapshot->>'taskId')", query)
        self.assertIn("'scheduled_retry'", query)


if __name__ == "__main__":
    unittest.main()
