"""Acceptance contract: exact pinned TypeScript readiness (typescript@5.9.3).

Builder contract under test (bundle key `typescript-exact-readiness`):

* Readiness for the stable prerequisite ID ``typescript@5.9.3`` means the
  *installed* TypeScript package version is exactly ``5.9.3`` **and** the
  compiler entry point is available. A successful ``require.resolve`` (or any
  other presence-only check) is never sufficient.
* The same decision is made consistently in three places:
  1. plan readiness (``--plan --json`` / ``--dry-run --json``),
  2. ``--no-prepare`` admission (refusal exits 3 and names the prerequisite),
  3. the default (preparation-enabled) guard, which must reach the existing
     pinned install path for anything not ready.
* Not ready, always: package absent, compiler entry point absent, installed
  version different from ``5.9.3`` (older or newer), package metadata
  malformed / unreadable / lacking a usable version string, and a ``node``
  invocation that fails while probing. A failing probe is never
  ready-by-default.
* Compatibility is preserved: plan ``schema_version`` stays ``1``; the
  prerequisite ID stays ``typescript@5.9.3``; prerequisite entries keep
  ``name``/``ready`` (bool); the preparation action for the TypeScript suite
  stays ``npm install --no-save --ignore-scripts typescript@5.9.3``;
  ``--no-prepare`` refusal keeps exit code ``3`` with a next action. Exactly
  one TypeScript pin version (``5.9.3``) may appear in the inventory and in
  ``scripts/verify-fast.sh``.
* ``--plan``/``--dry-run`` stay read-only and ``--no-prepare`` never invokes
  an installer/downloader, whatever the fixture. ``--help`` names the exact
  pin (``5.9.3``) so the enforced version is discoverable.
* The builder wires this module into the existing ``verify-fast-plan``
  inventory suite command (and its execution line) so it runs in the fast
  tier; drift guards then hold it in correspondence.

Fixtures are black-box: a disposable repository holding the real
``verify-fast.sh`` (suite execution lines neutralized, guards intact), the
real plan helper and inventory, and a synthetic installed
``node_modules/typescript`` package exercised through the real ``node``.
Installers/downloaders are poison spies; no real install, network or full
verifier run happens here.

IMMUTABLE: Builder starts from the pushed test branch/commit and never
edits this file. Corrections belong to the Test Author.

Run: python3 -B -m unittest -v test_typescript_readiness_acceptance
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

PIN = "5.9.3"
PREREQ_ID = f"typescript@{PIN}"
INSTALL_ACTION = f"npm install --no-save --ignore-scripts typescript@{PIN}"
NO_PREPARE_EXIT = 3
TIMEOUT = 60

FAIL_TAIL = re.compile(r'\s*\|\|\s*fail\s+"[^"]*"\s*$')
PRESENCE_ONLY_PROBE = ["node", "-e", "require.resolve('typescript/bin/tsc')"]


def scratch_dir():
    return tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))


def write_executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# --- Suite neutralization (same technique as the plan-gates suite): keep the
# real flag handling, readiness probes and preparation guards; replace every
# column-0 suite execution line with `true` so runs stay hermetic. ---------

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


def neutralized_script(body):
    out = []
    pending = []
    for raw in body.splitlines():
        pending.append(raw)
        if raw.endswith("\\"):
            continue
        logical = (logical_lines("\n".join(pending)) or [""])[0]
        out.append("true" if is_execution_line(logical) else "\n".join(pending))
        pending = []
    return "\n".join(out) + "\n"


# --- Synthetic installed typescript package --------------------------------
# Shape mirrors the real package closely enough that every reasonable probe
# (require.resolve of bin/tsc, require of package.json / main entry version,
# `tsc --version`) reads the fixture's declared version.

TSC_BIN = """#!/usr/bin/env node
try {
  const v = require("../package.json").version;
  if (process.argv.includes("--version") || process.argv.includes("-v")) {
    console.log("Version " + v);
  }
} catch (e) {
  process.exit(1);
}
"""

TYPESCRIPT_MAIN = """try {
  module.exports = { version: require("../package.json").version };
} catch (e) {
  module.exports = {};
}
"""


def install_fake_typescript(repo, version=PIN, compiler=True,
                            metadata="valid"):
    pkg = repo / "node_modules" / "typescript"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "lib").mkdir(parents=True)
    manifest = pkg / "package.json"
    if metadata == "valid":
        manifest.write_text(json.dumps({
            "name": "typescript",
            "version": version,
            "main": "./lib/typescript.js",
            "bin": {"tsc": "./bin/tsc", "tsserver": "./bin/tsserver"},
        }))
    elif metadata == "malformed":
        manifest.write_text('{"name": "typescript", "version": ')
    elif metadata == "no-version":
        manifest.write_text(json.dumps({
            "name": "typescript",
            "main": "./lib/typescript.js",
            "bin": {"tsc": "./bin/tsc"},
        }))
    elif metadata == "unreadable":
        manifest.write_text(json.dumps({"name": "typescript", "version": version}))
        manifest.chmod(0)
    else:  # pragma: no cover - fixture programming error
        raise AssertionError(f"unknown metadata fixture {metadata!r}")
    (pkg / "lib" / "typescript.js").write_text(TYPESCRIPT_MAIN)
    (pkg / "lib" / "tsc.js").write_text(TYPESCRIPT_MAIN)
    if compiler:
        write_executable(pkg / "bin" / "tsc", TSC_BIN)
        write_executable(pkg / "bin" / "tsserver", TSC_BIN)
        dot_bin = repo / "node_modules" / ".bin"
        dot_bin.mkdir(exist_ok=True)
        (dot_bin / "tsc").symlink_to(pkg / "bin" / "tsc")


# Fixture cases: name -> (installer kwargs or None, node_fails, expected_ready)
CASES = {
    "missing-package": (None, False, False),
    "exact-version": (dict(version=PIN), False, True),
    "wrong-version-older": (dict(version="5.9.2"), False, False),
    "wrong-version-newer": (dict(version="5.10.0"), False, False),
    "missing-compiler": (dict(version=PIN, compiler=False), False, False),
    "malformed-metadata": (dict(metadata="malformed"), False, False),
    "metadata-without-version": (dict(metadata="no-version"), False, False),
    "unreadable-metadata": (dict(metadata="unreadable"), False, False),
    "node-failure": (dict(version=PIN), True, False),
}

TOOLKIT_OK_PYTHON = """#!/usr/bin/env bash
echo "python3 $*" >> "$STUB_DIR/calls.log"
if [[ "$*" == *offline_ci_contract*--check-restored* ]]; then
  exit 0
