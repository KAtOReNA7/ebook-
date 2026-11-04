"""Safety checks for metric semantics, precision and book-identity matching."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collect_fanqie", ROOT / "scripts/collect_fanqie.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class FanqieMetricTests(unittest.TestCase):
    def test_rounded_display_cannot_assert_crossing(self):
        metric = collector.cumulative_display("1.0万人读过")
        self.assertEqual(metric["value"], 10000)
        self.assertEqual((metric["lower_bound"], metric["upper_bound"]), (9000, 11000))
        self.assertEqual(collector.strict_threshold_state(metric, 10000), "uncertain_due_to_display_precision")
        self.assertEqual(collector.strict_threshold_state(collector.cumulative_display("1.2万人读过"), 10000), "above")

    def test_exact_count_and_strict_boundary(self):
        metric = collector.cumulative_display("10,000人读过")
        self.assertEqual(metric["precision"], "exact")
        self.assertEqual(collector.strict_threshold_state(metric, 10000), "at_or_below")
        self.assertEqual(collector.strict_threshold_state(collector.cumulative_display("10001人读过"), 10000), "above")

    def test_different_or_absent_labels_never_become_cumulative(self):
        for raw in ["1.2万人在读", "384.7万飙升值", "117.8万", None, "0.1人读过"]:
            self.assertIsNone(collector.cumulative_display(raw)["value"])

    def test_count_semantics_and_copyright_separation(self):
        # Synthetic local fixture only; never written to any production table.
        book = {"title": "fixture", "reading_count": 301571, "total_reviewers": 8367,
                "score_dcnt_acc": 8261, "comment_cnt": 539,
                "reading_count_text": "117.8万人读过", "word_count": "292970",
                "author": {"name": "fixture"}, "category_tags": [],
                "item_names": ["版权信息"],
                "first_chapters_content": ['<p>出版社：出版社甲</p>\n<p class="cpFooter">本书由版权方乙授权本平台进行数字发行</p>']}
        identity = {"code": 0, "data": {"bookId": "1234567890123456789", "bookName": "fixture"}}
        result = collector.normalize("1234567890123456789", {"err_no": 0, "data": {"book_data": book}}, identity, {}, {})
        self.assertEqual(result["cumulative_readers"], 1178000)
        self.assertEqual(result["reviewer_count"], 8261)
        self.assertEqual(result["book_review_count"], 539)
        self.assertEqual(result["reading_count_raw"], 301571)
        self.assertEqual(result["publisher"], "出版社甲")
        self.assertEqual(result["copyright_provider"], "版权方乙")
        del book["reading_count_text"]
        result = collector.normalize("1234567890123456789", {"err_no": 0, "data": {"book_data": book}}, identity, {}, {})
        self.assertIsNone(result["cumulative_readers"])
        self.assertIn("cumulative_readers", result["missing_fields"])

    def test_wrong_book_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity/title mismatch"):
            collector.normalize("1234567890123456789", {"err_no": 0, "data": {"book_data": {"title": "fixture"}}},
                                {"code": 0, "data": {"bookId": "1234567890123456000", "bookName": "fixture"}}, {}, {})

    def test_missing_copyright_does_not_guess_from_publisher(self):
        result = collector.copyright_metadata({"item_names": ["版权信息"], "first_chapters_content": ["<p>出版社：出版社甲</p>"]})
        self.assertIsNone(result["copyright_provider"])
        self.assertEqual(result["publisher"], "出版社甲")


if __name__ == "__main__":
    unittest.main()
