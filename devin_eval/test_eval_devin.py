"""Offline suite for eval_devin.py. No network, credentials or remote writes.

Run: cd devin_eval && python3 -B -m unittest -v test_eval_devin
"""
import base64
import contextlib
import filecmp
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('eval_devin', HERE / 'eval_devin.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

DEVIN = {'login': 'devin-ai-integration[bot]', 'type': 'Bot'}
HUMAN = {'login': 'builder', 'type': 'User'}
SHA = {c: c * 40 for c in 'abcdef0123456789'}


def status(state, description, at, sha_creator='devin-ai-integration[bot]', context='Devin Review', ident=1):
    return dict(id=ident, context=context, state=state, description=description, created_at=at, target_url='u' + str(ident),
                creator={'login': sha_creator})


def versions_manifest(repo='TogetherWeOwn/two-web-next', entries=None):
    entries = entries or [dict(commit=SHA['1'], committed_at='2026-10-30T03:00:00Z', blob=SHA['a'], text='# R\n## Skip\n'),
                          dict(commit=SHA['2'], committed_at='2026-10-30T20:00:00Z', blob=SHA['b'],
                               text='# R\n## What merits a finding\n- **Severe (red):** x\n## Skip\n')]
    numbered = m.number_versions(repo, [dict(e) for e in entries], SHA['2'])
    return dict(schema_version=1, v2_merges={repo: SHA['2']}, captured_at='2026-10-31T00:00:00Z', repos={repo: numbered})


def write_snapshot(root, raws, versions, since='2026-10-30T16:00:00Z', round_name='rt', escapes=None):
    snap = root / 'snapshots' / round_name
    snap.mkdir(parents=True)
    entries = []
    for i, (repo, raw) in enumerate(raws):
        name = f'pr{i}.json'
        (snap / name).write_text(json.dumps(raw))
        entries.append(dict(repo=repo, number=raw['pr']['number'], file=name, head=raw['pr']['head']['sha']))
    (snap / 'manifest.json').write_text(json.dumps(dict(round=round_name, since=since, started_at='2026-10-31T00:00:00Z',
                                                        finished_at='2026-10-31T00:10:00Z', scope=m.REPOS, prs=entries)))
    (snap / m.VERSIONS_FILE).write_text(json.dumps(versions))
    if escapes is not None:
        (snap / m.ESCAPES_FILE).write_text(json.dumps(escapes))
    return snap


def pr_raw(number, head, base=SHA['0'], merged_at=None, files=('app.py',), blob=SHA['b'], reviews=(), comments=(), statuses=None):
    return dict(pr={'number': number, 'state': 'closed' if merged_at else 'open', 'head': {'sha': head}, 'base': {'sha': base},
                    'user': {'login': 'builder'}, 'created_at': '2026-10-30T10:00:00Z', 'merged_at': merged_at},
                reviews=list(reviews), comments=list(comments), commits=[], files=[{'filename': f} for f in files],
                checks={}, statuses=statuses or {}, review_blobs={head: blob, base: SHA['a']},
                timeline=[], captured_at='2026-10-31T00:05:00Z')


class CarriedOverTests(unittest.TestCase):
    """The original harness tests, kept as regressions."""

    def test_review_blob_at_commit_and_absence(self):
        def get(endpoint):
            if '/commits/' in endpoint:
                return {'commit': {'tree': {'sha': 'tree'}}}
            return {'tree': [{'path': 'REVIEW.md', 'type': 'blob', 'sha': 'blob1'}], 'truncated': False}
        with patch.object(m, 'gh_get', side_effect=get):
            self.assertEqual(m.review_blob('o/r', 'head'), 'blob1')
        with patch.object(m, 'gh_get', side_effect=lambda e: {'commit': {'tree': {'sha': 'tree'}}} if '/commits/' in e else {'tree': [], 'truncated': False}):
            self.assertEqual(m.review_blob('o/r', 'base'), 'none')

    def test_round_cutoff_filters_reviews_and_findings_and_tags_blobs(self):
        def review(i, time, sha):
            return {'id': i, 'commit_id': sha, 'submitted_at': time, 'user': DEVIN, 'html_url': 'url',
                    'body': '<!-- devin-review-comment {"id":"BUG_%s"} -->\n🐛 **Bug**' % i}
        raw = pr_raw(1, 'h', base='b', reviews=[review(1, '2026-10-30T15:00:00Z', 'old'), review(2, '2026-10-30T17:00:00Z', 'h')])
        raw['review_blobs'] = {'h': 'headblob', 'b': 'baseblob'}
        with tempfile.TemporaryDirectory() as d:
            snap = write_snapshot(pathlib.Path(d), [('o/r', raw)], versions_manifest())
            _, prs = m.normalize(snap)
        self.assertEqual([r['sha'] for r in prs[0]['reviews']], ['h'])
        self.assertEqual(len(prs[0]['findings']), 1)
        f = prs[0]['findings'][0]
        self.assertEqual((f['review_md_head_blob'], f['review_md_base_blob']), ('headblob', 'baseblob'))
        self.assertIsNone(f['devin_effort'])

    def test_bug_emoji_does_not_invent_severity(self):
        self.assertEqual(m.reported_severity('🐛 🔴 severe bug'), 'high')
        self.assertEqual(m.reported_severity('🐛 🟡 non-severe bug'), 'medium')
        self.assertIsNone(m.reported_severity('🐛 bug without severity'))
        self.assertEqual(m.reported_severity('🐛 Severe bug'), 'high')
        self.assertEqual(m.reported_severity('🐛 Non-severe bug'), 'medium')

    def test_effort_only_from_explicit_label(self):
        self.assertEqual(m.devin_effort('Review effort: Ultra'), 'ultra')
        self.assertIsNone(m.devin_effort('This requires ultra careful analysis.'))

    def test_since_and_round_are_required(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, '-I', str(HERE / 'eval_devin.py'), 'collect', '--workdir', d, '--round', 'r1'],
                                    capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--since', result.stderr)

    def test_cutoff_includes_exact_instant_with_offsets(self):
        self.assertTrue(m.at_or_after('2026-10-30T17:00:00+01:00', '2026-10-30T16:00:00Z'))
        self.assertFalse(m.at_or_after('2026-10-30T15:59:59Z', '2026-10-30T16:00:00Z'))

    def test_control_only_uses_absent_review_md_heads(self):
        prs = [{'repo': m.REPOS[-1], 'number': 1, 'head': 'a', 'reviews': [
            {'sha': 'a', 'bot': 'devin', 'review_md_head_blob': 'none'},
            {'sha': 'b', 'bot': 'devin', 'review_md_head_blob': 'unknown'},
            {'sha': 'c', 'bot': 'devin', 'review_md_head_blob': 'blob'}]}]
        self.assertEqual(m.control_heads(prs), {(m.REPOS[-1], 1, 'a')})

    def test_truncated_git_tree_does_not_mean_absent(self):
        with patch.object(m, 'gh_get', side_effect=lambda e: {'commit': {'tree': {'sha': 't'}}} if '/commits/' in e else {'tree': [], 'truncated': True}):
            with self.assertRaises(ValueError):
                m.review_blob('o/r', 'h')

    def test_read_only_client_rejects_mutations(self):
        # Removing endpoint/method validation would permit a remote write.
        with self.assertRaises(ValueError):
            m.validate_endpoint('POST', '/repos/o/r/pulls/1/comments')
        with self.assertRaises(ValueError):
            m.validate_endpoint('GET', '/user')
        self.assertIsNone(m.validate_endpoint('GET', '/repos/o/r/pulls?state=all'))

    def test_gh_get_never_issues_a_mutating_method(self):
        calls = []
        def run(argv, **_):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='[]', stderr='')
        with patch.object(m.subprocess, 'run', side_effect=run):
            m.gh_get('/repos/o/r/commits?path=REVIEW.md&sha=main')
            with self.assertRaises(ValueError):
                m.gh_get('/graphql')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ['gh', 'api', '--method', 'GET'])

    def test_devin_summary_and_inline_are_one_finding(self):
        # Counting the summary as well as its inline copy inflates precision denominators.
        r = {'id': 10, 'commit_id': 'a'*40, 'submitted_at': '2026-10-30T01:00:00Z', 'user': DEVIN, 'html_url': 'review',
             'body': '<!-- devin-review-comment {"id":"BUG_one","file_path":"x.py","start_line":1,"end_line":2,"kind":"bug"} -->\n### test\nwrong'}
        c = {'id': 11, 'commit_id': 'a'*40, 'original_commit_id': 'a'*40,
             'created_at': '2026-10-30T01:00:00Z', 'user': r['user'], 'path': 'x.py',
             'original_line': 2, 'body': r['body'], 'html_url': 'inline'}
        fs = m.extract_findings('o/r', 1, [r], [c], '2026-10-30T00:00:00Z')
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]['url'], 'inline')
        self.assertEqual(fs[0]['sha'], 'a'*40)

    def test_exact_head_coverage_never_counts_stale_or_neutral_check(self):
        # A check that completed without findings is not proof a review happened.
        prs = [{'repo': 'o/r', 'number': 1, 'head': 'b'*40, 'state': 'open',
                'reviews': [{'bot': 'devin', 'sha': 'a'*40, 'at': '2026-10-30T01:00:00Z'}],
                'findings': [], 'checks': [{'name': 'Devin Review', 'conclusion': 'neutral'}]}]
        result = m.coverage(prs)
        self.assertEqual(result['snapshot_heads'], 1)
        self.assertEqual(result['devin_exact_snapshot_heads'], 0)
        self.assertEqual(result['devin_stale_prs'], 1)

    def test_precision_and_recall_keep_unknowns_and_heads_separate(self):
        # Unknown findings must not become negatives; cross-SHA matches must not count as catches.
        fs = [dict(key='a', bot='devin', repo='o/r', pr=1, sha='a'*40),
              dict(key='b', bot='devin', repo='o/r', pr=1, sha='a'*40),
              dict(key='c', bot='devin', repo='o/r', pr=1, sha='a'*40),
              dict(key='d', bot='paperclip', repo='o/r', pr=1, sha='a'*40),
              dict(key='e', bot='paperclip', repo='o/r', pr=1, sha='b'*40)]
        judgments = {'a': dict(verdict='true_defect', defect='X', blocking=True),
                     'b': dict(verdict='false_positive'),
                     'd': dict(verdict='true_defect', defect='Y', blocking=True),
                     'e': dict(verdict='true_defect', defect='X', blocking=True)}
        metrics = m.quality(fs, judgments, {('o/r', 1, 'a'*40)})
        self.assertEqual(metrics['devin']['true'], 1)
        self.assertEqual(metrics['devin']['false'], 1)
        self.assertEqual(metrics['devin']['unknown'], 1)
        self.assertEqual(metrics['devin']['precision_adjudicated'], 0.5)
        self.assertEqual(metrics['reference']['blocking_defect_heads'], 2)
        self.assertEqual(metrics['devin']['blocking_recall_reference'], 0.5)

    def test_production_recall_does_not_hide_test_oracle_gaps(self):
        fs = [dict(key='a', bot='devin', repo='o/r', pr=1, sha='a'),
              dict(key='b', bot='paperclip', repo='o/r', pr=1, sha='a')]
        js = {'a': dict(verdict='true_defect', defect='prod', blocking=True, kind='behavior'),
              'b': dict(verdict='true_defect', defect='test', blocking=True, kind='test_gap')}
        result = m.quality(fs, js, {('o/r', 1, 'a')})
        self.assertEqual(result['reference']['by_kind'], {'behavior': 1, 'test_gap': 1})
        self.assertEqual(result['devin']['recall_by_kind'], {'behavior': 1.0, 'test_gap': 0.0})

    def test_scored_checks_count_clean_native_assessments_not_execution_guards(self):
        raw = {'reviews': [], 'checks': {'a'*40: [
            {'name': 'Paperclip Review', 'status': 'completed', 'conclusion': 'success',
             'output': {'title': '5/5'}, 'completed_at': '2026-10-29T12:00:00Z', 'html_url': 'score'},
            {'name': 'Paperclip Review', 'status': 'completed', 'conclusion': 'action_required',
             'output': {'title': 'Authorized manual review required'}, 'html_url': 'guard'}]}}
        reviews = m.review_observations(raw)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['sha'], 'a'*40)
        self.assertEqual(reviews[0]['source'], 'scored_check')

    def test_later_fix_requires_git_ancestry_not_a_fix_word_or_timestamp(self):
        commits = [{'sha': 'a', 'parents': []}, {'sha': 'b', 'parents': [{'sha': 'a'}]},
                   {'sha': 'c', 'parents': [{'sha': 'b'}]}, {'sha': 'z', 'parents': []}]
        self.assertTrue(m.is_descendant('c', 'a', commits))
        self.assertFalse(m.is_descendant('z', 'a', commits))
        self.assertFalse(m.is_descendant('a', 'a', commits))

    def test_reference_candidates_need_explicit_audits_not_automatic_true_labels(self):
        candidates = [dict(repo='o/r', sha='a', id=1), dict(repo='o/r', sha='a', id=2)]
        audited = m.audit_candidates('ci', candidates, {'ci:o/r:a:1': {'verdict': 'excluded', 'rationale': 'metadata only'}})
        self.assertEqual(audited[0]['audit']['verdict'], 'excluded')
        self.assertEqual(audited[1]['audit']['verdict'], 'unassessed')

    def test_hidden_flags_in_both_summary_formats_are_unobservable_not_clean(self):
        self.assertEqual(m.hidden_flag_count('1 flag not posted on this PR'), 1)
        self.assertEqual(m.hidden_flag_count('## Devin Review: 1 flag\n\nNot posted on this PR by your GitHub settings'), 1)
        self.assertEqual(m.hidden_flag_count('No issues found'), 0)

    def test_yellow_devin_severity_is_not_unknown(self):
        self.assertEqual(m.reported_severity('\U0001f7e1 **Finding**'), 'medium')
        self.assertEqual(m.reported_severity('P2: ordinary issue'), 'medium')

    def test_frd_requires_evidence_not_just_a_reply(self):
        # A generic acknowledgement must not count as resolved or rejected.
        self.assertIsNone(m.frd('Thanks, looking into it.'))
        self.assertIsNone(m.frd('Fixed.'))
        self.assertEqual(m.frd('Fixed in abc123456, x.py:42 now validates this.'), 'F')
        self.assertEqual(m.frd('Rejected: x.py:42 already checks the caller input.'), 'R')
        self.assertEqual(m.frd('Duplicate: https://github.com/o/r/pull/1#discussion_r123'), 'D')

    def test_completed_devin_status_only_and_cutoff(self):
        raw = {'reviews': [], 'checks': {}, 'statuses': {'a': [
            status('success', 'Completed analysis in 35s', '2026-10-30T17:00:00Z'),
            status('failure', 'Completed analysis in 40s', '2026-10-30T17:01:00Z'),
            status('pending', 'Completed analysis', '2026-10-30T17:02:00Z'),
            status('error', 'Completed analysis', '2026-10-30T17:02:00Z'),
            status('success', 'Completed analysis', '2026-10-30T17:02:00Z', sha_creator='someone-else[bot]'),
            status('success', 'Completed analysis', '2026-10-30T17:02:00Z', context='Other'),
            status('success', 'Running', '2026-10-30T17:02:00Z'),
            status('success', 'Completed analysis', '2026-10-30T15:00:00Z')]}}
        obs = m.review_observations(raw, '2026-10-30T16:00:00Z')
        self.assertEqual([(o['bot'], o['sha'], o['source']) for o in obs], [('devin', 'a', 'commit_status')] * 2)

    def test_draft_and_ineligible_and_unknown_are_separate_from_staleness(self):
        base = {'head': 'new', 'reviews': [{'bot': 'devin', 'sha': 'old'}]}
        prs = [{**base, 'head_trigger': 'draft'}, {**base, 'head_trigger': 'pre_enrollment'},
               {**base, 'head_trigger': 'eligible'}, {**base, 'head_trigger': 'unknown'}]
        cov = m.coverage(prs)
        self.assertEqual(cov['devin_stale_prs'], 1)
        self.assertEqual(cov['devin_stale_draft_prs'], 1)
        self.assertEqual(cov['devin_stale_pre_enrollment_prs'], 1)
        self.assertEqual(cov['devin_stale_trigger_unknown_prs'], 1)
        self.assertEqual(cov['paperclip_stale_prs'], 0)

    def test_devin_finding_kind_and_quality_split(self):
        reviews = [{'id': 1, 'commit_id': 'a', 'submitted_at': '2026-10-30T17:00:00Z', 'user': DEVIN, 'html_url': 'u',
                    'body': '<!-- devin-review-comment {"id":"ANALYSIS_1","kind":"analysis"} -->\nflag\n<!-- devin-review-comment {"id":"BUG_2","kind":"bug"} -->\nbug'}]
        fs = m.extract_findings('o/r', 1, reviews, [], '2026-10-30T16:00:00Z')
        js = {f['key']: {'verdict': 'true_defect', 'defect': f['marker_id']} for f in fs}
        out = m.quality(fs, js, {('o/r', 1, 'a')})
        self.assertEqual(out['devin']['by_finding_kind']['analysis']['true'], 1)
        self.assertEqual(out['devin']['by_finding_kind']['bug']['true'], 1)

    def test_canonical_alias_and_legacy_verdicts(self):
        prefix = m.REPOS[-1] + '#71@83a4f024' + '0'*32
        fs = [dict(key=prefix+':devin:a', bot='devin', repo=m.REPOS[-1], pr=71, sha='83a4f024'+'0'*32),
              dict(key=prefix+':paperclip:b', bot='paperclip', repo=m.REPOS[-1], pr=71, sha='83a4f024'+'0'*32)]
        js = {fs[0]['key']: dict(verdict='true', defect='held-fixed-running-pool-budget'),
              fs[1]['key']: dict(verdict='true', defect='held-running-pool-occupancy')}
        result = m.quality(fs, m.normalize_judgments(js), {(m.REPOS[-1], 71, fs[0]['sha'])})
        self.assertEqual(result['reference']['defect_heads'], 1)
        self.assertEqual(result['devin']['recall_reference'], 1)
        self.assertEqual(result['paperclip']['recall_reference'], 1)

    def test_head_trigger_uses_push_time_and_timeline_draft_state(self):
        raw = {'pr': {'draft': False, 'head': {'sha': 'h'}}, 'timeline': [
            {'event': 'convert_to_draft', 'created_at': '2026-10-30T18:00:00Z'},
            {'event': 'push', 'head_sha': 'h', 'created_at': '2026-10-30T18:10:00Z'},
            {'event': 'ready_for_review', 'created_at': '2026-10-30T18:20:00Z'}]}
        self.assertEqual(m.head_trigger(raw), 'eligible')
        raw['timeline'].pop()
        self.assertEqual(m.head_trigger(raw), 'draft')
        del raw['timeline']
        self.assertEqual(m.head_trigger(raw), 'unknown')


