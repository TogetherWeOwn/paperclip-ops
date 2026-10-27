#!/usr/bin/env python3
"""Acceptance tests for the per-finding verdict records of ci_policy.py.

These tests are the frozen paired contract for the opt-in `--json` report.
They run the lint as a hermetic subprocess against temporary synthetic
workflows only; they never touch the shared checkout (except one read-only
parity run over the repo's own workflows), the network or `gh`.

Run: python3 -B -m unittest discover -s .github/scripts \\
       -p 'test_ci_policy_json_acceptance.py'

Frozen contract: schema `ci-policy-verdicts.v1`
-----------------------------------------------
Invocation: `ci_policy.py [--json] [workflow-file ...]`; `--json` may appear
before or after the paths. The environment contract is unchanged
(`CI_POLICY_MODE` warn|error, default warn; `HEAVY_TIMEOUT_MINUTES`). The
in-process `main(env, argv)` convention (argv includes the program name at
index 0) is kept.

With `--json` the whole of stdout is exactly one JSON object (UTF-8; no
annotations, `note:` lines, prose or tracebacks on stdout, no traceback on
stderr). Additive top-level fields are allowed; these are frozen:

- `schema`: exactly `"ci-policy-verdicts.v1"`.
- `mode`: effective mode echo, `"warn"` or `"error"` (`CI_POLICY_MODE` is
  trimmed and case-folded; anything other than `error` is `warn`).
- `outcome`: `"unmeasurable"` when any requested input could not be measured
  (or nothing was requested); else `"findings"` when there is any finding;
  else `"clean"`. Unmeasurable is never reported as clean.
- `totals`: at least `requested`, `linted`, `skipped`, `unmeasurable`,
  `findings` (ints) with `requested == linted + skipped + unmeasurable` and
  `findings == len(findings)`. `skipped` counts out-of-scope workflows (no
  pull_request trigger, or the `ci-policy: non-required` marker).
  `unmeasurable` counts requested inputs that could not be measured (the
  `unmeasurable` entries with a non-null path); the scan-level
  `no-inputs-discovered` entry is not a requested input and is not counted.
- `findings`: one record per violation, sorted by
  `(path, job or "", rule, code)`, each with EXACTLY the keys
  `rule`, `path`, `job`, `line`, `code`, `retryable`, `next_action`:
    rule `change-gating`, code `heavy-job-no-timeout` or `heavy-job-ungated`,
      next_action `gate_job_on_change_detection`;
    rule `workflow-paths`, code `workflow-level-paths`, job null,
      next_action `move_paths_filter_to_job_level`;
    rule `ci-ok-coverage`, code `ci-ok-needs-missing`,
      next_action `add_job_to_ci_ok_needs`; or code `ci-ok-exempt-no-reason`,
      next_action `add_exempt_reason`.
  `path` echoes the path argument verbatim (discovered paths as discovered,
  e.g. `.github/workflows/a.yml`). `job` is the job name (null only for the
  workflow-level finding). `line` is the 1-based line of the job header when
  `job` is set; int or null for the workflow-level finding. `retryable` is
  false. No record carries messages, job bodies, step text, env/secret
  expressions or marker reasons.
- `unmeasurable`: inputs that could not be measured, sorted by
  `(path or "", code)`, each with EXACTLY the keys `path`, `code`,
  `retryable` (false), `next_action`. Frozen code -> next_action:
    `input-missing` -> `fix_input_path`
    `input-invalid-encoding` -> `fix_input_encoding`
    `workflow-unparseable` -> `fix_workflow_syntax` (no top-level `on:` key
      can be located, so the scope of the workflow cannot be decided)
    `no-inputs-discovered` -> `provide_workflow_files` (path null; default
      discovery found no `.github/workflows/*.yml`)
  Findings from inputs that were measured survive a partial input failure.
- `coverage.limits`: list of `{code, description, paths}` (description a
  non-empty string, paths a sorted list of distinct path strings). Always
  contains exactly one `ci-ok-always-structural` entry (the documented limit:
  a directly needed job running under `always()` counts as covered) with
  `paths == []`. Contains `skipped-no-pull-request-trigger`,
  `skipped-non-required-marker` and `ci-ok-rule-skipped-no-ci-ok-job` entries
  if and only if at least one linted/skipped path falls in that class,
  listing exactly those paths. Each code appears at most once.

Exit codes with `--json`: `unmeasurable` -> 2 in both modes; otherwise the
same 0/1 as the prose run on the same inputs (1 only when there is a finding
in error mode). Repeated identical runs give identical stdout bytes, and the
report does not depend on argv order.

Usage boundaries: `--help`/`-h` print plain-text help to stdout and exit 0
(never JSON, even with `--json`); the help names `--json` and the schema id.
An unknown option is a usage error outside the schema: exit 2, empty stdout,
usage on stderr, no traceback.

Without `--json`, stdout and exit codes are byte-identical to base `16e9850`
(golden outputs below), detection and marker behaviour are unchanged, and no
mode writes any file.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ci_policy.py"
REPO = HERE.parent.parent
SCHEMA = "ci-policy-verdicts.v1"

FINDING_KEYS = {"rule", "path", "job", "line", "code", "retryable", "next_action"}
UNMEASURABLE_KEYS = {"path", "code", "retryable", "next_action"}
NEXT_ACTION = {
    "heavy-job-no-timeout": "gate_job_on_change_detection",
    "heavy-job-ungated": "gate_job_on_change_detection",
    "workflow-level-paths": "move_paths_filter_to_job_level",
    "ci-ok-needs-missing": "add_job_to_ci_ok_needs",
    "ci-ok-exempt-no-reason": "add_exempt_reason",
    "input-missing": "fix_input_path",
    "input-invalid-encoding": "fix_input_encoding",
    "workflow-unparseable": "fix_workflow_syntax",
    "no-inputs-discovered": "provide_workflow_files",
}
RULE = {
    "heavy-job-no-timeout": "change-gating",
    "heavy-job-ungated": "change-gating",
    "workflow-level-paths": "workflow-paths",
    "ci-ok-needs-missing": "ci-ok-coverage",
    "ci-ok-exempt-no-reason": "ci-ok-coverage",
}
LIMIT_ALWAYS = "ci-ok-always-structural"
LIMIT_NO_PR = "skipped-no-pull-request-trigger"
LIMIT_NON_REQUIRED = "skipped-non-required-marker"
LIMIT_NO_CI_OK = "ci-ok-rule-skipped-no-ci-ok-job"

# Sentinels that must never be echoed into the machine output.
SENTINELS = (
    "SENTINEL_BODY_9c1d", "SENTINEL_SECRET_7f3a", "SENTINEL_REASON_4b2e",
    "SENTINEL_GARBAGE_e81f", "SENTINEL_NR_REASON_51aa",
)

# --- fixtures -----------------------------------------------------------------

CLEAN = """\
on:
  pull_request:

