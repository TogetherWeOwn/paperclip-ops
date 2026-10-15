#!/usr/bin/env python3
"""Run the documented checkout/lint steps with the actual pinned checkout action.

Supply CHECKOUT_ACTION_DIST pointing to that action's dist/index.js. The test
validates its digest and uses local Git URL rewrites, never GitHub or credentials.
Source: https://github.com/actions/checkout/tree/v4.2.2#checkout-multiple-repos-side-by-side
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIN = "11bd71901bbe5b1630ceea73d27597364c9af683"
DIST_SHA256 = "9a320e563c4dc21e2291453f0c922906ef8d98cbf4961dfadb2eb4f1a0fcc15e"


class PolicyCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied = os.environ.get("CHECKOUT_ACTION_DIST")
        if not supplied:
            raise RuntimeError("CHECKOUT_ACTION_DIST must supply the pinned checkout dist/index.js")
        cls.action = Path(supplied).resolve(strict=True)
        if hashlib.sha256(cls.action.read_bytes()).hexdigest() != DIST_SHA256:
            raise RuntimeError("checkout dist/index.js does not match the documented action pin")
        readme = (ROOT / "README.md").read_text()
        cls.steps = readme.split("<!-- ci-policy-adoption-steps -->\n```yaml\n", 1)[1].split("```", 1)[0]

    def test_normal_candidate_preserves_workflows_and_reports_warnings(self):
        self.check_layout(symlink=False)

    def test_candidate_symlink_cannot_redirect_trusted_checkout(self):
        self.check_layout(symlink=True)

    def check_layout(self, symlink):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("RUNNER_TEMP")
        with tempfile.TemporaryDirectory(prefix="policy-checkout-", dir=scratch) as tmp:
            root = Path(tmp)
            origin = root / "origin"
            workspace = root / "workspace"
            home = root / "home"
            for directory in (origin, workspace, home):
                directory.mkdir()
            # No inherited Git config, hooks, auth, Python path or shell startup.
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
                   "TMPDIR": tmp, "RUNNER_TEMP": tmp,
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                   "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            git = shutil.which("git", path="/usr/bin:/bin")
            self.assertIsNotNone(git)

            def command(*args, cwd=origin, command_env=env):
                result = subprocess.run(args, cwd=cwd, env=command_env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            def commit(message):
                command(git, "add", ".")
                command(git, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
                return command(git, "rev-parse", "HEAD").stdout.strip()

            command(git, "init", "-q", "--initial-branch=main", "--template=")
            scripts = origin / ".github/scripts"
            scripts.mkdir(parents=True)
            shutil.copy(ROOT / "scripts/ci_policy.py", scripts)
            shutil.copy(ROOT / "tests/test_ci_policy.py", scripts)
            conventions = ROOT.parents[1] / ".github"
            for filename in ("pr_standards.py", "test_pr_standards.py"):
                shutil.copy(conventions / "scripts" / filename, scripts)
            shutil.copy(conventions / "pull_request_template.md", origin / ".github/pull_request_template.md")
            base = commit("trusted conventions and policy fixture")
            workflow = origin / ".github/workflows/bad.yml"
            workflow.parent.mkdir()
            workflow.write_text("""name: candidate policy violation
