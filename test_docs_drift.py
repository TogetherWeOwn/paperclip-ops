#!/usr/bin/env python3
"""Offline controls for the portable docs accompaniment gate."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts/quality-gates/docs_drift.py"
SPEC = importlib.util.spec_from_file_location("docs_drift", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class DocsDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docs-drift-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.policy = {"version": 1, "rules": [{"id": "api", "sources": ["src/*"], "docs": ["docs/*"]}]}
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.put("src/api.py", "before\n")
        self.put("docs/api.md", "before\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True).strip()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git("rev-parse", "HEAD")

    def check(self, expected, *, base=None, head=None):
        result = subprocess.run(["python3", str(SCRIPT), "--repo", str(self.root),
                                 "--base=" + (base or self.base), "--head=" + (head or self.commit())],
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def change_source(self):
        self.put("src/api.py", "after\n")

    def test_empty_diff_and_unrelated_change(self):
        self.assertEqual(self.check(0, head=self.base)["changed_paths"], 0)
        self.put("unrelated.txt", "change\n")
        self.assertEqual(self.check(0)["checked_rules"], 0)

    def test_source_without_docs_fails(self):
        self.change_source()
        report = self.check(1)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["findings"][0]["paths"], ["src/api.py"])
        self.assertEqual(report["findings"][0]["code"], "docs_required")

    def test_modified_or_added_docs_pass(self):
        self.change_source()
        self.put("docs/api.md", "after\n")
        self.assertEqual(self.check(0)["checked_rules"], 1)
        self.git("reset", "--hard", self.base)
        self.change_source()
        self.put("docs/new.md", "new docs\n")
        self.check(0)

    def test_deleted_docs_do_not_satisfy(self):
        self.change_source()
        (self.root / "docs/api.md").unlink()
        self.check(1)

    def test_symlink_and_mode_only_docs_do_not_satisfy(self):
        self.change_source()
        os.symlink("api.md", self.root / "docs/link.md")
        self.check(1)
        self.git("reset", "--hard", self.base)
        self.change_source()
        os.chmod(self.root / "docs/api.md", 0o755)
        self.check(1)

    def test_source_deletion_requires_docs(self):
        (self.root / "src/api.py").unlink()
        self.check(1)

    def test_source_rename_retains_old_path(self):
        self.git("mv", "src/api.py", "outside.py")
        self.assertEqual(self.check(1)["findings"][0]["paths"], ["src/api.py"])

    def test_docs_rename_outside_mapping_does_not_satisfy(self):
        self.change_source()
        self.git("mv", "docs/api.md", "outside.md")
        self.check(1)

    def test_spaces_newlines_unicode_and_option_like_paths(self):
        paths = ["src/a b.py", "src/a\nb.py", "src/☃.py", "src/--flag.py"]
        for name in paths:
            self.put(name, "new\n")
        self.assertEqual(self.check(1)["findings"][0]["paths"], sorted(paths))
        self.put("docs/a\nb.md", "new docs\n")
        self.check(0)

    def test_head_policy_deletion_or_weakening_cannot_bypass(self):
        self.change_source()
        (self.root / ".docs-drift.json").unlink()
        self.check(1)
        self.put(".docs-drift.json", json.dumps({"version": 1, "rules": []}))
        self.check(1)

    def test_new_policy_only_activates_after_merge(self):
        self.git("rm", ".docs-drift.json")
        unconfigured = self.commit()
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.change_source()
        report = self.check(0, base=unconfigured)
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["checked_rules"], 0)

    def test_malformed_base_policy_is_unmeasurable(self):
        self.put(".docs-drift.json", '{"version":1,"rules":[]}' )
        self.base = self.commit()
        self.assertEqual(self.check(2)["code"], "invalid_policy")

    def test_invalid_base_shape(self):
        for value in [[], {"version": True, "rules": self.policy["rules"]},
                      {"version": 1, "rules": [{"id": "x", "sources": ["../*"], "docs": ["docs/*"]}]}]:
            with self.subTest(value=value):
                self.put(".docs-drift.json", json.dumps(value))
                self.base = self.commit()
                self.check(2)

    def test_bad_sha_missing_object_and_disjoint_history(self):
        self.assertEqual(self.check(2, head="--help")["code"], "invalid_sha")
        self.assertEqual(self.check(2, head="0" * 40)["code"], "git_failed")
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        self.put("new", "other history\n")
        self.assertEqual(self.check(2)["code"], "git_failed")

    def test_criss_cross_merge_bases_are_unmeasurable_at_cli(self):
        tree = self.git("rev-parse", self.base + "^{tree}")

        def node(name, *parents):
            args = ["commit-tree", tree, "-m", name + "\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>"]
            for parent in parents:
                args.extend(["-p", parent])
            return self.git(*args)

        left = node("left", self.base)
        right = node("right", self.base)
        base = node("left merge", left, right)
        head = node("right merge", right, left)
        self.assertCountEqual(self.git("merge-base", "--all", base, head).splitlines(), [left, right])
        report = self.check(2, base=base, head=head)
        self.assertEqual(report["status"], "unmeasurable")
        self.assertEqual(report["code"], "ambiguous_base")
        self.assertIs(report["retryable"], False)
        self.assertTrue(report["next_action"].strip())

    def synthetic_diff_main(self, count):
        # Mock only the large git output; parsing, policy, main and JSON stay real.
        raw = b"".join(f"M\0unmapped/file-{index}\0".encode() for index in range(count))
        real_run = subprocess.run

        def run(args, *positional, **kwargs):
            if args[0] == "git" and "diff" in args and "--name-status" in args and "-z" in args:
                return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")
            return real_run(args, *positional, **kwargs)

        output = io.StringIO()
        argv = ["--repo", str(self.root), "--base", self.base, "--head", self.base]
        with patch.object(GATE.subprocess, "run", side_effect=run):
            with redirect_stdout(output):
                code = GATE.main(argv)
        return code, json.loads(output.getvalue())

    def test_nul_diff_above_path_cap_is_unmeasurable_at_main(self):
        code, report = self.synthetic_diff_main(100001)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unmeasurable")
        self.assertEqual(report["code"], "invalid_diff")
        self.assertIs(report["retryable"], False)
        self.assertTrue(report["next_action"].strip())

    def test_nul_diff_at_exact_path_cap_is_accepted(self):
        code, report = self.synthetic_diff_main(100000)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["changed_paths"], 100000)
        self.assertEqual(report["checked_rules"], 0)

    def test_target_policy_uses_supplied_base_not_merge_base(self):
        self.change_source()
        head = self.commit()
        self.git("checkout", "--detach", self.base)
        self.policy["rules"][0]["docs"] = ["manual/*"]
        self.put(".docs-drift.json", json.dumps(self.policy))
        target = self.commit()
        report = self.check(1, base=target, head=head)
        self.assertEqual(report["policy_sha"], target)
        self.assertEqual(report["merge_base_sha"], self.base)
        self.assertEqual(report["findings"][0]["docs"], ["manual/*"])

    def test_one_rule_cannot_be_satisfied_by_another_rules_docs(self):
        self.policy["rules"].append({"id": "other", "sources": ["lib/*"], "docs": ["manual/*"]})
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.base = self.commit()
        self.change_source()
        self.put("lib/other.py", "change\n")
        self.put("docs/api.md", "after\n")
        self.assertEqual([f["rule"] for f in self.check(1)["findings"]], ["other"])

    def test_overlapping_source_and_docs_cannot_satisfy_itself(self):
        self.policy["rules"][0]["docs"] = ["src/*"]
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.base = self.commit()
        self.change_source()
        self.check(1)

    def test_git_timeout_is_unmeasurable(self):
        with patch.object(GATE.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaises(GATE.Unmeasurable) as result:
                GATE.check(self.root, self.base, self.base)
        self.assertEqual(result.exception.code, "git_timeout")
        self.assertTrue(result.exception.retryable)

    def test_git_environment_cannot_redirect_repository_or_inject_config(self):
        self.change_source()
        head = self.commit()
        with patch.dict(os.environ, {"GIT_DIR": "/missing/repo.git", "GIT_WORK_TREE": "/missing",
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
                                     "GIT_CONFIG_VALUE_0": "true"}):
            report = GATE.check(self.root, self.base, head)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["checked_rules"], 1)


class DocsWorkflow(unittest.TestCase):
    def setUp(self):
        workflow = (ROOT / ".github/workflows/docs-drift.yml").read_text()
        # Read this workflow's scalar env/with fields and literal run block,
        # without a YAML dependency or a second implementation of its routing.
        job_env = re.search(r"^    env:\n((?:      .+\n)+)", workflow, re.M)
        self.assertIsNotNone(job_env, "docs-drift job env not found")
        self.job_env = self.fields(job_env[1], 6)
        steps = dict(re.findall(r"^      - name: ([^\n]+)\n(.*?)(?=^      - |\Z)", workflow, re.M | re.S))
        self.validator = textwrap.dedent(steps["Validate immutable inputs"].split("        run: |\n", 1)[1])
        self.checkout, = [step for step in steps.values() if "path: docs-drift-tools" in step]
        self.checker, = [step for step in steps.values() if "          CHECKER:" in step]

    @staticmethod
    def fields(block, indent):
        return dict(re.findall(r"^" + " " * indent + r"([\w-]+): (.+)$", block, re.M))

    def expression(self, value, contexts):
        expression = value.strip().removeprefix("${{").removesuffix("}}").strip()
        if expression == "toJSON(inputs)":
            return json.dumps(contexts["inputs"])
        # Evaluate only the string/boolean subset actually used here. Tokenize
        # fully so unsupported syntax fails instead of silently testing a stub.
        pattern = r"'[^']*'|(?:inputs|github|env)(?:\.[\w-]+)+|==|!=|&&|\|\||[()]|true|false"
        self.assertRegex(expression, r"\A(?:\s*(?:" + pattern + r"))+\s*\Z",
                         "unsupported workflow expression: " + expression)
        tokens = re.findall(pattern, expression)
        rendered = []
        for token in tokens:
            if token.startswith(("inputs.", "github.", "env.")):
                resolved = contexts
                for part in token.split("."):
                    resolved = resolved.get(part, "") if isinstance(resolved, dict) else ""
                rendered.append(repr(resolved))
            else:
                rendered.append({"&&": "and", "||": "or", "true": "True", "false": "False"}.get(token, token))
        return eval(" ".join(rendered), {"__builtins__": {}})

    def run_workflow(self, inputs, repository, event="pull_request"):
        contexts = {"inputs": inputs, "github": {"repository": repository, "event_name": event,
                    "sha": "4" * 40, "event": {"before": "3" * 40}}}
        if event == "pull_request":
            contexts["github"]["event"]["pull_request"] = {"base": {"sha": "1" * 40}, "head": {"sha": "2" * 40}}
        env = {}
        for name, value in self.job_env.items():
            result = self.expression(value, contexts)
            env[name] = str(result).lower() if isinstance(result, bool) else str(result)
        contexts["env"] = env
        validation = subprocess.run(["bash", "--noprofile", "--norc", "-c", self.validator],
                                    env={"PATH": os.environ["PATH"], **env}, cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
        condition = self.fields(self.checkout, 8)["if"]
        pinned = bool(self.expression(condition, contexts))
        checker = self.expression(self.fields(self.checker, 10)["CHECKER"], contexts)
        ref = self.expression(self.fields(self.checkout, 10)["ref"], contexts)
        return validation, env, pinned, checker, ref

    def test_native_pr_and_push_use_local_checker_without_tools_pin(self):
        for repository in ["TogetherWeOwn/paperclip-ops", "fixture/fork"]:
            for event, base, head in [("pull_request", "1" * 40, "2" * 40), ("push", "3" * 40, "4" * 40)]:
                with self.subTest(repository=repository, event=event):
                    validation, env, pinned, checker, _ = self.run_workflow({}, repository, event)
                    self.assertEqual(validation.returncode, 0, validation.stderr)
                    self.assertFalse(pinned)
                    self.assertEqual(checker, "scripts/quality-gates/docs_drift.py")
                    self.assertEqual((env["BASE_SHA"], env["HEAD_SHA"]), (base, head))

    def test_reusable_calls_use_pinned_checker_and_explicit_shas(self):
        for repository in ["TogetherWeOwn/paperclip-ops", "fixture/consumer"]:
            for size in [40, 64]:
                with self.subTest(repository=repository, sha_length=size):
                    inputs = {"base-sha": "a" * size, "head-sha": "b" * size, "tools-ref": "c" * size}
                    validation, env, pinned, checker, ref = self.run_workflow(inputs, repository)
                    self.assertEqual(validation.returncode, 0, validation.stderr)
                    self.assertTrue(pinned, "reusable calls must check out their pinned tool, even in the canonical repo")
                    self.assertEqual(checker, "../docs-drift-tools/scripts/quality-gates/docs_drift.py")
                    self.assertEqual(ref, "c" * size)
                    self.assertEqual((env["BASE_SHA"], env["HEAD_SHA"]), ("a" * size, "b" * size))

    def test_reusable_calls_reject_empty_or_mutable_tools_ref(self):
        for repository in ["TogetherWeOwn/paperclip-ops", "fixture/consumer"]:
            for ref in ["", "main", "refs/heads/main", "c" * 39, "C" * 40]:
                with self.subTest(repository=repository, tools_ref=ref):
                    inputs = {"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": ref}
                    validation, *_ = self.run_workflow(inputs, repository)
                    self.assertNotEqual(validation.returncode, 0, "invalid tools-ref accepted")
                    self.assertIn("TOOLS_REF", validation.stderr)

    def test_empty_required_call_shas_cannot_be_rescued_by_event_shas(self):
        for key, name in [("base-sha", "BASE_SHA"), ("head-sha", "HEAD_SHA")]:
            for missing in [False, True]:
                with self.subTest(key=key, missing=missing):
                    inputs = {"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40}
                    if missing:
                        inputs.pop(key)
                    else:
                        inputs[key] = ""
                    validation, *_ = self.run_workflow(inputs, "TogetherWeOwn/paperclip-ops")
                    self.assertNotEqual(validation.returncode, 0, "empty call SHA fell through to healthy event evidence")
                    self.assertIn(name, validation.stderr)


if __name__ == "__main__":
    unittest.main()
