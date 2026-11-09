"""Daily WeRead collection, idempotent Base sync, and native-workflow event outbox.

No direct IM sends. A finished daily source is frozen and reused. Interrupted
mutations stop for read-only reconciliation; they are never blindly retried.
Run: python scripts/daily_run.py --run-kind scheduled [--dry-run] [--shadow]
Notifications default to shadow. Live/owner-only delivery also requires a
configured owner and notifications_enabled=true; dates never approve production.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

try:
    from .feishu_io import Base, ROOT, same
    from .rules import board_events, recommendation_events
except ImportError:
    from feishu_io import Base, ROOT, same
    from rules import board_events, recommendation_events

TZ = timezone(timedelta(hours=8))
BOARD_IDS = {'rising': 'weread_rising', 'newbook': 'weread_new',
             'novel_female_rising': 'weread_female_rising',
             'newbook_novel_female': 'weread_female_new',
             'newbook_novel_female_store': 'weread_female_new'}
BOARD_NAMES = {'weread_rising': '微信读书飙升榜Top50', 'weread_new': '微信读书新书榜Top50',
               'weread_female_rising': '女生小说飙升Top50', 'weread_female_new': '女生小说新书Top50'}
# Verified current user (auth status). Date changes never approve production.
try:
    from .notification_policy import OWNER_RECIPIENTS, enforce_recipients
except ImportError:
    from notification_policy import OWNER_RECIPIENTS, enforce_recipients
RECIPIENTS = OWNER_RECIPIENTS
FIXED_GAPS = ['番茄出版飙升榜由独立 fanqie_rising 模块报告；暂停、缺榜和完整榜分别处理。']


def now():
    return datetime.now(TZ)


def notification_runtime(force_shadow=False):
    path = ROOT / 'config' / 'runtime.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    effective_from = config.get('effective_from')
    eligible_date = effective_from is None or now().date() >= date.fromisoformat(effective_from)
    enabled = (not force_shadow and eligible_date and config.get('mode') in {'live', 'owner_only'}
               and config.get('notifications_enabled') is True)
    approved = config.get('production_approved') is True
    return {'mode': ('live' if approved and config.get('mode') == 'live' else 'owner_only') if enabled else 'shadow',
            'notifications_enabled': enabled, 'production_approved': approved,
            'configured_mode': config.get('mode', 'shadow'), 'effective_from': effective_from}


def shanghai_day(value=None):
    value = value or now()
    if value.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return value.astimezone(TZ).date().isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def process_lock(path):
    """OS releases the lock on process death; no unsafe stale-file deletion."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def options(value):
    if not isinstance(value, list):
        value = [value] if value else []
    return {str(item.get('name', item.get('text', ''))) if isinstance(item, dict) else str(item)
            for item in value}


def calendar_date(value):
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def is_marketing(project):
    return '营销项目' in options(project.get('监测范围'))


def is_weread(project):
    return str(project.get('作品唯一键', '')).startswith('weread:') and '微信读书' in options(project.get('平台'))


def tracked_projects(projects, day):
    """Keep departed books through D29; marketing projects remain monitored."""
    today = date.fromisoformat(day)
    tracked, pending = [], []
    for project in projects:
        if not is_weread(project):
            if is_marketing(project) and '微信读书' in options(project.get('平台')):
                pending.append(str(project.get('作品唯一键') or project.get('record_id') or 'unkeyed') + ': platform identity pending')
            continue
        start = calendar_date(project.get('首次上榜采集日'))
        active = is_marketing(project) or (start is not None and 0 <= (today - start).days < 30)
        if not active:
            continue
        key = project['作品唯一键']
        bid = str(project.get('平台作品ID') or key.removeprefix('weread:'))
        if key != 'weread:' + bid:
            pending.append(key + ': platform ID mismatch')
            continue
        url = project.get('作品主页') or ''
        match = re.fullmatch(r'\[[^\]]*\]\((https://[^\s]+)\)', url)
        url = match.group(1) if match else url
        if not url.startswith('https://weread.qq.com/web/reader/'):
            pending.append(key + ': no verified official reader URL')
            continue
        tracked.append({'book_id': bid, 'detail_url': url, 'title': project.get('书名')})
    return tracked, pending


