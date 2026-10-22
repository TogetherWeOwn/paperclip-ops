"""Wiring checks: the notify-guard patch consults the gate at the live wake path.

Implementation-owned companion to the frozen `test_pr_controller_notify_guard`
suite (which pins the guard decision contract and patch mechanics). These
tests pin WHERE the patch wires the gate: the `required` hoist and the
`ci_red` consult-and-suppress block inside `main()`, plus the module-level
`notify_author_wake` entry point. Static AST assertions over the patched
fixture: they fail if the wiring is moved, weakened, or widened beyond
`ci_red`, without executing any host path.

Boundary: `SHADOW`/`log` resolve against live module globals above the
fixture seam (verified against the exact installed bytes during review, and
re-verified by the install contract's compile step); everything else the
inserted code touches must resolve inside the patched fixture itself.

Run::

  (cd ops/productivity && python3 -B -m unittest -v test_pr_controller_notify_wiring)
"""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # sibling import; -B safe
PATCH = HERE / 'patches' / 'pr-controller-notify-guard.patch'
FIXTURE = HERE / 'fixtures' / 'pr_controller_notify_before.py'

# Live-module globals above the fixture seam, used by the inserted block.
# Verified present in the exact installed bytes at review time; the install
# contract recompiles the candidate, so a drifted live tree stops there.
LIVE_GLOBALS = {'SHADOW', 'log'}


def patched_fixture_source():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'ops/productivity/pr_controller.py'
        target.parent.mkdir(parents=True)
        target.write_text(FIXTURE.read_text())
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                       cwd=directory, check=True, capture_output=True, text=True)
        return target.read_text()


def module_defs(tree):
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def find_main(tree):
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == 'main')


def assigned_in_main(main):
    """Names bound anywhere in main() (Assign/For/With targets, walrus)."""
    bound = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
    return bound


def gate_block(main):
    """The inserted `if cur == 'ci_red':` consult block (exactly one).

    The pre-existing `red_since` marker shares the same test shape, so the
    gate is identified by its guard consult call.
    """
    def is_gate(node):
        return (isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == 'cur'
                and any(isinstance(op, ast.Eq) for op in node.test.ops)
                and any(isinstance(c, ast.Constant) and c.value == 'ci_red'
                        for c in node.test.comparators)
                and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == 'notify_author_wake'
                        for n in ast.walk(node)))
    matches = [n for n in ast.walk(main) if is_gate(n)]
    assert len(matches) == 1, f'expected one ci_red gate, found {len(matches)}'
    return matches[0]


class NotifyWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.before = ast.parse(FIXTURE.read_text())
        cls.after = ast.parse(patched_fixture_source())
        cls.main_before = find_main(cls.before)
        cls.main_after = find_main(cls.after)
        cls.gate = gate_block(cls.main_after)

    def test_entry_point_calls_guard(self):
        self.assertIn('notify_author_wake', module_defs(self.after))
        entry = next(n for n in self.after.body
                     if isinstance(n, ast.FunctionDef) and n.name == 'notify_author_wake')
        calls = [n for n in ast.walk(entry) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(c.func, ast.Attribute)
                            and c.func.attr == 'author_wake' for c in calls),
                        'entry point must delegate to author_wake')

    def test_required_hoist_feeds_classify(self):
        hoists = [n for n in ast.walk(self.main_after)
                  if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'required' for t in n.targets)
                  and isinstance(n.value, ast.Call)
                  and isinstance(n.value.func, ast.Name)
                  and n.value.func.id == 'required_checks']
        self.assertEqual(1, len(hoists), 'exactly one required hoist in main()')
        calls = [n for n in ast.walk(self.main_after)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == 'classify']
        self.assertEqual(1, len(calls))
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual('required', calls[0].args[1].id)

    def test_gate_consults_guard_stores_verdict_and_skips_post(self):
        body = self.gate.body
        consults = [n for n in ast.walk(self.gate)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == 'notify_author_wake']
        self.assertEqual(1, len(consults))
        args = consults[0].args
        self.assertEqual(['cur', 'detail', 'required'],
                         [a.id for a in args if isinstance(a, ast.Name)])
        stores = [n for n in ast.walk(self.gate)
                  if isinstance(n, ast.Subscript)
                  and isinstance(n.value, ast.Name) and n.value.id == 'rec']
        self.assertTrue(stores, 'gate must record the verdict on rec for the pump')
        suppress = [n for n in body if isinstance(n, ast.If)]
        self.assertEqual(1, len(suppress))
        self.assertTrue(any(isinstance(n, ast.Continue)
                            for n in ast.walk(suppress[0])),
                        'suppress path must skip the wake post')

    def test_gate_adds_exactly_one_continue(self):
        def count(main):
            return sum(isinstance(n, ast.Continue) for n in ast.walk(main))
        self.assertEqual(count(self.main_before) + 1, count(self.main_after))

    def test_inserted_names_resolve(self):
        entry = next(n for n in self.after.body
                     if isinstance(n, ast.FunctionDef) and n.name == 'notify_author_wake')
        defined = (module_defs(self.after) | assigned_in_main(self.main_after)
                   | LIVE_GLOBALS | {'notify_guard'}
                   | {a.arg for a in entry.args.args} | {'True', 'False', 'None'})
        loaded = set()
        for node in list(ast.walk(entry)) + list(ast.walk(self.gate)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
        self.assertEqual(set(), loaded - defined)


if __name__ == '__main__':
    unittest.main()
