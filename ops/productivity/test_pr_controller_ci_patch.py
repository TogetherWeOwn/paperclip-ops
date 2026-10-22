"""Exercise the ci-policy call-site patch against a portable stub.

Every removed or context line in pr-controller-ci.patch must already appear
in the reviewed pr-controller-wakes.patch post-image (anchor provenance), so
a mistranscribed anchor fails here instead of at host install. The patch is
then applied with --fuzz=0 to the stub and the result must compile.

CiPatchFlowTest goes further: it extracts the patched call-site blocks from
the applied stub and EXECUTES them with host fakes, driving the real patched
lines red -> rerun -> green -> red. Only the host's own names (post_wake,
save, rec/state) and the deps factory are faked; the policy entry points
are the real glue. A drift in the patch text fails loudly on its markers.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PATCH = HERE / 'patches' / 'pr-controller-ci.patch'
WAKES_PATCH = HERE / 'patches' / 'pr-controller-wakes.patch'

sys.path.insert(0, str(HERE / 'fixtures'))
sys.path.insert(0, str(HERE))
from pr_controller_ci_before import stub_source  # noqa: E402

import pr_controller_ci_host as host  # noqa: E402


def patch_lines(path, prefixes):
    lines = set()
    for raw in path.read_text(encoding='utf-8').splitlines():
        if raw.startswith(prefixes) and not raw.startswith(('---', '+++', '@@')):
            lines.add(raw[1:])
    return lines


def applied_source():
    """The stub with pr-controller-ci.patch applied exactly as at install."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / 'ops' / 'productivity'
        target.mkdir(parents=True)
        controller = target / 'pr_controller.py'
        controller.write_text(stub_source(), encoding='utf-8')
        result = subprocess.run(
            ['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
            cwd=root, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AssertionError(
                f'patch failed: {result.stdout}\n{result.stderr}')
        return controller.read_text(encoding='utf-8')


def extract_block(applied, start_marker, end_marker):
    """Slice exact patched lines out of the applied stub, dedented."""
    lines = applied.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == start_marker), None)
    if start is None:
        raise AssertionError(
            f'patched block start not found: {start_marker}')
    end = next((i for i in range(start, len(lines))
                if lines[i].strip() == end_marker), None)
    if end is None:
        raise AssertionError(f'patched block end not found: {end_marker}')
    return textwrap.dedent('\n'.join(lines[start:end + 1]) + '\n')


class CiPatchTest(unittest.TestCase):
    def test_anchors_come_from_the_wakes_post_image(self):
        known = patch_lines(WAKES_PATCH, ('+', ' '))
        needed = patch_lines(PATCH, ('-', ' '))
        # Hunk-1 context lines live in both patches; every other anchor must
        # be a wakes post-image line (added or context).
        missing = {line for line in needed
                   if line not in known
                   and 'review_error_rerequest' not in line
                   and 'SUPPLY_REPOS' not in line
                   and 'pr_controller_wakes import' not in line}
        self.assertEqual(set(), missing)

    def test_patch_applies_zero_fuzz_and_compiles(self):
        applied = applied_source()
        compile(applied, 'pr_controller.py', 'exec')
        for token in ('ci_policy_tick', 'ci_policy_deps', 'ci_policy_reset',
                      'ci_policy_kill', 'ci_policy_shadow', 'capped_head',
                      'tick_comment_id', 'pending_wake',
                      'ci_policy_shared'):
            self.assertIn(token, applied)
        # No optimistic delivery marker: like post_wake, the patch sets at
        # most pending_wake; reconcile_wake alone sets rec['woke'].
        self.assertNotIn("rec['woke']", applied)
        self.assertNotIn('last_outcome', applied)


def red_detail(run_id, attempt=1, job='rust tests'):
    return {'checks': [{'workflow': 'check.yml', 'job': job,
                        'run_id': run_id, 'attempt': attempt,
                        'conclusion': 'failure'}]}


def green_detail(run_id, attempt=1, job='rust tests'):
    row = red_detail(run_id, attempt, job)['checks'][0]
    return {'checks': [dict(row, conclusion='success')]}


