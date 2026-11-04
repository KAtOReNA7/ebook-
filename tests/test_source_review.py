import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from functools import partial
from scripts.sync_marketing import DOC_KEY, plan_import as source_plan_import, reviewed_source_rows

# These tests exercise interpretation of archived mixed-media records, independently
# of the current production admission gate (covered by test_ebook_scope).
plan_import=partial(source_plan_import,scope_policy={'enabled':False})


class SourceReviewTests(unittest.TestCase):
    def fixture(self, root):
        fingerprint = 'b' * 64
        row = {
            'source_content_sha256': fingerprint, 'source_sheet_id': 'wa845a',
            'source_sheet_name': '番茄渠道专用', 'source_row': 91,
            'source_coordinate_key': 'wa845a:row:91', 'source_pages': ['raw.jsonp'],
            'source_document_url': 'https://docs.qq.com/sheet/' + DOC_KEY,
            'cells': {'D': {'value': '某书'}, 'E': {'value': '好评30条'}},
            'ambiguities': [], 'fee_statistics': {'book_cost_eligible': True},
            'normalized': {'platform': 'fanqie', 'platform_raw': '番茄小说',
                           'service_medium': 'unknown', 'title': '某书', 'service': '好评30条',
                           'fee': 180, 'unit_price': 6, 'people': 30,
                           'service_date': '2026-01-09', 'arithmetic_check_passed': True,
                           'archive_links': [], 'fee_basis': '净额'},
        }
        source = root / 'filtered-rows.json'
        source.write_text(json.dumps({'schema_version': 'tencent.marketing-rows.v1', 'rows': [row]}), encoding='utf-8')
        source.with_name('verification.json').write_text(json.dumps({
            'target_rows': 1, 'independent_cell_comparisons': 1, 'fee_basis': '净额'}), encoding='utf-8')
        evidence = root / 'evidence.txt'
        evidence.write_bytes(b'reviewed screenshot fixture')
        override = {'source_document_key': DOC_KEY, 'source_content_sha256': fingerprint,
                    'source_sheet_id': 'wa845a', 'medium': 'audio', 'note': '收听页面，确认有声。',
                    'evidence_path': str(evidence), 'evidence_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}
        config = root / 'review-overrides.json'
        config.write_text(json.dumps({'schema_version': 'source-review-overrides.v1', 'overrides': [override]}), encoding='utf-8')
        return row, source, evidence, config

    def test_audio_override_preserves_source_keys_and_existing_pending_record(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            row, source, _, config = self.fixture(root)
            original_bytes = source.read_bytes()
            baseline = plan_import(source, [], [], import_day='2026-11-03', override_path=root / 'absent.json')
            old_project = {**baseline['projects'][0], 'record_id': 'project-record'}
            old_service = {**baseline['services'][0]['fields'], 'record_id': 'service-record'}
            plan = plan_import(source, [old_project], [old_service], import_day='2026-11-03', override_path=config)
            project, service = plan['projects'][0], plan['services'][0]['fields']
            self.assertTrue(plan['safe_to_apply'])
            self.assertEqual(project['作品唯一键'], old_project['作品唯一键'])
            self.assertEqual(service['服务明细唯一键'], old_service['服务明细唯一键'])
            self.assertEqual(service['源行指纹'], old_service['源行指纹'])
            self.assertEqual(service['原始内容'], old_service['原始内容'])
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(project['作品形式'], '有声书')
            self.assertEqual(service['服务对象'], '有声书')
            self.assertNotIn('平台作品ID', project)
            self.assertEqual(plan['summary']['new_projects'], 0)
            self.assertEqual(plan['summary']['new_services'], 0)
            self.assertEqual({k for k in project if project[k] != old_project.get(k)}, {'作品形式', '证据与备注'})
            self.assertEqual({k for k in service if service[k] != old_service.get(k)}, {'服务对象', '校验说明'})
            replay = plan_import(source, [{**old_project, **project, 'record_id': 'project-record'}],
                                 [{**service, 'record_id': 'service-record'}],
                                 import_day='2026-11-03', override_path=config)
            self.assertEqual(replay['projects'], plan['projects'])
            self.assertEqual(replay['services'], plan['services'])

    def test_only_exact_fingerprint_matches_and_normalized_input_is_not_mutated(self):
        with TemporaryDirectory() as directory:
            row, _, _, config = self.fixture(Path(directory))
            interpreted, applied = reviewed_source_rows([row], config)
            self.assertEqual(interpreted[0]['normalized']['service_medium'], 'audio')
            self.assertEqual(row['normalized']['service_medium'], 'unknown')
            self.assertEqual(len(applied), 1)
            row['source_content_sha256'] = 'c' * 64
            interpreted, applied = reviewed_source_rows([row], config)
            self.assertEqual(interpreted[0]['normalized']['service_medium'], 'unknown')
            self.assertEqual(applied, [])

    def test_changed_evidence_blocks_the_override(self):
        with TemporaryDirectory() as directory:
            row, _, evidence, config = self.fixture(Path(directory))
            evidence.write_bytes(b'changed evidence')
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                reviewed_source_rows([row], config)

    def test_verified_identity_map_cannot_recreate_pending_project_or_reset_d0(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, source, _, _ = self.fixture(root)
            baseline = plan_import(source, [], [], import_day='2026-10-01', override_path=root / 'absent.json')
            old_project = {**baseline['projects'][0], 'record_id': 'project-record'}
            old_service = {**baseline['services'][0]['fields'], 'record_id': 'service-record'}
            identities = {('fanqie', 'unknown', '某书'): {'book_id': '12345',
                          'detail_url': 'https://fanqienovel.com/page/12345', 'title': '某书'}}
            plan = plan_import(source, [old_project], [old_service], identities=identities,
                               import_day='2026-11-03', override_path=root / 'absent.json')
            self.assertFalse(plan['safe_to_apply'])
            self.assertEqual(plan['source_changes'][0]['kind'], 'identity_migration_requires_review')
            self.assertEqual(plan['summary']['new_projects'], 0)
            self.assertEqual(plan['summary']['new_services'], 0)
            self.assertEqual(plan['projects'][0]['作品唯一键'], old_project['作品唯一键'])
            self.assertEqual(plan['services'][0]['project_key'], old_project['作品唯一键'])
            self.assertNotIn('平台作品ID', plan['projects'][0])
            self.assertNotIn('首次进入项目总表', plan['projects'][0])
            self.assertEqual(plan['project_baselines'][old_project['作品唯一键']]['作品唯一键'], old_project['作品唯一键'])
            resulting_project = {**old_project, **plan['projects'][0]}
            self.assertEqual(resulting_project['首次进入项目总表'], '2026-10-01 00:00')
            self.assertEqual(resulting_project['追踪截止日'], '2026-10-30 00:00')
            # After explicit in-place linking, the normal importer accepts the
            # canonical key and still preserves the original registration dates.
            linked = {**old_project, '作品唯一键': 'fanqie:12345', '平台作品ID': '12345',
                      '作品主页': identities[('fanqie', 'unknown', '某书')]['detail_url']}
            linked_service = {**old_service, '作品唯一键': 'fanqie:12345'}
            accepted = plan_import(source, [linked], [linked_service], identities=identities,
                                   import_day='2026-11-03', override_path=root / 'absent.json')
            self.assertTrue(accepted['safe_to_apply'])
            self.assertEqual(accepted['summary']['new_projects'], 0)
            self.assertNotIn('首次进入项目总表', accepted['projects'][0])


if __name__ == '__main__':
    unittest.main()
