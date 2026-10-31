#!/usr/bin/env python3
"""Offline acceptance for the reusable diagnostic-delta workflow boundary.

Paired with test_diagnostic_delta.py; builder must preserve both test files.
Uses PyYAML, already used by the existing org-template suite. No GitHub calls.

Minimal integration contract: .github/workflows/diagnostic-delta.yml exposes
workflow_call inputs base-sha/head-sha/tools-ref (required strings), job
'diagnostic-delta', a 'Validate immutable inputs' run step before checkouts,
job env BASE_SHA/HEAD_SHA/TOOLS_REF plus CALL_INPUTS=toJSON(inputs), exact BASE
then HEAD checkouts sharing caller/, pinned tooling at diagnostic-delta-tools/.
The checker run step supplies CHECKER pointing at the pinned script. REPORT_PATH
is a job env workspace-absolute JSON artifact path. Retain it with always() even
on failure, never mask checker exit status. The caller NEVER supplies a script.

Runtime tests execute the actual validation shell and checker shell against a
fixture executable; they test SHA routing, argument quoting, retained report
bytes and nonzero propagation, not just strings existing in YAML. The fixture
executable records inputs; it does not implement the ratchet or fake its verdict.
Structural assertions cover GitHub-only permissions/runner/action semantics;
these do NOT claim a hosted Actions run occurred. Native triggers are optional.
"""
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / ".github/workflows/diagnostic-delta.yml"


