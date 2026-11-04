"""Reviewed, idempotent evidence-note append for pending WeRead marketing works.

Only 证据与备注 can change. All other project fields, IDs, amounts, identities
and scopes must survive a complete readback unchanged. Default is read-only.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path

try:
    from .feishu_io import Base, ROOT, same, save
    from .sync_marketing import normalize_title, unique_index
    from .collect_weread import detail_url
    from .merge_project_aliases import assert_no_unknown, digest
except ImportError:
    from feishu_io import Base, ROOT, same, save
    from sync_marketing import normalize_title, unique_index
    from collect_weread import detail_url
    from merge_project_aliases import assert_no_unknown, digest

BASE = ROOT / 'research/weread-work-identities'
INPUTS = ('identity-map.json', 'remaining-unresolved-after-supplement.json',
          'unavailable-ebook-identities.json', 'production-identity-map.json',
          'source-evidence-supplement/identity-map.json', 'existing-image-supplement/identity-map.json')
TZ = timezone(timedelta(hours=8))


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def evidence_files(base=BASE):
    return [{'path': str((base / name).resolve()), 'sha256': hashlib.sha256((base / name).read_bytes()).hexdigest()}
            for name in INPUTS]


def candidates(row):
    found = {str(r['book_id']): {k: r.get(k) for k in ('book_id', 'title', 'author', 'detail_url')}
             for r in row.get('candidates', [])}
    fmt = row.get('formatting_attempt', {})
    for candidate in fmt.get('candidates', fmt.get('discovery_candidates', [])):
        bid = str(candidate['bookId'])
        found.setdefault(bid, {'book_id': bid, 'title': candidate['title'], 'author': candidate['author'], 'detail_url': None})
    missing = {key for key, value in found.items() if not value['detail_url']}
    for request in row.get('requests', []) + fmt.get('requests', []) + fmt.get('discovery_requests', []):
        if not missing:
            break
        data = load(request['archive_path'])
        for value in data.get('books', []):
            info = value.get('bookInfo', {})
            bid = str(info.get('bookId'))
            if bid in missing and detail_url(info):
                found[bid]['detail_url'] = detail_url(info)
                missing.remove(bid)
    return sorted(found.values(), key=lambda r: (r['title'], r['author'], r['book_id']))


def entries_from_evidence(base=BASE):
    full = load(base / 'identity-map.json')['rows']
    by_title = {r['source_title']: r for r in full}
    verified = set()
    for name in INPUTS[3:]:
        verified.update(normalize_title(r['source_title']) for r in load(base / name)['rows'] if r.get('verified'))
    result = []
    for unavailable in load(base / 'unavailable-ebook-identities.json')['rows']:
        for title in unavailable['source_titles']:
            if normalize_title(title) in verified:
                continue
            raw = by_title[title]
            info = unavailable['book_info']
            result.append({'source_title': title, 'medium': raw['medium'], 'status': '当前电子书不可用，待平台可用',
                'explanation': unavailable['reason'] + '；保留营销费用和服务形式，不把占位零值计为正常指标。',
                'candidates': [{'book_id': unavailable['book_id'], 'title': info['title'], 'author': info['author'],
                                'detail_url': unavailable['detail_url']}],
                'source_rows': raw['source_rows'], 'evidence_path': str((base / 'unavailable-ebook-identities.json').resolve()),
                'official_evidence_path': str((base / 'availability' / (unavailable['book_id'] + '.json')).resolve())})
    for row in load(base / 'remaining-unresolved-after-supplement.json')['rows']:
        if normalize_title(row['source_title']) in verified:
            continue
        choices = candidates(row)
        if len(choices) > 1:
            status = '同名候选待核对作者'
            explanation = '存在多个官方同名/同格式标题候选，尚无充分的来源作者对应证据；按用户书名+作者标准核对，不按搜索顺序选择。'
        elif choices:
            status = '候选待来源作者确认'
            explanation = '发现官方标题候选，但原搜索未完整结束或来源作者尚未确认，暂不绑定。'
        else:
            status = '暂未确认标题对应条目'
            explanation = '本轮搜索未确认完整标题对应项，不表示平台不存在此书；需核对原书名和原著作者，不删副标题/版本词强行匹配。'
        slug = hashlib.sha256((row['medium'] + ':' + row['source_title']).encode()).hexdigest()[:20]
        result.append({'source_title': row['source_title'], 'medium': row['medium'], 'status': status,
                       'explanation': explanation, 'candidates': choices, 'source_rows': row['source_rows'],
                       'evidence_path': str((base / 'remaining-unresolved-after-supplement.json').resolve()),
                       'official_evidence_path': str((base / 'resolved' / (slug + '.json')).resolve())})
    return result


def note_block(entry):
    identity = digest(entry)[:20]
    start, end = '【作品核验记录 ' + identity + '】', '【作品核验记录结束 ' + identity + '】'
    lines = [start, '源作品名：' + entry['source_title'], '核验状态：' + entry['status'], entry['explanation']]
    for i, candidate in enumerate(entry['candidates'], 1):
        lines.append(str(i) + '.《' + candidate['title'] + '》；作者：' + candidate['author']
                     + '；官方作品页：' + (candidate['detail_url'] or '当前候选未取得详情入口，保留待核'))
    if not entry['candidates']:
        lines.append('候选作者/官方详情页：本轮未确认，不编造。')
    coordinates = sorted({r['source_coordinate_key'] for r in entry['source_rows']})
    archives = sorted({link['url'] for r in entry['source_rows'] for link in r.get('archive_links', [])})
    lines.append('原服务坐标：' + '、'.join(coordinates))
    lines.append('原服务留档：' + ('\n'.join(archives) if archives else '原源行未给有效留档链接'))
    lines.extend(['核验汇总证据：' + entry['evidence_path'], '官方响应证据：' + entry['official_evidence_path'], end])
    return start, '\n'.join(lines)


def build_plan(projects, entries):
    unique_index(projects, 'record_id')
    unique_index(projects, '作品唯一键')
    updates, decisions = {}, []
    for entry in entries:
        title = normalize_title(entry['source_title'])
        matches = [p for p in projects if normalize_title(p.get('书名')) == title
                   and str(p.get('作品唯一键', '')).startswith('pending:weread:' + entry['medium'] + ':')]
        if len(matches) != 1:
            decisions.append({'source_title': entry['source_title'], 'status': 'skip_pending_not_unique_or_absent'})
            continue
        project = matches[0]
        if project.get('监测范围') != ['营销项目'] or project.get('平台作品ID'):
            decisions.append({'source_title': entry['source_title'], 'record_id': project['record_id'],
                              'status': 'skip_archived_or_matched'})
            continue
        old = updates.get(project['record_id'], {}).get('证据与备注', project.get('证据与备注') or '')
        marker, block = note_block(entry)
        if marker in old:
            if block not in old:
                raise ValueError('Existing note marker has different content; review rather than overwrite')
            decisions.append({'source_title': entry['source_title'], 'record_id': project['record_id'], 'status': 'skip_already_appended'})
            continue
        updates[project['record_id']] = {'证据与备注': old + ('\n' if old else '') + block}
        decisions.append({'source_title': entry['source_title'], 'record_id': project['record_id'],
                          'status': 'append', 'evidence_status': entry['status'], 'candidate_count': len(entry['candidates'])})
    return {'schema_version': 'project.pending-work-notes.v1', 'project_preimage': projects,
            'updates': updates, 'decisions': decisions,
            'summary': {'project_count': len(projects), 'evidence_entries': len(entries), 'append_records': len(updates),
                        'decision_counts': dict(Counter(d['status'] for d in decisions)),
                        'evidence_status_counts': dict(Counter(e['status'] for e in entries)),
                        'max_result_note_length': max((len(v['证据与备注']) for v in updates.values()), default=0)},
            'allowed_mutation': 'Only append 证据与备注; no identity/scope/amount/record deletion or creation.'}


def assert_readback(plan, projects):
    old = unique_index(plan['project_preimage'], 'record_id')
    actual = unique_index(projects, 'record_id')
    if set(old) != set(actual):
        raise RuntimeError('Project record IDs changed')
    for rid, before in old.items():
        expected = {**before, **plan['updates'].get(rid, {})}
        if set(expected) != set(actual[rid]):
            raise RuntimeError('Project field set changed')
        for field, value in expected.items():
            if not same(value, actual[rid].get(field)):
                raise RuntimeError('Readback changed protected field or note: ' + rid + '/' + field)


def apply_plan(api, approved):
    if evidence_files() != approved['evidence_files']:
        raise RuntimeError('Evidence changed; rebuild the reviewed plan')
    fresh = build_plan(api.read('项目总表'), entries_from_evidence())
    if fresh != approved['plan']:
        raise RuntimeError('Full project preimage or note plan changed; no writes')
    if any(set(fields) != {'证据与备注'} for fields in fresh['updates'].values()):
        raise ValueError('Only note append is allowed')
    if not fresh['updates']:
        return
    state = api.receipt_dir / 'link-state.json'
    save(state, {'phase': 'unknown_until_full_readback', 'operation': 'pending_work_note_append',
                 'plan_digest': digest(fresh)})
    items = list(fresh['updates'].items())
    for i in range(0, len(items), 200):
        api.call('+record-batch-update', '项目总表', {'update_records': dict(items[i:i + 200])})
    assert_readback(fresh, api.read('项目总表'))
    save(state, {'phase': 'finished', 'operation': 'pending_work_note_append',
                 'readback': 'passed', 'protected_fields': 'unchanged', 'updated': len(items)})


def write_review(path, entries, plan):
    decisions = {d['source_title']: d for d in plan['decisions']}
    lines = ['# 微信读书待匹配作品核验计划', '',
             '仅追加项目的“证据与备注”，保留原备注及全部费用、服务形式、作品键和监测范围。', '',
             '没有匹配结果只表示本轮未确认，不能据此认定平台不存在作品。', '',
             '## 同名候选：按书名和原著作者确认', '']
    for entry in entries:
        if entry['status'] != '同名候选待核对作者':
            continue
        decision = decisions[entry['source_title']]
        lines.extend(['### ' + entry['source_title'], '',
                      '项目记录：' + str(decision.get('record_id', '未匹配当前 pending 记录')),
                      '原服务：' + '、'.join(r['source_coordinate_key'] for r in entry['source_rows']), ''])
        for candidate in entry['candidates']:
            label = '《' + candidate['title'] + '》—' + candidate['author']
            lines.append('- [' + label + '](' + candidate['detail_url'] + ')' if candidate['detail_url'] else '- ' + label)
        lines.extend(['', '来源留档：' + '；'.join(sorted({link['url'] for r in entry['source_rows'] for link in r.get('archive_links', [])})), ''])
    path.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    if args.apply and not args.plan:
        parser.error('--apply requires reviewed --plan PATH')
    assert_no_unknown()
    out = ROOT / 'receipts' / datetime.now(TZ).date().isoformat() / ('pending-work-notes-' + datetime.now(TZ).strftime('%H%M%S-%f'))
    api = Base(out)
    if args.apply:
        apply_plan(api, load(args.plan))
        print(json.dumps({'applied': True, 'receipt_dir': str(out)}, ensure_ascii=False))
    else:
        entries = entries_from_evidence()
        plan = build_plan(api.read('项目总表'), entries)
        wrapped = {'evidence_files': evidence_files(), 'entries': entries, 'plan': plan}
        save(out / 'plan.json', wrapped)
        write_review(out / 'candidate-review.md', entries, plan)
        print(json.dumps({'applied': False, 'plan_path': str(out / 'plan.json'), **plan['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
