import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import daily_run
from scripts.merge_weread_supplement import merge


def snapshot(hour, score, chart_book='a'):
    stamp = f'2026-11-03T{hour}:00:00+08:00'
    return {'collection_date': '2026-11-03', 'started_at': stamp, 'finished_at': stamp,
            'books': [{'book_id': 'a', 'recommendation_percent': score,
                       'detail_source': {'observed_at': stamp}}],
            'charts': [{'chart_id': 'rising', 'complete_top50': True,
                        'requests': [{'observed_at': stamp}], 'records': [{'book_id': chart_book}]}]}


class ObservationMergeTests(unittest.TestCase):
    def test_supplement_argument_order_cannot_regress_latest_observation(self):
        first, early, late = snapshot('10', 89), snapshot('11', 89.8, 'b'), snapshot('12', 90.2, 'c')
        for supplements in ([early, late], [late, early]):
            result = merge(first, supplements)
            self.assertEqual(result['books'][0]['recommendation_percent'], 90.2)
            self.assertEqual(result['charts'][0]['records'][0]['book_id'], 'c')
        self.assertEqual(first['books'][0]['recommendation_percent'], 89)

    def test_detail_observed_time_wins_over_later_job_finish(self):
        first, stale = snapshot('12', 90.2), snapshot('11', 89.8)
        stale['finished_at'] = '2026-11-03T13:00:00+08:00'
        self.assertEqual(merge(first, [stale])['books'][0]['recommendation_percent'], 90.2)

    def test_partial_supplement_rejected_and_legacy_chart_removed(self):
        original, supplement = snapshot('10', 89), snapshot('11', 90)
        original['charts'][0].update(chart_id='newbook_novel_female_store', complete_top50=False)
        supplement['charts'][0]['chart_id'] = 'newbook_novel_female'
        self.assertEqual([c['chart_id'] for c in merge(original, [supplement])['charts']], ['newbook_novel_female'])
        supplement['charts'][0]['complete_top50'] = False
        with self.assertRaises(ValueError):
            merge(original, [supplement])

    def test_child_timeout_is_bounded_without_retry(self):
        with TemporaryDirectory() as directory, patch.object(daily_run.subprocess, 'run') as run:
            run.side_effect = subprocess.TimeoutExpired('sync_weread.py', 9)
            with self.assertRaisesRegex(RuntimeError, 'reconcile receipts'):
                daily_run.call_script('sync_weread.py', [], Path(directory) / 'sync.log', timeout=9)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['timeout'], 9)


if __name__ == '__main__':
    unittest.main()