class VersionManifestTests(unittest.TestCase):
    """G1: REVIEW.md versions come from main history frozen in the snapshot, not hard-coded blobs."""

    def test_history_dedupes_blobs_oldest_first_and_freezes_text(self):
        repo = 'TogetherWeOwn/two-web-next'
        commits = [{'sha': SHA['3'], 'commit': {'committer': {'date': '2026-10-31T01:00:00Z'}}},
                   {'sha': SHA['4'], 'commit': {'committer': {'date': '2026-10-30T22:00:00Z'}}},
                   {'sha': SHA['2'], 'commit': {'committer': {'date': '2026-10-30T20:00:00Z'}}},
                   {'sha': SHA['1'], 'commit': {'committer': {'date': '2026-10-30T03:00:00Z'}}}]
        blobs = {SHA['1']: SHA['a'], SHA['2']: SHA['b'], SHA['4']: SHA['b'], SHA['3']: SHA['c']}
        def get(endpoint):
            if 'path=REVIEW.md' in endpoint:
                return commits
            if '/git/blobs/' in endpoint:
                blob = endpoint.rsplit('/', 1)[1]
                return {'sha': blob, 'encoding': 'base64', 'content': base64.b64encode(('# R ' + blob[:1]).encode()).decode()}
            raise AssertionError(endpoint)
        with patch.object(m, 'gh_get', side_effect=get), patch.object(m, 'review_blob', side_effect=lambda r, sha: blobs[sha]), \
                patch.dict(m.V2_MERGES, {repo: SHA['2']}):
            versions = m.review_md_history(repo)
        self.assertEqual([v['label'] for v in versions],
                         ['two-web-next@v1:aaaaaaaa', 'two-web-next@v2:bbbbbbbb', 'two-web-next@v3:cccccccc'])
        self.assertEqual([v['post_v2'] for v in versions], [False, True, True])
        self.assertEqual(versions[2]['text'], '# R c')

    def test_v2_pin_numbers_a_repo_whose_first_file_was_v2(self):
        # two-bot-next had no v1: an ordinal count would call its v2 file "v1" and drop it.
        entries = [dict(commit=SHA['2'], blob=SHA['b']), dict(commit=SHA['3'], blob=SHA['c'])]
        out = m.number_versions('TogetherWeOwn/two-bot-next', entries, SHA['2'])
        self.assertEqual([(v['version'], v['post_v2']) for v in out], [(2, True), (3, True)])

    def test_unpinned_repo_never_qualifies_and_missing_pin_refuses(self):
        entries = [dict(commit=SHA['1'], blob=SHA['a']), dict(commit=SHA['2'], blob=SHA['b'])]
        out = m.number_versions('o/unpinned', [dict(e) for e in entries], None)
        self.assertEqual([(v['version'], v['post_v2']) for v in out], [(1, False), (2, False)])
        with self.assertRaises(ValueError):
            m.number_versions('o/r', [dict(e) for e in entries], SHA['9'])

    def test_deleted_file_is_labelled_and_never_qualifies(self):
        entries = [dict(commit=SHA['2'], blob=SHA['b']), dict(commit=SHA['3'], blob='none')]
        out = m.number_versions('o/r', entries, SHA['2'])
        self.assertEqual((out[1]['label'], out[1]['post_v2']), ('r@deleted', False))

    def test_tags_label_unversioned_and_absent_blobs(self):
        index = m.version_index(versions_manifest())
        repo = 'TogetherWeOwn/two-web-next'
        self.assertEqual(m.version_tag(repo, SHA['b'], index), dict(review_md_version='two-web-next@v2:bbbbbbbb', review_md_qualifies=True))
        self.assertEqual(m.version_tag(repo, SHA['a'], index), dict(review_md_version='two-web-next@v1:aaaaaaaa', review_md_qualifies=False))
        self.assertEqual(m.version_tag(repo, SHA['f'], index)['review_md_version'], 'unversioned:ffffffff')
        self.assertFalse(m.version_tag(repo, 'none', index)['review_md_qualifies'])
        self.assertFalse(m.version_tag('TogetherWeOwn/kofra', SHA['b'], index)['review_md_qualifies'])

    def test_post_v2_gate_excludes_review_md_prs_and_pre_v2_heads(self):
        def p(number, files, qualifies):
            return {'repo': 'o/r', 'number': number, 'raw': {'files': files},
                    'reviews': [{'bot': 'devin', 'sha': str(number), 'review_md_qualifies': qualifies},
                                {'bot': 'paperclip', 'sha': 'p' + str(number), 'review_md_qualifies': True}]}
        prs = [p(1, [{'filename': 'app.py'}], True), p(2, [{'filename': 'REVIEW.md'}], True),
               p(3, [{'filename': 'web/REVIEW.md'}], True), p(4, [{'filename': 'NOTES.md', 'previous_filename': 'REVIEW.md'}], True),
               p(5, [{'filename': 'app.py'}], False)]
        self.assertEqual(m.post_v2_heads(prs), {('o/r', 1, '1')})

    def test_normalize_refuses_snapshot_without_frozen_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            snap = write_snapshot(pathlib.Path(d), [('o/r', pr_raw(1, SHA['c']))], versions_manifest())
            (snap / m.VERSIONS_FILE).unlink()
            with self.assertRaises(ValueError):
                m.normalize(snap)

    def test_freeze_manifest_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            snap = write_snapshot(pathlib.Path(d), [('o/r', pr_raw(1, SHA['c']))], versions_manifest())
            with self.assertRaises(ValueError):
                m.freeze_manifest(snap)

    def test_findings_and_reviews_carry_review_md_version(self):
        repo = 'TogetherWeOwn/two-web-next'
        review = {'id': 1, 'commit_id': SHA['c'], 'submitted_at': '2026-10-30T17:00:00Z', 'user': DEVIN, 'html_url': 'u',
                  'body': '<!-- devin-review-comment {"id":"BUG_1","file_path":"a.py","end_line":3} -->\nbug'}
        with tempfile.TemporaryDirectory() as d:
            snap = write_snapshot(pathlib.Path(d), [(repo, pr_raw(1, SHA['c'], reviews=[review]))], versions_manifest(repo))
            _, prs = m.normalize(snap)
        self.assertEqual(prs[0]['findings'][0]['review_md_version'], 'two-web-next@v2:bbbbbbbb')
        self.assertTrue(prs[0]['reviews'][0]['review_md_qualifies'])