on:
  pull_request:
    paths: [web/**]
jobs:
  heavy:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - run: echo heavy
""")
            workflow_bytes = workflow.read_bytes()
            marker = root / "candidate-executed"
            for filename in ("ci_policy.py", "pr_standards.py"):
                (scripts / filename).write_text(f'open({str(marker)!r}, "w").write("executed")\n')
            for filename in ("test_ci_policy.py", "test_pr_standards.py"):
                (scripts / filename).write_text('raise AssertionError("candidate test executed")\n')
            (origin / ".github/pull_request_template.md").write_text("untrusted candidate template\n")
            if symlink:
                (origin / ".ci-policy-source").symlink_to(".github/workflows", target_is_directory=True)
            else:
                control = origin / ".ci-policy-source/control.txt"
                control.parent.mkdir()
                control.write_text("ordinary candidate directory\n")
            command(git, "checkout", "-q", "-b", "candidate")
            head = commit("candidate policy and symlink fixture")
            event = root / "event.json"
            event.write_text("{}")
            env.update(GITHUB_WORKSPACE=str(workspace), GITHUB_REPOSITORY="fixture/source",
                       GITHUB_REF="refs/heads/candidate", GITHUB_SHA=head,
                       GITHUB_EVENT_PATH=str(event), GITHUB_ACTION_REPOSITORY="actions/checkout",
                       GITHUB_ACTION_REF=PIN, GIT_CONFIG_COUNT="2",
                       GIT_CONFIG_KEY_0=f"url.file://{origin}/.insteadOf",
                       GIT_CONFIG_VALUE_0="https://github.com/fixture/source",
                       GIT_CONFIG_KEY_1="protocol.file.allow", GIT_CONFIG_VALUE_1="always",
                       CI_POLICY_MODE="warn")
            paths = {}
            # Parse only the documented, deliberately simple checkout input map.
            # Execute its ordering/paths, rather than an independently safe copy.
            for index, block in enumerate(self.steps.split("      - name: ")[1:]):
                name, body = block.split("\n", 1)
                if "uses:" not in body:
                    continue
                self.assertIn(f"uses: actions/checkout@{PIN}", body)
                inputs = dict(re.findall(r"^          ([\w-]+): (.+)$", body, re.M))
                action_env = dict(env)
                for key, value in inputs.items():
                    if key == "ref":
                        self.assertEqual(value, "${{ github.event.pull_request.base.sha || github.sha }}")
                        value = base
                    action_env["INPUT_" + key.upper()] = value
                action_env.update(INPUT_REPOSITORY="fixture/source", INPUT_TOKEN="not-a-credential-local-fixture")
                for suffix in ("OUTPUT", "STATE"):
                    output = root / f"{index}-{suffix.lower()}"
                    output.touch()
                    action_env["GITHUB_" + suffix] = str(output)
                command("node", str(self.action), cwd=workspace, command_env=action_env)
                paths[name] = workspace / inputs.get("path", ".")
            candidate = paths["Check out candidate workflow files"]
            source = paths["Check out trusted policy source"]
            # The old nested checkout deletes bad.yml; normal-directory control
            # preserves it, so this is not an assertion on lint wording alone.
            checked_workflow = candidate / ".github/workflows/bad.yml"
            self.assertTrue(checked_workflow.is_file(), "trusted checkout deleted candidate YAML")
            self.assertEqual(checked_workflow.read_bytes(), workflow_bytes)
            names = re.findall(r"^      - name: (.+)$", self.steps, re.M)
            self.assertEqual(names, ["Check out trusted policy source", "Check out candidate workflow files",
                                     "Unit-test the lint", "Check title, body and commits",
                                     "CI change-gating policy", "Policy lint unit tests"])
            pr_body = """## Thinking Path
> - CI adoption must preserve conventions
> - Trusted scripts lint candidate metadata
> - Sibling checkouts prevent candidate cleanup
## Linked Issues or Issue Description
Refs: case_1cab5f7f
## What Changed
- Keep conventions checks working
## Verification
- Local fixture verification passes
## Risks
- Disposable local fixture only
## Model Used
- None - human-authored
## Checklist
- [x] I searched for duplicate or related PRs and linked them above
"""
            context = {"github.event_name": "pull_request",
                       "github.event.pull_request.title": "ci(lint): preserve conventions checks",
                       "github.event.pull_request.body": pr_body,
                       "github.event.pull_request.user.login": "fixture",
                       "github.event.pull_request.head.ref": "ci/fixture",
                       "github.event.repository.private": "true",
                       "toJSON(github.event.commits)": "[]"}

            def run_step(name, overrides=None, expected_exit=0):
                block = self.steps.split("      - name: " + name + "\n", 1)[1].split("      - name:", 1)[0]
                script = textwrap.dedent(block.split("        run: |\n", 1)[1])
                step_env = dict(env, REQUIRE_CARD_REF="true", PR_STANDARDS_MODE="warn")
                for key, value in re.findall(r"^          ([A-Z_]+): (.+)$", block, re.M):
                    match = re.fullmatch(r"\$\{\{ (.*?) \}\}", value)
                    step_env[key] = context[match.group(1)] if match else value
                step_env.update(overrides or {})
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=workspace,
                                        env=step_env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                return result

            # Run conventions and policy in the documented order, with real
            # trusted scripts, their tests and the template those tests read.
            for name in names[2:]:
                result = run_step(name)
                if name == "CI change-gating policy":
                    self.assertEqual(result.stdout.count("::warning title=Change gating::"), 2, result.stdout)
                    self.assertIn("CI policy OK (warnings above)", result.stdout)
                elif name == "Check title, body and commits":
                    self.assertIn("PR conventions OK", result.stdout)
                    self.assertNotIn("::warning", result.stdout)
                else:
                    self.assertRegex(result.stderr, r"Ran [1-9]\d* tests")
                    self.assertIn("OK", result.stderr)
            # The redirected entry point must still reject bad PRs and commits.
            result = run_step("Check title, body and commits", {"TITLE": "not conventional"}, expected_exit=1)
            self.assertIn("::error title=PR title::", result.stdout)
            result = run_step("Check title, body and commits",
                              {"EVENT": "push", "COMMITS": '[{"id":"fixture","message":"not conventional"}]'},
                              expected_exit=1)
            self.assertIn("::error title=Commit on main::", result.stdout)
            self.assertFalse(marker.exists(), "PR-supplied conventions or policy code was executed")
            self.assertEqual(checked_workflow.read_bytes(), workflow_bytes)
            self.assertEqual((source / ".github/scripts/ci_policy.py").read_bytes(),
                             (ROOT / "scripts/ci_policy.py").read_bytes())
            self.assertEqual((source / ".github/scripts/test_ci_policy.py").read_bytes(),
                             (ROOT / "tests/test_ci_policy.py").read_bytes())
            for relative in ("scripts/pr_standards.py", "scripts/test_pr_standards.py", "pull_request_template.md"):
                self.assertEqual((source / ".github" / relative).read_bytes(), (conventions / relative).read_bytes())
            self.assertEqual(source.parent, candidate.parent, "checkouts must be siblings")
            self.assertNotEqual(source, candidate)
            self.assertFalse(source.is_symlink())
            if symlink:
                link = candidate / ".ci-policy-source"
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), ".github/workflows")
            else:
                self.assertEqual((candidate / ".ci-policy-source/control.txt").read_text(),
                                 "ordinary candidate directory\n")


if __name__ == "__main__":
    unittest.main()
