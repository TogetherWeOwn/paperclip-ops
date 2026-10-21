#!/usr/bin/env python3
"""test_ax_lint_branches.py — pins each ax-lint certification branch.

Hermetic: stdlib only, synthetic fixtures. Run from the repo root:

  python3 -B -m unittest discover -s templates/org-github/tests -p 'test_ax_*py'

Each case writes one flag-taking script that handles only ``--target`` plus
one shape that must (or must not) certify ``--json``, then asserts whether
the script-surface rule fires.
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ax-lint"))

import ax_lint  # noqa: E402

SH = "#!/bin/sh\n"
PY = "#!/usr/bin/env python3\n"


def surface_findings(name, text):
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
        return ax_lint.check_script_surface(root)


class CertifyMixin:
    name = "tool.sh"
    head = SH

    def certifies(self, body):
        return not surface_findings(self.name, self.head + body)


class ShellComparison(CertifyMixin, unittest.TestCase):
    def test_positive_forms(self):
        for body in (
            '[[ "$1" == "--json" ]]\n--target x\n',
            '[ "$1" = --json ]\n',
            '[ --json = "$1" ]\n',
            '[[ $1 == --json && -n $2 ]]\n',
        ):
            self.assertTrue(self.certifies(body), body)

    def test_negative_forms(self):
        for body in (
            "echo '[ $1 = --json ]'\n--target x\n",
            "[ '$1' = --json ]\n--target x\n",
            '[ "$1" != --json ]\n--target x\n',
            '[ "$FMT" = --json ]\n--target x\n',
            'echo "x\\" [ $1 = --json ] y"\n--target x\n',
            'echo "[ $1 = --json ]"\n--target x\n',
        ):
            self.assertFalse(self.certifies(body), body)


class ShellCase(CertifyMixin, unittest.TestCase):
    def test_positive(self):
        self.assertTrue(self.certifies(
            'case "$1" in\n  --json) emit ;;\n  --target) go ;;\nesac\n'))
        self.assertTrue(self.certifies(
            'case "$1" in\n  --target|--json) go ;;\nesac\n'))

    def test_value_case_does_not_certify(self):
        self.assertFalse(self.certifies(
            'case "$FORMAT" in\n  --json) emit ;;\nesac\n--target x\n'))

    def test_nested_case_and_body_lines_do_not_certify(self):
        self.assertFalse(self.certifies(
            'case "$1" in\n  --target)\n    case "$FORMAT" in\n'
            '      --json) emit ;;\n    esac\n    ;;\nesac\n'))
        self.assertFalse(self.certifies(
            'case "$1" in\n  --target)\n    out=$(run \\\n'
            '    --json)\n    ;;\nesac\n'))


class UsageLine(CertifyMixin, unittest.TestCase):
    def test_positive(self):
        for body in ("# usage: tool --target X --json\n",
                     'echo "Usage: tool --json"\n--target\n'):
            self.assertTrue(self.certifies(body), body)

    def test_exact_token_and_prose_do_not_certify(self):
        for body in ("# usage: tool --target X --json-file F\n",
                     "# TODO: usage: --json (not implemented)\n--target\n",
                     "# see usage: --json elsewhere\n--target\n"):
            self.assertFalse(self.certifies(body), body)


class PythonShapes(CertifyMixin, unittest.TestCase):
    name = "tool.py"
    head = PY

    def test_positive(self):
        for body in (
            "p.add_argument('--json', action='store_true')\n",
            "p.add_argument('-j', '--json')\n",
            "if '--json' in sys.argv:\n    pass\n--target\n",
        ):
            self.assertTrue(self.certifies(body), body)

    def test_string_literals_do_not_certify(self):
        for body in (
            "p.add_argument('--target', help=\"like '--json' output\")\n",
            "DOC = \"p.add_argument('--json')\"\n--target\n",
            "MSG = \"x '--json' in sys.argv\"\n--target\n",
        ):
            self.assertFalse(self.certifies(body), body)


def dispatch(text):
    with tempfile.TemporaryDirectory() as root:
        wf = os.path.join(root, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "w.yml"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return ax_lint.check_dispatch_inputs(root), ax_lint.coverage(root)


class DispatchScan(unittest.TestCase):
    def test_comments_do_not_stop_the_scan(self):
        for text in (
            "on:\n  workflow_dispatch:\n# note\n    inputs:\n      a:\n"
            "        description: d\n",
            "on:\n  workflow_dispatch:\n  # note\n    inputs:\n      a:\n"
            "        description: d\n",
            "on:\n  workflow_dispatch:\n    inputs:\n      ok:\n"
            "        type: string\n    # note\n      a:\n"
            "        description: d\n",
        ):
            findings, _ = dispatch(text)
            self.assertEqual(["dispatch-inputs/untyped"],
                             [f["rule_id"] for f in findings], text)

    def test_unsupported_entry_does_not_hide_prior_missing_type(self):
        findings, cov = dispatch(
            "on:\n  workflow_dispatch:\n    inputs:\n      first:\n"
            "        description: d\n      second: &a\n"
            "        type: string\n")
        self.assertEqual(1, len(findings))
        self.assertIn("'first'", findings[0]["message"])
        self.assertEqual(1, len(cov["unknown"]))


if __name__ == "__main__":
    unittest.main()
