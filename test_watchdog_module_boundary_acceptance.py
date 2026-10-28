"""Blind acceptance for the watchdog section-reader module boundary.

Run: python3 -B -m unittest -v test_watchdog_module_boundary_acceptance

Source-owned boundary (not a new architecture):
``watchdog/sections.py`` says findings are built by the caller's ``_finding``
so it never needs to import ``detectors`` (which imports it), and
``watchdog/detectors.py`` imports ``SectionRead``/``read_float``/``read_int``
from sections in both package and standalone-script spellings. Only the
static reverse edge sections -> detectors is forbidden. Unrelated imports
are unspecified and must not be prohibited. Dynamic imports
(``__import__``, ``importlib.import_module``) and transitive dependencies
are explicit non-goals: this is one static, direct-import invariant, not a
dependency-graph or purity framework.

Frozen contract (the builder implements exactly this; do not weaken it):

* Command: ``python3 verification/watchdog-module-boundary.py [--root PATH]``.
  No ``--root`` means the repository checkout containing the checker
  (the parent of ``verification/``), independent of the caller's CWD. A
  relative ``--root`` resolves against the caller's CWD. The root must
  contain ``watchdog/sections.py`` and ``watchdog/detectors.py``.
* Both files are read as strict UTF-8 text and parsed with the stdlib
  ``ast`` module. Inspected source is never imported or executed, and the
  checker never writes to the inspected tree.
* Stdout is exactly one UTF-8 JSON object and stderr is empty on exits
  0/1/2. Top-level keys, no more and no fewer: ``schema`` (exactly
  ``"watchdog-module-boundary/v1"``), ``rule`` (exactly
  ``"watchdog-sections-must-not-import-detectors"``), ``status``
  (``"pass"``/``"violation"``/``"error"``), ``checkedFiles`` (sorted
  repo-relative POSIX paths actually parsed), ``checkedFileCount``
  (``len(checkedFiles)``), ``allowedEdgeCount``, ``violationCount``,
  ``violations``, ``errors``. Diagnostics name repo-relative paths only;
  they never echo absolute paths, source text, or supplied values.
* Exit 0 is ``pass``, exit 1 is ``violation``, exit 2 is ``error``. On
  ``error``, ``allowedEdgeCount`` and ``violationCount`` are JSON ``null``
  (never a fabricated zero) and ``violations`` is empty: an unchecked or
  partly checked tree must not claim a clean measurement.
* One edge per import statement. A violation is a static ``import`` /
  ``from``-import statement anywhere in the ``sections.py`` AST (nested,
  conditional and ``try`` imports included) whose target is the detectors
  module in any of these spellings: ``import watchdog.detectors``,
  ``import detectors``, ``from watchdog.detectors import ...``,
  ``from detectors import ...``, ``from .detectors import ...``,
  ``from watchdog import detectors``, ``from . import detectors`` —
  aliases included. ``allowedEdgeCount`` counts the mirror-image forward
  statements in ``detectors.py`` targeting sections; every other import
  is ignored entirely.
* A violation object has exactly ``rule``, ``source``
  (``"watchdog/sections.py"``), ``target``
  (``"watchdog/detectors.py"``), ``line`` (1-based line of the import
  statement) and ``kind`` (``"import"`` or ``"from-import"``), sorted by
  ``(source, line, kind, target)``.
* An error object has exactly ``code``, ``source``, ``error``, ``line``.
  ``code`` is ``"missing-root"`` (source ``"."``), ``"missing-file"``,
  ``"unreadable-file"``, ``"invalid-encoding"`` or ``"invalid-python"``;
  ``error`` is the exception class name only; ``line`` is the syntax
  error line for ``invalid-python`` when known, else ``null``. Errors
  are sorted by ``(source, code, error)``.
* CI: a reusable (``workflow_call``) workflow under
  ``.github/workflows/`` runs both this suite and the checker against the
  real repository, on a GitHub-hosted Ubuntu runner, with read-only
  permissions, persisted credentials disabled, no secrets and no
  ``continue-on-error``; ``.github/workflows/ci.yml`` calls it.

These tests start RED: a missing checker or missing reusable workflow is
a failure, never a skip. Only this test file belongs to the acceptance
author; the builder must not edit it. Fixtures are string literals in
temporary roots — tracked production files are never mutated, and no
fixture source is ever executed by this suite.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
CHECKER = ROOT / "verification" / "watchdog-module-boundary.py"
SCHEMA = "watchdog-module-boundary/v1"
RULE = "watchdog-sections-must-not-import-detectors"
SECTIONS = "watchdog/sections.py"
DETECTORS = "watchdog/detectors.py"
TOP_KEYS = {
    "schema", "rule", "status", "checkedFiles", "checkedFileCount",
    "allowedEdgeCount", "violationCount", "violations", "errors",
}
VIOLATION_KEYS = {"rule", "source", "target", "line", "kind"}
ERROR_KEYS = {"code", "source", "error", "line"}
SENTINEL = "INERT_BOUNDARY_SENTINEL_7c1d"  # Not a credential.
TIMEOUT = 30

VALID_DETECTORS = (
    "try:\n"
    "    from watchdog.sections import SectionRead, read_float, read_int\n"
    "except ImportError:\n"
    "    from sections import SectionRead, read_float, read_int\n"
    "import json\n"
    "def evaluate(finding):\n"
    "    return SectionRead\n"
    'raise RuntimeError("fixture must never be executed")\n'
)

VALID_SECTIONS = (
    "import math\n"
    "# Comment mentioning the reverse edge: import detectors\n"
    'NOTE = "from watchdog.detectors import anything"\n'
    "def read(finding, detector):\n"
    "    return finding(detector)\n"
    'raise RuntimeError("fixture must never be executed")\n'
)

AS_DIRECTORY = object()


class BoundaryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watchdog-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.counter = 0
        self.env = {
            "HOME": str(self.work),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(self.work),
        }

    def require_checker(self):
        self.assertTrue(
            CHECKER.is_file(),
            "baseline RED: verification/watchdog-module-boundary.py "
            "has not been implemented",
        )

    def make_root(self, sections=VALID_SECTIONS, detectors=VALID_DETECTORS):
        """Build one temporary root; a None value leaves the file absent."""
        self.counter += 1
        root = self.work / f"case-{self.counter}"
        package = root / "watchdog"
        package.mkdir(parents=True)
        for name, content in (("sections.py", sections), ("detectors.py", detectors)):
            if content is None:
                continue
            path = package / name
            if content is AS_DIRECTORY:
                path.mkdir()
            elif isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        return root

    def run_checker(self, *args, cwd=None):
        self.require_checker()
        return subprocess.run(
            [sys.executable, "-B", str(CHECKER), *args],
            cwd=cwd or self.work,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )

    def snapshot(self, root):
        result = {}
        for rel in (SECTIONS, DETECTORS):
            path = root / rel
            if path.is_file():
                result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def envelope(self, result, expected_exit, expected_status, forbidden=()):
        self.assertEqual(
            result.returncode, expected_exit,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertEqual(result.stderr, "", "diagnostics belong in the JSON envelope")
        for token in (SENTINEL, str(self.work), *forbidden):
            self.assertNotIn(token, result.stdout, "output leaked supplied text")
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            self.fail(f"expected exactly one JSON object: {error}; stdout={result.stdout!r}")
        self.assertIsInstance(value, dict)
        self.assertEqual(set(value), TOP_KEYS, "top-level keys drifted from the frozen contract")
        self.assertEqual(value["schema"], SCHEMA)
        self.assertEqual(value["rule"], RULE)
        self.assertEqual(value["status"], expected_status)
        self.assertEqual(value["checkedFiles"], sorted(value["checkedFiles"]))
        self.assertTrue(set(value["checkedFiles"]) <= {SECTIONS, DETECTORS})
        self.assertEqual(value["checkedFileCount"], len(value["checkedFiles"]))
        for violation in value["violations"]:
            self.assertEqual(set(violation), VIOLATION_KEYS)
        for error in value["errors"]:
            self.assertEqual(set(error), ERROR_KEYS)
        if expected_status == "error":
            self.assertIsNone(value["allowedEdgeCount"], "error must not fabricate a count")
            self.assertIsNone(value["violationCount"], "error must not claim zero violations")
            self.assertEqual(value["violations"], [])
            self.assertTrue(value["errors"], "error status needs at least one error")
        else:
            self.assertEqual(value["errors"], [])
            self.assertIsInstance(value["allowedEdgeCount"], int)
            self.assertEqual(value["violationCount"], len(value["violations"]))
            self.assertEqual(
                value["checkedFiles"], [DETECTORS, SECTIONS],
                "a verdict needs both production files parsed",
            )
        if expected_status == "pass":
            self.assertEqual(value["violations"], [])
            self.assertEqual(value["violationCount"], 0)
        if expected_status == "violation":
            self.assertGreater(value["violationCount"], 0)
        return value

    def violations_for(self, sections, detectors=VALID_DETECTORS):
        root = self.make_root(sections=sections, detectors=detectors)
        return self.envelope(self.run_checker("--root", str(root)), 1, "violation")

    def pass_for(self, sections=VALID_SECTIONS, detectors=VALID_DETECTORS):
        root = self.make_root(sections=sections, detectors=detectors)
        return self.envelope(self.run_checker("--root", str(root)), 0, "pass")


class AllowedControls(BoundaryFixture):
    def test_existing_forward_spellings_and_callback_direction_pass(self):
        value = self.pass_for()
        self.assertEqual(value["allowedEdgeCount"], 2)

    def test_each_forward_spelling_counts_as_one_allowed_edge(self):
        spellings = [
            "import watchdog.sections\n",
            "import watchdog.sections as sec\n",
            "import sections\n",
            "import sections as sec\n",
            "from watchdog.sections import read_int\n",
            "from sections import read_int\n",
            "from .sections import read_int\n",
            "from watchdog import sections\n",
            "from watchdog import sections as sec\n",
            "from . import sections\n",
            "from . import sections as sec\n",
        ]
        for spelling in spellings:
            with self.subTest(spelling=spelling.strip()):
                value = self.pass_for(detectors=spelling)
                self.assertEqual(value["allowedEdgeCount"], 1)

    def test_unrelated_imports_are_neither_allowed_edges_nor_violations(self):
        value = self.pass_for(
            sections=(
                "import collections\n"
                "import definitely_unrelated_helper_xyz\n"
                "from collections import deque\n"
                "import math\n"
            ),
            detectors=(
                "import watchdog.assignment_contract\n"
                "from assignment_contract import valid_coverage\n"
                "from watchdog.sections import read_int\n"
            ),
        )
        self.assertEqual(value["allowedEdgeCount"], 1)

    def test_comments_and_strings_mentioning_detectors_do_not_fail(self):
        value = self.pass_for(
            sections=(
                "# import detectors -- a comment, not an edge\n"
                'A = "import watchdog.detectors"\n'
                "B = 'from . import detectors'\n"
                'C = """from watchdog import detectors"""\n'
            ),
        )
        self.assertEqual(value["allowedEdgeCount"], 2)

    def test_dynamic_imports_are_out_of_scope_for_this_static_rule(self):
        # Documented exclusion: only direct static imports are checked.
        self.pass_for(
            sections=(
                "import importlib\n"
                'LAZY = importlib.import_module("watchdog.detectors")\n'
                'OTHER = __import__("detectors")\n'
            ),
        )

    def test_inspected_source_is_not_executed_or_modified(self):
        # VALID_* fixtures raise at module level and import only through
        # names a parser never resolves; a passing verdict proves parsing.
        root = self.make_root()
        before = self.snapshot(root)
        value = self.envelope(self.run_checker("--root", str(root)), 0, "pass")
        self.assertEqual(value["allowedEdgeCount"], 2)
        self.assertEqual(self.snapshot(root), before, "checker mutated inspected files")

    def test_relative_root_resolves_against_caller_cwd(self):
        root = self.make_root()
        result = self.run_checker("--root", root.name, cwd=root.parent)
        self.assertEqual(self.envelope(result, 0, "pass")["allowedEdgeCount"], 2)


class ForbiddenEdgeControls(BoundaryFixture):
    # (sections source, expected line, expected kind); line 1 is a comment
    # and line 2 an unrelated import in every flat case, so the reverse
    # edge lands on line 3 unless a case says otherwise.
    FLAT_CASES = [
        "import watchdog.detectors",
        "import watchdog.detectors as det",
        "import detectors",
        "import detectors as det",
        "from watchdog.detectors import evaluate",
        "from watchdog.detectors import *",
        "from detectors import evaluate",
        "from .detectors import evaluate",
        "from watchdog import detectors",
        "from watchdog import detectors as det",
        "from . import detectors",
        "from . import detectors as det",
    ]

    def expected_violation(self, line, kind):
        return {
            "rule": RULE, "source": SECTIONS, "target": DETECTORS,
            "line": line, "kind": kind,
        }

    def test_each_reverse_spelling_is_detected_with_source_and_line(self):
        for statement in self.FLAT_CASES:
            with self.subTest(statement=statement):
                kind = "from-import" if statement.startswith("from") else "import"
                value = self.violations_for(
                    f"# fixture\nimport math\n{statement}\n"
                )
                self.assertEqual(value["violations"], [self.expected_violation(3, kind)])
                self.assertEqual(value["allowedEdgeCount"], 2)

    def test_one_statement_is_one_violation_however_many_names_it_imports(self):
        statements = [
            ("import detectors, watchdog.detectors, os\n", "import"),
            ("from watchdog.detectors import first, second, third\n", "from-import"),
            ("from watchdog import detectors, sections\n", "from-import"),
        ]
        for source, kind in statements:
            with self.subTest(source=source.strip()):
                value = self.violations_for(source)
                self.assertEqual(value["violations"], [self.expected_violation(1, kind)])

    def test_nested_and_conditional_reverse_imports_are_detected(self):
        cases = [
            (
                "def read():\n    import detectors\n    return detectors\n",
                2, "import",
            ),
            (
                "if True:\n    from watchdog import detectors\n",
                2, "from-import",
            ),
            (
                "try:\n    from .detectors import evaluate\n"
                "except ImportError:\n    evaluate = None\n",
                2, "from-import",
            ),
            (
                "class Reader:\n    def load(self):\n"
                "        from detectors import evaluate\n        return evaluate\n",
                3, "from-import",
            ),
        ]
        for source, line, kind in cases:
            with self.subTest(line=line, kind=kind):
                value = self.violations_for(source)
                self.assertEqual(value["violations"], [self.expected_violation(line, kind)])

    def test_multiple_violations_are_counted_and_sorted_by_line(self):
        value = self.violations_for(
            "import detectors\n"
            "import math\n"
            "def read():\n"
            "    from . import detectors\n"
            "from watchdog.detectors import evaluate\n"
        )
        self.assertEqual(
            value["violations"],
            [
                self.expected_violation(1, "import"),
                self.expected_violation(4, "from-import"),
                self.expected_violation(5, "from-import"),
            ],
        )
        self.assertEqual(value["violationCount"], 3)

    def test_violation_output_never_echoes_source_text(self):
        root = self.make_root(
            sections=f'# {SENTINEL}\nimport detectors\nTOKEN = "{SENTINEL}"\n'
        )
        result = self.run_checker("--root", str(root))
        self.envelope(result, 1, "violation")
        self.assertNotIn(SENTINEL, result.stdout)


class InvalidInputControls(BoundaryFixture):
    def assert_error(self, value, expected_errors, expected_checked):
        self.assertEqual(value["errors"], expected_errors)
        self.assertEqual(value["checkedFiles"], expected_checked)

    def test_absent_root_is_an_error_not_a_clean_pass(self):
        result = self.run_checker("--root", str(self.work / "no-such-root"))
        value = self.envelope(result, 2, "error")
        self.assert_error(
            value,
            [{"code": "missing-root", "source": ".",
              "error": "FileNotFoundError", "line": None}],
            [],
        )

    def test_root_that_is_a_file_is_an_error(self):
        file_root = self.work / "not-a-directory"
        file_root.write_text("not a directory\n", encoding="utf-8")
        value = self.envelope(self.run_checker("--root", str(file_root)), 2, "error")
        self.assert_error(
            value,
            [{"code": "missing-root", "source": ".",
              "error": "NotADirectoryError", "line": None}],
            [],
        )

    def test_missing_sections_file_is_an_error_with_detectors_still_checked(self):
        root = self.make_root(sections=None)
        value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
        self.assert_error(
            value,
            [{"code": "missing-file", "source": SECTIONS,
              "error": "FileNotFoundError", "line": None}],
            [DETECTORS],
        )

    def test_missing_detectors_file_is_an_error_with_sections_still_checked(self):
        root = self.make_root(detectors=None)
        value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
        self.assert_error(
            value,
            [{"code": "missing-file", "source": DETECTORS,
              "error": "FileNotFoundError", "line": None}],
            [SECTIONS],
        )

    def test_both_files_missing_reports_both_sorted(self):
        root = self.make_root(sections=None, detectors=None)
        value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
        self.assert_error(
            value,
            [
                {"code": "missing-file", "source": DETECTORS,
                 "error": "FileNotFoundError", "line": None},
                {"code": "missing-file", "source": SECTIONS,
                 "error": "FileNotFoundError", "line": None},
            ],
            [],
        )

    def test_malformed_sections_is_invalid_python(self):
        root = self.make_root(sections='MARKER = "x"\ndef broken(:\n    pass\n')
        value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
        self.assert_error(
            value,
            [{"code": "invalid-python", "source": SECTIONS,
              "error": "SyntaxError", "line": 2}],
            [DETECTORS],
        )

    def test_malformed_detectors_is_invalid_python_too(self):
        root = self.make_root(detectors='MARKER = "x"\ndef broken(:\n    pass\n')
        value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
        self.assert_error(
            value,
            [{"code": "invalid-python", "source": DETECTORS,
              "error": "SyntaxError", "line": 2}],
            [SECTIONS],
        )

    def test_invalid_utf8_in_either_file_is_invalid_encoding(self):
        bad = b"VALUE = '\xff'\n"
        for kwargs, source, other in (
            ({"sections": bad}, SECTIONS, DETECTORS),
            ({"detectors": bad}, DETECTORS, SECTIONS),
        ):
            with self.subTest(source=source):
                root = self.make_root(**kwargs)
                value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
                self.assert_error(
                    value,
                    [{"code": "invalid-encoding", "source": source,
                      "error": "UnicodeDecodeError", "line": None}],
                    [other],
                )

    def test_unreadable_files_fail_portably_in_either_module(self):
        # A directory where a file belongs is unreadable as source on any
        # platform and needs no privilege or permission games to set up.
        for kwargs, source, other in (
            ({"sections": AS_DIRECTORY}, SECTIONS, DETECTORS),
            ({"detectors": AS_DIRECTORY}, DETECTORS, SECTIONS),
        ):
            with self.subTest(source=source):
                root = self.make_root(**kwargs)
                value = self.envelope(self.run_checker("--root", str(root)), 2, "error")
                self.assertEqual(value["checkedFiles"], [other])
                self.assertEqual(len(value["errors"]), 1)
                error = value["errors"][0]
                self.assertEqual(error["code"], "unreadable-file")
                self.assertEqual(error["source"], source)
                self.assertIn(
                    error["error"],
                    {"IsADirectoryError", "PermissionError", "OSError"},
                )
                self.assertIsNone(error["line"])

    def test_error_output_never_echoes_source_or_secret_shaped_text(self):
        root = self.make_root(
            sections=f'TOKEN = "{SENTINEL}"\ndef broken(:\n    pass\n'
        )
        result = self.run_checker("--root", str(root))
        self.envelope(result, 2, "error")
        self.assertNotIn(SENTINEL, result.stdout)

    def test_no_root_argument_never_invents_a_verdict_for_a_foreign_cwd(self):
        # Default root is the checker's own checkout, so running from an
        # empty temporary CWD must still measure the real repository.
        result = self.run_checker(cwd=self.work)
        value = self.envelope(result, 0, "pass")
        self.assertEqual(value["allowedEdgeCount"], 2)


class RealRepositoryControls(BoundaryFixture):
    def test_real_repository_passes_with_the_two_existing_forward_edges(self):
        before = self.snapshot(ROOT)
        self.assertEqual(set(before), {SECTIONS, DETECTORS})
        value = self.envelope(self.run_checker("--root", str(ROOT)), 0, "pass")
        self.assertEqual(value["allowedEdgeCount"], 2)
        self.assertEqual(value["checkedFiles"], [DETECTORS, SECTIONS])
        self.assertEqual(self.snapshot(ROOT), before, "real scan mutated production files")

    def test_default_root_from_another_cwd_measures_this_checkout(self):
        value = self.envelope(self.run_checker(cwd=self.work), 0, "pass")
        self.assertEqual(value["allowedEdgeCount"], 2)
        self.assertEqual(value["violationCount"], 0)


class CiContractControls(unittest.TestCase):
    WORKFLOWS = ROOT / ".github" / "workflows"

    def candidate_workflows(self):
        candidates = []
        paths = sorted(self.WORKFLOWS.glob("*.yml")) + sorted(
            self.WORKFLOWS.glob("*.yaml")
        )
        for path in paths:
            if path.name == "ci.yml":
                continue
            text = path.read_text(encoding="utf-8")
            if (
                "workflow_call" in text
                and "verification/watchdog-module-boundary.py" in text
                and "test_watchdog_module_boundary_acceptance" in text
            ):
                candidates.append((path, text))
        return candidates

    def test_a_reusable_workflow_runs_the_suite_and_the_real_scan(self):
        candidates = self.candidate_workflows()
        self.assertTrue(
            candidates,
            "baseline RED: no reusable workflow_call workflow runs both "
            "test_watchdog_module_boundary_acceptance and "
            "verification/watchdog-module-boundary.py",
        )

    def test_reusable_workflow_is_github_hosted_read_only_and_credential_free(self):
        candidates = self.candidate_workflows()
        self.assertTrue(candidates, "no reusable boundary workflow found")
        write_permission = re.compile(
            r"(?m)^\s*(?:contents|actions|checks|issues|pull-requests|"
            r"packages|deployments|statuses|security-events|id-token)"
            r"\s*:\s*write\b"
        )
        for path, text in candidates:
            with self.subTest(workflow=path.name):
                self.assertRegex(text, r"(?m)^\s*runs-on:\s*\[?\s*ubuntu-")
                self.assertRegex(text, r"contents:\s*read\b")
                self.assertNotRegex(text, write_permission)
                self.assertNotIn("write-all", text)
                self.assertNotIn("continue-on-error: true", text)
                self.assertNotRegex(text, r"(?m)^\s*secrets\s*:")
                self.assertNotIn("${{ secrets.", text)
                self.assertNotIn("persist-credentials: true", text)
                if "actions/checkout@" in text:
                    self.assertIn("persist-credentials: false", text)

    def test_ci_calls_the_reusable_workflow_without_swallowing_its_failure(self):
        candidates = self.candidate_workflows()
        self.assertTrue(candidates, "no reusable boundary workflow found")
        ci_text = (self.WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        for path, _text in candidates:
            with self.subTest(workflow=path.name):
                needle = f"uses: ./.github/workflows/{path.name}"
                index = ci_text.find(needle)
                self.assertGreaterEqual(
                    index, 0, f"ci.yml does not call {path.name}"
                )
                window = ci_text[max(0, index - 600):index + 200]
                self.assertNotIn("continue-on-error: true", window)


if __name__ == "__main__":
    unittest.main()
