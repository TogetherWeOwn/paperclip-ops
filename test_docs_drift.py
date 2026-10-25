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
        if expression == "toJSON(github.event)":
            return json.dumps(contexts["github"]["event"])
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

    def resolve(self, value, contexts):
        if not isinstance(value, str) or "${{" not in value:
            return str(value)
        result = DocsWorkflow.expression(self, value, contexts)
        return str(result).lower() if isinstance(result, bool) else str(result)

    def run_workflow(self, inputs, repository, event="pull_request"):
        contexts = {"inputs": inputs, "github": {"repository": repository, "event_name": event,
                    "sha": "4" * 40, "workflow_sha": "5" * 40,
                    "workflow_ref": "TogetherWeOwn/paperclip-ops/.github/workflows/docs-drift.yml@refs/heads/main",
                    "event": {"before": "3" * 40}}}
        if event == "pull_request":
            contexts["github"]["event"]["pull_request"] = {"base": {"sha": "1" * 40}, "head": {"sha": "2" * 40}}
        env = {name: self.resolve(value, contexts) for name, value in self.job_env.items()}
        contexts["env"] = env
        validation = subprocess.run(["bash", "--noprofile", "--norc", "-c", self.validator],
                                    env={"PATH": os.environ["PATH"], **env}, cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
        condition = self.fields(self.checkout, 8).get("if", "true")
        pinned = bool(self.expression(condition, contexts))
        checker = self.resolve(self.fields(self.checker, 10)["CHECKER"], contexts)
        ref = self.resolve(self.fields(self.checkout, 10)["ref"], contexts)
        return validation, env, pinned, checker, ref

    def test_native_events_select_immutable_workflow_checker_without_call_pin(self):
        for repository in ["TogetherWeOwn/paperclip-ops", "fixture/fork"]:
            for event, base, head in [("pull_request", "1" * 40, "2" * 40), ("push", "3" * 40, "4" * 40)]:
                with self.subTest(repository=repository, event=event):
                    validation, env, pinned, checker, ref = self.run_workflow({}, repository, event)
                    self.assertEqual(validation.returncode, 0, validation.stderr)
                    self.assertTrue(pinned, "a native caller head need not contain the checker")
                    self.assertEqual(ref, "5" * 40)
                    self.assertNotEqual(checker, "scripts/quality-gates/docs_drift.py")
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


class DocsWorkflowHelper(unittest.TestCase):
    """The fixture must accept ordinary workflow values without hiding syntax errors."""

    def setUp(self):
        self.helper = DocsWorkflow()
        self.helper.job_env = {"TOOLS_SHA": "${{ github.workflow_sha }}"}
        self.helper.validator = "true"
        self.helper.checkout = ("        if: github.event_name == 'pull_request'\n"
                                "          ref: ${{ env.TOOLS_SHA }}\n")
        self.helper.checker = "          CHECKER: ${{ '../docs-drift-tools/scripts/quality-gates/docs_drift.py' }}\n"

    def test_literal_env_checker_and_ref_are_not_expressions(self):
        self.helper.job_env["STATIC_VALUE"] = "ordinary env scalar"
        self.helper.checker = "          CHECKER: ../docs-drift-tools/scripts/quality-gates/docs_drift.py\n"
        self.helper.checkout = "        if: true\n          ref: literal-ref\n"
        validation, env, pinned, checker, ref = self.helper.run_workflow({}, "fixture/repo")
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertEqual(env["STATIC_VALUE"], "ordinary env scalar")
        self.assertEqual(env["TOOLS_SHA"], "5" * 40)
        self.assertTrue(pinned)
        self.assertEqual(checker, "../docs-drift-tools/scripts/quality-gates/docs_drift.py")
        self.assertEqual(ref, "literal-ref")

    def test_expression_fields_and_bare_conditions_still_evaluate(self):
        self.helper.checker = ("          CHECKER: ${{ github.repository == 'fixture/repo' && "
                               "'../docs-drift-tools/scripts/quality-gates/docs_drift.py' || 'wrong' }}\n")
        for event, want in [("pull_request", True), ("push", False)]:
            with self.subTest(event=event):
                validation, _, pinned, checker, ref = self.helper.run_workflow({}, "fixture/repo", event)
                self.assertEqual(validation.returncode, 0, validation.stderr)
                self.assertIs(pinned, want)
                self.assertEqual(checker, "../docs-drift-tools/scripts/quality-gates/docs_drift.py")
                self.assertEqual(ref, "5" * 40)

    def test_raw_event_json_reaches_shell_as_one_value(self):
        self.helper.job_env["RAW_EVENT"] = "${{ toJSON(github.event) }}"
        self.helper.validator = 'printf \'%s\' "$RAW_EVENT"'
        validation, env, *_ = self.helper.run_workflow({}, "fixture/repo")
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertEqual(validation.stdout, env["RAW_EVENT"])
        self.assertEqual(json.loads(validation.stdout), {
            "before": "3" * 40,
            "pull_request": {"base": {"sha": "1" * 40}, "head": {"sha": "2" * 40}}})

    def test_raw_event_json_preserves_empty_and_nested_untrusted_values(self):
        for event in [{}, {"before": "", "after": None, "deleted": False,
                          "extra": ["quotes'\"\n☃", "${{ inputs.tools-ref }}", "$(id)"]}]:
            with self.subTest(event=event):
                result = self.helper.expression("${{ toJSON(github.event) }}", {"github": {"event": event}})
                self.assertEqual(json.loads(result), event)
        inputs = {"base-sha": "", "tools-ref": "main"}
        self.assertEqual(json.loads(self.helper.expression("${{ toJSON(inputs) }}", {"inputs": inputs})), inputs)

    def test_unsupported_expressions_fail_in_every_evaluated_field(self):
        for expression in ["toJSON(github)", "toJSON(github.event) || '{}'",
                           "github.workflow_sha + 'bad'", "github.workflow_sha ; true",
                           "steps.checkout.outputs.ref"]:
            for field in ("job_env", "checker", "ref", "if"):
                with self.subTest(expression=expression, field=field):
                    value = "${{ " + expression + " }}"
                    if field == "job_env":
                        overrides = {"job_env": {"INVALID": value}}
                    elif field == "checker":
                        overrides = {"checker": "          CHECKER: " + value + "\n"}
                    elif field == "ref":
                        overrides = {"checkout": "          ref: " + value + "\n"}
                    else:
                        overrides = {"checkout": "        if: " + expression + "\n"}
                    with patch.multiple(self.helper, **overrides):
                        with self.assertRaisesRegex(AssertionError, "unsupported workflow expression"):
                            self.helper.run_workflow({}, "fixture/repo")


class NativeDocsWorkflow(unittest.TestCase):
    """Execute real workflow run blocks; replace only the network checkout boundary.

    Fixtures deliberately separate caller history from the trusted checker repo.
    A caller can omit/poison its checker and weaken its head policy. Neither may
    change the trusted program or the policy snapshot used for the actual diff.
    The parser accepts this workflow's block-scalar subset, not arbitrary YAML;
    unsupported Actions/expressions fail loudly instead of being silently skipped.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docs-native-workflow-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        self.workflow = (ROOT / ".github/workflows/docs-drift.yml").read_text()
        self.caller = self.root / "caller-source"
        self.tools = self.root / "trusted-source"
        for repo in (self.caller, self.tools):
            repo.mkdir()
            self.git(repo, "init", "-q")
            self.git(repo, "config", "user.name", "Fixture")
            self.git(repo, "config", "user.email", "fixture@example.invalid")
        self.policy = {"version": 1, "rules": [
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]}]}
        self.put(self.caller, ".docs-drift.json", json.dumps(self.policy))
        self.put(self.caller, "src/api.py", "before\n")
        self.put(self.caller, "docs/api.md", "before\n")
        self.base = self.commit(self.caller)
        self.put(self.caller, "src/api.py", "after\n")
        self.head = self.commit(self.caller)
        # Instrument the real checker only to prove provenance and cwd. Its
        # parser, Git calls, policy selection and exit status are unmodified.
        trace = ('import json as _json, os as _os\n'
                 'from pathlib import Path as _Path\n'
                 '_Path(_os.environ["CHECKER_TRACE"]).write_text(_json.dumps({'
                 '"cwd": _os.getcwd(), "file": str(_Path(__file__).resolve())}))\n')
        self.put(self.tools, "scripts/quality-gates/docs_drift.py", trace + SCRIPT.read_text())
        self.tools_sha = self.commit(self.tools)

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], env=self.env,
                                       text=True, stderr=subprocess.PIPE, timeout=10).strip()

    @staticmethod
    def put(repo, name, content):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, repo):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "--allow-empty", "-m",
                 "test: workflow fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git(repo, "rev-parse", "HEAD")

    @staticmethod
    def section(block, name, indent):
        match = re.search(r"^" + " " * indent + re.escape(name) + r":\n"
                          r"((?:" + " " * (indent + 2) + r"[^\n]*\n|\n)*)", block, re.M)
        return DocsWorkflow.fields(match[1], indent + 2) if match else {}

    resolve = DocsWorkflow.resolve

    def run_native(self, event="pull_request", *, fork=False, inputs=None,
                   workflow_sha=None, base=None, head=None, event_patch=None):
        base = self.base if base is None else base
        head = self.head if head is None else head
        inputs = {} if inputs is None else inputs
        workspace = self.root / ("run-" + str(len(list(self.root.glob("run-*")))))
        workspace.mkdir()
        trace = workspace / "checker-trace.json"
        github = {"repository": "TogetherWeOwn/paperclip-ops", "event_name": event,
                  "sha": head, "workflow_sha": self.tools_sha if workflow_sha is None else workflow_sha,
                  "workflow_ref": "TogetherWeOwn/paperclip-ops/.github/workflows/docs-drift.yml@refs/heads/main",
                  "workspace": str(workspace), "event": {"before": base}}
        if event == "pull_request":
            github["event"]["pull_request"] = {
                "base": {"sha": base, "repo": {"full_name": github["repository"]}},
                "head": {"sha": head, "repo": {"full_name": "fixture/fork" if fork else github["repository"]}}}
        elif event == "merge_group":
            github["event"]["merge_group"] = {"base_sha": base, "head_sha": head}
        if event_patch:
            event_patch(github)
        contexts = {"inputs": inputs, "github": github, "env": {}, "steps": {}}
        env = {**self.env, "GITHUB_WORKSPACE": str(workspace),
               "GITHUB_SHA": str(github["sha"]), "GITHUB_EVENT_NAME": event,
               "GITHUB_REPOSITORY": github["repository"], "CHECKER_TRACE": str(trace),
               "GITHUB_ENV": str(workspace / "github-env"),
               "GITHUB_OUTPUT": str(workspace / "github-output")}
        for name, value in self.section(self.workflow, "env", 4).items():
            contexts["env"][name] = self.resolve(value, contexts)
        env.update(contexts["env"])
        checkouts, stdout, stderr = [], "", ""
        code = 0
        caller_checkout = None
        steps = re.findall(r"^      - name: [^\n]+\n(.*?)(?=^      - |\Z)",
                           self.workflow, re.M | re.S)
        self.assertTrue(steps, "no workflow steps parsed")
        for block in steps:
            fields = DocsWorkflow.fields(block, 8)
            if "if" in fields:
                condition = DocsWorkflow.expression(self, fields["if"], contexts)
                if not condition:
                    continue
            if "uses" in fields:
                self.assertRegex(fields["uses"], r"^actions/checkout@[0-9a-f]{40}(?:\s|$)")
                options = {key: self.resolve(value, contexts)
                           for key, value in self.section(block, "with", 8).items()}
                repository = options.get("repository", github["repository"])
                ref = options.get("ref", github["sha"])
                checkouts.append({**options, "repository": repository, "ref": ref})
                # Only known repository+commit pairs are available offline.
                # A mutable/foreign ref is NOT rescued with the trusted fixture.
                source = None
                if repository == "TogetherWeOwn/paperclip-ops" and ref == self.tools_sha:
                    source = self.tools
                elif repository in (github["repository"], "fixture/fork") and ref in (self.base, self.head):
                    source = self.caller
                if source is None:
                    code, stderr = 128, stderr + "fixture checkout: unknown repository or commit\n"
                    break
                destination = workspace / options.get("path", ".")
                self.assertTrue(destination.resolve().is_relative_to(workspace.resolve()))
                self.git(workspace, "clone", "-q", "--no-hardlinks", str(source), str(destination))
                self.git(destination, "checkout", "-q", "--detach", ref)
                if source == self.caller:
                    caller_checkout = destination
                continue
            self.assertEqual(fields.get("shell", "bash"), "bash", "unsupported workflow shell")
            match = re.search(r"^        run: \|\n(.*)", block, re.M | re.S)
            self.assertIn("run", fields, "unsupported workflow step (must execute, never ignore)")
            command = textwrap.dedent(match[1]) if match else fields["run"]
            step_env = {name: self.resolve(value, contexts)
                        for name, value in self.section(block, "env", 8).items()}
            cwd = workspace / self.resolve(fields.get("working-directory", "."), contexts)
            result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                                     command], cwd=cwd, env={**env, **step_env},
                                    capture_output=True, text=True, timeout=10)
            stdout, stderr = stdout + result.stdout, stderr + result.stderr
            code = result.returncode
            if code:
                break
            exported = Path(env["GITHUB_ENV"])
            if exported.exists():
                for line in exported.read_text().splitlines():
                    name, value = line.split("=", 1)
                    env[name] = contexts["env"][name] = value
                exported.write_text("")
        if caller_checkout:
            self.assertEqual(self.git(caller_checkout, "status", "--porcelain"), "",
                             "documentation gate mutated caller files")
            self.assertEqual(self.git(caller_checkout, "rev-parse", "HEAD"), head)
        return {"code": code, "stdout": stdout, "stderr": stderr, "checkouts": checkouts,
                "trace": json.loads(trace.read_text()) if trace.exists() else None,
                "caller": caller_checkout, "workspace": workspace}

    def assert_report(self, result, code, status):
        self.assertEqual(result["code"], code, result["stdout"] + result["stderr"])
        self.assertIsNotNone(result["trace"], "trusted checker did not execute (absence must not skip/pass)")
        report = json.loads(result["stdout"])
        self.assertEqual(report["status"], status)
        self.assertEqual((report["base_sha"], report["head_sha"], report["policy_sha"]),
                         (self.base, self.head, self.base))
        self.assertEqual(Path(result["trace"]["cwd"]), result["caller"])
        self.assertFalse(Path(result["trace"]["file"]).is_relative_to(result["caller"]),
                         "checker came from caller-controlled history")
        caller_checkout, = [item for item in result["checkouts"] if item["ref"] == self.head]
        self.assertEqual(caller_checkout.get("fetch-depth"), "0", "caller diff requires full history")
        for checkout in result["checkouts"]:
            self.assertEqual(checkout.get("persist-credentials"), "false")
        return report

    def test_native_heads_without_checker_measure_real_diff_for_every_event(self):
        # Removing the native tools checkout or falling back to HEAD must fail.
        for event, fork in [("pull_request", False), ("pull_request", True),
                            ("push", False), ("merge_group", False)]:
            for change, want, status in [("undocumented", 1, "finding"),
                                         ("covered", 0, "clean"), ("unrelated", 0, "clean")]:
                with self.subTest(event=event, fork=fork, change=change):
                    self.git(self.caller, "reset", "--hard", self.base)
                    if change == "unrelated":
                        self.put(self.caller, "unrelated.txt", "new\n")
                    else:
                        self.put(self.caller, "src/api.py", "after\n")
                    if change == "covered":
                        self.put(self.caller, "docs/api.md", "updated\n")
                    self.head = self.commit(self.caller)
                    report = self.assert_report(self.run_native(event, fork=fork), want, status)
                    self.assertEqual(report["checked_rules"], 0 if change == "unrelated" else 1)
                    if want:
                        self.assertEqual(report["findings"][0]["paths"], ["src/api.py"])

    def test_empty_native_diff_still_executes_trusted_checker(self):
        self.head = self.base
        report = self.assert_report(self.run_native(), 0, "clean")
        self.assertEqual(report["changed_paths"], 0)

    def test_native_head_checker_cannot_replace_trusted_program(self):
        self.put(self.caller, "scripts/quality-gates/docs_drift.py", 'print("untrusted checker ran")\n')
        self.head = self.commit(self.caller)
        result = self.run_native(fork=True)
        self.assertNotIn("untrusted checker ran", result["stdout"])
        self.assert_report(result, 1, "finding")

    def test_missing_trusted_checker_fails_even_when_caller_has_one(self):
        self.git(self.tools, "rm", "scripts/quality-gates/docs_drift.py")
        self.tools_sha = self.commit(self.tools)
        for caller_has_checker in (False, True):
            with self.subTest(caller_has_checker=caller_has_checker):
                if caller_has_checker:
                    self.put(self.caller, "scripts/quality-gates/docs_drift.py", 'print("caller fallback ran")\n')
                    self.head = self.commit(self.caller)
                result = self.run_native()
                self.assertNotEqual(result["code"], 0, "missing trusted tool was silently skipped or replaced")
                self.assertIsNone(result["trace"])
                self.assertNotIn("caller fallback ran", result["stdout"])

    def test_native_head_policy_cannot_disable_target_base_mapping(self):
        for policy in [None, {"version": 1, "rules": []}]:
            with self.subTest(policy=policy):
                self.git(self.caller, "reset", "--hard", self.head)
                if policy is None:
                    self.git(self.caller, "rm", ".docs-drift.json")
                else:
                    self.put(self.caller, ".docs-drift.json", json.dumps(policy))
                self.head = self.commit(self.caller)
                self.assert_report(self.run_native("merge_group"), 1, "finding")

    def test_diverged_target_policy_and_caller_history_are_preserved(self):
        merge_base = self.base
        self.git(self.caller, "checkout", "-q", "--detach", self.base)
        self.policy["rules"][0]["docs"] = ["manual/*"]
        self.put(self.caller, ".docs-drift.json", json.dumps(self.policy))
        self.base = self.commit(self.caller)
        report = self.assert_report(self.run_native("pull_request", fork=True), 1, "finding")
        self.assertEqual(report["merge_base_sha"], merge_base)
        self.assertEqual(report["findings"][0]["docs"], ["manual/*"])

    def test_native_bad_or_empty_workflow_provenance_fails_before_checkout(self):
        for sha in ["", "main", "refs/heads/main", "A" * 40, "a" * 39, "a" * 41, "$(id)"]:
            with self.subTest(sha=sha):
                result = self.run_native(workflow_sha=sha)
                self.assertNotEqual(result["code"], 0)
                self.assertEqual(result["checkouts"], [], "invalid provenance reached checkout")

    def test_native_bad_raw_event_shas_fail_before_checkout(self):
        for event in ("pull_request", "push", "merge_group"):
            for field in ("base", "head"):
                for value in ("", "main", "A" * 40, "a" * 39):
                    with self.subTest(event=event, field=field, value=value):
                        def poison(github):
                            if event == "pull_request":
                                github["event"][event][field]["sha"] = value
                            elif event == "merge_group":
                                github["event"][event][field + "_sha"] = value
                            elif field == "base":
                                github["event"]["before"] = value
                            else:
                                github["sha"] = value
                        result = self.run_native(event, event_patch=poison)
                        self.assertNotEqual(result["code"], 0)
                        self.assertEqual(result["checkouts"], [], "invalid raw event SHA reached checkout")

    def test_reusable_invalid_raw_inputs_never_reach_checkout(self):
        for field in ("base-sha", "head-sha", "tools-ref"):
            for value in (None, "", "main", "A" * 40, "a" * 39):
                with self.subTest(field=field, value=value):
                    inputs = {"base-sha": self.base, "head-sha": self.head, "tools-ref": self.tools_sha}
                    if value is None:
                        inputs.pop(field)
                    else:
                        inputs[field] = value
                    result = self.run_native(inputs=inputs)
                    self.assertNotEqual(result["code"], 0)
                    self.assertEqual(result["checkouts"], [], "raw call input fell back to native evidence")

    def test_reusable_pinned_checker_remains_trusted_in_consumer_repository(self):
        inputs = {"base-sha": self.base, "head-sha": self.head, "tools-ref": self.tools_sha}
        result = self.run_native(inputs=inputs, workflow_sha="f" * 40,
                                 event_patch=lambda github: github.update(repository="fixture/consumer"))
        report = self.assert_report(result, 1, "finding")
        self.assertEqual(report["checked_rules"], 1)
        tool_checkout, = [item for item in result["checkouts"] if item["ref"] == self.tools_sha]
        self.assertEqual(tool_checkout["repository"], "TogetherWeOwn/paperclip-ops")

    def test_workflow_stays_unprivileged_and_read_only(self):
        self.assertNotRegex(self.workflow, r"(?m)^  (?:pull_request_target|workflow_run):")
        self.assertEqual(self.section(self.workflow, "permissions", 0), {"contents": "read"})
        self.assertEqual(self.section(self.workflow, "permissions", 4), {"contents": "read"})
        self.assertNotRegex(self.workflow, r"(?m)^\s+(?:secrets|secrets-inherit):")
        self.put(self.caller, "package.json", json.dumps({"scripts": {
            "docs": "touch generator-ran", "postinstall": "touch generator-ran"}}))
        self.head = self.commit(self.caller)
        result = self.run_native()
        self.assert_report(result, 1, "finding")
        self.assertFalse((result["caller"] / "generator-ran").exists())


if __name__ == "__main__":
    unittest.main()
