"""Persist a real public metadata snapshot. No messages or fabricated metrics.

First snapshot establishes a baseline. Writes use stable keys and full readback.
The input must be produced by collect_weread.py; keep its evidence beside it.
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
try:
    from .feishu_io import Base, ROOT, save
    from .work_copyright import apply_work_copyright
    from .presentation import short_copyright
    from .service_scope import policy, admitted_weread_keys
    from .daily_observation import select_daily_observations
    from .board_display import memberships, OFF_BOARD
except ImportError:
    from feishu_io import Base, ROOT, save
    from work_copyright import apply_work_copyright
    from presentation import short_copyright
    from service_scope import policy, admitted_weread_keys
    from daily_observation import select_daily_observations
    from board_display import memberships, OFF_BOARD

BOARDS = {'rising': '微信飙升Top50', 'newbook': '微信新书Top50',
          'novel_female_rising': '微信女生小说飙升Top50',
          'newbook_novel_female': '微信女生小说新书Top50',
          'newbook_novel_female_store': '微信女生小说新书Top50'}


def minute(s):
    return s[:16].replace('T', ' ')


def present(row):
    return {k: v for k, v in row.items() if v is not None and v != ''}


def validate_recent_tracking_dates(existing, books, tracks):
    """A stale device registry must not restart an already established cohort."""
    for book in books:
        book_id = str(book['book_id'])
        old = existing.get('weread:' + book_id, {})
        track = tracks[book_id]
        fields = (('首次进入最近上架日', 'start_date'), ('版权方追踪截止日', 'end_date'))
        values = [old.get(field) for field, _ in fields]
        if not any(values):
            continue  # Existing marketing/ranking book entering this scope for the first time.
        for field, source in fields:
            value = old.get(field)
            if not isinstance(value, str) or value[:10] != track[source]:
                raise ValueError('Publisher tracking dates conflict for weread:' + book_id
                                 + '; reconcile the device registry with Base before writing')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--run-kind', choices=['scheduled', 'local', 'supplement'], default='local')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    data = json.loads(source.read_text(encoding='utf-8'))
    result = sync(data, source, args.run_kind)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def sync(data, source, run_kind='local', api=None, recent_tracks=None):
    """Shared metric persistence; recent_tracks must come from the validated registry."""
    if data.get('schema_version') != 'weread.public.v1' or not data.get('finished_at'):
        raise ValueError('Not a finished collector snapshot')
    day = data['collection_date']
    batch = ('publisher-recent:' if recent_tracks is not None else 'weread:') + data['started_at']
    run_dir = ROOT / 'receipts' / day / datetime.now().strftime('%H%M%S-%f')
    api = api or Base(run_dir)
    prior = [r for r in api.read('项目总表') if r.get('作品唯一键')]
    existing = {r['作品唯一键']: r for r in prior}
    if recent_tracks is not None:
        if data.get('charts') or any(str(b['book_id']) not in recent_tracks for b in data['books']):
            raise ValueError('Recent-list sync requires registered targets and no chart mutations')
        validate_recent_tracking_dates(existing, data['books'], recent_tracks)
    elif policy().get('enabled'):
        admitted=admitted_weread_keys(data,existing)
        data={**data,'books':[b for b in data['books'] if 'weread:'+str(b['book_id']) in admitted]}
    alias_path = ROOT / 'config/copyright-aliases.json'
    aliases = set(json.loads(alias_path.read_text(encoding='utf-8')).get('exact_aliases', [])) if alias_path.exists() else set()
    if len(existing) != len(prior):
        raise ValueError('Duplicate book identity in Base')
    projects = []
    dayrows = []
    board_labels = memberships(data)
    board_fields = api._fields('项目总表')
    for book in data['books']:
        key = 'weread:' + str(book['book_id'])
        old = existing.get(key, {})
        book = apply_work_copyright(book, '营销项目' in old.get('监测范围', []))
        missing = [k for k in ['word_count','copyright_provider','reviewer_count','recommendation_percent','today_reading_count']
                   if book.get(k) is None or book.get(k) == '']
        status = '未开分' if missing == ['recommendation_percent'] else '部分缺失' if missing else '成功'
        project = {
            '书名': book.get('title'), '作品唯一键': key, '平台': ['微信读书'],
            '平台作品ID': str(book['book_id']), '作品主页': book.get('detail_url'),
            '作者': book.get('author'), '字数': book.get('word_count'),
            '版权方原文': book.get('platform_copyright_provider', book.get('copyright_provider')),
            '版权方': [short_copyright(book['copyright_provider'])] if book.get('copyright_provider') else [],
            '作品分类': book.get('category'),
            '最新点评人数': book.get('reviewer_count'), '最新推荐值': book.get('recommendation_percent'),
            '最新今日阅读人数': book.get('today_reading_count'),
            '最新采集时间': minute(book.get('detail_source', {}).get('observed_at', data['finished_at'])),
            '采集状态': [status],
            '指标精度说明': '点评人数/今日阅读为公开数值；推荐值按网页显示的百分数存储（如90.1）。详情页阅读量不代作今日阅读。',
        }
        if board_labels is not None and '当前榜单' in board_fields:
            project['当前榜单'] = [board_labels.get(key, OFF_BOARD)]
            project['榜单核验日期'] = day + ' 00:00'
        # Existing project status, marketing dates, tracking deadline and copyright
        # decisions are user-owned; do not overwrite them during metadata refresh.
        if not old:
            project.update({'作品形式': '电子书', '监测范围': ['仅榜单样本'], '首次上榜采集日': day + ' 00:00',
                            '追踪截止日': (datetime.fromisoformat(day) + timedelta(days=29)).strftime('%Y-%m-%d 00:00'),
                            '磨铁版权确认': ['待核实'],
                            '证据与备注': '首次公开榜单观察；非腾讯营销项目。首次快照仅建基线，不发送首次上榜/越线通知。'})
        if recent_tracks is not None:
            track = recent_tracks[str(book['book_id'])]
            project.update({'监测范围': old.get('监测范围') or ['版权方上新'],
                            '首次进入最近上架日': track['start_date'] + ' 00:00',
                            '版权方追踪截止日': track['end_date'] + ' 00:00'})
            if not old:
                project.pop('首次上榜采集日', None)
                project['追踪截止日'] = track['end_date'] + ' 00:00'
                project['证据与备注'] = '完整最近上架列表基线之后首次出现的电子书ID；追踪30天，连载更新与重入不重置。累计阅读人数口径尚未核实。'
        if book.get('copyright_basis') or book.get('copyright_provider') in aliases:
            project['磨铁版权确认'] = ['是']
        elif old.get('版权方原文') in aliases and book.get('copyright_provider') not in aliases:
            project['磨铁版权确认'] = ['待核实']
        # Clear an unavailable latest metric instead of showing yesterday as today.
        # New records simply omit unavailable cells; updates explicitly clear them.
        if not old or not old.get('最新采集时间') or project['最新采集时间'] >= minute(old['最新采集时间']):
            projects.append(project if old else present(project))
        dayrows.append((book, key, missing, status))
    project_index = api.ensure('项目总表', '作品唯一键', projects, update=True)
    daily = []
    for book, key, missing, status in dayrows:
        detail = book.get('detail_source', {})
        daily.append({
            '日数据唯一键': day + ':' + key, '作品唯一键': key, '关联项目': [{'id': project_index[key]['record_id']}],
            '平台': ['微信读书'], '数据日期': day + ' 00:00', '采集时间': minute(detail.get('observed_at', data['finished_at'])),
            '点评人数': book.get('reviewer_count'), '推荐值': book.get('recommendation_percent'),
            '今日阅读人数': book.get('today_reading_count'), '字数': book.get('word_count'),
            '版权方原文': book.get('platform_copyright_provider', book.get('copyright_provider')),
            '版权方': [short_copyright(book['copyright_provider'])] if book.get('copyright_provider') else [],
            '作品分类': book.get('category'),
            '采集状态': [status], '精度': ['精确'], '采集批次': batch,
            '数据来源': book.get('detail_url'), '原始证据定位': detail.get('archive_path', str(source)),
            '缺失字段与原因': '；'.join({'word_count':'字数：平台未提供',
                'copyright_provider':'版权方：当前采集来源未返回，尚待核实',
                'reviewer_count':'点评人数：未取得有效值',
                'recommendation_percent':'推荐值：未开分',
                'today_reading_count':'今日阅读人数：'+(book.get('today_reading_missing_reason') or '未取得带今日阅读含义的有效值')
                }.get(k,k) for k in missing) or None,
            '指标原文': json.dumps({'newRating':book.get('recommendation_raw'),
                                  'copyright_basis':book.get('copyright_basis'),
                                  'platform_copyright_provider':book.get('platform_copyright_provider',book.get('copyright_provider')),
                                  'newRatingCount':book.get('reviewer_count'),
                                  'today_reading_source':book.get('today_reading_source'),
                                  'field_mapping':data['field_mapping']}, ensure_ascii=False),
        })
        if recent_tracks is not None:
            daily[-1]['缺失字段与原因'] = '；'.join(filter(None, [daily[-1]['缺失字段与原因'],
                '累计阅读人数：来源指标含义尚未核实，留空；不使用今日阅读累加或详情阅读量替代']))
    daily_filter=[['数据日期', '==', f'ExactDate({day})']]
    daily=select_daily_observations(daily,api.read('每日数据',daily_filter))
    api.ensure('每日数据', '日数据唯一键', daily, daily_filter, update=True)
    snapshots = []
    for chart in data['charts']:
        chart_id = chart['chart_id']
        if chart_id not in BOARDS:
            raise ValueError('Unknown chart ID')
        common = {'榜单': [BOARDS[chart_id]], '采集日期': day + ' 00:00', '采集批次': batch,
                  '采集时间': minute(data['started_at']), '本榜已取条数': len(chart['records']),
                  '快照完整性': ['完整' if chart['complete_top50'] else '不完整'],
                  '榜单来源': 'https://weread.qq.com/web/category/' + chart_id,
                  '证据与缺口': json.dumps(chart.get('errors', []), ensure_ascii=False)}
        for book in chart['records']:
            key = 'weread:' + str(book['book_id'])
            snapshots.append({**common, '榜单快照唯一键': day + ':' + chart_id + ':' + key,
                              '平台作品ID': str(book['book_id']), '作品唯一键': key, '书名': book['title'],
                              '名次': book['rank'], '关联项目': [{'id': project_index[key]['record_id']}]})
        if not chart['records']:
            snapshots.append({**common, '榜单快照唯一键': day + ':' + chart_id + ':missing',
                              '证据与缺口': '公开入口返回0条，无法证明榜单为空，不参与上下榜判断。'})
    api.ensure('榜单快照', '榜单快照唯一键', snapshots, [['采集日期', '==', f'ExactDate({day})']])
    complete = sum(c['complete_top50'] for c in data['charts'])
    log = {'运行批次': batch, '计划时间': day + ' 10:00', '开始时间': minute(data['started_at']),
           '结束时间': minute(data['finished_at']), '预期榜单数': len(data['charts']), '完整榜单数': complete,
           '预期作品数': len(data['books']), '成功作品数': sum(s == '成功' for _,_,_,s in dayrows),
           '失败作品数': len(data.get('detail_errors', [])), '新增事件数': 0,
           '状态': ['成功' if complete == len(data['charts']) and all(s == '成功' for _,_,_,s in dayrows) else '部分失败'], '证据路径': str(source),
           '缺口原因': ('本次运行方式：' + run_kind + '；快照实际采集时间：' + data['started_at']
                       + '；完整榜单数：' + str(complete) + '/' + str(len(data['charts'])) + '。'
                       + '本日志只核验本次微信读书采集与同步；其他来源状态见各来源运行日志。'
                       + '通知由独立事件阶段生成，本同步阶段不发送消息。'
                       + ' '.join(data.get('gaps', [])))}
    api.ensure('运行日志', '运行批次', [log], [['运行批次', '==', batch]])
    result = {'source': str(source), 'base_url': api.manifest['base']['url'], 'date': day,
              'books': len(data['books']), 'daily_records':len(daily), 'rank_rows':len(snapshots),
              'complete_boards':complete, 'write_count': api.changes, 'readback': 'passed',
              'business_notifications_sent':0, 'receipt_dir': str(run_dir)}
    save(run_dir / 'summary.json', result)
    return result


if __name__ == '__main__':
    main()