class EscapeTests(unittest.TestCase):
    """G2: escape candidates from reverts, Regression-of lines and regression issues."""

    def fake_github(self):
        repo = 'TogetherWeOwn/kofra'
        prs = {7: {'number': 7, 'state': 'closed', 'merged_at': '2026-10-20T10:00:00Z', 'head': {'sha': SHA['7']},
                   'merge_commit_sha': SHA['8'], 'html_url': 'pr7', 'base': {'repo': {'full_name': repo}}},
               9: {'number': 9, 'state': 'closed', 'merged_at': '2026-10-21T10:00:00Z', 'head': {'sha': SHA['9']},
                   'merge_commit_sha': SHA['e'], 'html_url': 'pr9', 'base': {'repo': {'full_name': repo}}}}
        def get(endpoint):
            if endpoint.startswith(f'/repos/{repo}/commits?sha=main'):
                return [{'sha': SHA['d'], 'commit': {'message': f'Revert "x"\n\nThis reverts commit {SHA["8"]}.',
                                                     'committer': {'date': '2026-10-25T00:00:00Z'}}}]
            if endpoint == f'/repos/{repo}/commits/{SHA["8"]}/pulls':
                return [prs[7]]
            if endpoint.startswith(f'/repos/{repo}/pulls?state=all'):
                return [{'number': 12, 'updated_at': '2026-10-26T00:00:00Z', 'body': 'Fix.\n\nRegression-of: TogetherWeOwn/kofra#9\n'},
                        {'number': 13, 'updated_at': '2026-10-26T00:00:00Z', 'body': 'mentions Regression-of: TogetherWeOwn/kofra#5 inline'}]
            if endpoint.startswith(f'/repos/{repo}/issues?labels=regression'):
                return [{'number': 40, 'updated_at': '2026-10-27T00:00:00Z', 'body': 'Broke after #7, see #41 and https://github.com/o/elsewhere/pull/3'},
                        {'number': 42, 'pull_request': {}, 'body': 'a PR, not an issue'}]
            if endpoint == f'/repos/{repo}/issues/7':
                return {'number': 7, 'pull_request': {}}
            if endpoint == f'/repos/{repo}/issues/41':
                return {'number': 41}
            if endpoint.startswith(f'/repos/{repo}/pulls/'):
                return prs[int(endpoint.rsplit('/', 1)[1])]
            if endpoint.startswith(f'/repos/{repo}/commits/{SHA["7"]}/statuses'):
                return [status('success', 'Completed analysis in 1m', '2026-10-20T09:00:00Z')]
            if endpoint.startswith(f'/repos/{repo}/commits/{SHA["9"]}/statuses'):
                return [status('success', 'Completed analysis in 1m', '2026-10-21T11:00:00Z')]
            raise AssertionError(endpoint)
        return repo, get

    def test_collect_and_report_candidates_against_merge_head_status(self):
        repo, get = self.fake_github()
        with patch.object(m, 'gh_get', side_effect=get):
            escapes = m.collect_escapes([repo], '2026-10-03T00:00:00Z', '2026-10-31T00:00:00Z')
        self.assertEqual(sorted(s['source'] for s in escapes['sources']), ['regression_issue', 'regression_of', 'revert'])
        issue = next(s for s in escapes['sources'] if s['source'] == 'regression_issue')
        # #41 is an issue, not a PR; the out-of-scope PR link is kept but never fetched.
        self.assertEqual([(r['repo'], r['number']) for r in issue['introducing']], [('o/elsewhere', 3), (repo, 7)])
        report = m.escape_report(escapes)
        rows = {r['introducing_pr']: r for r in report['candidates']}
        self.assertTrue(rows[repo + '#7']['devin_completed_at_merge_head'])
        # Devin finished after the merge: the merge head was not Devin-passed.
        self.assertFalse(rows[repo + '#9']['devin_completed_at_merge_head'])
        self.assertFalse(rows['o/elsewhere#3']['in_scope'])
        self.assertEqual(len(rows[repo + '#7']['evidence']), 2)
        self.assertEqual((report['devin_passed_candidates'], report['devin_not_completed_candidates']), (1, 1))

    def test_regression_of_requires_its_own_line(self):
        self.assertEqual(m.REGRESSION_OF.findall('Regression-of: o/r#4\nRegression-of: o/r#5 extra\n'), [('o/r', '4')])

    def test_missing_escape_data_is_unavailable_not_zero(self):
        self.assertFalse(m.escape_report(None)['available'])


