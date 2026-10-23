"""Shadow-exclusion regression for the pr-controller newest-review selection.

The deployed controller picks the newest review row per repository/PR/head with
``select distinct on ... order by ... created_at desc``. Without a
``shadow_label is null`` predicate, a later shadow-only assessment row wins the
pick and hides the real review, so the gate verdict (and any author wake) is
computed from shadow data. The reviewed patch adds the predicate before the
newest-row pick; this suite pins that behavior.

How it runs: ``latest_reviews`` is AST-loaded alone from the patched fixture
(or from a full staged controller via ``PR_CONTROLLER_SOURCE``) without
importing any host client. ``rr.psql`` is replaced by a minimal in-test model
of Postgres that derives the shadow filter from the real captured SQL text and
applies the ``distinct on`` newest-row pick over explicit row lists. Verdicts
are computed with the review branch of the (unchanged) gate contract: a
missing entry stays ``green`` so the review gate re-requests it, a completed
``5`` is ``passed``, and any other completed score is ``findings`` carrying
the real score and URL.

Red-before / green-after: point ``PR_CONTROLLER_SOURCE`` at a copy of the raw
fixture and the shadow scenarios fail (the shadow row wins); against the
patched source the whole suite passes. The real-only control scenario passes
on both, proving real newest-row behavior is unchanged.
"""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PATCH = HERE / 'patches/pr-controller-shadow.patch'
FIXTURE = HERE / 'fixtures/pr_controller_shadow_before.py'

EP = 'test-endpoint'

REPO = 'Example/Widget'
REPO_OTHER = 'Other/Gadget'
PR = 7
HEAD = 'abc123def'
REAL_URL = 'https://example.com/review/real'
REAL_URL_2 = 'https://example.com/review/real-2'
SHADOW_URL = 'https://example.com/review/shadow'


def row(repo, pr, head, state, score, url, shadow, created):
    """One chat_github_reviews row: created is an ordinal timestamp, larger is newer."""
    return (repo, pr, head, state, score, url, shadow, created)


def emulate_psql(sql, table):
    """Minimal model of the Postgres query: WHERE filter, then newest-row pick.

    Only the predicate the patch changes is derived from the SQL text: shadow
    rows are excluded exactly when the captured statement carries
    ``shadow_label is null``. Every fixture row is assumed to match the
    endpoint predicate and the 14-day window, and to carry post-coalesce
    score/URL values; fixtures avoid created_at ties (Postgres would break
    them arbitrarily, this model keeps the first).
    """
    exclude_shadow = 'shadow_label is null' in sql
    winners, seen = {}, {}
    for entry in table:
        repo, pr, head, state, score, url, shadow, created = entry
        if exclude_shadow and shadow is not None:
            continue
        key = (repo.lower(), pr, head)
        if key not in winners or created > seen[key]:
            winners[key] = (repo.lower(), pr, head, state, score, url)
            seen[key] = created
    return sorted(winners.values(), key=lambda r: (r[0], r[1], r[2]))


def review_verdict(reviews, key):
    """Gate verdict for one head from the newest-review map.

    This is the review branch of the host gate contract, which the patch does
    not change: with CI green on a ready, mergeable PR, a missing entry stays
    ``green`` (the review gate re-requests it), a completed ``5`` is
    ``passed``, and any other completed score is ``findings`` carrying the
    real score and URL. The suite only stages completed rows; anything else is
    outside this regression's scope.
    """
    rv = reviews.get(key)
    if not rv:
        return 'green', {}
    state, score, url = rv
    assert state == 'completed', f'fixture staged {state!r}, suite covers completed rows'
    if score == '5':
        return 'passed', {}
    return 'findings', {'score': score, 'url': url}


def patched_source():
    if os.environ.get('PR_CONTROLLER_SOURCE'):
        return Path(os.environ['PR_CONTROLLER_SOURCE']).read_text()
    return patched_fixture_source()


def patched_fixture_source():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'ops/productivity/pr_controller.py'
        target.parent.mkdir(parents=True)
        target.write_text(FIXTURE.read_text())
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                       cwd=directory, check=True, capture_output=True, text=True)
        return target.read_text()


def load_latest_reviews(source, table):
    """AST-load latest_reviews alone; rr.psql answers from the fixture table."""
    tree = ast.parse(source)
    function = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == 'latest_reviews')
    code = compile(ast.Module(body=[function], type_ignores=[]), '<controller>', 'exec')
    captured = []

    def fake_psql(sql):
        captured.append(sql)
        return emulate_psql(sql, table)

    env = {'rr': types.SimpleNamespace(psql=fake_psql), 'EP': EP}
    exec(code, env)
    return env['latest_reviews'], captured


