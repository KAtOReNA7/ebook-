import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "collect_weread.py"
spec = importlib.util.spec_from_file_location("collector_chart_test", MODULE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
CID = "newbook_novel_female"


def reply(value):
    return json.dumps(value), {"observed_at": "2026-11-03T12:00:00+08:00", "http_status": 200, "response_sha256": "fixture"}


def identity(cid=CID, title="新书·女生小说"):
    return reply({"CategoryId": cid, "title": title, "ranklist": 1, "totalCount": 300})


def page(start, stop):
    return {"books": [{"searchIdx": i, "readingCount": 3,
            "bookInfo": {"bookId": str(1000+i), "title": "book " + str(i)}} for i in range(start, stop+1)], "hasMore": 1}


class ChartIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def collect(self, responses):
        with patch.object(collector, "get", side_effect=responses), patch.object(collector.time, "sleep"):
            return collector.collect_chart(CID, self.path)

    def test_current_female_id_and_exact_top50(self):
        self.assertIn(CID, collector.CHARTS)
        self.assertNotIn("newbook_novel_female_store", collector.CHARTS)
        result = self.collect([identity(), reply(page(1,20)), reply(page(21,40)), reply(page(41,60))])
        self.assertTrue(result["identity_valid"])
        self.assertTrue(result["complete_top50"])
        self.assertEqual([r["rank"] for r in result["records"]], list(range(1,51)))
        self.assertEqual(len(result["requests"]), 4)

    def test_wrong_identity_stops_before_list(self):
        for response in (identity(title="飙升·女生小说"), identity(cid="newbook_novel_female_store"), reply({"errCode": -2040})):
            with self.subTest(response=response):
                result = self.collect([response])
                self.assertFalse(result["identity_valid"])
                self.assertFalse(result["safe_for_membership_diff"])
                self.assertEqual(result["records"], [])

    def test_duplicate_book_id_never_complete(self):
        last = page(41,60)
        last["books"][0]["bookInfo"]["bookId"] = "1001"
        result = self.collect([identity(), reply(page(1,20)), reply(page(21,40)), reply(last)])
        self.assertFalse(result["safe_for_membership_diff"])

    def test_noncontinuous_ranks_never_complete(self):
        last = page(41,60)
        last["books"][0]["searchIdx"] = 42
        result = self.collect([identity(), reply(page(1,20)), reply(page(21,40)), reply(last)])
        self.assertFalse(result["complete_top50"])

    def test_early_end_never_infers_absence(self):
        first = page(1,20)
        first["hasMore"] = 0
        result = self.collect([identity(), reply(first)])
        self.assertEqual(len(result["records"]),20)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["safe_for_membership_diff"])


if __name__ == "__main__":
    unittest.main()