def load_snapshot(path, expected_day):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 'weread.public.v1' or not data.get('finished_at'):
        raise ValueError(f'Unfinished or incompatible source retained for review: {path}')
    if data.get('collection_date') != expected_day:
        raise ValueError(f'Source date mismatch: {path}')
    if not isinstance(data.get('books'), list) or not isinstance(data.get('charts'), list):
        raise ValueError(f'Source schema invalid: {path}')
    return data


def call_script(name, arguments, log_path, timeout=1800):
    with log_path.open('w', encoding='utf-8') as log:
        try:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts' / name), *arguments],
                                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                    encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        except subprocess.TimeoutExpired as exc:
            # A sync may have reached Feishu before timing out. Its caller keeps
            # the existing unknown-write guard, so this must not trigger retries.
            raise RuntimeError(f'{name} timed out after {timeout}s; inspect {log_path}; '
                               'preserve mutation state and reconcile receipts before any retry') from exc
    if result.returncode:
        raise RuntimeError(f'{name} failed ({result.returncode}); inspect {log_path}; no automatic mutation retry')


def collect_frozen(run_dir, day, tracked, run_kind):
    source = run_dir / 'weread.json'
    existing = load_snapshot(source, day)
    if existing is not None:
        return existing, True
    tracked_path = run_dir / 'tracked-projects.json'
    atomic_json(tracked_path, tracked)
    staging = run_dir / 'weread.collecting.json'
    # A collector finished before interruption may be promoted without refetching.
    data = load_snapshot(staging, day) if staging.exists() else None
    if data is None:
        call_script('collect_weread.py', ['--output', str(staging), '--tracked-json', str(tracked_path)],
                    run_dir / 'collect.log')
        data = load_snapshot(staging, day)
    data['run_kind'] = run_kind
    data['scheduled_for'] = day + 'T10:00:00+08:00' if run_kind == 'scheduled' else None
    atomic_json(source, data)
    return data, False


def event_candidates(previous, current, projects):
    """Adapt archived collector data to pure rules, with exact entity scoping."""
    keyed_projects = [row for row in projects if row.get('作品唯一键')]
    project_index = {str(row['作品唯一键']): row for row in keyed_projects}
    if len(project_index) != len(keyed_projects):
        raise ValueError('Duplicate project identity')
    scoped = [{'platform': 'weread', 'book_id': key.removeprefix('weread:')}
              for key, row in project_index.items() if is_weread(row) and is_marketing(row)]
    pending, events = [], []

    def board(snapshot, chart):
        entries = []
        for row in chart['records']:
            project = project_index.get('weread:' + str(row['book_id']), {})
            verified = '是' in options(project.get('磨铁版权确认'))
            entries.append({'book_id': str(row['book_id']), 'rank': row['rank'],
                            'copyright_verified': verified, 'copyright_group': '磨铁' if verified else None})
        return {'platform': 'weread', 'board_id': BOARD_IDS[chart['chart_id']],
                'date': snapshot['collection_date'], 'valid': not chart.get('errors'),
                'complete': chart.get('complete_top50') is True, 'entries': entries}

    old_charts = {row['chart_id']: row for row in previous['charts']} if previous else {}
    for chart in current['charts']:
        cid = chart['chart_id']
        if cid not in BOARD_IDS:
            pending.append('unknown board: ' + str(cid))
            continue
        old = old_charts.get(cid)
        result = board_events(board(previous, old) if old else None, board(current, chart), scoped)
        events.extend(result['events'])
        pending.extend(cid + ': ' + error for error in result['pending'])
    old_books = {str(row['book_id']): row for row in previous['books']} if previous else {}
    for book in current['books']:
        bid = str(book['book_id'])
        project = project_index.get('weread:' + bid, {})
        if not is_marketing(project):
            continue
        def observation(snapshot, row):
            return {'platform': 'weread', 'book_id': bid, 'date': snapshot['collection_date'],
                    'valid': row.get('status') != 'error', 'recommendation': row.get('recommendation_percent')}
        old = old_books.get(bid)
        result = recommendation_events(observation(previous, old) if old else None, observation(current, book))
        events.extend(result['events'])
        pending.extend('weread:' + bid + ': ' + error for error in result['pending'])
    return list({event['event_id']: event for event in events}.values()), pending