class DiagnosticDeltaWorkflow(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), "Missing reusable integration: .github/workflows/diagnostic-delta.yml")
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.job = self.workflow["jobs"]["diagnostic-delta"]
        self.steps = self.job["steps"]
        self.validators = [s for s in self.steps if s.get("name") == "Validate immutable inputs"]
        self.assertEqual(len(self.validators), 1, "One pre-checkout immutable-input validator is required")
        self.validator = self.validators[0]
        self.checkouts = [s for s in self.steps if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(self.checkouts), 3, "Acquire exact base, caller head and pinned tooling separately")
        callers = [s for s in self.checkouts if s.get("with", {}).get("path") == "caller"]
        self.assertEqual(len(callers), 2, "Both caller snapshots must share the same object database")
        self.base_checkout, self.caller = callers
        self.tools, = [s for s in self.checkouts if s.get("with", {}).get("path") == "diagnostic-delta-tools"]
        self.checker, = [s for s in self.steps if "CHECKER" in s.get("env", {})]
        self.tmp = tempfile.TemporaryDirectory(prefix="delta-workflow-")
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        (self.workspace / "caller").mkdir()

    def expression(self, value, contexts):
        if "${{" not in value:
            return value
        self.assertEqual(value.count("${{"), 1, "Only one expression is supported in offline harness: " + value)
        match = re.fullmatch(r"(.*?)\$\{\{(.*?)\}\}(.*)", value, re.DOTALL)
        self.assertIsNotNone(match, "Unsupported expression in offline harness: " + value)
        prefix, expression, suffix = match.groups()
        if prefix or suffix:
            resolved = self.expression("${{" + expression + "}}", contexts)
            rendered = str(resolved).lower() if isinstance(resolved, bool) else str(resolved)
            return prefix + rendered + suffix
        expression = expression.strip()
        if expression == "toJSON(inputs)":
            return json.dumps(contexts["inputs"])
        # Only the sibling workflow's simple expression subset. Reject new
        # syntax rather than silently evaluating the wrong Actions semantics.
        pattern = r"'[^']*'|(?:inputs|github|env)(?:\.[\w-]+)+|==|!=|&&|\|\||[()]|true|false"
        self.assertRegex(expression, r"\A(?:\s*(?:" + pattern + r"))+\s*\Z",
                         "Unsupported expression in offline harness: " + expression)
        rendered = []
        for token in re.findall(pattern, expression):
            if token.startswith(("inputs.", "github.", "env.")):
                resolved = contexts
                for part in token.split("."):
                    resolved = resolved.get(part, "") if isinstance(resolved, dict) else ""
                rendered.append(repr(resolved))
            else:
                rendered.append({"&&": "and", "||": "or", "true": "True", "false": "False"}.get(token, token))
        return eval(" ".join(rendered), {"__builtins__": {}})

    def contexts(self, inputs):
        context = {"inputs": inputs, "env": {}, "github": {
            "workspace": str(self.workspace), "repository": "fixture/consumer",
            "sha": "d" * 40, "event": {"pull_request": {
                "base": {"sha": "e" * 40}, "head": {"sha": "f" * 40}}}}}
        for name, value in self.job.get("env", {}).items():
            resolved = self.expression(value, context)
            context["env"][name] = str(resolved).lower() if isinstance(resolved, bool) else str(resolved)
        return context

    def run_step(self, step, context, extra_env=None):
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
               "GITHUB_WORKSPACE": str(self.workspace), **context["env"], **(extra_env or {})}
        env.update({key: str(self.expression(value, context)) for key, value in step.get("env", {}).items()})
        cwd = self.workspace / self.expression(step.get("working-directory", "."), context)
        # GitHub's explicit bash shell uses --noprofile --norc -e -o pipefail.
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=10)

    def test_required_immutable_inputs_validate_before_any_checkout(self):
        inputs = self.workflow["on"]["workflow_call"]["inputs"]
        for name in ("base-sha", "head-sha", "tools-ref"):
            self.assertEqual(inputs[name]["required"], "true")
            self.assertEqual(inputs[name]["type"], "string")
        self.assertNotIn("pull_request_target", self.workflow["on"])
        for step in self.checkouts:
            self.assertLess(self.steps.index(self.validator), self.steps.index(step))
        self.assertLess(self.steps.index(self.tools), self.steps.index(self.checker))
        self.assertEqual(self.checkouts, [self.base_checkout, self.caller, self.tools],
                         "Exact base acquisition must precede head and separate pinned tooling")
        values = {"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40}
        context = self.contexts(values)
        result = self.run_step(self.validator, context)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(context["env"]["BASE_SHA"], "a" * 40)
        self.assertEqual(context["env"]["HEAD_SHA"], "b" * 40)
        self.assertEqual(context["env"]["TOOLS_REF"], "c" * 40)
        self.assertEqual(self.expression(self.base_checkout["with"]["ref"], context), "a" * 40)
        self.assertEqual(self.expression(self.caller["with"]["ref"], context), "b" * 40)
        self.assertEqual(self.expression(self.tools["with"]["ref"], context), "c" * 40)
        for base_length, head_length in ((64, 40), (40, 64), (64, 64)):
            with self.subTest(base_length=base_length, head_length=head_length, tools_length=40):
                values = {"base-sha": "a" * base_length, "head-sha": "b" * head_length, "tools-ref": "c" * 40}
                result = self.run_step(self.validator, self.contexts(values))
                self.assertNotEqual(result.returncode, 0, "Hosted caller snapshots require 40-character commit SHAs")
        for base_length, head_length in ((40, 40), (40, 64), (64, 40), (64, 64)):
            with self.subTest(base_length=base_length, head_length=head_length, tools_length=64):
                values = {"base-sha": "a" * base_length, "head-sha": "b" * head_length, "tools-ref": "c" * 64}
                result = self.run_step(self.validator, self.contexts(values))
                self.assertNotEqual(result.returncode, 0, "A 64-character tools-ref is not a pinned checkout commit")

    def test_pr_only_base_survives_head_update_in_the_same_object_database(self):
        # Characterize the declared Git fetch boundary, not a hosted checkout
        # action: ordinary branch/tag history need not contain the requested base.
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file"}

        def git(repo, *args, check=True):
            result = subprocess.run(["git", *args], cwd=repo, env=env,
                                    capture_output=True, text=True, timeout=10)
            if check:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        seed = self.workspace / "seed"
        remote = self.workspace / "remote.git"
        head_only = self.workspace / "head-only"
        for directory in (seed, remote, head_only):
            directory.mkdir()
        git(remote, "init", "--bare", "-q")
        git(seed, "init", "-q", "--initial-branch=main")
        git(seed, "config", "user.name", "Fixture")
        git(seed, "config", "user.email", "fixture@example.invalid")
        (seed / "README.md").write_text("common root\n")
        git(seed, "add", "README.md")
        attribution = "\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>"
        git(seed, "commit", "-qm", "test: common fixture" + attribution)
        common = git(seed, "rev-parse", "HEAD").stdout.strip()
        tree = git(seed, "rev-parse", "HEAD^{tree}").stdout.strip()
        base = git(seed, "commit-tree", tree, "-p", common, "-m",
                   "test: independent base fixture" + attribution).stdout.strip()
        (seed / "README.md").write_text("caller head\n")
        git(seed, "commit", "-qam", "test: head fixture" + attribution)
        head = git(seed, "rev-parse", "HEAD").stdout.strip()
        git(seed, "push", "-q", remote.as_uri(), base + ":refs/pull/7/head",
            head + ":refs/heads/main")
        self.assertEqual(git(remote, "rev-parse", "refs/pull/7/head").stdout.strip(), base)
        branch_refs = ("+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
        git(head_only, "init", "-q")
        git(head_only, "remote", "add", "origin", remote.as_uri())
        git(head_only, "fetch", "--no-tags", "origin", *branch_refs)
        self.assertEqual(git(head_only, "cat-file", "-e", head + "^{commit}").returncode, 0)
        self.assertNotEqual(git(head_only, "cat-file", "-e", base + "^{commit}", check=False).returncode, 0,
                            "Fixture base must be absent from ordinary branch/tag history")

        context = self.contexts({"base-sha": base, "head-sha": head, "tools-ref": "c" * 40})
        validation = self.run_step(self.validator, context)
        self.assertEqual(validation.returncode, 0, validation.stderr)
        base_path = self.workspace / self.expression(self.base_checkout["with"]["path"], context)
        head_path = self.workspace / self.expression(self.caller["with"]["path"], context)
        self.assertEqual(base_path, head_path)
        base_ref = self.expression(self.base_checkout["with"]["ref"], context)
        head_ref = self.expression(self.caller["with"]["ref"], context)
        git(base_path, "init", "-q")
        git(base_path, "remote", "add", "origin", remote.as_uri())
        git(base_path, "fetch", "--no-tags", "--depth=" + self.base_checkout["with"]["fetch-depth"],
            "origin", base_ref)
        git(base_path, "checkout", "-q", "--detach", base_ref)
        self.assertEqual(git(base_path, "rev-parse", "HEAD").stdout.strip(), base)
        object_database = git(base_path, "rev-parse", "--absolute-git-dir").stdout.strip()
        self.assertEqual(git(base_path, "rev-parse", "--is-shallow-repository").stdout.strip(), "true")
        self.assertEqual(self.caller["with"]["fetch-depth"], "0")
        git(head_path, "fetch", "--no-tags", "--unshallow", "origin", *branch_refs)
        git(head_path, "checkout", "-q", "--detach", head_ref)
        self.assertEqual(git(head_path, "rev-parse", "--absolute-git-dir").stdout.strip(), object_database)
        self.assertEqual(git(head_path, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual((head_path / "README.md").read_text(), "caller head\n")
        for commit in (base, head):
            self.assertEqual(git(head_path, "cat-file", "-t", commit).stdout.strip(), "commit")

    def test_empty_missing_mutable_or_injected_input_cannot_fall_back_to_event(self):
        marker = self.workspace / "executed"
        for key in ("base-sha", "head-sha", "tools-ref"):
            for value in (None, "", "main", "refs/heads/main", "a" * 39, "A" * 40,
                          "0" * 40 + "\n", f"$(touch '{marker}')", f"'; touch '{marker}'; #"):
                with self.subTest(key=key, value=value):
                    inputs = {"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40}
                    if value is None:
                        del inputs[key]
                    else:
                        inputs[key] = value
                    result = self.run_step(self.validator, self.contexts(inputs))
                    self.assertNotEqual(result.returncode, 0, "Bad raw call input accepted or replaced by event SHA")
                    self.assertFalse(marker.exists(), "Inputs must be inert environment values, never shell source")

    def test_public_runner_read_only_permissions_and_bounded_runtime(self):
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(self.job.get("permissions", self.workflow["permissions"]), {"contents": "read"})
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")
        self.assertGreater(int(self.job["timeout-minutes"]), 0)
        self.assertLessEqual(int(self.job["timeout-minutes"]), 5)
        self.assertNotIn("secrets", self.workflow["on"]["workflow_call"])
        self.assertNotIn("continue-on-error", self.job)
        for step in self.steps:
            self.assertNotIn("continue-on-error", step)
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[\w./-]+@[0-9a-f]{40}$")
        for step in self.checkouts:
            self.assertEqual(step["with"]["persist-credentials"], "false")
        self.assertEqual(self.base_checkout["with"]["fetch-depth"], "1")
        self.assertEqual(self.caller["with"]["fetch-depth"], "0")
        self.assertEqual(self.tools["with"]["repository"], "TogetherWeOwn/paperclip-ops")

    def test_pinned_checker_receives_exact_commits_retains_json_and_propagates_failure(self):
        inputs = {"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40}
        context = self.contexts(inputs)
        target = self.workspace / "diagnostic-delta-tools/scripts/quality-gates/diagnostic_delta.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['ARGV_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps({'fixture': 'trusted-pinned-checker', 'code': int(os.environ['FIXTURE_EXIT'])}))\n"
            "raise SystemExit(int(os.environ['FIXTURE_EXIT']))\n")
        # A caller-local checker would turn failure into success: the run must
        # ignore it even when both copies are present.
        local = self.workspace / "caller/scripts/quality-gates/diagnostic_delta.py"
        local.parent.mkdir(parents=True)
        local.write_text("raise SystemExit('untrusted caller checker executed')\n")
        checker_path = self.expression(self.checker["env"]["CHECKER"], context)
        cwd = self.workspace / self.expression(self.checker.get("working-directory", "."), context)
        self.assertEqual((cwd / checker_path).resolve(), target.resolve())
        log = self.workspace / "argv.json"
        for exit_code in (0, 1, 2):
            with self.subTest(exit_code=exit_code):
                result = self.run_step(self.checker, context,
                                       {"ARGV_LOG": str(log), "FIXTURE_EXIT": str(exit_code)})
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                argv = json.loads(log.read_text())
                self.assertEqual(argv[argv.index("--base") + 1], inputs["base-sha"])
                self.assertEqual(argv[argv.index("--head") + 1], inputs["head-sha"])
                if "--repo" in argv:
                    self.assertEqual((cwd / argv[argv.index("--repo") + 1]).resolve(), self.workspace / "caller")
                else:
                    self.assertEqual(cwd, self.workspace / "caller")
                report_path = Path(context["env"]["REPORT_PATH"])
                self.assertTrue(report_path.is_absolute(), "Artifact location must not depend on step cwd")
                self.assertEqual(json.loads(report_path.read_text()),
                                 {"fixture": "trusted-pinned-checker", "code": exit_code})

    def test_report_upload_runs_on_failure_and_missing_evidence_is_not_success(self):
        artifact, = [s for s in self.steps if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(artifact["if"].replace("${{", "").replace("}}", "").strip(), "always()")
        self.assertEqual(artifact["with"]["if-no-files-found"], "error")
        self.assertLess(self.steps.index(self.checker), self.steps.index(artifact))
        context = self.contexts({"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40})
        self.assertEqual(self.expression(artifact["with"]["path"], context), context["env"]["REPORT_PATH"])


class DiagnosticDeltaDiscovery(unittest.TestCase):
    def test_acceptance_suites_are_wired_not_allowlisted_as_orphans(self):
        # Exercise existing discovery, not grep for names in comments or
        # never-executed commands. Scope assertions to these two suites: an
        # unrelated orphan must not become part of this builder's contract.
        path = ROOT / "verification/orphan_suites.py"
        spec = importlib.util.spec_from_file_location("delta_orphan_discovery", path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        suites = set(checker.tracked_suites(ROOT))
        tracked = set(checker.tracked_files(ROOT))
        segments = checker.build_corpus(ROOT)
        discovers = checker.discover_patterns(segments)
        counts = (Counter(Path(s).name for s in suites), Counter(Path(s).stem for s in suites))
        allowlist = json.loads((ROOT / "verification/orphan_suites_allowlist.json").read_text())
        for name in ("test_diagnostic_delta.py", "test_diagnostic_delta_workflow.py"):
            with self.subTest(suite=name):
                self.assertIn(name, suites, "Acceptance files must be tracked")
                self.assertTrue(checker.is_wired(name, segments, discovers, counts, tracked),
                                "Wire acceptance suite into CI: " + name)
                self.assertNotIn(name, allowlist, "Wire acceptance suites; do not exempt them")


if __name__ == "__main__":
    unittest.main()