fi
exec "{real}" "$@"
"""

FAILING_NODE = """#!/usr/bin/env bash
echo "node $*" >> "$STUB_DIR/calls.log"
exit 1
"""


def poison(name, rc):
    return f"""#!/usr/bin/env bash
echo "{name} $*" >> "$STUB_DIR/calls.log"
exit {rc}
"""


class ReadinessFixture:
    """Disposable repo runner shared by the behavioral test classes."""

    def run_case(self, case, args, npm_rc=99):
        """Run verify-fast.sh for one fixture case; return (proc, calls)."""
        install_kwargs, node_fails, _ = CASES[case]
        with scratch_dir() as tmp:
            tmp = Path(tmp)
            repo = tmp / "repo"
            (repo / "scripts").mkdir(parents=True)
            # Resolution fence: node walks parent directories looking for
            # node_modules, so a host-level install (e.g. /tmp/node_modules)
            # would otherwise leak into the fixture and mask a missing or
            # compiler-less repo package. An exports-empty typescript scope
            # one level above the repo makes any repo-level miss terminal.
            fence = tmp / "node_modules" / "typescript"
            fence.mkdir(parents=True)
            (fence / "package.json").write_text(
                '{"name": "typescript", "exports": {}}')
            shutil.copy(PLAN_HELPER, repo / "scripts" / "verify-fast-plan.py")
            shutil.copy(INVENTORY, repo / "scripts" / "verify-fast-inventory.json")
            write_executable(repo / "scripts" / "verify-fast.sh",
                             neutralized_script(VERIFY_FAST.read_text()))
            if install_kwargs is not None:
                install_fake_typescript(repo, **install_kwargs)
            stubs = tmp / "stubs"
            stubs.mkdir()
            (stubs / "calls.log").write_text("")
            real_python = shutil.which("python3") or "/usr/bin/python3"
            write_executable(stubs / "python3",
                             TOOLKIT_OK_PYTHON.format(real=real_python))
            if node_fails:
                write_executable(stubs / "node", FAILING_NODE)
            write_executable(stubs / "npm", poison("npm", npm_rc))
            for name in ("curl", "wget", "tar"):
                write_executable(stubs / name, poison(name, 99))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                                "BASH_ENV", "NODE_PATH")}
            env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
            env["STUB_DIR"] = str(stubs)
            completed = subprocess.run(
                ["bash", str(repo / "scripts" / "verify-fast.sh"), *args],
                capture_output=True, text=True, timeout=TIMEOUT,
                cwd=str(repo), env=env)
            manifest = (repo / "node_modules" / "typescript" / "package.json")
            if manifest.exists():
                manifest.chmod(0o644)  # let scratch cleanup stat/unlink freely
            return completed, (stubs / "calls.log").read_text()

    @staticmethod
    def typescript_entries(parsed):
        return [
            (suite.get("id"), prereq, suite.get("preparation"))
            for suite in parsed.get("suites", [])
            for prereq in suite.get("prerequisites", [])
            if prereq.get("name") == PREREQ_ID
        ]


class PlanReadinessTest(ReadinessFixture, unittest.TestCase):
    """--plan --json reports the exact-version decision, schema unchanged."""

    def _plan(self, case):
        completed, calls = self.run_case(case, ["--plan", "--json"])
        self.assertEqual(completed.returncode, 0,
                         f"plan must exit 0 ({case}): {completed.stderr[:400]!r}")
        parsed = json.loads(completed.stdout)
        return parsed, calls

    def test_plan_readiness_matches_expected_for_every_case(self):
        for case, (_, _, expected_ready) in CASES.items():
            with self.subTest(case=case):
                parsed, _ = self._plan(case)
                entries = self.typescript_entries(parsed)
                self.assertTrue(entries,
                                f"plan must report prerequisite {PREREQ_ID!r}")
                for suite_id, prereq, _ in entries:
                    self.assertIs(
                        prereq.get("ready"), expected_ready,
                        f"{case}: suite {suite_id} prerequisite {PREREQ_ID} "
                        f"ready must be {expected_ready}, got {prereq!r}")

    def test_plan_schema_and_stable_id_are_compatible(self):
        parsed, _ = self._plan("exact-version")
        self.assertEqual(parsed.get("schema_version"), 1,
                         "plan schema_version must stay 1")
        for suite_id, prereq, preparation in self.typescript_entries(parsed):
            with self.subTest(suite=suite_id):
                self.assertEqual(prereq.get("name"), PREREQ_ID)
                self.assertIsInstance(prereq.get("ready"), bool)
                self.assertIsInstance(preparation, dict)
                self.assertIsInstance(preparation.get("required"), bool)
                self.assertIn(INSTALL_ACTION, preparation.get("actions", []),
                              "pinned preparation action must be preserved")

    def test_plan_and_dry_run_agree_on_wrong_version(self):
        parsed, _ = self._plan("wrong-version-older")
        dry_completed, _ = self.run_case("wrong-version-older",
                                         ["--dry-run", "--json"])
        self.assertEqual(dry_completed.returncode, 0)
        dry = json.loads(dry_completed.stdout)
        self.assertEqual(
            [p["ready"] for _, p, _ in self.typescript_entries(parsed)],
            [p["ready"] for _, p, _ in self.typescript_entries(dry)],
            "--plan and --dry-run must make the same readiness decision")

    def test_plan_never_invokes_installers_or_downloaders(self):
        for case in ("wrong-version-older", "malformed-metadata",
                     "missing-package"):
            with self.subTest(case=case):
                _, calls = self._plan(case)
                for tool in ("npm", "curl", "wget", "tar"):
                    self.assertNotRegex(
                        calls, rf"(?m)^{tool}\b",
                        f"plan mode must never invoke {tool} ({case})")


class NoPrepareAdmissionTest(ReadinessFixture, unittest.TestCase):
    """--no-prepare admits only the exact pin; refusal is exit 3, named."""

    def test_exact_version_is_admitted_without_preparation(self):
        completed, calls = self.run_case("exact-version", ["--no-prepare"])
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, combined[:800])
        self.assertNotRegex(calls, r"(?m)^npm\b",
                            "--no-prepare must never run npm")
        self.assertNotIn("installing pinned typescript", combined)

    def test_not_ready_cases_are_refused_with_code_3_and_named(self):
        for case, (_, _, expected_ready) in CASES.items():
            if expected_ready:
                continue
            with self.subTest(case=case):
                completed, calls = self.run_case(case, ["--no-prepare"])
                combined = completed.stdout + "\n" + completed.stderr
                self.assertEqual(
                    completed.returncode, NO_PREPARE_EXIT,
                    f"{case}: --no-prepare must refuse with exit "
                    f"{NO_PREPARE_EXIT}, got {completed.returncode}: "
                    f"{combined[:800]!r}")
                self.assertIn(
                    PREREQ_ID, combined,
                    f"{case}: refusal must name {PREREQ_ID}")
                self.assertRegex(
                    combined.lower(),
                    r"next[^:\n]*:|next action|run .*without --no-prepare|"
                    r"to (fix|proceed|restore|install)",
                    f"{case}: refusal must print a next action")
                self.assertNotRegex(
                    calls, r"(?m)^npm\b",
                    f"{case}: --no-prepare must never run npm, even to fix "
                    "a wrong version")
                for tool in ("curl", "wget", "tar"):
                    self.assertNotRegex(calls, rf"(?m)^{tool}\b")
                self.assertNotIn("installing pinned typescript",
                                 combined.lower())
                self.assertNotIn("verify-fast: pass", combined.lower(),
                                 "a refusal must never report passing")


class DefaultPreparationGuardTest(ReadinessFixture, unittest.TestCase):
    """Default mode reaches the pinned install path iff not ready."""

    def _default_run(self, case):
        # npm spy succeeds: reaching the existing preparation path is the
        # behavior under test, not a real install.
        return self.run_case(case, [], npm_rc=0)

    def test_exact_version_never_reaches_installer(self):
        completed, calls = self._default_run("exact-version")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, combined[:800])
        self.assertNotRegex(calls, r"(?m)^npm\b",
                            "exact pin installed: guard must not reinstall")
        self.assertNotIn("installing pinned typescript", combined)

    def test_not_ready_cases_reach_the_pinned_installer(self):
        for case, (_, _, expected_ready) in CASES.items():
            if expected_ready:
                continue
            with self.subTest(case=case):
                _, calls = self._default_run(case)
                npm_lines = [ln for ln in calls.splitlines()
                             if ln.startswith("npm ")]
                self.assertTrue(
                    npm_lines,
                    f"{case}: default mode must reach the pinned "
                    f"preparation path; calls were: {calls!r}")
                self.assertTrue(
                    any(PREREQ_ID in ln for ln in npm_lines),
                    f"{case}: installer must be pinned to {PREREQ_ID}, "
                    f"got {npm_lines!r}")

    def test_plan_and_default_guard_agree_for_every_case(self):
        for case in CASES:
            with self.subTest(case=case):
                plan_completed, _ = self.run_case(case, ["--plan", "--json"])
                parsed = json.loads(plan_completed.stdout)
                ready_flags = {p["ready"] for _, p, _
                               in self.typescript_entries(parsed)}
                self.assertEqual(len(ready_flags), 1,
                                 "plan must decide consistently across suites")
                _, calls = self._default_run(case)
                installer_reached = bool(
                    re.search(r"(?m)^npm\b", calls))
                self.assertEqual(
                    ready_flags.pop(), not installer_reached,
                    f"{case}: plan readiness and the default preparation "
                    "guard disagree")


class PinAndProbeDefinitionTest(unittest.TestCase):
    """One repo-owned pin/probe definition; presence-only probe is gone."""

    def setUp(self):
        self.inventory = json.loads(INVENTORY.read_text())
        self.script = VERIFY_FAST.read_text()

    def test_catalog_keeps_stable_prerequisite_id(self):
        catalog = self.inventory.get("prerequisite_catalog", {})
        self.assertIn(PREREQ_ID, catalog,
                      f"catalog must keep stable prerequisite {PREREQ_ID!r}")
        ts_suites = [s["id"] for s in self.inventory.get("suites", [])
                     if PREREQ_ID in s.get("prerequisites", [])]
        self.assertTrue(ts_suites,
                        "at least one suite must require the typescript pin")
        for suite in self.inventory["suites"]:
            if PREREQ_ID in suite.get("prerequisites", []):
                self.assertIn(INSTALL_ACTION,
                              suite.get("preparation", {}).get("actions", []))

    def test_probe_is_not_presence_only_and_encodes_the_pin(self):
        probe = (self.inventory["prerequisite_catalog"][PREREQ_ID]
                 .get("probe", {}))
        self.assertNotEqual(
            probe.get("argv"), PRESENCE_ONLY_PROBE,
            "the presence-only require.resolve probe cannot remain: it "
            "reports any installed version as ready")
        serialized = json.dumps(probe)
        pin_encoded = PIN in serialized
        if not pin_encoded:
            # A shared repo-owned probe helper is acceptable iff the file it
            # names exists in the repo and itself encodes the exact pin.
            for token in probe.get("argv", []):
                candidate = ROOT / token
                if candidate.is_file() and PIN in candidate.read_text(
                        errors="ignore"):
                    pin_encoded = True
                    break
        self.assertTrue(
            pin_encoded,
            "the probe definition (or the repo-owned helper it invokes) "
            f"must encode the exact pin {PIN}; probe is {serialized!r}")

    def test_exactly_one_typescript_pin_version_exists(self):
        found = set(re.findall(r"typescript@([0-9][0-9A-Za-z.\-]*)",
                               INVENTORY.read_text() + "\n" + self.script))
        self.assertEqual(found, {PIN},
                         "inventory and verify-fast.sh must agree on the "
                         f"single pin {PIN}; found {sorted(found)}")


class HelpDiscoverabilityTest(unittest.TestCase):
    """The enforced exact pin is discoverable from --help."""

    def test_help_names_the_exact_pin(self):
        completed = subprocess.run(
            ["bash", str(VERIFY_FAST), "--help"],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=str(ROOT))
        self.assertEqual(completed.returncode, 0)
        self.assertIn(PIN, completed.stdout,
                      "--help must name the exact enforced TypeScript pin "
                      f"({PREREQ_ID}); got: {completed.stdout[:400]!r}")


if __name__ == "__main__":
    unittest.main()
