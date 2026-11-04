import unittest
from scripts.coverage import REQUIRED_BOARDS, weread_coverage
from scripts.daily_run import pipeline_exit_code


class CoverageTests(unittest.TestCase):
    def snapshot(self):
        return {'charts': [{'chart_id': cid, 'complete_top50': True, 'records': [{'book_id': '1'}]}
                           for cid in REQUIRED_BOARDS],
                'books': [{'book_id': '1', 'status': 'ok', 'reviewer_count': 0,
                           'recommendation_percent': 91, 'today_reading_count': 0}]}

    def test_zero_is_observed_and_unknown_identity_is_partial_not_source_failure(self):
        result = weread_coverage(self.snapshot(), [{'book_id': '1'}], ['pending:123'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_metric_fields'], {})
        self.assertEqual(pipeline_exit_code({'modules': {'weread': {'coverage': result}}}), 0)

    def test_tracked_book_missing_after_leaving_chart_is_actionable(self):
        result = weread_coverage(self.snapshot(), [{'book_id': '1'}, {'book_id': '2'}], [])
        self.assertEqual(result['missing_book_ids'], ['2'])
        self.assertEqual(pipeline_exit_code({'modules': {'weread': {'coverage': result}}}), 2)

    def test_incomplete_board_is_actionable_even_if_books_were_returned(self):
        snapshot = self.snapshot()
        snapshot['charts'][3]['complete_top50'] = False
        result = weread_coverage(snapshot, [], [])
        self.assertEqual(result['incomplete_boards'], ['newbook_novel_female'])
        self.assertEqual(result['status'], 'requires_review')

    def test_official_unavailable_metric_is_not_fabricated_as_source_failure(self):
        snapshot = self.snapshot()
        snapshot['books'][0]['today_reading_count'] = None
        result = weread_coverage(snapshot, [], [])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_metric_fields'], {'1': ['today_reading_count']})
