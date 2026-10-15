#!/usr/bin/env python3
"""Offline checks for starter metadata, concurrency and Operator lint wiring."""
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


def expression(value, context):
    # Evaluate the starter's literal/comparison/boolean expression subset only.
    value = re.sub(r"github\.[a-zA-Z0-9_.]+",
                   lambda match: repr(context.get(match.group())), value)
    tree = ast.parse(value.strip().replace("&&", " and ").replace("||", " or "), mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare,
               ast.Eq, ast.NotEq, ast.Constant)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise ValueError("unsupported concurrency expression")
    return eval(compile(tree, "<concurrency>", "eval"), {"__builtins__": {}})


class TemplateContractTests(unittest.TestCase):
    def test_metadata_is_supplied_for_the_workflow_choice(self):
        workflow = ROOT / "workflow-templates/starter-ci.yml"
        metadata = json.loads(workflow.with_suffix(".properties.json").read_text())
        for field in ("name", "description"):
            self.assertIsInstance(metadata[field], str)
            self.assertTrue(metadata[field].strip())
        self.assertIn("starter-ci.properties.json", (ROOT / "README.md").read_text())

    def test_pr_groups_cancel_superseded_runs_only(self):
        groups = [self.concurrency("pull_request", run, number=42) for run in (101, 102, 103)]
        self.assertEqual(len({group for group, cancel in groups}), 1)
        self.assertTrue(all(cancel for group, cancel in groups))
        self.assertNotEqual(groups[0][0], self.concurrency("pull_request", 104, number=43)[0])

    def test_non_pr_groups_preserve_pending_and_running_runs(self):
        groups = []
        for event in ("push", "schedule", "workflow_dispatch", "merge_group"):
            event_groups = [self.concurrency(event, run) for run in (101, 102, 103)]
            # A, B and C never share a group: B cannot be replaced while A runs.
            self.assertEqual(len({group for group, cancel in event_groups}), 3, event)
            self.assertFalse(any(cancel for group, cancel in event_groups), event)
            groups.extend(group for group, cancel in event_groups)
        self.assertEqual(len(set(groups)), len(groups))

    def concurrency(self, event, run, number=None):
        text = (ROOT / "workflow-templates/starter-ci.yml").read_text()
        block = text.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
        group = re.search(r"^  group: (.+)$", block, re.M).group(1)
        cancel = re.search(r"^  cancel-in-progress: (.+)$", block, re.M).group(1)
        context = {"github.workflow": "CI", "github.event_name": event,
                   "github.event.pull_request.number": number,
                   "github.run_id": run, "github.ref": "refs/heads/main"}
        render = lambda value: re.sub(r"\$\{\{(.*?)\}\}",
                                     lambda match: str(expression(match.group(1), context)), value)
        return render(group), expression(re.fullmatch(r"\$\{\{(.*?)\}\}", cancel).group(1), context)

    def test_script_based_conventions_preserve_commands_and_environment(self):
        readme = (ROOT / "README.md").read_text()
        steps = readme.split("<!-- ci-policy-adoption-steps -->\n```yaml\n", 1)[1].split("```", 1)[0]
        current = (ROOT.parents[1] / ".github/workflows/pr-lint.yml").read_text()
        # The lint commands may sit on a single-line `run:` or inside a `run: |` block.
        commands = re.findall(r"^\s+(?:run: )?(python3 .*pr_standards\.py.*)$", current, re.M)
        self.assertEqual(len(commands), 2)
        for command in commands:
            relocated = command.replace("python3 ", "python3 -I ", 1).replace(
                ".github/scripts", ".ci-policy-source/.github/scripts")
            self.assertIn(relocated, steps)
        existing = current.split("      - name: Check title, body and commits\n", 1)[1].split("        run:", 1)[0]
        documented = steps.split("      - name: Check title, body and commits\n", 1)[1].split("        run:", 1)[0]
        self.assertEqual(documented, existing, "conventions metadata environment must be preserved")
        self.assertLess(steps.index("name: Check out candidate workflow files"),
                        steps.index("name: Unit-test the lint"))
        self.assertLess(steps.index("name: Unit-test the lint"),
                        steps.index("name: Check title, body and commits"))
        self.assertLess(steps.index("name: Check title, body and commits"),
                        steps.index("name: CI change-gating policy"))

    def test_operator_steps_run_trusted_policy_in_warn_mode(self):
        readme = (ROOT / "README.md").read_text()
        steps = readme.split("<!-- ci-policy-adoption-steps -->\n```yaml\n", 1)[1].split("```", 1)[0]
        self.assertIn("ref: ${{ github.event.pull_request.base.sha || github.sha }}", steps)
        self.assertIn("CI_POLICY_MODE: warn", steps)
        self.assertEqual(steps.count("persist-credentials: false"), 2)
        self.assertIn("path: .ci-policy-source", steps)
        self.assertIn("path: .ci-policy-candidate", steps)
        self.assertLess(steps.index("name: Check out trusted policy source"),
                        steps.index("name: Check out candidate workflow files"))
        lint = steps.split("      - name: CI change-gating policy\n", 1)[1]
        lint = textwrap.dedent(lint.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        tests = textwrap.dedent(steps.split("      - name: Policy lint unit tests\n", 1)[1]
                               .split("        run: |\n", 1)[1])
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("RUNNER_TEMP")
        with tempfile.TemporaryDirectory(prefix="template-contract-", dir=scratch) as tmp:
            repo = Path(tmp)
            source = repo / ".ci-policy-source/.github/scripts"
            candidate = repo / ".ci-policy-candidate/.github/scripts"
            workflows = repo / ".ci-policy-candidate/.github/workflows"
            for directory in (source, candidate, workflows):
                directory.mkdir(parents=True)
            shutil.copy(ROOT / "scripts/ci_policy.py", source)
            shutil.copy(ROOT / "tests/test_ci_policy.py", source)
            shutil.copy(ROOT / "workflow-templates/starter-ci.yml", workflows / "ci.yml")
            marker = repo / "candidate-executed"
            spoof = candidate / "ci_policy.py"
            spoof.write_text(f'open({str(marker)!r}, "w").write("executed")\n')
            spoof_bytes = spoof.read_bytes()
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.pop("BASH_ENV", None)
            env.update(CI_POLICY_MODE="warn", TMPDIR=tmp)
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", lint], cwd=repo,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("::warning title=Change gating::", result.stdout)
            self.assertIn("CI policy OK (warnings above)", result.stdout)
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", tests], cwd=repo,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Ran 10 tests", result.stderr)
            self.assertFalse(marker.exists(), "executed PR-supplied policy code")
            self.assertEqual(spoof.read_bytes(), spoof_bytes)
            self.assertEqual((source / "ci_policy.py").read_bytes(), (ROOT / "scripts/ci_policy.py").read_bytes())
            self.assertEqual((source / "test_ci_policy.py").read_bytes(), (ROOT / "tests/test_ci_policy.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
