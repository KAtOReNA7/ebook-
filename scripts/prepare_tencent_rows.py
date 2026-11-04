"""Build coordinate-preserving source grid and unmerged target-platform rows."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

try:
    from .parse_tencent import column_name
except ImportError:
    from parse_tencent import column_name

FEE_BASIS = '原表声明：服务费金额为抵扣代缴个税后金额；不推定为含税成本或付款总额。'


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def signature(cell):
    return {'value': cell['value'], 'kind': cell['value_kind'],
            'links': sorted({link['url'] for link in cell['links']}),
            'formula_cached': cell['formula']['cached_value'] if cell.get('formula') else None}


def normalized_platform(raw):
    if raw == '微信读书':
        return 'weread'
    if raw in ('番茄', '番茄小说'):
        return 'fanqie'
    return None


def build(directory):
    manifest = load(directory / 'manifest.json')
    source_cells, coverage, conflicts, source_files = {}, defaultdict(set), [], defaultdict(list)
    merge_index = {}
    coverage_evidence = []
    for page in manifest['pages']:
        parsed = load(directory / page['parsed_file'])
        sid = page['requested']['tab']
        start, end = page['requested']['block_start_row'], page['requested']['block_end_row']
        response_ends = [chunk['metadata'].get('end_row_index') for chunk in parsed['chunks']]
        if not response_ends or max(response_ends) < end:
            raise ValueError('Response block metadata does not cover requested final row')
        declared_sheet = next(sheet for sheet in parsed['sheets'] if sheet['sheet_id'] == sid)
        coverage_evidence.append({'sheet_id': sid, 'requested_rows': [start, end],
                                  'response_block_ends': response_ends, 'source_sha256': page['sha256']})
        overlap = coverage[sid] & set(range(start, end + 1))
        if overlap:
            raise ValueError('Unexpected overlapping primary-page coverage')
        coverage[sid].update(range(start, end + 1))
        for chunk in parsed['chunks']:
            for merged in chunk.get('merge_ranges', []):
                merge_key = tuple(merged[k] for k in ('sheet_id', 'start_row', 'start_col', 'end_row', 'end_col'))
                merge_index[merge_key] = merged
            if chunk['unknown_value_kinds']:
                raise ValueError('Unresolved value types must be inspected before export')
            for cell in chunk['cells']:
                if cell['sheet_id'] != sid:
                    continue
                if not start <= cell['row'] <= end:
                    # Ignore repeated reference rows outside the requested page.
                    continue
                if not 0 <= cell['col'] < declared_sheet['columns']:
                    raise ValueError('Cell outside declared sheet columns')
                key = (sid, cell['row'], cell['col'])
                if key in source_cells and signature(source_cells[key]) != signature(cell):
                    conflicts.append({'sheet_id': sid, 'a1': cell['a1']})
                source_cells[key] = cell
                source_files[key].append(page['file'])
    if conflicts:
        raise ValueError('Conflicting source cells')
    verified_cells, independent_mismatches = 0, []
    for read in manifest.get('independent_reads', []):
        sid = read['requested']['tab']
        start, end = read['requested']['block_start_row'], read['requested']['block_end_row']
        parsed = load(directory / read['parsed_file'])
        for chunk in parsed['chunks']:
            for cell in chunk['cells']:
                if cell['sheet_id'] != sid or not start <= cell['row'] <= end:
                    continue
                key = (sid, cell['row'], cell['col'])
                if key not in source_cells or signature(source_cells[key]) != signature(cell):
                    independent_mismatches.append({'sheet_id': sid, 'a1': cell['a1']})
                else:
                    verified_cells += 1
    archive_merge_lookup = {}
    for merged in merge_index.values():
        if merged['start_col'] != 8 or merged['end_col'] != 8:
            raise ValueError('A new merge outside the archive column requires explicit handling; no blind inheritance')
        for row in range(merged['start_row'], merged['end_row'] + 1):
            key = (merged['sheet_id'], row, 8)
            if key in archive_merge_lookup and archive_merge_lookup[key] != merged:
                raise ValueError('Overlapping archive merge ranges')
            archive_merge_lookup[key] = merged
    grid, filtered, all_business_rows, unresolved, arithmetic = [], [], [], [], []
    for sheet in manifest['sheets']:
        sid = sheet['sheet_id']
        expected = set(range(sheet['rows']))
        if coverage[sid] != expected:
            raise ValueError('Missing or excess rows in source coverage')
        headers = {col: source_cells.get((sid, 0, col), {}).get('value') for col in range(sheet['columns'])}
        grid_rows = []
        for row in range(sheet['rows']):
            cells, rich_cells = [], {}
            for col in range(sheet['columns']):
                key = (sid, row, col)
                cell = source_cells.get(key)
                cells.append(cell['value'] if cell else None)
                rich_cells[column_name(col)] = ({'a1': cell['a1'], 'value': cell['value'],
                                                'value_kind': cell['value_kind'], 'formula': cell.get('formula'),
                                                'links': cell['links'], 'source_present': True}
                                               if cell else {'a1': column_name(col) + str(row + 1), 'value': None,
                                                             'value_kind': None, 'formula': None,
                                                             'links': [], 'source_present': False})
            grid_rows.append({'source_row': row + 1, 'values': cells})
            if row == 0 or not any(value is not None for value in cells):
                continue
            raw_platform = cells[1]
            platform = normalized_platform(raw_platform)
            all_business_rows.append({'source_sheet_id': sid, 'source_row': row + 1, 'platform_raw': raw_platform})
            if platform is None:
                continue
            ambiguities, date_value = [], None
            if isinstance(cells[0], (int, float)):
                try:
                    date_value = (datetime(1899, 12, 30) + timedelta(days=cells[0])).date().isoformat()
                    if not 2000 <= int(date_value[:4]) <= 2100 or cells[0] % 1:
                        ambiguities.append('发生日期数值超出预期范围或含时分，需复核。')
                except (ValueError, OverflowError):
                    ambiguities.append('发生日期无法按Excel日期解释。')
            elif isinstance(cells[0], str):
                match = re.fullmatch(r'\s*(\d{2}|\d{4})[./-](\d{1,2})[./-](\d{1,2})\s*', cells[0])
                if match:
                    year, month, day = map(int, match.groups())
                    year = year + 2000 if year < 100 else year
                    try:
                        date_value = datetime(year, month, day).date().isoformat()
                    except ValueError:
                        ambiguities.append('发生日期文本不是有效日历日期。')
                else:
                    ambiguities.append('发生日期文本格式未识别。')
            else:
                ambiguities.append('发生日期为空；无已确认合并范围，不继承上一行。')
            service = str(cells[4]) if cells[4] is not None else None
            medium = 'audio' if service and '有声' in service else 'ebook' if service and '电子书' in service else 'unknown'
            if medium == 'audio':
                ambiguities.append('有声书服务；不得与电子书点评人数直接对照。')
            if not cells[3]:
                ambiguities.append('书名为空。')
            links = []
            archive_cell = rich_cells['I']
            archive_inheritance = None
            merged = archive_merge_lookup.get((sid, row, 8))
            if merged and row != merged['start_row']:
                anchor = source_cells.get((sid, merged['start_row'], 8))
                if anchor and not archive_cell['value'] and not archive_cell['links']:
                    archive_cell = anchor
                    archive_inheritance = {'anchor_a1': anchor['a1'], 'range': merged,
                                           'reason': 'Explicit source MergeCells operation; only archive evidence inherited.'}
                elif anchor and archive_cell['value'] != anchor['value']:
                    ambiguities.append('留档合并区间非锚点包含不同原值，未覆盖，需核实。')
            for link in archive_cell['links']:
                url = link['url']
                if urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc:
                    ambiguities.append('留档链接格式无法确认。')
                    continue
                if url not in [entry['url'] for entry in links]:
                    links.append({'url': url, 'extraction_origin': link['origin'], 'source_cell': archive_cell['a1'],
                                  'is_attribute_hyperlink': link['origin'] not in ('displayed_url', 'attachment_url'),
                                  'is_attachment': link['origin'] == 'attachment_url'})
            if not links:
                ambiguities.append('留档链接缺失' + ('；原文为“见附件”，源包未给出可用链接。' if cells[8] == '见附件' else '；不从相邻行推断。'))
            numeric = all(isinstance(cells[c], (int, float)) for c in (5, 6, 7))
            equality = None
            if numeric:
                delta = Decimal(str(cells[5])) * Decimal(str(cells[6])) - Decimal(str(cells[7]))
                equality = abs(delta) <= Decimal('0.01')
                arithmetic.append({'source_sheet_id': sid, 'source_row': row + 1,
                                   'unit_price_times_people_equals_fee': equality, 'difference': str(delta)})
                if not equality:
                    ambiguities.append('单价×人数与服务费不一致；可能数量单位/计费口径不同，需逐行核实。')
                if cells[6] < 0 or cells[6] % 1:
                    ambiguities.append('人数不是非负整数。')
            else:
                ambiguities.append('单价、人数、服务费至少一项缺失或非数值。')
            title = str(cells[3]) if cells[3] is not None else ''
            bundle_candidate = bool(re.search(r'\n|[、；;/＋+]|》\s*[和与]\s*《', title))
            if bundle_candidate:
                ambiguities.append('书名可能包含多作品合包；保留原行，未拆分或分摊。')
            identifier = cells[2]
            if isinstance(identifier, float) and identifier.is_integer():
                identifier = str(int(identifier))
            elif identifier is not None:
                identifier = str(identifier)
            normalized = {'service_date': date_value, 'service_date_raw': cells[0],
                          'date_parse_method': 'Excel serial date' if isinstance(cells[0], (int, float)) else 'Y.M.D text; two-digit years interpreted as 2000+year',
                          'platform_raw': raw_platform, 'platform': platform,
                          'title': cells[3], 'source_book_id': identifier, 'service': service,
                          'service_medium': medium, 'unit_price': cells[5], 'people': cells[6], 'fee': cells[7],
                          'fee_basis': FEE_BASIS, 'archive_links': links, 'archive_display_text': cells[8],
                          'archive_effective_display_text': archive_cell['value'], 'archive_inheritance': archive_inheritance,
                          'reimbursement_batch': cells[9], 'notes': cells[10], 'payment_month_raw': cells[12],
                          'settlement_date_raw': cells[13], 'bundle_candidate': bundle_candidate,
                          'arithmetic_check_passed': equality}
            record = {'source_document_url': manifest['source'], 'source_sheet_id': sid,
                      'source_sheet_name': sheet['name'], 'source_row': row + 1,
                      'source_coordinate_key': f'{sid}:row:{row + 1}',
                      'source_pages': sorted({file for col in range(sheet['columns']) for file in source_files[(sid, row, col)]}),
                      'cells': rich_cells, 'normalized': normalized, 'ambiguities': ambiguities,
                      'confirmation_status': '待核实' if (not equality or not date_value or bundle_candidate) else '结构与算式核验通过',
                      'source_content_sha256': hashlib.sha256(json.dumps(cells, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
            filtered.append(record)
            if ambiguities:
                unresolved.append({'source_sheet_id': sid, 'source_row': row + 1, 'reasons': ambiguities})
        grid.append({'sheet_id': sid, 'sheet_name': sheet['name'], 'row_count': sheet['rows'],
                     'column_count': sheet['columns'], 'headers': headers, 'rows': grid_rows,
                     'source_cells': [cell for (ss, rr, cc), cell in source_cells.items() if ss == sid],
                     'blank_semantics': 'null retains source blank/omitted cells; no forward fill was performed',
                     'merge_ranges': [m for m in merge_index.values() if m['sheet_id'] == sid],
                     'merge_status': 'Source MergeCells operations decoded using official schema; raw cells unchanged. Only archive-column inheritance applied in normalized rows.'})
    duplicate_groups = defaultdict(list)
    for row in filtered:
        n = row['normalized']
        key = tuple(n[k] for k in ('service_date', 'platform', 'title', 'service', 'unit_price', 'people', 'fee'))
        duplicate_groups[key].append(row)
    possible_duplicates = []
    for group in duplicate_groups.values():
        if len(group) > 1:
            group_id = 'dup:' + hashlib.sha256('|'.join(sorted(r['source_coordinate_key'] for r in group)).encode()).hexdigest()[:16]
            possible_duplicates.append({'group_id': group_id, 'cross_sheet': len({r['source_sheet_id'] for r in group}) > 1,
                                        'rows': [r['source_coordinate_key'] for r in group],
                                        'classification': 'exact business-field match only; not automatically deduplicated'})
            for row in group:
                row.setdefault('duplicate_candidate_groups', []).append(group_id)
                row['ambiguities'].append('业务字段完全相同的重复候选；尚未判断为同一笔真实支出。')
    for row in filtered:
        n = row['normalized']
        reasons = []
        if not isinstance(n['fee'], (int, float)):
            reasons.append('服务费非数值或缺失')
        if n['bundle_candidate']:
            reasons.append('疑似多书合包未明确分摊')
        if row.get('duplicate_candidate_groups'):
            reasons.append('重复候选尚未核实')
        # An inconsistent quantity equation does not erase the explicitly stated
        # raw expense. Keep it eligible only for the source-row total, not for
        # confirmed per-book cost or cost-per-review analyses.
        row['fee_statistics'] = {'raw_row_fee_available': isinstance(n['fee'], (int, float)),
                                 'raw_row_fee': n['fee'], 'book_cost_eligible': not reasons and n['arithmetic_check_passed'] is True,
                                 'confirmed_analysis_eligible': not reasons and n['arithmetic_check_passed'] is True,
                                 'exclusion_reasons': reasons,
                                 'allocation': 'not_performed', 'deduplication': 'not_performed'}
    verification = {'status': 'source_collected_with_business_ambiguities', 'sheet_inventory': manifest['sheets'],
                    'coverage': coverage_evidence, 'covered_rows': {sid: len(rows) for sid, rows in coverage.items()},
                    'full_grid_cells': sum(s['rows'] * s['columns'] for s in manifest['sheets']),
                    'explicit_cell_messages': len(source_cells), 'coordinate_conflicts': conflicts,
                    'independent_read_requests': len(manifest.get('independent_reads', [])),
                    'independent_cell_comparisons': verified_cells, 'independent_mismatches': independent_mismatches,
                    'target_rows': len(filtered), 'target_rows_by_platform': dict(Counter(r['normalized']['platform'] for r in filtered)),
                    'target_rows_by_sheet': dict(Counter(r['source_sheet_id'] for r in filtered)),
                    'target_rows_by_service_medium': dict(Counter(r['normalized']['service_medium'] for r in filtered)),
                    'unit_price_people_fee_checks': len(arithmetic), 'arithmetic_pass': sum(r['unit_price_times_people_equals_fee'] for r in arithmetic),
                    'arithmetic_failures': [row for row in arithmetic if not row['unit_price_times_people_equals_fee']],
                    'archive_links_present_rows': sum(bool(r['normalized']['archive_links']) for r in filtered),
                    'missing_archive_rows': [r['source_coordinate_key'] for r in filtered if not r['normalized']['archive_links']],
                    'possible_duplicate_groups': possible_duplicates,
                    'merge_range_count': len(merge_index),
                    'archive_merge_inherited_rows': sum(bool(r['normalized']['archive_inheritance']) for r in filtered),
                    'bundle_candidates': [r['source_coordinate_key'] for r in filtered if r['normalized']['bundle_candidate']],
                    'unresolved_rows': unresolved, 'fee_basis': FEE_BASIS,
                    'numeric_mapping_validation': 'Wire numeric indexes 0..128 resolve literal small integers; higher indexes resolve pool[index-129]. Checked with independent source requests and price×people identities; no rendered-cell verification yet.',
                    'formula_preservation': 'Original token bytes and source cached values retained; source formulas were not recalculated.',
                    'archive_link_validation': 'URL targets and image-attachment URLs extracted from actual attributes; direct URLs in text labeled separately. Explicit merge anchors provide inherited links. Linked targets were not fetched.',
                    'merge_validation': 'Official frontend schema proves MergeCells=5 and GridRangeData coordinates. All observed merges are archive-column-only; raw blanks preserved, normalized links inherit only their explicit anchor.',
                    'filter_rule': 'Only exact 服务平台 values 微信读书/番茄/番茄小说; all service types retained, including audio.'}
    if independent_mismatches:
        verification['status'] = 'blocked_by_source_mismatch'
    save(directory / 'raw-grid.json', {'schema_version': 'tencent.raw-grid.v1', 'source': manifest['source'], 'sheets': grid})
    save(directory / 'filtered-rows.json', {'schema_version': 'tencent.marketing-rows.v1', 'source': manifest['source'],
                                           'rows': filtered, 'no_deduplication_or_allocation': True})
    save(directory / 'verification.json', verification)
    return {key: verification[key] for key in ('status', 'covered_rows', 'target_rows', 'target_rows_by_platform',
                                               'target_rows_by_sheet', 'target_rows_by_service_medium',
                                               'arithmetic_pass', 'archive_links_present_rows',
                                               'independent_cell_comparisons', 'independent_mismatches')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source.resolve()), ensure_ascii=False, indent=2))