class FreshnessTests(unittest.TestCase):
    """G3: did Devin review the final head before merge?"""

    def prs(self):
        def p(number, merged_at, statuses, reviews=()):
            raw = pr_raw(number, SHA[str(number)], merged_at=merged_at, statuses=statuses, reviews=reviews)
            return dict(repo='o/r', number=number, merged=bool(merged_at), merged_at=merged_at, head=SHA[str(number)],
                        head_trigger='eligible', raw=raw)
        return [p(1, '2026-10-30T18:00:00Z', {SHA['1']: [status('success', 'Completed analysis', '2026-10-30T17:00:00Z')]}),
                p(2, '2026-10-30T18:00:00Z', {SHA['2']: [status('success', 'Completed analysis', '2026-10-30T19:00:00Z')]}),
                p(3, '2026-10-30T18:00:00Z', {}, reviews=[{'id': 1, 'commit_id': SHA['a'], 'submitted_at': '2026-10-30T17:00:00Z',
                                                           'user': DEVIN, 'html_url': 'r', 'body': ''}]),
                p(4, '2026-10-29T18:00:00Z', {SHA['4']: [status('success', 'Completed analysis', '2026-10-29T17:00:00Z')]}),
                p(5, None, {})]

    def test_lists_misses_and_skips_merges_before_cutoff(self):
        result = m.freshness_at_merge(self.prs(), '2026-10-30T00:00:00Z')
        self.assertEqual((result['merged_prs'], result['devin_reviewed_merge_head']), (3, 1))
        self.assertEqual([x['pr'] for x in result['misses']], [2, 3])
        self.assertEqual(result['misses'][1]['last_devin_reviewed_sha'], SHA['a'])
        self.assertIsNone(result['misses'][0]['last_devin_reviewed_sha'])


