from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import merge_work_projects as merge
from scripts.sync_marketing import (DOC_KEY, project_identity, plan_import,
                                    identities_from_file)
from scripts.tencent_daily import new_entry_candidates


def fixtures():
    projects, services, identities = [], [], {}
    for i, medium in enumerate(('audio', 'ebook')):
        rid, sid, key = 'p' + str(i), 's' + str(i), 'pending:weread:' + medium + ':source'
        projects.append({'record_id': rid, '作品唯一键': key, '书名': '原书名',
            '平台': ['微信读书'], '监测范围': ['营销项目'], '作品形式': '有声书' if i == 0 else '电子书',
            '首次进入项目总表': '2026-10-01 00:00' if i == 0 else '2026-11-03 00:00',
            '追踪截止日': '2026-10-30 00:00' if i == 0 else '2026-12-02 00:00',
            '营销服务明细': [{'id': sid}], '每日记录': [{'id': 'old-audio-observation'}] if i == 0 else [],
            '上榜记录': [], '通知记录': [], '证据与备注': 'original'})
        services.append({'record_id': sid, '服务明细唯一键': 'service-' + sid,
            '作品唯一键': key, '关联项目': [{'id': rid}], '原始书名': '原书名',
            '服务对象': '有声书' if i == 0 else '电子书', '原始行总金额': 50 + i,
            '已确认分摊金额': 50 + i, '邀评人数': 10, '服务日期': '2026-01-01 00:00',
            '原始内容': 'original-' + sid, '留档链接': 'https://docs.qq.com/sheet/example'})
        identities[('weread', medium, '原书名')] = {'verified': True, 'platform': 'weread',
            'medium': medium, 'tracking_medium': 'ebook', 'source_title': '原书名', 'book_id': '123456',
            'title': '官方作品', 'author': '作者', 'detail_url': 'https://weread.qq.com/web/reader/verified'}
    return projects, services, identities


def apply_in_memory(plan, projects, services):
    ps, ss = deepcopy(projects), deepcopy(services)
    for s in ss:
        s.update(plan['service_updates'].get(s['record_id'], {}))
    for p in ps:
        p.update(plan['project_updates'].get(p['record_id'], {}))
        p['营销服务明细'] = [{'id': s['record_id']} for s in ss if merge.prior.links(s['关联项目']) == [p['record_id']]]
    return ps, ss