class FlowGh:
    """Public repo, reruns recorded; no network."""

    def __init__(self):
        self.reruns = []

    def __call__(self, argv):
        if argv[:3] == ['gh', 'run', 'rerun']:
            self.reruns.append(list(argv))
            return (0, 'rerun requested')
        if argv[1:2] == ['api']:
            if argv[2].endswith('/jobs'):
                return (0, json.dumps([['ubuntu-latest']]))
            return (0, json.dumps(False))
        raise AssertionError(f'unexpected gh call: {argv}')


class FlowApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, dict(payload)))
        return ('201', json.dumps({'id': f'comment-{len(self.calls)}'}))


class FlowLedger:
    def __init__(self):
        self.recorded = []

    def verdict(self, repo, key):
        return 'no'

    def record_flake(self, entry):
        self.recorded.append(dict(entry))


def clean_env():
    """Pin the policy flags off so ambient env cannot sway the flow."""
    return mock.patch.dict(os.environ, {'PR_CONTROLLER_CI_DISABLE': '',
                                        'PR_CONTROLLER_CI_DISABLE_FILE': '',
                                        'CI_POLICY_SHADOW': ''},
                           clear=False)


class WakeFlow:
    """Runs the patched wake-site block pass by pass with host fakes."""

    def __init__(self, block):
        self.block = compile(block, '<patched-wake-site>', 'exec')
        self.gh = FlowGh()
        self.api = FlowApi()
        self.ledger = FlowLedger()
        self.cards = []
        self.logs = []
        self.rec = {}
        self.state = {}
        self.wakes = []
        self.saves = 0

    def deps(self, api, log):
        return {'api': self.api, 'gh': self.gh, 'ledger': self.ledger,
                'quarantine_card': self.cards.append, 'log': log,
                'log_action': self.logs.append, 'shadow': False}

    def post_wake(self, rec, wkey, iid, text, api):
        self.wakes.append({'wkey': wkey, 'issue_id': iid, 'text': text})
        return '201'

    def save(self, state):
        self.saves += 1

    def run_pass(self, cur, head, detail):
        namespace = {
            'post_wake': self.post_wake,
            'rec': self.rec, 'wkey': f'{cur}@{head[:8]}', 'iid': 'card-1',
            'text': f'WAKE {cur} {head[:8]}', 'api': self.api,
            'cur': cur, 'cards': [('owner-card-1',)],
            'full': 'TogetherWeOwn/two-bot-next',
            'pr': {'number': 7, 'headRefOid': head},
            'detail': detail, 'head': head, 'key': 'pr-7',
            'state': self.state, 'save': self.save, 'ident': 'o/r#7',
            'log': self.logs.append,
            'ci_policy_deps': self.deps,
            'ci_policy_tick': host.handle_ci_red,
            'ci_policy_reset': host.handle_ci_reset,
            'ci_policy_kill': host.kill_switch_active,
            'ci_policy_shadow': host.shadow_active,
        }
        exec(self.block, namespace)
        return namespace


class CiPatchFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        applied = applied_source()
        cls.wake_block = extract_block(
            applied, "_ci_verdict = 'proceed'", 'save(state)')
        cls.rewake_block = extract_block(
            applied, "_ci_state = rec.get('ci_policy') or {}",
            'state[key] = rec')

    def test_red_rerun_green_red_flow_through_patched_lines(self):
        # The passing-state name is the host's business; the patch only
        # distinguishes `== 'ci_red'`, so 'passing' stands in for it.
        with clean_env():
            flow = WakeFlow(self.wake_block)
            head_a = 'a' * 40
            # Pass 1: first red reruns; the host wake is skipped.
            namespace = flow.run_pass('ci_red', head_a, red_detail(101))
            self.assertEqual('200 ci-policy handled wake decision',
                             namespace['code'])
            self.assertEqual([], flow.wakes)
            self.assertEqual(1, len(flow.gh.reruns))
            self.assertIn('ci_policy_shared', flow.state)
            # Pass 2: the retry passed and the head left ci_red. The
            # reset records the flake but the host wake still proceeds.
            namespace = flow.run_pass(
                'passing', head_a, green_detail(101, attempt=2))
            self.assertTrue(namespace['code'].startswith('2'))
            self.assertEqual(1, len(flow.wakes))
            self.assertEqual(1, len(flow.ledger.recorded))
            self.assertEqual(0, flow.rec['ci_policy']['round_count'])
            self.assertEqual(1, len(flow.gh.reruns))
            self.assertEqual([], flow.api.calls)
            # Pass 3: a new red head reruns fresh (no stale rounds/cap).
            namespace = flow.run_pass('ci_red', 'b' * 40, red_detail(102))
            self.assertEqual('200 ci-policy handled wake decision',
                             namespace['code'])
            self.assertEqual(1, len(flow.wakes))
            self.assertEqual(2, len(flow.gh.reruns))

    def test_cap_mutes_until_green_then_rearms(self):
        with clean_env():
            flow = WakeFlow(self.wake_block)
            for head, run_id in (('a' * 40, 101), ('b' * 40, 102),
                                 ('c' * 40, 103)):
                flow.run_pass('ci_red', head, red_detail(run_id))
                flow.run_pass('ci_red', head,
                              red_detail(run_id, attempt=2))
            self.assertEqual(3, flow.rec['ci_policy']['round_count'])
            self.assertEqual('c' * 40,
                             flow.rec['ci_policy']['capped_head'])
            self.assertEqual(3, len(flow.api.calls))  # 2 wakes + cap
            # Round 4: still reruns, still silent.
            flow.run_pass('ci_red', 'd' * 40, red_detail(104))
            flow.run_pass('ci_red', 'd' * 40, red_detail(104, attempt=2))
            self.assertEqual(4, len(flow.gh.reruns))
            self.assertEqual(3, len(flow.api.calls))
            # A green head resets the cap through the patched elif, and
            # the host wake for that pass still goes out.
            wakes_before = len(flow.wakes)
            flow.run_pass('passing', 'e' * 40, green_detail(105))
            self.assertEqual(0, flow.rec['ci_policy']['round_count'])
            self.assertNotIn('capped_head', flow.rec['ci_policy'])
            self.assertEqual(wakes_before + 1, len(flow.wakes))
            # The next red head reruns and wakes again (not silence).
            flow.run_pass('ci_red', 'f' * 40, red_detail(106))
            flow.run_pass('ci_red', 'f' * 40, red_detail(106, attempt=2))
            self.assertEqual(4, len(flow.api.calls))

    def run_rewake(self, rec, cur='ci_red'):
        calls = []

        def post_wake(rec, wkey, iid, text, api):
            calls.append((wkey, iid))
            return '201'

        namespace = {
            'post_wake': post_wake, 'rec': rec, 'wkey': 'ci_red@a',
            'iid': 'card-1', 'text': 'REMINDER', 'api': FlowApi(),
            'cur': cur, 'key': 'pr-7', 'state': {}, 'ident': 'o/r#7',
            'pr': {'headRefOid': 'a' * 40}, 'since_h': 9,
            'log': lambda line: None,
            'ci_policy_kill': host.kill_switch_active,
            'ci_policy_shadow': host.shadow_active,
        }
        block = compile(self.rewake_block, '<patched-rewake-site>', 'exec')
        exec(block, namespace)
        return namespace, calls

    def capped_rec(self):
        return {'ci_policy': {'round_count': 3,
                              'capped_head': 'a' * 40,
                              'rounds': [{}, {}, {}]}}

    def test_capped_rewake_skipped_but_kill_switch_restores(self):
        namespace, calls = self.run_rewake(self.capped_rec())
        self.assertEqual([], calls)
        self.assertIn('capped', namespace['code'])
        with mock.patch.dict(os.environ,
                             {'PR_CONTROLLER_CI_DISABLE': '1',
                              'CI_POLICY_SHADOW': ''},
                             clear=False):
            _, calls = self.run_rewake(self.capped_rec())
        self.assertEqual(1, len(calls))

    def test_capped_rewake_not_skipped_in_shadow(self):
        with mock.patch.dict(os.environ,
                             {'PR_CONTROLLER_CI_DISABLE': '',
                              'CI_POLICY_SHADOW': '1'},
                             clear=False):
            _, calls = self.run_rewake(self.capped_rec())
        self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
