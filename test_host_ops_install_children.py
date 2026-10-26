#!/usr/bin/env python3
"""Pin nested installer coverage and prove real child failures reach the parent."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
PARENT = "test_host_ops_install.sh"
MARKER = "host-ops deliberately red child"
# Literal failure-count contract: preparation imports transaction, so replacing
# transaction with an exit-1 module also makes preparation fail on import.
# The final two children run after the counter gate and exit under errexit.
EXPECTED_FAILURE_COUNTS = {
    "test_host_ops_install_transaction.py": ["2"],
    "test_host_ops_install_preparation.py": ["1"],
    "test_host_ops_prepare_first_install.sh": ["1"],
    "test_host_ops_install_manifest.py": ["1"],
    "test_host_ops_spool_mapping.py": ["1"],
    "test_host_ops_spool_carrier.py": ["1"],
    "test_host_ops_spool_provisioning.py": ["1"],
    "test_host_ops_image_binding.py": [],
    "test_host_ops_install_state.py": [],
}
EXPECTED_CHILDREN = frozenset(EXPECTED_FAILURE_COUNTS)
GUARDED_CHILD = "test_host_ops_spool_mapping.py"
UNGUARDED_CHILD = "test_host_ops_install_state.py"
# Broker behavior and staging-preflight carriers have independent suite entry
# points, not installer contracts. This pin must not require the installer to
# invoke them. The parent and this meta-test are excluded to avoid recursion.
NOT_CHILDREN = {
    PARENT,
    "test_host_ops_broker.py",
    "test_host_ops_staging_preflight_carrier.py",
    "test_host_ops_install_children.py",
}
INVOCATION = re.compile(
    r'^\s*(?P<guard>if\s+)?(?:python3|bash|sh)\s+'
    r'"\$ROOT/(?P<child>test_host_ops_[^"/]+)"(?P<tail>.*)$'
)
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
PARENT_TIMEOUT = 900


def have_visudo():
    # Same order as the parent: command -v, then executable /usr/sbin fallback.
    return bool(shutil.which("visudo")) or os.access("/usr/sbin/visudo", os.X_OK)


def in_ci():
    return bool(os.environ.get("GITHUB_ACTIONS"))


def existing_names(root=ROOT):
    return {path.name for path in root.glob("test_host_ops_*") if path.is_file()}


def invocation_matches(parent_text):
    return [match for line in parent_text.splitlines()
            if (match := INVOCATION.match(line))]


def check_parent(parent_text, names):
    invoked = {match["child"] for match in invocation_matches(parent_text)}
    # Keep the nine known contracts pinned even if both a file and call vanish.
    children = (names - NOT_CHILDREN) | EXPECTED_CHILDREN
    return sorted(children - invoked), sorted(invoked - names)


def swallowed_children(parent_text):
    swallowed = []
    for match in invocation_matches(parent_text):
        # Guarded calls must feed their status into the existing if/else failure
        # counter. Unguarded calls must retain errexit. Accept neither `|| true`
        # nor a trailing `; true` (including on a guarded condition).
        expected = r'\s*;\s*then\s*(?:#.*)?' if match["guard"] else r'\s*(?:#.*)?'
        if not re.fullmatch(expected, match["tail"]):
            swallowed.append(match["child"])
    return sorted(swallowed)


def without_invocation(parent_text, child):
    return "\n".join(
        line for line in parent_text.splitlines()
        if not ((match := INVOCATION.match(line)) and match["child"] == child)
    ) + "\n"


def clean_env():
    # Do not let shell startup files hide visudo or inherited Git settings send
    # fixture commits to the real checkout. Python imports stay in the fixture.
    env = {key: value for key, value in os.environ.items()
           if key not in {"BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME"}
           and not key.startswith(("GIT_", "BASH_FUNC_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               PYTHONDONTWRITEBYTECODE="1")
    return env


def run_bounded(argv, *, cwd, env, timeout):
    # Drain one merged pipe incrementally: both retained output and wall time
    # are bounded. Kill the whole process group on overflow/timeout, not just
    # bash while its children continue writing into a removed fixture.
    output = bytearray()
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AssertionError(f"{argv[0]} exceeded {timeout}s")
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            if len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                                raise AssertionError(f"{argv[0]} exceeded output limit")
                            output.extend(chunk)
            remaining = max(0.001, deadline - time.monotonic())
            returncode = process.wait(timeout=remaining)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return subprocess.CompletedProcess(argv, returncode,
                                       output.decode("utf-8", errors="replace"), "")


def copy_tree(dest):
    tree = dest / "tree"
    tree.mkdir()
    # Copy only the offline installer's inputs, never .git, caches, nested
    # checkouts, credentials, or the entire working directory.
    paths = [ROOT / PARENT, *(ROOT / name for name in sorted(existing_names() - NOT_CHILDREN)),
             ROOT / "host-ops", ROOT / "deploy/paperclip-immutable"]
    for source in paths:
        candidates = [source, *source.rglob("*")] if source.is_dir() else [source]
        if any(path.is_symlink() for path in candidates):
            raise AssertionError(f"fixture input must not be a symlink: {source.name}")
        target = tree / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache"))
        else:
            shutil.copy2(source, target)
    # The manifest child's live-tree control uses git ls-tree on HEAD. Give it
    # new local fixture history; never import source Git objects or metadata.
    git = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
           "-c", "commit.gpgSign=false", "-c", "user.name=Host Ops Fixture",
           "-c", "user.email=fixture@example.invalid"]
    for args in (("init", "-q", "--template="), ("add", "--", "."),
                 ("commit", "-qm", "Offline installer fixture")):
        result = run_bounded(git + list(args), cwd=tree, env=clean_env(), timeout=60)
        if result.returncode:
            raise AssertionError(f"fixture git {args[0]} failed: {failures(result)}")
    return tree


def neuter(path, child):
    if path.suffix == ".sh":
        body = f'#!/usr/bin/env bash\nprintf "%s\\n" "{MARKER}: {child}" >&2\nexit 1\n'
    else:
        body = f'import sys\nprint("{MARKER}: {child}", file=sys.stderr)\nsys.exit(1)\n'
    path.write_text(body)


def run_parent(tree):
    env = clean_env()
    tmp = tree.parent / "tmp"
    tmp.mkdir(exist_ok=True)
    env["TMPDIR"] = str(tmp)
    return run_bounded(["bash", PARENT], cwd=tree, env=env, timeout=PARENT_TIMEOUT)


def failures(result):
    lines = result.stdout.splitlines()
    named = [line for line in lines if line.startswith(("FAIL", "REFUSED", "Traceback"))]
    return "\n".join(named or lines[-10:])[-8000:]


class InvocationPinTests(unittest.TestCase):
    def setUp(self):
        self.parent_text = (ROOT / PARENT).read_text()
        self.names = existing_names()

    def test_pin_sees_all_nine_existing_nested_children(self):
        self.assertEqual(sorted(EXPECTED_CHILDREN - self.names), [])
        self.assertEqual(len(EXPECTED_CHILDREN), 9)

    def test_parent_invokes_every_nested_child(self):
        uninvoked, dangling = check_parent(self.parent_text, self.names)
        self.assertEqual(uninvoked, [], f"nested child not invoked by {PARENT}")
        self.assertEqual(dangling, [], f"{PARENT} invokes a child that does not exist")

    def test_removing_each_invocation_fails_by_name(self):
        for child in sorted(EXPECTED_CHILDREN):
            with self.subTest(child=child):
                mutated = without_invocation(self.parent_text, child)
                self.assertNotEqual(mutated, self.parent_text)
                self.assertEqual(check_parent(mutated, self.names), ([child], []))

    def test_removing_both_known_child_and_call_cannot_erase_the_pin(self):
        for child in sorted(EXPECTED_CHILDREN):
            with self.subTest(child=child):
                mutated = without_invocation(self.parent_text, child)
                self.assertEqual(check_parent(mutated, self.names - {child}), ([child], []))

    def test_new_orphan_child_fails_by_name(self):
        orphan = "test_host_ops_new_orphan.py"
        self.assertEqual(check_parent(self.parent_text, self.names | {orphan}), ([orphan], []))

    def test_invocation_of_a_missing_child_fails_by_name(self):
        self.assertEqual(check_parent(self.parent_text, self.names - {UNGUARDED_CHILD}),
                         ([], [UNGUARDED_CHILD]))

    def test_standalone_suites_and_meta_test_are_not_nested_contracts(self):
        self.assertEqual(check_parent(self.parent_text, self.names - NOT_CHILDREN), ([], []))
        for name in NOT_CHILDREN:
            with self.subTest(name=name):
                self.assertNotIn(name, {match["child"] for match in invocation_matches(self.parent_text)})

    def test_no_swallowed_child_invocation(self):
        self.assertEqual(swallowed_children(self.parent_text), [],
                         f"{PARENT} swallows a nested child status")

    def test_swallowed_guarded_and_unguarded_statuses_are_rejected(self):
        for child, prefix, suffix in ((GUARDED_CHILD, "if ", "; then"),
                                       (UNGUARDED_CHILD, "", "")):
            for swallow in (" || true", "; true"):
                with self.subTest(child=child, swallow=swallow):
                    line = f'{prefix}python3 "$ROOT/{child}"{swallow}{suffix}'
                    self.assertEqual(swallowed_children(line), [child])

    def test_comment_only_reference_is_not_an_invocation(self):
        for child in sorted(EXPECTED_CHILDREN):
            with self.subTest(child=child):
                text = without_invocation(self.parent_text, child)
                text += f'# python3 "$ROOT/{child}"\n'
                self.assertEqual(check_parent(text, self.names), ([child], []))


class VisudoAvailabilityTests(unittest.TestCase):
    def test_ci_runner_provides_visudo_for_red_propagation(self):
        if not in_ci():
            self.skipTest("only enforced in CI; developer containers may lack visudo")
        self.assertTrue(have_visudo(), "CI must provide real visudo (PATH or /usr/sbin/visudo); "
                        "red-child propagation coverage must not silently skip")


class RedChildPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not have_visudo():
            message = "needs real visudo (PATH or /usr/sbin/visudo); no stub is permitted"
            if in_ci():
                raise AssertionError(message)
            raise unittest.SkipTest(message)

    def test_unmutated_tree_passes(self):
        with tempfile.TemporaryDirectory(prefix="host-ops-children-") as tmp:
            result = run_parent(copy_tree(Path(tmp)))
        self.assertEqual(result.returncode, 0, failures(result))
        self.assertIn("host-ops installer contract tests passed", result.stdout)

    def test_red_guarded_child_fails_parent(self):
        self.assert_red_child_fails_parent(GUARDED_CHILD)

    def test_red_unguarded_child_fails_parent(self):
        self.assert_red_child_fails_parent(UNGUARDED_CHILD)

    def test_every_red_child_fails_parent(self):
        for child in sorted(existing_names() - NOT_CHILDREN):
            with self.subTest(child=child):
                self.assert_red_child_fails_parent(child)

    def assert_red_child_fails_parent(self, child):
        with tempfile.TemporaryDirectory(prefix="host-ops-children-") as tmp:
            tree = copy_tree(Path(tmp))
            neuter(tree / child, child)
            result = run_parent(tree)
        self.assertEqual(result.returncode, 1, f"{PARENT} did not propagate {child}: {failures(result)}")
        self.assertIn(f"{MARKER}: {child}", result.stdout, "the deliberately red child never ran")
        self.assertNotIn("host-ops installer contract tests passed", result.stdout)
        counts = re.findall(r"^(\d+) host-ops installer contract tests failed$", result.stdout, re.M)
        self.assertEqual(counts, EXPECTED_FAILURE_COUNTS[child], failures(result))


if __name__ == "__main__":
    unittest.main()
