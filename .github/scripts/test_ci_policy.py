#!/usr/bin/env python3
"""Tests for ci_policy.py. Run: python3 -m unittest discover -s .github/scripts -p 'test_ci_policy.py'"""
import io
import os
import unittest
from contextlib import redirect_stdout

import ci_policy as cp

GATED_HEAVY = """
on:
  pull_request:
  push:
    branches: [main]

jobs:
  changes:
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 5
    outputs:
      core: ${{ steps.detect.outputs.core }}
    steps:
      - run: echo detect
  heavy:
    needs: [changes]
    if: needs.changes.outputs.core == 'true'
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 20
    steps:
      - run: echo heavy
  light:
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 5
    steps:
      - run: echo light
  ci-ok:
    needs: [changes, heavy, light]
    if: always()
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

UNGATED_HEAVY = """
on:
  pull_request:

jobs:
  heavy:
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 60
    steps:
      - run: echo heavy
"""

WORKFLOW_PATHS = """
on:
  pull_request:
    paths:
      - plugins/**
  push:
    branches: [main]

jobs:
  heavy:
    needs: [changes]
    if: needs.changes.outputs.core == 'true'
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 20
    steps:
      - run: echo heavy
"""

SCHEDULE_ONLY = """
on:
  schedule:
    - cron: "41 2 * * *"

jobs:
  heavy:
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 60
    steps:
      - run: echo heavy
"""

NON_REQUIRED = """
# ci-policy: non-required -- self-scoped audit, no required check.
on:
  pull_request:
    paths:
      - .github/workflows/self-audit.yml

jobs:
  audit:
    runs-on: [self-hosted, two-ephemeral]
    timeout-minutes: 15
    steps:
      - run: echo audit
"""

# --- ci-ok needs-completeness (case_b1c5ef61) ---------------------------------

CI_OK_HEAD = """
on:
  pull_request:

jobs:
"""

CI_OK_JOB = """  ci-ok:
    needs: [{needs}]
    if: always()
    runs-on: [self-hosted, two-isolated]
    timeout-minutes: 5
    steps:
      - run: echo ok
"""


def light(name, extra=""):
    return (f"  {name}:\n{extra}    runs-on: [self-hosted, two-isolated]\n"
            "    timeout-minutes: 5\n    steps:\n      - run: echo x\n")


def ci_ok_wf(jobs, needs):
    return CI_OK_HEAD + "".join(jobs) + CI_OK_JOB.format(needs=needs)


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class Policy(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def findings(self, text, **kw):
        path = write(self.tmp, "wf.yml", text)
        return cp.lint_file(path, kw.get("mode", "warning"), kw.get("heavy_after", 10))

    def levels(self, findings):
        return sorted((f.level, f.title) for f in findings)

    def test_gated_standard_passes(self):
        f, _ = self.findings(GATED_HEAVY)
        self.assertEqual([], f)

    def test_ungated_heavy_warns_in_warn_mode(self):
        f, _ = self.findings(UNGATED_HEAVY, mode="warning")
        self.assertEqual([("warning", "Change gating")], self.levels(f))

    def test_ungated_heavy_fails_in_error_mode(self):
        f, _ = self.findings(UNGATED_HEAVY, mode="error")
        self.assertEqual([("error", "Change gating")], self.levels(f))

    def test_missing_timeout_is_heavy(self):
        f, _ = self.findings(UNGATED_HEAVY.replace("    timeout-minutes: 60\n", ""))
        self.assertEqual([("warning", "Change gating")], self.levels(f))

    def test_workflow_paths_flagged(self):
        f, _ = self.findings(WORKFLOW_PATHS)
        self.assertIn(("warning", "Change gating"), self.levels(f))

    def test_failure_drain_lifecycle_guard_not_flagged(self):
        text = UNGATED_HEAVY.replace("    timeout-minutes: 60",
                                     "    timeout-minutes: 10\n    needs: [heavy]\n    if: failure()")
        f, _ = self.findings(text)
        self.assertEqual([], f)

    def test_schedule_only_is_out_of_scope(self):
        f, notes = self.findings(SCHEDULE_ONLY)
        self.assertEqual([], f)
        self.assertTrue(notes)

    def test_non_required_marker_is_out_of_scope(self):
        f, notes = self.findings(NON_REQUIRED)
        self.assertEqual([], f)
        self.assertTrue(notes)

    def test_detector_outputs_not_flagged(self):
        f, _ = self.findings(GATED_HEAVY)
        self.assertFalse(any("changes" in x.message for x in f))

    def needs_findings(self, text, **kw):
        f, notes = self.findings(text, **kw)
        return [x for x in f if x.title == "ci-ok needs"], notes

    def test_job_missing_from_ci_ok_needs_is_red_with_file_and_line(self):
        text = ci_ok_wf([light("lint"), light("tests")], "lint")
        f, _ = self.needs_findings(text)
        self.assertEqual(1, len(f))
        self.assertIn("`tests`", f[0].message)
        self.assertEqual(text.splitlines().index("  tests:") + 1, f[0].line)
        self.assertTrue(f[0].file.endswith("wf.yml"))
        self.assertIn(f"wf.yml:{f[0].line}:", f[0].message)

    def test_missing_job_level_follows_mode(self):
        text = ci_ok_wf([light("lint")], "")
        self.assertEqual("warning", self.needs_findings(text, mode="warning")[0][0].level)
        self.assertEqual("error", self.needs_findings(text, mode="error")[0][0].level)

    def test_transitively_reached_job_is_red(self):
        # ci-ok needs `tests`, `tests` needs `build`. A red `build` makes GitHub skip `tests`,
        # so ci-ok never sees the failure and a ci-ok that accepts `skipped` merges it.
        text = ci_ok_wf([light("build"), light("tests", "    needs: [build]\n")], "tests")
        f, _ = self.needs_findings(text)
        self.assertEqual(1, len(f))
        self.assertIn("`build`", f[0].message)
        self.assertEqual(text.splitlines().index("  build:") + 1, f[0].line)

    def test_transitive_chain_of_three_is_red_for_every_hidden_job(self):
        text = ci_ok_wf([light("a"), light("b", "    needs: a\n"),
                         light("c", "    needs: [b]\n")], "c")
        f, _ = self.needs_findings(text)
        self.assertEqual(["a", "b"], sorted(x.message.split("`")[1] for x in f))

    def test_hidden_job_is_green_once_it_is_directly_needed_or_exempt(self):
        direct = ci_ok_wf([light("build"), light("tests", "    needs: [build]\n")], "build, tests")
        self.assertEqual([], self.needs_findings(direct)[0])
        exempt = ci_ok_wf([light("build", "    # ci-ok: exempt covered by tests below\n"),
                           light("tests", "    needs: [build]\n")], "tests")
        self.assertEqual([], self.needs_findings(exempt)[0])

    def test_needs_edge_pointing_the_wrong_way_does_not_cover(self):
        # `tests` needs `build`, but ci-ok only needs `build`: tests is NOT in ci-ok's closure.
        text = ci_ok_wf([light("build"), light("tests", "    needs: [build]\n")], "build")
        f, _ = self.needs_findings(text)
        self.assertEqual(1, len(f))
        self.assertIn("`tests`", f[0].message)

    def test_block_list_and_multiline_flow_needs_parse(self):
        block = CI_OK_HEAD + light("a") + light("b") + (
            "  ci-ok:\n    needs:\n      - a   # first\n      - b\n    if: always()\n"
            "    runs-on: x\n    timeout-minutes: 5\n")
        self.assertEqual([], self.needs_findings(block)[0])
        flow = CI_OK_HEAD + light("a") + light("b") + (
            "  ci-ok:\n    needs: [a,\n      b]\n    if: always()\n"
            "    runs-on: x\n    timeout-minutes: 5\n")
        self.assertEqual([], self.needs_findings(flow)[0])

    def test_exempt_marker_with_reason_is_green_in_every_position(self):
        trailing = "  drain:  # ci-ok: exempt runs only on failure\n"
        above = "  # ci-ok: exempt -- canary, not a merge gate\n  canary:\n"
        inside = "  audit:\n    # ci-ok: exempt: nightly mirror of main\n"
        text = CI_OK_HEAD
        text += trailing + "    runs-on: x\n    timeout-minutes: 5\n"
        text += "\n" + above + "    runs-on: x\n    timeout-minutes: 5\n"
        text += "\n" + inside + "    runs-on: x\n    timeout-minutes: 5\n"
        text += CI_OK_JOB.format(needs="")
        self.assertEqual([], self.needs_findings(text)[0])

    def test_exempt_marker_without_reason_is_red(self):
        for marker in ("# ci-ok: exempt", "# ci-ok: exempt --", "# ci-ok: exempt   "):
            text = ci_ok_wf([f"  drain:\n    {marker}\n    runs-on: x\n    timeout-minutes: 5\n"], "")
            f, _ = self.needs_findings(text)
            self.assertEqual(1, len(f), marker)
            self.assertIn("no reason", f[0].message)

    def test_exempt_marker_above_a_job_does_not_leak_to_the_previous_job(self):
        text = ci_ok_wf([
            light("first"),
            "  # ci-ok: exempt belongs to second only\n" + light("second"),
        ], "")
        f, _ = self.needs_findings(text)
        self.assertEqual(1, len(f))
        self.assertIn("`first`", f[0].message)

    def test_exempt_marker_does_not_leak_to_the_next_job(self):
        text = ci_ok_wf([
            "  first:\n    # ci-ok: exempt only first\n    runs-on: x\n    timeout-minutes: 5\n",
            light("second"),
        ], "")
        f, _ = self.needs_findings(text)
        self.assertEqual(1, len(f))
        self.assertIn("`second`", f[0].message)

    def test_no_ci_ok_job_is_skipped_not_an_error(self):
        text = CI_OK_HEAD + light("a") + light("b")
        f, notes = self.needs_findings(text)
        self.assertEqual([], f)
        self.assertTrue(any("no `ci-ok` job" in n for n in notes), notes)

    def test_ci_ok_alone_has_nothing_to_miss(self):
        self.assertEqual([], self.needs_findings(CI_OK_HEAD + CI_OK_JOB.format(needs=""))[0])

    def test_rule_respects_the_pull_request_scope_guard(self):
        text = ci_ok_wf([light("tests")], "").replace("  pull_request:\n", "  push:\n", 1)
        f, notes = self.findings(text)
        self.assertEqual([], f)
        self.assertTrue(notes)

    def test_non_required_marker_skips_the_rule(self):
        text = "# ci-policy: non-required -- canary\n" + ci_ok_wf([light("tests")], "")
        self.assertEqual([], self.findings(text)[0])


class Main(unittest.TestCase):
    def run_main(self, e, files):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cp.main(e, ["ci_policy.py"] + files)
        return rc, buf.getvalue()

    def test_exit_code_follows_errors_only(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        good = write(tmp, "good.yml", GATED_HEAVY)
        bad = write(tmp, "bad.yml", UNGATED_HEAVY)
        self.assertEqual(0, self.run_main({"CI_POLICY_MODE": "warn"}, [good])[0])
        rc, out = self.run_main({"CI_POLICY_MODE": "warn"}, [bad])
        self.assertEqual(0, rc)
        self.assertIn("::warning title=Change gating::", out)
        rc, out = self.run_main({"CI_POLICY_MODE": "error"}, [bad])
        self.assertEqual(1, rc)
        self.assertTrue(out.startswith("::error title=Change gating::"), out)

    def test_ci_ok_finding_is_annotated_with_file_and_line(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        text = ci_ok_wf([light("tests")], "")
        bad = write(tmp, "needs.yml", text)
        line = text.splitlines().index("  tests:") + 1
        rc, out = self.run_main({"CI_POLICY_MODE": "warn"}, [bad])
        self.assertEqual(0, rc)
        self.assertIn(f"::warning file={bad.replace(':', '%3A')},line={line},title=ci-ok needs::", out)
        rc, out = self.run_main({"CI_POLICY_MODE": "error"}, [bad])
        self.assertEqual(1, rc)
        self.assertIn("::error file=", out)


if __name__ == "__main__":
    unittest.main()
