import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
from scripts import tencent_daily
from decimal import Decimal
from scripts.parse_tencent import parse_cells
from scripts.sync_marketing import amount, normalize_title, project_identity, plan_import, DOC_KEY
from scripts.tencent_daily import new_entry_candidates


def integer(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value, wire=None):
    wire = (0 if isinstance(value, int) else 2) if wire is None else wire
    payload = integer(value) if wire == 0 else integer(len(value)) + value if wire == 2 else value
    return integer((number << 3) | wire) + payload


def source_packet(cell_kind, value_index, pool, extra=b'', merge=False):
    bounds = field(1, b'BB08J2') + field(4, 3) + field(5, 8)
    cell_value = field(1, cell_kind) + field(2, field(1, value_index)) + extra
    cell = field(1, 1) + field(2, 7) + field(3, cell_value)
    payload = field(3, bounds) + field(5, pool) + field(6, cell)
    ops = field(5, field(1, 18) + field(19, payload))
    if merge:
        merge_bounds = field(1, b'BB08J2') + field(2, 1) + field(3, 8) + field(4, 3) + field(5, 8)
        ops += field(5, field(1, 5) + field(6, field(3, merge_bounds)))
    return field(1, field(1, 1) + ops)


class TencentParserTests(unittest.TestCase):
    def test_numeric_pool_and_explicit_merge(self):
        pool = field(3, field(1, struct.pack('<d', 250.0), wire=1))
        parsed = parse_cells(source_packet(2, 129, pool, merge=True))
        self.assertEqual(parsed['cells'][0]['value'], 250)
        self.assertEqual(parsed['cells'][0]['a1'], 'H2')
        merged = parsed['merge_ranges'][0]
        self.assertEqual((merged['start_row'], merged['end_row'], merged['start_col'], merged['end_col']), (1, 3, 8, 8))
        self.assertEqual(parse_cells(source_packet(2, 25, b''))['cells'][0]['value'], 25)

    def test_formula_cached_result_is_retained(self):
        numbers = field(3, field(1, struct.pack('<d', 62.7), wire=1))
        cached = field(1, 2) + field(3, field(1, 129))
        formula = field(4, field(1, cached) + field(3, field(1, 0)))
        definition = field(5, field(1, b'formula-token-evidence'))
        cell = parse_cells(source_packet(5, 0, numbers + formula + definition))['cells'][0]
        self.assertEqual(cell['value'], 62.7)
        self.assertIsNotNone(cell['formula']['raw_formula_base64'])

    def test_actual_attachment_url_not_just_display_label(self):
        pool = field(1, field(1, '见附件'.encode()))
        attachment = field(17, field(1, json.dumps(['https://example.invalid/evidence.png']).encode()))
        cell = parse_cells(source_packet(4, 0, pool, extra=attachment))['cells'][0]
        self.assertEqual(cell['value'], '见附件')
        self.assertEqual(cell['links'][0]['url'], 'https://example.invalid/evidence.png')
        self.assertEqual(cell['links'][0]['origin'], 'attachment_url')

    def test_internal_source_id_never_becomes_platform_id(self):
        raw = {'platform': 'fanqie', 'service_medium': 'unknown', 'title': '《某书》', 'source_book_id': '244036'}
        key, verified = project_identity(raw, {})
        self.assertTrue(key.startswith('pending:fanqie:unknown:'))
        self.assertIsNone(verified)
        self.assertEqual(normalize_title('《 某书 》'), '某书')

    def test_financial_totals_use_integer_cents(self):
        self.assertEqual(amount(62.699999999999996), Decimal('62.70'))
        self.assertEqual(amount(95.69999999999999), Decimal('95.70'))

    def test_deleting_one_identical_source_occurrence_requires_review(self):
        fingerprint = 'a' * 64
        key = 'tencent:' + DOC_KEY + ':BB08J2:' + fingerprint
        normalized = {'platform': 'weread', 'platform_raw': '微信读书', 'service_medium': 'ebook',
                      'title': '某书', 'service': '电子书开分', 'fee': 180.0, 'unit_price': 6.0,
                      'people': 30.0, 'service_date': '2026-01-09', 'source_book_id': None,
                      'arithmetic_check_passed': True, 'archive_links': [], 'fee_basis': '净额'}
        row = {'normalized': normalized, 'source_coordinate_key': 'BB08J2:row:3',
               'source_content_sha256': fingerprint, 'source_sheet_id': 'BB08J2', 'source_sheet_name': '工作表1',
               'source_row': 3, 'source_pages': ['source.jsonp'], 'source_document_url': 'https://docs.qq.com/sheet/example',
               'cells': {'A': {'value': 46031.0}}, 'fee_statistics': {'book_cost_eligible': True}, 'ambiguities': []}
        previous = [{'服务明细唯一键': key + ':' + str(i) + ':service:1',
                     '原始行唯一键': key + ':' + str(i), '源行指纹': fingerprint} for i in (1, 2)]
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'filtered-rows.json'
            source.write_text(json.dumps({'schema_version': 'tencent.marketing-rows.v1', 'rows': [row]}), encoding='utf-8')
            source.with_name('verification.json').write_text(json.dumps({'target_rows': 1,
                'independent_cell_comparisons': 1, 'fee_basis': '净额'}), encoding='utf-8')
            plan = plan_import(source, [], previous, import_day='2026-11-03')
        self.assertFalse(plan['safe_to_apply'])
        missing = [entry['old_service_key'] for entry in plan['source_changes']]
        self.assertEqual(missing, [key + ':2:service:1'])


