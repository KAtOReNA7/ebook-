"""Synthetic ranking fixtures; no external requests or operational state."""
import copy
import json
import unittest
from urllib.parse import urlencode, urlsplit, parse_qs

from scripts import collect_fanqie_rising as rising


def route(algo="197", names="推荐榜,出版飙升榜", types="101,197", cell="1234567890123456789"):
    query = urlencode({"main_algo_name": names, "main_algo_type": types,
                       "algo_type": algo, "cell_id": cell, "category_id": "0",
                       "list_type": "daily", "tab_type": "2", "cell_gender": "2", "genre_type": "0"})
    template = "https://example.invalid/template.js?" + query
    lynx = "sslocal://lynxview?" + urlencode({"surl": template})
    return "dragon1967://lynxview?" + urlencode({"url": lynx})


def fixtures(size=3):
    cell = {"cell_id_str": "1234567890123456789", "cell_url": route()}
    directory = {"code": 0, "data": {"tab_item": [{"cell_data": [copy.deepcopy(cell)]}]}}
    cell["book_data"] = [{"book_id": str(2234567890123456789 + i), "book_name": "合成作品" + str(i),
                          "is_ebook": "1", "genre": "6", "book_type": "0",
                          "recommend_info": json.dumps({"rank": str(i + 1)}),
                          "rank_score": "12.3万飙升值", "sub_info": "5万人读过"} for i in range(size)]
    ranking = {"code": 0, "data": {"cell_view": cell, "has_more": False, "rank_version": ""}}
    return directory, ranking


class FanqieRisingTests(unittest.TestCase):
    def test_nested_official_mapping_not_fixed_cell_id(self):
        directory, _ = fixtures()
        entry = rising.discover(directory)
        self.assertEqual(entry["algo_type"], "197")
        self.assertEqual(entry["cell_id"], "1234567890123456789")
        self.assertEqual(entry["route_params"]["category_id"], "0")

    def test_original_fiction_directory_is_not_publication(self):
        directory, _ = fixtures()
        directory["data"]["tab_item"][0]["cell_data"][0]["cell_url"] = route(names="推荐榜,完本榜", types="101,100")
        with self.assertRaisesRegex(ValueError, "verified publication"):
            rising.discover(directory)

    def test_conflicting_entries_fail(self):
        directory, _ = fixtures()
        directory["data"]["tab_item"][0]["cell_data"].append({
            "cell_id_str": "3234567890123456789", "cell_url": route(cell="3234567890123456789")})
        with self.assertRaises(ValueError):
            rising.discover(directory)

    def test_success_preserves_metric_labels_and_unknown_update_time(self):
        directory, ranking = fixtures()
        result = rising.normalize(ranking, rising.discover(directory), expected_size=3)
        self.assertTrue(result["complete"])
        self.assertEqual(result["books"][0]["rising_value_display"], "12.3万飙升值")
        self.assertNotIn("cumulative_readers", result["books"][0])
        self.assertIsNone(result["source_updated_at"])

    def test_wrong_algorithm_cannot_succeed_with_publication_catalog(self):
        directory, ranking = fixtures()
        ranking["data"]["cell_view"]["cell_url"] = route(algo="101")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            rising.normalize(ranking, rising.discover(directory), expected_size=3)

    def test_more_pages_and_missing_completion_fail(self):
        for value in (True, None, 0):
            directory, ranking = fixtures()
            ranking["data"]["has_more"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                rising.normalize(ranking, rising.discover(directory), expected_size=3)

    def test_short_or_empty_board_is_not_a_complete_board(self):
        for count in (0, 2):
            directory, ranking = fixtures(count)
            with self.subTest(count=count), self.assertRaises(ValueError):
                rising.normalize(ranking, rising.discover(directory), expected_size=3)

    def test_duplicate_numeric_and_unknown_ids_fail(self):
        for value in ("2234567890123456789", 2234567890123456790, "123"):
            directory, ranking = fixtures()
            ranking["data"]["cell_view"]["book_data"][1]["book_id"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                rising.normalize(ranking, rising.discover(directory), expected_size=3)

    def test_wrong_medium_gaps_and_label_changes_fail(self):
        for field, value in (("is_ebook", "0"), ("genre", "0"), ("book_type", "1"),
                             ("rank_score", "12.3万人在读"), ("recommend_info", '{"rank":"3"}')):
            directory, ranking = fixtures()
            ranking["data"]["cell_view"]["book_data"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                rising.normalize(ranking, rising.discover(directory), expected_size=3)

    def test_collect_uses_discovered_id_and_only_two_gets(self):
        directory, ranking = fixtures(100)
        class MockArchive:
            calls = []
            def get(self, url, name):
                self.calls.append((url, name))
                body = directory if len(self.calls) == 1 else ranking
                return json.dumps(body).encode(), {"url": url}
        archive = MockArchive()
        result = rising.collect(archive)
        self.assertEqual(result["count"], 100)
        self.assertEqual(len(archive.calls), 2)
        query = parse_qs(urlsplit(archive.calls[1][0]).query)
        self.assertEqual(query["cell_id"], ["1234567890123456789"])
        self.assertEqual(query["algo_type"], ["197"])
        self.assertNotIn("device_id", query)
        self.assertNotIn("iid", query)


if __name__ == "__main__":
    unittest.main()