class ThreadAnswerTests(unittest.TestCase):
    """G4: strict F/R/D first-line answers before merge, looser frd() kept as a secondary column."""

    def test_first_line_forms(self):
        self.assertEqual(m.thread_answer('Fixed in 1a2b3c4\nmore'), 'F')
        self.assertEqual(m.thread_answer('\n  Rejected: x.py:4 checks the caller.'), 'R')
        self.assertEqual(m.thread_answer('Duplicate of https://github.com/o/r/pull/1#discussion_r9'), 'D')
        self.assertIsNone(m.thread_answer('Thanks!\nFixed in 1a2b3c4'))
        self.assertIsNone(m.thread_answer('Rejected:'))
        self.assertIsNone(m.thread_answer('Fixed in main'))

    def test_counts_only_human_answers_before_merge(self):
        finding = dict(bot='devin', inline_id=10, key='k1', url='u')
        other = dict(bot='devin', inline_id=20, key='k2', url='u2')
        comments = [
            {'id': 11, 'in_reply_to_id': 10, 'user': {'login': 'devin-ai-integration[bot]', 'type': 'Bot'}, 'body': 'Fixed in 1a2b3c4', 'created_at': '2026-10-30T10:00:00Z', 'html_url': 'bot'},
            {'id': 12, 'in_reply_to_id': 10, 'user': HUMAN, 'body': 'Fixed in 1a2b3c4d', 'created_at': '2026-10-30T11:00:00Z', 'html_url': 'h1'},
            {'id': 21, 'in_reply_to_id': 20, 'user': HUMAN, 'body': 'Fixed in 1a2b3c4d', 'created_at': '2026-10-30T13:00:00Z', 'html_url': 'late'},
            {'id': 22, 'in_reply_to_id': 20, 'user': HUMAN, 'body': 'Resolved in 1a2b3c4d at x.py:3', 'created_at': '2026-10-30T11:00:00Z', 'html_url': 'loose'}]
        prs = [dict(merged=True, merged_at='2026-10-30T12:00:00Z', findings=[finding, other], raw={'comments': comments})]
        result = m.thread_answers(prs)
        self.assertEqual((result['threads'], result['answered'], result['loose_frd_secondary']), (2, 1, 2))
        self.assertEqual(result['unanswered_merged'], ['k2'])
        self.assertEqual(result['threads_detail'][0]['answer_url'], 'h1')


