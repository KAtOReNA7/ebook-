import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from resolve_fanqie_projects import choose, title_key


def candidate(bid, title="作品", author="甲", copyright=None):
    return {"book_id": bid, "title": title, "author": author, "copyright_provider": copyright,
            "identity_status": "book_id_and_title_verified"}


class IdentityTests(unittest.TestCase):
    def test_full_title_retains_version(self):
        self.assertNotEqual(title_key("《作品》（全三册）"), title_key("作品"))
        self.assertEqual(title_key("《作品》，第一卷！"), title_key("作品,第一卷"))

    def test_title_only_is_not_confirmed(self):
        self.assertEqual(choose("作品", [candidate("1234567890123456789")])["status"], "unresolved")

    def test_unique_title_with_verified_copyright(self):
        result = choose("作品", [candidate("1234567890123456789", copyright="磨铁数盟")])
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["source_author_unavailable"])

    def test_same_title_multiple_versions_stay_ambiguous(self):
        rows = [candidate("1234567890123456789", copyright="磨铁数盟"),
                candidate("2234567890123456789", author="乙", copyright="磨铁数盟")]
        self.assertEqual(choose("作品", rows)["status"], "ambiguous")

    def test_source_author_rejects_wrong_version(self):
        review = {"review_method": "visual_inspection", "source_image_sha256": "fixture", "observed_author": "甲", "observed_title": "作品"}
        result = choose("作品", [candidate("1234567890123456789", author="乙", copyright="磨铁数盟")], review)
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("source_author_mismatch", result["candidates"][0]["rejection_reasons"])

    def test_same_cover_needs_full_visible_title(self):
        review = {"review_method": "visual_inspection", "source_image_sha256": "fixture",
                  "official_cover_sha256": "fixture", "cover_visual_match": True,
                  "cover_match_book_id": "1234567890123456789", "observed_title": "另一作品"}
        self.assertEqual(choose("作品", [candidate("1234567890123456789")], review)["status"], "unresolved")
        review["observed_title"] = "作品"
        result = choose("作品", [candidate("1234567890123456789")], review)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["source_author_unavailable"])

    def test_audio_source_cannot_bind_ebook(self):
        review = {"review_method": "visual_inspection", "source_image_sha256": "fixture",
                  "observed_author": "甲", "observed_title": "作品", "observed_medium": "audio"}
        result = choose("作品", [candidate("1234567890123456789", copyright="磨铁数盟")], review)
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("source_audio_edition_not_verified_by_ebook_collector", result["candidates"][0]["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
