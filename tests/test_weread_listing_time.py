import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.collect_weread import collect_detail, listing_time


class ListingTimeTests(unittest.TestCase):
    def test_timezone_and_missing_values(self):
        self.assertEqual(listing_time(1793256969), "2026-10-29T14:56:09+08:00")
        for value in (None, 0, -1, True, "1793256969", 1793256969.0, 10**30):
            with self.subTest(value=value):
                self.assertIsNone(listing_time(value))

    def test_detail_keeps_publication_separate_and_archives_listing_source(self):
        info = {"bookId": "123", "publishTime": "2026-10-01 00:00:00",
                "onTime": 1793256969}
        body = "window.__INITIAL_STATE__=" + json.dumps({"reader": {"bookInfo": info}})
        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.collect_weread.get", return_value=(body, {})), \
                patch("scripts.collect_weread.time.sleep"):
            result = collect_detail(("123", "https://weread.qq.com/web/reader/test", Path(directory)))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["platform_publish_time"], "2026-10-01 00:00:00")
            self.assertEqual(result["platform_listing_time"], "2026-10-29T14:56:09+08:00")
            raw = json.loads((Path(directory) / "detail-123.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["bookInfo"]["onTime"], 1793256969)

    def test_missing_listing_time_does_not_fall_back_to_publication(self):
        body = 'window.__INITIAL_STATE__={"reader":{"bookInfo":{"bookId":"123","publishTime":"2026-10-01"}}}'
        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.collect_weread.get", return_value=(body, {})), \
                patch("scripts.collect_weread.time.sleep"):
            result = collect_detail(("123", "https://weread.qq.com/web/reader/test", Path(directory)))
            self.assertIsNone(result["platform_listing_time"])
            self.assertIsNone(result["platform_listing_time_raw"])


if __name__ == "__main__":
    unittest.main()
