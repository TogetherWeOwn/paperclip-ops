"""Immutable, test-first acceptance contract for public_safety.py --json.

Run offline: python3 -B -m unittest -v test_public_safety_verdict_acceptance
The builder consumes this suite unchanged; it intentionally fails before the
JSON feature exists. Only the existing scanner is copied into temporary trees;
no repository files, Git index, workflows, host services or network are changed.

Frozen JSON contract (opt-in; default prose remains unchanged):
- Exactly one object on stdout, no stderr, schema ``public-safety.v1``.
- Keys: schema, outcome, files_scanned, finding_count, counts_by_code, findings,
  diagnostic, truncated. Outcomes: clean, findings, refused.
- Complete scans publish integer files_scanned/finding_count, a counts_by_code
  mapping containing only nonzero category totals, and diagnostic=null.
- findings contains at most 100 records, sorted by (path, code), each with only
  code, path (repository-relative), retryable=false, next_action. Actions are
  nonempty, single-line, at most 240 characters; they never contain input text.
- Totals describe the whole scan, not the truncated list. truncated is true
  exactly when more than 100 findings exist. Clean/findings exit 0/1.
- An OSError during traversal or file reading yields refused, nonzero exit,
  files_scanned/finding_count/counts_by_code=null, findings=[], truncated=false,
  and diagnostic={code: scan-io-error, path: relative path or null,
  retryable: true, next_action: bounded safe remediation}. Never report partial
  counts, raw exception strings, matched content or absolute paths in JSON.
- Category codes below identify existing rules only; there are no new rules.

The assertions catch lost detection rules, prose regressions, false-clean IO
failures, partial/invented measurements, truncation-before-counting, content
leaks, and widened or removed byte-pinned ZIP exemptions. CLI entrypoints are
real; only deterministic filesystem failures are injected at pathlib's IO edge
so the refusal tests work even when the test runner can read chmod-000 files.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT_REL = ".github/scripts/public_safety.py"
SCRIPT = ROOT / SCRIPT_REL
TOP_KEYS = {
    "schema", "outcome", "files_scanned", "finding_count", "counts_by_code",
    "findings", "diagnostic", "truncated",
}
ENTRY_KEYS = {"code", "path", "retryable", "next_action"}
CONTENT_MARKER = "synthetic-content-must-not-appear-in-diagnostics"
INTERNAL_URL = "https://fixture." + "internal"
INTERNAL_CARD = "TO" + "G-86420"
ZIP_FIXTURES = (
    ("staging-migrate-apply-claim.upload-artifact.zip",
     "63b113e94c8952b5e709e75f3caddf6fe59f47c7aeeb0db08dee8c7f5a1ddb98",
     "staging-migrate-apply-claim.json"),
    ("staging-migrate-plan.upload-artifact.zip",
     "7759abe812f7669d36bfe38a644616a946b363287ea24a46b4b412b06a00efe8",
     "staging-migrate-plan.json"),
)
FIXTURE_DIR = "protection-rule/test/fixtures"

# Preserve the real scanner/entrypoint; inject only the operation that fails.
FAULT_RUNNER = r"""
import pathlib
import runpy
import sys

script, mode, target, *arguments = sys.argv[1:]
original_read = pathlib.Path.read_bytes
original_rglob = pathlib.Path.rglob

def read_bytes(path):
    if path.name == target:
        raise OSError("synthetic-content-must-not-appear-in-diagnostics")
    return original_read(path)

def rglob(path, pattern, *args, **kwargs):
    yield from original_rglob(path, pattern, *args, **kwargs)
    raise OSError("synthetic-content-must-not-appear-in-diagnostics")

if mode == "read":
    pathlib.Path.read_bytes = read_bytes
elif mode == "walk":
    pathlib.Path.rglob = rglob
else:
    raise AssertionError("unknown test fault")
sys.argv = [script, *arguments]
runpy.run_path(script, run_name="__main__")
"""


@contextmanager
def overlay(files=None):
    """Own and remove every file, including on failed assertions/subprocesses."""
    parent = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
              or os.environ.get("TMPDIR") or tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="public-safety-acceptance-",
                                     dir=parent) as directory:
        root = Path(directory)
        script = root / SCRIPT_REL
        script.parent.mkdir(parents=True)
        script.write_bytes(SCRIPT.read_bytes())
        for relative, payload in (files or {}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        yield root


def run_scan(root, *arguments, fault=None):
    script = str(root / SCRIPT_REL)
    if fault is None:
        command = [sys.executable, "-B", script, *arguments]
    else:
        command = [sys.executable, "-B", "-c", FAULT_RUNNER,
                   script, fault, "z-unreadable.txt", *arguments]
    # Do not give child processes API credentials or inherited Git overrides.
    env = {"PATH": os.environ.get("PATH", os.defpath),
           "HOME": str(root), "TMPDIR": str(root), "LC_ALL": "C.UTF-8"}
    return subprocess.run(command, cwd=root, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=15, check=False)


def tree_snapshot(root):
    """Detect scanner writes; symlinks are recorded without following them."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        else:
            result[relative] = ("directory",)
    return result


