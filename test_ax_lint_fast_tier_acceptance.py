"""Acceptance contract: AX lint runs in the fast tier, advisory only.

Builder contract under test (wire only; the lint tool itself is unchanged):

* A fast-tier suite ``ax-lint`` exists in ``scripts/verify-fast-inventory.json``
  with prerequisites exactly ``["python3"]``, no preparation actions, and the
  pinned warn-mode command: ``python3`` (optional ``-B``) on
  ``templates/org-github/ax-lint/ax_lint.py`` with ``--mode warn`` and, if
  ``--root`` is given at all, ``--root .`` (the repo root). No error mode, no
  ``--json`` rendering, no shell masking (``|| true``, ``;`` ...).
* The same command is an execution line of ``scripts/verify-fast.sh`` below
  the fast-tier marker, so the inventory-to-execution correspondence held by
  ``test_verify_fast_plan_gates.DriftTest`` stays green in both directions
  and in order. ``--plan``/``--list`` (text and JSON) name the suite; the
  parse tier does not.
* Focused execution (the suite command from the inventory, run at the repo
  root) always exits 0 in warn mode: on the current tree it lists every
  advisory warning and ends ``ax-lint: N finding(s) (mode=warn, advisory)``
  with N >= 1, byte-identical across runs. Warnings are never failures.
* Orphan-suite disposition is compliant and the gate stays green: this
  acceptance module is either referenced by a CI workflow run step (the
  natural fit is appending it to the existing ``verify-fast-plan`` suite
  command in the inventory and in ``scripts/verify-fast.sh`` plus the
  ``verify-fast plan suites`` CI step), or a workflow runs the ax-lint
  warn command itself, or this module carries a one-line-reason exemption
  in ``verification/orphan_suites_allowlist.json``.
* The inventory schema stays at version 1 with unique suite ids; the
  default (full) fast-tier selection gains exactly this one suite section
  and the parse tier gains nothing.

Positive controls pin what already holds today (the tool exists and is
advisory, the inventory/plan/list harness works) so a RED run fails only
for the missing wiring, never for a broken harness.

IMMUTABLE: Builder starts from the pushed test branch/commit and never
edits this file. Corrections belong to the Test Author.

Run: python3 -B -m unittest -v test_ax_lint_fast_tier_acceptance
Offline, stdlib only, no network or credentials. Never runs the full
default fast tier (that would recurse once this module is wired into it);
the default selection is proven through the plan/execution correspondence.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERIFY_FAST = ROOT / "scripts" / "verify-fast.sh"
INVENTORY = ROOT / "scripts" / "verify-fast-inventory.json"
AX_LINT = ROOT / "templates" / "org-github" / "ax-lint" / "ax_lint.py"
AX_LINT_REL = "templates/org-github/ax-lint/ax_lint.py"
ORPHAN_GATE = ROOT / "verification" / "orphan_suites.py"
ALLOWLIST = ROOT / "verification" / "orphan_suites_allowlist.json"
THIS_FILE = Path(__file__).resolve().name
THIS_MODULE = Path(__file__).resolve().stem

SUITE_ID = "ax-lint"
FAST_MARKER = 'echo "=== verify-fast: fast unit/contract gates ==="'
FAIL_TAIL = re.compile(r'\s*\|\|\s*fail\s+"[^"]*"\s*$')
SUMMARY = re.compile(r"(?m)^ax-lint: (\d+) finding\(s\) \(mode=warn, advisory\)$")
TIMEOUT = 300

BASH = shutil.which("bash") or "/bin/bash"
PYTHON = shutil.which("python3") or "/usr/bin/python3"


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                         "BASH_ENV")}


def run(args, timeout=TIMEOUT):
    return subprocess.run([str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout, cwd=str(ROOT),
                          env=clean_env())


def load_inventory():
    return json.loads(INVENTORY.read_text())


def command_tokens(command):
    if isinstance(command, list):
        return [str(t) for t in command]
    return shlex.split(str(command))


def normalize(command):
    # Whitespace-only normalization, matching the plan-gates drift check:
    # shell quoting in unrelated suite commands must survive verbatim.
    text = command if isinstance(command, str) else " ".join(command)
    return " ".join(str(text).split())


def find_suites(inventory, suite_id=SUITE_ID):
    return [s for s in inventory.get("suites", []) if s.get("id") == suite_id]


# --- Drift parsing (same technique as the plan-gates suite, self-contained
# so this module stays an independent acceptance oracle). ------------------

def logical_lines(text):
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
    if not line or line[0] in " \t#":
        return False
    if line.startswith(("echo ", "if ", "fi", "fail(")):
        return False
    return FAIL_TAIL.search(line) is not None


def execution_fast_commands(body):
    _head, marker, tail = body.partition(FAST_MARKER)
    assert marker, "fast-tier marker line missing from verify-fast.sh"
    return [" ".join(FAIL_TAIL.sub("", line).split())
            for line in logical_lines(tail) if is_execution_line(line)]


def inventory_commands(inventory, tier):
    return [normalize(s["command"]) for s in inventory.get("suites", [])
            if s.get("tier") == tier]


def assert_pinned_warn_command(case, tokens):
    """Fail unless tokens are exactly the pinned warn-mode invocation."""
    original = " ".join(tokens)
    case.assertTrue(tokens, "ax-lint command must not be empty")
    case.assertEqual(
        tokens[0], "python3",
        f"ax-lint must run through python3 (stdlib only); got: {original!r}")
    rest = tokens[1:]
    if rest[:1] == ["-B"]:
        rest = rest[1:]
    case.assertTrue(rest, f"ax-lint command names no script: {original!r}")
    case.assertEqual(
        rest[0], AX_LINT_REL,
        f"ax-lint must run the repo's own {AX_LINT_REL}; got: {original!r}")
    args = rest[1:]
    mode = None
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--mode" and i + 1 < len(args):
            mode, i = args[i + 1], i + 2
        elif token.startswith("--mode="):
            mode, i = token.split("=", 1)[1], i + 1
        elif token == "--root" and i + 1 < len(args):
            case.assertEqual(
                args[i + 1], ".",
                f"ax-lint must lint the repo root; got: {original!r}")
            i += 2
        elif token.startswith("--root="):
            case.assertEqual(
                token.split("=", 1)[1], ".",
                f"ax-lint must lint the repo root; got: {original!r}")
            i += 1
        else:
            case.fail(
                f"unexpected token {token!r} in pinned ax-lint command "
                f"{original!r}: only --mode warn and optional --root . "
                "are allowed (no --json, no shell masking, no extra flags)")
    case.assertEqual(
        mode, "warn",
        f"ax-lint must be pinned to --mode warn (advisory, never error); "
        f"got mode={mode!r} in: {original!r}")


class PositiveControlTest(unittest.TestCase):
    """Holds today; proves a RED elsewhere is missing wiring, not a harness."""

    @classmethod
    def setUpClass(cls):
        cls.direct = run([PYTHON, str(AX_LINT), "--mode", "warn",
                          "--root", "."])

    def test_ax_lint_tool_help_documents_warn_mode(self):
        proc = run([PYTHON, str(AX_LINT), "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--mode {warn,error}", proc.stdout)
        self.assertIn("warn mode", proc.stdout)

    def test_ax_lint_tool_warn_run_is_advisory_on_current_tree(self):
        self.assertEqual(self.direct.returncode, 0,
                         "the unwired tool already exits 0 in warn mode: "
                         + self.direct.stderr[:400])
        match = SUMMARY.search(self.direct.stdout)
        self.assertIsNotNone(match, self.direct.stdout[-400:])
        self.assertGreaterEqual(
            int(match.group(1)), 1,
            "current tree is known to carry advisory ax-lint findings")

    def test_inventory_schema_is_valid_with_unique_ids(self):
        inventory = load_inventory()
        self.assertEqual(inventory.get("schema_version"), 1,
                         "no new inventory schema version for this wiring")
        catalog = inventory.get("prerequisite_catalog", {})
        self.assertIn("python3", catalog)
        ids = []
        for suite in inventory.get("suites", []):
            with self.subTest(suite=suite.get("id")):
                self.assertIsInstance(suite.get("id"), str)
                self.assertIn(suite.get("tier"), ("parse", "fast"))
                self.assertIsInstance(suite.get("prerequisites"), list)
                for name in suite["prerequisites"]:
                    self.assertIn(name, catalog,
                                  f"unknown prerequisite {name!r}")
                preparation = suite.get("preparation", {})
                self.assertIsInstance(preparation.get("actions"), list)
                self.assertIsInstance(suite.get("command"), (str, list))
            ids.append(suite["id"])
        self.assertEqual(len(ids), len(set(ids)), "suite ids must be unique")

    def test_plan_and_list_harness_names_existing_suites(self):
        listed = run([BASH, str(VERIFY_FAST), "--list"])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("verify-fast-plan", listed.stdout.splitlines())
        planned = run([BASH, str(VERIFY_FAST), "--plan", "--json"])
        self.assertEqual(planned.returncode, 0, planned.stderr)
        parsed = json.loads(planned.stdout)
        self.assertEqual(parsed.get("schema_version"), 1)
        self.assertTrue(find_suites(parsed, "verify-fast-plan"),
                        "--plan --json must describe suites by stable id")


class InventorySuiteTest(unittest.TestCase):
    """The ax-lint inventory entry: name the break = absent/wrong wiring."""

    def setUp(self):
        self.inventory = load_inventory()
        self.matches = find_suites(self.inventory)

    def the_suite(self):
        self.assertEqual(
            len(self.matches), 1,
            f"inventory must contain exactly one {SUITE_ID!r} suite; "
            f"found {len(self.matches)} (ids: "
            f"{[s.get('id') for s in self.inventory.get('suites', [])]})")
        return self.matches[0]

    def test_exactly_one_ax_lint_suite_exists_in_inventory(self):
        self.the_suite()

    def test_ax_lint_is_fast_tier_python3_only_without_preparation(self):
        suite = self.the_suite()
        self.assertEqual(suite.get("tier"), "fast",
                         "ax-lint belongs to the fast tier, not parse")
        self.assertEqual(suite.get("prerequisites"), ["python3"],
                         "ax-lint is stdlib-only: python3 and nothing else")
        self.assertEqual(suite.get("preparation", {}).get("actions"), [],
                         "ax-lint needs no preparation (offline, no writes)")

    def test_ax_lint_command_is_the_pinned_warn_mode_invocation(self):
        assert_pinned_warn_command(self,
                                   command_tokens(self.the_suite()["command"]))

    def test_ax_lint_command_never_uses_error_mode_or_masking(self):
        tokens = command_tokens(self.the_suite()["command"])
        joined = " ".join(tokens)
        self.assertNotIn("error", tokens, joined)
        for masked in ("||", ";", "&&", "--json"):
            self.assertNotIn(masked, tokens,
                             f"{masked!r} must not appear in: {joined!r}")


class PlanAndListTest(unittest.TestCase):
    """--plan/--list are generated from the inventory; ax-lint must appear."""

    @classmethod
    def setUpClass(cls):
        cls.list_text = run([BASH, str(VERIFY_FAST), "--list"])
        cls.list_json = run([BASH, str(VERIFY_FAST), "--list", "--json"])
        cls.plan_json = run([BASH, str(VERIFY_FAST), "--plan", "--json"])
        cls.plan_text = run([BASH, str(VERIFY_FAST), "--plan"])
        cls.parse_plan = run([BASH, str(VERIFY_FAST), "--parse-only",
                              "--plan", "--json"])

    def test_list_names_ax_lint_exactly_once_in_text_and_json(self):
        self.assertEqual(self.list_text.returncode, 0, self.list_text.stderr)
        self.assertEqual(self.list_text.stdout.splitlines().count(SUITE_ID), 1,
                         "--list must name the ax-lint suite exactly once")
        self.assertEqual(self.list_json.returncode, 0, self.list_json.stderr)
        self.assertEqual(json.loads(self.list_json.stdout).count(SUITE_ID), 1,
                         "--list --json must name the ax-lint suite once")

    def test_plan_json_describes_ax_lint_with_the_inventory_command(self):
        self.assertEqual(self.plan_json.returncode, 0, self.plan_json.stderr)
        parsed = json.loads(self.plan_json.stdout)
        matches = find_suites(parsed)
        self.assertEqual(len(matches), 1,
                         "--plan --json must contain exactly one ax-lint "
                         f"suite; found {len(matches)}")
        planned = matches[0]
        inventory_matches = find_suites(load_inventory())
        self.assertEqual(len(inventory_matches), 1,
                         "ax-lint inventory suite is missing")
        self.assertEqual(normalize(planned["command"]),
                         normalize(inventory_matches[0]["command"]))
        self.assertEqual(planned.get("tier"), "fast")
        assert_pinned_warn_command(self, command_tokens(planned["command"]))
        names = [p.get("name") for p in planned.get("prerequisites", [])]
        self.assertEqual(names, ["python3"])
        for prereq in planned["prerequisites"]:
            self.assertIsInstance(prereq.get("ready"), bool)
        self.assertEqual(planned.get("preparation", {}).get("actions"), [])

    def test_plan_text_lists_ax_lint_as_a_fast_warn_suite(self):
        self.assertEqual(self.plan_text.returncode, 0, self.plan_text.stderr)
        self.assertRegex(
            self.plan_text.stdout,
            rf"(?m)^- {SUITE_ID} \[fast\] ready=(yes|no)\b[^\n]*:: "
            rf"[^\n]*ax_lint\.py[^\n]*--mode warn\b",
            "--plan text must list ax-lint as a fast suite running the "
            "pinned warn-mode command")

    def test_parse_only_plan_excludes_ax_lint(self):
        self.assertEqual(self.parse_plan.returncode, 0,
                         self.parse_plan.stderr)
        parsed = json.loads(self.parse_plan.stdout)
        self.assertEqual(find_suites(parsed), [],
                         "ax-lint is fast-tier only; the parse tier gains "
                         "no suite")
        self.assertTrue(all(s.get("tier") == "parse"
                            for s in parsed.get("suites", [])))


class FocusedExecutionTest(unittest.TestCase):
    """Run the pinned suite command directly: advisory means never failed."""

    @classmethod
    def setUpClass(cls):
        matches = find_suites(load_inventory())
        cls.tokens = (command_tokens(matches[0]["command"])
                      if len(matches) == 1 else None)
        cls.runs = ([run(cls.tokens), run(cls.tokens)]
                    if cls.tokens else [])

    def the_runs(self):
        self.assertIsNotNone(
            self.tokens,
            "no focused execution is possible: the ax-lint inventory "
            "suite and its pinned command do not exist yet")
        return self.runs

    def test_focused_execution_exits_zero_despite_findings(self):
        for proc in self.the_runs():
            self.assertEqual(
                proc.returncode, 0,
                "warn mode must exit 0 even when warnings exist: "
                + (proc.stdout[-400:] + proc.stderr[:400]))

    def test_focused_execution_lists_every_warning_then_the_summary(self):
        proc = self.the_runs()[0]
        match = SUMMARY.search(proc.stdout)
        self.assertIsNotNone(
            match, "focused output must end with the ax-lint advisory "
            f"summary; tail was: {proc.stdout[-400:]!r}")
        count = int(match.group(1))
        self.assertGreaterEqual(
            count, 1,
            "current tree carries known advisory findings; warnings must "
            "be listed, not swallowed")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        warnings = [ln for ln in lines if not ln.startswith("ax-lint: ")]
        self.assertEqual(len(warnings), count,
                         "every finding in the summary count must be "
                         "listed as its own warning line")
        self.assertRegex(warnings[0], r"^[^:]+(?::\d+)?: \[[a-z-]+\] .+")

    def test_focused_execution_is_deterministic_and_never_a_failure(self):
        first, second = self.the_runs()
        self.assertEqual(first.stdout, second.stdout,
                         "focused execution must be idempotent/deterministic")
        for proc in (first, second):
            self.assertEqual(proc.stderr, "")
            self.assertNotIn("::error::", proc.stdout)
            self.assertNotIn("Traceback", proc.stdout)
            self.assertNotIn("FAIL", proc.stdout)


class CorrespondenceTest(unittest.TestCase):
    """Inventory and verify-fast.sh execution must agree, both directions."""

    def setUp(self):
        self.inventory = load_inventory()
        self.planned = inventory_commands(self.inventory, "fast")
        self.executed = execution_fast_commands(VERIFY_FAST.read_text())

    def test_ax_lint_command_is_one_execution_line_at_the_same_position(self):
        matches = find_suites(self.inventory)
        self.assertEqual(len(matches), 1,
                         "ax-lint inventory suite is missing")
        pinned = normalize(matches[0]["command"])
        self.assertEqual(self.planned.count(pinned), 1)
        self.assertEqual(
            self.executed.count(pinned), 1,
            "verify-fast.sh must execute the pinned ax-lint command "
            f"exactly once below the fast-tier marker; found "
            f"{self.executed.count(pinned)} for {pinned!r}")
        self.assertEqual(self.planned.index(pinned),
                         self.executed.index(pinned),
                         "inventory order must match execution order")

    def test_inventory_and_execution_fast_commands_match_both_directions(self):
        self.assertEqual(
            sorted(set(self.planned) - set(self.executed)), [],
            "inventory fast commands with no execution line")
        self.assertEqual(
            sorted(set(self.executed) - set(self.planned)), [],
            "execution lines with no inventory entry")
        self.assertEqual(self.planned, self.executed,
                         "ordered fast-tier correspondence must hold")

    def test_plan_gate_drift_suite_stays_green(self):
        proc = run([PYTHON, "-B", "-m", "unittest",
                    "test_verify_fast_plan_gates.ExecutionParserTest",
                    "test_verify_fast_plan_gates.DriftTest"])
        self.assertEqual(proc.returncode, 0,
                         "plan-gate drift checks must stay green:\n"
                         + proc.stdout[-800:] + proc.stderr[-800:])


class DefaultFastTierTest(unittest.TestCase):
    """The default selection gains exactly the additive ax-lint section."""

    def test_fast_execution_section_has_exactly_one_ax_lint_invocation(self):
        executed = execution_fast_commands(VERIFY_FAST.read_text())
        ax_lines = [c for c in executed if "ax_lint.py" in c]
        self.assertEqual(len(ax_lines), 1,
                         "default fast tier must gain exactly one ax-lint "
                         f"execution section; found {ax_lines}")
        assert_pinned_warn_command(self, shlex.split(ax_lines[0]))

    def test_no_parse_tier_suite_runs_ax_lint(self):
        inventory = load_inventory()
        parse_commands = inventory_commands(inventory, "parse")
        self.assertEqual([c for c in parse_commands if "ax_lint.py" in c],
                         [], "ax-lint must not be wired into the parse tier")


class OrphanDispositionTest(unittest.TestCase):
    """Orphan gate green, with CI execution proven or an exemption recorded."""

    def test_orphan_suite_gate_is_green(self):
        proc = run([PYTHON, str(ORPHAN_GATE)])
        self.assertEqual(proc.returncode, 0,
                         "orphan-suite gate must stay green:\n"
                         + proc.stdout[-800:] + proc.stderr[-800:])
        self.assertIn("PASS:", proc.stdout)

    def test_this_suite_is_ci_wired_or_exempt_with_a_one_line_reason(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        workflows += sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
        ci_proven = False
        for workflow in workflows:
            for line in workflow.read_text().splitlines():
                if THIS_MODULE in line and ("unittest" in line
                                            or "python3" in line):
                    ci_proven = True
                if "ax_lint.py" in line and "warn" in line:
                    ci_proven = True
        exempt = False
        if ALLOWLIST.is_file():
            reason = json.loads(ALLOWLIST.read_text()).get(THIS_FILE)
            exempt = (isinstance(reason, str) and bool(reason.strip())
                      and "\n" not in reason)
        self.assertTrue(
            ci_proven or exempt,
            "orphan disposition missing: either wire this acceptance "
            f"module ({THIS_MODULE}) or the ax-lint warn command into a "
            "CI workflow run step, or record a one-line-reason exemption "
            f"for {THIS_FILE} in verification/orphan_suites_allowlist.json")


if __name__ == "__main__":
    unittest.main()