class ShadowSelectionTest(unittest.TestCase):
    def select(self, table):
        latest_reviews, captured = load_latest_reviews(patched_source(), table)
        return latest_reviews(), captured

    def test_newer_shadow_failure_does_not_hide_real_gate(self):
        table = [row(REPO, PR, HEAD, 'completed', '5', REAL_URL, None, 100),
                 row(REPO, PR, HEAD, 'completed', '4', SHADOW_URL, 'shadow', 200)]
        reviews, _ = self.select(table)
        key = (REPO.lower(), PR, HEAD)
        self.assertEqual(('completed', '5', REAL_URL), reviews[key])
        self.assertEqual(('passed', {}), review_verdict(reviews, key))

    def test_newer_shadow_gate_does_not_hide_real_failure(self):
        table = [row(REPO, PR, HEAD, 'completed', '4', REAL_URL, None, 100),
                 row(REPO, PR, HEAD, 'completed', '5', SHADOW_URL, 'shadow', 200)]
        reviews, _ = self.select(table)
        key = (REPO.lower(), PR, HEAD)
        self.assertEqual(('completed', '4', REAL_URL), reviews[key])
        state, detail = review_verdict(reviews, key)
        self.assertEqual('findings', state)
        self.assertEqual({'score': '4', 'url': REAL_URL}, detail)

    def test_shadow_only_head_has_no_entry_and_stays_green(self):
        table = [row(REPO, PR, HEAD, 'completed', '4', SHADOW_URL, 'shadow', 200)]
        reviews, _ = self.select(table)
        key = (REPO.lower(), PR, HEAD)
        self.assertNotIn(key, reviews)
        self.assertEqual(('green', {}), review_verdict(reviews, key))

    def test_newer_real_row_supersedes_older_real_row(self):
        cases = [((('completed', '5', 100), ('completed', '4', 200)), 'findings'),
                 ((('completed', '4', 100), ('completed', '5', 200)), 'passed')]
        for (older, newer), expected in cases:
            with self.subTest(older=older, newer=newer):
                table = [row(REPO, PR, HEAD, older[0], older[1], REAL_URL, None, older[2]),
                         row(REPO, PR, HEAD, newer[0], newer[1], REAL_URL_2, None, newer[2])]
                reviews, _ = self.select(table)
                key = (REPO.lower(), PR, HEAD)
                self.assertEqual((newer[0], newer[1], REAL_URL_2), reviews[key])
                self.assertEqual(expected, review_verdict(reviews, key)[0])

    def test_shadow_rows_never_leak_across_heads_or_repos(self):
        table = [row(REPO, PR, 'head-one', 'completed', '5', REAL_URL, None, 100),
                 row(REPO, PR, 'head-one', 'completed', '4', SHADOW_URL, 'shadow', 200),
                 row(REPO, PR, 'head-two', 'completed', '4', SHADOW_URL, 'shadow', 200),
                 row(REPO_OTHER, 9, 'head-nine', 'completed', '4', REAL_URL_2, None, 100),
                 row(REPO_OTHER, 9, 'head-nine', 'completed', '5', SHADOW_URL, 'shadow', 200)]
        reviews, _ = self.select(table)
        gated = (REPO.lower(), PR, 'head-one')
        shadow_only = (REPO.lower(), PR, 'head-two')
        failed = (REPO_OTHER.lower(), 9, 'head-nine')
        self.assertEqual(('completed', '5', REAL_URL), reviews[gated])
        self.assertEqual(('passed', {}), review_verdict(reviews, gated))
        self.assertNotIn(shadow_only, reviews)
        self.assertEqual(('green', {}), review_verdict(reviews, shadow_only))
        self.assertEqual(('completed', '4', REAL_URL_2), reviews[failed])
        state, detail = review_verdict(reviews, failed)
        self.assertEqual('findings', state)
        self.assertEqual({'score': '4', 'url': REAL_URL_2}, detail)
        self.assertEqual({gated, failed}, set(reviews))

    def test_newer_shadow_transient_row_does_not_hide_real_gate(self):
        # A shadow assessment still running must not displace the real gate
        # either: only completed real rows participate in the verdict.
        table = [row(REPO, PR, HEAD, 'completed', '5', REAL_URL, None, 100),
                 row(REPO, PR, HEAD, 'running', '', SHADOW_URL, 'shadow', 200)]
        reviews, _ = self.select(table)
        key = (REPO.lower(), PR, HEAD)
        self.assertEqual(('completed', '5', REAL_URL), reviews[key])
        self.assertEqual(('passed', {}), review_verdict(reviews, key))

    def test_patched_sql_excludes_shadow_rows(self):
        _, captured = self.select([])
        self.assertEqual(1, len(captured))
        self.assertIn('shadow_label is null', captured[0])


class RawFixtureGuardTest(unittest.TestCase):
    def test_raw_fixture_still_exhibits_defect(self):
        # Guard: the fixture must stay pre-fix. If the patch ever stops
        # changing behavior, this test fails instead of going quietly green.
        table = [row(REPO, PR, HEAD, 'completed', '5', REAL_URL, None, 100),
                 row(REPO, PR, HEAD, 'completed', '4', SHADOW_URL, 'shadow', 200)]
        latest_reviews, captured = load_latest_reviews(FIXTURE.read_text(), table)
        reviews = latest_reviews()
        self.assertNotIn('shadow_label', captured[0])
        key = (REPO.lower(), PR, HEAD)
        self.assertEqual(('completed', '4', SHADOW_URL), reviews[key])
        state, detail = review_verdict(reviews, key)
        self.assertEqual('findings', state)
        self.assertEqual({'score': '4', 'url': SHADOW_URL}, detail)


class ShadowPatchOffsetTest(unittest.TestCase):
    def test_offset_check_is_independent_of_staged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / 'staged_controller.py'
            staged.write_text('# full staged controller\n' + patched_source())
            with patch.dict(os.environ, {'PR_CONTROLLER_SOURCE': str(staged)}):
                self.test_shadow_patch_applies_at_live_offset_without_fuzz()

    def test_shadow_patch_applies_at_live_offset_without_fuzz(self):
        # The live controller carries latest_reviews near line 160 (base
        # header -160,7). A hunk anchored at line 1 cannot float down with
        # --fuzz=0, so the patch must also apply embedded after prefix lines,
        # installing the byte-identical function the BOF round-trip exercises.
        prefix = ''.join(f'# live controller prefix line {i}\n' for i in range(1, 160))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'ops/productivity/pr_controller.py'
            target.parent.mkdir(parents=True)
            target.write_text(prefix + FIXTURE.read_text())
            result = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            offset_patched = target.read_text()
        self.assertEqual(patched_fixture_source(), offset_patched[len(prefix):])


if __name__ == '__main__':
    unittest.main()
