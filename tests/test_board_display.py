import unittest
from scripts.board_display import memberships, BOARD_LABELS, all_labels


class BoardDisplayTests(unittest.TestCase):
    def snapshot(self):
        return {'charts': [{'chart_id': k, 'complete_top50': True,
                'records': [{'book_id': str(i), 'rank': i} for i in range(1, 51)]}
                for k in BOARD_LABELS]}

    def test_overlaps_retained_as_one_label(self):
        result = memberships(self.snapshot())
        self.assertEqual(result['weread:1'], '出版飙升＋出版新书＋女生飙升＋女生新书')
        self.assertIn(result['weread:1'], all_labels())

    def test_incomplete_cannot_clear_previous_memberships(self):
        source = self.snapshot()
        source['charts'][0]['records'].pop()
        self.assertIsNone(memberships(source))

    def test_duplicate_id_rejected_even_with_50_ranks(self):
        source = self.snapshot()
        source['charts'][0]['records'][1]['book_id'] = '1'
        self.assertIsNone(memberships(source))
