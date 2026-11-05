import copy
import unittest

from scripts.recent_entry_tracking import observe, active_targets


class EntryTrackingTests(unittest.TestCase):
    provider = {'name': 'Example', 'copyright_id': 7}

    def snapshot(self, ids, day='2026-11-05', status='complete'):
        return {'provider': 'Example', 'status': status, 'total_count': len(ids),
                'returned_rows': len(ids), 'unique_ids': len(set(ids)),
                'source': {'observed_at': day+'T10:00:00+08:00'},
                'books': [{'book_id': bid, 'copyright_id': 7, 'title': 'Book '+bid} for bid in ids]}

    def step(self, state, ids, day='2026-11-05', status='complete'):
        return observe(state, [(self.provider, self.snapshot(ids, day, status))], day)

    def test_initial_baseline_does_not_create_historical_cohort(self):
        state, report = self.step(None, ['1', '2'])
        self.assertEqual(report['changes'][0]['status'], 'baseline_created')
        self.assertEqual(state['tracks'], {})

    def test_serial_update_and_reordering_do_not_create_new_entries(self):
        state, _ = self.step(None, ['1', '2'])
        snapshot = self.snapshot(['2', '1'], '2026-11-06')
        snapshot['books'][0].update(title='Changed title', recent_list_time='2026-11-06', reviewer_count=100)
        updated, report = observe(state, [(self.provider, snapshot)], '2026-11-06')
        self.assertEqual(updated['tracks'], {})
        self.assertEqual(report['changes'][0]['new_entries'], [])

    def test_new_id_tracks_30_days_and_continues_after_leaving(self):
        state, _ = self.step(None, ['1'])
        state, report = self.step(state, ['1', '2'], '2026-11-06')
        self.assertEqual(len(report['active_targets']), 1)
        self.assertEqual(state['tracks']['2']['end_date'], '2026-12-05')
        state, report = self.step(state, ['1'], '2026-11-07')
        self.assertEqual(len(report['active_targets']), 1)
        self.assertEqual(len(active_targets(state, '2026-12-05')), 1)
        self.assertEqual(active_targets(state, '2026-12-06'), [])

    def test_reentry_never_restarts_or_creates_new_cohort(self):
        state, _ = self.step(None, ['1'])
        state, _ = self.step(state, ['2'], '2026-11-06')
        original = copy.deepcopy(state['tracks']['2'])
        state, _ = self.step(state, [], '2026-11-07')
        state, report = self.step(state, ['1', '2'], '2026-12-10')
        self.assertEqual(state['tracks']['2'], original)
        self.assertNotIn('1', state['tracks'])
        self.assertEqual(report['changes'][0]['new_entries'], [])
        self.assertEqual(report['active_targets'], [])

    def test_failure_partial_stale_and_repeat_do_not_advance(self):
        state, _ = self.step(None, ['1'])
        for status in ['source_error', 'incomplete']:
            updated, _ = self.step(state, [], '2026-11-06', status)
            self.assertEqual(updated, state)
        for day in ['2026-11-04', '2026-11-05']:
            updated, _ = self.step(state, ['2'], day)
            self.assertEqual(updated, state)

    def test_duplicate_ids_cannot_advance(self):
        state, _ = self.step(None, ['1'])
        updated, report = self.step(state, ['2', '2'], '2026-11-06')
        self.assertEqual(updated, state)
        self.assertEqual(report['changes'][0]['status'], 'held_invalid')


if __name__ == '__main__':
    unittest.main()
