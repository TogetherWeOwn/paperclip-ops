#!/usr/bin/env python3
"""Fast unit tests for code_nav.py (Pilot B). Hermetic: temp dirs only, no
network, no Serena server. Wired into scripts/verify-fast.sh."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import code_nav


class RootRelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "a.ts"), "w") as f:
            f.write("x")
        self._cwd = os.getcwd()
        os.chdir(os.path.join(self.root, "src"))

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def test_cwd_relative(self):
        self.assertEqual(code_nav._to_root_rel(self.root, "a.ts"), "src/a.ts")

    def test_repo_relative(self):
        self.assertEqual(code_nav._to_root_rel(self.root, "src/a.ts"), "src/a.ts")

    def test_cwd_wins_over_root(self):
        with open(os.path.join(self.root, "a.ts"), "w") as f:
            f.write("root")
        self.assertEqual(code_nav._to_root_rel(self.root, "a.ts"), "src/a.ts")

    def test_absolute(self):
        self.assertEqual(
            code_nav._to_root_rel(self.root, os.path.join(self.root, "src", "a.ts")),
            "src/a.ts",
        )

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "../../etc/hostname")
        self.assertEqual(c.exception.code, 2)

    def test_absolute_escape_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "/etc/hostname")
        self.assertEqual(c.exception.code, 2)

    def test_missing_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "nope.ts")
        self.assertEqual(c.exception.code, 2)


class ServerArgvTest(unittest.TestCase):
    """The spec-critical flags must not silently drift: without
    --agent-interface tools the 3-tool allowlist does not apply (2.0.0
    defaults to REPL), and without --project-from-cwd the server cannot see
    the caller's worktree."""

    def test_required_flags_present(self):
        tail = code_nav.SERENA_ARGV_TAIL
        for flag in ("--project-from-cwd", "--agent-interface", "--mode",
                     "--enable-web-dashboard", "--open-web-dashboard",
                     "--context", "--transport"):
            self.assertIn(flag, tail)

    def test_flag_values(self):
        tail = code_nav.SERENA_ARGV_TAIL
        self.assertEqual(tail[tail.index("--agent-interface") + 1], "tools")
        self.assertEqual(tail[tail.index("--mode") + 1], "no-memories")
        self.assertEqual(tail[tail.index("--enable-web-dashboard") + 1], "false")
        self.assertEqual(tail[tail.index("--open-web-dashboard") + 1], "false")
        self.assertEqual(tail[tail.index("--transport") + 1], "stdio")


HERE = Path(__file__).resolve().parent


class ConfigTemplateTest(unittest.TestCase):
    def test_template_pins_load_bearing_keys(self):
        config = yaml.safe_load((HERE / "serena-config.pilot.yml").read_text())
        self.assertEqual(config["projects"], [])
        self.assertEqual(config["trusted_project_path_patterns"], [])
        self.assertEqual(config["project_serena_folder_location"],
                         "@SERENA_HOME@/projects/$projectFolderName")


class ConfigSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.dest = self.home / "serena_config.yml"
        self.template = HERE / "serena-config.pilot.yml"

    def seed(self):
        code_nav._ensure_global_config(str(self.home), str(self.template))

    def valid_config(self):
        return {"projects": [], "trusted_project_path_patterns": [],
                "project_serena_folder_location": str(self.home / "projects/$projectFolderName")}

    def test_missing_config_rendered_for_effective_home(self):
        self.seed()
        self.assertEqual(yaml.safe_load(self.dest.read_text()), self.valid_config())

    def test_normalized_config_preserved(self):
        config = self.valid_config()
        config.update(projects=["/some/previous/repo"], auth_secret="synthetic-test-value")
        self.dest.write_text(yaml.safe_dump(config))
        before = self.dest.read_bytes()
        self.seed()
        self.assertEqual(self.dest.read_bytes(), before)

    def test_unsafe_or_malformed_config_refused_without_overwrite(self):
        valid = self.valid_config()
        cases = [
            {**valid, "project_serena_folder_location": "$projectDir/.serena"},
            {**valid, "project_serena_folder_location": "/tmp/serena-home/projects/$projectFolderName"},
            {**valid, "trusted_project_path_patterns": ["**"]},
            {k: v for k, v in valid.items() if k != "trusted_project_path_patterns"},
            {**valid, "trusted_project_path_patterns": None},
            {**valid, "projects": None}, None, [], "not a mapping",
        ]
        for config in cases:
            with self.subTest(config=config):
                self.dest.write_text(yaml.safe_dump(config))
                before = self.dest.read_bytes()
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
                    self.seed()
                self.assertEqual(c.exception.code, 2)
                self.assertIn(str(self.dest), err.getvalue())
                self.assertNotIn("synthetic-test-value", err.getvalue())
                self.assertEqual(self.dest.read_bytes(), before)
        self.dest.write_text("projects: [\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
            self.seed()
        self.assertEqual(c.exception.code, 2)

    def test_parse_failure_does_not_echo_config_contents(self):
        # Parser errors can quote source lines (str input) or values; whatever
        # they carry, the secret must not reach stderr or agent logs.
        self.dest.write_text("auth_secret: synthetic-test-value\nprojects: [\n")
        for exc in (yaml.YAMLError("in line 1: auth_secret: synthetic-test-value"),
                    ValueError("synthetic-test-value"),
                    OSError("synthetic-test-value")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(code_nav.yaml, "safe_load", side_effect=exc), \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
                self.seed()
            self.assertEqual(c.exception.code, 2)
            self.assertIn("cannot parse config: %s" % self.dest, err.getvalue())
            self.assertNotIn("synthetic-test-value", err.getvalue())

    def test_non_regular_or_writable_config_file_refused(self):
        real = self.home / "real.yml"
        real.write_text(yaml.safe_dump(self.valid_config()))
        self.dest.symlink_to(real)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
            self.seed()
        self.assertEqual(c.exception.code, 2)
        self.assertIn("unsafe config file", err.getvalue())
        self.dest.unlink()
        self.dest.write_text(yaml.safe_dump(self.valid_config()))
        self.dest.chmod(0o666)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
            self.seed()
        self.assertEqual(c.exception.code, 2)
        self.assertIn("unsafe config file", err.getvalue())


class DispatchTest(RootRelTest):
    def test_exact_tool_and_argument_mapping(self):
        cases = [
            (["outline", "a.ts"], "get_symbols_overview", {"relative_path": "src/a.ts", "depth": 1}),
            (["outline", "a.ts", "--depth", "2"], "get_symbols_overview", {"relative_path": "src/a.ts", "depth": 2}),
            (["symbol", "Thing"], "find_symbol", {"name_path_pattern": "Thing", "depth": 0,
             "relative_path": "", "include_body": False, "include_info": False}),
            (["symbol", "Thing/method", "--in", "a.ts", "--body", "--info", "--depth", "2"],
             "find_symbol", {"name_path_pattern": "Thing/method", "depth": 2,
             "relative_path": "src/a.ts", "include_body": True, "include_info": True}),
            (["refs", "Thing", "a.ts"], "find_referencing_symbols",
             {"name_path": "Thing", "relative_path": "src/a.ts"}),
        ]
        for argv, tool, args in cases:
            with self.subTest(argv=argv), mock.patch.object(code_nav, "_repo_root", return_value=self.root), \
                    mock.patch.object(code_nav, "_run_once") as run:
                code_nav.main(argv)
                run.assert_called_once_with(tool, args, 120)

    def test_invalid_timeout_refused_before_server(self):
        for timeout in ("0", "-1"):
            with self.subTest(timeout=timeout), mock.patch.object(code_nav, "_run_once") as run, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
                code_nav.main(["--timeout", timeout, "symbol", "Thing"])
            self.assertEqual(c.exception.code, 2)
            run.assert_not_called()


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.root.mkdir()
        self.home = base / "home"
        self.lock_dir = base / "locks"
        self.bin = base / "serena"
        self.bin.touch()
        self.opt = {"serena_bin": str(self.bin), "context": str(HERE / "contexts/builder-readonly.yml"),
                    "config_template": str(HERE / "serena-config.pilot.yml")}
        self.addCleanup(signal.signal, signal.SIGALRM, signal.getsignal(signal.SIGALRM))
        for patcher in (mock.patch.dict(os.environ, {"SERENA_HOME": str(self.home),
                                                    "CODE_NAV_LOCK_DIR": str(self.lock_dir)}),
                        mock.patch.object(code_nav, "_repo_root", return_value=str(self.root)),
                        mock.patch.object(code_nav, "_opt_paths", return_value=self.opt)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mcp_class = mock.patch.object(code_nav, "_MCP").start()
        self.addCleanup(mock.patch.stopall)
        self.mcp = self.mcp_class.return_value
        self.mcp.tool.return_value = {"content": [{"type": "text", "text": "outline"}]}

    def run_once(self, timeout=120):
        code_nav._run_once("get_symbols_overview", {"relative_path": "a.ts", "depth": 1}, timeout)

    def test_effective_config_and_external_folder_exist_before_spawn(self):
        local = self.root / ".serena"
        local.mkdir()
        (local / "project.yml").write_text("activation_command: do-not-run\n")
        before = (local / "project.yml").read_bytes()

        def before_spawn(argv, env, cwd):
            config = yaml.safe_load((self.home / "serena_config.yml").read_text())
            self.assertEqual(config["trusted_project_path_patterns"], [])
            self.assertEqual(config["project_serena_folder_location"],
                             str(self.home / "projects/$projectFolderName"))
            self.assertTrue((self.home / "projects" / self.root.name).is_dir())
            self.assertEqual(env["SERENA_HOME"], str(self.home))
            self.assertEqual(env["SERENA_USAGE_REPORTING"], "false")
            self.assertEqual(cwd, str(self.root))
            self.assertEqual(argv[argv.index("--context") + 1], self.opt["context"])
            return self.mcp

        self.mcp_class.side_effect = before_spawn
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.run_once()
        self.assertEqual(out.getvalue(), "outline\n")
        self.mcp.tool.assert_called_once_with("get_symbols_overview", {"relative_path": "a.ts", "depth": 1})
        self.mcp.close.assert_called_once()
        self.assertEqual((local / "project.yml").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in local.iterdir()), ["project.yml"])

    def test_poisoned_existing_config_prevents_spawn(self):
        self.home.mkdir()
        (self.home / "serena_config.yml").write_text("projects: []\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
            self.run_once()
        self.assertEqual(c.exception.code, 2)
        self.mcp_class.assert_not_called()

    def test_external_project_symlink_refused(self):
        (self.home / "projects").mkdir(parents=True)
        (self.home / "projects" / self.root.name).symlink_to(self.root, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
            self.run_once()
        self.assertEqual(c.exception.code, 2)
        self.mcp_class.assert_not_called()

    def test_home_inside_repo_refused(self):
        with mock.patch.dict(os.environ, {"SERENA_HOME": str(self.root / "state")}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
            self.run_once()
        self.assertEqual(c.exception.code, 2)
        self.mcp_class.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_home_in_paperclip_or_with_placeholder_refused(self):
        # $ placeholders would make the pre-created folder differ from the one
        # Serena resolves, reviving the repo .serena fallback.
        for home in ("/paperclip", "/paperclip/state", str(Path(self.tmp.name) / "$projectDir")):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"SERENA_HOME": home}), \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
                self.run_once()
            self.assertEqual(c.exception.code, 2)
            self.assertIn("SERENA_HOME must be outside", err.getvalue())
            self.mcp_class.assert_not_called()

    def test_tool_error_exits_nonzero_and_closes(self):
        self.mcp.tool.return_value = {"isError": True, "content": [{"type": "text", "text": "failed outline"}]}
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as c:
            self.run_once()
        self.assertEqual(c.exception.code, 1)
        self.assertIn("failed outline", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        self.mcp.close.assert_called_once()

    def test_protocol_failure_exits_nonzero_and_closes(self):
        self.mcp.open.side_effect = RuntimeError("MCP server exited during initialize")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
            self.run_once()
        self.assertEqual(c.exception.code, 1)
        self.assertIn("MCP server exited", err.getvalue())
        self.mcp.tool.assert_not_called()
        self.mcp.close.assert_called_once()

    def test_repo_flock_blocks_spawn_until_timeout(self):
        self.lock_dir.mkdir()
        lock = self.lock_dir / (hashlib.sha1(str(self.root).encode()).hexdigest() + ".lock")
        with lock.open("a+b") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            # A second alarm bounds broken mutants too: if code-nav forgets
            # its alarm, the test fails instead of hanging the entire suite.
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AssertionError("missing timeout")))
            signal.alarm(4)
            try:
                with mock.patch.object(code_nav.signal, "alarm", wraps=signal.alarm) as alarm, \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
                    self.run_once(timeout=1)
                self.assertEqual(c.exception.code, 124)
                alarm.assert_any_call(1)
                self.mcp_class.assert_not_called()
            finally:
                signal.alarm(0)

    def test_hung_call_times_out_and_closes_child(self):
        self.mcp.tool.side_effect = lambda *_: signal.pause()
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AssertionError("missing timeout")))
        signal.alarm(4)
        try:
            with mock.patch.object(code_nav.signal, "alarm", wraps=signal.alarm) as alarm, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as c:
                self.run_once(timeout=1)
            self.assertEqual(c.exception.code, 124)
            alarm.assert_any_call(1)
            self.mcp.close.assert_called_once()
            self.assertEqual(signal.alarm(0), 0)
        finally:
            signal.alarm(0)