jobs:
  changes:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs:
      core: ${{ steps.detect.outputs.core }}
    steps:
      - run: echo detect
  heavy:
    needs: [changes]
    if: needs.changes.outputs.core == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - run: echo heavy
  ci-ok:
    needs: [changes, heavy]
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

# heavy-job-ungated on `heavy` (line 5); no ci-ok job.
UNGATED = """\
on:
  pull_request:

jobs:
  heavy:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      API_TOKEN: ${{ secrets.SENTINEL_SECRET_7f3a }}
    steps:
      - run: echo SENTINEL_BODY_9c1d
"""

# heavy-job-no-timeout on `build` (line 5); no ci-ok job.
NO_TIMEOUT = """\
on:
  pull_request:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo build
"""

# workflow-level-paths only (the job is gated); no ci-ok job.
WF_PATHS = """\
on:
  pull_request:
    paths:
      - plugins/**

jobs:
  heavy:
    needs: [changes]
    if: needs.changes.outputs.core == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - run: echo heavy
"""

# ci-ok-needs-missing on `tests` (line 10).
CI_OK_GAP = """\
on:
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo lint
  tests:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo tests
  ci-ok:
    needs: [lint]
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

# Exempt with a reason: clean.
EXEMPT_REASON = """\
on:
  pull_request:

jobs:
  canary:  # ci-ok: exempt SENTINEL_REASON_4b2e canary only
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo canary
  ci-ok:
    needs: []
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

# ci-ok-exempt-no-reason on `drain` (line 5).
EXEMPT_NO_REASON = """\
on:
  pull_request:

jobs:
  drain:
    # ci-ok: exempt
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo drain
  ci-ok:
    needs: []
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

# Exempt (with reason) from ci-ok, but the job is heavy and ungated: the ci-ok
# marker must not hide the change-gating finding (heavy-job-ungated, line 6).
EXEMPT_BUT_HEAVY = """\
on:
  pull_request:

