import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.prepare_tencent_rows import build, source_layout
from scripts.parse_tencent import column_name


HEADERS = ['发生日期', '服务平台', 'id', '书名', '事务', '单价\n（元/人）',
           '人数\n（人）', '服务费\n（元）', '服务内容留档', '报销批次',
           '备注：该服务费金额是抵扣代缴个税后金额', None, '付款月份', '结算时间']


def fixture(directory, inserted=False, bad_merge=False):
    rows = [HEADERS[:], [46000, '微信读书', 123, '测试书', '电子书开分', 6, 50,
                        300, 'https://example.invalid/archive', None, None, None, None, None],
            [46001, '微信读书', 123, '测试书', '电子书开分', 6, 20,
             120, None, None, None, None, None, None]]
    if inserted:
        for i, row in enumerate(rows):
            row.insert(2, '需求方' if i == 0 else '编辑部')
    for row in rows:
        row.extend([None] * (28 - len(row)))
    cells = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is None:
                continue
            cells.append({'sheet_id': 's', 'row': r, 'col': c,
                          'a1': column_name(c) + str(r + 1), 'value': value,
                          'value_kind': 2 if isinstance(value, int) else 4,
                          'links': [{'url': value, 'origin': 'displayed_url'}]
                          if str(value).startswith('https:') else []})
    col = 7 if bad_merge else 8 + int(inserted)
    sheet = {'sheet_id': 's', 'name': '费用', 'rows': 3, 'columns': 28}
    parsed = {'sheets': [sheet], 'chunks': [{'metadata': {'end_row_index': 2},
              'unknown_value_kinds': [], 'cells': cells, 'merge_ranges': [
                  {'sheet_id': 's', 'start_row': 1, 'end_row': 2, 'start_col': col, 'end_col': col}]}]}
    page = {'requested': {'tab': 's', 'block_start_row': 0, 'block_end_row': 2},
            'parsed_file': 'parsed.json', 'file': 'raw.jsonp', 'sha256': 'a' * 64}
    manifest = {'source': 'https://example.invalid/sheet', 'sheets': [sheet],
                'pages': [page], 'independent_reads': [page]}
    for name, data in [('manifest.json', manifest), ('parsed.json', parsed)]:
        (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return rows


class TencentLayoutTests(unittest.TestCase):
    def test_inserted_requester_preserves_metrics_and_real_archive_coordinates(self):
        fingerprints = []
        for inserted in [False, True]:
            with TemporaryDirectory() as tmp:
                directory = Path(tmp)
                rows = fixture(directory, inserted)
                result = build(directory)
                self.assertEqual(result['target_rows'], 2)
                data = json.loads((directory / 'filtered-rows.json').read_text('utf-8'))['rows']
                for i, row in enumerate(data):
                    normalized = row['normalized']
                    self.assertEqual(normalized['title'], '测试书')
                    self.assertEqual(normalized['service_medium'], 'ebook')
                    self.assertEqual(normalized['people'], [50, 20][i])
                    self.assertEqual(normalized['fee'], [300, 120][i])
                    self.assertEqual(normalized['archive_links'][0]['source_cell'], 'J2' if inserted else 'I2')
                    raw_hash = hashlib.sha256(json.dumps(rows[i + 1], ensure_ascii=False,
                                                         separators=(',', ':')).encode()).hexdigest()
                    self.assertEqual(row['source_content_sha256'], raw_hash)
                self.assertIsNotNone(data[1]['normalized']['archive_inheritance'])
                fingerprints.append(data[0]['source_content_sha256'])
        # A layout edit cannot silently reuse the old row approval/key.
        self.assertNotEqual(*fingerprints)

    def test_non_archive_merge_still_blocks(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture(directory, inserted=True, bad_merge=True)
            with self.assertRaisesRegex(ValueError, 'outside the archive column'):
                build(directory)

    def test_duplicate_or_missing_required_header_blocks(self):
        headers = dict(enumerate(HEADERS))
        headers[20] = '服务费（元）'
        with self.assertRaisesRegex(ValueError, 'ambiguous source header'):
            source_layout(headers)
        headers.pop(20)
        headers[7] = '金额未知'
        with self.assertRaisesRegex(ValueError, 'Missing.*source header'):
            source_layout(headers)


if __name__ == '__main__':
    unittest.main()
