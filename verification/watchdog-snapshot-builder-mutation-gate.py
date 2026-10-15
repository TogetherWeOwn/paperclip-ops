#!/usr/bin/env python3
"""Offline assertion-killed mutants for the snapshot builder (case_d42263d6).

Like platform-watchdog-cli-mutation-gate.py, every mutant runs in a fresh
copy after a green baseline. Parse failures, errors, skips, loader failures,
timeouts and missing named assertion kills refuse. The checkout is untouched.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = "watchdog/snapshot_builder.py"
MODULE = "test_watchdog_snapshot_builder"
FIXTURES = "test/fixtures/watchdog-snapshot-builder"

# (label, unique anchor, replacement, assertion test required to kill it)
MUTANTS = [
    ("medium-priority-is-focus",
     'FOCUS_PRIORITIES = frozenset({"high", "critical"})',
     'FOCUS_PRIORITIES = frozenset({"high", "critical", "medium"})',
     "IssuesTest.test_focus_is_high_or_critical_in_todo_in_progress_or_in_review"),
    ("todo-counted-as-backlog",
     '        if row["status"] == "backlog":\n            entry["inBacklogMin"]',
     '        if row["status"] in ("backlog", "todo"):\n            entry["inBacklogMin"]',
     "IssuesTest.test_backlog_minutes_count_only_for_backlog_cards"),
    ("missing-assignee-invented-as-null",
     '              and "assigneeAgentId" in row\n',
     '',
     "IssuesTest.test_hostile_rows_leave_the_section_absent"),
    ("manager-url-key-lost",
     '            entry["owningLead"] = lead_key[row["reportsTo"]]',
     '            entry["owningLead"] = row["reportsTo"]',
     "AgentsTest.test_status_and_owning_lead_come_from_the_manager_url_key"),
    ("stall-wakes-filtered-out",
     '        if run.wake in (SWEEP_WAKE_REASON, "issue_commented"):',
     '        if run.wake == "issue_commented":',
     "RunEventsTest.test_four_sweep_wakes_in_six_hours_fire_the_churn_detector"),
    ("future-events-counted",
     '        if not run.issue or not start <= run.created <= now:',
     '        if not run.issue or run.created < start:',
     "RunEventsTest.test_future_wakes_are_not_counted"),
    ("run-recovery-double-counted",
     '        if start <= at <= now and (run_id is None or run_id not in counted):',
     '        if start <= at <= now:',
     "RunEventsTest.test_a_recovery_event_for_a_counted_run_is_the_same_wake"),
    ("park-count-invented-zero",
     '                     r.wake == DISPOSITION_WAKE_REASON for r in issue_runs),',
     '                     False for r in issue_runs),',
     "ParksTest.test_three_repair_wakes_fire_the_missing_disposition_finding"),
    ("setup-failures-ignored",
     '                     r.error == "setup_failed" for r in issue_runs)}',
     '                     False for r in issue_runs)}',
     "ParksTest.test_run_comment_and_setup_failure_counts_are_per_issue"),
    ("every-comment-is-a-self-wake",
     '    return comment["authorAgentId"] == run.agent',
     '    return True',
     "ParksTest.test_other_agent_and_human_comments_are_not_self_wakes"),
    ("live-run-invented-zero",
     '"liveRunCount": max(live[row["id"]], int(active == "running")),',
     '"liveRunCount": 0,',
     "AssignedInProgressTest.test_a_live_run_keeps_the_stranded_detector_quiet"),
    ("scheduled-retry-not-counted",
     'if r.status in ("queued", "scheduled_retry") and r.created <= now)',
     'if r.status == "queued" and r.created <= now)',
     "AssignedInProgressTest.test_a_scheduled_retry_is_a_queued_continuation"),
    ("active-scheduled-retry-not-counted",
     'int(active in ("queued", "scheduled_retry"))',
     'int(active == "queued")',
     "AssignedInProgressTest.test_active_scheduled_retry_also_floors_the_queued_count"),
    ("full-company-page-coverage-skipped",
     '    if need_min is not None and _run_page_may_be_capped(rows, runs):',
     '    if False and need_min is not None and _run_page_may_be_capped(rows, runs):',
     "RunEventsTest.test_a_full_page_that_does_not_reach_six_hours_back_is_absent"),
    ("complete-page-still-needs-six-hour-coverage",
     '    if need_min is not None and _run_page_may_be_capped(rows, runs):',
     '    if need_min is not None and len(rows) >= LIST_CAP:',
     "RunExportContractTest.test_uncapped_pages_prove_recent_sections_without_six_hour_coverage"),
    ("per-agent-full-page-counted-as-complete",
     '    if reason and _run_page_may_be_capped(rows, runs):\n'
     '        raise Unbuildable(f"{label}: full page {reason}")',
     '    if False and reason and _run_page_may_be_capped(rows, runs):\n'
     '        raise Unbuildable(f"{label}: full page {reason}")',
     "RunExportContractTest.test_full_per_agent_page_only_proves_covered_recent_sections"),
    ("uncapped-per-agent-page-rejected",
     '    return len(rows) >= LIST_CAP and len(runs) <= LIST_CAP',
     '    return len(rows) >= LIST_CAP',
     "RunExportContractTest.test_uncapped_per_agent_page_proves_all_time_sections"),
    ("duplicate-rows-prove-uncapped-page",
     '    if reason and _run_page_may_be_capped(rows, runs):',
     '    if reason and len(rows) == LIST_CAP:',
     "RunExportContractTest.test_duplicate_rows_do_not_prove_uncapped_run_history"),
    ("duplicate-rows-skip-recent-coverage",
     '    if need_min is not None and _run_page_may_be_capped(rows, runs):\n'
     '        covered = _minutes(min(run.created for run in runs), now)',
     '    if need_min is not None and len(rows) == LIST_CAP:\n'
     '        covered = _minutes(min(run.created for run in runs), now)',
     "RunExportContractTest.test_duplicate_rows_do_not_prove_recent_coverage"),
    ("999-row-page-treated-as-capped",
     'LIST_CAP = 1000',
     'LIST_CAP = 999',
     "RunExportContractTest.test_short_page_below_cap_proves_all_time_sections"),
    ("per-agent-row-owner-mismatch-accepted",
     '        if any(run.agent != agent_id for run in page_runs):',
     '        if False:',
     "RunExportContractTest.test_bad_per_agent_bundles_are_not_measured"),
    ("unattributed-live-run-counted-as-zero",
     '    if any(run.status in ("running", "queued", "scheduled_retry")\n'
     '           and run.issue is None for run in runs):',
     '    if False:',
     "RunExportContractTest.test_live_or_queued_rows_need_issue_context"),
    ("oldest-success-wins",
     'latest[run.agent] is None or run.finished > latest[run.agent]',
     'latest[run.agent] is None or run.finished < latest[run.agent]',
     "AgentRunsTest.test_last_succeeded_is_the_newest_finish_per_agent"),
    ("partial-red-poll-is-complete",
     'RED_MAIN_COMPLETE_RC = (0, 1)',
     'RED_MAIN_COMPLETE_RC = (0, 1, 3)',
     "RedMainsTest.test_a_poll_that_did_not_see_every_repo_is_absent"),
    ("unwritable-output-exits-zero",
     '        return 2\n    for section, reason in sorted(absent.items()):',
     '        return 0\n    for section, reason in sorted(absent.items()):',
     "CliTest.test_an_unwritable_output_exits_2_and_leaves_no_temp_file"),
    ("unknown-agent-is-healthy",
     'and _text(row.get("status")) in AGENT_STATUSES',
     'and _text(row.get("status")) is not None',
     "ReviewRegressionTest.test_unknown_agent_status_is_not_measured_healthy"),
    ("unknown-run-is-inactive",
     'and _text(row.get("status")) in RUN_STATUSES',
     'and _text(row.get("status")) is not None',
     "ReviewRegressionTest.test_unknown_run_status_cannot_invent_zero_live_runs"),
    ("coverage-rounded-up",
     'return max(0.0, (now - since).total_seconds() / 60)',
     'return max(0.0, round((now - since).total_seconds() / 60, 1))',
     "ReviewRegressionTest.test_full_page_coverage_is_not_rounded_up_to_six_hours"),
    ("creation-proves-completion",
     '    return _run_rows(inputs, now,\n'
     '                     require_complete="cannot prove completion history")',
     '    return _run_rows(inputs, now, require_complete=None)',
     "ReviewRegressionTest.test_full_creation_page_cannot_prove_completion_history"),
    ("fractional-seconds-dropped",
     'return moment.astimezone(datetime.timezone.utc).isoformat().replace(',
     'return moment.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(',
     "ReviewRegressionTest.test_timestamp_precision_preserves_the_lane_threshold"),
    ("comment-prefix-truncated-before-normalizing",
     'value["body"].lstrip()[:COMMENT_KEEP_CHARS]',
     'value["body"][:COMMENT_KEEP_CHARS]',
     "ReviewRegressionTest.test_comment_normalization_precedes_bounded_truncation"),
    ("conflicting-recovery-first-wins",
     'if run_id in by_id and identity != by_id[run_id]:',
     'if False:',
     "ReviewRegressionTest.test_conflicting_recovery_ids_refuse_in_either_order"),
    ("recovery-contradicts-known-run",
     'if (run.issue != issue_id or run.created != at\n'
     '                    or run.wake != SWEEP_WAKE_REASON):',
     'if False:',
     "ReviewRegressionTest.test_recovery_identity_must_agree_with_a_known_run"),
    ("unsupported-clock-is-accepted",
     '        utc - datetime.timedelta(hours=REWOKE_WINDOW_H)',
     '        utc',
     "CliTest.test_out_of_range_clocks_exit_2_without_a_traceback"),
    ("handoff-stale-accepted",
     "    if age > RED_MAIN_HANDOFF_MAX_AGE:",
     "    if False:",
     "RedMainHandoffTest.test_the_thirty_minute_window_is_inclusive_on_both_sides"),
    ("handoff-future-accepted",
     "    if age < -RED_MAIN_HANDOFF_SKEW:",
     "    if False:",
     "RedMainHandoffTest.test_the_thirty_minute_window_is_inclusive_on_both_sides"),
    ("handoff-missing-time-is-now",
     '    measured = _time(doc.get("measuredAt"))',
     '    measured = _time(doc.get("measuredAt")) or _time("2026-10-07T03:00:00Z")',
     "RedMainHandoffTest.test_every_handoff_that_cannot_be_trusted_reads_as_unknown"),
    ("handoff-schema-unchecked",
     'doc.get("schema") != RED_MAIN_HANDOFF_SCHEMA:',
     "False:",
     "RedMainHandoffTest.test_every_handoff_that_cannot_be_trusted_reads_as_unknown"),
    ("handoff-source-dropped",
     '    if handoff is not None and "redMains" in snapshot:',
     "    if False:",
     "RedMainHandoffTest.test_a_fresh_red_handoff_is_read_with_its_source"),
    ("handoff-digest-not-the-file",
     "    digest = hashlib.sha256(raw).hexdigest()",
     '    digest = "0" * 64',
     "RedMainHandoffTest.test_a_fresh_red_handoff_is_read_with_its_source"),
    ("handoff-flags-may-mix",
     "    if args.red_mains_handoff is not None and (",
     "    if False and (",
     "RedMainHandoffTest.test_the_handoff_replaces_the_pair_it_cannot_mix_with"),
    ("handoff-repo-format-unchecked",
     "RED_MAIN_REPO.fullmatch(repo) is not None",
     "True",
     "RedMainHandoffTest.test_a_handoff_row_the_card_is_built_from_must_be_well_formed"),
    ("handoff-signature-format-unchecked",
     "RED_MAIN_SIGNATURE.fullmatch(signature) is not None",
     "True",
     "RedMainHandoffTest.test_a_handoff_row_the_card_is_built_from_must_be_well_formed"),
    ("handoff-read-blocks-on-a-fifo",
     "os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW",
     "os.O_RDONLY | os.O_NOFOLLOW",
     "RedMainHandoffTest.test_a_handoff_that_is_not_a_regular_file_is_unknown_without_blocking"),
    ("handoff-row-cap-dropped",
     "if len(rows) > RED_MAIN_HANDOFF_MAX_ROWS:",
     "if False:",
     "RedMainHandoffTest.test_a_handoff_longer_than_the_poll_scope_is_unknown"),
]

WORKER = '''
import io, json, sys, unittest
import test_watchdog_snapshot_builder as tests
loader = unittest.TestLoader()
suite = (loader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:]
         else loader.loadTestsFromModule(tests))
result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print(json.dumps({
    "success": result.wasSuccessful(), "run": result.testsRun,
    "failures": [t.id() for t, _ in result.failures],
    "errors": [t.id() for t, _ in result.errors],
    "skipped": len(result.skipped), "loader_errors": loader.errors,
    "expected_failures": len(result.expectedFailures),
    "unexpected_successes": len(result.unexpectedSuccesses),
}))
'''


def stage(directory, source):
    (directory / "watchdog").mkdir()
    for name in ("__init__.py", "assignment_contract.py", "detectors.py", "sections.py"):
        shutil.copyfile(ROOT / "watchdog" / name, directory / "watchdog" / name)
    (directory / TARGET).write_text(source, encoding="utf-8")
    shutil.copyfile(ROOT / (MODULE + ".py"), directory / (MODULE + ".py"))
    shutil.copytree(ROOT / FIXTURES, directory / FIXTURES)


def run_suite(directory, names=()):
    proc = subprocess.run(
        [sys.executable, "-B", "-c", WORKER, *names], cwd=directory,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("test worker failed before producing a result")
    result = json.loads(proc.stdout)
    if (not result["run"] or result["errors"] or result["skipped"]
            or result["loader_errors"] or result["expected_failures"]
            or result["unexpected_successes"]):
        raise RuntimeError(f"unattributable test result: {result}")
    return result


def main():
    source = (ROOT / TARGET).read_text(encoding="utf-8")
    scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
        "PAPERCLIP_SCRATCH_DIR")
    with tempfile.TemporaryDirectory(prefix="snapshot-baseline-", dir=scratch) as tmp:
        stage(Path(tmp), source)
        baseline = run_suite(Path(tmp))
        if not baseline["success"]:
            raise RuntimeError(f"baseline failed: {baseline}")
    print(f"BASELINE: {baseline['run']} passed")
    for label, old, new, test in MUTANTS:
        if source.count(old) != 1:
            raise RuntimeError(f"{label}: mutation anchor missing or ambiguous")
        mutated = source.replace(old, new)
        compile(mutated, TARGET, "exec")
        required = MODULE + "." + test
        with tempfile.TemporaryDirectory(prefix="snapshot-mutant-", dir=scratch) as tmp:
            stage(Path(tmp), source)
            control = run_suite(Path(tmp), [required])
            if not control["success"] or control["run"] != 1:
                raise RuntimeError(f"{label}: named baseline failed: {control}")
            (Path(tmp) / TARGET).write_text(mutated, encoding="utf-8")
            result = run_suite(Path(tmp), [required])
        killed = any(f == required or f.startswith(required + " ")
                     for f in result["failures"])
        if result["run"] != control["run"] or result["success"] or not killed:
            raise RuntimeError(f"{label}: missing named assertion kill: {result}")
        print(f"KILLED {label}: {test}")
    print(f"PASS: {len(MUTANTS)} mutants killed")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, SyntaxError,
            subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