class RuleIdTests(unittest.TestCase):
    """G5: stable rule ids from REVIEW.md text and per-rule true-finding counts."""

    TEXT = ('# Review guidelines\n\n## What merits a finding\n- **Severe (red):** x\n  **not a lead** wrapped\n'
            '- **Security exception:** y\n\n## Test rigor\nprose\n1. **Oracle first** z\n## Skip\n')

    def test_parse_sections_and_bold_lead_ins(self):
        self.assertEqual(m.parse_rules(self.TEXT), ['what-merits-a-finding', 'what-merits-a-finding/severe-red',
                                                    'what-merits-a-finding/not-a-lead', 'what-merits-a-finding/security-exception',
                                                    'test-rigor', 'test-rigor/oracle-first', 'skip'])
        self.assertEqual(m.parse_rules('**Lead** before any heading'), ['preamble/lead'])

    def test_unknown_rule_id_is_rejected(self):
        good = {'o/r#1@a:devin:x': dict(verdict='true_defect', rationale='r', evidence=['e'], rule_id='skip')}
        m.validate_judgments(good, {'o/r': {'skip'}})
        bad = {'o/r#1@a:devin:x': dict(verdict='true_defect', rationale='r', evidence=['e'], rule_id='made-up')}
        with self.assertRaises(ValueError):
            m.validate_judgments(bad, {'o/r': {'skip'}})

    def test_counts_and_two_round_zero_list_need_exposure(self):
        repo = 'TogetherWeOwn/two-web-next'
        versions = versions_manifest(repo, [dict(commit=SHA['2'], committed_at='t', blob=SHA['b'], text=self.TEXT)])
        findings = [dict(key='k1', bot='devin', repo=repo, pr=1, sha='h'), dict(key='k2', bot='devin', repo=repo, pr=2, sha='old')]
        judgments = {'k1': dict(verdict='true_defect', rule_id='test-rigor/oracle-first'),
                     'k2': dict(verdict='true_defect', rule_id='skip')}
        qualifying = {(repo, 1, 'h')}
        first = m.rule_metrics(versions, findings, judgments, qualifying, None)
        self.assertEqual(first['counts'][repo]['test-rigor/oracle-first'], {'devin_true': 1, 'devin_findings': 1})
        self.assertEqual(first['counts'][repo]['skip']['devin_true'], 0, 'k2 is not on a post-v2 head')
        self.assertIsNone(first['zero_true_last_two_rounds'])
        second = m.rule_metrics(versions, [], {}, qualifying, {'rules': first})
        self.assertIn('skip', second['zero_true_last_two_rounds'][repo])
        self.assertNotIn('test-rigor/oracle-first', second['zero_true_last_two_rounds'][repo])
        unexercised = m.rule_metrics(versions, [], {}, set(), {'rules': first})
        self.assertIsNone(unexercised['zero_true_last_two_rounds'][repo])


