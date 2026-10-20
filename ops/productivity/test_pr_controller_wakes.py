"""Offline request/claim lifecycle: no database, network, or agent configuration."""
import json
import subprocess
import unittest
from unittest.mock import Mock

from pr_controller_wakes import post_wake, reconcile_wake

ISSUE = '00000000-0000-4000-8000-000000000001'
COMMENT = '00000000-0000-4000-8000-000000000002'
RUN = '00000000-0000-4000-8000-000000000003'
KEY = 'findings@example-head'


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.rec = {}
        self.post = Mock(return_value=('201', json.dumps({'id': COMMENT})))
        self.read = Mock(return_value=[])
        self.now = Mock(return_value='2026-01-01T00:00:00Z')

    def send(self):
        return post_wake(self.rec, KEY, ISSUE, "A review's findings", self.post)

    def reconcile(self):
        return reconcile_wake(self.rec, KEY, ISSUE, "A review's findings", self.read, self.now)

    def test_comment_http_success_is_only_pending(self):
        self.send()
        self.assertNotIn('woke', self.rec)
        self.assertEqual(COMMENT, self.rec['pending_wake']['comment_id'])

    def test_any_skipped_reason_is_retryable_on_next_pass(self):
        for reason in ('heartbeat.wakeOnDemand.disabled', 'capacity', 'unknown'):
            with self.subTest(reason=reason):
                self.rec = {}
                self.send()
                self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
                self.assertEqual('retry', self.reconcile())
                self.assertNotIn('woke', self.rec)
                self.assertNotIn('pending_wake', self.rec)
                self.send()
                self.assertIn('pending_wake', self.rec)

    def test_skipped_then_retry_then_claim_closes_delivery(self):
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.send()
        self.read.return_value = [[COMMENT, 'claimed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])
        self.assertNotIn('pending_wake', self.rec)
        self.assertEqual(self.now.return_value, self.rec['rewake'][KEY])
        self.assertEqual(2, self.post.call_count)

    def test_queued_or_deferred_request_is_not_duplicated(self):
        self.send()
        for status in ('pending', 'queued', 'deferred'):
            self.read.return_value = [[COMMENT, status, '', 'f']]
            self.assertEqual('pending', self.reconcile())
            self.assertIn('pending_wake', self.rec)
        self.post.assert_called_once()

    def test_coalesced_delivery_with_run_counts_as_claimed(self):
        self.send()
        self.read.return_value = [[COMMENT, 'coalesced', RUN, 'f']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])
        self.assertNotIn('pending_wake', self.rec)
        self.assertEqual(self.now.return_value, self.rec['rewake'][KEY])
        self.assertEqual(RUN, self.rec['wake_claimed']['run_id'])
        # Stays claimed without re-querying; woke is set for the stale-reminder path.
        self.read.reset_mock()
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual('claimed', self.reconcile())
        self.read.assert_not_called()

    def test_coalesced_without_run_stays_pending(self):
        self.send()
        self.read.return_value = [[COMMENT, 'coalesced', '', 'f']]
        self.assertEqual('pending', self.reconcile())
        self.assertIn('pending_wake', self.rec)
        self.assertNotIn('woke', self.rec)

    def test_legacy_coalesced_with_run_counts_as_claimed(self):
        self.rec['woke'] = KEY
        self.read.return_value = [[COMMENT, 'coalesced', RUN, 'f']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])

    def test_linked_run_without_claim_is_still_pending(self):
        self.send()
        self.read.return_value = [[COMMENT, 'queued', RUN, 'f']]
        self.assertEqual('pending', self.reconcile())
        self.assertNotIn('woke', self.rec)

    def test_completed_claim_counts_even_after_run_finishes(self):
        self.send()
        self.read.return_value = [[COMMENT, 'completed', RUN, 'true']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])

    def test_acknowledged_claim_is_not_requeried_on_every_inventory_pass(self):
        self.send()
        self.read.return_value = [[COMMENT, 'completed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.read.reset_mock()
        self.assertEqual('claimed', self.reconcile())
        self.read.assert_not_called()

    def test_new_pending_reminder_is_not_hidden_by_an_earlier_claim(self):
        self.send()
        self.read.return_value = [[COMMENT, 'completed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        # Immediate repost is held by backoff; after it elapses the new event posts.
        self.assertEqual('pending', self.reconcile())
        self.now.return_value = '2026-01-01T02:00:00Z'
        self.assertEqual('new', self.reconcile())

    def test_failed_or_cancelled_request_retries(self):
        for status in ('failed', 'cancelled'):
            self.rec = {}
            self.send()
            self.read.return_value = [[COMMENT, status, '', 'f']]
            self.assertEqual('retry', self.reconcile())

    def test_missing_request_after_comment_does_not_count_as_delivery(self):
        self.send()
        self.assertEqual('retry', self.reconcile())
        self.assertNotIn('woke', self.rec)

    def test_head_change_discards_old_pending_request(self):
        self.send()
        self.assertEqual('new', reconcile_wake(self.rec, 'findings@new-head', ISSUE, '', self.read, self.now))
        self.assertNotIn('pending_wake', self.rec)
        self.read.assert_not_called()

    def test_owner_change_discards_old_pending_request(self):
        self.send()
        new_owner = '00000000-0000-4000-8000-000000000004'
        self.assertEqual('new', reconcile_wake(self.rec, KEY, new_owner, '', self.read, self.now))
        self.assertNotIn('pending_wake', self.rec)

    def test_legacy_http_dedup_is_reopened_when_wake_was_skipped(self):
        self.rec['woke'] = KEY
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertNotIn('woke', self.rec)
        sql = self.read.call_args.args[0]
        self.assertIn("position('A review''s findings' in c.body)", sql)
        self.assertIn(f"c.issue_id='{ISSUE}'", sql)
        self.assertIn("w.payload->>'commentId'=c.id::text", sql)

    def test_legacy_queued_request_is_tracked_without_reposting(self):
        self.rec['woke'] = KEY
        self.read.return_value = [[COMMENT, 'queued', '', 'f']]
        self.assertEqual('pending', self.reconcile())
        self.assertNotIn('woke', self.rec)
        self.assertEqual(COMMENT, self.rec['pending_wake']['comment_id'])
        self.post.assert_not_called()

    def test_legacy_claimed_request_preserves_dedup(self):
        self.rec['woke'] = KEY
        self.read.return_value = [[COMMENT, 'completed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])

    def test_missing_legacy_evidence_does_not_trigger_a_migration_flood(self):
        self.rec['woke'] = KEY
        self.assertEqual('legacy', self.reconcile())
        self.assertEqual(KEY, self.rec['woke'])

    def test_failed_post_and_malformed_response_never_mark_delivery(self):
        for response in [('500', '{}'), ('200', '{}'), ('200', 'not-json'), ('200', '{"id":"bad"}')]:
            self.rec = {}
            self.post.return_value = response
            self.send()
            self.assertNotIn('woke', self.rec)
            self.assertNotIn('pending_wake', self.rec)

    def test_failed_read_preserves_request_without_reposting(self):
        self.send()
        self.read.side_effect = subprocess.CalledProcessError(1, 'psql')
        self.assertEqual('pending', self.reconcile())
        self.assertIn('pending_wake', self.rec)
        self.post.assert_called_once()

    def test_unrecognized_read_shape_is_not_delivery(self):
        self.send()
        self.read.return_value = [['unexpected']]
        self.assertEqual('pending', self.reconcile())
        self.assertNotIn('woke', self.rec)

    def test_invalid_stored_comment_id_is_not_interpolated(self):
        self.send()
        self.rec['pending_wake']['comment_id'] = "x'; select 1; --"
        self.assertEqual('pending', self.reconcile())
        self.read.assert_not_called()

    def test_stale_reminder_uses_the_same_acknowledgement_path(self):
        self.rec['woke'] = KEY
        self.rec['rewake'] = {KEY: '2025-12-31T00:00:00Z'}
        self.send()
        self.assertEqual('2025-12-31T00:00:00Z', self.rec['rewake'][KEY])
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertNotIn('woke', self.rec)
        self.send()
        self.read.return_value = [[COMMENT, 'claimed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.assertEqual(self.now.return_value, self.rec['rewake'][KEY])

    def test_permanently_skipped_row_posts_bounded_comments(self):
        # Five controller passes against a permanently skipped row must not post 5×.
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        for _ in range(5):
            result = self.reconcile()
            if result in ('new', 'retry'):
                self.send()
        self.assertLessEqual(self.post.call_count, 2)
        self.assertEqual('pending', self.reconcile())

    def test_retry_backoff_elapses_then_retries_again(self):
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertEqual(1, self.rec['wake_retries'][KEY]['count'])
        self.assertEqual('pending', self.reconcile())
        self.now.return_value = '2026-01-01T02:00:00Z'
        self.assertEqual('new', self.reconcile())
        self.send()
        self.assertEqual('retry', self.reconcile())
        self.assertEqual(2, self.rec['wake_retries'][KEY]['count'])

    def test_retry_cap_stops_reposting_even_after_backoff(self):
        from pr_controller_wakes import WAKE_MAX_RETRIES
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        posts = 0
        # Advance time past every backoff so only the cap can stop us.
        for day in range(1, WAKE_MAX_RETRIES + 3):
            self.now.return_value = f'2026-01-{day:02d}T00:00:00Z'
            result = self.reconcile()
            if result in ('new', 'retry'):
                self.send()
                posts += 1
        self.assertEqual(1 + WAKE_MAX_RETRIES, posts)
        self.assertEqual(1 + WAKE_MAX_RETRIES, self.post.call_count)
        self.now.return_value = '2026-02-01T00:00:00Z'
        self.assertEqual('pending', self.reconcile())
        self.assertEqual(1 + WAKE_MAX_RETRIES, self.post.call_count)

    def test_retry_resets_on_claim(self):
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertEqual(1, self.rec['wake_retries'][KEY]['count'])
        self.send()
        self.read.return_value = [[COMMENT, 'claimed', RUN, 't']]
        self.assertEqual('claimed', self.reconcile())
        self.assertNotIn(KEY, self.rec.get('wake_retries', {}))
        # A later skip for the same key starts over at attempt 1.
        self.rec.pop('wake_claimed', None)
        self.rec.pop('woke', None)
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertEqual(1, self.rec['wake_retries'][KEY]['count'])

    def test_retry_resets_on_head_change(self):
        self.send()
        self.read.return_value = [[COMMENT, 'skipped', '', 'f']]
        self.assertEqual('retry', self.reconcile())
        self.assertIn(KEY, self.rec.get('wake_retries', {}))
        self.send()
        self.assertEqual('new', reconcile_wake(self.rec, 'findings@new-head', ISSUE, '', self.read, self.now))
        self.assertNotIn(KEY, self.rec.get('wake_retries', {}))

    def test_missing_row_retries_are_bounded(self):
        self.send()
        self.read.return_value = []
        self.assertEqual('retry', self.reconcile())
        # Immediate next pass is held; the missing row does not repost every pass.
        self.assertEqual('pending', self.reconcile())
        self.now.return_value = '2026-01-01T02:00:00Z'
        self.assertEqual('new', self.reconcile())


if __name__ == '__main__':
    unittest.main()
