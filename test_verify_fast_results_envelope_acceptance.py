"""Immutable acceptance: opt-in verify-fast execution outcome records.

Run: python3 -B -m unittest -v test_verify_fast_results_envelope_acceptance

Builder must not edit this file. Implement the runner separately, preserving
this suite. The real, unmodified runner executes in a disposable git repository;
only suite dependencies are replaced with offline fixtures. No network, host
commands, package preparation, implementation imports or second suite selector.
The existing inventory supplies every suite ID and command.

Binding JSON conventions (extra fields allowed):
* One object at --results-json PATH: schema="verify-fast-results.v1", head_sha,
  suites=[{suite, tier, status, exit_code, next_action}], totals={pass, fail,
  skipped, unmeasurable}. Optional totals.total must equal len(suites).
* Measured pass/fail retain the actual integer command exit (0/nonzero).
  Skipped has null exit_code; an unlaunchable command is unmeasurable, with
  null or shell launch-error 126/127, never a measured pass/fail.
* Non-pass entries have a nonempty next_action. Invalid mode combinations
  exit 2 with a stable symbolic refusal code and a next action. Write failure
  is nonzero with the same diagnostic properties, never silent success.
* Parse-only may omit fast entries or explicitly mark them skipped. An early
  parse failure may do the same, but never count an unexecuted suite as pass.

Breaks caught: missing/duplicate records or wrong IDs, discarded command exits,
parse failures hidden by later successes, launch failure counted as a test
failure, stale/wrong-tree SHA, partial/early publication, swallowed write errors,
and altered legacy prose or execution exits. Builder owns the additional
mechanical bypass guard and docs/wiring required by the source card.
"""

import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_SHA = "16e9850e71b711744e9ef880ca593605137d1755"
RUNNER = "scripts/verify-fast.sh"
INVENTORY = "scripts/verify-fast-inventory.json"
STATUSES = ("pass", "fail", "skipped", "unmeasurable")
TIMEOUT = 30
SENTINEL = b"previous completed run\n"

# This dispatcher replaces external suite dependencies, NOT result recording or
# control flow. Unknown invocations fail closed; Python helpers still run real.
DISPATCHER = r'''import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ["FIXTURE_ROOT"])
config = json.loads((root.parent / "config.json").read_text())
kind, *args = sys.argv[1:]
def suite(suite_id):
    target = root / "results with spaces.json"
    if target.is_file() and target.read_bytes() != b"previous completed run\n":
        (root.parent / "early-publication").write_text(suite_id)
    with (root.parent / "executed.jsonl").open("a") as out:
        out.write(json.dumps(suite_id) + "\n")
    print("fixture stdout: " + suite_id)
    print("fixture stderr: " + suite_id, file=sys.stderr)
    return config.get("exits", {}).get(suite_id, 0)
if kind == "suite":
    sys.exit(suite(args[0]))
if kind == "blocked":
    (root.parent / "forbidden-call").write_text(" ".join(args))
    sys.exit(99)
if kind == "python3":
    if "verification/offline_ci_contract.py" in args and "--check-restored" in args:
        sys.exit(0)
    if "-c" in args and "import yaml" in args[args.index("-c") + 1]:
        sys.exit(0)
if kind == "node":
    if "-e" in args and "typescript" in args[args.index("-e") + 1]:
        sys.exit(0)
    if "--check" in args:
        suite_id = "parse-esm" if "--input-type=module" in args else "parse-node"
        sys.exit(config.get("exits", {}).get(suite_id, 0))
cwd = str(Path.cwd().relative_to(root))
for invocation in config["invocations"]:
    if [kind, cwd, args] == invocation[:3]:
        sys.exit(suite(invocation[3]))
if kind == "python3" and ("-c" in args or args[:2] == ["-m", "py_compile"]
                          or (args and args[0].startswith("scripts/"))
                          or (args and Path(args[0]).is_absolute()
                              and Path(args[0]).is_relative_to(root / "scripts"))):
    os.execv(sys.executable, [sys.executable, *args])
print("unexpected fixture invocation: " + repr([kind, cwd, args]), file=sys.stderr)
sys.exit(98)
'''