def notification_rows(events, projects, batch):
    index = {row['作品唯一键']: row for row in projects if row.get('作品唯一键')}
    labels = {'board_enter': '上榜', 'board_exit': '下榜',
              'recommendation_up': '推荐值达到90%', 'recommendation_down': '推荐值跌破90%'}
    rows = []
    for event in events:
        key = 'weread:' + event['book_id']
        project = index.get(key)
        if not project or not project.get('record_id'):
            raise ValueError('Missing verified project link for event: ' + key)
        label = labels[event['kind']]
        title = (project.get('书名') or key) + '｜' + label
        if event.get('board_id'):
            detail = BOARD_NAMES[event['board_id']] + '；前次名次：' + str(event['previous_rank']) + '；本次名次：' + str(event['current_rank'])
        else:
            detail = f"推荐值：{event['previous_value']}% → {event['current_value']}%"
        text = (title + '\n' + detail + '\n观测区间：' + event['previous_date'] + ' 至 ' + event['observed_date']
                + '\n作品：' + str(project.get('作品主页', key)))
        rows.append({'事件唯一键': event['event_id'], '事件类型': event['kind'],
                     '关联项目': [{'id': project['record_id']}], '发生日期': event['observed_date'] + ' 00:00',
                     '规则ID': 'v' + event['rule_version'] + ':' + event['kind'],
                     '判定证据': json.dumps(event, ensure_ascii=False, sort_keys=True),
                     '通知标题': title, '通知正文': text, '收件人': [{'id': value} for value in RECIPIENTS],
                     '通知状态': ['待发送'], '采集批次': batch})
    return rows


def create_event_rows(api, rows, day):
    try:
        from .notification_digest import configured, stage
    except ImportError:
        from notification_digest import configured, stage
    if configured(ROOT):
        return stage(api, rows, day, ROOT)
    return deliver_event_rows(api, rows, day)


def deliver_event_rows(api, rows, day):
    """Never overwrite state advanced by native automation; verify immutable fields."""
    conditions = [['发生日期', '==', f'ExactDate({day})']]
    existing = api.read('通知事件', conditions)
    index = {row['事件唯一键']: row for row in existing}
    if len(index) != len(existing):
        raise RuntimeError('Duplicate event keys in Base')
    missing = [row for row in rows if row['事件唯一键'] not in index]
    for row in missing:
        enforce_recipients(row, ROOT)
    for offset in range(0, len(missing), 200):
        api.call('+record-batch-create', '通知事件', {'create_records': missing[offset:offset + 200]})
    readback = api.read('通知事件', conditions) if missing else existing
    verified = {row['事件唯一键']: row for row in readback}
    if len(verified) != len(readback):
        raise RuntimeError('Duplicate event keys in readback')
    for expected in rows:
        actual = verified.get(expected['事件唯一键'])
        if not actual:
            raise RuntimeError('Event write missing on readback; stop without retry')
        for field in ['事件唯一键', '事件类型', '关联项目', '规则ID', '判定证据', '通知标题', '通知正文', '收件人', '采集批次']:
            if not same(expected[field], actual.get(field)):
                raise RuntimeError('Event readback mismatch: ' + expected['事件唯一键'] + '/' + field)
        status = options(actual.get('通知状态'))
        if len(status) != 1 or not status <= {'待发送', '发送中', '已发送', '发送失败', '结果未知', '暂不发送'}:
            raise RuntimeError('Unexpected notification state')
    return len(missing)