class WorkProjectModelTests(unittest.TestCase):
    def test_explicit_audio_service_tracks_ebook_but_legacy_audio_id_is_not_guessed(self):
        _, _, ids = fixtures()
        n = {'platform': 'weread', 'service_medium': 'audio', 'title': '原书名'}
        self.assertEqual(project_identity(n, ids)[0], 'weread:123456')
        ids[('weread', 'audio', '原书名')].pop('tracking_medium')
        self.assertEqual(project_identity(n, ids)[0], 'weread:audio:123456')
        self.assertTrue(project_identity(n, {})[0].startswith('pending:weread:audio:'))

    def test_conflicting_same_numeric_id_in_two_tracking_namespaces_is_rejected(self):
        with TemporaryDirectory() as tmp:
            rows = [deepcopy(fixtures()[2][('weread', 'audio', '原书名')]) for _ in range(2)]
            rows[1]['tracking_medium'] = 'audio'
            p = Path(tmp) / 'identities.json'
            p.write_text(json.dumps(rows), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Conflicting'):
                identities_from_file([p])

    def test_merge_keeps_earliest_record_and_all_source_fields_and_history(self):
        p, s, ids = fixtures()
        plan = merge.build_plan(p, s, ids)
        self.assertEqual(len(plan['groups']), 1)
        group = plan['groups'][0]
        self.assertEqual(group['canonical_record_id'], 'p0')
        self.assertEqual(group['service_media'], ['有声书', '电子书'])
        self.assertEqual(plan['project_updates']['p0']['作品形式'], '电子书')
        self.assertEqual(group['first_registered'], '2026-10-01 00:00')
        self.assertEqual(group['tracking_deadline'], '2026-12-02 00:00')
        self.assertNotIn('作品唯一键', plan['project_updates']['p1'])
        after_p, after_s = apply_in_memory(plan, p, s)
        merge.readback(plan, after_p, after_s)
        self.assertEqual(after_p[0]['每日记录'], p[0]['每日记录'])
        self.assertEqual([x['服务对象'] for x in after_s], ['有声书', '电子书'])
        self.assertEqual(merge.build_plan(after_p, after_s, ids)['groups'], [])
        after_s[0]['服务对象'] = '电子书'
        with self.assertRaisesRegex(RuntimeError, 'form/content conservation'):
            merge.readback(plan, after_p, after_s)

    def test_existing_canonical_record_wins_and_same_title_different_ids_do_not_merge(self):
        p, s, ids = fixtures()
        p.append({'record_id': 'existing', '作品唯一键': 'weread:123456', '平台作品ID': '123456',
                  '书名': '官方作品', '追踪截止日': '2026-12-09 00:00', '营销服务明细': [],
                  '首次上榜采集日': '2026-11-01 00:00', '每日记录': [{'id': 'existing-history'}]})
        plan = merge.build_plan(p, s, ids)
        self.assertEqual(plan['groups'][0]['canonical_record_id'], 'existing')
        self.assertEqual(len(plan['groups'][0]['aliases']), 2)
        merge.readback(plan, *apply_in_memory(plan, p, s))
        ids[('weread', 'audio', '原书名')]['book_id'] = '987654'
        separate = merge.build_plan(p, s, ids)
        self.assertEqual(len(separate['groups']), 2)
        self.assertEqual({g['service_count'] for g in separate['groups']}, {1})

    def test_stale_or_timed_out_merge_keeps_unknown_guard_without_retry(self):
        p, s, ids = fixtures()
        approved = {'plan': merge.build_plan(p, s, ids)}
        with TemporaryDirectory() as tmp:
            class FakeBase:
                receipt_dir = Path(tmp) / 'receipts' / 'day' / 'attempt'
                manifest = {'tables': {'项目总表': {'field_readback': {'fields': [
                    {'name': '监测范围', 'options': [{'name': '别名留档'}]}]}}}}
                calls = []
                def read(self, table):
                    return deepcopy(p if table == '项目总表' else s)
                def call(self, command, table, payload):
                    self.calls.append((command, table))
                    raise TimeoutError('unknown write')
            api = FakeBase()
            s[0]['服务对象'] = 'changed'
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                merge.apply_plan(api, approved, ids)
            self.assertEqual(api.calls, [])
            s[0]['服务对象'] = '有声书'
            with self.assertRaises(TimeoutError):
                merge.apply_plan(api, approved, ids)
            self.assertEqual(len(api.calls), 1)
            with self.assertRaisesRegex(RuntimeError, 'Unknown/incomplete'):
                merge.prior.assert_no_unknown(Path(tmp))

    def test_large_merge_batches_at_200_and_finishes_only_after_full_readback(self):
        p, s, ids = fixtures()
        for i in range(2, 235):
            row = deepcopy(s[0])
            row.update(record_id='s' + str(i), 服务明细唯一键='service-s' + str(i))
            s.append(row)
            p[0]['营销服务明细'].append({'id': row['record_id']})
        approved = {'plan': merge.build_plan(p, s, ids)}
        with TemporaryDirectory() as tmp:
            class FakeBase:
                receipt_dir = Path(tmp)
                manifest = {'tables': {'项目总表': {'field_readback': {'fields': [
                    {'name': '监测范围', 'options': [{'name': '别名留档'}]}]}}}}
                calls = []
                reads = 0
                def read(self, table):
                    self.reads += 1
                    rows = (p, s) if self.reads <= 2 else apply_in_memory(approved['plan'], p, s)
                    return deepcopy(rows[0 if table == '项目总表' else 1])
                def call(self, command, table, payload):
                    self.calls.append((table, len(payload['update_records'])))
                    state = json.loads((self.receipt_dir / 'link-state.json').read_text(encoding='utf-8'))
                    if state['phase'] == 'finished':
                        raise AssertionError('Guard cleared before full readback')
            api = FakeBase()
            merge.apply_plan(api, approved, ids)
            self.assertEqual(api.calls, [('营销服务明细', 200), ('营销服务明细', 35), ('项目总表', 2)])
            self.assertEqual(api.reads, 4)
            self.assertEqual(json.loads((Path(tmp) / 'link-state.json').read_text(encoding='utf-8'))['phase'], 'finished')

    def test_daily_import_combines_verified_work_preserves_service_forms_and_no_new_event(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for i, medium in enumerate(('audio', 'ebook')):
                rows.append({'source_content_sha256': str(i + 1) * 64, 'source_sheet_id': 'tab',
                    'source_sheet_name': '原表', 'source_row': i + 1, 'source_coordinate_key': 'tab:row:' + str(i + 1),
                    'source_pages': [], 'source_document_url': 'https://docs.qq.com/sheet/' + DOC_KEY,
                    'cells': {}, 'ambiguities': [], 'fee_statistics': {'book_cost_eligible': True},
                    'normalized': {'platform': 'weread', 'platform_raw': '微信读书', 'service_medium': medium,
                        'title': '原书名', 'service': '邀评', 'fee': 50 + i, 'unit_price': 50 + i, 'people': 1,
                        'service_date': '2026-01-01', 'arithmetic_check_passed': True, 'archive_links': [], 'fee_basis': '净额'}})
            source = root / 'filtered-rows.json'
            source.write_text(json.dumps({'schema_version': 'tencent.marketing-rows.v1', 'rows': rows}), encoding='utf-8')
            source.with_name('verification.json').write_text(json.dumps({'target_rows': 2, 'independent_cell_comparisons': 1, 'fee_basis': '净额'}), encoding='utf-8')
            ids = fixtures()[2]
            # Legacy ebook mappings need no explicit tracking_medium. The
            # final row's mapping must not inherit the first audio service form.
            ids[('weread', 'ebook', '原书名')].pop('tracking_medium')
            plan = plan_import(source, [], [], ids, '2026-11-03', root / 'none.json')
            self.assertEqual(len(plan['projects']), 1)
            self.assertEqual(plan['projects'][0]['作品唯一键'], 'weread:123456')
            self.assertEqual(plan['projects'][0]['作品形式'], '电子书')
            self.assertEqual([s['fields']['服务对象'] for s in plan['services']], ['电子书'])
            self.assertEqual(plan['summary']['excluded_by_reason'], {'confirmed_audio':1})
            p = [{**plan['projects'][0], 'record_id': 'canonical'}]
            s = [{**r['fields'], 'record_id': 's' + str(i)} for i, r in enumerate(plan['services'])]
            # Normal daily imports learn already-verified relationships from the
            # existing service links even without the one-off identity file.
            replay = plan_import(source, p, s, {}, '2026-11-04', root / 'none.json')
            self.assertTrue(replay['safe_to_apply'])
            self.assertEqual(replay['summary']['new_projects'], 0)
            self.assertEqual(new_entry_candidates(replay, s, p)[0], [])
            self.assertNotIn('首次进入项目总表', replay['projects'][0])
            self.assertEqual(replay['projects'][0]['作品形式'], '电子书')
            self.assertEqual([x['fields']['服务对象'] for x in replay['services']], ['电子书'])


if __name__ == '__main__':
    unittest.main()
