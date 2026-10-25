"""Immutable acceptance contract for read-only selection by inventory suite ID.

Run: python3 -B -m unittest -v test_verify_fast_suite_selection

Builder starts from this test commit and never edits this file. Only the real
CLI and its existing planning helper are copied; inventories are tiny local
fixtures, never an alternate production registry. No implementation diff is
used to derive the expectations.

Binding contract:
* Repeatable --suite ID is legal only with --plan, --dry-run or --list. A
  selection is a set, emitted in inventory order, before prerequisite probes.
* Existing plan objects and JSON list arrays keep their shapes. No selector
  preserves the complete inventory. --parse-only intersects a valid selection;
  an empty intersection is successful and explicit (suites: [], [], or a text
  plan naming zero suites / an empty text list). Unknown IDs still fail.
* Usage refusals exit 2, atomically: no partial plan, probes or preparation.
  With --json anywhere in argv, stdout is one versioned JSON error object with
  error.code (stable nonempty string), error.retryable=false and actionable
  error.next_action (nonempty string). Specific code spelling is not prescribed;
  the same defect has the same code regardless of flag order or output mode.
* A selected missing prerequisite is reported in a successful read-only plan,
  never installed or silently omitted. --no-prepare execution still exits 3
  for missing prerequisites; existing execution exit behavior is not extended.

Breaks caught: ignored/last-only selectors, request-order sorting, duplicate
emission, hardcoded registries, lost commands/readiness, probing before filters,
unknown-ID tier bypass, partial success, parser-order-dependent errors, and
accidental suite/preparation execution or checkout writes in read-only modes.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TIMEOUT = 15
ALL_IDS = ["zeta.check", "alpha.parse", "middle-missing", "poison.fast"]
READY_IDS = ["zeta.check", "alpha.parse"]
READ_ONLY_MODES = ("--plan", "--dry-run", "--list")

# Deliberately not alphabetic or grouped by tier. Expected IDs below are literal,
# independent of any planner/filter helper.
INVENTORY = {
    "schema_version": 1,
    "prerequisite_catalog": {
        "ready-local": {"probe": {"argv": ["suite-probe", "ready", "two words"]}},
        "missing-local": {"probe": {"argv": ["suite-probe", "missing"]}},
        "poison-local": {"probe": {"argv": ["suite-probe", "poison"]}},
    },
    "suites": [
        {"id": "zeta.check", "tier": "fast",
         "prerequisites": ["ready-local"],
         "preparation": {"actions": []},
         "command": ["suite-do-not-run", "zeta", "two words"]},
        {"id": "alpha.parse", "tier": "parse",
         "prerequisites": ["ready-local"],
         "preparation": {"actions": []},
         "command": "suite-do-not-run alpha"},
        {"id": "middle-missing", "tier": "fast",
         "prerequisites": ["missing-local"],
         "preparation": {"actions": ["suite-prepare-do-not-run missing"]},
         "command": ["suite-do-not-run", "missing"]},
        {"id": "poison.fast", "tier": "fast",
         "prerequisites": ["poison-local"],
         "preparation": {"actions": ["suite-prepare-do-not-run poison"]},
         "command": ["suite-do-not-run", "poison"]},
    ],
}

# Stub only process boundaries. The actual CLI, JSON loader, selection and
# readiness evaluator remain real. Log exact argv (including embedded spaces).
STUB = r'''import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
if tool == "python3" and args and Path(args[0]).resolve() == Path(os.environ["PLAN_HELPER"]):
    os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], "-B", *args])
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps({"tool": tool, "argv": args}) + "\n")
if tool == "suite-probe":
    if args == ["ready", "two words"]:
        sys.exit(0)
    if args == ["missing"]:
        sys.exit(17)
    if args == ["poison"]:
        sys.exit(99)
print("forbidden fixture invocation: " + tool, file=sys.stderr)
sys.exit(97)
'''


def snapshot(tree):
    """Detect created/removed files, content writes, chmod and cache cleanup."""
    result = {}
    for path in [tree, *sorted(tree.rglob("*"))]:
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        result[str(path.relative_to(tree))] = (
            info.st_mode, info.st_size, info.st_mtime_ns, digest)
    return result


class SuiteSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        for name in ("verify-fast.sh", "verify-fast-plan.py"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        self.inventory_path = scripts / "verify-fast-inventory.json"
        self.inventory_path.write_text(json.dumps(INVENTORY))
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "calls.jsonl"
        self.log.write_text("")
        self.python = str(Path(sys.executable).resolve())
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash, "fixture requires the existing bash runtime")
        stub_body = "#!" + self.python + "\n" + STUB
        for tool in (
            "python3", "suite-probe", "suite-do-not-run", "suite-prepare-do-not-run",
            "node", "npm", "npx", "pnpm", "curl", "wget", "tar", "git", "gh",
            "ssh", "scp", "rsync", "pip", "pip3", "uv", "bash", "sh", "make",
            "sudo", "docker", "psql", "redis-cli", "sha256sum", "rm", "find",
            "env", "touch", "mkdir", "chmod", "cp", "mv",
        ):
            self.executable(self.bin / tool, stub_body)
        # Only local path resolution and help output need external utilities.
        for tool in ("dirname", "cat", "wc", "tr", "sed"):
            real = shutil.which(tool)
            self.assertIsNotNone(real, "fixture requires local utility " + tool)
            (self.bin / tool).symlink_to(real)
        self.executable(scripts / "sync-toolkit.sh", stub_body)
        for name in ("__pycache__/keep", "omniroute/__pycache__/keep",
                     "node_modules/keep", "vendor/toolkit-files.txt"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("read-only sentinel\n")
        home = self.base / "home"
        home.mkdir()
        temp = self.base / "temp"
        temp.mkdir()
        # An allowlist, not a scrub of arbitrary inherited hooks/credentials.
        self.env = {
            "PATH": str(self.bin), "HOME": str(home), "LC_ALL": "C",
            "TMPDIR": str(temp), "PYTHONDONTWRITEBYTECODE": "1",
            "PLAN_HELPER": str(scripts / "verify-fast-plan.py"),
            "REAL_PYTHON": self.python, "CALL_LOG": str(self.log),
        }

    @staticmethod
    def executable(path, body):
        path.write_text(body)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def invoke(self, *args):
        self.log.write_text("")
        before = snapshot(self.repo)
        home_before = snapshot(Path(self.env["HOME"]))
        temp_before = snapshot(Path(self.env["TMPDIR"]))
        try:
            completed = subprocess.run(
                [self.bash, str(self.repo / "scripts" / "verify-fast.sh"), *args],
                cwd=self.repo, env=self.env, capture_output=True, text=True,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"read-only CLI exceeded {TIMEOUT}s for {args!r}")
        self.assertEqual(snapshot(self.repo), before, "CLI wrote or cleaned the checkout")
        self.assertEqual(snapshot(Path(self.env["HOME"])), home_before, "CLI wrote HOME")
        self.assertEqual(snapshot(Path(self.env["TMPDIR"])), temp_before, "CLI wrote TMPDIR")
        self.calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        forbidden = [call for call in self.calls if call["tool"] != "suite-probe"]
        self.assertEqual(forbidden, [], "suite/preparation/network/credential tool invoked")
        self.assertLess(len(completed.stdout.encode()), 65536, "fixture output is unbounded")
        return completed

    def decoded(self, completed):
        try:
            return json.loads(completed.stdout)
        except (ValueError, TypeError):
            self.fail("expected one JSON value on stdout, got " + repr(completed.stdout[:600])
                      + "; stderr=" + repr(completed.stderr[:600]))

    def success(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "", "successful machine output must not warn")

    def plan_ids(self, completed):
        self.success(completed)
        plan = self.decoded(completed)
        self.assertIsInstance(plan, dict)
        self.assertEqual(plan.get("schema_version"), 1)
        suites = plan.get("suites")
        self.assertIsInstance(suites, list)
        self.assertTrue(all(isinstance(suite, dict) for suite in suites))
        return [suite.get("id") for suite in suites], plan

    def assert_ready_probe_only(self):
        self.assertEqual(self.calls, [
            {"tool": "suite-probe", "argv": ["ready", "two words"]},
        ], "only the selected readiness probe may run (no poison or missing probe)")

    def test_unfiltered_json_plan_control_keeps_inventory_order_and_readiness(self):
        ids, plan = self.plan_ids(self.invoke("--plan", "--json"))
        self.assertEqual(ids, ALL_IDS)
        self.assertEqual(plan["suites"][2]["prerequisites"], [
            {"name": "missing-local", "ready": False},
        ])
        self.assertEqual(self.calls, [
            {"tool": "suite-probe", "argv": ["ready", "two words"]},
            {"tool": "suite-probe", "argv": ["missing"]},
            {"tool": "suite-probe", "argv": ["poison"]},
        ], "control proves the poison probe is reachable without selection")

    def test_unfiltered_modes_and_parse_only_controls_keep_legacy_shapes(self):
        for mode in READ_ONLY_MODES:
            for parse in (False, True):
                with self.subTest(mode=mode, parse=parse):
                    flags = [mode, "--json"] + (["--parse-only"] if parse else [])
                    result = self.invoke(*flags)
                    self.success(result)
                    value = self.decoded(result)
                    ids = value if mode == "--list" else [s["id"] for s in value["suites"]]
                    self.assertEqual(ids, ["alpha.parse"] if parse else ALL_IDS)
                    if parse:
                        self.assert_ready_probe_only()

    def test_single_selection_in_every_machine_read_only_mode(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                result = self.invoke(mode, "--suite", "zeta.check", "--json")
                self.success(result)
                value = self.decoded(result)
                ids = value if mode == "--list" else [s["id"] for s in value["suites"]]
                self.assertEqual(ids, ["zeta.check"])
                self.assert_ready_probe_only()

    def test_repeated_duplicate_and_reordered_ids_emit_inventory_order_once(self):
        requests = (
            ["alpha.parse", "zeta.check"],
            ["zeta.check", "alpha.parse"],
            ["alpha.parse", "zeta.check", "alpha.parse", "zeta.check"],
        )
        for mode in READ_ONLY_MODES:
            for ids in requests:
                with self.subTest(mode=mode, ids=ids):
                    flags = [arg for suite_id in ids for arg in ("--suite", suite_id)]
                    result = self.invoke(*flags, mode, "--json")
                    self.success(result)
                    value = self.decoded(result)
                    selected = value if mode == "--list" else [s["id"] for s in value["suites"]]
                    self.assertEqual(selected, READY_IDS)
                    self.assert_ready_probe_only()

    def test_selected_output_is_identical_across_reruns_and_aliases(self):
        outputs = []
        for mode in ("--plan", "--dry-run", "--plan"):
            result = self.invoke(mode, "--json", "--suite", "alpha.parse",
                                 "--suite", "zeta.check")
            self.success(result)
            outputs.append(self.decoded(result))
            self.assert_ready_probe_only()
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def test_text_modes_name_only_selected_suites_in_inventory_order(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                result = self.invoke(mode, "--suite", "alpha.parse", "--suite", "zeta.check")
                self.success(result)
                if mode == "--list":
                    self.assertEqual(result.stdout.splitlines(), READY_IDS)
                else:
                    self.assertIn("zeta.check", result.stdout)
                    self.assertIn("alpha.parse", result.stdout)
                    self.assertLess(result.stdout.index("zeta.check"), result.stdout.index("alpha.parse"))
                    self.assertNotIn("middle-missing", result.stdout)
                    self.assertNotIn("poison.fast", result.stdout)
                    self.assertIn("suite-do-not-run", result.stdout)
                self.assert_ready_probe_only()

    def test_selection_preserves_exact_commands_tiers_and_ready_metadata(self):
        ids, plan = self.plan_ids(self.invoke(
            "--plan", "--json", "--suite", "alpha.parse", "--suite", "zeta.check"))
        self.assertEqual(ids, READY_IDS)
        self.assertEqual([s["command"] for s in plan["suites"]], [
            ["suite-do-not-run", "zeta", "two words"], "suite-do-not-run alpha",
        ])
        self.assertEqual([s["tier"] for s in plan["suites"]], ["fast", "parse"])
        for suite in plan["suites"]:
            self.assertEqual(suite["prerequisites"], [{"name": "ready-local", "ready": True}])
            self.assertEqual(suite["preparation"], {"required": False, "actions": []})
        self.assert_ready_probe_only()

    def test_missing_selected_prerequisite_is_reported_without_preparing(self):
        for mode in ("--plan", "--dry-run"):
            with self.subTest(mode=mode):
                ids, plan = self.plan_ids(self.invoke(mode, "--json", "--suite", "middle-missing"))
                self.assertEqual(ids, ["middle-missing"])
                self.assertEqual(plan["suites"][0]["prerequisites"], [
                    {"name": "missing-local", "ready": False},
                ])
                self.assertEqual(plan["suites"][0]["preparation"], {
                    "required": True, "actions": ["suite-prepare-do-not-run missing"],
                })
                self.assertEqual(self.calls, [{"tool": "suite-probe", "argv": ["missing"]}])

    def test_new_inventory_id_and_changed_command_need_no_second_registry(self):
        inventory = json.loads(self.inventory_path.read_text())
        inventory["suites"] = [{
            "id": "fresh-fixture-id", "tier": "parse", "prerequisites": [],
            "preparation": {"actions": []}, "command": ["suite-do-not-run", "fresh argv"],
        }]
        self.inventory_path.write_text(json.dumps(inventory))
        ids, plan = self.plan_ids(self.invoke("--plan", "--json", "--suite", "fresh-fixture-id"))
        self.assertEqual(ids, ["fresh-fixture-id"])
        self.assertEqual(plan["suites"][0]["command"], ["suite-do-not-run", "fresh argv"])
        self.assertEqual(self.calls, [])
        result = self.invoke("--list", "--json", "--suite", "fresh-fixture-id")
        self.success(result)
        self.assertEqual(self.decoded(result), ["fresh-fixture-id"])

    def test_parse_only_intersection_uses_inventory_not_requested_order(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                result = self.invoke(mode, "--parse-only", "--json", "--suite", "zeta.check",
                                     "--suite", "alpha.parse")
                self.success(result)
                value = self.decoded(result)
                self.assertEqual(value if mode == "--list" else [s["id"] for s in value["suites"]],
                                 ["alpha.parse"])
                self.assert_ready_probe_only()

    def test_valid_id_excluded_by_parse_only_is_explicit_empty_without_any_probe(self):
        for mode in READ_ONLY_MODES:
            for machine in (False, True):
                with self.subTest(mode=mode, machine=machine):
                    result = self.invoke(mode, "--parse-only", "--suite", "poison.fast",
                                         *(["--json"] if machine else []))
                    self.success(result)
                    self.assertEqual(self.calls, [], "tier-excluded poison must never be probed")
                    if machine:
                        value = self.decoded(result)
                        self.assertEqual(value if mode == "--list" else value["suites"], [])
                    elif mode == "--list":
                        self.assertEqual(result.stdout, "")
                    else:
                        self.assertRegex(result.stdout.lower(), r"\b0 suites\b|\bno suites\b|\bempty\b")

    def refusal(self, flags, machine=False):
        result = self.invoke(*flags)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls, [], "invalid invocation must fail before every prerequisite probe")
        if not machine:
            self.assertEqual(result.stdout, "", "no partial plan on refusal")
            self.assertRegex(result.stderr.lower(), r"invalid|unknown|usage|requires|cannot|suite")
            return None
        value = self.decoded(result)
        self.assertIsInstance(value, dict)
        version = value.get("schema_version")
        self.assertIn(type(version), (str, int), "schema_version must not be null or boolean")
        self.assertTrue(str(version).strip(), "versioned error envelope required")
        self.assertNotIn("suites", value, "refusal must not emit even a partial successful plan")
        error = value.get("error")
        self.assertIsInstance(error, dict)
        self.assertIsInstance(error.get("code"), str)
        self.assertRegex(error["code"], r"\S", "stable machine error code required")
        self.assertIs(error.get("retryable"), False)
        self.assertIsInstance(error.get("next_action"), str)
        self.assertRegex(error["next_action"], r"\S", "actionable remediation required")
        return error["code"]

    def test_unknown_id_rejects_whole_request_including_parse_only(self):
        for mode in READ_ONLY_MODES:
            for parse in (False, True):
                with self.subTest(mode=mode, parse=parse):
                    flags = [mode, "--suite", "alpha.parse", "--suite", "not-in-inventory"]
                    if parse:
                        flags.append("--parse-only")
                    self.refusal(flags)
                    self.refusal([*flags, "--json"], machine=True)

    def test_empty_missing_and_flag_shaped_values_refuse_before_preparation(self):
        cases = (
            ["--suite"], ["--suite", ""], ["--suite", " "],
            ["--suite", "--parse-only"], ["--suite", "--list"],
            ["--suite", "ZETA.CHECK"], ["--suite", "zeta.check,alpha.parse"],
        )
        for flags in cases:
            with self.subTest(flags=flags):
                self.refusal(["--plan", *flags])
                self.refusal(["--plan", "--json", *flags], machine=True)

    def test_selector_without_read_only_mode_never_enters_execution(self):
        for flags in (
            ["--suite", "zeta.check"],
            ["--parse-only", "--suite", "alpha.parse"],
            ["--no-prepare", "--suite", "middle-missing"],
            ["--parse-only", "--no-prepare", "--suite", "alpha.parse"],
        ):
            with self.subTest(flags=flags):
                self.refusal(flags)
                self.refusal([*flags, "--json"], machine=True)

    def test_invalid_flags_and_conflicting_modes_never_emit_partial_selection(self):
        for flags in (
            ["--plan", "--bogus"], ["--plan", "--list"],
            ["--dry-run", "--list"], ["--plan", "--no-prepare"],
            ["--dry-run", "--no-prepare"], ["--list", "--no-prepare"],
            ["--plan", "unexpected-positional"],
        ):
            with self.subTest(flags=flags):
                selection = ["--suite", "zeta.check"]
                self.refusal([*flags, *selection])
                self.refusal([*flags, *selection, "--json"], machine=True)

    def test_json_error_codes_do_not_depend_on_flag_order_or_output_mode(self):
        codes = []
        for flags in (
            ["--json", "--plan", "--suite", "not-in-inventory"],
            ["--suite", "not-in-inventory", "--dry-run", "--json"],
            ["--list", "--suite", "not-in-inventory", "--json"],
            ["--suite", "not-in-inventory", "--parse-only", "--plan", "--json"],
        ):
            codes.append(self.refusal(flags, machine=True))
        self.assertEqual(len(set(codes)), 1)
        # An unknown flag preceding --json must still select machine errors.
        first = self.refusal(["--bogus", "--suite", "zeta.check", "--plan", "--json"], machine=True)
        last = self.refusal(["--json", "--plan", "--suite", "zeta.check", "--bogus"], machine=True)
        self.assertEqual(first, last)

    def test_unfiltered_no_prepare_missing_prereqs_keeps_exit_three(self):
        result = self.invoke("--no-prepare")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("missing-local", result.stderr)
        self.assertIn("poison-local", result.stderr)
        self.assertRegex(result.stderr.lower(), r"next|restore|provision")


if __name__ == "__main__":
    unittest.main()