def run(args):
    day = shanghai_day()
    run_dir = ROOT / 'runs' / day
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / 'daily-state.json'
    runtime = notification_runtime(getattr(args, 'shadow', False))
    for prior_state_path in (ROOT / 'runs').glob('*/daily-state.json'):
        prior_state = json.loads(prior_state_path.read_text(encoding='utf-8'))
        if prior_state.get('phase') in {'sync_in_progress', 'events_in_progress', 'requires_review'}:
            raise RuntimeError('Unresolved mutation state requires read-only reconciliation: ' + str(prior_state_path))
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if state.get('phase') in {'sync_in_progress', 'events_in_progress', 'requires_review'}:
        raise RuntimeError('Previous mutation may have an unknown outcome. Read-only receipt reconciliation is required; do not rerun writes.')
    if state.get('phase') == 'finished' and not args.dry_run:
        # Replays do not write or resend, but current coverage must include
        # independently verified supplements and newly registered projects.
        api = Base(ROOT / 'receipts' / day / ('coverage-replay-' + now().strftime('%H%M%S-%f')))
        tracked, pending = tracked_projects(api.read('项目总表'), day)
        coverage_source = run_dir / 'weread-effective.json'
        if not coverage_source.exists():
            coverage_source = run_dir / 'weread.json'
        try:
            from .coverage import weread_coverage
        except ImportError:
            from coverage import weread_coverage
        current = load_snapshot(coverage_source, day)
        if current is None:
            raise RuntimeError('Completed run has no retained source snapshot')
        return {**state['summary'], 'replayed': True, 'writes_this_replay': 0,
                'coverage': weread_coverage(current, tracked, pending),
                'coverage_source': str(coverage_source)}
    attempt = now().strftime('%H%M%S-%f')
    api = Base(ROOT / 'receipts' / day / ('daily-' + attempt))
    projects = api.read('项目总表')
    tracked, pending = tracked_projects(projects, day)
    if args.dry_run and (run_dir / 'weread-effective.json').exists():
        data, reused = load_snapshot(run_dir / 'weread-effective.json', day), True
    else:
        data, reused = collect_frozen(run_dir, day, tracked, args.run_kind)
    try:
        from .coverage import weread_coverage
    except ImportError:
        from coverage import weread_coverage
    coverage = weread_coverage(data, tracked, pending)
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    previous_path = ROOT / 'runs' / yesterday / 'weread-effective.json'
    if not previous_path.exists():
        previous_path = ROOT / 'runs' / yesterday / 'weread.json'
    previous = load_snapshot(previous_path, yesterday)
    baseline = previous is None
    candidates, rule_pending = event_candidates(previous, data, projects)
    gaps = FIXED_GAPS + list(data.get('gaps', [])) + pending + rule_pending
    if baseline:
        gaps.append('无前一日可用快照，本次只建立基线，不产生上下榜或越线事件。')
    frozen_ids = {str(row['book_id']) for row in data['books']}
    for row in tracked:
        if row['book_id'] not in frozen_ids:
            gaps.append('冻结快照未包含新增追踪作品 ' + row['book_id'] + '，次日补采；不伪造当日值。')
    summary = {'date': day, 'status': 'partial', 'coverage': coverage, 'run_kind': args.run_kind,
               'snapshot_reused': reused, 'snapshot_started_at': data['started_at'],
               'source': str(run_dir / ('weread-effective.json' if args.dry_run and (run_dir / 'weread-effective.json').exists() else 'weread.json')),
               'baseline': baseline, 'candidate_events': len(candidates), 'pending': rule_pending,
               'gaps': list(dict.fromkeys(gaps)), 'direct_im_sent': 0, 'dry_run': args.dry_run,
               'notification_mode': runtime['mode'], 'notifications_enabled': runtime['notifications_enabled']}
    if runtime['mode'] == 'shadow':
        summary['gaps'].append('并行验收影子模式：真实事件只留本地，不写飞书通知事件，不发送消息。旧任务尚保留。')
    atomic_json(run_dir / ('dry-run-plan.json' if args.dry_run else 'daily-plan.json'),
                {'summary': summary, 'events': candidates, 'tracked': tracked})
    if args.dry_run:
        # Read the outbox to prove key dedup without queuing anything.
        existing = api.read('通知事件', [['发生日期', '==', f'ExactDate({day})']])
        known = {row['事件唯一键'] for row in existing}
        summary['new_event_candidates'] = sum(event['event_id'] not in known for event in candidates)
        summary['write_count'] = 0
        atomic_json(run_dir / 'dry-run-summary.json', summary)
        return summary
    state = {'date': day, 'attempt': attempt, 'started_at': now().isoformat(), 'phase': 'sync_in_progress'}
    atomic_json(state_path, state)
    try:
        call_script('sync_weread.py', ['--source', str(run_dir / 'weread.json'), '--run-kind', args.run_kind],
                    run_dir / ('sync-' + attempt + '.log'))
        projects = api.read('项目总表')
        # Refresh scopes after sync; immutable source data stays unchanged.
        candidates, rule_pending = event_candidates(previous, data, projects)
        rows = notification_rows(candidates, projects, 'weread:' + data['started_at'])
        atomic_json(run_dir / 'event-plan.json', rows)
        atomic_json(run_dir / 'event-outbox.json', {'mode': runtime['mode'], 'source': summary['source'],
                                                   'events': rows, 'delivery': 'shadow_not_queued' if not runtime['notifications_enabled'] else 'native_pending'})
        created = 0
        if runtime['notifications_enabled']:
            state['phase'] = 'events_in_progress'
            atomic_json(state_path, state)
            created = create_event_rows(api, rows, day)
        summary.update({'candidate_events': len(rows), 'events_created': created,
                        'native_delivery': 'queued_by_event_only; check native workflow receipts' if runtime['notifications_enabled'] else 'shadow_not_queued',
                        'event_readback': 'passed' if runtime['notifications_enabled'] else 'not_applicable_shadow',
                        'local_outbox_events': len(rows), 'finished_at': now().isoformat()})
        state.update({'phase': 'finished', 'summary': summary})
        atomic_json(state_path, state)
        atomic_json(run_dir / 'daily-summary.json', summary)
        return summary
    except BaseException as exc:
        state.update({'phase': 'requires_review', 'error': str(exc), 'stopped_at': now().isoformat()})
        atomic_json(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-kind', choices=['scheduled', 'local'], default='local')
    parser.add_argument('--dry-run', action='store_true', help='Reads and local plans only; no Base mutation')
    parser.add_argument('--shadow', action='store_true', help='Force local event outbox only, even if runtime enables notifications')
    args = parser.parse_args()
    try:
        from .shared_runtime import session
        from .runner_policy import enforce_writer
    except ImportError:
        from shared_runtime import session
        from runner_policy import enforce_writer
    with process_lock(ROOT / 'runs' / '.daily.lock'), session(ROOT, args.dry_run) as shared:
        if shared and shared.get('skipped'):
            print(json.dumps({'date': shanghai_day(), 'status': 'already_running_elsewhere',
                              'shared_runtime': shared, 'process_exit_code': 0}, ensure_ascii=False))
            return 0
        if not args.dry_run:
            enforce_writer(ROOT, verify_commit=True)
        try:
            from .tencent_daily import run as run_tencent
            from .fanqie_daily import run as run_fanqie
            from .fanqie_rising_daily import run as run_fanqie_rising
            from .publisher_recent_daily import run as run_publisher_recent
        except ImportError:
            from tencent_daily import run as run_tencent
            from fanqie_daily import run as run_fanqie
            from fanqie_rising_daily import run as run_fanqie_rising
            from publisher_recent_daily import run as run_publisher_recent
        args.source_dir = None
        args.identity_map = []
        initialization = run_project_initialization(args)
        summary = run_pipeline(args, run_tencent, run, run_fanqie, initialization, run_publisher_recent,
                               run_fanqie_rising)
        summary['modules']['project_notifications'] = run_project_notifications(args, summary)
        summary['modules']['marketing_expense_notifications'] = run_expense_notifications(args, summary)
        summary['modules']['notification_digest'] = run_notification_digest(args, summary)
        summary['notification_delivery'] = run_notification_health(args.shadow)
        summary['process_exit_code'] = pipeline_exit_code(summary)
        summary['operational_status'] = {0:'ok', 1:'failed', 2:'requires_review'}[summary['process_exit_code']]
        summary['source_pipeline_finished_at'] = summary.get('finished_at')
        summary['finished_at'] = now().isoformat()
        if shared is not None:
            summary['shared_runtime'] = shared
        atomic_json(ROOT / 'runs' / summary.get('date', shanghai_day()) /
                    ('pipeline-dry-run-summary.json' if args.dry_run else 'pipeline-summary.json'), summary)
    # The context commits the shared checkpoint and releases the mutex first.
    # Never print a successful run while its cross-device state is uncommitted.
    atomic_json(ROOT / 'runs' / summary.get('date', shanghai_day()) /
                ('pipeline-dry-run-summary.json' if args.dry_run else 'pipeline-summary.json'), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return pipeline_exit_code(summary)


def run_notification_digest(args, summary):
    try:
        from .notification_digest import flush
    except ImportError:
        from notification_digest import flush
    try:
        return flush(args, summary, notification_runtime(args.shadow), ROOT,
                     Base(ROOT / 'receipts' / shanghai_day() / ('notification-digest-' + now().strftime('%H%M%S-%f'))),
                     deliver_event_rows)
    except Exception as exc:
        return {'status': 'requires_review', 'write_count': 0, 'error': str(exc),
                'reason': 'Digest may have unknown outcome; reconcile before retry'}


def run_project_initialization(args):
    try:
        from .initialize_manual_projects import run as initialize, has_unknown_guard
    except ImportError:
        from initialize_manual_projects import run as initialize, has_unknown_guard
    try:
        return initialize(args, root=ROOT)
    except Exception as exc:
        try:
            unknown = has_unknown_guard(ROOT)
        except Exception:
            unknown = True
        return {'status': 'failed', 'error': str(exc), 'identity_mutation_unknown': unknown,
                'reason': 'Do not retry unknown initialization writes; full read-only reconciliation required' if unknown else 'Initialization failed before mutation'}


def run_project_notifications(args, pipeline):
    initialization = pipeline.get('modules', {}).get('project_initialization', {})
    if initialization.get('identity_mutation_unknown'):
        return {'status': 'requires_review', 'registry_committed': False, 'reason': 'Project initialization outcome unknown'}
    try:
        from .manual_project_notifications import run as notify_projects
    except ImportError:
        from manual_project_notifications import run as notify_projects
    try:
        return notify_projects(args, pipeline['modules'].get('tencent', {}), notification_runtime(args.shadow), root=ROOT)
    except Exception as exc:
        return {'status': 'failed', 'registry_committed': False, 'error': str(exc)}


def run_expense_notifications(args, pipeline):
    if pipeline.get('modules', {}).get('project_initialization', {}).get('identity_mutation_unknown'):
        return {'status': 'requires_review', 'registry_committed': False, 'events_created': 0,
                'reason': 'Shared mutation outcome unknown; reconcile before notifications'}
    try:
        from .marketing_expense_notifications import run as notify_expenses
    except ImportError:
        from marketing_expense_notifications import run as notify_expenses
    try:
        return notify_expenses(args, pipeline['modules'].get('tencent', {}), notification_runtime(args.shadow), root=ROOT)
    except Exception as exc:
        return {'status': 'requires_review', 'registry_committed': False,
                'events_created': 0, 'error': str(exc), 'reason': '费用通知结果待核；不自动重试。'}


def run_notification_health(force_shadow=False, observed_at=None, api=None):
    """Audit all event dates after source processing; no external mutations."""
    try:
        from .notification_health import assess, fingerprint, with_changes
    except ImportError:
        from notification_health import assess, fingerprint, with_changes
    observed_at = observed_at or now()
    day = shanghai_day(observed_at)
    state_path = ROOT / 'runs' / 'notification-health.json'
    check_path = ROOT / 'runs' / 'notification-health-last-check.json'
    mode = notification_runtime(force_shadow)['mode']
    previous_check = {}
    if check_path.exists():
        try:
            previous_check = json.loads(check_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass  # A damaged notice cache must not hide a current issue.
    try:
        previous = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if api is None:
            api = Base(ROOT / 'receipts' / day / ('notification-health-' + observed_at.strftime('%H%M%S-%f')))
        rows = api.read('通知事件')  # Includes historical unresolved notifications.
        summary, state = assess(rows, previous, observed_at, mode)
        atomic_json(state_path, state)
    except Exception as exc:
        # Receipt paths contain new attempt IDs; normalize them for quiet replay.
        normalized_error = re.sub(r'\b[0-9a-f]{32}\b', '<receipt>', str(exc))
        normalized_error = re.sub(r'notification-health-\d{6}-\d{6}', 'notification-health-<attempt>', normalized_error)
        summary = {'schema_version': 'notification.health.v1', 'checked_at': observed_at.isoformat(),
                   'status': 'read_error', 'mode': mode, 'read_only': True,
                   'error_type': type(exc).__name__, 'error': str(exc)[:800],
                   'issue_fingerprints': [fingerprint({'type': type(exc).__name__, 'error': normalized_error})],
                   'recipient_delivery_verified': False, 'messages_sent_by_check': 0, 'automatic_retries': 0}
    summary = with_changes(summary, previous_check)
    atomic_json(check_path, {'status': summary['status'], 'checked_at': summary['checked_at'],
                             'issue_fingerprints': summary['issue_fingerprints']})
    atomic_json(ROOT / 'runs' / day / 'notification-health-summary.json', summary)
    return summary


def pipeline_exit_code(summary):
    """Operational failures need scheduler attention; known data gaps remain partial."""
    statuses = {module.get('status') for module in summary.get('modules', {}).values()}
    if summary.get('status') == 'failed' or statuses & {'failed', 'blocked'}:
        return 1
    delivery = summary.get('notification_delivery', {}).get('status')
    if delivery == 'read_error':
        return 1
    if delivery == 'requires_review':
        return 2
    if summary.get('status') == 'source_review_required' or statuses & {'source_review_required', 'requires_review'}:
        return 2
    if any(module.get('coverage', {}).get('status') == 'requires_review'
           for module in summary.get('modules', {}).values()):
        return 2
    return 0


def run_pipeline(args, tencent_runner, weread_runner, fanqie_runner, project_initialization=None, publisher_runner=None,
                 fanqie_rising_runner=None):
    """A source read failure must not cancel the independent platform observations."""
    day=shanghai_day()
    directory=ROOT/'runs'/day
    summary={'date':day,'run_kind':args.run_kind,'modules':{},'status':'partial','started_at':now().isoformat()}
    if project_initialization is not None:
        summary['modules']['project_initialization'] = project_initialization
    shared_identity_unknown=bool((project_initialization or {}).get('identity_mutation_unknown'))
    runners = [('tencent',tencent_runner),('weread',weread_runner),('fanqie',fanqie_runner)]
    if fanqie_rising_runner is not None:
        runners.append(('fanqie_rising', fanqie_rising_runner))
    if publisher_runner is not None:
        runners.append(('publisher_recent', publisher_runner))
    for name,runner in runners:
        if shared_identity_unknown:
            summary['modules'][name]={'status':'blocked','reason':'Shared project identity mutation has unknown outcome; reconcile before dependent writes'}
            continue
        try:
            summary['modules'][name]=runner(args)
            if name=='tencent' and summary['modules'][name].get('source_changes'):
                summary['modules'][name]['status']='source_review_required'
        except Exception as exc:
            summary['modules'][name]={'status':'failed','error':str(exc),'failed_at':now().isoformat()}
            if name=='tencent':
                for path in (ROOT/'runs').glob('*/tencent-state.json'):
                    state=json.loads(path.read_text(encoding='utf-8'))
                    if state.get('phase') in {'apply_in_progress','events_in_progress','requires_review'}:
                        shared_identity_unknown=True
                        break
        if name == 'fanqie_rising':
            # A failed write must stop dependent modules and notification stages
            # in this attempt, not only tomorrow's initialization.
            try:
                from .initialize_manual_projects import unknown_guard_paths
            except ImportError:
                from initialize_manual_projects import unknown_guard_paths
            if unknown_guard_paths(ROOT):
                shared_identity_unknown = True
                summary['modules'].setdefault('project_initialization', {})['identity_mutation_unknown'] = True
    summary['finished_at']=now().isoformat()
    summary['status']='failed' if any(r.get('status') in {'failed','blocked'} for r in summary['modules'].values()) else 'partial'
    summary['process_exit_code']=pipeline_exit_code(summary)
    summary['operational_status']={0:'ok',1:'failed',2:'requires_review'}[summary['process_exit_code']]
    summary['notification_mode']=notification_runtime(args.shadow)['mode']
    current_readback=directory/'acceptance-summary.json'
    if current_readback.exists():
        summary['latest_independent_readback']=json.loads(current_readback.read_text(encoding='utf-8'))
    atomic_json(directory/('pipeline-dry-run-summary.json' if args.dry_run else 'pipeline-summary.json'),summary)
    return summary


if __name__ == '__main__':
    sys.exit(main())
