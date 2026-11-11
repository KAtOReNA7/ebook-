import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from scripts.work_category import category_fields
from scripts.presentation import to_remote


class CategoryReferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        evidence = json.dumps({'bookInfo': {'category': '精品小说-女性小说'}},ensure_ascii=False).encode()
        (self.root/'evidence.json').write_bytes(evidence)
        self.book = {'book_id':'123', 'author':'作者', 'category':'精品小说'}
        self.references = {'fanqie:123': {'platform_key':'fanqie:123','verified':True,'author':'作者',
            'first':'精品小说','second':'女性小说','reference_category':'精品小说-女性小说',
            'native_single_categories':['精品小说'], 'source_platform':'微信读书',
            'source_url':'https://weread.qq.com/web/reader/fixture','evidence_path':'evidence.json',
            'evidence_sha256':hashlib.sha256(evidence).hexdigest()}}

    def test_single_native_tag_retains_reviewed_pair_and_native_raw(self):
        fields,basis=category_fields(self.book,self.references,self.root)
        remote=to_remote('项目总表',fields,{'版权方':{},'一级分类':{},'二级分类':{}})
        self.assertEqual(remote['二级分类'],['女性小说'])
        self.assertEqual(remote['分类原文'],'精品小说')
        self.assertEqual(basis['kind'],'reviewed_work_reference')
        self.assertEqual(category_fields(self.book,self.references,self.root),(fields,basis))

    def test_two_native_tags_take_precedence(self):
        fields,basis=category_fields({**self.book,'category':'精品小说/都市小说'},self.references,self.root)
        self.assertEqual(fields,{'作品分类':'精品小说/都市小说'})
        self.assertEqual(basis['kind'],'native')

    def test_no_guessed_reference_for_other_identity_author_or_tag(self):
        for changes in [{'book_id':'456'},{'author':'其他作者'},{'category':'科幻小说'}]:
            book={**self.book,**changes}
            fields,basis=category_fields(book,self.references,self.root)
            self.assertEqual(fields,{'作品分类':book['category']})
            self.assertEqual(basis['kind'],'native')

    def test_corrupt_evidence_blocks_fallback(self):
        (self.root/'evidence.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'evidence changed'):
            category_fields(self.book,self.references,self.root)

    def test_missing_category_does_not_clear_existing_labels(self):
        fields,basis=category_fields({**self.book,'category':None},self.references,self.root)
        self.assertEqual(fields,{})
        self.assertEqual(basis['kind'],'unavailable')
