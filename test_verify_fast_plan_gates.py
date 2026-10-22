"""Strict gates for the verify-fast plan: inventory drift and --no-prepare guards.

Complements test_verify_fast_plan.py (the acceptance contract, left as is) with
two checks its first-token / parse-only coverage cannot make:

* DriftTest: the inventory's fast-tier commands and the execution lines of
  scripts/verify-fast.sh are the same ordered list of commands, compared in
  both directions on the whole whitespace-normalized command, so a deleted
  execution line, a typo in the inventory or a suite missing from the
  inventory turns this red.
* PrepareGuardTest: a successful full-mode --no-prepare run never restores
  toolkit files or installs typescript, even if readiness at probe time and at
  guard time disagree. The guards at the two preparation sites are the only
  thing enforcing that, and every other test exits before reaching them.

Run: python3 -B -m unittest -v test_verify_fast_plan_gates
Offline, stdlib only, no network or credentials.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERIFY_FAST = ROOT / "scripts" / "verify-fast.sh"
PLAN_HELPER = ROOT / "scripts" / "verify-fast-plan.py"
INVENTORY = ROOT / "scripts" / "verify-fast-inventory.json"

FAST_MARKER = 'echo "=== verify-fast: fast unit/contract gates ==="'
FAIL_TAIL = re.compile(r'\s*\|\|\s*fail\s+"[^"]*"\s*$')
TIMEOUT = 60


def scratch_dir():
    return tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))


def logical_lines(text):
    """Script lines with backslash continuations joined; keeps indentation."""
    out, buf = [], ""
    for raw in text.splitlines():
        if raw.endswith("\\"):
            buf += raw[:-1] + " "
            continue
        out.append(buf + raw)
        buf = ""
    if buf:
        out.append(buf)
    return out


def is_execution_line(line):
    """A column-0 suite execution line: `<command> || fail "<message>"`."""
    if not line or line[0] in " \t#":
        return False
    if line.startswith(("echo ", "if ", "fi", "fail(")):
        return False
    return FAIL_TAIL.search(line) is not None


def normalize(text):
    return " ".join(text.split())


def execution_commands(body):
    """Ordered fast-tier commands the script actually executes."""
    head, marker, tail = body.partition(FAST_MARKER)
    assert marker, "fast-tier marker line missing from verify-fast.sh"
    return [
        normalize(FAIL_TAIL.sub("", line))
        for line in logical_lines(tail)
        if is_execution_line(line)
    ]


def inventory_fast_commands():
    suites = json.loads(INVENTORY.read_text())["suites"]
    commands = []
    for suite in suites:
        if suite.get("tier") != "fast":
            continue
        command = suite["command"]
        text = command if isinstance(command, str) else " ".join(command)
        commands.append(normalize(text))
    return commands


class ExecutionParserTest(unittest.TestCase):
    """The drift check must itself notice the divergences it exists to catch."""

    BODY = (
        'x\n' + FAST_MARKER + '\n'
        'node --test a.mjs || fail "a failed"\n'
        '# a comment\n'
        'python3 -m unittest b \\\n'
        '  c || fail "b failed"\n'
        'if (( PREPARE )); then\n'
        '  npm install || fail "install failed"\n'
        'fi\n'
    )

    def test_parses_commands_joins_continuations_ignores_indented_and_comments(self):
        self.assertEqual(
            execution_commands(self.BODY),
            ["node --test a.mjs", "python3 -m unittest b c"],
        )

    def test_deleted_execution_line_changes_the_list(self):
        mutated = self.BODY.replace('node --test a.mjs || fail "a failed"\n', "")
        self.assertNotEqual(execution_commands(mutated), execution_commands(self.BODY))


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.executed = execution_commands(VERIFY_FAST.read_text())
        self.planned = inventory_fast_commands()

    def test_every_fast_suite_has_an_execution_line_and_vice_versa(self):
        executed, planned = set(self.executed), set(self.planned)
        self.assertEqual(
            sorted(planned - executed), [],
            "inventory commands with no matching execution line in verify-fast.sh "
            "(typo or deleted execution line)")
        self.assertEqual(
            sorted(executed - planned), [],
            "execution lines in verify-fast.sh with no inventory entry "
            "(--plan/--list would omit them)")

    def test_inventory_order_matches_execution_order(self):
        self.assertEqual(self.planned, self.executed)

    def test_no_duplicate_fast_commands(self):
        self.assertEqual(len(self.planned), len(set(self.planned)))

    def test_plan_json_emits_the_inventory_commands(self):
        completed = subprocess.run(
            ["python3", str(PLAN_HELPER), "--inventory", str(INVENTORY),
             "--emit-json", "--tier", "fast"],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=str(ROOT))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        emitted = []
        for suite in json.loads(completed.stdout)["suites"]:
            command = suite["command"]
            emitted.append(normalize(
                command if isinstance(command, str) else " ".join(command)))
        self.assertEqual(emitted, self.executed)


# Stubs for the guard test. The probe-time call succeeds (so --no-prepare is
# accepted); every later call fails, i.e. readiness disagrees at guard time and
# only the PREPARE guard keeps the run from restoring or installing.
COUNTING_STUB = """#!/usr/bin/env bash
if [[ {match} ]]; then
  count_file="$STUB_DIR/{name}.count"
  n=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$count_file"
  echo "{name}-probe $n" >> "$STUB_DIR/calls.log"
  (( n == 1 )) && exit 0
  exit 1
