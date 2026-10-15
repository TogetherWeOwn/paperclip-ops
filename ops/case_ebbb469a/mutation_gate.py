#!/usr/bin/env python3
"""Prove test_runner_census.py can still go red (case_ebbb469a).

A suite that only ever passes proves nothing. For each invariant the census
promises, this gate breaks the module in one precise way and requires the suite
to fail in the test written for that invariant:

  * the baseline (unmutated copy) must pass, or every kill below is meaningless;
  * each anchor must match EXACTLY the expected number of times, or the mutant
    is vacuous (the line it meant to break has moved);
  * the mutant must turn the suite red AND the failing set must contain the
    named test, so a mutant killed only by an unrelated incidental assertion
    does not count.

Mutants are applied to a throwaway COPY of ops/, never the working tree, so a
timeout or Ctrl-C cannot strand a mutated file. Exit 0 only if every mutant is
killed by its named test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPS = HERE.parent
SUITE = HERE / "test_runner_census.py"
TIMEOUT_S = 180
# A mutant that fails to even load (syntax/import/name error) would "kill" the
# suite by accident rather than by the test written for its invariant.
BROKE_MODULE = re.compile(r"^(?:SyntaxError|IndentationError|ImportError|ModuleNotFoundError|NameError)\b", re.M)

PUSH = "push-runner-census.py"
STARVED = '        if asking and not online and oldest_s > grace_s:'

# (name, file relative to the copied ops/ dir, anchor, replacement, expected
#  occurrences of the anchor, test that must fail)
MUTANTS = [
    ("starved-never-fires", f"case_ebbb469a/{PUSH}", STARVED, "        if False:", 1,
     "test_all_offline_with_queue_is_starved_fail"),
    ("demand-alone-fails-ignoring-online", f"case_ebbb469a/{PUSH}", STARVED,
     "        if asking and oldest_s > grace_s:", 1, "test_all_busy_draining_is_info_only"),
    ("grace-boundary-inclusive", f"case_ebbb469a/{PUSH}", STARVED,
     "        if asking and not online and oldest_s >= grace_s:", 1, "test_starved_grace_boundary"),
    ("draining-becomes-a-failure", f"case_ebbb469a/{PUSH}",
     '            row["state"] = "draining"',
     '            row["state"] = "draining"\n            reasons.append("draining")', 1,
     "test_all_busy_draining_is_info_only"),
    ("idle-with-queue-branch-removed", f"case_ebbb469a/{PUSH}",
     "        elif online and asking and oldest_s > grace_s:", "        elif False:", 1,
     "test_idle_capacity_with_old_queue_is_info"),
    ("dual-label-check-removed", f"case_ebbb469a/{PUSH}",
     '    dual = sorted(safe_name(r["name"]) for r in runners if set(EXCLUSIVE_LABELS) <= r["labels"])',
     "    dual = []", 1, "test_dual_labeled_runner_fails"),
    ("runner-name-not-allow-listed", f"case_ebbb469a/{PUSH}",
     '    return text if NAME_RE.fullmatch(text) else "?"', "    return text", 1,
     "test_runner_name_is_allow_listed_before_it_reaches_the_alert"),
    ("api-denied-read-as-empty-census", f"case_ebbb469a/{PUSH}",
     '            raise CensusError(f"github HTTP {exc.code} on {method} {path}") from exc',
     '            return 200, {"total_count": 0, "runners": []}', 1,
     "test_api_denied_is_unknown_not_green"),
    ("malformed-body-defaulted", f"case_ebbb469a/{PUSH}",
     "        if not isinstance(batch, list) or not isinstance(count, int) or isinstance(count, bool) or count < 0:",
     "        batch = batch if isinstance(batch, list) else []\n"
     "        count = count if isinstance(count, int) and not isinstance(count, bool) else len(batch)\n"
     "        if False:", 1, "test_malformed_body_is_unknown_not_green"),
    ("truncation-not-checked", f"case_ebbb469a/{PUSH}",
     "    if len(runners) != total:", "    if False:", 1, "test_truncated_pages_is_unknown_not_green"),
    ("pagination-stops-after-page-one", f"case_ebbb469a/{PUSH}",
     "        if len(runners) >= total or not batch:", "        if True:", 1, "test_every_page_is_read"),
    ("busy-counted-on-offline-runners", f"case_ebbb469a/{PUSH}",
     '        busy = [r for r in online if r["busy"]]', '        busy = [r for r in carrying if r["busy"]]', 1,
     "test_busy_counts_online_runners_only"),
    ("unknown-pushes-green", f"case_ebbb469a/{PUSH}",
     "        push(PUSH_KEY, False, text)", '        push(PUSH_KEY, True, "")', 1,
     "test_api_denied_is_unknown_not_green"),
    ("stale-queued-file-accepted", f"case_ebbb469a/{PUSH}",
     "        if age_s > max_age_min * 60 or age_s < -FUTURE_SKEW_S:", "        if False:", 1,
     "test_queued_json_unreadable_malformed_or_stale_is_unknown"),
    ("future-dated-job-accepted", f"case_ebbb469a/{PUSH}",
     "        if (created - now).total_seconds() > FUTURE_SKEW_S:", "        if False:", 1,
     "test_future_dated_queued_job_is_unknown"),
    ("drain-job-counted-as-demand", f"case_ebbb469a/{PUSH}",
     '        if job.get("status") != "queued" or job.get("name") == ignore_job:',
     '        if job.get("status") != "queued":', 1, "test_healthy_counts_and_ignored_demand"),
    ("non-queued-job-counted-as-demand", f"case_ebbb469a/{PUSH}",
     '        if job.get("status") != "queued" or job.get("name") == ignore_job:',
     '        if job.get("name") == ignore_job:', 1, "test_healthy_counts_and_ignored_demand"),
    ("label-case-not-normalised", f"case_ebbb469a/{PUSH}",
     '            "labels": frozenset(l["name"].strip().lower() for l in raw_labels),',
     '            "labels": frozenset(l["name"].strip() for l in raw_labels),', 1,
     "test_labels_match_case_insensitively"),
    ("gh-token-refusal-removed", f"case_ebbb469a/{PUSH}", "    if not token:", "    if False:", 1,
     "test_missing_credentials_refuse"),
    ("gh-token-reaches-push-subprocess", f"case_ebbb469a/{PUSH}",
     '    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}', "    env = dict(os.environ)", 1,
     "test_failure_push_key_alert_and_non_disclosure"),
    ("wrong-push-key", f"case_ebbb469a/{PUSH}",
     'PUSH_KEY = "platform_runner-census"', 'PUSH_KEY = "platform_runner_census"', 1,
     "test_failure_push_key_alert_and_non_disclosure"),
    ("failed-gatus-push-exits-zero", f"case_ebbb469a/{PUSH}",
     "    if rc != 0:\n        return 1\n", "    if False:\n        return 1\n", 1,
     "test_a_refused_gatus_push_is_a_failed_run"),
    ("fragment-loses-owner", "case_ebbb469a/gatus-endpoints.yaml", "OWNER devops.", "OWNR.", 2,
     "test_fragment_carries_threshold_owner_and_runbook_per_alert"),
    ("fragment-loses-runbook", "case_ebbb469a/gatus-endpoints.yaml",
     "RUNBOOK ops/case_ebbb469a/OPERATOR.md#runner-census", "RUNBOOK (none)", 2,
     "test_fragment_carries_threshold_owner_and_runbook_per_alert"),
    ("runbook-anchor-removed", "case_ebbb469a/OPERATOR.md", "\n### runner-census\n", "\n### census\n", 1,
     "test_runbook_anchor_and_cron_line_exist"),
    ("registry-entry-removed", "case_658bf6b9/MODULES.md", "### runner-census (case_ebbb469a", "### removed (case_ebbb469a", 1,
     "test_registry_lists_the_module"),
]


def build_tree(dest):
    shutil.copytree(OPS / "case_ebbb469a", dest / "case_ebbb469a",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "case_658bf6b9").mkdir()
    for name in ("_gatus_push.sh", "MODULES.md"):
        shutil.copy2(OPS / "case_658bf6b9" / name, dest / "case_658bf6b9" / name)


def run_suite(ops_dir):
    env = dict(os.environ, RUNNER_CENSUS_OPS_DIR=str(ops_dir), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(SUITE)], env=env, capture_output=True,
                          text=True, timeout=TIMEOUT_S)
    failed = set(re.findall(r"^(?:FAIL|ERROR): (\w+)", proc.stderr, re.M))
    return proc.returncode, failed, proc.stderr


def main():
    with tempfile.TemporaryDirectory(prefix="census-mutants.") as tmp:
        base = Path(tmp) / "base"
        base.mkdir()
        build_tree(base)
        rc, failed, err = run_suite(base)
        if rc != 0:
            print("BASELINE FAILED: the unmutated copy must pass\n" + err[-2000:])
            return 1
        print(f"baseline ok; {len(MUTANTS)} mutants")
        bad = 0
        for index, (name, rel, anchor, replacement, count, expected) in enumerate(MUTANTS):
            work = Path(tmp) / f"m{index}"
            work.mkdir()
            build_tree(work)
            target = work / rel
            text = target.read_text()
            if text.count(anchor) != count:
                print(f"  VACUOUS  {name}: anchor found {text.count(anchor)}x, expected {count}x in {rel}")
                bad += 1
                continue
            if anchor == replacement:
                print(f"  VACUOUS  {name}: replacement equals the anchor")
                bad += 1
                continue
            target.write_text(text.replace(anchor, replacement))
            rc, failed, err = run_suite(work)
            if rc == 0:
                print(f"  SURVIVED {name}: suite stayed green")
                bad += 1
            elif BROKE_MODULE.search(err):
                print(f"  WRONG    {name}: the mutant does not load; it was not killed by a test")
                bad += 1
            elif expected not in failed:
                print(f"  WRONG    {name}: red, but {expected} did not fail (failed: {sorted(failed)})")
                bad += 1
            else:
                print(f"  killed   {name}  <- {expected} ({len(failed)} failing)")
        print(("FAIL" if bad else "OK") + f": {len(MUTANTS) - bad}/{len(MUTANTS)} mutants killed by their named test")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
