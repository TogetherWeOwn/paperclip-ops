#!/usr/bin/env python3
"""Offline acceptance of the reusable workflow's real inline shell/Python blocks.

Only checkout is omitted: fixtures are the caller checkout. Inputs flow through
YAML env declarations, and each run block executes verbatim in a disposable dir.
This is not a GitHub Actions emulator; unsupported expressions/actions fail loudly
rather than being mistaken for an expected workflow rejection.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/reusable-behavioral-tests.yml"
GOOD = b'<testsuite tests="1"><testcase name="golden"/></testsuite>'
TWO = (b'<testsuites tests="2"><testsuite tests="1"><testcase name="one"/>'
       b'</testsuite><testsuite tests="1"><testcase name="two"/></testsuite></testsuites>')
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def command_file(path):
    """Read runner output/env files, including GitHub's multiline form."""
    fields = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            value = []
            for item in lines:
                if item == delimiter:
                    break
                value.append(item)
            else:
                raise AssertionError("unterminated runner command file value")
            fields[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
        elif line:
            raise AssertionError(f"unsupported runner command file line: {line!r}")
    return fields


class InlineWorkflow:
    def __init__(self, workflow):
        # BaseLoader keeps 'on' a string and all workflow inputs textual.
        self.workflow = workflow
        self.specs = workflow["on"]["workflow_call"]["inputs"]
        jobs = [job for job in workflow["jobs"].values() if "steps" in job]
        if len(jobs) != 1:
            raise AssertionError("expected one caller-command job")
        self.job = jobs[0]
        self.steps = self.job["steps"]
        for step in self.steps:
            if "uses" in step:
                if not re.fullmatch(r"actions/checkout@[0-9a-fA-F]{40}", step["uses"]):
                    raise AssertionError("offline harness only omits SHA-pinned checkout")
            elif "run" not in step:
                raise AssertionError("workflow step has neither run nor uses")
            if EXPRESSION.search(step.get("run", "")):
                raise AssertionError("caller input must flow via env, not shell source interpolation")

    def execute(self, directory, supplied):
        inputs = {key: value.get("default", "") for key, value in self.specs.items()}
        inputs.update(supplied)
        workspace = directory / "repo"
        workspace.mkdir(exist_ok=True)
        for name in ("home", "tmp"):
            (directory / name).mkdir(exist_ok=True)
        # Do not inherit credentials, Python modules, BASH_ENV, Git config or hooks.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(directory / "home"),
               "TMPDIR": str(directory / "tmp"), "RUNNER_TEMP": str(directory / "tmp"),
               "GITHUB_WORKSPACE": str(workspace), "PYTHONDONTWRITEBYTECODE": "1",
               "LC_ALL": "C.UTF-8"}
        states, executions = {}, []
        failed = False

        def evaluate(expression):
            expression = expression.strip()
            for operator in (" || ", " && "):
                if operator in expression:
                    left, right = expression.split(operator, 1)
                    value = evaluate(left)
                    return ((value or evaluate(right)) if operator == " || "
                            else (value and evaluate(right)))
            match = re.fullmatch(r"(.+?)\s*(==|!=)\s*('.*?'|\".*?\")", expression)
            if match:
                equal = evaluate(match[1]) == match[3][1:-1]
                return equal if match[2] == "==" else not equal
            if expression.startswith("!"):
                return not evaluate(expression[1:])
            if expression in ("always()", "success()", "failure()", "cancelled()"):
                return {"always()": True, "success()": not failed,
                        "failure()": failed, "cancelled()": False}[expression]
            if expression in ("true", "false"):
                return expression == "true"
            if expression.startswith(("'", '"')) and expression[-1:] == expression[:1]:
                return expression[1:-1]
            match = re.fullmatch(r"inputs\.([\w-]+)", expression)
            if match:
                return inputs[match[1]]
            match = re.fullmatch(r"env\.([\w]+)", expression)
            if match:
                return env.get(match[1], "")
            match = re.fullmatch(r"steps\.([\w-]+)\.(outcome|conclusion|outputs\.[\w-]+)", expression)
            if match:
                state = states.get(match[1], {})
                if match[2].startswith("outputs."):
                    return state.get("outputs", {}).get(match[2][8:], "")
                return state.get(match[2], "skipped")
            if expression == "github.workspace":
                return str(workspace)
            raise AssertionError(f"unsupported workflow expression: {expression!r}")

        def render(value):
            return EXPRESSION.sub(lambda match: str(evaluate(match[1])), str(value))

        for mapping in (self.workflow.get("env", {}), self.job.get("env", {})):
            env.update({key: render(value) for key, value in mapping.items()})
        for index, step in enumerate(self.steps):
            if "uses" in step:
                continue
            step_id = step.get("id", str(index))
            condition = step.get("if", "success()")
            condition = EXPRESSION.sub(lambda match: match[1], condition)
            implicit_success = not re.search(r"\b(always|success|failure|cancelled)\(\)", condition)
            if (failed and implicit_success) or not evaluate(condition):
                states[step_id] = {"outcome": "skipped", "conclusion": "skipped"}
                continue
            shell = step.get("shell", self.job.get("defaults", {}).get("run", {}).get("shell", "bash"))
            if shell != "bash":
                raise AssertionError(f"unsupported offline shell: {shell!r}")
            files = {name: directory / f"{index}-{name.lower()}"
                     for name in ("OUTPUT", "ENV", "STEP_SUMMARY")}
            for path in files.values():
                path.write_text("")
            step_env = dict(env, **{"GITHUB_" + name: str(path) for name, path in files.items()})
            step_env.update({key: render(value) for key, value in step.get("env", {}).items()})
            result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                                    cwd=workspace, env=step_env, capture_output=True,
                                    text=True, timeout=15)
            outcome = "success" if result.returncode == 0 else "failure"
            continued = render(step.get("continue-on-error", "false")).lower() == "true"
            states[step_id] = {"outcome": outcome,
                               "conclusion": "success" if continued else outcome,
                               "outputs": command_file(files["OUTPUT"])}
            executions.append({"name": step.get("name", step_id), "result": result,
                               "summary": files["STEP_SUMMARY"].read_text()})
            env.update(command_file(files["ENV"]))
            failed = failed or (outcome == "failure" and not continued)
        outputs = {key: render(value) for key, value in self.job.get("outputs", {}).items()}
        return {"passed": not failed, "executions": executions, "outputs": outputs,
                "log": "\n".join(item["result"].stdout + item["result"].stderr for item in executions)}