fi
exec "{real}" "$@"
"""

NPM_STUB = """#!/usr/bin/env bash
echo "npm $*" >> "$STUB_DIR/calls.log"
exit 99
"""


def write_executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def neutralized_script(body):
    """verify-fast.sh with every suite/gate execution line replaced by `true`.

    Keeps the real flag handling, --no-prepare refusal, readiness probe and the
    two PREPARE guards; removes the suites so the success path stays hermetic.
    """
    out = []
    pending = []
    for raw in body.splitlines():
        pending.append(raw)
        if raw.endswith("\\"):
            continue
        # Same joining as logical_lines, so indentation is judged on line 1.
        logical = (logical_lines("\n".join(pending)) or [""])[0]
        out.append("true" if is_execution_line(logical) else "\n".join(pending))
        pending = []
    return "\n".join(out) + "\n"


class PrepareGuardTest(unittest.TestCase):
    def _run(self, args, probe_ready_once):
        """Run the neutralized script in a scratch repo; return (proc, calls)."""
        with scratch_dir() as tmp:
            tmp = Path(tmp)
            repo = tmp / "repo"
            (repo / "scripts").mkdir(parents=True)
            shutil.copy(PLAN_HELPER, repo / "scripts" / "verify-fast-plan.py")
            shutil.copy(INVENTORY, repo / "scripts" / "verify-fast-inventory.json")
            write_executable(repo / "scripts" / "verify-fast.sh",
                             neutralized_script(VERIFY_FAST.read_text()))
            stubs = tmp / "stubs"
            stubs.mkdir()
            first_ok = "(( n == 1 )) && exit 0\n"
            for name, real, match in (
                ("python3", shutil.which("python3") or "/usr/bin/python3",
                 '"$*" == *offline_ci_contract*--check-restored*'),
                ("node", shutil.which("node") or "/usr/bin/node",
                 '"$*" == *require.resolve*typescript*'),
            ):
                body = COUNTING_STUB.format(name=name, real=real, match=match)
                if not probe_ready_once:
                    body = body.replace(first_ok, "")
                write_executable(stubs / name, body)
            write_executable(stubs / "npm", NPM_STUB)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "BASH_ENV")}
            env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
            env["STUB_DIR"] = str(stubs)
            completed = subprocess.run(
                ["bash", str(repo / "scripts" / "verify-fast.sh"), *args],
                capture_output=True, text=True, timeout=TIMEOUT,
                cwd=str(repo), env=env)
            calls = stubs / "calls.log"
            return completed, (calls.read_text() if calls.exists() else "")

    def test_success_path_never_prepares_when_guard_time_readiness_disagrees(self):
        completed, calls = self._run(["--no-prepare"], probe_ready_once=True)
        combined = completed.stdout + completed.stderr
        # The probe saw both prerequisites ready: --no-prepare was accepted.
        self.assertIn("python3-probe 1", calls, combined)
        self.assertIn("node-probe 1", calls, combined)
        # Neither preparation site ran, and neither guard re-checked readiness.
        self.assertNotIn("restoring stale", combined)
        self.assertNotIn("installing pinned typescript", combined)
        self.assertNotIn("npm ", calls, "--no-prepare must never run npm")
        self.assertNotIn("python3-probe 2", calls,
                         "toolkit guard ran under --no-prepare")
        self.assertNotIn("node-probe 2", calls,
                         "typescript guard ran under --no-prepare")
        self.assertEqual(completed.returncode, 0, combined)

    def test_default_mode_still_prepares_when_readiness_fails(self):
        # Control: without --no-prepare the same stubs reach both sites, so the
        # test above is not passing for lack of reachability.
        completed, calls = self._run([], probe_ready_once=False)
        combined = completed.stdout + completed.stderr
        self.assertIn("restoring stale", combined)
        self.assertIn("installing pinned typescript", combined)
        self.assertIn("npm install", calls)


if __name__ == "__main__":
    unittest.main()
