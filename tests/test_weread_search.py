import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "collect_weread.py"
spec = importlib.util.spec_from_file_location("collector_search_test", MODULE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def response(rows, has_more=0):
    return json.dumps({"books": rows, "hasMore": has_more}), {
        "observed_at": "2026-11-03T12:00:00+08:00", "response_sha256": "test-fixture", "http_status": 200}


def row(bid, value, index=1):
    return {"bookInfo": {"bookId": bid, "title": "same title"}, "readingCount": value, "searchIdx": index}


class SearchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.book = {"book_id": "123", "title": "same title"}

    def test_same_title_is_not_identity_and_zero_is_valid(self):
        with patch.object(collector, "get", return_value=response([row("999", 500), row("123", 0, 2)])):
            result = collector.collect_search_today(self.book, self.path)
        self.assertEqual(result["value"], 0)
        self.assertEqual(result["source"]["matched_book_id"], "123")
        self.assertTrue(Path(result["requests"][0]["archive_path"]).is_file())

    def test_wrong_id_never_substitutes_even_same_title(self):
        with patch.object(collector, "get", return_value=response([row("999", 500)])) as get:
            result = collector.collect_search_today(self.book, self.path)
        self.assertIsNone(result["value"])
        self.assertEqual(get.call_count, 1)
        self.assertIn("相同作品ID", result["missing_reason"])

    def test_only_paginates_when_has_more_and_cursor_advances(self):
        with patch.object(collector, "get", side_effect=[response([row("999", 8, 20)], 1), response([row("123", 9, 21)])]) as get, patch.object(collector.time, "sleep"):
            result = collector.collect_search_today(self.book, self.path)
        self.assertEqual(result["value"], 9)
        self.assertIn("maxIdx=20", get.call_args_list[1].args[0])
        self.assertEqual(len(result["requests"]), 2)

    def test_invalid_count_and_duplicate_id_fail_closed(self):
        for rows in ([row("123", True)], [row("123", -1)], [row("123", "8")], [row("123", 5), row("123", 6, 2)]):
            with self.subTest(rows=rows), patch.object(collector, "get", return_value=response(rows)):
                self.assertIsNone(collector.collect_search_today(self.book, self.path)["value"])

    def test_missing_never_uses_reader_stat(self):
        book = {**self.book, "memberships": [], "today_reading_observations": [], "reader_page_reading_count_raw": 999999}
        with patch.object(collector, "get", side_effect=OSError("network unavailable")):
            collector.finalize_today_reading(book, self.path)
        self.assertIsNone(book["today_reading_count"])
        self.assertEqual(book["today_reading_status"], "unavailable_off_chart")
        self.assertIn("network unavailable", book["today_reading_missing_reason"])

    def test_does_not_search_current_chart_or_existing_observation(self):
        books = [
            {**self.book, "memberships": [{"chart_id": "rising", "rank": 1}], "today_reading_observations": []},
            {**self.book, "memberships": [], "today_reading_observations": [{"value": 0, "observed_at": "2026-11-03T10:00:00+08:00"}]},
        ]
        with patch.object(collector, "collect_search_today") as search:
            for book in books:
                collector.finalize_today_reading(book, self.path)
        search.assert_not_called()
        self.assertEqual(books[1]["today_reading_count"], 0)

    def test_no_progress_and_page_limit_are_explicit(self):
        with patch.object(collector, "get", return_value=response([], 1)) as get:
            result = collector.collect_search_today(self.book, self.path)
        self.assertEqual(get.call_count, 1)
        self.assertIn("未前进", result["missing_reason"])
        with patch.object(collector, "get", side_effect=[response([row("999", 1, 20)], 1), response([row("999", 1, 40)], 1)]), patch.object(collector.time, "sleep"):
            result = collector.collect_search_today(self.book, self.path, max_pages=2)
        self.assertIn("分页上限", result["missing_reason"])
        self.assertIsNone(result["value"])


if __name__ == "__main__":
    unittest.main()