jobs:
  # ci-ok: exempt SENTINEL_REASON_4b2e nightly mirror
  mirror:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - run: echo mirror
  ci-ok:
    needs: []
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

# Every rule would fire, but the non-required marker skips the workflow.
NON_REQUIRED = """\
# ci-policy: non-required -- SENTINEL_NR_REASON_51aa self-scoped audit
on:
  pull_request:
    paths:
      - .github/workflows/audit.yml

jobs:
  audit:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - run: echo SENTINEL_BODY_9c1d
  other:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo other
  ci-ok:
    needs: [audit]
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo ok
"""

SCHEDULE_ONLY = """\
on:
  schedule:
    - cron: "41 2 * * *"

jobs:
  heavy:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - run: echo heavy
"""

# No top-level `on:` key at all: the lint cannot decide its scope.
UNPARSEABLE = "{{{ SENTINEL_GARBAGE_e81f : [ not yaml\n  ::: ]]\n"

MEASURABLE = {
    "clean.yml": CLEAN,
    "ungated.yml": UNGATED,
    "no_timeout.yml": NO_TIMEOUT,
    "wf_paths.yml": WF_PATHS,
    "ci_ok_gap.yml": CI_OK_GAP,
    "exempt_reason.yml": EXEMPT_REASON,
    "exempt_no_reason.yml": EXEMPT_NO_REASON,
    "exempt_but_heavy.yml": EXEMPT_BUT_HEAVY,
    "non_required.yml": NON_REQUIRED,
    "schedule_only.yml": SCHEDULE_ONLY,
}

# (file, job, line, code) expected for the whole MEASURABLE set.
EXPECTED_FINDINGS = [
    ("ci_ok_gap.yml", "tests", 10, "ci-ok-needs-missing"),
    ("exempt_but_heavy.yml", "mirror", 6, "heavy-job-ungated"),
    ("exempt_no_reason.yml", "drain", 5, "ci-ok-exempt-no-reason"),
    ("no_timeout.yml", "build", 5, "heavy-job-no-timeout"),
    ("ungated.yml", "heavy", 5, "heavy-job-ungated"),
    ("wf_paths.yml", None, None, "workflow-level-paths"),
]

# --- golden prose output of base 16e9850 ({d} = fixture directory) -------------
# Captured by running base ci_policy.py on MEASURABLE (sorted names) plus
# unparseable.yml, with the fixture directory substituted.
GOLDEN_FILES = sorted(MEASURABLE) + ["unparseable.yml"]
GOLDEN_PROSE = {
    'warn': (0, (
        "::warning file={d}/ci_ok_gap.yml,line=10,title=ci-ok needs::{d}/ci_ok_gap.yml:10: job `tests` is not in `ci-ok` `needs:` directly (a job reached only through another job's `needs:` is skipped, not failed, when its dependency fails), so it can go red and still merge. Add it to `ci-ok` `needs:` (and to any per-job list the gate reads), or mark the job `# ci-ok: exempt <reason>`.\n"
        '::warning title=Change gating::{d}/exempt_but_heavy.yml: heavy job `mirror` `timeout-minutes: 30` with no change-gating `if:`. Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::warning file={d}/exempt_no_reason.yml,line=5,title=ci-ok needs::{d}/exempt_no_reason.yml:5: job `drain` has `# ci-ok: exempt` with no reason. Say why it sits outside the required gate.\n'
        '::warning title=Change gating::{d}/no_timeout.yml: heavy job `build` has no `timeout-minutes` (GitHub defaults to 6h). Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::warning title=Change gating::{d}/ungated.yml: heavy job `heavy` `timeout-minutes: 60` with no change-gating `if:`. Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::warning title=Change gating::{d}/wf_paths.yml: workflow-level `on: pull_request: paths:` silences required checks on PRs outside it. Gate at job level instead (`needs:` + `if:`, aggregated by `ci-ok`), or mark the workflow `ci-policy: non-required` with a reason when it truly produces no required check.\n'
        'note: {d}/no_timeout.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/non_required.yml: marked ci-policy non-required; out of scope\n'
        'note: {d}/schedule_only.yml: no pull_request trigger; full-run path, out of scope\n'
        'note: {d}/ungated.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/wf_paths.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/unparseable.yml: no pull_request trigger; full-run path, out of scope\n'
        'CI policy OK (warnings above)\n'
    )),
    'error': (1, (
        "::error file={d}/ci_ok_gap.yml,line=10,title=ci-ok needs::{d}/ci_ok_gap.yml:10: job `tests` is not in `ci-ok` `needs:` directly (a job reached only through another job's `needs:` is skipped, not failed, when its dependency fails), so it can go red and still merge. Add it to `ci-ok` `needs:` (and to any per-job list the gate reads), or mark the job `# ci-ok: exempt <reason>`.\n"
        '::error title=Change gating::{d}/exempt_but_heavy.yml: heavy job `mirror` `timeout-minutes: 30` with no change-gating `if:`. Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::error file={d}/exempt_no_reason.yml,line=5,title=ci-ok needs::{d}/exempt_no_reason.yml:5: job `drain` has `# ci-ok: exempt` with no reason. Say why it sits outside the required gate.\n'
        '::error title=Change gating::{d}/no_timeout.yml: heavy job `build` has no `timeout-minutes` (GitHub defaults to 6h). Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::error title=Change gating::{d}/ungated.yml: heavy job `heavy` `timeout-minutes: 60` with no change-gating `if:`. Run it only when its area changed (`needs:` the detector, `if:` on its outputs), or record why it always runs.\n'
        '::error title=Change gating::{d}/wf_paths.yml: workflow-level `on: pull_request: paths:` silences required checks on PRs outside it. Gate at job level instead (`needs:` + `if:`, aggregated by `ci-ok`), or mark the workflow `ci-policy: non-required` with a reason when it truly produces no required check.\n'
        'note: {d}/no_timeout.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/non_required.yml: marked ci-policy non-required; out of scope\n'
        'note: {d}/schedule_only.yml: no pull_request trigger; full-run path, out of scope\n'
        'note: {d}/ungated.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/wf_paths.yml: no `ci-ok` job; needs-completeness rule skipped\n'
        'note: {d}/unparseable.yml: no pull_request trigger; full-run path, out of scope\n'
    )),
}


