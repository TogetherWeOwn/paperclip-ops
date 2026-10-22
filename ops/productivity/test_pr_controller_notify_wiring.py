"""Wiring checks: the notify-guard patch records and gates on the live wake path.

Implementation-owned companion to the frozen `test_pr_controller_notify_guard`
suite (which pins the guard decision contract and patch mechanics). These
tests pin WHERE the patch wires the gate: the `required` hoist, the
verdict-record block immediately after `state[key] = rec` (before the
WAKE_STATES/age/pressure/red-settle exits, so a saved same-head verdict
always describes the checks that pass classified), and the `ci_red`
suppress gate inside `main()` after the wake key, plus the module-level
`notify_author_wake` entry point. Static AST assertions over the patched
fixture: they fail if the wiring is moved, weakened, or widened beyond
`ci_red`, without executing any host path.

The suppress comparator, the entry call arguments, the record key and the
verdict-dict shape are all pinned: inverting the comparator, swapping
entry arguments, passing an emptied known-base set, dropping a dict key,
or storing under a wrong rec key each turn a test red.

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


def plain_ci_red_ifs(main):
    """Plain `if cur == 'ci_red':` blocks (excludes the red-settle BoolOp)."""
    return [n for n in ast.walk(main)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Name)
            and n.test.left.id == 'cur'
            and any(isinstance(op, ast.Eq) for op in n.test.ops)
            and any(isinstance(c, ast.Constant) and c.value == 'ci_red'
                    for c in n.test.comparators)]


def consult_calls(node):
    """`notify_author_wake(...)` calls under a node."""
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'notify_author_wake']


def record_block(main):
    """The inserted verdict-record block: the only consult of the gate.

    The pre-existing `red_since` marker shares the plain-ci_red test shape
    but consults nothing, so the record block is identified by its guard
    consult call.
    """
    matches = [n for n in plain_ci_red_ifs(main) if consult_calls(n)]
    assert len(matches) == 1, f'expected one verdict-record block, found {len(matches)}'
    return matches[0]


def gate_block(main):
    """The inserted suppress gate: reads the recorded verdict, skips the post.

    Identified by its wake-skip `continue` plus the absence of a consult:
    the gate must read the verdict recorded above, never re-consult.
    """
    matches = [n for n in plain_ci_red_ifs(main)
               if not consult_calls(n)
               and any(isinstance(inner, ast.Continue) for inner in ast.walk(n))]
    assert len(matches) == 1, f'expected one suppress gate, found {len(matches)}'
    return matches[0]


class NotifyWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.before = ast.parse(FIXTURE.read_text())
        cls.after = ast.parse(patched_fixture_source())
        cls.main_before = find_main(cls.before)
        cls.main_after = find_main(cls.after)
        cls.record = record_block(cls.main_after)
        cls.gate = gate_block(cls.main_after)

    def test_entry_point_pins_guard_call(self):
        """The entry delegates with (state, detail, required) and the live
        known-base set: swapped arguments or an emptied set each turn red."""
        self.assertIn('notify_author_wake', module_defs(self.after))
        entry = next(n for n in self.after.body
                     if isinstance(n, ast.FunctionDef) and n.name == 'notify_author_wake')
        self.assertEqual(['state', 'detail', 'required'],
                         [a.arg for a in entry.args.args])
        returns = [n for n in ast.walk(entry) if isinstance(n, ast.Return)]
        self.assertEqual(1, len(returns))
        call = returns[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual('author_wake', call.func.attr)
        self.assertIsInstance(call.func.value, ast.Name)
        self.assertEqual('notify_guard', call.func.value.id)
        self.assertEqual(3, len(call.args))
        self.assertEqual(['state', 'detail', 'required'],
                         [a.id for a in call.args if isinstance(a, ast.Name)])
        self.assertEqual(1, len(call.keywords))
        keyword = call.keywords[0]
        self.assertEqual('known_base_executed', keyword.arg)
        self.assertIsInstance(keyword.value, ast.Attribute)
        self.assertEqual('KNOWN_BASE_EXECUTED', keyword.value.attr)
        self.assertIsInstance(keyword.value.value, ast.Name)
        self.assertEqual('notify_guard', keyword.value.value.id)

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

    def test_record_site_consults_once_and_stores_shaped_verdict(self):
        """The record block consults with (cur, detail, required) and stores
        rec['notify_verdict'] = {'head', 'verdict'}: a dropped head, a wrong
        rec key, or moved consult arguments each turn red. Non-ci_red passes
        pop the verdict so a later ci_red cannot revive an old suppress."""
        consults = consult_calls(self.record)
        self.assertEqual(1, len(consults))
        self.assertEqual([], consults[0].keywords)
        self.assertEqual(3, len(consults[0].args))
        self.assertEqual(['cur', 'detail', 'required'],
                         [a.id for a in consults[0].args if isinstance(a, ast.Name)])
        stores = [n for n in ast.walk(self.record)
                  if isinstance(n, ast.Assign)
                  and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Subscript)
                  and isinstance(n.targets[0].value, ast.Name)
                  and n.targets[0].value.id == 'rec'
                  and isinstance(n.targets[0].slice, ast.Constant)
                  and n.targets[0].slice.value == 'notify_verdict']
        self.assertEqual(1, len(stores), "record must store rec['notify_verdict']")
        value = stores[0].value
        self.assertIsInstance(value, ast.Dict)
        keys = [k.value for k in value.keys if isinstance(k, ast.Constant)]
        self.assertEqual({'head', 'verdict'}, set(keys))
        by_key = {k.value: v for k, v in zip(value.keys, value.values)
                  if isinstance(k, ast.Constant)}
        self.assertIsInstance(by_key['head'], ast.Name)
        self.assertEqual('head', by_key['head'].id)
        verdict_value = by_key['verdict']
        self.assertIsInstance(verdict_value, ast.Subscript)
        self.assertIs(consults[0], verdict_value.value)
        self.assertIsInstance(verdict_value.slice, ast.Constant)
        self.assertEqual(0, verdict_value.slice.value)
        pops = [n for n in ast.walk(self.record)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'rec'
                and n.func.attr == 'pop']
        self.assertEqual(1, len(pops))
        self.assertEqual(2, len(pops[0].args))
        self.assertIsInstance(pops[0].args[0], ast.Constant)
        self.assertEqual('notify_verdict', pops[0].args[0].value)
        self.assertIsInstance(pops[0].args[1], ast.Constant)
        self.assertIsNone(pops[0].args[1].value)
        orelse_nodes = [n for branch in self.record.orelse for n in ast.walk(branch)]
        self.assertIn(pops[0], orelse_nodes)

    def test_single_consult_in_main(self):
        """The gate reads the recorded verdict; only the record site consults."""
        self.assertEqual(1, len(consult_calls(self.main_after)))

    def test_gate_reads_recorded_verdict_and_pins_suppress_comparator(self):
        """The gate reads rec['notify_verdict'] and skips only on
        verdict == 'suppress': an inverted comparator turns red. The
        fail-closed `.get` chain wakes (never KeyErrors the tick) when the
        verdict is missing."""
        assigns = [n for n in ast.walk(self.gate)
                   if isinstance(n, ast.Assign)
                   and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name)
                   and n.targets[0].id == 'verdict']
        self.assertEqual(1, len(assigns))
        reads = [n for n in ast.walk(assigns[0].value)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'rec'
                 and n.func.attr == 'get'
                 and len(n.args) == 1 and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == 'notify_verdict']
        self.assertEqual(1, len(reads), "gate must read the recorded rec['notify_verdict']")
        suppress = [n for n in self.gate.body if isinstance(n, ast.If)]
        self.assertEqual(1, len(suppress))
        test = suppress[0].test
        self.assertIsInstance(test, ast.Compare)
        self.assertIsInstance(test.left, ast.Name)
        self.assertEqual('verdict', test.left.id)
        self.assertEqual(1, len(test.ops))
        self.assertIsInstance(test.ops[0], ast.Eq)
        self.assertEqual(1, len(test.comparators))
        self.assertIsInstance(test.comparators[0], ast.Constant)
        self.assertEqual('suppress', test.comparators[0].value)
        self.assertTrue(any(isinstance(n, ast.Continue)
                            for n in ast.walk(suppress[0])),
                        'suppress path must skip the wake post')

    def test_gate_adds_exactly_one_continue(self):
        def count(main):
            return sum(isinstance(n, ast.Continue) for n in ast.walk(main))
        self.assertEqual(count(self.main_before) + 1, count(self.main_after))

    def test_gate_placement_orders_wake_path(self):
        """The gate must sit after the wake key and before stale collection
        and the wake cap: earlier would skip card creation/backstop, later
        would seed rewake candidates or consume cap for suppressed wakes."""
        main = self.main_after
        wkey = [n for n in ast.walk(main)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'wkey' for t in n.targets)]
        self.assertEqual(1, len(wkey))
        def is_woke_compare(v):
            return (isinstance(v, ast.Compare)
                    and isinstance(v.left, ast.Call)
                    and isinstance(v.left.func, ast.Attribute)
                    and isinstance(v.left.func.value, ast.Name)
                    and v.left.func.value.id == 'rec'
                    and v.left.func.attr == 'get'
                    and len(v.comparators) == 1
                    and isinstance(v.comparators[0], ast.Name)
                    and v.comparators[0].id == 'wkey')

        woke_collect = [n for n in ast.walk(main)
                        if isinstance(n, ast.If)
                        and isinstance(n.test, ast.BoolOp)
                        and isinstance(n.test.op, ast.And)
                        and any(is_woke_compare(v) for v in n.test.values)
                        and any(isinstance(v, ast.Name) and v.id == 'cards'
                                for v in n.test.values)]
        self.assertEqual(1, len(woke_collect))
        cap = [n for n in ast.walk(main)
               if isinstance(n, ast.AugAssign)
               and isinstance(n.target, ast.Name) and n.target.id == 'wakes']
        self.assertEqual(1, len(cap))
        self.assertLess(wkey[0].lineno, self.gate.lineno)
        self.assertLess(self.gate.lineno, woke_collect[0].lineno)
        self.assertLess(self.gate.lineno, cap[0].lineno)

    def test_record_precedes_exits_saves_and_gate(self):
        """The record must sit after the state store and before the dupe
        re-store, the WAKE_STATES/age/pressure and red-settle exits, and the
        gate: a saved verdict then always describes the checks that pass
        classified."""
        main = self.main_after
        stores = [n for n in ast.walk(main)
                  if isinstance(n, ast.Assign)
                  and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Subscript)
                  and isinstance(n.targets[0].value, ast.Name)
                  and n.targets[0].value.id == 'state'
                  and isinstance(n.targets[0].slice, ast.Name)
                  and n.targets[0].slice.id == 'key']
        self.assertEqual(2, len(stores))
        first_store, second_store = sorted(n.lineno for n in stores)
        def is_wake_exit(n):
            return (isinstance(n, ast.If)
                    and isinstance(n.test, ast.BoolOp)
                    and isinstance(n.test.op, ast.Or)
                    and any(isinstance(v, ast.Compare)
                            and isinstance(v.left, ast.Name) and v.left.id == 'cur'
                            and any(isinstance(op, ast.NotIn) for op in v.ops)
                            for v in n.test.values))
        wake_exits = [n for n in ast.walk(main) if is_wake_exit(n)]
        self.assertEqual(1, len(wake_exits))
        def is_settle_exit(n):
            if not (isinstance(n, ast.If) and isinstance(n.test, ast.BoolOp)
                    and isinstance(n.test.op, ast.And)):
                return False
            has_ci_red = any(isinstance(v, ast.Compare)
                             and isinstance(v.left, ast.Name) and v.left.id == 'cur'
                             and any(isinstance(c, ast.Constant) and c.value == 'ci_red'
                                     for c in v.comparators)
                             for v in n.test.values)
            has_pending = any(isinstance(v, ast.Call)
                              and isinstance(v.func, ast.Attribute)
                              and isinstance(v.func.value, ast.Name)
                              and v.func.value.id == 'detail'
                              and v.func.attr == 'get'
                              for v in ast.walk(n.test))
            return has_ci_red and has_pending
        settle_exits = [n for n in ast.walk(main) if is_settle_exit(n)]
        self.assertEqual(1, len(settle_exits))
        self.assertLess(first_store, self.record.lineno)
        self.assertLess(self.record.lineno, second_store)
        self.assertLess(self.record.lineno, wake_exits[0].lineno)
        self.assertLess(self.record.lineno, settle_exits[0].lineno)
        self.assertLess(self.record.lineno, self.gate.lineno)

    def test_inserted_names_resolve(self):
        entry = next(n for n in self.after.body
                     if isinstance(n, ast.FunctionDef) and n.name == 'notify_author_wake')
        defined = (module_defs(self.after) | assigned_in_main(self.main_after)
                   | LIVE_GLOBALS | {'notify_guard'}
                   | {a.arg for a in entry.args.args} | {'True', 'False', 'None'})
        loaded = set()
        for node in (list(ast.walk(entry)) + list(ast.walk(self.record))
                     + list(ast.walk(self.gate))):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
        self.assertEqual(set(), loaded - defined)


if __name__ == '__main__':
    unittest.main()
