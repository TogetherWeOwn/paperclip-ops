#!/usr/bin/env python3
"""test_ax_org_template.py — structural contract for the staged AX drop.

Hermetic: stdlib + PyYAML, no network, no Docker. Run from the repo root:

  python3 -m unittest discover -s templates/org-github/tests \\
      -p 'test_ax_org_template.py'

It pins the properties that make the AX drop safe to apply: every
staged file exists and is named in the destination map, the PR-template
block carries the Consumer line plus AX1-AX8, the repo-template AGENTS.md
fits the map budget it preaches, the ax-lint workflow is advisory
(warn default, pinned actions, no secrets) with a tested matcher, the
ax-metrics job is weekly with cost/card data kept out of the summary,
the review dimension states the blocking/nit split, and every staged
file is public-safe.
"""

import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(BASE, "ax-lint"))
sys.path.insert(0, os.path.join(BASE, "ax-metrics"))

import ax_lint  # noqa: E402
import ax_metrics  # noqa: E402

CHECKOUT_PIN = ("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
                " # v4.2.2")

STAGED_FILES = [
    "pull-request-template/AX-BLOCK.md",
    "repo-template/AGENTS.md",
    "ax-lint/ax-lint.yml",
    "ax-lint/ax_lint.py",
    "ax-metrics/ax-metrics.yml",
    "ax-metrics/ax_metrics.py",
    "review/AX-DIMENSION.md",
]


def read(relpath):
    with open(os.path.join(BASE, relpath), encoding="utf-8") as fh:
        return fh.read()


def triggers(workflow):
    on = workflow.get("on", workflow.get(True, {}))
    return on or {}


class DestinationMapTest(unittest.TestCase):
    def test_staged_files_exist(self):
        for relpath in STAGED_FILES:
            self.assertTrue(os.path.isfile(os.path.join(BASE, relpath)),
                            f"staged file missing: {relpath}")

    def test_destination_map_names_every_staged_file(self):
        readme = read("README.md")
        for relpath in STAGED_FILES:
            self.assertIn(relpath, readme,
                          f"destination map omits {relpath}")


class AxBlockTest(unittest.TestCase):
    def test_consumer_and_ax_lines(self):
        block = read("pull-request-template/AX-BLOCK.md")
        self.assertIn("Consumer: <agent | human | both>", block)
        for num in range(1, 9):
            self.assertIn(f"- AX{num}:", block)
        self.assertIn("N/A", block)

    def test_destination_and_merge_note(self):
        block = read("pull-request-template/AX-BLOCK.md")
        self.assertIn(".github/pull_request_template.md", block)
        self.assertIn("Operator merge note", block)
        self.assertIn("pr-lint", block)

    def test_no_internal_ids(self):
        block = read("pull-request-template/AX-BLOCK.md")
        self.assertIsNone(re.search(r"\bTOG-\d+", block))


class RepoTemplateAgentsTest(unittest.TestCase):
    def test_ax_rules_section(self):
        agents = read("repo-template/AGENTS.md")
        self.assertIn("## Agent Experience (AX) rules", agents)
        section = agents.split("## Agent Experience (AX) rules")[1]
        section = section.split("## ")[0]
        rules = [ln for ln in section.splitlines()
                 if re.match(r"^\d+\.", ln)]
        self.assertGreaterEqual(len(rules), 5)
        self.assertLessEqual(len(rules), 12)
        self.assertIn("agent | human | both", section)
        self.assertIn("AX1-AX8", section)

    def test_template_fits_map_budget(self):
        agents = read("repo-template/AGENTS.md")
        self.assertLessEqual(len(agents.splitlines()), 120)

    def test_placeholders_documented(self):
        agents = read("repo-template/AGENTS.md")
        self.assertIn("<REPO>", agents)
        self.assertIn("<FAST COMMAND>", agents)
        self.assertIn("<DEPLOY DOC>", agents)
        self.assertIn("<REPO SKILLS>", agents)
        self.assertIn("Operator seeding note", agents)

    def test_no_dangling_map_references(self):
        agents = read("repo-template/AGENTS.md")
        top_map = agents.split("## Agent Experience (AX) rules")[0]
        self.assertNotIn("below", top_map)
        self.assertNotIn("docs/deploy.md", top_map)