def run(command, *, cwd, env):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                          timeout=TIMEOUT)


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class RunnerFixture:
    def __init__(self, directory):
        self.work = Path(directory)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.dispatcher = self.work / "dispatch.py"
        self.dispatcher.write_text(DISPATCHER)
        # Keep runtime helpers next to the real runner (including a future
        # results writer). Never rewrite the runner or its inventory commands.
        shutil.copytree(ROOT / "scripts", self.repo / "scripts")
        self.current_body = (self.repo / RUNNER).read_bytes()
        self.inventory = json.loads((self.repo / INVENTORY).read_text())["suites"]
        self.config = {"invocations": [], "exits": {}}
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "HOME": str(self.work), "TMPDIR": str(self.work),
            "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "FIXTURE_ROOT": str(self.repo),
        }
        for name in ("python3", "node"):
            executable(self.bin / name, self.command(name, '"$@"'))
        for name in ("npm", "curl", "wget", "ssh", "docker", "gh", "tar"):
            executable(self.bin / name, self.command("blocked", name + ' "$@"'))
        original_path = os.environ.get("PATH", os.defpath)
        self.env["PATH"] = str(self.bin) + os.pathsep + original_path
        # The provisioned git executable can itself be a Node wrapper. Keep its
        # interpreter outside the suite's synthetic node; retain the supplied
        # executable rather than bypassing it or changing credentials.
        executable(self.bin / "git", "#!/bin/bash\nPATH=" + shlex.quote(original_path)
                   + " exec " + shlex.quote(shutil.which("git")) + ' "$@"\n')
        # No inherited BASH_ENV, exported functions, credentials, Git locators,
        # Python/Node hooks or control-plane configuration reach any subprocess.
        self._provision_parse_inputs()
        for entry in self.inventory:
            if entry["tier"] == "fast":
                self._provision_suite(entry)
        executable(self.repo / "scripts/check-toolkit-vendored-tracked.sh",
                   self.command("suite", "vendored-tracked-gate"))
        self.config["invocations"].append(
            ["python3", ".", ["verification/orphan_suites.py"], "orphan-suite-ratchet"])
        self.save_config()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture input")
        self.head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.results = self.repo / "results with spaces.json"

    def command(self, kind, arguments):
        return ("#!/bin/bash\nexec " + shlex.quote(sys.executable) + " "
                + shlex.quote(str(self.dispatcher)) + " " + kind + " "
                + arguments + "\n")

    def git(self, *args):
        proc = run([str(self.bin / "git"), *args], cwd=self.repo, env=self.env)
        if proc.returncode:
            raise AssertionError(proc.stderr.decode())
        return proc

    def _provision_parse_inputs(self):
        for path, content in {
            "parse-fixture.sh": "true\n", "parse-fixture.js": "void 0;\n",
            "parse_fixture.py": "value = 1\n", "mcp/fixture.mjs": "export {};\n",
            "verification/private-mutation-gates.sh": "true\n",
            "ops/agent-tooling/serena-pilot-b/code-nav": "true\n",
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        # The parse gate has hard-coded AST inputs as well as globs. Populate
        # those paths with valid Python; do not replace the real Python parser.
        body = self.current_body.decode()
        logical = body.replace("\\\n", " ")
        for line in logical.splitlines():
            if "ast.parse" not in line:
                continue
            tokens = shlex.split(line.split("||", 1)[0])
            for token in tokens:
                if token.endswith(".py") and token.startswith("ops/"):
                    if "*" in token:
                        token = token.replace("*", "fixture")
                    target = self.repo / token
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("value = 1\n")

    def _provision_suite(self, entry):
        command = entry["command"]
        cwd = "."
        if isinstance(command, str):
            if command.startswith("(cd "):
                command = command[1:-1]
                parts = shlex.split(command)
                cwd = parts[1]
                (self.repo / cwd).mkdir(parents=True, exist_ok=True)
                parts = parts[parts.index("&&") + 1:]
            elif command == "run_backup_observation_contract":
                parts = ["python3", "-m", "unittest", "-v",
                         "test_backup_observation_contract", "test_backup_observation_regressions"]
            else:
                parts = shlex.split(command)
                while "=" in parts[0]:
                    parts.pop(0)
        else:
            parts = list(command)
        if parts[:3] == ["env", "-u", "BASH_ENV"]:
            parts = parts[3:]
        if parts[0] in ("node", "python3"):
            self.config["invocations"].append([parts[0], cwd, parts[1:], entry["id"]])
        elif parts[0] in ("bash", "sh") or parts[0].startswith("./"):
            path = parts[1] if parts[0] in ("bash", "sh") else parts[0]
            executable(self.repo / cwd / path, self.command("suite", entry["id"]))
        else:
            raise AssertionError("unhandled fixture command: " + repr(command))

    def save_config(self):
        (self.work / "config.json").write_text(json.dumps(self.config))

    def invoke(self, *args, results=True, target=None, legacy=False):
        (self.repo / RUNNER).write_bytes(self.current_body if not legacy else self.legacy_body())
        self.save_config()
        for name in ("executed.jsonl", "early-publication", "forbidden-call"):
            (self.work / name).unlink(missing_ok=True)
        if results:
            args = (*args, "--results-json", str(target or self.results))
        proc = run([shutil.which("bash"), RUNNER, *args], cwd=self.repo, env=self.env)
        if (self.work / "forbidden-call").exists():
            raise AssertionError("runner attempted a forbidden external command: "
                                 + (self.work / "forbidden-call").read_text())
        if b"unexpected fixture invocation:" in proc.stderr:
            raise AssertionError(proc.stderr.decode())
        return proc

    def legacy_body(self):
        proc = run([str(self.bin / "git"), "show", BASE_SHA + ":" + RUNNER],
                   cwd=ROOT, env=self.env)
        if proc.returncode:
            raise AssertionError("pinned legacy source unavailable: " + proc.stderr.decode())
        return proc.stdout

    def executed(self):
        path = self.work / "executed.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ResultsEnvelopeAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.fixture = RunnerFixture(self.tmp.name)

    def envelope(self, proc):
        f = self.fixture
        self.assertTrue(f.results.is_file(),
                        "missing results object; exit=%s stderr=%r" % (proc.returncode, proc.stderr))
        data = json.loads(f.results.read_text())  # rejects multiple objects/trailing prose
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("schema"), "verify-fast-results.v1")
        self.assertEqual(data.get("head_sha"), f.head)
        entries = data.get("suites")
        self.assertIsInstance(entries, list)
        self.assertTrue(entries)
        expected = {s["id"]: s["tier"] for s in f.inventory}
        seen = []
        for entry in entries:
            self.assertTrue({"suite", "tier", "status", "exit_code", "next_action"} <= entry.keys())
            suite = entry["suite"]
            seen.append(suite)
            self.assertIn(suite, expected)
            self.assertEqual(entry["tier"], expected[suite])
            status, code = entry["status"], entry["exit_code"]
            self.assertIn(status, STATUSES)
            if status in ("pass", "fail"):
                self.assertIs(type(code), int, "bool/null is not a measured exit code")
                if status == "pass":
                    self.assertEqual(code, 0)
                else:
                    self.assertGreater(code, 0)
            elif status == "skipped":
                self.assertIsNone(code)
            else:
                self.assertIn(code, (None, 126, 127))
            if status != "pass":
                self.assertIsInstance(entry["next_action"], str)
                self.assertTrue(entry["next_action"].strip())
        self.assertEqual(len(seen), len(set(seen)), "one record per stable suite ID")
        counts = collections.Counter(entry["status"] for entry in entries)
        totals = data.get("totals")
        self.assertIsInstance(totals, dict)
        for status in STATUSES:
            self.assertIs(type(totals.get(status)), int)
            self.assertEqual(totals[status], counts[status])
        self.assertEqual(sum(totals[status] for status in STATUSES), len(entries))
        if "total" in totals:
            self.assertEqual(totals["total"], len(entries))
        self.assertFalse((f.work / "early-publication").exists(),
                         "results file published before suites finished")
        return data, {entry["suite"]: entry for entry in entries}

    def diagnostic(self, proc):
        self.assertNotEqual(proc.returncode, 0)
        text = (proc.stdout + proc.stderr).decode()
        self.assertNotIn("Traceback (most recent call last)", text)
        # Exit status alone does not tell an agent which refusal occurred.
        match = re.search(r"(?:code[\s\"']*[:=][\s\"']*([A-Za-z][A-Za-z0-9_.-]+)"
                          r"|\b([A-Z][A-Z0-9_]*(?:_[A-Z0-9]+)+)\b)", text)
        self.assertIsNotNone(match, "refusal/write error needs a stable symbolic code: " + text)
        self.assertRegex(text.lower(), r"next[_ -](?:action|step)[\s\"']*[:=]\s*\S")
        return match.group(1) or match.group(2)

    def test_clean_full_run_records_all_inventory_suites_and_reconciles_totals(self):
        f = self.fixture
        f.results.write_bytes(SENTINEL)
        proc = f.invoke("--no-prepare")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        data, entries = self.envelope(proc)
        self.assertEqual(set(entries), {s["id"] for s in f.inventory})
        self.assertEqual(data["totals"]["pass"], len(f.inventory))
        self.assertEqual(data["totals"]["fail"], 0)
        self.assertTrue(all(e["status"] == "pass" for e in entries.values()))
        self.assertEqual(set(f.executed()), {s["id"] for s in f.inventory if s["tier"] == "fast"}
                         | {"vendored-tracked-gate", "orphan-suite-ratchet"})

    def test_induced_failures_keep_exact_exits_and_later_suites_still_record(self):
        f = self.fixture
        f.config["exits"] = {"external-disclosure": 7, "deploy-window-open": 19,
                               "backup-observation-contract": 23}
        proc = f.invoke("--no-prepare")
        self.assertEqual(proc.returncode, 1, proc.stderr.decode())
        data, entries = self.envelope(proc)
        for suite, code in f.config["exits"].items():
            with self.subTest(suite=suite):
                self.assertEqual(entries[suite]["status"], "fail")
                self.assertEqual(entries[suite]["exit_code"], code)
        self.assertEqual(data["totals"]["fail"], 3)
        self.assertEqual(entries["verify-fast-plan"]["status"], "pass")

    def test_parse_only_writes_results_without_executing_or_passing_fast_suites(self):
        f = self.fixture
        proc = f.invoke("--parse-only", "--no-prepare")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        data, entries = self.envelope(proc)
        for suite in f.inventory:
            if suite["tier"] == "parse":
                self.assertEqual(entries[suite["id"]]["status"], "pass")
            elif suite["id"] in entries:
                self.assertEqual(entries[suite["id"]]["status"], "skipped")
        self.assertEqual(data["totals"]["pass"], sum(s["tier"] == "parse" for s in f.inventory))
        self.assertEqual(set(f.executed()), {"vendored-tracked-gate", "orphan-suite-ratchet"})

    def test_parse_failure_is_recorded_even_when_following_parse_commands_pass(self):
        f = self.fixture
        (f.repo / "parse_fixture.py").write_text("def broken(:\n")
        proc = f.invoke("--no-prepare")
        self.assertEqual(proc.returncode, 1, proc.stderr.decode())
        _, entries = self.envelope(proc)
        self.assertEqual(entries["parse-python"]["status"], "fail")
        self.assertEqual(entries["parse-python"]["exit_code"], 1)
        self.assertEqual(entries["orphan-suite-ratchet"]["status"], "pass")
        for entry in entries.values():
            if entry["tier"] == "fast":
                self.assertEqual(entry["status"], "skipped")
        self.assertNotIn("external-disclosure", f.executed())

    def test_unlaunchable_suite_is_unmeasurable_not_pass_or_measured_failure(self):
        f = self.fixture
        (f.repo / "test_deploy_window_open.sh").unlink()
        proc = f.invoke("--no-prepare")
        self.assertNotEqual(proc.returncode, 0)
        data, entries = self.envelope(proc)
        self.assertEqual(entries["deploy-window-open"]["status"], "unmeasurable")
        self.assertEqual(data["totals"]["unmeasurable"], 1)
        self.assertEqual(data["totals"]["fail"], 0)
        self.assertEqual(data["totals"]["pass"], len(f.inventory) - 1)
        self.assertNotIn("deploy-window-open", f.executed())

    def test_measured_exit_127_is_not_mistaken_for_an_unlaunchable_command(self):
        f = self.fixture
        f.config["exits"] = {"external-disclosure": 127}
        proc = f.invoke("--no-prepare")
        self.assertEqual(proc.returncode, 1, proc.stderr.decode())
        data, entries = self.envelope(proc)
        self.assertIn("external-disclosure", f.executed())
        self.assertEqual(entries["external-disclosure"]["status"], "fail")
        self.assertEqual(entries["external-disclosure"]["exit_code"], 127)
        self.assertEqual(data["totals"]["unmeasurable"], 0)
        self.assertEqual(data["totals"]["fail"], 1)

    def test_plan_list_and_dry_run_refuse_results_without_touching_the_target(self):
        f = self.fixture
        codes = []
        for mode in ("--plan", "--list", "--dry-run"):
            for order in ("before", "after"):
                with self.subTest(mode=mode, order=order):
                    f.results.write_bytes(SENTINEL)
                    args = [mode, "--results-json", str(f.results)]
                    if order == "before":
                        args = args[1:] + args[:1]
                    proc = f.invoke(*args, results=False)
                    self.assertEqual(proc.returncode, 2)
                    codes.append(self.diagnostic(proc))
                    self.assertEqual(f.results.read_bytes(), SENTINEL)
                    self.assertEqual(f.executed(), [])
        self.assertEqual(len(set(codes)), 1, "same invalid mode class needs one stable code")

    def test_missing_results_path_is_controlled_usage_refusal(self):
        proc = self.fixture.invoke("--results-json", results=False)
        self.assertEqual(proc.returncode, 2)
        self.diagnostic(proc)
        self.assertEqual(self.fixture.executed(), [])

    def test_results_write_failure_is_nonzero_with_code_and_remediation(self):
        f = self.fixture
        # Directory-as-file is deterministic even when tests run as root.
        f.results.mkdir()
        first = f.invoke("--no-prepare", target=f.results)
        code = self.diagnostic(first)
        self.assertTrue(f.results.is_dir())
        second = f.invoke("--no-prepare", target=f.results)
        self.assertEqual(self.diagnostic(second), code)
        self.assertEqual(second.returncode, first.returncode)

    def test_head_sha_binds_fixture_tree_and_changes_after_a_new_commit(self):
        f = self.fixture
        first = f.invoke("--parse-only", "--no-prepare")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        data, _ = self.envelope(first)
        previous = data["head_sha"]
        f.results.unlink()
        (f.repo / "new-input.txt").write_text("changed input\n")
        f.git("add", "new-input.txt")
        f.git("commit", "-qm", "changed fixture input")
        f.head = f.git("rev-parse", "HEAD").stdout.decode().strip()
        self.assertNotEqual(f.head, previous)
        second = f.invoke("--parse-only", "--no-prepare")
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.envelope(second)

    def test_default_stdout_stderr_and_exit_match_pinned_legacy_runner(self):
        f = self.fixture
        for mode, failures in (
            ([], {}), (["--parse-only"], {}),
            ([], {"external-disclosure": 7, "deploy-window-open": 19}),
        ):
            with self.subTest(mode=mode, failures=failures):
                f.config["exits"] = failures
                legacy = f.invoke(*mode, results=False, legacy=True)
                current = f.invoke(*mode, results=False)
                self.assertEqual(legacy.returncode, 1 if failures else 0,
                                 "legacy fixture must exercise the intended outcome")
                self.assertEqual((current.returncode, current.stdout, current.stderr),
                                 (legacy.returncode, legacy.stdout, legacy.stderr))
                self.assertFalse(f.results.exists(), "default run must not create a results file")

    def test_results_flag_does_not_change_execution_prose_or_exit(self):
        f = self.fixture
        for failures in ({}, {"external-disclosure": 7}):
            with self.subTest(failures=failures):
                f.config["exits"] = failures
                plain = f.invoke("--no-prepare", results=False)
                structured = f.invoke("--no-prepare")
                self.assertEqual((structured.returncode, structured.stdout, structured.stderr),
                                 (plain.returncode, plain.stdout, plain.stderr))
                self.envelope(structured)
                f.results.unlink()


if __name__ == "__main__":
    unittest.main()