class GoldenTests(unittest.TestCase):
    """G6: committed golden set shape and the file/line-window scorer."""

    def cases(self):
        return json.loads((HERE / 'golden.json').read_text())

    def test_committed_golden_set_is_valid(self):
        cases = m.validate_golden(self.cases())
        self.assertEqual({c['kind'] for c in cases}, set(m.GOLDEN_KINDS))
        self.assertGreaterEqual(len({c['repo'] for c in cases}), 3)

    def test_golden_rejects_extra_fields_and_unbalanced_kinds(self):
        cases = self.cases()
        cases[0]['exploit'] = 'payload'
        with self.assertRaises(ValueError):
            m.validate_golden(cases)
        cases = self.cases()
        cases[0]['kind'] = 'gate'
        with self.assertRaises(ValueError):
            m.validate_golden(cases)
        cases = self.cases()
        for c in cases:
            c['baseline_devin'] = 'missed'
        with self.assertRaises(ValueError):
            m.validate_golden(cases)

    def test_window_match_and_unmatched_queue(self):
        case = self.cases()[0]
        f = lambda key, path, line, sha=case['head_sha']: dict(key=key, bot='devin', repo=case['repo'], pr=999, sha=sha, path=path, line=line, url='u')
        findings = [f('hit', case['file'], case['line_end'] + 5), f('far', case['file'], case['line_end'] + 6),
                    f('other-file', 'x', case['line_start']), f('other-head', case['file'], case['line_start'], sha=SHA['0']),
                    dict(f('paperclip', case['file'], case['line_start']), bot='paperclip')]
        result = m.golden_score(self.cases(), findings, 5)
        row = next(r for r in result['cases'] if r['id'] == case['id'])
        self.assertEqual(row['matched_findings'], ['hit'])
        self.assertEqual(sorted(q['key'] for q in result['adjudication_queue']), ['far', 'other-file'])
        self.assertEqual(result['caught'], 1)


