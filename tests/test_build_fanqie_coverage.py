import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.build_fanqie_coverage import build


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name)/'synthetic-evidence'
        self.image.write_bytes(b'synthetic evidence, not a real image')
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.bid = '1234567890123456789'
        self.projects = [{'书名': '合成作品', '作品唯一键': 'pending:fanqie:ebook:fixture',
                          '作品形式': '电子书', 'record_id': 'fixture-record'}]
        self.candidates = [{'source_title': '合成作品', 'book_id': self.bid,
                            'discovery_sources': ['https://fanqienovel.com/page/'+self.bid]}]
        self.books = [{'book_id': self.bid, 'title': '合成作品', 'author': '合成作者',
                       'identity_status': 'book_id_and_title_verified',
                       'detail_url': 'https://fanqienovel.com/page/'+self.bid,
                       'detail_source': {'observed_at': '2026-11-05T10:00:00+08:00'}}]
        self.reviews = [{'source_title': '合成作品', 'observed_title': '合成作品',
                         'observed_medium': 'ebook', 'review_method': 'visual_inspection',
                         'source_image_path': str(self.image), 'source_image_sha256': digest,
                         'official_cover_path': str(self.image), 'official_cover_sha256': digest,
                         'cover_visual_match': True, 'cover_match_book_id': self.bid}]

    def test_only_reviewed_exact_identity_enters_import_map(self):
        report, identities = build(self.projects, self.candidates, self.books, self.reviews)
        self.assertEqual(report['counts']['confirmed'], 1)
        self.assertEqual(identities['rows'][0]['book_id'], self.bid)
        _, unreviewed = build(self.projects, self.candidates, self.books, [])
        self.assertEqual(unreviewed['rows'], [])

    def test_unobserved_alternative_prevents_silent_choice(self):
        self.candidates.append({**self.candidates[0], 'book_id': '2234567890123456789'})
        report, identities = build(self.projects, self.candidates, self.books, self.reviews)
        self.assertEqual(identities['rows'], [])
        self.assertEqual(report['rows'][0]['reason'], 'other_candidate_observation_missing')

    def test_changed_evidence_rejected(self):
        self.image.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            build(self.projects, self.candidates, self.books, self.reviews)

    def test_existing_canonical_project_requires_merge(self):
        self.projects.append({'书名': '合成作品', '作品唯一键': 'fanqie:'+self.bid,
                              '平台作品ID': self.bid, '作品形式': '电子书', 'record_id': 'canonical'})
        report, identities = build(self.projects, self.candidates, self.books, self.reviews)
        self.assertEqual(report['counts']['requires_merge'], 1)
        self.assertEqual(identities['rows'], [])

    def test_audio_scope_and_short_internal_ids_rejected(self):
        self.projects[0]['作品形式'] = '有声书'
        with self.assertRaises(ValueError):
            build(self.projects, self.candidates, self.books, self.reviews)
        self.projects[0]['作品形式'] = '电子书'
        self.candidates[0]['book_id'] = '12345'
        with self.assertRaises(ValueError):
            build(self.projects, self.candidates, self.books, self.reviews)

    def test_distinctive_screenshot_quote_requires_official_hashed_same_id_evidence(self):
        quote = '合成故事中的主人公前往海边小城寻找失踪多年的朋友途中发现旧信件记载着一段从未听闻的家族往事'
        path = Path(self.temp.name)/'landing.json'
        raw = json.dumps({'data': {'book_data': {'title': '合成作品', 'intro': quote}}}, ensure_ascii=False).encode()
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        receipt = {'url': 'https://api.fqnovel.com/novel_ug/share/landing_page?book_id='+self.bid,
                   'http_status': 200, 'response_sha256': digest}
        path.with_name('landing.json.source.json').write_text(json.dumps(receipt), encoding='utf-8')
        review = {k: v for k, v in self.reviews[0].items() if not k.startswith(('cover_', 'official_cover'))}
        review.update(observed_title='另一展示标题', synopsis_visual_match=True,
                      quote_match_book_id=self.bid, official_text_path=str(path),
                      official_text_sha256=digest, official_text_title='合成作品', source_text_quote=quote)
        report, identities = build(self.projects, self.candidates, self.books, [review])
        self.assertEqual(report['counts']['confirmed'], 1)
        self.assertIn('synopsis', identities['rows'][0]['matching_note'])
        for change in [{'source_text_quote': '太短'}, {'quote_match_book_id': '2234567890123456789'},
                       {'official_text_sha256': 'bad'}, {'source_text_quote': '不同的内容'*15}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                build(self.projects, self.candidates, self.books, [{**review, **change}])
        other = '2234567890123456789'
        _, identities = build(self.projects,
            self.candidates+[{**self.candidates[0], 'book_id': other}],
            self.books+[{**self.books[0], 'book_id': other, 'detail_url': 'https://fanqienovel.com/page/'+other}], [review])
        self.assertEqual(identities['rows'], [])  # Shared synopsis cannot distinguish editions.


if __name__ == '__main__':
    unittest.main()