def scrubbed_env(mode=None, extra=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    if mode is not None:
        env["CI_POLICY_MODE"] = mode
    env.update(extra or {})
    return env


def run(args, cwd, mode=None, extra=None):
    p = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=cwd,
                       env=scrubbed_env(mode, extra), capture_output=True,
                       text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


class Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def write(self, name, text):
        p = self.d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
        return str(p)

    def paths(self, mapping):
        return [self.write(n, t) for n, t in sorted(mapping.items())]

    def parse(self, out, rc=None, err=""):
        try:
            doc = json.loads(out)
        except ValueError:
            self.fail(f"--json stdout is not one JSON object (rc={rc}):\n{out}\n{err}")
        self.assertIsInstance(doc, dict)
        return doc

    def report(self, args, mode=None, cwd=None, extra=None):
        rc, out, err = run(args, cwd or self.d, mode, extra)
        self.assertNotIn("Traceback", err, err)
        self.assertNotIn("Traceback", out, out)
        doc = self.parse(out, rc, err)
        self.assert_shape(doc)
        return rc, doc, out, err

    def assert_shape(self, doc):
        self.assertEqual(SCHEMA, doc.get("schema"))
        self.assertIn(doc.get("mode"), ("warn", "error"))
        self.assertIn(doc.get("outcome"), ("clean", "findings", "unmeasurable"))
        totals = doc.get("totals")
        self.assertIsInstance(totals, dict)
        for k in ("requested", "linted", "skipped", "unmeasurable", "findings"):
            self.assertIs(type(totals.get(k)), int, k)
        self.assertEqual(totals["requested"],
                         totals["linted"] + totals["skipped"] + totals["unmeasurable"])
        self.assertIsInstance(doc.get("findings"), list)
        self.assertIsInstance(doc.get("unmeasurable"), list)
        self.assertEqual(totals["findings"], len(doc["findings"]))
        self.assertEqual(totals["unmeasurable"],
                         len([u for u in doc["unmeasurable"] if u["path"] is not None]))
        for f in doc["findings"]:
            self.assertEqual(FINDING_KEYS, set(f), f)
            self.assertIn(f["code"], RULE, f)
            self.assertEqual(RULE[f["code"]], f["rule"], f)
            self.assertEqual(NEXT_ACTION[f["code"]], f["next_action"], f)
            self.assertIs(f["retryable"], False)
            self.assertIsInstance(f["path"], str)
            if f["code"] == "workflow-level-paths":
                self.assertIsNone(f["job"])
                self.assertTrue(f["line"] is None or (type(f["line"]) is int and f["line"] >= 1))
            else:
                self.assertIsInstance(f["job"], str)
                self.assertIs(type(f["line"]), int)
                self.assertGreaterEqual(f["line"], 1)
        keys = [(f["path"], f["job"] or "", f["rule"], f["code"]) for f in doc["findings"]]
        self.assertEqual(sorted(keys), keys, "findings must be sorted")
        for u in doc["unmeasurable"]:
            self.assertEqual(UNMEASURABLE_KEYS, set(u), u)
            self.assertEqual(NEXT_ACTION.get(u["code"]), u["next_action"], u)
            self.assertEqual(u["code"] == "no-inputs-discovered", u["path"] is None, u)
            if u["path"] is not None:
                self.assertIsInstance(u["path"], str)
            self.assertIs(u["retryable"], False)
        ukeys = [(u["path"] or "", u["code"]) for u in doc["unmeasurable"]]
        self.assertEqual(sorted(ukeys), ukeys, "unmeasurable must be sorted")
        expected_outcome = ("unmeasurable" if doc["unmeasurable"] or totals["requested"] == 0
                            else "findings" if doc["findings"] else "clean")
        self.assertEqual(expected_outcome, doc["outcome"])
        limits = (doc.get("coverage") or {}).get("limits")
        self.assertIsInstance(limits, list)
        codes = [lim.get("code") for lim in limits]
        self.assertEqual(len(codes), len(set(codes)), codes)
        self.assertEqual(1, codes.count(LIMIT_ALWAYS), codes)
        for lim in limits:
            self.assertIsInstance(lim.get("description"), str)
            self.assertTrue(lim["description"].strip())
            self.assertIsInstance(lim.get("paths"), list)
            self.assertEqual(sorted(set(lim["paths"])), lim["paths"])

    def limits(self, doc):
        return {lim["code"]: lim for lim in doc["coverage"]["limits"]}

    def assert_no_echo(self, *texts):
        for text in texts:
            for s in SENTINELS:
                self.assertNotIn(s, text)


# --- RED anchor: the flag exists ----------------------------------------------

class JsonFlag(Base):
    def test_json_flag_is_recognised_and_prints_one_schema_object(self):
        path = self.write("clean.yml", CLEAN)
        rc, out, err = run(["--json", path], self.d, "error")
        self.assertNotIn("Traceback", err, err)
        self.assertEqual(0, rc, f"stdout={out!r} stderr={err!r}")
        self.assertEqual(SCHEMA, self.parse(out, rc, err)["schema"])

    def test_json_flag_position_does_not_matter(self):
        path = self.write("ungated.yml", UNGATED)
        _, before, _ = run(["--json", path], self.d, "warn")
        _, after, _ = run([path, "--json"], self.d, "warn")
        self.assertEqual(SCHEMA, self.parse(before)["schema"])
        self.assertEqual(before, after)

    def test_in_process_main_keeps_env_and_argv_convention(self):
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import io
        from contextlib import redirect_stdout
        import ci_policy
        path = self.write("ungated.yml", UNGATED)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = ci_policy.main({"CI_POLICY_MODE": "error"}, ["ci_policy.py", "--json", path])
        except OSError as exc:
            self.fail(f"main() did not accept --json: {exc!r}")
        self.assertEqual(1, rc)
        doc = self.parse(buf.getvalue(), rc)
        self.assertEqual("findings", doc["outcome"])
        self.assertEqual("error", doc["mode"])


# --- outcomes ------------------------------------------------------------------

class Outcomes(Base):
    def test_clean_outcome(self):
        rc, doc, _, _ = self.report(["--json", self.write("clean.yml", CLEAN)], "error")
        self.assertEqual(0, rc)
        self.assertEqual("clean", doc["outcome"])
        self.assertEqual([], doc["findings"])
        self.assertEqual([], doc["unmeasurable"])
        self.assertEqual({"requested": 1, "linted": 1, "skipped": 0, "unmeasurable": 0,
                          "findings": 0},
                         {k: doc["totals"][k] for k in
                          ("requested", "linted", "skipped", "unmeasurable", "findings")})

    def test_findings_outcome_has_one_exact_record_per_violation(self):
        paths = self.paths(MEASURABLE)
        rc, doc, out, err = self.report(["--json", *paths], "warn")
        self.assertEqual(0, rc)
        self.assertEqual("findings", doc["outcome"])
        got = [(Path(f["path"]).name, f["job"], f["line"], f["code"]) for f in doc["findings"]]
        expected = list(EXPECTED_FINDINGS)
        # workflow-level line may be an int anchor or null; compare it loosely.
        got = [(n, j, None if c == "workflow-level-paths" else ln, c) for n, j, ln, c in got]
        self.assertEqual(expected, got)
        for f in doc["findings"]:
            self.assertIn(f["path"], paths, "explicit paths are echoed verbatim")
        t = doc["totals"]
        self.assertEqual((10, 8, 2, 0, 6),
                         (t["requested"], t["linted"], t["skipped"], t["unmeasurable"],
                          t["findings"]))
        self.assert_no_echo(out, err)

    def test_heavy_threshold_env_is_honoured(self):
        path = self.write("ungated.yml", UNGATED)
        rc, doc, _, _ = self.report(["--json", path], "error",
                                    extra={"HEAVY_TIMEOUT_MINUTES": "90"})
        self.assertEqual(0, rc)
        self.assertEqual("clean", doc["outcome"])

    def test_unparseable_workflow_is_unmeasurable_never_clean(self):
        path = self.write("unparseable.yml", UNPARSEABLE)
        for mode in ("warn", "error"):
            rc, doc, out, err = self.report(["--json", path], mode)
            self.assertEqual(2, rc, mode)
            self.assertEqual("unmeasurable", doc["outcome"])
            self.assertEqual([{"path": path, "code": "workflow-unparseable", "retryable": False,
                               "next_action": "fix_workflow_syntax"}], doc["unmeasurable"])
            self.assertEqual(0, doc["totals"]["skipped"], "unparseable is not a skip")
            for lim in doc["coverage"]["limits"]:
                self.assertNotIn(path, lim["paths"])
            self.assert_no_echo(out, err)

    def test_missing_input_is_unmeasurable(self):
        missing = str(self.d / "nope" / "absent.yml")
        rc, doc, _, _ = self.report(["--json", missing], "warn")
        self.assertEqual(2, rc)
        self.assertEqual([{"path": missing, "code": "input-missing", "retryable": False,
                           "next_action": "fix_input_path"}], doc["unmeasurable"])

    def test_invalid_encoding_is_unmeasurable(self):
        path = self.write("bad.yml", b"on:\n  pull_request:\n\xff\xfe\xfa jobs:\n")
        rc, doc, _, _ = self.report(["--json", path], "warn")
        self.assertEqual(2, rc)
        self.assertEqual(["input-invalid-encoding"], [u["code"] for u in doc["unmeasurable"]])

    def test_partial_failure_keeps_measured_findings(self):
        good = self.write("ungated.yml", UNGATED)
        bad = self.write("unparseable.yml", UNPARSEABLE)
        missing = str(self.d / "absent.yml")
        rc, doc, _, _ = self.report(["--json", good, bad, missing], "error")
        self.assertEqual(2, rc)
        self.assertEqual("unmeasurable", doc["outcome"])
        self.assertEqual([(good, "heavy-job-ungated")],
                         [(f["path"], f["code"]) for f in doc["findings"]])
        self.assertEqual([(missing, "input-missing"), (bad, "workflow-unparseable")],
                         [(u["path"], u["code"]) for u in doc["unmeasurable"]])
        t = doc["totals"]
        self.assertEqual((3, 1, 0, 2), (t["requested"], t["linted"], t["skipped"],
                                        t["unmeasurable"]))

    def test_no_discovered_workflows_is_unmeasurable(self):
        rc, doc, _, _ = self.report(["--json"], "warn")
        self.assertEqual(2, rc)
        self.assertEqual([{"path": None, "code": "no-inputs-discovered", "retryable": False,
                           "next_action": "provide_workflow_files"}], doc["unmeasurable"])
        self.assertEqual((0, 0), (doc["totals"]["requested"], doc["totals"]["unmeasurable"]))

    def test_default_discovery_paths_are_repo_relative(self):
        self.write(".github/workflows/b.yml", NO_TIMEOUT)
        self.write(".github/workflows/a.yml", UNGATED)
        rc, doc, _, _ = self.report(["--json"], "warn")
        self.assertEqual(0, rc)
        self.assertEqual([".github/workflows/a.yml", ".github/workflows/b.yml"],
                         [f["path"] for f in doc["findings"]])


# --- stability -------------------------------------------------------------------

class Stability(Base):
    def test_two_runs_are_byte_identical_and_argv_order_free(self):
        paths = self.paths(MEASURABLE)
        paths.append(self.write("unparseable.yml", UNPARSEABLE))
        first = run(["--json", *paths], self.d, "error")
        second = run(["--json", *paths], self.d, "error")
        self.assertEqual(first, second)
        reverse = run(["--json", *reversed(paths)], self.d, "error")
        self.assertEqual(first[0], reverse[0])
        a, b = self.parse(first[1]), self.parse(reverse[1])
        self.assertEqual(a["findings"], b["findings"])
        self.assertEqual(a["unmeasurable"], b["unmeasurable"])
        self.assertEqual(a["coverage"], b["coverage"])
        self.assertEqual(a["totals"], b["totals"])


# --- exit parity prose vs JSON -------------------------------------------------------

class ExitParity(Base):
    def test_exit_codes_match_prose_in_warn_and_error_modes(self):
        sets = {
            "clean": [self.write("clean.yml", CLEAN)],
            "findings": self.paths(MEASURABLE),
            "skipped-only": [self.write("schedule_only.yml", SCHEDULE_ONLY),
                             self.write("non_required.yml", NON_REQUIRED)],
        }
        expected = {("clean", "warn"): 0, ("clean", "error"): 0,
                    ("findings", "warn"): 0, ("findings", "error"): 1,
                    ("skipped-only", "warn"): 0, ("skipped-only", "error"): 0}
        for (name, mode), want in expected.items():
            with self.subTest(name=name, mode=mode):
                prose_rc, _, _ = run(sets[name], self.d, mode)
                json_rc, doc, _, _ = self.report(["--json", *sets[name]], mode)
                self.assertEqual(want, prose_rc)
                self.assertEqual(prose_rc, json_rc)
                self.assertEqual(mode, doc["mode"])

    def test_mode_echo_follows_env_normalisation(self):
        path = self.write("clean.yml", CLEAN)
        for raw, echo in ((None, "warn"), ("warn", "warn"), (" ERROR ", "error"),
                          ("bogus", "warn")):
            with self.subTest(raw=raw):
                _, doc, _, _ = self.report(["--json", path], raw)
                self.assertEqual(echo, doc["mode"])

    def test_repo_workflows_json_matches_prose_in_error_mode(self):
        prose_rc, _, _ = run([], REPO, "error")
        json_rc, doc, _, _ = self.report(["--json"], "error", cwd=REPO)
        self.assertEqual(prose_rc, json_rc)
        self.assertNotEqual("unmeasurable", doc["outcome"], doc["unmeasurable"])


# --- coverage limits -----------------------------------------------------------------

class CoverageLimits(Base):
    def test_always_limit_is_disclosed_on_every_report(self):
        cases = {
            "clean": ["--json", self.write("clean.yml", CLEAN)],
            "findings": ["--json", self.write("ci_ok_gap.yml", CI_OK_GAP)],
            "unmeasurable": ["--json", self.write("unparseable.yml", UNPARSEABLE)],
        }
        for name, args in cases.items():
            with self.subTest(name=name):
                _, doc, _, _ = self.report(args, "warn")
                lim = self.limits(doc)[LIMIT_ALWAYS]
                self.assertEqual([], lim["paths"])
                self.assertIn("always()", lim["description"])

    def test_skipped_classes_are_listed_exactly(self):
        paths = dict(zip(sorted(MEASURABLE), self.paths(MEASURABLE)))
        _, doc, _, _ = self.report(["--json", *paths.values()], "warn")
        lims = self.limits(doc)
        self.assertEqual({LIMIT_ALWAYS, LIMIT_NO_PR, LIMIT_NON_REQUIRED, LIMIT_NO_CI_OK},
                         set(lims))
        self.assertEqual([paths["schedule_only.yml"]], lims[LIMIT_NO_PR]["paths"])
        self.assertEqual([paths["non_required.yml"]], lims[LIMIT_NON_REQUIRED]["paths"])
        self.assertEqual(sorted([paths["no_timeout.yml"], paths["ungated.yml"],
                                 paths["wf_paths.yml"]]),
                         lims[LIMIT_NO_CI_OK]["paths"])

    def test_skip_limits_absent_when_nothing_was_skipped(self):
        _, doc, _, _ = self.report(["--json", self.write("clean.yml", CLEAN)], "warn")
        self.assertEqual({LIMIT_ALWAYS}, set(self.limits(doc)))


# --- no content echo -------------------------------------------------------------------

class NoContentEcho(Base):
    def test_records_carry_location_and_code_only(self):
        paths = self.paths(MEASURABLE)
        paths.append(self.write("unparseable.yml", UNPARSEABLE))
        for mode in ("warn", "error"):
            rc, doc, out, err = self.report(["--json", *paths], mode)
            self.assert_no_echo(out, err)
            self.assertNotIn("::warning", out)
            self.assertNotIn("::error", out)
            self.assertNotIn("secrets.", out)
            self.assertNotIn("echo ", out)
            self.assertNotIn("timeout-minutes", out)


# --- exempt / non-required marker interplay ------------------------------------------------

class Markers(Base):
    def codes(self, text, name="wf.yml"):
        _, doc, _, _ = self.report(["--json", self.write(name, text)], "error")
        return doc, [(f["job"], f["code"]) for f in doc["findings"]]

    def test_exempt_with_reason_is_clean(self):
        doc, codes = self.codes(EXEMPT_REASON)
        self.assertEqual([], codes)
        self.assertEqual("clean", doc["outcome"])

    def test_exempt_without_reason_is_its_own_code(self):
        _, codes = self.codes(EXEMPT_NO_REASON)
        self.assertEqual([("drain", "ci-ok-exempt-no-reason")], codes)

    def test_ci_ok_exempt_does_not_hide_change_gating(self):
        _, codes = self.codes(EXEMPT_BUT_HEAVY)
        self.assertEqual([("mirror", "heavy-job-ungated")], codes)

    def test_non_required_marker_skips_every_rule(self):
        doc, codes = self.codes(NON_REQUIRED)
        self.assertEqual([], codes)
        self.assertEqual("clean", doc["outcome"])
        self.assertEqual((1, 0, 1), (doc["totals"]["requested"], doc["totals"]["linted"],
                                     doc["totals"]["skipped"]))
        self.assertIn(LIMIT_NON_REQUIRED, self.limits(doc))

    def test_scope_guard_wins_over_non_required_marker(self):
        text = "# ci-policy: non-required -- nightly\n" + SCHEDULE_ONLY
        doc, codes = self.codes(text)
        self.assertEqual([], codes)
        lims = self.limits(doc)
        self.assertIn(LIMIT_NO_PR, lims)
        self.assertNotIn(LIMIT_NON_REQUIRED, lims)


# --- usage boundaries --------------------------------------------------------------------

class Usage(Base):
    def test_help_is_plain_text_naming_the_flag_and_schema(self):
        for args in (["--help"], ["-h"], ["--json", "--help"]):
            with self.subTest(args=args):
                rc, out, err = run(args, self.d, "warn")
                self.assertEqual(0, rc, err)
                self.assertIn("--json", out)
                self.assertIn(SCHEMA, out)
                with self.assertRaises(ValueError):
                    json.loads(out)

    def test_unknown_option_is_a_usage_error(self):
        path = self.write("clean.yml", CLEAN)
        for args in (["--bogus", path], ["--json", "--bogus", path]):
            with self.subTest(args=args):
                rc, out, err = run(args, self.d, "warn")
                self.assertEqual(2, rc)
                self.assertEqual("", out)
                self.assertTrue(err.strip())
                self.assertNotIn("Traceback", err)


# --- default behaviour unchanged ------------------------------------------------------------

class ProseUnchanged(Base):
    def golden_paths(self):
        out = []
        for name in GOLDEN_FILES:
            text = MEASURABLE.get(name, UNPARSEABLE)
            out.append(self.write(name, text))
        return out

    def test_prose_output_is_byte_identical_to_base(self):
        paths = self.golden_paths()
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                rc, out, err = run(paths, self.d, mode)
                want_rc, want_out = GOLDEN_PROSE[mode]
                self.assertEqual(want_rc, rc)
                self.assertEqual(want_out.replace("{d}", str(self.d)), out)
                self.assertEqual("", err)

    def test_no_mode_writes_files(self):
        paths = self.golden_paths()
        before = sorted(p.relative_to(self.d) for p in self.d.rglob("*"))
        run(paths, self.d, "error")
        run(["--json", *paths], self.d, "error")
        run(["--help"], self.d, "warn")
        self.assertEqual(before, sorted(p.relative_to(self.d) for p in self.d.rglob("*")))


if __name__ == "__main__":
    unittest.main()