class StatusSignalTests(unittest.TestCase):
    """G7: every non-'Completed analysis' Devin status is flagged except superseded pending ones."""

    def test_flags(self):
        statuses = {SHA['1']: [status('pending', 'Analyzing your changes', '2026-10-30T10:00:00Z', ident=1),
                               status('success', 'Completed analysis in 1m', '2026-10-30T10:01:00Z', ident=2)],
                    SHA['2']: [status('error', 'Usage limit reached', '2026-10-30T10:00:00Z', ident=3)],
                    SHA['3']: [status('pending', 'Analyzing your changes', '2026-10-30T10:00:00Z', ident=4),
                               status('success', 'Skipped', '2026-10-30T10:00:00Z', context='Other', ident=5)]}
        prs = [dict(repo='o/r', raw={'statuses': statuses})]
        result = m.devin_status_signals(prs, None)
        self.assertEqual([(f['sha'], f['description']) for f in result['flags']],
                         [(SHA['2'], 'Usage limit reached'), (SHA['3'], 'Analyzing your changes')])
        self.assertEqual(result['superseded_in_progress'], 1)


class ReplayTests(unittest.TestCase):
    """G8: offline replay is byte-identical and never writes inside the repository."""

    def build(self, root):
        repo = 'TogetherWeOwn/two-web-next'
        review = {'id': 1, 'commit_id': SHA['c'], 'submitted_at': '2026-10-30T17:00:00Z', 'user': DEVIN, 'html_url': 'u',
                  'body': '<!-- devin-review-comment {"id":"BUG_1","file_path":"a.py","end_line":3} -->\nbug'}
        raws = [(repo, pr_raw(1, SHA['c'], reviews=[review], merged_at='2026-10-30T18:00:00Z',
                              statuses={SHA['c']: [status('success', 'Completed analysis', '2026-10-30T17:00:01Z')]})),
                (repo, pr_raw(2, SHA['d'], files=('REVIEW.md',), reviews=[dict(review, id=2, commit_id=SHA['d'])]))]
        write_snapshot(root, raws, versions_manifest(repo), escapes=dict(window_start='s', window_end='e', lookback_days=28,
                                                                        sources=[], introducing={}))
        (root / 'adjudications.json').write_text(json.dumps({f'{repo}#1@{SHA["c"]}:devin:BUG_1': dict(
            verdict='true_defect', defect='d', blocking=True, kind='behavior', rationale='r', evidence=['e'],
            rule_id='what-merits-a-finding/severe-red')}))

    def test_two_scoring_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            self.build(root)
            m.score('rt', root)
            first = root / 'first'
            (root / 'out' / 'rt').rename(first)
            result = m.score('rt', root)
            comparison = filecmp.dircmp(first, root / 'out' / 'rt')
            self.assertEqual(sorted(comparison.common_files), ['adjudication-queue.json', 'metrics.json', 'normalized.json'])
            for name in comparison.common_files:
                self.assertEqual((first / name).read_bytes(), (root / 'out' / 'rt' / name).read_bytes(), name)
        self.assertEqual(result['post_v2_quality']['qualifying_heads'], 1)
        self.assertEqual(result['review_md_versions']['excluded_review_md_prs'], ['TogetherWeOwn/two-web-next#2'])
        self.assertEqual(result['rules']['counts']['TogetherWeOwn/two-web-next']['what-merits-a-finding/severe-red']['devin_true'], 1)
        self.assertEqual(result['freshness_at_merge']['devin_reviewed_merge_head'], 1)

    def test_workdir_inside_repository_is_refused(self):
        with self.assertRaises(ValueError):
            m.outside_repo(HERE)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            m.main(['score', '--workdir', str(HERE / 'tmp'), '--round', 'r1'])
        self.assertIn('outside the repository', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
