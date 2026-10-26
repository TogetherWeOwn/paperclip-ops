"""Immutable, offline acceptance for the standalone shell-substitution checker.

Run: python3 -B -m unittest -v test_shell_substitution_json_acceptance

Contract (independent of the implementation):
* check --json selects one UTF-8 JSON object and empty stderr, on exits 0/2/3.
* Exact envelope: schema, outcome, input_kind, finding_count, findings, error.
  schema is shell-substitution.v1; input_kind is command/file/stdin, or null
  when argument selection is invalid. Findings are ordered code-only objects;
  distinct legacy findings remain distinct, identical ones are deduplicated.
* Errors have code, message, retryable=false, next_action. Codes/actions:
  invalid-arguments / correct_arguments; input-unreadable / provide_readable_input;
  invalid-utf8 / provide_utf8_input. Messages are nonempty, generic and private.
* --json with --help exits 0, adds help={usage: str, examples: [str, ...]},
  and otherwise uses the clean envelope with null input_kind. Plain --help
  remains text. Both help forms must show all input channels and a JSON example.
* No source offsets or supplied text appear in the machine envelope. This is
  private checker output, NOT a general redactor or credential boundary.
  Legacy text deliberately retains its existing diagnostic excerpts.

These tests intentionally start RED until the paired implementation lands.
Only this test file belongs to the acceptance author; implementations must not
weaken its assertions. No commands being checked are executed by the tests.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
GUARD = ROOT / "shell_subst_guard.py"
SENTINEL = "INERT_PRIVATE_CHECKER_TEXT_42d9"  # Not a credential.
RULE = "unbounded-yes-substitution"
TIMEOUT = 30


class CheckerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shell-json-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.home = self.work / "home"
        self.home.mkdir()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.marker = self.work / "unexpected-execution"
        self.bash_env = self.work / "poison-bash-env"
        poison = (
            "#!/bin/sh\n"
            f"printf 'unexpected execution\\n' >> {shlex.quote(str(self.marker))}\n"
            "exit 97\n"
        )
        self.bash_env.write_text(poison, encoding="utf-8")
        # A fresh allowlist environment prevents inherited credentials, Git
        # config, Python injection and shell startup state entering the checker.
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home),
            "PATH": str(self.bin),
            "BASH_ENV": str(self.bash_env),
            "ENV": str(self.bash_env),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(self.work),
        }
        for name in (
            "sh", "bash", "yes", "curl", "wget", "git", "ssh", "sudo",
            "env", "node", "npm", "npx", "pnpm", "pip", "pip3", "install",
            "touch", "acceptance-executor",
        ):
            path = self.bin / name
            path.write_text(poison, encoding="utf-8")
            path.chmod(0o755)

    def invoke(self, *args, data=b""):
        result = subprocess.run(
            [str(Path(sys.executable).resolve()), "-B", str(GUARD), *args],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.work, env=self.env, timeout=TIMEOUT, check=False,
        )
        self.assertFalse(self.marker.exists(), "checker spawned a poisoned executor")
        return result

    def channel(self, kind, command, *, machine=True):
        args = ["check"] + (["--json"] if machine else [])
        if kind == "command":
            return self.invoke(*args, "--command", command)
        if kind == "stdin":
            return self.invoke(*args, "--stdin", data=command.encode("utf-8"))
        path = self.work / (SENTINEL + ".sh")
        original = command.encode("utf-8")
        path.write_bytes(original)
        before = path.stat()
        try:
            return self.invoke(*args, "--file", str(path))
        finally:
            self.assertEqual(path.read_bytes(), original, "checker changed input bytes")
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(path.stat().st_mode, before.st_mode)

    def private_json(self, result, *, forbidden=()):
        for token in (SENTINEL, str(self.work), *forbidden):
            for stream in (result.stdout, result.stderr):
                self.assertNotIn(token.encode("utf-8"), stream, "machine output leaked supplied text")
        self.assertEqual(result.stderr, b"", "machine errors belong in the JSON envelope")
        try:
            text = result.stdout.decode("utf-8")
            def no_constant(value):
                raise ValueError("non-finite JSON constant: " + value)
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON key: " + key)
                    value[key] = item
                return value
            value = json.loads(text, parse_constant=no_constant, object_pairs_hook=unique_object)
        except (UnicodeError, ValueError) as error:
            self.fail(f"expected exactly one finite JSON object: {error}; stdout={result.stdout!r}")
        self.assertIsInstance(value, dict)
        # Also inspect decoded strings: JSON escaping must not conceal leakage.
        def strings(item):
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for key, child in item.items():
                    yield key
                    yield from strings(child)
            elif isinstance(item, list):
                for child in item:
                    yield from strings(child)
        for text_value in strings(value):
            for token in (SENTINEL, str(self.work), *forbidden):
                self.assertNotIn(token, text_value, "decoded JSON leaked supplied text")
        return value

    def envelope(self, kind, count=0, *, error=None):
        return {
            "schema": "shell-substitution.v1",
            "outcome": "input_error" if error else ("findings" if count else "clean"),
            "input_kind": kind,
            "finding_count": count,
            "findings": [{"code": RULE} for _ in range(count)],
            "error": error,
        }

    def verdict(self, result, kind, count, *, forbidden=()):
        value = self.private_json(result, forbidden=forbidden)
        self.assertEqual(result.returncode, 3 if count else 0)
        self.assertIs(type(value.get("finding_count")), int)
        self.assertEqual(value, self.envelope(kind, count))
        return value

    def input_error(self, result, kind, code, action, *, forbidden=()):
        value = self.private_json(result, forbidden=forbidden)
        self.assertEqual(result.returncode, 2)
        error = value.get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(set(error), {"code", "message", "retryable", "next_action"})
        self.assertEqual(error["code"], code)
        self.assertIs(error["retryable"], False)
        self.assertEqual(error["next_action"], action)
        self.assertIsInstance(error["message"], str)
        self.assertTrue(error["message"].strip())
        self.assertEqual(value, self.envelope(kind, error=error))
        return value


class StructuredVerdicts(CheckerFixture):
    def test_clean_findings_and_deduplication_on_every_channel(self):
        cases = (
            (f"printf '%s' '{SENTINEL}'", 0),
            (f"x=$(yes {SENTINEL})", 1),
            (f"x=$(yes {SENTINEL}); y=$(yes {SENTINEL})", 1),
            (f"x=$(yes {SENTINEL}_a); y=$(yes {SENTINEL}_b)", 2),
            (f"x=`yes {SENTINEL}`", 1),
            ("", 0),
        )
        for kind in ("command", "file", "stdin"):
            for command, count in cases:
                with self.subTest(kind=kind, count=count, command=command):
                    self.verdict(self.channel(kind, command), kind, count)

    def test_machine_output_is_byte_deterministic(self):
        command = f"x=$(yes {SENTINEL}_b); y=$(yes {SENTINEL}_a)"
        for kind in ("command", "file", "stdin"):
            with self.subTest(kind=kind):
                first = self.channel(kind, command)
                second = self.channel(kind, command)
                self.verdict(first, kind, 2)
                self.verdict(second, kind, 2)
                self.assertEqual(first.stdout, second.stdout)

    def test_scope_and_fail_closed_pins_have_text_json_exit_parity(self):
        cases = (
            ("x=$(yes | head -n 1)", 1),
            ("x=$(yes --help)", 1),
            ("x=$(command -v yes)", 1),
            ("x=${A:-$(yes)}", 1),
            ("x=$(ye\"s\")", 1),
            ("x=$([ -n \"$FLAG\" ] && printf yes || printf no)", 0),
            ("yes | head -n 1", 0),
            ("if yes; then printf ok; fi", 0),
            ("x=$(type yes)", 0),
            ("x=$((yes + 1))", 0),
            ("cat <<'EOF'\n$(yes)\nEOF\n", 0),
            ("# $(yes)\nprintf yes", 0),
        )
        for command, count in cases:
            for kind in ("command", "file", "stdin"):
                with self.subTest(command=command, kind=kind):
                    text = self.channel(kind, command, machine=False)
                    machine = self.channel(kind, command)
                    self.assertEqual(text.returncode, 3 if count else 0)
                    self.verdict(machine, kind, count)
                    self.assertEqual(text.returncode, machine.returncode)

    def test_unusual_valid_text_never_becomes_output(self):
        for payload in (f"{SENTINEL} '\"\\\n\t\x1b[31m", f"{SENTINEL} ☃ λ 🧪"):
            for kind in ("command", "file", "stdin"):
                with self.subTest(kind=kind, payload=payload):
                    self.verdict(self.channel(kind, "# " + payload), kind, 0)

    def test_file_and_stdin_accept_embedded_nul_as_inert_text(self):
        for kind in ("file", "stdin"):
            with self.subTest(kind=kind):
                self.verdict(self.channel(kind, f"# {SENTINEL}\x00\n"), kind, 0)

    def test_json_option_is_valid_after_input_selection(self):
        self.verdict(self.invoke("check", "--command", f"x=$(yes {SENTINEL})", "--json"), "command", 1)


class InputFailures(CheckerFixture):
    def test_missing_file_is_private_and_actionable(self):
        path = str(self.work / (SENTINEL + "-missing"))
        first = self.invoke("check", "--json", "--file", path)
        self.input_error(first, "file", "input-unreadable", "provide_readable_input")
        second = self.invoke("check", "--json", "--file", path)
        self.assertEqual(first.stdout, second.stdout)

    def test_directory_input_is_unreadable_not_a_traceback(self):
        self.input_error(
            self.invoke("check", "--json", "--file", str(self.work)),
            "file", "input-unreadable", "provide_readable_input",
        )

    def test_invalid_utf8_is_private_on_file_and_stdin(self):
        for suffix in (b"\xff\xfe", b"\xc3", b"\xed\xa0\x80"):
            data = SENTINEL.encode() + b" x=$(yes) " + suffix
            path = self.work / (SENTINEL + "-bad-utf8.sh")
            path.write_bytes(data)
            for kind in ("file", "stdin"):
                with self.subTest(kind=kind, suffix=suffix):
                    result = (
                        self.invoke("check", "--json", "--file", str(path))
                        if kind == "file" else self.invoke("check", "--json", "--stdin", data=data)
                    )
                    self.input_error(result, kind, "invalid-utf8", "provide_utf8_input")
                    self.assertEqual(path.read_bytes(), data)

    def test_argument_errors_are_json_without_echoing_argv(self):
        cases = (
            ("check", "--json"),
            ("check", "--json", "--command"),
            ("check", "--json", "--file"),
            ("check", "--json", "--command", SENTINEL, "--stdin"),
            ("check", "--json", "--file", SENTINEL, "--stdin"),
            ("check", "--json", "--command", SENTINEL, "--file", SENTINEL),
            ("check", "--json", "--unknown-" + SENTINEL),
            ("check", "--json", "--stdin", SENTINEL),
            ("check", "--json", "--command", SENTINEL, "extra-" + SENTINEL),
            ("bad-verb-" + SENTINEL, "--json"),
            ("--json",),
        )
        for args in cases:
            with self.subTest(args=args):
                self.input_error(self.invoke(*args), None, "invalid-arguments", "correct_arguments")


class HelpAndReadOnly(CheckerFixture):
    def assert_help(self, text):
        for option in ("check", "--json", "--command", "--file", "--stdin"):
            self.assertIn(option, text)

    def test_json_help_has_safe_structured_usage_and_examples(self):
        for args in (("check", "--json", "--help"), ("--help", "--json")):
            with self.subTest(args=args):
                result = self.invoke(*args)
                value = self.private_json(result)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(set(value), set(self.envelope(None)) | {"help"})
                help_value = value.pop("help")
                self.assertEqual(value, self.envelope(None))
                self.assertEqual(set(help_value), {"usage", "examples"})
                self.assertIsInstance(help_value["usage"], str)
                self.assertIsInstance(help_value["examples"], list)
                self.assertTrue(help_value["examples"])
                for example in help_value["examples"]:
                    self.assertIsInstance(example, str)
                    self.assertTrue(example.strip())
                self.assert_help(help_value["usage"] + "\n" + "\n".join(help_value["examples"]))
                self.assertTrue(any("--json" in example for example in help_value["examples"]))
                self.assertEqual(result.stdout, self.invoke(*args).stdout)

    def test_plain_help_remains_text_and_discovers_json(self):
        result = self.invoke("check", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        self.assert_help(result.stdout.decode("utf-8"))
        self.assertFalse(result.stdout.lstrip().startswith(b"{"))

    def test_supplied_program_is_never_executed_in_either_mode(self):
        canary = self.work / "supplied-program-ran"
        command = (
            f"printf '{SENTINEL}' > {shlex.quote(str(canary))}; "
            "x=$(acceptance-executor); curl https://invalid.example/; "
            "git status; npm install"
        )
        for machine in (False, True):
            for kind in ("command", "file", "stdin"):
                with self.subTest(machine=machine, kind=kind):
                    result = self.channel(kind, command, machine=machine)
                    self.assertFalse(canary.exists(), "supplied shell program executed")
                    self.assertEqual(result.returncode, 0)
                    if machine:
                        self.verdict(result, kind, 0)


class LegacyControls(CheckerFixture):
    def test_default_text_still_includes_legacy_finding_excerpts(self):
        for kind in ("command", "file", "stdin"):
            with self.subTest(kind=kind):
                result = self.channel(kind, f"x=$(yes {SENTINEL})", machine=False)
                self.assertEqual(result.returncode, 3)
                self.assertIn(b"FINDING unbounded-yes-substitution", result.stdout)
                self.assertIn(SENTINEL.encode(), result.stdout)
                self.assertEqual(result.stderr, b"shell_subst_guard: 1 finding(s)\n")

    def test_default_clean_text_and_scope_are_unchanged(self):
        for command in ("", "printf yes", "yes | head -n 1", "x=$(printf yes)"):
            with self.subTest(command=command):
                result = self.channel("command", command, machine=False)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"shell_subst_guard: clean\n")
                self.assertEqual(result.stderr, b"")

    def test_check_command_helper_retains_string_results_and_deduplication(self):
        # Import only the public helper, in a fresh sanitized process: no
        # implementation source/diff is read to derive these expectations.
        code = (
            "import json,runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
            "check=namespace['check_command']; "
            "print(json.dumps([check('x=$(printf yes)'),check('x=$(yes)'),"
            "check('x=$(yes);y=$(yes)'),check('x=$(yes a);y=$(yes b)')]))"
        )
        result = subprocess.run(
            [str(Path(sys.executable).resolve()), "-B", "-c", code, str(GUARD)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.work,
            env=self.env, timeout=TIMEOUT, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        clean, one, duplicate, distinct = json.loads(result.stdout)
        self.assertEqual(clean, [])
        self.assertEqual(len(one), 1)
        self.assertIsInstance(one[0], str)
        self.assertIn(RULE, one[0])
        self.assertEqual(duplicate, one)
        self.assertEqual(len(distinct), 2)
        self.assertTrue(all(isinstance(item, str) for item in distinct))
        self.assertFalse(self.marker.exists())


class OptionSpellingPrivacy(CheckerFixture):
    """Parser-accepted spellings must never fall back to excerpt-bearing text."""

    def test_json_prefixes_select_private_verdicts_or_private_argument_errors(self):
        for option in ("--jso", "--js", "--j"):
            for command, count in ((f"# {SENTINEL}", 0), (f"x=$(yes {SENTINEL})", 1)):
                for args in (
                    ("check", option, "--command", command),
                    ("check", "--command", command, option),
                ):
                    with self.subTest(option=option, count=count, args=args):
                        result = self.invoke(*args)
                        # Preserving argparse prefixes is preferred, but an
                        # explicit, private rejection is also a valid policy.
                        if result.returncode == 2:
                            self.input_error(result, None, "invalid-arguments", "correct_arguments")
                        else:
                            self.verdict(result, "command", count)

    def test_json_prefix_argument_errors_do_not_echo_supplied_text(self):
        for option in ("--jso", "--js", "--j"):
            with self.subTest(option=option):
                self.input_error(
                    self.invoke("check", option, "--unknown-" + SENTINEL),
                    None, "invalid-arguments", "correct_arguments",
                )

    def test_json_with_attached_value_is_a_private_argument_error(self):
        for args in (
            ("check", "--json=" + SENTINEL, "--command", "x"),
            ("check", "--command", "x", "--json=" + SENTINEL),
        ):
            with self.subTest(args=args):
                self.input_error(self.invoke(*args), None, "invalid-arguments", "correct_arguments")

    def test_help_spellings_are_structured_or_privately_rejected(self):
        for option in ("--h", "--he", "--hel", "-h", "-hx"):
            for args in (("check", "--json", option), ("check", option, "--json")):
                with self.subTest(args=args):
                    result = self.invoke(*args)
                    if result.returncode == 2:
                        self.input_error(result, None, "invalid-arguments", "correct_arguments")
                        continue
                    value = self.private_json(result)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(set(value), set(self.envelope(None)) | {"help"})
                    help_value = value.pop("help")
                    self.assertEqual(value, self.envelope(None))
                    self.assertIsInstance(help_value, dict)
                    self.assertEqual(set(help_value), {"usage", "examples"})
                    self.assertIsInstance(help_value["usage"], str)
                    self.assertIsInstance(help_value["examples"], list)
                    self.assertTrue(help_value["examples"])
                    for example in help_value["examples"]:
                        self.assertIsInstance(example, str)
                        self.assertTrue(example.strip())
                    text = help_value["usage"] + "\n" + "\n".join(help_value["examples"])
                    for name in ("check", "--json", "--command", "--file", "--stdin"):
                        self.assertIn(name, text)
                    self.assertTrue(any("--json" in example for example in help_value["examples"]))


class ClosedStandardStreams(CheckerFixture):
    def invoke_closed(self, fd, *args):
        # Close only the child's fd, then exec a fresh Python interpreter so
        # CPython initializes the corresponding sys.stdin/sys.stdout to None.
        # No shell evaluates argv; the supplied command remains inert data.
        launcher = (
            "import os,sys; os.close(int(sys.argv[1])); "
            "os.execv(sys.executable, [sys.executable, '-B', *sys.argv[2:]])"
        )
        result = subprocess.run(
            [str(Path(sys.executable).resolve()), "-B", "-c", launcher, str(fd), str(GUARD), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.work, env=self.env, timeout=TIMEOUT, check=False,
        )
        self.assertFalse(self.marker.exists(), "checker spawned a poisoned executor")
        return result

    def test_closed_stdin_returns_private_unreadable_stdin_error(self):
        self.input_error(
            self.invoke_closed(0, "check", "--json", "--stdin"),
            "stdin", "input-unreadable", "provide_readable_input",
        )

    def test_closed_stdout_preserves_clean_and_finding_exit_codes(self):
        for command, expected in ((f"# {SENTINEL}", 0), (f"x=$(yes {SENTINEL})", 3)):
            with self.subTest(expected=expected):
                result = self.invoke_closed(1, "check", "--json", "--command", command)
                self.assertEqual(result.stderr, b"", "closed stdout must not cause a traceback")
                self.assertEqual(result.stdout, b"", "no envelope can reach a closed stdout")
                self.assertEqual(result.returncode, expected)

    def test_closed_stdout_help_has_no_traceback(self):
        for option, allowed in (("--help", (0,)), ("--hel", (0, 2)), ("-hx", (0, 2))):
            with self.subTest(option=option):
                result = self.invoke_closed(1, "check", "--json", option)
                self.assertEqual(result.stderr, b"", "closed stdout must not cause a traceback")
                self.assertEqual(result.stdout, b"")
                self.assertIn(result.returncode, allowed)

    def test_closed_stdout_argument_errors_remain_private_exit_two(self):
        for args in (
            ("check", "--json", "--command"),
            ("check", "--json", "--unknown-" + SENTINEL),
            ("check", "--json=" + SENTINEL, "--command", "x"),
        ):
            with self.subTest(args=args):
                result = self.invoke_closed(1, *args)
                self.assertEqual(result.stderr, b"", "argument errors must not leak to stderr")
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