class MCPClientTest(unittest.TestCase):
    def client(self, messages):
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(
            "\n".join(json.dumps(m) for m in messages) + "\n"))
        with mock.patch.object(code_nav.subprocess, "Popen", return_value=process):
            client = code_nav._MCP(["fake-serena"], {}, cwd="/unused")
        return client, process

    def test_initialize_notification_and_single_tool_call(self):
        result = {"content": [{"type": "text", "text": "body"}]}
        client, process = self.client([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "method": "notifications/message"},
            {"jsonrpc": "2.0", "id": 2, "result": result},
        ])
        client.open()
        self.assertEqual(client.tool("find_symbol", {"name_path_pattern": "Thing"}), result)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual([m["method"] for m in sent],
                         ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(sent[2]["params"],
                         {"name": "find_symbol", "arguments": {"name_path_pattern": "Thing"}})
        client.close()
        self.assertTrue(process.stdin.closed)
        process.wait.assert_called_once_with(timeout=10)

    def test_rpc_error_not_treated_as_success(self):
        client, _ = self.client([{"jsonrpc": "2.0", "id": 1, "error": {"message": "bad request"}}])
        with self.assertRaisesRegex(RuntimeError, "bad request"):
            client.open()
        client.close()

    def test_early_eof_not_treated_as_success(self):
        client, _ = self.client([])
        with self.assertRaisesRegex(RuntimeError, "server exited during initialize"):
            client.open()
        client.close()

    def test_stuck_child_killed_during_close(self):
        client, process = self.client([])
        process.wait.side_effect = [subprocess.TimeoutExpired("fake-serena", 10), 0]
        client.close()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


class InstallAndCIContractTest(unittest.TestCase):
    def test_install_spec_only_change_wakes_core_suite(self):
        classifier = HERE.parents[2] / ".github/scripts/ci_changes.py"
        result = subprocess.run([sys.executable, str(classifier),
                                 "ops/agent-tooling/serena-pilot-b/OPERATOR_SPEC.md"],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertIn("core=true", result.stdout.splitlines())
        self.assertIn("full=false", result.stdout.splitlines())

    def test_ci_really_executes_suite_for_core_changes(self):
        workflow = yaml.safe_load((HERE.parents[2] / ".github/workflows/ci.yml").read_text())
        step = next(s for s in workflow["jobs"]["suites"]["steps"]
                    if s.get("name") == "code-nav read-only and resource guard suite")
        self.assertEqual(step["if"], "needs.changes.outputs.full == 'true' || needs.changes.outputs.core == 'true'")
        self.assertIn('-B -m unittest -v test_code_nav', step["run"])
        self.assertIn('"$STATIC_LABEL_PYTHON"', step["run"])
        self.assertEqual(step["env"]["PYTHONPATH"], "${{ runner.temp }}/static-label-guard-deps")
        self.assertNotIn("continue-on-error", step)
        contract = json.loads((HERE.parents[2] / "vendor/offline-ci-contract.json").read_text())
        self.assertIn(step["name"] + ":1", contract["baseline_ids"])

    def test_full_lock_is_pinned_and_hash_checked_by_installer(self):
        lock = (HERE / "requirements-serena-2.0.0-lock.txt").read_text()
        blocks = [b for b in lock.split("\n\n") if b and not b.startswith("#")]
        self.assertEqual(len(blocks), 74)
        names = set()
        for block in blocks:
            pin, *hashes = block.splitlines()
            self.assertRegex(pin, r"^[A-Za-z0-9_.-]+==[^ ]+ \\$")
            names.add(pin.split("==")[0].lower().replace("_", "-"))
            self.assertTrue(hashes)
            for line in hashes:
                self.assertRegex(line, r"^    --hash=sha256:[a-f0-9]{64}( \\)?$")
        self.assertTrue({"serena-agent", "httpx", "anyio", "jsonschema", "distro", "sniffio"} <= names)
        spec = (HERE / "OPERATOR_SPEC.md").read_text()
        self.assertIn("pip install --require-hashes --requirement", spec)
        self.assertNotIn("get-pip.py", spec)


class ChildEnvTest(unittest.TestCase):
    def test_telemetry_opt_out_is_lowercase_false(self):
        # Serena's agent.py compares against exactly "false".
        with mock.patch.dict(os.environ, {"HOME": "/root"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["SERENA_USAGE_REPORTING"], "false")

    def test_home_redirected_from_world_writable_paperclip(self):
        with mock.patch.dict(os.environ, {"HOME": "/paperclip"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["HOME"], "/tmp/serena-home")

    def test_home_preserved_elsewhere(self):
        with mock.patch.dict(os.environ, {"HOME": "/root"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["HOME"], "/root")


if __name__ == "__main__":
    unittest.main()
