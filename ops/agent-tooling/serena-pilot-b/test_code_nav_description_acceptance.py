#!/usr/bin/env python3
"""Immutable acceptance contract for offline code-nav discovery.

Run with stdlib only:
  python3 -B -m unittest discover -s ops/agent-tooling/serena-pilot-b \
      -p 'test_code_nav_description_acceptance.py'

The builder must not edit this file. Hand off a pushed RED branch, not a
standalone test-only PR; the builder bundles these tests unchanged with the
implementation and real CI/inventory/generated-contract wiring in one green PR.
Discovery is exactly --describe --json
(in either order), with no execution operands or --timeout override. --describe
without --json and --json without --describe are invalid, not text modes.
Invalid combinations exit 2, write no stdout, and emit one JSON object on stderr:
  {"code": "invalid_discovery_arguments", "retryable": false,
   "next_action": "use code-nav --describe --json without execution arguments"}
No raw operands or environment values may leak into that response.

The public wire contract below pins command IDs, CLI spellings, argument types,
requiredness, defaults and tool argument names. Required operands have null
(no parser default). Metadata describes capabilities, NOT installed readiness.
Additional descriptive fields are allowed; these pinned fields must correspond
mechanically to the real parser and real dispatcher, not an independent catalog.
The timeout covers lock queue plus call, in seconds; timeout exit remains 124.
External-state categories describe execution only, never discovery side effects.

All child invocations have a five-second wall bound and disposable, allowlisted
environments. No installed wrapper is invoked: its upgrade/canary is separately
owned. The source CLI is the offline contract, not installation evidence.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "code_nav.py"
ERROR = {
    "code": "invalid_discovery_arguments",
    "retryable": False,
    "next_action": "use code-nav --describe --json without execution arguments",
}
# Each row is (CLI name, JSON type, required, default, tool argument).
COMMANDS = {
    "outline": ("get_symbols_overview", [
        ("file", "string", True, None, "relative_path"),
        ("--depth", "integer", False, 1, "depth"),
    ]),
    "symbol": ("find_symbol", [
        ("name_path", "string", True, None, "name_path_pattern"),
        ("--body", "boolean", False, False, "include_body"),
        ("--in", "string", False, "", "relative_path"),
        ("--depth", "integer", False, 0, "depth"),
        ("--info", "boolean", False, False, "include_info"),
    ]),
    "refs": ("find_referencing_symbols", [
        ("name_path", "string", True, None, "name_path"),
        ("file", "string", True, None, "relative_path"),
    ]),
}
TIMEOUT = {
    "argument": "--timeout", "default": 120, "unit": "seconds",
    "scope": "queue-plus-call", "exit_code": 124,
}
RUNTIME = {
    "dependencies": ["git", "serena-agent==2.0.0", "PyYAML"],
    "side_effects": [
        "repo-lock", "external-config", "external-project-state",
        "language-server-cache", "language-server-download", "serena-server-process",
    ],
    "writes_repository": False,
    "read_only_tools": True,
}
DISCOVERY = {"requires_runtime": False, "side_effects": [], "runtime_ready": None}
INVALID = (
    ["--describe"],
    ["--json"],
    ["--describe", "--json", "outline", "private-operand.ts"],
    ["symbol", "private-operand", "--describe", "--json"],
    ["--describe", "--json", "refs", "private-operand", "private-file.ts"],
    ["--timeout", "9", "--describe", "--json"],
    ["--describe", "--json", "--timeout", "0"],
    ["--describe", "--json", "--unknown-private-option"],
    ["--describe", "--json", "--help"],
)

# Used by both literal `python -B code_nav.py` (sitecustomize) and isolated
# `python -I -S` entry-point tests. The audit hook blocks before an effect,
# even if an implementation catches the resulting exception. Profile guards
# also catch prerequisite probes that do not reach an audited OS operation.
GUARDS = textwrap.dedent(r'''
    import builtins
    import os
    import sys

    forbidden = []
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            forbidden.append("import:yaml")
            raise ImportError("acceptance guard: runtime-only yaml import")
        return original_import(name, *args, **kwargs)
    builtins.__import__ = guarded_import

    def audit(event, args):
        blocked = (event.startswith(("subprocess.", "socket.", "os.exec", "os.spawn"))
                   or event in {"os.system", "os.fork", "os.posix_spawn", "pty.spawn",
                                "os.mkdir", "os.remove", "os.rename", "os.rmdir",
                                "os.link", "os.symlink", "os.chmod", "os.chown",
                                "os.utime", "os.truncate", "os.putenv", "os.unsetenv",
                                "fcntl.flock", "fcntl.lockf"})
        if event == "open":
            _, mode, flags = args
            blocked = ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                       or (isinstance(flags, int) and bool(flags &
                           (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))))
        if blocked:
            forbidden.append(event)
            raise AssertionError("acceptance guard: " + event)
    sys.addaudithook(audit)

    runtime_functions = {
        "_repo_root", "_opt_paths", "_ensure_home", "_ensure_global_config",
        "_load_config", "_child_env", "_run_once",
    }
    def profile(frame, event, arg):
        is_server = (frame.f_code.co_name == "__init__"
                     and type(frame.f_locals.get("self")).__name__ == "_MCP")
        if (event == "call" and frame.f_code.co_filename == os.environ["NAV_SOURCE"]
                and (frame.f_code.co_name in runtime_functions or is_server)):
            name = frame.f_code.co_name
            forbidden.append(name)
            raise AssertionError("acceptance guard: " + name)
    sys.setprofile(profile)
''')

RUN_MAIN = textwrap.dedent(r'''
    import contextlib
    import io
    import json
    import runpy
    import sys

    source, guard, arguments, allow_yaml = sys.argv[1:]
    if allow_yaml == "yes":
        # Import-only stand-in, used only to expose missing CLI behavior even
        # while the baseline still unconditionally imports YAML. Runtime use
        # remains forbidden. Cold discovery tests never enable this control.
        import types
        sys.modules["yaml"] = types.ModuleType("yaml")
    exec(guard)
    if allow_yaml == "yes":
        builtins.__import__ = original_import
    out, err = io.StringIO(), io.StringIO()
    code, exception = 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            module = runpy.run_path(source, run_name="offline_acceptance_target")
            result = module["main"](json.loads(arguments))
            code = result if isinstance(result, int) else 0
        except SystemExit as exc:
            code = exc.code
        except Exception as exc:
            code, exception = 99, type(exc).__name__ + ": " + str(exc)
    sys.setprofile(None)
    print(json.dumps({"code": code, "stdout": out.getvalue(), "stderr": err.getvalue(),
                      "forbidden": forbidden, "exception": exception}))
''')


class OfflineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cwd = self.base / "not-a-checkout"
        self.cwd.mkdir()
        self.poison = self.base / "poison"
        self.poison.mkdir()
        (self.poison / "sitecustomize.py").write_text(GUARDS)
        (self.poison / "yaml.py").write_text('raise ImportError("runtime-only yaml is unavailable")\n')
        # Never inherit Git config, credential helpers, proxies, Python startup,
        # BASH_ENV, HOME, or any other runtime/secret-bearing caller setting.
        self.env = {
            "PATH": str(self.poison), "HOME": str(self.base / "missing-home"),
            "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "PYTHONPATH": str(self.poison),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": str(self.base),
            "NAV_SOURCE": str(SOURCE),
        }
        bash_env = self.poison / "bash-env"
        bash_env.write_text('printf "unexpected BASH_ENV execution\\n" >&2; exit 91\n')
        self.env["BASH_ENV"] = str(bash_env)
        for name in ("git", "serena", "uv", "pip", "pip3", "curl", "wget", "code-nav"):
            target = self.poison / name
            target.write_text('#!/bin/sh\nprintf "unexpected prerequisite/executor\\n" >&2\nexit 92\n')
            target.chmod(0o700)
        self.before = self.snapshot()

    def snapshot(self):
        return {str(p.relative_to(self.base)): ("directory" if p.is_dir() else p.read_bytes())
                for p in self.base.rglob("*")}

    def invoke(self, argv, isolated=False, runtime_env=False, allow_yaml=False):
        env = dict(self.env)
        if runtime_env:
            env.update({
                "SERENA_HOME": str(self.base / "missing-state"),
                "CODE_NAV_LOCK_DIR": str(self.base / "missing-locks"),
                "SERENA_OPT_DIR": str(self.base / "missing-install"),
                "SERENA_VENV": str(self.base / "missing-venv"),
                "SERENA_CONTEXT": str(self.base / "missing-context.yml"),
            })
        command = ([sys.executable, "-I", "-S", "-B", "-c", RUN_MAIN,
                    str(SOURCE), GUARDS, json.dumps(argv), "yes" if allow_yaml else "no"] if isolated else
                   [sys.executable, "-B", str(SOURCE), *argv])
        try:
            process = subprocess.run(command, cwd=self.cwd, env=env,
                                     capture_output=True, text=True, timeout=5)
        finally:
            self.assertEqual(self.snapshot(), self.before, "discovery changed disposable state")
        if not isolated:
            return process
        self.assertEqual(process.returncode, 0, process.stderr)
        receipt = json.loads(process.stdout)
        self.assertIsNone(receipt["exception"], receipt)
        self.assertEqual(receipt["forbidden"], [], receipt)
        return subprocess.CompletedProcess(command, receipt["code"],
                                           receipt["stdout"], receipt["stderr"])

    def description(self, **kwargs):
        result = self.invoke(["--describe", "--json"], **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        try:
            description = json.loads(result.stdout)
        except ValueError:
            self.fail("expected exactly one JSON description, got " + repr(result.stdout))
        self.assertIsInstance(description, dict)
        return description


class DescriptionAcceptance(OfflineFixture):
    def test_source_cli_discovers_without_yaml_git_install_or_state(self):
        self.description()

    def test_isolated_entrypoint_never_calls_runtime_or_probes_prerequisites(self):
        self.description(isolated=True, runtime_env=True)

    def test_wire_schema_has_only_the_three_existing_commands_and_exact_arguments(self):
        document = self.description(isolated=True, allow_yaml=True)
        self.assertEqual(document["schema"], "code-nav-description.v1")
        commands = document["commands"]
        self.assertIsInstance(commands, list)
        self.assertEqual([c["id"] for c in commands], ["outline", "symbol", "refs"])
        for command in commands:
            with self.subTest(command=command["id"]):
                tool, rows = COMMANDS[command["id"]]
                self.assertEqual(command["tool"], tool)
                self.assertEqual([
                    (a["name"], a["type"], a["required"], a["default"], a["tool_argument"])
                    for a in command["arguments"]
                ], rows)
                for argument, row in zip(command["arguments"], rows):
                    self.assertIs(type(argument["required"]), bool)
                    self.assertIs(type(argument["default"]), type(row[3]))

    def test_description_reports_queue_plus_call_timeout_and_execution_only_effects(self):
        document = self.description(isolated=True, allow_yaml=True)
        self.assertEqual(document["timeout"], TIMEOUT)
        self.assertEqual(document["runtime"], RUNTIME)
        self.assertEqual(document["discovery"], DISCOVERY)

    def test_bytes_are_deterministic_across_order_cwd_and_runtime_environment(self):
        first = self.invoke(["--describe", "--json"])
        self.assertEqual(first.returncode, 0, first.stderr)
        for flags in (["--describe", "--json"], ["--json", "--describe"]):
            with self.subTest(flags=flags):
                second = self.invoke(flags, runtime_env=True)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stdout, first.stdout)
                self.assertEqual(second.stderr, "")
        nested = self.cwd / "nested"
        nested.mkdir()
        self.cwd = nested
        self.before = self.snapshot()
        third = self.invoke(["--describe", "--json"])
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(third.stdout, first.stdout)

    def test_invalid_discovery_combinations_fail_safely_before_runtime(self):
        for argv in INVALID:
            with self.subTest(argv=argv):
                result = self.invoke(argv, runtime_env=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                try:
                    refusal = json.loads(result.stderr)
                except ValueError:
                    self.fail("expected stable JSON refusal, got " + repr(result.stderr))
                self.assertEqual(refusal, ERROR)

    def test_invalid_combinations_take_the_same_dependency_free_entrypoint(self):
        for argv in INVALID:
            with self.subTest(argv=argv):
                result = self.invoke(argv, isolated=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr), ERROR)

    def test_help_is_also_dependency_free_and_explains_discovery_json_boundary(self):
        result = self.invoke(["--help"], isolated=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("--describe", result.stdout)
        self.assertIn("--json", result.stdout)
        for command in COMMANDS:
            self.assertIn(command, result.stdout)


class ParserCorrespondenceAcceptance(OfflineFixture):
    """Dispatch controls run with an import-only YAML stand-in, not a server.

    YAML is irrelevant to parsing/dispatch. The separate cold-process tests
    above refuse the import entirely; this stand-in cannot make them pass.
    """
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("code_nav_acceptance_target", SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"yaml": types.ModuleType("yaml")}):
            spec.loader.exec_module(self.module)
        self.repo = self.cwd
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.ts").write_text("export class Thing {}\n")
        self.before = self.snapshot()

    def dispatch(self, argv):
        old_cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.object(self.module, "_repo_root", return_value=str(self.repo)), \
                    mock.patch.object(self.module, "_run_once") as run:
                self.module.main(argv)
            return run
        finally:
            os.chdir(old_cwd)

    def test_existing_parser_defaults_and_tool_mapping_are_preserved(self):
        cases = [
            (["outline", "src/a.ts"], "get_symbols_overview",
             {"relative_path": "src/a.ts", "depth": 1}),
            (["symbol", "Thing"], "find_symbol",
             {"name_path_pattern": "Thing", "depth": 0, "relative_path": "",
              "include_body": False, "include_info": False}),
            (["refs", "Thing", "src/a.ts"], "find_referencing_symbols",
             {"name_path": "Thing", "relative_path": "src/a.ts"}),
            (["--timeout", "7", "outline", "src/a.ts", "--depth", "3"],
             "get_symbols_overview", {"relative_path": "src/a.ts", "depth": 3}),
            (["--timeout", "7", "symbol", "Thing/method", "--in", "src/a.ts",
              "--body", "--info", "--depth", "2"], "find_symbol",
             {"name_path_pattern": "Thing/method", "depth": 2, "relative_path": "src/a.ts",
              "include_body": True, "include_info": True}),
        ]
        for argv, tool, arguments in cases:
            with self.subTest(argv=argv):
                self.dispatch(argv).assert_called_once_with(
                    tool, arguments, 7 if argv[0] == "--timeout" else 120)

    def help_parser(self):
        parser_class = self.module.argparse.ArgumentParser
        original_init = parser_class.__init__
        parsers = []

        def capture_parser(parser, *args, **kwargs):
            original_init(parser, *args, **kwargs)
            parsers.append(parser)

        with mock.patch.object(parser_class, "__init__", capture_parser), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
            self.module.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        return parsers[0]

    def test_actual_help_parser_control_matches_frozen_command_arguments(self):
        root = self.help_parser()
        sub = next(action for action in root._actions
                   if isinstance(action, self.module.argparse._SubParsersAction))
        self.assertEqual(list(sub.choices), list(COMMANDS))
        for name, (_, rows) in COMMANDS.items():
            with self.subTest(command=name):
                actions = [a for a in sub.choices[name]._actions if a.dest != "help"]
                self.assertEqual([(a.option_strings[0] if a.option_strings else a.dest,
                                   a.required, a.default) for a in actions],
                                 [(r[0], r[2], r[3]) for r in rows])

    def test_description_and_help_parser_expose_exactly_the_same_arguments(self):
        document = self.description(isolated=True, allow_yaml=True)
        root = self.help_parser()
        sub = next(action for action in root._actions
                   if isinstance(action, self.module.argparse._SubParsersAction))
        self.assertEqual(list(sub.choices), [c["id"] for c in document["commands"]])
        for command in document["commands"]:
            with self.subTest(command=command["id"]):
                actions = [a for a in sub.choices[command["id"]]._actions
                           if a.dest != "help"]
                actual = [(a.option_strings[0] if a.option_strings else a.dest,
                           a.required, a.default) for a in actions]
                self.assertEqual(actual, [(a["name"], a["required"], a["default"])
                                          for a in command["arguments"]])
                for action, argument in zip(actions, command["arguments"]):
                    actual_type = ("boolean" if isinstance(action, self.module.argparse._StoreTrueAction)
                                   else "integer" if action.type is int else "string")
                    self.assertEqual(actual_type, argument["type"])
        timeout = next(a for a in root._actions if "--timeout" in a.option_strings)
        self.assertEqual(timeout.default, document["timeout"]["default"])
        self.assertIs(timeout.type, int)

    def test_invalid_discovery_refusal_is_not_masked_by_the_yaml_dependency(self):
        for argv in INVALID:
            with self.subTest(argv=argv):
                result = self.invoke(argv, isolated=True, allow_yaml=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                try:
                    refusal = json.loads(result.stderr)
                except ValueError:
                    self.fail("expected stable JSON refusal, got " + repr(result.stderr))
                self.assertEqual(refusal, ERROR)

    def test_wire_defaults_correspond_to_actual_parser_dispatch(self):
        # The schema gets its own literal checks above; here it must also agree
        # with the actual execution path. Either a stale catalog or parser drift
        # breaks this test, even if the other half looks internally consistent.
        self.before = self.snapshot()
        document = self.description()
        samples = {"outline": ["outline", "src/a.ts"], "symbol": ["symbol", "Thing"],
                   "refs": ["refs", "Thing", "src/a.ts"]}
        positional = {"file": "src/a.ts", "name_path": "Thing"}
        for command in document["commands"]:
            with self.subTest(command=command["id"]):
                arguments = {a["tool_argument"]: positional[a["name"]] if a["required"]
                             else a["default"] for a in command["arguments"]}
                self.dispatch(samples[command["id"]]).assert_called_once_with(
                    command["tool"], arguments, document["timeout"]["default"])

    def test_required_operands_and_unknown_tools_are_rejected_before_dispatch(self):
        for argv in (["outline"], ["symbol"], ["refs", "Thing"], ["rename", "Thing"]):
            with self.subTest(argv=argv), mock.patch.object(self.module, "_run_once") as run, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                self.module.main(argv)
            self.assertEqual(exc.exception.code, 2)
            run.assert_not_called()

    def test_invalid_paths_and_nonpositive_timeout_still_fail_closed(self):
        outside = self.base / "outside.ts"
        outside.write_text("not in the checkout\n")
        (self.repo / "escape.ts").symlink_to(outside)
        cases = [
            ["outline", "../outside.ts"], ["outline", "escape.ts"],
            ["outline", "missing.ts"], ["symbol", "Thing", "--in", str(outside)],
            ["refs", "Thing", str(outside)],
            ["--timeout", "0", "symbol", "Thing"],
            ["--timeout", "-1", "symbol", "Thing"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as exc:
                self.dispatch(argv)
            self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