class PublicSafetyVerdictAcceptance(unittest.TestCase):
    def assert_json_scan(self, root, exit_code=None, fault=None):
        proc = run_scan(root, "--json", fault=fault)
        self.assertEqual(proc.stderr, b"", "JSON mode must not leak a traceback")
        try:
            # Parsing the entire stream rejects extra JSON objects/prose.
            obj = json.loads(proc.stdout)
        except (ValueError, UnicodeError):
            self.fail("--json must emit one public-safety.v1 object, not prose "
                      f"(exit {proc.returncode})")
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], "public-safety.v1")
        self.assertIs(type(obj["truncated"]), bool)
        self.assertNotIn(CONTENT_MARKER, proc.stdout.decode("utf-8"))
        self.assertNotIn(str(root), proc.stdout.decode("utf-8"))
        if exit_code is not None:
            self.assertEqual(proc.returncode, exit_code)
        return proc, obj

    def assert_entry(self, entry, code, path, retryable=False):
        self.assertEqual(set(entry), ENTRY_KEYS)
        self.assertEqual(entry["code"], code)
        self.assertEqual(entry["path"], path)
        self.assertIs(entry["retryable"], retryable)
        action = entry["next_action"]
        self.assertIsInstance(action, str)
        self.assertTrue(action.strip())
        self.assertLessEqual(len(action), 240)
        self.assertFalse(any(ord(char) < 32 for char in action))
        for forbidden in (CONTENT_MARKER, INTERNAL_URL, INTERNAL_CARD):
            self.assertNotIn(forbidden, action)
        if path is not None:
            self.assertIsInstance(path, str)
            self.assertFalse(Path(path).is_absolute())
            self.assertNotIn("..", Path(path).parts)

    def assert_complete(self, obj, files, expected, counts=None, total=None):
        self.assertEqual(obj["outcome"], "findings" if expected else "clean")
        self.assertIs(type(obj["files_scanned"]), int)
        self.assertIs(type(obj["finding_count"]), int)
        self.assertEqual(obj["files_scanned"], files)
        self.assertEqual(obj["finding_count"], len(expected) if total is None else total)
        self.assertIsNone(obj["diagnostic"])
        self.assertEqual(len(obj["findings"]), len(expected))
        self.assertIs(obj["truncated"], (total or len(expected)) > 100)
        for entry, (path, code) in zip(obj["findings"], expected):
            self.assert_entry(entry, code, path)
        if counts is None:
            counts = {}
            for _, code in expected:
                counts[code] = counts.get(code, 0) + 1
        self.assertEqual(obj["counts_by_code"], counts)
        for value in obj["counts_by_code"].values():
            self.assertIs(type(value), int)
            self.assertGreater(value, 0)
        self.assertEqual(sum(obj["counts_by_code"].values()), obj["finding_count"])

    def test_clean_overlay_has_measured_zero_findings(self):
        with overlay({"README.md": b"Public example\n"}) as root:
            _, obj = self.assert_json_scan(root, 0)
            self.assert_complete(obj, 2, [])

    def test_private_artifact_has_one_actionable_finding(self):
        with overlay({"fixture.pem": CONTENT_MARKER.encode()}) as root:
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 2, [("fixture.pem", "private-artifact")])

    def test_internal_card_in_filename_has_a_path_finding(self):
        relative = "docs/" + INTERNAL_CARD + ".md"
        with overlay({relative: b"Public example\n"}) as root:
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 2, [(relative, "internal-task-in-path")])

    def test_internal_url_is_categorized_without_echoing_content(self):
        content = (CONTENT_MARKER + " " + INTERNAL_URL + "/example\n").encode()
        with overlay({"endpoint.txt": content}) as root:
            proc, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 2, [("endpoint.txt", "internal-endpoint")])
            self.assertNotIn(INTERNAL_URL.encode(), proc.stdout + proc.stderr)

    def test_multiple_categories_on_one_file_count_separately(self):
        content = (INTERNAL_URL + " " + INTERNAL_CARD + " " + CONTENT_MARKER).encode()
        with overlay({"fixture.pem": content}) as root:
            proc, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 2, [
                ("fixture.pem", "internal-endpoint"),
                ("fixture.pem", "internal-task"),
                ("fixture.pem", "private-artifact"),
            ])
            self.assertNotIn(INTERNAL_CARD.encode(), proc.stdout + proc.stderr)
            self.assertNotIn(INTERNAL_URL.encode(), proc.stdout + proc.stderr)

    def test_existing_detection_rules_keep_their_categories(self):
        cases = (
            ("backup-example.json", b"{}", "backup-dump"),
            ("runtime.env", b"SYNTHETIC=value", "private-artifact"),
            ("bytes.bin", b"a\0b", "unreviewed-binary"),
            ("text.txt", b"\xff", "non-utf8"),
            ("network.txt", ("10." + "23.45.67").encode(), "private-network-address"),
        )
        for relative, payload, code in cases:
            with self.subTest(code=code), overlay({relative: payload}) as root:
                _, obj = self.assert_json_scan(root, 1)
                self.assert_complete(obj, 2, [(relative, code)])

    def test_existing_example_address_exceptions_remain_clean(self):
        content = b"127.0.0.1 0.0.0.0 192.0.2.1 198.51.100.1 203.0.113.1 169.254.169.254\n"
        with overlay({"examples.txt": content}) as root:
            _, obj = self.assert_json_scan(root, 0)
            self.assert_complete(obj, 2, [])

    def test_symlink_is_a_finding_without_reading_its_target(self):
        with overlay({"target.txt": b"Public example\n"}) as root:
            (root / "link.txt").symlink_to("target.txt")
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 3, [("link.txt", "symlink-review")])

    def test_git_directory_is_not_scanned(self):
        with overlay({".git/ignored.pem": CONTENT_MARKER.encode()}) as root:
            _, obj = self.assert_json_scan(root, 0)
            self.assert_complete(obj, 1, [])

    def test_json_is_deterministic_and_sorted_by_path_and_code(self):
        with overlay({"z.pem": b"example", "a.pem": b"example"}) as root:
            first, obj = self.assert_json_scan(root, 1)
            second, _ = self.assert_json_scan(root, 1)
            self.assertEqual(first.stdout, second.stdout)
            self.assert_complete(obj, 3, [
                ("a.pem", "private-artifact"), ("z.pem", "private-artifact"),
            ])

    def test_control_characters_in_paths_are_json_escaped(self):
        relative = 'odd/line\n"quote.pem'
        with overlay({relative: b"example"}) as root:
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 2, [(relative, "private-artifact")])

    def test_exactly_100_findings_are_not_truncated(self):
        files = {f"items/{i:03d}.pem": b"example" for i in range(100)}
        with overlay(files) as root:
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 101,
                                 [(p, "private-artifact") for p in sorted(files)])

    def test_totals_include_findings_and_categories_beyond_the_bound(self):
        files = {f"items/{i:03d}.pem": b"example" for i in range(101)}
        files["z-endpoint.txt"] = INTERNAL_URL.encode()
        with overlay(files) as root:
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(
                obj, 103, [(f"items/{i:03d}.pem", "private-artifact")
                           for i in range(100)],
                counts={"private-artifact": 101, "internal-endpoint": 1}, total=102)

    def test_io_refusals_never_publish_partial_counts_or_raw_errors(self):
        files = {"a.pem": b"example", "z-unreadable.txt": b"example"}
        for fault in ("read", "walk"):
            with self.subTest(fault=fault), overlay(files) as root:
                proc, obj = self.assert_json_scan(root, fault=fault)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(obj["outcome"], "refused")
                for key in ("files_scanned", "finding_count", "counts_by_code"):
                    self.assertIsNone(obj[key], "a failed scan has unknown totals")
                self.assertEqual(obj["findings"], [])
                self.assertIs(obj["truncated"], False)
                diagnostic = obj["diagnostic"]
                self.assertIsInstance(diagnostic, dict)
                self.assertIn(diagnostic["path"], (None, "z-unreadable.txt")
                              if fault == "read" else (None,))
                self.assert_entry(diagnostic, "scan-io-error", diagnostic["path"], True)

    def test_default_prose_clean_is_byte_identical(self):
        with overlay({"README.md": b"Public example\n"}) as root:
            proc = run_scan(root)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr),
                             (0, b"public-safety: 2 files; 0 findings\n", b""))

    def test_default_prose_findings_are_byte_identical(self):
        content = (INTERNAL_URL + " " + INTERNAL_CARD + " " + CONTENT_MARKER).encode()
        with overlay({"fixture.pem": content}) as root:
            proc = run_scan(root)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (
                1, b"fixture.pem: internal endpoint; internal task identifier; "
                   b"private artifact or live environment file\n"
                   b"public-safety: 2 files; 3 findings\n", b""))

    def test_default_prose_io_errors_keep_tracebacks_and_no_summary(self):
        for fault in ("read", "walk"):
            with self.subTest(fault=fault), overlay({
                    "a.pem": b"example", "z-unreadable.txt": b"example"}) as root:
                proc = run_scan(root, fault=fault)
                self.assertNotEqual(proc.returncode, 0)
                self.assertTrue(proc.stderr.startswith(b"Traceback (most recent call last):"))
                self.assertIn(b"OSError: " + CONTENT_MARKER.encode(), proc.stderr)
                self.assertNotIn(b"public-safety:", proc.stdout)
                expected = (b"a.pem: private artifact or live environment file\n"
                            if fault == "read" else b"")
                self.assertEqual(proc.stdout, expected)

    def test_help_discovers_the_opt_in_json_mode_without_a_scan(self):
        with overlay({"fixture.pem": b"example"}) as root:
            proc = run_scan(root, "--help")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, b"")
            self.assertIn(b"--json", proc.stdout)
            self.assertIn(b"public-safety.v1", proc.stdout)
            self.assertNotIn(b"fixture.pem:", proc.stdout)
            self.assertNotIn(b"public-safety:", proc.stdout)

    def zip_files(self, archive, digest, sibling):
        raw = (ROOT / FIXTURE_DIR / archive).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest,
                         "reviewed ZIP bytes changed: requires independent review")
        return {f"{FIXTURE_DIR}/{archive}": raw,
                f"{FIXTURE_DIR}/{sibling}": (ROOT / FIXTURE_DIR / sibling).read_bytes()}

    def test_byte_pinned_zip_fixtures_are_clean_in_both_modes(self):
        for archive, digest, sibling in ZIP_FIXTURES:
            with self.subTest(archive=archive):
                files = self.zip_files(archive, digest, sibling)
                with overlay(files) as root:
                    prose = run_scan(root)
                    self.assertEqual((prose.returncode, prose.stdout, prose.stderr),
                                     (0, b"public-safety: 3 files; 0 findings\n", b""))
                    _, obj = self.assert_json_scan(root, 0)
                    self.assert_complete(obj, 3, [])

    def test_changed_zip_or_sibling_bytes_lose_the_exemption(self):
        for archive, digest, sibling in ZIP_FIXTURES:
            for changed in (archive, sibling):
                with self.subTest(archive=archive, changed=changed):
                    files = self.zip_files(archive, digest, sibling)
                    files[f"{FIXTURE_DIR}/{changed}"] += b"\npublic example\n"
                    with overlay(files) as root:
                        relative = f"{FIXTURE_DIR}/{archive}"
                        prose = run_scan(root)
                        self.assertEqual((prose.returncode, prose.stdout, prose.stderr), (
                            1, (relative + ": unreviewed binary payload\n"
                                "public-safety: 3 files; 1 findings\n").encode(), b""))
                        _, obj = self.assert_json_scan(root, 1)
                        self.assert_complete(obj, 3, [(relative, "unreviewed-binary")])

    def test_moving_a_reviewed_zip_does_not_widen_the_path_exemption(self):
        archive, digest, sibling = ZIP_FIXTURES[0]
        files = self.zip_files(archive, digest, sibling)
        raw = files.pop(f"{FIXTURE_DIR}/{archive}")
        files["elsewhere.zip"] = raw
        with overlay(files) as root:
            prose = run_scan(root)
            self.assertEqual((prose.returncode, prose.stdout, prose.stderr), (
                1, b"elsewhere.zip: unreviewed binary payload\n"
                   b"public-safety: 3 files; 1 findings\n", b""))
            _, obj = self.assert_json_scan(root, 1)
            self.assert_complete(obj, 3, [("elsewhere.zip", "unreviewed-binary")])

    def test_scans_do_not_mutate_the_overlay_even_when_they_fail(self):
        with overlay({"a.pem": b"example", "z-unreadable.txt": b"example"}) as root:
            before = tree_snapshot(root)
            for arguments, fault in (((), None), (("--json",), None),
                                     ((), "read"), (("--json",), "read")):
                run_scan(root, *arguments, fault=fault)
                self.assertEqual(tree_snapshot(root), before)
        self.assertFalse(root.exists())

    def test_overlay_cleanup_also_runs_on_an_assertion_failure(self):
        with self.assertRaisesRegex(AssertionError, "synthetic test assertion"):
            with overlay({"fixture.pem": b"example"}) as root:
                raise AssertionError("synthetic test assertion")
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
