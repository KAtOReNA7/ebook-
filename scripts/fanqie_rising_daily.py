"""Publication rising daily source; caller holds the existing daily process lock.

All 100 ebook ranks are archived. Only existing marketing projects are linked
and eligible for authorized board events. No project creation or expense mutation.
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from .collect_fanqie import Archive
    from .collect_fanqie_rising import collect, discover, normalize, ranking_archive, BOARD_NAME, HOST, DIRECTORY_PATH, RANK_PATH
    from .daily_run import (ROOT, now, shanghai_day, atomic_json, options,
                            notification_runtime, create_event_rows, notification_recipients)
    from .feishu_io import Base
    from .initialize_manual_projects import unknown_guard_paths
    from .rules import _event
except ImportError:
    from collect_fanqie import Archive
    from collect_fanqie_rising import collect, discover, normalize, ranking_archive, BOARD_NAME, HOST, DIRECTORY_PATH, RANK_PATH
    from daily_run import (ROOT, now, shanghai_day, atomic_json, options,
                           notification_runtime, create_event_rows, notification_recipients)
    from feishu_io import Base
    from initialize_manual_projects import unknown_guard_paths
    from rules import _event


BOARD_ID = 'fanqie_publication_rising'
BOARD_LABEL = '番茄出版飙升榜'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def configuration(root):
    path = root/'config/fanqie-ranking.json'
    config = read(path) if path.exists() else {}
    if config.get('status') == 'user_paused':
        if config.get('enabled'):
            raise ValueError('Conflicting paused/enabled Fanqie ranking configuration')
        return config
    if config.get('enabled') is True and config.get('status') != 'web_daily':
        raise ValueError('Enabled ranking requires explicit web_daily configuration')
    return config


def verified_snapshot(path, day):
    """Recompute normalized ranks from hashed official responses, not flags alone."""
    data = read(path)
    if data.get('schema_version') != 'fanqie.publication-rising.v1':
        raise ValueError('Unexpected ranking snapshot schema')
    start = datetime.fromisoformat(data['started_at'])
    end = datetime.fromisoformat(data['finished_at'])
    if shanghai_day(start) != day or shanghai_day(end) != day or end < start:
        raise ValueError('Ranking timestamps do not belong to the actual run day')
    if data.get('complete') is not True:
        raise ValueError('Incomplete ranking cannot advance daily comparison')
    raw = []
    for name, endpoint in [('directory', DIRECTORY_PATH), ('ranking', RANK_PATH)]:
        receipt = data[name+'_source']
        # Relative sibling evidence survives private handoff to another computer.
        content = Path(path).with_name(name+'.json').read_bytes()
        stamp = datetime.fromisoformat(receipt['observed_at'])
        if (receipt.get('http_status') != 200 or not receipt['url'].startswith(HOST+endpoint+'?')
                or hashlib.sha256(content).hexdigest() != receipt['response_sha256']
                or stamp.tzinfo is None or not start <= stamp <= end):
            raise ValueError('Ranking source receipt differs from archived evidence')
        raw.append(json.loads(content))
    normalized = normalize(raw[1], discover(raw[0]))
    if any(data.get(key) != value for key, value in normalized.items()):
        raise ValueError('Ranking normalized data differs from official evidence')
    return data


def marketing_index(projects):
    index = {}
    for project in projects:
        key = str(project.get('作品唯一键', ''))
        if not key.startswith('fanqie:') or ':audio:' in key:
            continue
        bid = str(project.get('平台作品ID', ''))
        if key != 'fanqie:'+bid or bid in index:
            raise ValueError('Conflicting Fanqie project identity; reconcile before ranking sync')
        index[bid] = project
    return {bid: p for bid, p in index.items()
            if p.get('作品形式') == '电子书' and '番茄小说' in options(p.get('平台'))
            and '营销项目' in options(p.get('监测范围'))}


def entry_events(previous, current, projects):
    """Only consecutive complete daily observations establish a new entry."""
    if previous is None or previous.get('complete') is not True or current.get('complete') is not True:
        return []
    before = shanghai_day(datetime.fromisoformat(previous['started_at']))
    day = shanghai_day(datetime.fromisoformat(current['started_at']))
    if (date.fromisoformat(day)-date.fromisoformat(before)).days != 1:
        return []
    old = {b['book_id'] for b in previous['books']}
    return [_event('board_enter', ('fanqie', b['book_id']), [BOARD_ID, before, day],
                   board_id=BOARD_ID, previous_date=before, observed_date=day,
                   current_rank=b['rank'], previous_rank=None,
                   observation_gap_days=1, reasons=['marketing_project'],
                   previous_observed_at=previous['ranking_source']['observed_at'],
                   current_observed_at=current['ranking_source']['observed_at'],
                   source_updated_at=current.get('source_updated_at'),
                   meaning='两次实际网页观察间进入完整出版飙升榜；不代表平台精确上榜时刻')
            for b in current['books'] if b['book_id'] not in old and b['book_id'] in projects]


def snapshot_rows(data, projects, source):
    day = shanghai_day(datetime.fromisoformat(data['started_at']))
    rows = []
    for book in data['books']:
        bid = book['book_id']
        row = {'榜单快照唯一键': day+':'+BOARD_ID+':fanqie:'+bid,
               '榜单': [BOARD_LABEL], '采集日期': day+' 00:00',
               '采集时间': data['ranking_source']['observed_at'][:16].replace('T', ' '),
               '采集批次': BOARD_ID+':'+data['started_at'], '本榜已取条数': 100,
               '快照完整性': ['完整'], '榜单来源': data['ranking_source']['url'],
               '平台作品ID': bid, '作品唯一键': 'fanqie:'+bid,
               '书名': book['title'], '名次': book['rank'],
               '证据与缺口': json.dumps({'source': str(source),
                   'rising_value_display': book['rising_value_display'],
                   'reader_label_raw': book.get('reader_label_raw'),
                   'note': data['note']}, ensure_ascii=False)}
        if bid in projects:
            row['关联项目'] = [{'id': projects[bid]['record_id']}]
        # No platform ranking date is fabricated from our request time.
        rows.append(row)
    return rows


def exit_observations(previous, current, projects):
    """Observed exits; notification delivery requires separate configuration."""
    if not previous or not previous.get('complete') or not current.get('complete'):
        return []
    before = shanghai_day(datetime.fromisoformat(previous['started_at']))
    day = shanghai_day(datetime.fromisoformat(current['started_at']))
    if (date.fromisoformat(day)-date.fromisoformat(before)).days != 1:
        return []
    present = {b['book_id'] for b in current['books']}
    return [{'kind': 'board_exit', 'book_id': b['book_id'], 'previous_rank': b['rank'],
             'previous_date': before, 'observed_date': day}
            for b in previous['books'] if b['book_id'] not in present and b['book_id'] in projects]


def exit_events(previous, current, projects):
    return [_event('board_exit', ('fanqie', row['book_id']),
                   [BOARD_ID, row['previous_date'], row['observed_date']],
                   board_id=BOARD_ID, previous_date=row['previous_date'],
                   observed_date=row['observed_date'], previous_rank=row['previous_rank'],
                   current_rank=None, observation_gap_days=1, reasons=['marketing_project'],
                   previous_observed_at=previous['ranking_source']['observed_at'],
                   current_observed_at=current['ranking_source']['observed_at'],
                   source_updated_at=current.get('source_updated_at'),
                   meaning='两次实际网页观察间离开完整出版飙升榜；不代表平台精确下榜时刻或作品下架')
            for row in exit_observations(previous, current, projects)]


def notification_gate(config, runtime, data, day):
    if config.get('notifications_enabled') is not True or not runtime['notifications_enabled']:
        return False
    if config.get('notification_basis') == 'consecutive_complete_observations':
        # Explicit owner-approved observation semantics; never manufacture the
        # platform update time or turn old candidates into a historical replay.
        effective = config.get('notifications_effective_from')
        if not effective:
            return False
        return date.fromisoformat(day) >= date.fromisoformat(effective)
    return bool(data.get('source_updated_at'))


def notification_rows(events, projects, batch):
    rows = []
    for event in events:
        project = projects[event['book_id']]
        entering = event['kind'] == 'board_enter'
        title = project['书名']+'｜'+('进入' if entering else '离开')+'番茄出版飙升榜'
        rank = ('本次第'+str(event['current_rank'])+'名' if entering else
                '上次第'+str(event['previous_rank'])+'名，本次不在完整Top100中')
        rows.append({'事件唯一键': event['event_id'], '事件类型': event['kind'],
                     '关联项目': [{'id': project['record_id']}],
                     '发生日期': event['observed_date']+' 00:00', '规则ID': 'v1:FANQIE_RANK',
                     '判定证据': json.dumps(event, ensure_ascii=False, sort_keys=True),
                     '通知标题': title,
                     '通知正文': title+'\n'+rank+'\n'+event['meaning']+
                     '\n实际采样区间：'+event['previous_observed_at']+' 至 '+event['current_observed_at'],
                     '收件人': [{'id': r} for r in notification_recipients()],
                     '通知状态': ['待发送'], '采集批次': batch})
    return rows


def run(args, root=ROOT, api=None, clock=now):
    config = configuration(root)
    if config.get('enabled') is not True:
        return {'status': config.get('status', 'disabled'), 'collection_enabled': False,
                'complete': False, 'write_count': 0, 'reason': config.get('reason')}
    guards = unknown_guard_paths(root)
    for name in ('tencent-state.json', 'daily-state.json', 'fanqie-state.json'):
        for path in (root/'runs').glob('*/'+name):
            if read(path).get('phase') != 'finished':
                guards.append(str(path))
    if guards:
        return {'status': 'requires_review', 'write_count': 0, 'guards': guards}
    stamp = clock()
    day = shanghai_day(stamp)
    directory = root/'runs'/day
    state_path = directory/'fanqie-rising-state.json'
    source = directory/'fanqie-rising'/'snapshot.json'
    if state_path.exists():
        state = read(state_path)
        if state.get('phase') != 'finished':
            raise RuntimeError('Ranking mutation requires reconciliation')
        verified_snapshot(source, day)
        return {**state['summary'], 'replayed': True, 'write_count': 0, 'dry_run': args.dry_run}
    if not source.exists():
        # A partial local archive is retained; use a new attempt directory, then
        # freeze a successful normalized snapshot with its exact raw evidence.
        attempt = directory/('fanqie-rising-attempt-'+stamp.strftime('%H%M%S-%f'))
        archive = ranking_archive(attempt)
        try:
            result = collect(archive)
        except Exception as exc:
            atomic_json(attempt/'failure.json', {'error': str(exc), 'sources': archive.sources})
            return {'status': 'failed', 'complete': False, 'write_count': 0,
                    'error': str(exc), 'evidence': str(attempt)}
        data = {'schema_version': 'fanqie.publication-rising.v1', 'started_at': stamp.isoformat(),
                'finished_at': clock().isoformat(), **result, 'sources': archive.sources}
        atomic_json(attempt/'snapshot.json', data)
        verified_snapshot(attempt/'snapshot.json', day)
        source.parent.mkdir(parents=True, exist_ok=True)
        for name in ('directory.json', 'ranking.json'):
            source.with_name(name).write_bytes((attempt/name).read_bytes())
        atomic_json(source, data)
    data = verified_snapshot(source, day)
    yesterday = (date.fromisoformat(day)-timedelta(days=1)).isoformat()
    prior_dir = root/'runs'/yesterday
    previous = None
    if (prior_dir/'fanqie-rising-state.json').exists():
        if read(prior_dir/'fanqie-rising-state.json').get('phase') == 'finished':
            previous = verified_snapshot(prior_dir/'fanqie-rising'/'snapshot.json', yesterday)
    api = api or Base(root/'receipts'/day/('fanqie-rising-'+stamp.strftime('%H%M%S-%f')))
    projects = marketing_index(api.read('项目总表'))
    events = entry_events(previous, data, projects)
    exits = exit_observations(previous, data, projects)
    if config.get('notify_exits') is True:
        events += exit_events(previous, data, projects)
    rows = snapshot_rows(data, projects, source)
    outbox = notification_rows(events, projects, BOARD_ID+':'+data['started_at'])
    runtime = notification_runtime(getattr(args, 'shadow', False))
    notify = notification_gate(config, runtime, data, day)
    summary = {'status': 'completed', 'date': day, 'complete': True, 'count': 100,
               'collection_enabled': True, 'baseline': previous is None,
               'marketing_matches': sum(b['book_id'] in projects for b in data['books']),
               'candidate_events': len(events), 'events_created': 0, 'write_count': 0,
               'observed_exits': len(exits),
               'notifications_enabled': notify, 'source_updated_at': data.get('source_updated_at'),
               'notification_basis': config.get('notification_basis', 'platform_update_time'),
               'notification_hold': None if notify else '榜单通知未启用、未到生效日或时间口径未确认；候选仅本地留档',
               'source': str(source), 'dry_run': args.dry_run}
    atomic_json(directory/'fanqie-rising-plan.json', {'summary': summary, 'rows': rows, 'events': outbox,
                                                   'exit_observations': exits})
    if args.dry_run:
        return summary
    atomic_json(state_path, {'phase': 'requires_review', 'source': str(source), 'summary': summary})
    api.ensure('榜单快照', '榜单快照唯一键', rows, [['采集日期', '==', f'ExactDate({day})']])
    if notify:
        summary['events_created'] = create_event_rows(api, outbox, day)
    batch = BOARD_ID+':'+data['started_at']
    log = {'运行批次': batch, '计划时间': day+' 10:00',
           '开始时间': data['started_at'][:16].replace('T', ' '),
           '结束时间': data['finished_at'][:16].replace('T', ' '),
           '预期榜单数': 1, '完整榜单数': 1, '预期作品数': 100,
           '成功作品数': 100, '失败作品数': 0, '新增事件数': summary['events_created'],
           '状态': ['成功'], '证据路径': str(source),
           '缺口原因': '仅榜单身份与排名采集成功，不表示作品详情指标完整；平台更新时间未提供。'
                       '飙升值不作为累计读者；未关联榜单作品不自动创建营销项目。'}
    api.ensure('运行日志', '运行批次', [log], [['运行批次', '==', batch]])
    summary.update(write_count=api.changes, readback='passed')
    atomic_json(state_path, {'phase': 'finished', 'summary': summary})
    return summary
