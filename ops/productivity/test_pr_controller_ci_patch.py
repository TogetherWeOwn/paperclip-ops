"""Exercise the ci-policy call-site patch against a portable stub.

Every removed or context line in pr-controller-ci.patch must already appear
in the reviewed pr-controller-wakes.patch post-image (anchor provenance), so
a mistranscribed anchor fails here instead of at host install. The patch is
then applied with --fuzz=0 to the stub and the result must compile.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / 'patches' / 'pr-controller-ci.patch'
WAKES_PATCH = HERE / 'patches' / 'pr-controller-wakes.patch'

sys.path.insert(0, str(HERE / 'fixtures'))
from pr_controller_ci_before import stub_source  # noqa: E402


def patch_lines(path, prefixes):
    lines = set()
    for raw in path.read_text(encoding='utf-8').splitlines():
        if raw.startswith(prefixes) and not raw.startswith(('---', '+++', '@@')):
            lines.add(raw[1:])
    return lines


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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'ops' / 'productivity'
            target.mkdir(parents=True)
            controller = target / 'pr_controller.py'
            controller.write_text(stub_source(), encoding='utf-8')
            result = subprocess.run(
                ['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(
                0, result.returncode,
                f'patch failed: {result.stdout}\n{result.stderr}')
            applied = controller.read_text(encoding='utf-8')
            compile(applied, str(controller), 'exec')
            for token in ('ci_policy_tick', 'ci_policy_deps', 'capped_head',
                          'tick_comment_id', 'pending_wake',
                          'ci_policy_shared'):
                self.assertIn(token, applied)
            # No optimistic delivery marker: like post_wake, the patch sets at
            # most pending_wake; reconcile_wake alone sets rec['woke'].
            self.assertNotIn("rec['woke']", applied)
            self.assertNotIn('last_outcome', applied)


if __name__ == '__main__':
    unittest.main()
