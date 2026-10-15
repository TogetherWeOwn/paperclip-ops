import datetime as dt
import errno
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'lib'))
sys.modules.setdefault('cardops', types.SimpleNamespace(comment=lambda *_args: True, sql=lambda _query: ''))

import owner_guard as og  # noqa: E402

TODAY = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
STAMP = '2026-10-04T21:44:57+00:00'
EMPTY = json.dumps({'commented': {}, 'digest_day': ''})
ROUTED = 'case_65397829|Owner click new PR|do the thing\n'


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'state.json'
        self.rows = ROUTED
        self.posts = []
        self.post_result = True
        self.post_raises = None

        def fake_comment(ident, _body):
            self.posts.append(ident)
            if self.post_raises:
                raise self.post_raises
            return self.post_result

        self.comment = mock.Mock(side_effect=fake_comment)
        self.stdout = mock.patch('sys.stdout', new_callable=io.StringIO)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)
        patched = {'STATE': self.state, 'LOG': self.dir / 'guard.log', 'PAUSE': self.dir / 'PAUSE', 'DRY': False,
                   'comment': self.comment, 'sql': lambda _query: self.rows}
        for name, value in patched.items():
            patch = mock.patch.object(og, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def disk(self):
        return json.loads(self.state.read_text())


class FailClosed(Base):
    def test_empty_state_posts_nothing(self):
        self.state.write_bytes(b'')
        with self.assertRaises(og.atomic_state.StateError):
            og.main()
        self.comment.assert_not_called()

    def test_missing_state_posts_nothing(self):
        with self.assertRaises(og.atomic_state.StateError):
            og.main()
        self.comment.assert_not_called()

    def test_malformed_or_invalid_state_posts_nothing(self):
        for raw in ('{"commented": {', '{"commented": {}}', '{"commented": {"x": "y"}, "digest_day": ""}',
                    '{"commented": {"case_8c5fe4cc": 5}, "digest_day": ""}', '{"commented": {}, "digest_day": "10/09/2026"}'):
            with self.subTest(raw=raw):
                self.state.write_text(raw)
                with self.assertRaises(og.atomic_state.StateError):
                    og.main()
        self.comment.assert_not_called()


class Posting(Base):
    def test_suppression_is_on_disk_before_the_post(self):
        self.state.write_text(EMPTY)
        seen = []

        def post(ident, _body):
            seen.append((ident, self.disk()['commented'].keys()))
            return True

        self.comment.side_effect = post
        og.main()
        self.assertEqual(seen[0][0], 'case_65397829')
        self.assertIn('case_65397829', seen[0][1])

    def test_posted_card_is_not_posted_again(self):
        self.state.write_text(EMPTY)
        og.main()
        og.main()
        self.assertEqual(self.posts, ['case_65397829', og.DIGEST_CARD])

    def test_failed_save_before_the_post_posts_nothing(self):
        self.state.write_text(EMPTY)
        before = self.state.read_bytes()
        full = OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(og.atomic_state.os, 'fsync', side_effect=full):
            with self.assertRaises(OSError):
                og.main()
        self.comment.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_false_post_is_retried_on_the_next_run(self):
        self.state.write_text(EMPTY)
        self.post_result = False
        og.main()
        self.assertEqual(self.disk()['commented'], {})
        self.post_result = True
        og.main()
        self.assertIn('case_65397829', self.disk()['commented'])
        self.assertEqual(self.posts.count('case_65397829'), 2)

    def test_post_that_raises_stays_suppressed(self):
        self.state.write_text(EMPTY)
        self.post_raises = RuntimeError('network down')
        with self.assertRaises(RuntimeError):
            og.main()
        self.assertIn('case_65397829', self.disk()['commented'])
        self.post_raises = None
        og.main()
        self.assertEqual(self.posts.count('case_65397829'), 1)


class Digest(Base):
    def state_with_card(self):
        self.state.write_text(json.dumps({'commented': {'case_65397829': STAMP}, 'digest_day': ''}))

    def test_digest_posts_once_per_day(self):
        self.state_with_card()
        og.main()
        og.main()
        self.assertEqual(self.posts, [og.DIGEST_CARD])
        self.assertEqual(self.disk()['digest_day'], TODAY)

    def test_failed_digest_post_is_retried(self):
        self.state_with_card()
        self.post_result = False
        og.main()
        self.assertEqual(self.disk()['digest_day'], '')
        self.post_result = True
        og.main()
        self.assertEqual(self.disk()['digest_day'], TODAY)


class Recovery(Base):
    def doc(self, **overrides):
        doc = {'provenance': {'source': 'board comments on every card', 'retrieved_at': '2026-10-09T03:40:00Z',
                              'complete': True},
               'commented': {'case_ff18cbc2': {'comment_id': 'c1', 'created_at': '2026-10-04T21:44:57Z'}},
               'digest': {'day': '2026-10-04', 'comment_id': 'd1'}}
        doc.update(overrides)
        return doc

    def write_doc(self, doc):
        path = self.dir / 'recovery.json'
        path.write_text(json.dumps(doc))
        return path

    def test_empty_state_recovers_from_verified_history_and_keeps_prior_cards_suppressed(self):
        self.state.write_bytes(b'')
        self.rows = ROUTED + 'case_ff18cbc2|Owner click new PR|older card\n'
        og.recover_state(self.write_doc(self.doc()))
        self.assertEqual(self.disk(), {'commented': {'case_ff18cbc2': '2026-10-04T21:44:57Z'}, 'digest_day': '2026-10-04'})
        og.main()
        self.assertNotIn('case_ff18cbc2', self.posts)
        self.assertEqual(self.posts.count('case_65397829'), 1)
        self.assertTrue(any('.replaced-' in p.name for p in self.dir.iterdir()))

    def test_null_digest_records_no_digest_day(self):
        self.state.write_bytes(b'')
        og.recover_state(self.write_doc(self.doc(digest=None)))
        self.assertEqual(self.disk()['digest_day'], '')

    def test_refuses_incomplete_or_unverified_history(self):
        self.state.write_bytes(b'')
        bad = [self.doc(provenance={'source': 's', 'retrieved_at': 't', 'complete': False}),
               self.doc(commented={'case_ff18cbc2': {'created_at': 't'}}),
               self.doc(digest={'day': 'not-a-day', 'comment_id': 'd1'})]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                og.recover_state(self.write_doc(doc))
        self.assertEqual(self.state.read_bytes(), b'')

    def test_refuses_to_overwrite_valid_state(self):
        self.state.write_text(EMPTY)
        with self.assertRaises(og.atomic_state.StateError):
            og.recover_state(self.write_doc(self.doc()))
        self.assertEqual(self.disk(), {'commented': {}, 'digest_day': ''})


if __name__ == '__main__':
    unittest.main()
