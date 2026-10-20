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
        for status in ('pending', 'queued', 'deferred', 'coalesced'):
            self.read.return_value = [[COMMENT, status, '', 'f']]
            self.assertEqual('pending', self.reconcile())
            self.assertIn('pending_wake', self.rec)
        self.post.assert_called_once()

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


if __name__ == '__main__':
    unittest.main()