class TencentDailyTests(unittest.TestCase):
    def test_dry_run_uses_latest_reviewed_source_instead_of_initial_frozen_source(self):
        with TemporaryDirectory() as directory, patch.object(tencent_daily,'ROOT',Path(directory)):
            day='2026-11-04';run=Path(directory)/'runs'/day;run.mkdir(parents=True)
            reviewed=Path(directory)/'reviewed-source'
            (run/'tencent-state.json').write_text(json.dumps({'phase':'finished','summary':{'source_dir':str(reviewed)}}),encoding='utf-8')
            args=SimpleNamespace(source_dir=None,dry_run=True,shadow=True,identity_map=[],run_kind='local')
            with patch.object(tencent_daily,'shanghai_day',return_value=day), \
                 patch.object(tencent_daily,'frozen_source',side_effect=LookupError('reached reviewed source')) as read:
                with self.assertRaisesRegex(LookupError,'reached reviewed source'):tencent_daily.run(args)
                read.assert_called_once_with(reviewed.resolve())

    def test_source_review_is_isolated_from_unknown_mutations(self):
        with TemporaryDirectory() as directory, patch.object(tencent_daily, 'ROOT', Path(directory)):
            day = '2026-11-03'
            run_dir = Path(directory) / 'runs' / day
            run_dir.mkdir(parents=True)
            path = run_dir / 'tencent-state.json'
            path.write_text(json.dumps({'phase': 'source_review_required'}), encoding='utf-8')
            args = SimpleNamespace(source_dir=None, dry_run=True, shadow=True, identity_map=[], run_kind='local')
            # Reaching source reading proves that a business-only review does not
            # trip the earlier hard-stop used for uncertain external mutations.
            with patch.object(tencent_daily, 'shanghai_day', return_value=day), \
                 patch.object(tencent_daily, 'frozen_source', side_effect=LookupError('source-stage-reached')):
                with self.assertRaisesRegex(LookupError, 'source-stage-reached'):
                    tencent_daily.run(args)
            path.write_text(json.dumps({'phase': 'requires_review'}), encoding='utf-8')
            with patch.object(tencent_daily, 'shanghai_day', return_value=day):
                with self.assertRaisesRegex(RuntimeError, 'Unresolved Tencent mutation'):
                    tencent_daily.run(args)

    def plan(self):
        return {'safe_to_apply': True, 'import_day': '2026-11-03', 'services': [
            {'project_key': 'pending:weread:ebook:a', 'source_coordinate_key': 'BB08J2:row:3',
             'fields': {'服务明细唯一键': 'new-service', '源行指纹': 'fingerprint', '原始书名': '某书',
                        '服务平台': '微信读书', '服务对象': '电子书', '服务内容': '电子书开分',
                        '原始行总金额': 180, '计入统计': ['是'], '原始文档链接': 'https://docs.qq.com/sheet/source'}}]}

    def previous(self):
        return [{'服务明细唯一键': 'old-service', '原始行唯一键': 'tencent:' + DOC_KEY + ':old'}]

    def test_historical_baseline_never_emits(self):
        events, baseline = new_entry_candidates(self.plan(), [], [])
        self.assertTrue(baseline)
        self.assertFalse(events)

    def test_new_service_and_rank_project_promotion(self):
        events, baseline = new_entry_candidates(self.plan(), self.previous(),
                                               [{'作品唯一键': 'pending:weread:ebook:a', '监测范围': ['仅榜单样本']}])
        self.assertFalse(baseline)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['transition'], '榜单样本转为营销项目')
        replay, _ = new_entry_candidates(self.plan(), self.previous() + [{'服务明细唯一键': 'new-service'}], [])
        self.assertFalse(replay)
        unsafe = self.plan()
        unsafe['safe_to_apply'] = False
        self.assertFalse(new_entry_candidates(unsafe, self.previous(), [])[0])


if __name__ == '__main__':
    unittest.main()