class AxLintWorkflowTest(unittest.TestCase):
    def test_advisory_reusable_shape(self):
        workflow = yaml.safe_load(read("ax-lint/ax-lint.yml"))
        options = triggers(workflow).get("workflow_call", {})
        self.assertEqual(options["inputs"]["mode"]["default"], "warn")
        self.assertEqual(options["inputs"]["root"]["default"], ".")
        jobs = workflow["jobs"]
        self.assertEqual(list(jobs), ["ax-lint"])
        job = jobs["ax-lint"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertIn("timeout-minutes", job)
        self.assertEqual(job["permissions"], {"contents": "read"})

    def test_pinned_actions_no_secrets(self):
        text = read("ax-lint/ax-lint.yml")
        self.assertEqual(text.count(CHECKOUT_PIN), 2)
        self.assertNotIn("secrets.", text)
        self.assertIn("persist-credentials: false", text)

    def test_called_workflow_identity(self):
        # The pin guard must use the job.* contexts (called workflow),
        # never github.workflow_ref (the caller). See CodeRabbit round 1.
        text = read("ax-lint/ax-lint.yml")
        self.assertIn("job.workflow_ref", text)
        self.assertIn("job.workflow_sha", text)
        self.assertIn("job.workflow_repository", text)
        self.assertNotIn("github.workflow_ref", text)
        self.assertIn("TogetherWeOwn/.github", text)
        workflow = yaml.safe_load(text)
        for trigger, config in triggers(workflow).items():
            if isinstance(config, dict):
                self.assertNotIn("paths", config)
                self.assertNotIn("paths-ignore", config)

    def test_source_ref_guard_accepts_sha_rejects_tag_and_repo(self):
        # Execute the extracted guard under bash: neutering any exit 1
        # must turn a reject into an accept and fail this test.
        import subprocess
        text = read("ax-lint/ax-lint.yml")
        workflow = yaml.safe_load(text)
        steps = workflow["jobs"]["ax-lint"]["steps"]
        run = next(s["run"] for s in steps
                   if s.get("name") == "Resolve ax-lint source ref")
        sha = "a" * 40
        good_ref = (f"TogetherWeOwn/.github/.github/workflows/"
                    f"ax-lint.yml@{sha}")
        cases = [
            ({"WORKFLOW_REPO": "TogetherWeOwn/.github",
              "WORKFLOW_SHA": sha,
              "WORKFLOW_REF": good_ref}, 0),
            ({"WORKFLOW_REPO": "TogetherWeOwn/.github",
              "WORKFLOW_SHA": sha,
              "WORKFLOW_REF": "TogetherWeOwn/.github/.github/workflows/"
                              "ax-lint.yml@v1"}, 1),
            ({"WORKFLOW_REPO": "EvilOrg/.github",
              "WORKFLOW_SHA": sha,
              "WORKFLOW_REF": good_ref}, 1),
            ({"WORKFLOW_REPO": "TogetherWeOwn/.github",
              "WORKFLOW_SHA": "v1",
              "WORKFLOW_REF": "TogetherWeOwn/.github/.github/workflows/"
                              "ax-lint.yml@v1"}, 1),
        ]
        for env_extra, expected in cases:
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".github-output",
                    delete=False) as out:
                out_path = out.name
            try:
                env = dict(os.environ, GITHUB_OUTPUT=out_path,
                           **env_extra)
                proc = subprocess.run(
                    ["bash", "-c", run], env=env,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(
                    proc.returncode, expected,
                    f"env {env_extra}: stdout={proc.stdout!r} "
                    f"stderr={proc.stderr!r}")
                if expected == 0:
                    with open(out_path, encoding="utf-8") as fh:
                        self.assertIn(f"sha={sha}", fh.read())
            finally:
                os.unlink(out_path)


class AxLintMatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relpath, text, executable=False):
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if executable:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def test_missing_agents_map_warns(self):
        findings = ax_lint.check_agents_map(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("no AGENTS.md", findings[0]["message"])

    def test_oversize_agents_map_warns(self):
        self.write("AGENTS.md", "\n".join(f"line {n}" for n in range(150)))
        findings = ax_lint.check_agents_map(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("150 lines", findings[0]["message"])

    def test_short_agents_map_passes(self):
        self.write("AGENTS.md", "# map\n")
        self.assertEqual(ax_lint.check_agents_map(self.root), [])

    def test_flag_cli_without_json_or_dry_run_warns(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X --force\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "script-surface")

    def test_cli_with_json_passes(self):
        self.write("status.sh",
                   "#!/usr/bin/env bash\n# usage: --json [--watch]\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_cli_with_dry_run_passes(self):
        self.write("promote.sh",
                   "#!/usr/bin/env bash\n# usage: --mode X --dry-run\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_flagless_script_skipped(self):
        self.write("setup.sh", "#!/usr/bin/env bash\necho hi\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_test_scripts_skipped(self):
        self.write("test_deploy.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        self.write("tests/check.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_ax_source_checkout_excluded(self):
        # The reusable workflow checks the lint source into .ax-source/
        # inside the scanned root; its flag-taking scripts must not warn.
        self.write("AGENTS.md", "# map\n")
        self.write(".ax-source/review-rules/tests/selftest.py",
                   "#!/usr/bin/env python3\n# --target X\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])
        # ... while a caller-owned flag CLI still warns.
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual([f["path"] for f in findings], ["deploy.sh"])

    def test_untyped_dispatch_input_warns(self):
        self.write(".github/workflows/ship.yml",
                   "name: ship\non:\n  workflow_dispatch:\n"
                   "    inputs:\n      target:\n"
                   "        description: where to ship\n"
                   "        required: true\n")
        findings = ax_lint.check_dispatch_inputs(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("has no type", findings[0]["message"])

    def test_choice_without_options_warns(self):
        self.write(".github/workflows/ship.yml",
                   "name: ship\non:\n  workflow_dispatch:\n"
                   "    inputs:\n      target:\n"
                   "        description: where\n"
                   "        type: choice\n")
        findings = ax_lint.check_dispatch_inputs(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("lists no options", findings[0]["message"])

    def test_typed_dispatch_passes(self):
        self.write(".github/workflows/ship.yml",
                   "name: ship\non:\n  workflow_dispatch:\n"
                   "    inputs:\n      target:\n"
                   "        description: where\n"
                   "        type: choice\n"
                   "        options: [a, b]\n"
                   "      dry:\n"
                   "        type: boolean\n")
        self.assertEqual(ax_lint.check_dispatch_inputs(self.root), [])

    def test_inline_dispatch_skipped(self):
        self.write(".github/workflows/ci.yml",
                   "name: ci\non:\n  workflow_dispatch: {}\n")
        self.assertEqual(ax_lint.check_dispatch_inputs(self.root), [])

    def test_warn_mode_exits_zero_with_findings(self):
        self.write("AGENTS.md", "# map\n")
        self.write("run.sh", "#!/usr/bin/env bash\n# --force\n",
                   executable=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ax_lint.main(["--root", self.root, "--mode", "warn"])
        self.assertEqual(code, 0)
        self.assertIn("1 finding(s)", buf.getvalue())

    def test_error_mode_exits_one_with_findings(self):
        self.write("run.sh", "#!/usr/bin/env bash\n# --force\n",
                   executable=True)
        with redirect_stdout(io.StringIO()):
            code = ax_lint.main(["--root", self.root, "--mode", "error"])
        self.assertEqual(code, 1)

    def test_json_shape(self):
        self.write("AGENTS.md", "# map\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ax_lint.main(["--root", self.root, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["tool"], "ax-lint")
        self.assertIn("findings", report)
        self.assertIn("summary", report)


class AxMetricsWorkflowTest(unittest.TestCase):
    def test_weekly_schedule_and_dispatch(self):
        workflow = yaml.safe_load(read("ax-metrics/ax-metrics.yml"))
        on = triggers(workflow)
        self.assertIn("schedule", on)
        crons = [entry["cron"] for entry in on["schedule"]]
        self.assertTrue(any(re.match(r"^\S+ \S+ \* \* \d$", c) for c in crons),
                        "expected a weekly cron")
        self.assertIn("workflow_dispatch", on)
        job = workflow["jobs"]["ax-metrics"]
        self.assertIn("timeout-minutes", job)
        self.assertEqual(job["permissions"], {"contents": "read"})

    def test_pinned_checkout_and_private_destination(self):
        text = read("ax-metrics/ax-metrics.yml")
        self.assertIn(CHECKOUT_PIN, text)
        self.assertIn("PRIVATE", text)
        self.assertIn("see log (internal)", text)
        self.assertNotIn("secrets.GITHUB_TOKEN}", text.replace(
            "secrets.METRICS_GITHUB_TOKEN}", ""))


class AxMetricsComputeTest(unittest.TestCase):
    def test_first_push_green_rate(self):
        self.assertAlmostEqual(
            ax_metrics.compute_first_push_green_rate(
                ["success", "success", "failure", None, "pending"]),
            2 / 3)
        self.assertIsNone(ax_metrics.compute_first_push_green_rate([]))
        self.assertIsNone(
            ax_metrics.compute_first_push_green_rate([None, "pending"]))

    def test_escalations_per_100(self):
        issues = [
            {"status": "blocked", "updatedAt": "2026-10-20T00:00:00Z"},
            {"status": "done", "updatedAt": "2026-10-20T00:00:00Z"},
            {"status": "blocked", "updatedAt": "2026-01-01T00:00:00Z"},
        ]
        self.assertAlmostEqual(
            ax_metrics.compute_escalations_per_100(
                issues, "2026-10-13T00:00:00Z"), 50.0)
        self.assertIsNone(
            ax_metrics.compute_escalations_per_100([], "2026-10-13T00:00:00Z"))

    def test_tokens_per_pr(self):
        self.assertAlmostEqual(
            ax_metrics.compute_tokens_per_pr(1000, 4), 250.0)
        self.assertIsNone(ax_metrics.compute_tokens_per_pr(None, 4))
        self.assertIsNone(ax_metrics.compute_tokens_per_pr(1000, 0))

    def test_fetch_merged_prs_reports_total_count(self):
        payload = {
            "total_count": 45,
            "items": [{"number": n, "closed_at": "2026-10-20T00:00:00Z"}
                      for n in range(1, 46)],
        }
        real_get = ax_metrics._http_get_json
        ax_metrics._http_get_json = lambda url, headers: payload
        try:
            sample, truncated, total = ax_metrics.fetch_merged_prs(
                "Example/a", "2026-10-13T00:00:00Z", "token")
        finally:
            ax_metrics._http_get_json = real_get
        self.assertEqual(total, 45)
        self.assertEqual(len(sample), ax_metrics.MAX_PRS_PER_REPO)
        self.assertTrue(truncated)

    def test_fetch_merged_prs_small_window_not_truncated(self):
        payload = {
            "total_count": 2,
            "items": [{"number": 7, "closed_at": "2026-10-20T00:00:00Z"},
                      {"number": 8, "closed_at": None}],
        }
        real_get = ax_metrics._http_get_json
        ax_metrics._http_get_json = lambda url, headers: payload
        try:
            sample, truncated, total = ax_metrics.fetch_merged_prs(
                "Example/a", "2026-10-13T00:00:00Z", "token")
        finally:
            ax_metrics._http_get_json = real_get
        self.assertEqual(total, 2)
        self.assertEqual([pr["number"] for pr in sample], [7, 8])
        self.assertFalse(truncated)

    def test_combine_commit_state(self):
        combine = ax_metrics.combine_commit_state
        # Legacy-only evidence still works.
        self.assertEqual(
            combine({"state": "success", "total_count": 1}, []), "success")
        self.assertEqual(
            combine({"state": "failure", "total_count": 2}, []), "failure")
        # Actions-only commit: empty pending legacy plus check runs.
        self.assertEqual(
            combine({"state": "pending", "total_count": 0},
                    [{"conclusion": "success", "status": "completed"}]),
            "success")
        self.assertEqual(
            combine({"state": "success", "total_count": 1},
                    [{"conclusion": "failure", "status": "completed"}]),
            "failure")
        # Every non-success conclusion fails, including the quiet ones.
        for conclusion in ("timed_out", "cancelled", "stale",
                           "startup_failure", "action_required"):
            self.assertEqual(
                combine({"state": "success", "total_count": 1},
                        [{"conclusion": conclusion,
                          "status": "completed"}]),
                "failure", f"conclusion {conclusion} should fail")
        # Neutral/skipped carry no signal; in-progress votes pending.
        self.assertEqual(
            combine({"state": "pending", "total_count": 0},
                    [{"conclusion": None, "status": "in_progress"},
                     {"conclusion": "skipped", "status": "completed"}]),
            "pending")
        # No evidence anywhere returns None.
        self.assertIsNone(
            combine({"state": "pending", "total_count": 0}, []))
        self.assertIsNone(combine(None, None))

    def test_fetch_first_commit_state_merges_check_runs(self):
        # The wrapper must fetch /status plus /check-runs and merge them:
        # dropping check_runs from the call must fail this test.
        seen_urls = []

        def fake_get(url, headers):
            seen_urls.append(url)
            if "/pulls/7/commits" in url:
                return [{"sha": "abc123"}]
            if url.endswith("/commits/abc123/status"):
                return {"state": "pending", "total_count": 0}
            if url.endswith("/commits/abc123/check-runs?per_page=100"):
                return {"check_runs": [
                    {"conclusion": "success", "status": "completed"}]}
            raise AssertionError(f"unexpected url {url}")

        real_get = ax_metrics._http_get_json
        ax_metrics._http_get_json = fake_get
        try:
            state = ax_metrics.fetch_first_commit_state(
                "Example/a", 7, "token")
        finally:
            ax_metrics._http_get_json = real_get
        self.assertEqual(state, "success")
        self.assertTrue(
            any("/pulls/7/commits?per_page=1" in u for u in seen_urls),
            f"first-commit selection missing per_page=1: {seen_urls}")

    def test_fetch_first_commit_state_failing_check_run_fails(self):
        def fake_get(url, headers):
            if "/pulls/7/commits" in url:
                return [{"sha": "abc123"}]
            if url.endswith("/commits/abc123/status"):
                return {"state": "success", "total_count": 1}
            if url.endswith("/commits/abc123/check-runs?per_page=100"):
                return {"check_runs": [
                    {"conclusion": "failure", "status": "completed"}]}
            raise AssertionError(f"unexpected url {url}")

        real_get = ax_metrics._http_get_json
        ax_metrics._http_get_json = fake_get
        try:
            state = ax_metrics.fetch_first_commit_state(
                "Example/a", 7, "token")
        finally:
            ax_metrics._http_get_json = real_get
        self.assertEqual(state, "failure")

    def test_from_file_report_shape(self):
        # Schema v2: a bare cost total no longer yields a ratio; the
        # fixture carries explicit window evidence (see
        # test_ax_paperclip_window.py for the withhold cases).
        fixture = {
            "github_rows": [{"repo": "Example/a", "merged_prs": 2,
                             "truncated": False,
                             "first_commit_states": ["success", "failure"]}],
            "cost_tokens": 1000,
            "cost_evidence": {
                "tokens": 1000,
                "window_start": "2026-10-13T00:00:00Z",
                "window_end": "2026-10-20T00:00:00Z",
            },
            "issues": [{"status": "done",
                        "updatedAt": "2026-10-20T00:00:00Z"}],
            "window_start": "2026-10-13T00:00:00Z",
        }
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ax_metrics.main(["--from-file", path, "--days", "7"])
            self.assertEqual(code, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["tool"], "ax-metrics")
            self.assertEqual(report["merged_prs_total"], 2)
            metrics = report["metrics"]
            self.assertAlmostEqual(
                metrics["first_push_green_rate"]["value"], 0.5)
            self.assertAlmostEqual(
                metrics["tokens_per_merged_pr"]["value"], 500.0)
            self.assertAlmostEqual(
                metrics["escalations_per_100_cards"]["value"], 0.0)
            for metric in metrics.values():
                self.assertIn("definition", metric)
                self.assertIn("provenance", metric)
        finally:
            os.unlink(path)

    def test_degraded_paperclip_metrics_carry_reason(self):
        fixture = {
            "github_rows": [{"repo": "Example/a", "merged_prs": 1,
                             "truncated": False,
                             "first_commit_states": ["success"]}],
            "cost_tokens": None,
            "issues": None,
            "window_start": "2026-10-13T00:00:00Z",
            "paperclip_reason": "paperclip credentials not configured",
        }
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ax_metrics.main(["--from-file", path, "--days", "7"])
            self.assertEqual(code, 0)
            report = json.loads(buf.getvalue())
            self.assertIsNone(
                report["metrics"]["tokens_per_merged_pr"]["value"])
            self.assertIn("not configured", report["metrics"]
                          ["tokens_per_merged_pr"]["provenance"])
        finally:
            os.unlink(path)

    def test_no_repos_exits_two(self):
        with redirect_stdout(io.StringIO()):
            code = ax_metrics.main(["--days", "7"])
        self.assertEqual(code, 2)

    def test_failed_issue_scan_degrades_to_none(self):
        issues, truncated = ax_metrics.resolve_issue_scan(
            [{"status": "done"}], False, "HTTP Error 500")
        self.assertIsNone(issues)
        self.assertFalse(truncated)

    def test_truncated_issue_scan_rides_along(self):
        fetched = [{"status": "done"}]
        issues, truncated = ax_metrics.resolve_issue_scan(
            fetched, True, None)
        self.assertEqual(issues, fetched)
        self.assertTrue(truncated)

    def test_fetch_paperclip_issues_paginates_until_short_page(self):
        import urllib.parse as urlparse

        def fake_get(base_url, api_key, path):
            page = int(urlparse.parse_qs(
                urlparse.urlparse(path).query)["page"][0])
            if page == 1:
                return [{"id": n} for n in range(100)], None
            if page == 2:
                return {"issues": [{"id": n} for n in range(100, 200)]}, None
            if page == 3:
                return [{"id": 200}, {"id": 201}], None
            raise AssertionError(f"unexpected page {page}")

        real_get = ax_metrics.paperclip_get
        ax_metrics.paperclip_get = fake_get
        try:
            issues, truncated, reason = ax_metrics.fetch_paperclip_issues(
                "https://paperclip.example", "key", "company")
        finally:
            ax_metrics.paperclip_get = real_get
        self.assertEqual(len(issues), 202)
        self.assertFalse(truncated)
        self.assertIsNone(reason)

    def test_fetch_paperclip_issues_truncated_at_page_cap(self):
        calls = []

        def fake_get(base_url, api_key, path):
            calls.append(path)
            return [{"id": n} for n in range(100)], None

        real_get = ax_metrics.paperclip_get
        ax_metrics.paperclip_get = fake_get
        try:
            issues, truncated, reason = ax_metrics.fetch_paperclip_issues(
                "https://paperclip.example", "key", "company")
        finally:
            ax_metrics.paperclip_get = real_get
        self.assertEqual(len(issues),
                         ax_metrics.MAX_ISSUE_PAGES * ax_metrics.ISSUES_PER_PAGE)
        self.assertEqual(len(calls), ax_metrics.MAX_ISSUE_PAGES)
        self.assertTrue(truncated)
        self.assertIsNone(reason)

    def test_fetch_paperclip_issues_mid_scan_failure(self):
        import urllib.parse as urlparse

        def fake_get(base_url, api_key, path):
            page = int(urlparse.parse_qs(
                urlparse.urlparse(path).query)["page"][0])
            if page == 1:
                return [{"id": n} for n in range(100)], None
            return None, "HTTPError: HTTP Error 500"

        real_get = ax_metrics.paperclip_get
        ax_metrics.paperclip_get = fake_get
        try:
            issues, truncated, reason = ax_metrics.fetch_paperclip_issues(
                "https://paperclip.example", "key", "company")
        finally:
            ax_metrics.paperclip_get = real_get
        self.assertEqual(len(issues), 100)
        self.assertTrue(truncated)
        self.assertIn("500", reason)

    def test_issue_truncation_reaches_report(self):
        report = ax_metrics.build_report(
            [], None, None, "2026-10-13T00:00:00Z", 7,
            paperclip_reason="scan failed", issues_truncated=True)
        self.assertTrue(report["issues_truncated"])
        self.assertIsNone(
            report["metrics"]["escalations_per_100_cards"]["value"])
        self.assertIn("scan failed", report["metrics"]
                      ["escalations_per_100_cards"]["provenance"])

    def test_parse_repos_skips_indented_comments(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False) as fh:
            fh.write("# top comment\n\n  # indented comment\nExample/a\n")
            path = fh.name
        try:
            self.assertEqual(
                ax_metrics.parse_repos("Example/b, ", path),
                ["Example/b", "Example/a"])
        finally:
            os.unlink(path)

    def test_paperclip_get_round_trip_and_failure(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen_auth = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_auth["value"] = self.headers.get("Authorization")
                if self.path == "/ok":
                    body = json.dumps({"hello": "world"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            data, reason = ax_metrics.paperclip_get(base, "secret-key", "/ok")
            self.assertEqual(data, {"hello": "world"})
            self.assertIsNone(reason)
            self.assertEqual(seen_auth.get("value"), "Bearer secret-key")
            data, reason = ax_metrics.paperclip_get(base, "secret-key", "/boom")
            self.assertIsNone(data)
            self.assertIn("HTTP Error 500", reason)
        finally:
            server.shutdown()
            thread.join()


def _reject_json_constant(value):
    raise AssertionError(f"non-standard JSON constant in report: {value}")


class AxMetricsInputValidationTest(unittest.TestCase):
    """External cost payloads are validated, never trusted (PR #60)."""

    def _run_fixture(self, fixture):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ax_metrics.main(
                    ["--from-file", path, "--days", "7"])
            return code, buf.getvalue()
        finally:
            os.unlink(path)

    def _base_fixture(self, **overrides):
        fixture = {
            "github_rows": [{"repo": "Example/a", "merged_prs": 2,
                             "truncated": False,
                             "first_commit_states": ["success", "failure"]}],
            "cost_tokens": 1000,
            "cost_evidence": {
                "tokens": 1000,
                "window_start": "2026-10-13T00:00:00Z",
                "window_end": "2026-10-20T00:00:00Z",
            },
            "issues": [{"status": "done",
                        "updatedAt": "2026-10-20T00:00:00Z"}],
            "window_start": "2026-10-13T00:00:00Z",
        }
        fixture.update(overrides)
        return fixture

    def test_evidence_tokens_rejects_bool_and_nonfinite(self):
        self.assertIsNone(ax_metrics._evidence_tokens({"tokens": True}))
        self.assertIsNone(ax_metrics._evidence_tokens({"tokens": False}))
        self.assertIsNone(
            ax_metrics._evidence_tokens({"tokens": float("nan")}))
        self.assertIsNone(
            ax_metrics._evidence_tokens({"tokens": float("inf")}))
        self.assertIsNone(ax_metrics._evidence_tokens(
            {"summary": {"totalTokens": float("inf")}}))
        self.assertIsNone(
            ax_metrics._evidence_tokens({"tokens": 10 ** 1000}))
        self.assertEqual(ax_metrics._evidence_tokens({"tokens": 0}), 0)
        self.assertEqual(
            ax_metrics._evidence_tokens({"tokens": 1000}), 1000)

    def test_window_days_between_rejects_mixed_timezones(self):
        self.assertIsNone(ax_metrics._window_days_between(
            "2026-10-01", "2026-10-08T00:00:00+00:00"))
        self.assertEqual(ax_metrics._window_days_between(
            "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z"), 7)

    def test_boolean_token_total_withheld(self):
        fixture = self._base_fixture(cost_evidence={
            "tokens": True,
            "window_start": "2026-10-13T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        })
        fixture["cost_tokens"] = None
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["tokens_per_merged_pr"]
        self.assertIsNone(metric["value"])
        self.assertNotEqual(metric["validity"], "valid")
        self.assertIsNone(metric["numerator"])

    def test_negative_token_totals_rejected(self):
        for bad in (-100, -0.5, -5e-324, -(10 ** 1000)):
            with self.subTest(bad=bad):
                self.assertIsNone(ax_metrics._as_token_total(bad))

    def test_negative_token_total_withheld(self):
        for bad in (-100, -0.5):
            for payload in ({"tokens": bad}, {"totalTokens": bad},
                            {"summary": {"totalTokens": bad}}):
                with self.subTest(payload=payload):
                    fixture = self._base_fixture(cost_evidence={
                        **payload,
                        "window_start": "2026-10-13T00:00:00Z",
                        "window_end": "2026-10-20T00:00:00Z",
                    })
                    code, text = self._run_fixture(fixture)
                    self.assertEqual(code, 0)
                    metrics = json.loads(text)["metrics"]
                    metric = metrics["tokens_per_merged_pr"]
                    self.assertIsNone(metric["value"])
                    self.assertEqual(metric["validity"], "unavailable")
                    self.assertEqual(metric["reason_code"], "no_evidence")
                    self.assertIsNone(metric["numerator"])
                    self.assertEqual(
                        metrics["first_push_green_rate"]["validity"], "valid")
                    self.assertEqual(
                        metrics["escalations_per_100_cards"]["validity"],
                        "valid")

    def test_zero_token_total_remains_valid(self):
        for total in (0, 0.0, -0.0):
            with self.subTest(total=total):
                fixture = self._base_fixture(cost_evidence={
                    "tokens": total,
                    "window_start": "2026-10-13T00:00:00Z",
                    "window_end": "2026-10-20T00:00:00Z",
                })
                code, text = self._run_fixture(fixture)
                self.assertEqual(code, 0)
                metric = json.loads(text)["metrics"]["tokens_per_merged_pr"]
                self.assertEqual(metric["value"], 0)
                self.assertEqual(metric["validity"], "valid")
                self.assertEqual(metric["reason_code"], "ok")
                self.assertEqual(metric["numerator"], 0)

    def test_nonfinite_token_total_withheld(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                fixture = self._base_fixture(cost_evidence={
                    "tokens": bad,
                    "window_start": "2026-10-13T00:00:00Z",
                    "window_end": "2026-10-20T00:00:00Z",
                })
                fixture["cost_tokens"] = None
                code, text = self._run_fixture(fixture)
                self.assertEqual(code, 0)
                metric = json.loads(text)["metrics"][
                    "tokens_per_merged_pr"]
                self.assertIsNone(metric["value"])
                self.assertNotEqual(metric["validity"], "valid")

    def test_oversized_token_total_withheld(self):
        fixture = self._base_fixture(cost_evidence={
            "tokens": 10 ** 1000,
            "window_start": "2026-10-13T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        })
        fixture["cost_tokens"] = None
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"][
            "tokens_per_merged_pr"]
        self.assertIsNone(metric["value"])
        self.assertNotEqual(metric["validity"], "valid")

    def test_legacy_boolean_and_nonfinite_cost_tokens_ignored(self):
        for bad in (True, False, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                fixture = self._base_fixture(cost_tokens=bad)
                fixture["cost_evidence"] = {
                    "window_start": "2026-10-13T00:00:00Z",
                    "window_end": "2026-10-20T00:00:00Z",
                }
                code, text = self._run_fixture(fixture)
                self.assertEqual(code, 0)
                metric = json.loads(text)["metrics"][
                    "tokens_per_merged_pr"]
                self.assertIsNone(metric["value"])
                self.assertNotEqual(metric["validity"], "valid")
                self.assertIsNone(metric["numerator"])

    def test_mixed_timezone_mismatch_withholds_without_crash(self):
        fixture = self._base_fixture(cost_evidence={
            "tokens": 1000,
            "window_start": "2026-10-01",
            "window_end": "2026-10-08T00:00:00+00:00",
        })
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["tokens_per_merged_pr"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["validity"], "withheld")
        self.assertEqual(metric["reason_code"], "window_mismatch")
        self.assertIsNone(metric["source_window"]["days"])

    def test_report_output_is_strict_json(self):
        fixture = self._base_fixture(cost_evidence={
            "tokens": float("nan"),
            "window_start": "2026-10-13T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        })
        fixture["cost_tokens"] = float("inf")
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        report = json.loads(text, parse_constant=_reject_json_constant)
        self.assertIsNone(
            report["metrics"]["tokens_per_merged_pr"]["value"])

    def test_tokenless_matching_evidence_ignores_legacy_total(self):
        fixture = self._base_fixture(cost_evidence={
            "window_start": "2026-10-13T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        })
        fixture["cost_tokens"] = 1000
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["tokens_per_merged_pr"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["validity"], "unavailable")
        self.assertEqual(metric["reason_code"], "no_evidence")
        self.assertIsNone(metric["numerator"])

    def test_invalid_evidence_tokens_ignore_legacy_total(self):
        fixture = self._base_fixture(cost_evidence={
            "tokens": "bogus",
            "window_start": "2026-10-13T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        })
        fixture["cost_tokens"] = 1000
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["tokens_per_merged_pr"]
        self.assertIsNone(metric["value"])
        self.assertNotEqual(metric["validity"], "valid")
        self.assertIsNone(metric["numerator"])

    def test_fixture_scan_failure_withholds_despite_issues_list(self):
        fixture = self._base_fixture(
            issues=[{"status": "blocked",
                     "updatedAt": "2026-10-19T00:00:00Z"},
                    {"status": "done",
                     "updatedAt": "2026-10-19T00:00:00Z"}],
            paperclip_reason="HTTP Error 500")
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        report = json.loads(text)
        metric = report["metrics"]["escalations_per_100_cards"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["validity"], "withheld")
        self.assertEqual(metric["reason_code"], "scan_failed")
        self.assertEqual(metric["completeness"], "partial")
        tokens = report["metrics"]["tokens_per_merged_pr"]
        self.assertEqual(tokens["validity"], "valid")

    def test_fixture_incomplete_scan_withholds_despite_issues_list(self):
        fixture = self._base_fixture(
            issues=[{"status": "blocked",
                     "updatedAt": "2026-10-19T00:00:00Z"},
                    {"status": "done",
                     "updatedAt": "2026-10-19T00:00:00Z"}],
            paperclip_reason="scan returned a partial page")
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        report = json.loads(text)
        metric = report["metrics"]["escalations_per_100_cards"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["validity"], "withheld")
        self.assertEqual(metric["reason_code"], "scan_incomplete")
        self.assertEqual(metric["completeness"], "partial")
        self.assertEqual(
            report["metrics"]["tokens_per_merged_pr"]["validity"], "valid")

    def _two_issues(self):
        return [{"status": "blocked",
                 "updatedAt": "2026-10-19T00:00:00Z"},
                {"status": "done",
                 "updatedAt": "2026-10-19T00:00:00Z"}]

    def test_fixture_cost_only_failure_keeps_complete_issue_scan(self):
        fixture = self._base_fixture(
            issues=self._two_issues(),
            paperclip_reason="costs api HTTP Error 500",
            issues_reason=None)
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["escalations_per_100_cards"]
        self.assertEqual(metric["validity"], "valid")
        self.assertEqual(metric["value"], 50.0)

    def test_fixture_issues_reason_withholds_escalations(self):
        fixture = self._base_fixture(
            issues=self._two_issues(),
            issues_reason="HTTP Error 500")
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["escalations_per_100_cards"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["reason_code"], "scan_failed")

    def test_issue_reason_ignores_unrelated_cost_reason(self):
        fixture = self._base_fixture(
            issues=None,
            paperclip_reason="costs api HTTP Error 500",
            issues_reason="scan returned a partial page")
        code, text = self._run_fixture(fixture)
        self.assertEqual(code, 0)
        metric = json.loads(text)["metrics"]["escalations_per_100_cards"]
        self.assertEqual(metric["reason_code"], "scan_incomplete")
        self.assertNotIn("costs api", metric["reason"])


class ReviewDimensionTest(unittest.TestCase):
    def test_blocking_and_nit_split(self):
        dimension = read("review/AX-DIMENSION.md")
        self.assertIn("AX2", dimension)
        self.assertIn("AX4", dimension)
        self.assertIn("deploy", dimension)
        self.assertIn("Blocking", dimension)
        self.assertIn("AX6", dimension)
        self.assertIn("AX7", dimension)
        self.assertIn("Non-blocking nits", dimension)
        self.assertIn("Reference only", dimension)


class StagedPublicSafetyTest(unittest.TestCase):
    # Loopback binds (127.0.0.1) in hermetic tests are standard practice,
    # not private data, so they are not denied.
    DENY = [r"\bTOG-\d+", r"localhost", r"192\.168\.",
            r"\b10\.\d+\.\d+\.\d+"]

    def test_no_private_markers_in_staged_files(self):
        for relpath in STAGED_FILES:
            text = read(relpath)
            for pattern in self.DENY:
                self.assertIsNone(
                    re.search(pattern, text),
                    f"{relpath} matches private pattern {pattern}")


if __name__ == "__main__":
    unittest.main()
