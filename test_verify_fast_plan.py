"""Acceptance contract: read-only plan + explicit no-preparation mode.

Builder contract under test (paired acceptance card, bundle key `verification-plan`):

* `scripts/verify-fast.sh --plan --json` (with `--dry-run` as a plan alias)
  returns schema version, stable suite IDs, exact commands, tier,
  prerequisite readiness and preparation requirements. Plan mode performs
  no suite execution, downloads, cache cleanup or filesystem writes and
  needs no network/credentials.
* `--list`, JSON planning and fast-tier execution selection derive from
  one repo-owned suite inventory so commands cannot silently diverge.
  Refactor limited to fast-tier selection/preparation; default execution
  behavior and required suites preserved.
* Explicit `--no-prepare`: missing prerequisites produce stable error
  code 3 with a next action before auto-restoration/install. Invalid
  flag combinations fail clearly (exit 2). Never silently skip a
  required suite or report skipped/unknown as passing.
* Normal execution and `--parse-only` compatibility explicit; ordinary
  verification is NOT wholly read-only (help must say so). Concise
  `--help` with examples and a focused offline contract command,
  discoverable from the existing Makefile/README entrypoint.

IMMUTABLE: Builder starts from the pushed test branch/commit and never
edits this file. Corrections belong to the Test Author.

Run: python3 -B -m unittest -v test_verify_fast_plan
Existing focused checks: scripts/verify-fast.sh --list ; make verify-fast-parse
No host/network/DB, no package installation. Synthetic fixtures only.

Conventions used below (binding on the builder):
* Plan JSON top level: {"schema_version": <str|int>, "suites": [...]}.
  Extra top-level keys allowed.
* Each suite: {"id": <stable id>, "command": <str|list[str]>,
  "tier": <"parse"|"fast">, "prerequisites": [{"name":..., "ready":bool}],
  "preparation": {"required": bool, "actions": [...]}}.
  "command" may be a string or a non-empty argv list. Tier values are
  exactly "parse" or "fast". Extra suite keys allowed.
* Stable IDs match ^[a-z0-9][a-z0-9._-]*$ (lowercase, non-empty).
* --no-prepare with missing prerequisites exits 3 (NO_PREPARE_EXIT).
  Usage/invalid-flag failures exit 2 (INVALID_USAGE_EXIT). Success 0.
* Focused offline contract target: one of MAKE_PLAN_TARGETS in Makefile.
* Suite inventory: one repo-owned source; accepted as a repo-owned
  inventory file from INVENTORY_CANDIDATES sourced by verify-fast.sh,
  or a single in-script SUITE_INVENTORY definition site. --list must
  emit the same stable IDs as the plan (no silent divergence).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERIFY_FAST = ROOT / "scripts" / "verify-fast.sh"
MAKEFILE = ROOT / "Makefile"
README = ROOT / "README.md"

PLAN_TIMEOUT = 15
PARSE_TIMEOUT = 120
MAKE_TIMEOUT = 30
PLAN_MAX_BYTES = 256 * 1024
HELP_MAX_LINES = 80
HELP_MAX_BYTES = 4096

NO_PREPARE_EXIT = 3
INVALID_USAGE_EXIT = 2

SUITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TIERS = {"parse", "fast"}

MAKE_PLAN_TARGETS = (
    "verify-fast-plan",
    "verify-plan",
    "verify-fast-contract",
    "plan",
)

INVENTORY_CANDIDATES = (
    "scripts/verify-fast-inventory.json",
    "scripts/verify-fast-suites.json",
    "scripts/verify-fast-inventory.sh",
    "scripts/verify-fast-suites.sh",
    "verification/verify-fast-inventory.json",
    "verification/verify-fast-suites.json",
)

# Fast-tier suites that must survive the refactor (substring match,
# case-insensitive, against plan IDs/commands and the script body).
REQUIRED_SUITE_MARKERS = (
    "external-disclosure",
    "muse-stop-guard",
    "disposition-stop-guard",
    "translator",
    "migration",
)

CREDENTIAL_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY", re.I)


def scratch_dir():
    """Run-owned temp dir; Paperclip reaps PAPERCLIP_SCRATCH_DIR runs."""
    return tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))


def scrubbed_env(extra=None):
    """Env without credential-like vars; proves no credential need."""
    env = dict(os.environ)
    for key in [k for k in env if CREDENTIAL_ENV_RE.search(k)]:
        env.pop(key, None)
    # Never let a caller-inherited repo locator retarget fixture git calls.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    if extra:
        env.update(extra)
    return env


def run_verify_fast(args, env=None, cwd=None, timeout=PLAN_TIMEOUT):
    """Run verify-fast.sh with deterministic bounds; never raises."""
    return subprocess.run(
        ["bash", str(VERIFY_FAST), *args],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(cwd or ROOT), env=env or scrubbed_env(),
    )


def run_make(target, env=None, timeout=MAKE_TIMEOUT):
    return subprocess.run(
        ["make", target],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(ROOT), env=env or scrubbed_env(),
    )


def plan_json(args, env=None, timeout=PLAN_TIMEOUT):
    """Run a plan invocation; return (completed, parsed-dict-or-None)."""
    completed = run_verify_fast(args, env=env, timeout=timeout)
    parsed = None
    if completed.stdout.strip():
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            parsed = None
    return completed, parsed


def suite_commands(suite):
    cmd = suite.get("command")
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd):
        return " ".join(cmd)
    return None


def extract_list_ids(text):
    """Normalize --list output to a stable-ID token set."""
    tokens = re.split(r"[\s,;:|()\[\]]+", text.strip().lower())
    return {t for t in tokens if SUITE_ID_RE.match(t)}


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_paths(paths):
    snap = {}
    for path in paths:
        path = Path(path)
        try:
            st = path.stat()
            digest = file_sha256(path) if path.is_file() else None
            snap[str(path)] = (st.st_mtime_ns, st.st_size, digest)
        except FileNotFoundError:
            snap[str(path)] = None
    return snap


def repo_write_sentinels():
    """Paths plan/no-prepare purity checks must leave untouched."""
    return [
        ROOT / "__pycache__",
        ROOT / "omniroute" / "__pycache__",
        ROOT / "node_modules",
        ROOT / "vendor" / "toolkit-files.txt",
        ROOT / "vendor" / "toolkit-pins.txt",
        ROOT / "Makefile",
        VERIFY_FAST,
    ]


def write_spy(spy_dir, name, body):
    path = Path(spy_dir) / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def spy_path_with(env, spy_dir):
    env = dict(env)
    env["PATH"] = f"{spy_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


DELEGATE_HEADER = """#!/usr/bin/env bash
# Spy: log argv, then delegate or fail per fixture below.
LOG="${SPY_LOG:?SPY_LOG unset}"
echo "$(basename "$0") $*" >> "$LOG"
"""


class PlanJsonContractTest(unittest.TestCase):
    """--plan --json returns the machine planning contract."""

    def test_plan_json_exits_zero_and_is_valid_json(self):
        completed, parsed = plan_json(["--plan", "--json"])
        self.assertEqual(
            completed.returncode, 0,
            f"--plan --json must exit 0, got {completed.returncode}: "
            f"stdout={completed.stdout[:500]!r} stderr={completed.stderr[:500]!r}")
        self.assertIsNotNone(parsed, "stdout must be valid JSON")
        self.assertIsInstance(parsed, dict, "plan root must be an object")
        self.assertLessEqual(
            len(completed.stdout.encode()), PLAN_MAX_BYTES,
            "plan output exceeds deterministic bound")

    def test_plan_json_has_schema_version(self):
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON (see previous test)")
        self.assertIn("schema_version", parsed,
                      "plan must carry top-level schema_version")
        version = parsed["schema_version"]
        self.assertIsInstance(version, (str, int),
                              "schema_version must be str or int")
        self.assertTrue(str(version).strip(), "schema_version must be non-empty")

    def test_plan_suites_have_stable_ids_commands_tier(self):
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        suites = parsed.get("suites")
        self.assertIsInstance(suites, list, "plan.suites must be a list")
        self.assertGreater(len(suites), 0, "plan.suites must be non-empty")
        ids = []
        for suite in suites:
            with self.subTest(suite=suite):
                self.assertIsInstance(suite, dict)
                suite_id = suite.get("id")
                self.assertIsInstance(suite_id, str, "suite.id must be a string")
                self.assertRegex(suite_id, SUITE_ID_RE,
                                 "suite.id must be a stable lowercase id")
                ids.append(suite_id)
                cmd = suite_commands(suite)
                self.assertIsNotNone(cmd, "suite.command must be str or argv list")
                self.assertTrue(cmd.strip(), "suite.command must be non-empty")
                self.assertIn(suite.get("tier"), TIERS,
                              f"suite.tier must be one of {sorted(TIERS)}")
        self.assertEqual(len(ids), len(set(ids)), "suite IDs must be unique")

    def test_plan_reports_prerequisite_readiness_and_preparation(self):
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        suites = parsed.get("suites", [])
        self.assertTrue(suites, "need suites to check readiness")
        for suite in suites:
            with self.subTest(suite=suite.get("id")):
                prereqs = suite.get("prerequisites")
                self.assertIsInstance(prereqs, list,
                                      "suite.prerequisites must be a list")
                for prereq in prereqs:
                    self.assertIsInstance(prereq, dict)
                    self.assertTrue(str(prereq.get("name", "")).strip(),
                                    "prerequisite.name must be non-empty")
                    self.assertIsInstance(prereq.get("ready"), bool,
                                          "prerequisite.ready must be bool")
                prep = suite.get("preparation")
                self.assertIsInstance(prep, dict,
                                      "suite.preparation must be an object")
                self.assertIsInstance(prep.get("required"), bool,
                                      "preparation.required must be bool")
                self.assertIsInstance(prep.get("actions"), list,
                                      "preparation.actions must be a list")

    def test_plan_covers_required_fast_suites(self):
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        blob = json.dumps(parsed).lower()
        for marker in REQUIRED_SUITE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, blob,
                              f"plan must still cover required suite {marker!r}")

    def test_dry_run_is_plan_alias(self):
        plan_completed, plan = plan_json(["--plan", "--json"])
        dry_completed, dry = plan_json(["--dry-run", "--json"])
        self.assertEqual(plan_completed.returncode, 0, "need --plan --json exit 0")
        self.assertEqual(dry_completed.returncode, 0,
                         "--dry-run --json must exit 0 like --plan --json")
        self.assertIsNotNone(plan, "need valid --plan JSON")
        self.assertIsNotNone(dry, "need valid --dry-run JSON")
        self.assertEqual(plan.get("schema_version"), dry.get("schema_version"),
                         "alias must share schema_version")
        plan_ids = [s.get("id") for s in plan.get("suites", [])]
        dry_ids = [s.get("id") for s in dry.get("suites", [])]
        self.assertEqual(plan_ids, dry_ids,
                         "--dry-run must list identical suite IDs in order")
        plan_cmds = [suite_commands(s) for s in plan.get("suites", [])]
        dry_cmds = [suite_commands(s) for s in dry.get("suites", [])]
        self.assertEqual(plan_cmds, dry_cmds,
                         "--dry-run must list identical exact commands")

    def test_plan_order_is_stable_across_runs(self):
        _, first = plan_json(["--plan", "--json"])
        _, second = plan_json(["--plan", "--json"])
        self.assertIsNotNone(first, "need valid plan JSON")
        self.assertIsNotNone(second, "need valid plan JSON on rerun")
        first_ids = [s.get("id") for s in first.get("suites", [])]
        second_ids = [s.get("id") for s in second.get("suites", [])]
        self.assertEqual(first_ids, second_ids, "suite order must be stable")

    def test_plan_needs_no_network_or_credentials(self):
        env = scrubbed_env()
        _, parsed = plan_json(["--plan", "--json"], env=env)
        self.assertIsNotNone(parsed,
                              "plan must succeed with no credential vars in env")
        self.assertTrue(parsed.get("suites"),
                        "credential-free plan must still list suites")

    def test_bare_plan_matches_bare_dry_run_without_json(self):
        plan = run_verify_fast(["--plan"])
        dry = run_verify_fast(["--dry-run"])
        self.assertEqual(plan.returncode, 0, "bare --plan must exit 0")
        self.assertEqual(dry.returncode, 0, "bare --dry-run must exit 0 like --plan")
        self.assertTrue(plan.stdout.strip(), "bare --plan must print a plan")
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        for suite_id in [s.get("id") for s in parsed.get("suites", [])[:5]]:
            with self.subTest(suite=suite_id):
                self.assertIn(suite_id, plan.stdout,
                              "human-readable plan must name stable IDs")
                self.assertIn(suite_id, dry.stdout,
                              "--dry-run human output must name stable IDs")


class PlanPurityTest(unittest.TestCase):
    """Plan/dry-run never executes suites, downloads, restores or writes."""

    def test_plan_invokes_no_suite_or_downloader_commands(self):
        with scratch_dir() as tmp:
            spy_dir = Path(tmp) / "bin"
            spy_dir.mkdir()
            log = Path(tmp) / "spy.log"
            log.write_text("")
            real = {name: shutil.which(name) or f"/usr/bin/{name}"
                    for name in ("npm", "curl", "wget", "tar", "node")}
            for name, target in real.items():
                write_spy(spy_dir, name,
                          DELEGATE_HEADER +
                          f'echo "BLOCKED:{name}" >&2\nexit 99\n'
                          if name in ("npm", "curl", "wget", "tar") else
                          DELEGATE_HEADER + f'exec "{target}" "$@"\n')
            env = spy_path_with(scrubbed_env({"SPY_LOG": str(log)}), spy_dir)
            completed, parsed = plan_json(["--plan", "--json"], env=env)
            self.assertEqual(completed.returncode, 0,
                             f"plan must exit 0 under blocked downloaders: "
                             f"{completed.stderr[:500]!r}")
            self.assertIsNotNone(parsed, "plan must stay valid JSON under spies")
            logged = log.read_text()
            for blocked in ("npm", "curl", "wget", "tar"):
                with self.subTest(tool=blocked):
                    self.assertNotRegex(
                        logged, rf"(?m)^{re.escape(blocked)}\b",
                        f"plan must never invoke {blocked}")

    def test_plan_writes_no_files(self):
        sentinels = repo_write_sentinels()
        before_listing = sorted(str(p) for p in ROOT.iterdir())
        before = snapshot_paths(sentinels)
        completed, parsed = plan_json(["--plan", "--json"])
        self.assertEqual(completed.returncode, 0, "need plan exit 0")
        self.assertIsNotNone(parsed, "need valid plan JSON")
        after = snapshot_paths(sentinels)
        self.assertEqual(before, after,
                         "plan mode must not create/modify sentinel paths")
        self.assertEqual(sorted(str(p) for p in ROOT.iterdir()), before_listing,
                         "plan mode must not add/remove repo-root entries")

    def test_plan_does_no_cache_cleanup(self):
        caches = [ROOT / "__pycache__" / ".plan-purity-probe",
                  ROOT / "omniroute" / "__pycache__" / ".plan-purity-probe"]
        created = []
        try:
            for probe in caches:
                probe.parent.mkdir(parents=True, exist_ok=True)
                probe.write_text("plan must not delete cache sentinels\n")
                created.append(probe)
            completed, parsed = plan_json(["--plan", "--json"])
            self.assertEqual(completed.returncode, 0, "need plan exit 0")
            self.assertIsNotNone(parsed, "need valid plan JSON")
            for probe in created:
                with self.subTest(probe=str(probe)):
                    self.assertTrue(probe.is_file(),
                                    "plan must not remove cache-cleanup targets")
        finally:
            for probe in created:
                try:
                    probe.unlink()
                except FileNotFoundError:
                    pass

    def test_dry_run_is_equally_pure(self):
        sentinels = repo_write_sentinels()
        before = snapshot_paths(sentinels)
        completed, parsed = plan_json(["--dry-run", "--json"])
        self.assertEqual(completed.returncode, 0, "need --dry-run exit 0")
        self.assertIsNotNone(parsed, "need valid --dry-run JSON")
        self.assertEqual(snapshot_paths(sentinels), before,
                         "--dry-run must not write sentinel paths either")
        self.assertNotIn("restoring", completed.stdout.lower(),
                         "--dry-run must not restore toolkit files")
        self.assertNotIn("installing", completed.stdout.lower(),
                         "--dry-run must not install anything")

    def test_plan_is_bounded_and_deterministic(self):
        start = time.monotonic()
        first, first_parsed = plan_json(["--plan", "--json"])
        mid = time.monotonic()
        second, second_parsed = plan_json(["--plan", "--json"])
        end = time.monotonic()
        self.assertEqual(first.returncode, 0, "need plan exit 0")
        self.assertEqual(second.returncode, 0, "need plan exit 0 on rerun")
        self.assertLess(mid - start, PLAN_TIMEOUT, "first plan exceeded bound")
        self.assertLess(end - mid, PLAN_TIMEOUT, "rerun plan exceeded bound")
        self.assertIsNotNone(first_parsed, "need valid plan JSON")
        self.assertIsNotNone(second_parsed, "need valid plan JSON on rerun")
        self.assertEqual(first_parsed, second_parsed,
                         "plan must be deterministic within one checkout")


class SuiteInventoryTest(unittest.TestCase):
    """--list, plan JSON and execution share one repo-owned inventory."""

    def test_single_inventory_source_exists_and_is_repo_owned(self):
        body = VERIFY_FAST.read_text()
        found_files = [c for c in INVENTORY_CANDIDATES if (ROOT / c).is_file()]
        in_script = bool(re.search(r"SUITE_INVENTORY|suite_inventory|"
                                    r"suite-inventory|SUITE_TABLE",
                                    body, re.IGNORECASE))
        sources = len(found_files) + (1 if in_script and not found_files else 0)
        self.assertGreaterEqual(
            sources, 1,
            f"need one repo-owned suite inventory: tried {INVENTORY_CANDIDATES} "
            "or a single in-script SUITE_INVENTORY site; found none")
        if found_files:
            self.assertEqual(
                len(found_files), 1,
                f"exactly one inventory file must exist, found {found_files}")
            inventory = (ROOT / found_files[0]).read_text()
            self.assertTrue(inventory.strip(), "inventory file must be non-empty")
            self.assertRegex(
                body, re.compile(re.escape(Path(found_files[0]).name)),
                "verify-fast.sh must source its repo-owned inventory file")

    def test_list_emits_plan_suite_ids_without_divergence(self):
        listed = run_verify_fast(["--list"])
        self.assertEqual(listed.returncode, 0, "--list must exit 0")
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        plan_ids = [s.get("id") for s in parsed.get("suites", [])]
        self.assertTrue(plan_ids, "need plan IDs to compare")
        list_ids = extract_list_ids(listed.stdout)
        missing = [i for i in plan_ids if i not in list_ids]
        self.assertEqual(missing, [],
                         f"--list must emit every plan suite ID; missing {missing}")
        extra = sorted(list_ids - set(plan_ids))
        self.assertEqual(extra, [],
                         f"--list must not invent IDs outside the plan: {extra}")

    def test_list_branch_is_generated_not_hardcoded_prose(self):
        body = VERIFY_FAST.read_text()
        list_branch = body.split('"--list"', 1)
        self.assertEqual(len(list_branch), 2, "script must handle --list")
        branch = list_branch[1].split("exit 0", 1)[0]
        echo_lines = [ln for ln in branch.splitlines()
                      if ln.strip().startswith("echo ")]
        self.assertLessEqual(
            len(echo_lines), 3,
            f"--list branch must be generated from inventory, not "
            f"{len(echo_lines)} hardcoded echo lines")
        self.assertRegex(
            branch, re.compile(r"inventor|suite|jq|python3", re.IGNORECASE),
            "--list branch must reference the shared inventory/selection")

    def test_plan_commands_match_execution_selection(self):
        body = VERIFY_FAST.read_text()
        _, parsed = plan_json(["--plan", "--json"])
        self.assertIsNotNone(parsed, "need valid plan JSON")
        suites = parsed.get("suites", [])
        self.assertGreater(len(suites), 3, "need a real suite inventory")
        # Every planned exact command must select the same argv the script
        # would execute: the command's first token must occur in the script.
        unmatched = []
        for suite in suites:
            cmd = suite_commands(suite) or ""
            first = cmd.strip().split()
            if not first or first[0] not in body:
                unmatched.append((suite.get("id"), cmd[:80]))
        self.assertEqual(
            unmatched, [],
            f"planned commands must match script execution selection: {unmatched}")

    def test_default_execution_still_lists_required_suites(self):
        body = VERIFY_FAST.read_text().lower()
        for marker in REQUIRED_SUITE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, body,
                              f"default execution must preserve {marker!r}")


class NoPrepareRefusalTest(unittest.TestCase):
    """--no-prepare refuses before auto-restoration/install with code 3."""

    def _missing_everything_env(self, tmp):
        """Fixture: typescript probe fails, toolkit check fails, no network."""
        spy_dir = Path(tmp) / "bin"
        spy_dir.mkdir()
        log = Path(tmp) / "spy.log"
        log.write_text("")
        real_node = shutil.which("node") or "/usr/local/bin/node"
        real_python = shutil.which("python3") or "/usr/bin/python3"
        # node: fail only the typescript resolve probe, delegate the rest.
        write_spy(spy_dir, "node", DELEGATE_HEADER + textwrap_node_stub(real_node))
        # python3: fail only offline_ci_contract --check-restored, delegate rest.
        write_spy(spy_dir, "python3",
                  DELEGATE_HEADER + textwrap_python_stub(real_python))
        for name in ("npm", "curl", "wget", "tar", "sha256sum"):
            write_spy(spy_dir, name,
                      DELEGATE_HEADER + f'echo "BLOCKED:{name}" >&2\nexit 99\n')
        env = spy_path_with(scrubbed_env({"SPY_LOG": str(log)}), spy_dir)
        return env, log

    def test_no_prepare_missing_prereqs_exit_code_and_next_action(self):
        with scratch_dir() as tmp:
            env, _ = self._missing_everything_env(tmp)
            completed = run_verify_fast(["--no-prepare"], env=env,
                                        timeout=PLAN_TIMEOUT)
            combined = (completed.stdout + "\n" + completed.stderr).lower()
            self.assertEqual(
                completed.returncode, NO_PREPARE_EXIT,
                f"--no-prepare with missing prereqs must exit {NO_PREPARE_EXIT}, "
                f"got {completed.returncode}: {combined[:800]!r}")
            self.assertRegex(
                combined, re.compile(r"next[^:\n]*:|next action|run .*without "
                                     r"--no-prepare|to (fix|proceed|restore|install)",
                                     re.IGNORECASE),
                "refusal must print a next action before any preparation")
            self.assertNotIn("verify-fast: pass", combined,
                             "a refusal must never report passing")
            self.assertNotIn("parse ok", combined,
                             "a refusal must never report passing")

    def test_no_prepare_names_missing_prerequisites(self):
        with scratch_dir() as tmp:
            env, _ = self._missing_everything_env(tmp)
            _, parsed = plan_json(["--plan", "--json"], env=env)
            completed = run_verify_fast(["--no-prepare"], env=env,
                                        timeout=PLAN_TIMEOUT)
            combined = (completed.stdout + "\n" + completed.stderr).lower()
            self.assertIsNotNone(parsed, "need valid plan JSON under fixture")
            not_ready = sorted({
                str(p.get("name", "")).lower()
                for s in parsed.get("suites", [])
                for p in s.get("prerequisites", [])
                if p.get("ready") is False})
            self.assertTrue(not_ready,
                            "missing-everything fixture must mark prereqs not-ready")
            for name in not_ready:
                with self.subTest(prereq=name):
                    key = name.split()[0] if name else name
                    self.assertIn(key, combined,
                                  f"--no-prepare must name missing prereq {name!r}")

    def test_no_prepare_never_installs_or_restores(self):
        with scratch_dir() as tmp:
            env, log = self._missing_everything_env(tmp)
            sentinels = repo_write_sentinels()
            before = snapshot_paths(sentinels)
            completed = run_verify_fast(["--no-prepare"], env=env,
                                        timeout=PLAN_TIMEOUT)
            combined = (completed.stdout + "\n" + completed.stderr).lower()
            self.assertEqual(completed.returncode, NO_PREPARE_EXIT,
                             "need the stable refusal exit code")
            logged = log.read_text()
            for blocked in ("npm", "curl", "wget", "tar", "sha256sum"):
                with self.subTest(tool=blocked):
                    self.assertNotRegex(
                        logged, rf"(?m)^{re.escape(blocked)}\b",
                        f"--no-prepare must never invoke {blocked}")
            self.assertNotIn("restoring stale", combined,
                             "--no-prepare must not auto-restore toolkit files")
            self.assertNotIn("installing pinned typescript", combined,
                             "--no-prepare must not auto-install typescript")
            self.assertEqual(snapshot_paths(sentinels), before,
                             "--no-prepare refusal must not write sentinel paths")

    def test_no_prepare_never_reports_skipped_or_unknown_as_passing(self):
        with scratch_dir() as tmp:
            env, _ = self._missing_everything_env(tmp)
            completed = run_verify_fast(["--no-prepare"], env=env,
                                        timeout=PLAN_TIMEOUT)
            combined = (completed.stdout + "\n" + completed.stderr).lower()
            self.assertNotEqual(completed.returncode, 0,
                                "missing prereqs must not exit 0")
            for verdict in ("pass", "ok", "success"):
                if verdict in combined:
                    self.assertIn("fail", combined,
                                  f"refusal output mentions {verdict!r} without "
                                  "a failure verdict; skipped/unknown must not "
                                  "read as passing")

    def test_no_prepare_parse_only_with_complete_prereqs_stays_compatible(self):
        # Deterministic complete fixture: force both readiness probes to
        # succeed regardless of real checkout state, then --no-prepare
        # --parse-only must run the parse gate (0/1), never the refusal
        # code and never a silent skip, without restoring/installing.
        with scratch_dir() as tmp:
            spy_dir = Path(tmp) / "bin"
            spy_dir.mkdir()
            log = Path(tmp) / "spy.log"
            log.write_text("")
            real_node = shutil.which("node") or "/usr/local/bin/node"
            real_python = shutil.which("python3") or "/usr/bin/python3"
            write_spy(spy_dir, "node", DELEGATE_HEADER +
                      'if [[ "$*" == *"require.resolve"*typescript* ]]; then\n'
                      '  exit 0\n'
                      'fi\n'
                      f'exec "{real_node}" "$@"\n')
            write_spy(spy_dir, "python3", DELEGATE_HEADER +
                      'if [[ "$*" == *offline_ci_contract*--check-restored* ]]; '
                      'then\n'
                      '  exit 0\n'
                      'fi\n'
                      f'exec "{real_python}" "$@"\n')
            env = spy_path_with(scrubbed_env({"SPY_LOG": str(log)}), spy_dir)
            completed = run_verify_fast(["--no-prepare", "--parse-only"],
                                        env=env, timeout=PARSE_TIMEOUT)
        # Complete fixture: refusal must not fire when nothing is missing;
        # the parse gate itself decides pass/fail (0/1), never the refusal
        # code and never a silent skip.
        self.assertIn(completed.returncode, (0, 1),
                      f"--no-prepare --parse-only with complete prereqs must "
                      f"run the parse gate (0/1), got {completed.returncode}: "
                      f"{completed.stderr[:400]!r}")
        combined = (completed.stdout + "\n" + completed.stderr).lower()
        self.assertNotIn("restoring stale", combined,
                         "--no-prepare must not restore even on success path")
        self.assertNotIn("installing pinned typescript", combined,
                         "--no-prepare must not install even on success path")


def textwrap_node_stub(real_node):
    return (
        'if [[ "$*" == *"require.resolve"*typescript* ]]; then\n'
        '  echo "spy: typescript probe forced missing" >&2\n'
        '  exit 1\n'
        'fi\n'
        f'exec "{real_node}" "$@"\n'
    )


def textwrap_python_stub(real_python):
    return (
        'if [[ "$*" == *offline_ci_contract*--check-restored* ]]; then\n'
        '  echo "spy: toolkit check forced missing" >&2\n'
        '  exit 1\n'
        'fi\n'
        f'exec "{real_python}" "$@"\n'
    )


class FlagsHelpCompatibilityTest(unittest.TestCase):
    """Invalid combinations fail clearly; help is concise and explicit."""

    def test_json_without_plan_or_list_fails_clearly(self):
        completed = run_verify_fast(["--json"])
        self.assertEqual(completed.returncode, INVALID_USAGE_EXIT,
                         "--json alone must exit 2")
        combined = (completed.stdout + "\n" + completed.stderr).lower()
        self.assertRegex(combined, re.compile(r"usage|invalid|requires? --plan"),
                         "--json alone must say it needs --plan/--dry-run/--list")

    def test_plan_and_no_prepare_cannot_combine(self):
        for flags in (["--plan", "--no-prepare"],
                      ["--dry-run", "--no-prepare"],
                      ["--list", "--no-prepare"]):
            with self.subTest(flags=flags):
                completed = run_verify_fast(flags)
                self.assertEqual(
                    completed.returncode, INVALID_USAGE_EXIT,
                    f"{' '.join(flags)} must exit 2 (plan/list never prepare)")
                combined = (completed.stdout + "\n" + completed.stderr).lower()
                self.assertRegex(
                    combined, re.compile(r"usage|invalid|cannot|mutually|combine"),
                    "invalid combination must fail with a clear message")

    def test_unknown_flags_fail_clearly(self):
        for flags in (["--bogus-flag"], ["--plan", "--bogus"], ["--no-prepare",
                                                                "--bogus"]):
            with self.subTest(flags=flags):
                completed = run_verify_fast(flags)
                self.assertEqual(completed.returncode, INVALID_USAGE_EXIT,
                                 f"{' '.join(flags)} must exit 2")
                combined = (completed.stdout + "\n" + completed.stderr).lower()
                self.assertRegex(combined, re.compile(r"usage|unknown|invalid"),
                                 "unknown flags must print usage-like guidance")

    def test_help_is_concise_with_examples(self):
        for flags in (["--help"], ["-h"]):
            with self.subTest(flags=flags):
                completed = run_verify_fast(flags)
                self.assertEqual(completed.returncode, 0,
                                 f"{' '.join(flags)} must exit 0")
                out = completed.stdout
                self.assertTrue(out.strip(), "help must print to stdout")
                self.assertLessEqual(len(out.splitlines()), HELP_MAX_LINES,
                                     "help must stay concise")
                self.assertLessEqual(len(out.encode()), HELP_MAX_BYTES,
                                     "help must stay concise")
                lowered = out.lower()
                for example in ("--plan --json", "--dry-run", "--no-prepare",
                                "--parse-only"):
                    self.assertIn(example, lowered,
                                  f"help must show a {example} example")

    def test_help_disclaims_normal_execution_is_not_read_only(self):
        completed = run_verify_fast(["--help"])
        self.assertEqual(completed.returncode, 0, "need --help exit 0")
        lowered = completed.stdout.lower()
        self.assertRegex(
            lowered,
            re.compile(r"not read-only|not readonly|may (install|restore|write|"
                       r"download)|prepar(es|ation)|writes?", re.IGNORECASE),
            "help must explicitly disclaim: normal execution prepares/writes")
        self.assertNotRegex(
            lowered, re.compile(r"verify-fast is (fully |wholly )?read-only"),
            "help must not claim ordinary verification is wholly read-only")

    def test_parse_only_and_default_execution_preserved(self):
        body = VERIFY_FAST.read_text()
        self.assertIn('--parse-only', body, "script must keep --parse-only")
        self.assertIn('MODE="full"', body, "default mode must stay full")
        self.assertIn('sync-toolkit.sh', body,
                      "default execution must keep toolkit preparation")
        self.assertIn('typescript@', body,
                      "default execution must keep typescript preparation")
        completed = run_verify_fast(["--help"])
        self.assertEqual(completed.returncode, 0, "need --help exit 0")
        self.assertIn("--parse-only", completed.stdout,
                      "help must document preserved --parse-only")


class EntrypointDiscoverabilityTest(unittest.TestCase):
    """Focused offline contract command via the Makefile/README entrypoint."""

    def _plan_target(self):
        makefile = MAKEFILE.read_text()
        for target in MAKE_PLAN_TARGETS:
            if re.search(rf"(?m)^{re.escape(target)}\s*:", makefile):
                return target
        return None

    def test_makefile_exposes_focused_offline_contract_target(self):
        target = self._plan_target()
        self.assertIsNotNone(
            target,
            f"Makefile must define one of {list(MAKE_PLAN_TARGETS)}")
        makefile = MAKEFILE.read_text()
        recipe = makefile.split(f"{target}:", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(
            recipe, re.compile(r"--plan|--dry-run|inventory|contract",
                               re.IGNORECASE),
            "plan target recipe must invoke planning, not full execution")
        self.assertNotRegex(
            recipe, re.compile(r"verify-fast\.sh\s*$"),
            "plan target must not run bare full verify-fast")

    def test_contract_target_is_offline_pure_and_bounded(self):
        target = self._plan_target()
        self.assertIsNotNone(target, "need the Makefile plan target")
        with scratch_dir() as tmp:
            spy_dir = Path(tmp) / "bin"
            spy_dir.mkdir()
            log = Path(tmp) / "spy.log"
            log.write_text("")
            for name in ("npm", "curl", "wget", "tar"):
                write_spy(spy_dir, name,
                          DELEGATE_HEADER + f'echo "BLOCKED:{name}" >&2\nexit 99\n')
            env = spy_path_with(scrubbed_env({"SPY_LOG": str(log)}), spy_dir)
            sentinels = repo_write_sentinels()
            before = snapshot_paths(sentinels)
            start = time.monotonic()
            completed = run_make(target, env=env)
            elapsed = time.monotonic() - start
            self.assertEqual(completed.returncode, 0,
                             f"make {target} must exit 0 offline: "
                             f"{completed.stderr[:500]!r}")
            self.assertLess(elapsed, MAKE_TIMEOUT, "contract target exceeded bound")
            self.assertEqual(snapshot_paths(sentinels), before,
                             "contract target must not write sentinel paths")
            logged = log.read_text()
            for blocked in ("npm", "curl", "wget", "tar"):
                with self.subTest(tool=blocked):
                    self.assertNotRegex(
                        logged, rf"(?m)^{re.escape(blocked)}\b",
                        f"make {target} must never invoke {blocked}")

    def test_contract_command_is_discoverable(self):
        target = self._plan_target()
        self.assertIsNotNone(target, "need the Makefile plan target")
        makefile = MAKEFILE.read_text()
        helps = run_verify_fast(["--help"])
        readme = README.read_text() if README.is_file() else ""
        candidates = (makefile + "\n" + helps.stdout + "\n" + readme).lower()
        self.assertIn(f"make {target}".lower(), candidates,
                      f"`make {target}` must be discoverable from "
                      "Makefile/README/--help")
        self.assertIn("--plan", candidates,
                      "--plan must be discoverable from Makefile/README/--help")

    def test_existing_fast_entrypoints_still_present(self):
        makefile = MAKEFILE.read_text()
        self.assertRegex(makefile, re.compile(r"(?m)^verify-fast\s*:"),
                         "Makefile must keep verify-fast")
        self.assertRegex(makefile, re.compile(r"(?m)^verify-fast-parse\s*:"),
                         "Makefile must keep verify-fast-parse")
        listed = run_verify_fast(["--list"])
        self.assertEqual(listed.returncode, 0, "--list must stay exit 0")
        self.assertTrue(listed.stdout.strip(), "--list must stay non-empty")


if __name__ == "__main__":
    unittest.main()