class InlineHarnessTests(unittest.TestCase):
    def test_real_blocks_get_literal_env_outputs_and_failure_state(self):
        workflow = {"on": {"workflow_call": {"inputs": {"seed": {"default": "1729"}}}},
                    "jobs": {"fixture": {"env": {"SEED": "${{ inputs.seed }}"},
                        "outputs": {"observed": "${{ steps.first.outputs.observed }}"},
                        "steps": [
                            {"id": "first", "run": 'printf "observed=%s\\n" "$SEED" >> "$GITHUB_OUTPUT"\n'
                             'printf "SHARED=visible\\n" >> "$GITHUB_ENV"\nexit 7'},
                            {"id": "skipped", "run": "touch must-not-run"},
                            {"id": "last", "if": "${{ !cancelled() && steps.first.outcome == 'failure' }}",
                             "run": 'test "$SHARED" = visible\ntest ! -e must-not-run'}]}}}
        with tempfile.TemporaryDirectory(prefix="behavioral-harness-") as tmp:
            directory = Path(tmp)
            result = InlineWorkflow(workflow).execute(directory, {"seed": "$(touch injected)"})
            self.assertFalse(result["passed"])
            self.assertEqual(result["outputs"]["observed"], "$(touch injected)")
            self.assertEqual([row["result"].returncode for row in result["executions"]], [7, 0])
            self.assertFalse((directory / "repo/injected").exists())

    def test_unsupported_expressions_are_errors_not_workflow_rejections(self):
        workflow = {"on": {"workflow_call": {"inputs": {}}}, "jobs": {"fixture": {
            "steps": [{"env": {"VALUE": "${{ unknown.context }}"}, "run": "exit 1"}]}}}
        with tempfile.TemporaryDirectory(prefix="behavioral-harness-") as tmp:
            with self.assertRaisesRegex(AssertionError, "unsupported workflow expression"):
                InlineWorkflow(workflow).execute(Path(tmp), {})

    def test_runner_command_files_support_multiline_json(self):
        with tempfile.TemporaryDirectory(prefix="behavioral-harness-") as tmp:
            path = Path(tmp) / "output"
            path.write_text('summary<<END\n{\n"tests": 1\n}\nEND\nseed=1729\n')
            result = command_file(path)
            self.assertEqual(json.loads(result["summary"]), {"tests": 1})
            self.assertEqual(result["seed"], "1729")


class BehavioralWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not WORKFLOW.is_file():
            raise AssertionError(f"missing reusable behavioral workflow: {WORKFLOW.relative_to(ROOT)}")
        cls.source = WORKFLOW.read_text()
        cls.runner = InlineWorkflow(yaml.load(cls.source, Loader=yaml.BaseLoader))

    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("RUNNER_TEMP")
        self.temp = tempfile.TemporaryDirectory(prefix="behavioral-acceptance-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.serial = 0

    def call(self, report=GOOD, *, inputs=None, test=None, setup="", stale=None):
        self.serial += 1
        directory = self.directory / str(self.serial)
        workspace = directory / "repo"
        workspace.mkdir(parents=True)
        values = dict(inputs or {})
        report_path = values.get("report-path", self.runner.specs["report-path"]["default"])
        # Fixtures live outside the caller checkout. The caller command must
        # create the report itself; the verifier never receives a prepared report.
        fixture = directory / "fixture.xml"
        if report is not None:
            fixture.write_bytes(report)
        writer = ("from pathlib import Path\n"
                  "Path('test-ran').write_text('yes')\n"
                  f"path = Path({report_path!r})\n"
                  "path.parent.mkdir(parents=True, exist_ok=True)\n"
                  f"path.write_bytes(Path({str(fixture)!r}).read_bytes())\n")
        command = "python3 -I - <<'PY'\n" + writer + "PY\n" if report is not None else "touch test-ran"
        values["test-command"] = command if test is None else test.replace("@WRITE_REPORT@", command)
        values["setup-command"] = setup
        if stale is not None:
            path = workspace / report_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stale)
        result = self.runner.execute(directory, values)
        result["workspace"] = workspace
        return result

    def accept(self, result, *, seed=1729, max_cases=100, tests=1):
        self.assertTrue(result["passed"], result["log"])
        self.assertTrue((result["workspace"] / "test-ran").exists(), result["log"])
        summary = json.loads(result["outputs"]["summary"])
        self.assertEqual(summary["seed"], seed)
        self.assertEqual(summary["max_cases"], max_cases)
        self.assertEqual(summary["tests"], tests)
        self.assertEqual(summary["completed"], tests)
        for key in ("failed", "errors", "skipped"):
            self.assertEqual(summary[key], 0)
        self.assertEqual(summary["status"], "passed")
        self.assertIn(summary, [json.loads(line) for line in result["log"].splitlines()
                                if line.startswith("{")])
        return summary

    def reject(self, result, *, report_failure=False):
        self.assertFalse(result["passed"], result["log"])
        self.assertFalse(result["outputs"].get("summary"), result["log"])
        if report_failure:
            # Do not pass a negative report test due to some unrelated setup,
            # input, or command error: the actual verifier must reject the XML.
            rows = result["executions"]
            self.assertGreaterEqual(len(rows), 3, result["log"])
            for row in rows[:-1]:
                self.assertEqual(row["result"].returncode, 0, result["log"])
            self.assertNotEqual(rows[-1]["result"].returncode, 0, result["log"])
            self.assertTrue((result["workspace"] / "test-ran").exists())

    def test_workflow_contract_and_read_only_checkout(self):
        specs = self.runner.specs
        self.assertEqual(specs["seed"]["default"], "1729")
        self.assertEqual(specs["max-cases"]["default"], "100")
        self.assertEqual(specs["setup-command"]["default"], "")
        self.assertEqual(specs["expected-tests"]["default"], "")
        self.assertEqual(specs["report-path"]["default"], "behavioral-tests.xml")
        self.assertEqual(specs["test-command"]["required"], "true")
        for spec in specs.values():
            self.assertEqual(spec["type"], "string")
        self.assertFalse(self.runner.workflow["on"]["workflow_call"].get("secrets"))
        self.assertEqual(self.runner.job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.runner.job["timeout-minutes"], "10")
        permissions = self.runner.job.get("permissions", self.runner.workflow.get("permissions"))
        self.assertEqual(permissions, {"contents": "read"})
        checkouts = [step for step in self.runner.steps if "uses" in step]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertEqual(step.get("with", {}).get("persist-credentials"), "false")
        self.assertNotIn("secrets.", self.source)

    def test_default_inputs_and_single_suite(self):
        self.accept(self.call())

    def test_testsuites_root_counts_actual_cases_across_suites(self):
        self.accept(self.call(TWO), tests=2)

    def test_valid_reports_without_optional_aggregate_counters(self):
        for xml in (b'<testsuite><testcase name="one"/></testsuite>',
                    b'<testsuites><testsuite><testcase name="one"/></testsuite></testsuites>'):
            with self.subTest(xml=xml):
                self.accept(self.call(xml))

    def test_seed_and_case_budget_boundaries(self):
        for seed in ("0", "2147483647"):
            for cases in ("1", "1000"):
                with self.subTest(seed=seed, max_cases=cases):
                    self.accept(self.call(inputs={"seed": seed, "max-cases": cases}),
                                seed=int(seed), max_cases=int(cases))

    def test_invalid_seed_rejected_before_caller_command(self):
        self.accept(self.call())
        for value in ("", "-1", "2147483648", "+1", "1.5", "1e3", "0x10",
                      " 1", "1 ", "1\n2", "١", "9" * 100, "$(touch seed-injected)"):
            with self.subTest(seed=value):
                result = self.call(inputs={"seed": value})
                self.reject(result)
                self.assertFalse((result["workspace"] / "test-ran").exists())
                self.assertFalse((result["workspace"] / "seed-injected").exists())

    def test_invalid_max_cases_rejected_before_caller_command(self):
        self.accept(self.call())
        for value in ("", "0", "-1", "1001", "+1", "1.5", "1e2", " 1", "1 ",
                      "1\n2", "١", "9" * 100, "$(touch cases-injected)"):
            with self.subTest(max_cases=value):
                result = self.call(inputs={"max-cases": value})
                self.reject(result)
                self.assertFalse((result["workspace"] / "test-ran").exists())
                self.assertFalse((result["workspace"] / "cases-injected").exists())

    def test_optional_expected_tests_requires_exact_count(self):
        self.accept(self.call(TWO, inputs={"expected-tests": ""}), tests=2)
        summary = self.accept(self.call(TWO, inputs={"expected-tests": "2"}), tests=2)
        self.assertEqual(summary["expected_tests"], 2)
        for expected in ("1", "3"):
            with self.subTest(expected=expected):
                self.reject(self.call(TWO, inputs={"expected-tests": expected}), report_failure=True)

    def test_invalid_expected_tests(self):
        for value in ("0", "-1", "+1", "1.5", "1e2", " 1", "1 ", "١",
                      "$(touch expected-injected)"):
            with self.subTest(expected=value):
                result = self.call(inputs={"expected-tests": value})
                self.reject(result)
                self.assertFalse((result["workspace"] / "test-ran").exists())
                self.assertFalse((result["workspace"] / "expected-injected").exists())

    def test_blank_test_command_is_not_success(self):
        for command in ("", "  ", "\n\t"):
            with self.subTest(command=command):
                self.reject(self.call(test=command))

    def test_setup_and_test_both_receive_reproducible_budget(self):
        setup = ('test "$BEHAVIOR_TEST_SEED" = 42\n'
                 'test "$BEHAVIOR_TEST_MAX_CASES" = 7\ntouch setup-ran')
        test = ('test -f setup-ran\ntest "$BEHAVIOR_TEST_SEED" = 42\n'
                'test "$BEHAVIOR_TEST_MAX_CASES" = 7\n@WRITE_REPORT@')
        self.accept(self.call(setup=setup, test=test, inputs={"seed": "42", "max-cases": "7"}),
                    seed=42, max_cases=7)

    def test_setup_failure_prevents_test_and_success_summary(self):
        for setup in ("exit 7", "false\ntouch setup-must-not-run", "false | true"):
            with self.subTest(setup=setup):
                result = self.call(setup=setup)
                self.reject(result)
                self.assertFalse((result["workspace"] / "test-ran").exists())
                self.assertFalse((result["workspace"] / "setup-must-not-run").exists())

    def test_test_command_failure_cannot_be_overridden_by_valid_report(self):
        for test in ("@WRITE_REPORT@\nexit 7", "@WRITE_REPORT@\nfalse\ntrue",
                     "@WRITE_REPORT@\nfalse | true"):
            with self.subTest(test=test):
                result = self.call(test=test)
                self.reject(result)
                self.assertTrue((result["workspace"] / "test-ran").exists())

    def test_missing_report_from_successful_command(self):
        self.accept(self.call())
        self.reject(self.call(None), report_failure=True)

    def test_empty_malformed_and_non_junit_reports(self):
        self.accept(self.call())
        for xml in (b"", b" ", b"<testsuite>", b"not xml", b"\xff",
                    b'<report><testcase name="one"/></report>', b'<testcase name="one"/>'):
            with self.subTest(xml=xml):
                self.reject(self.call(xml), report_failure=True)

    def test_zero_or_omitted_testcases_are_incomplete(self):
        for xml in (b'<testsuite tests="0"/>', b'<testsuites tests="0"/>',
                    b'<testsuites><testsuite/></testsuites>', b'<testsuite tests="1"/>'):
            with self.subTest(xml=xml):
                self.reject(self.call(xml), report_failure=True)

    def test_actual_skipped_failed_or_errored_case_overrides_zero_counters(self):
        self.accept(self.call())
        for tag in ("skipped", "failure", "error"):
            xml = (f'<testsuite tests="1" skipped="0" failures="0" errors="0">'
                   f'<testcase name="incomplete"><{tag}/></testcase></testsuite>').encode()
            with self.subTest(tag=tag):
                self.reject(self.call(xml), report_failure=True)

    def test_incomplete_case_in_later_suite_is_not_ignored(self):
        for tag in ("skipped", "failure", "error"):
            xml = (f'<testsuites tests="2"><testsuite><testcase name="passed"/></testsuite>'
                   f'<testsuite><testcase name="bad"><{tag}/></testcase></testsuite></testsuites>').encode()
            with self.subTest(tag=tag):
                self.reject(self.call(xml), report_failure=True)

    def test_nonzero_aggregate_failure_counters_are_incomplete(self):
        for tag in ("testsuite", "testsuites"):
            for attr in ("skipped", "failures", "errors"):
                xml = (f'<{tag} tests="1" {attr}="1"><testsuite><testcase name="one"/>'
                       f'</testsuite></{tag}>').encode()
                with self.subTest(tag=tag, counter=attr):
                    self.reject(self.call(xml), report_failure=True)

    def test_declared_test_counts_match_actual_nodes_at_every_suite(self):
        for xml in (b'<testsuite tests="2"><testcase name="one"/></testsuite>',
                    b'<testsuite tests="0"><testcase name="one"/></testsuite>',
                    b'<testsuites tests="2"><testsuite tests="1"><testcase name="one"/></testsuite></testsuites>',
                    b'<testsuites tests="2"><testsuite tests="2"><testcase name="one"/></testsuite>'
                    b'<testsuite tests="0"><testcase name="two"/></testsuite></testsuites>'):
            with self.subTest(xml=xml):
                self.reject(self.call(xml), report_failure=True)

    def test_invalid_junit_counters_are_not_silently_coerced(self):
        for attr in ("tests", "skipped", "failures", "errors"):
            for value in ("-1", "n/a", "1.0"):
                xml = f'<testsuite {attr}="{value}"><testcase name="one"/></testsuite>'.encode()
                with self.subTest(counter=attr, value=value):
                    self.reject(self.call(xml), report_failure=True)

    def test_one_mib_report_boundary(self):
        # Valid XML at exactly the byte limit, not a malformed oversized fixture.
        xml = GOOD + b"<!--" + b"x" * (1024 * 1024 - len(GOOD) - 7) + b"-->"
        self.assertEqual(len(xml), 1024 * 1024)
        self.accept(self.call(xml))
        self.reject(self.call(xml + b" "), report_failure=True)

    def test_stale_preexisting_report_is_not_evidence_for_noop_command(self):
        self.accept(self.call())
        result = self.call(None, stale=GOOD)
        self.reject(result)
        self.assertFalse((result["workspace"] / "test-ran").exists())

    def test_setup_cannot_substitute_report_for_test_execution(self):
        setup = "printf %s " + shlex.quote(GOOD.decode()) + " > behavioral-tests.xml"
        result = self.call(None, setup=setup)
        self.reject(result)
        self.assertFalse((result["workspace"] / "test-ran").exists())

    def test_custom_report_path_is_literal_not_shell_source(self):
        path = "reports/space 'quote;$(touch path-injected).xml"
        result = self.call(inputs={"report-path": path})
        self.accept(result)
        self.assertTrue((result["workspace"] / path).is_file())
        self.assertFalse((result["workspace"] / "path-injected").exists())


if __name__ == "__main__":
    unittest.main()
