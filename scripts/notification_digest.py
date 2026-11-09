"""Durable daily category digests; never scan or replay historical outboxes.

Callers hold the daily single-writer lock. Detail producers stage only their
newly authorized events. The coordinator flushes after every source/fee stage.
"""
import json
import re
from pathlib import Path

GROUPS = {
    'board_enter': ('上榜', 'board_enter'),
    'board_exit': ('下榜', 'board_exit'),
    'recommendation_up': ('推荐值90%', 'recommendation_90'),
    'recommendation_down': ('推荐值90%', 'recommendation_90'),
    'new_marketing_service': ('营销', 'marketing'),
    'new_project_entry': ('营销', 'marketing'),
    'marketing_expense_changed': ('营销', 'marketing'),
    'fanqie_milestone': ('番茄阅读里程碑', 'fanqie_milestone'),
}


def configured(root):
    path = Path(root) / 'config/notification-grouping.json'
    return path.exists() and json.loads(path.read_text(encoding='utf-8')).get('enabled') is True


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def helpers():
    try:
        from .daily_run import atomic_json, shanghai_day
        from .notification_policy import enforce_recipients
    except ImportError:
        from daily_run import atomic_json, shanghai_day
        from notification_policy import enforce_recipients
    return atomic_json, shanghai_day, enforce_recipients


def consumed_ids(records):
    result = set()
    for row in records:
        result.add(row['事件唯一键'])
        if str(row.get('规则ID', '')).startswith(('v1:daily_digest:', 'v1:daily_report:')):
            result.update(json.loads(row['判定证据']).get('member_event_ids',[]))
    return result


def stage(api, rows, day, root):
    atomic_json, shanghai_day, enforce_recipients = helpers()
    if day != shanghai_day():
        raise ValueError('Historical events cannot enter the current digest')
    path = Path(root) / 'runs' / day / 'notification-digest-queue.json'
    queue = load(path, {'date': day, 'events': {}})
    delivered = consumed_ids(api.read('通知事件', [['发生日期', '==', f'ExactDate({day})']]))
    for row in rows:
        enforce_recipients(row, root)
        if row['事件类型'] not in GROUPS or str(row['发生日期'])[:10] != day:
            raise ValueError('Unsupported or wrong-date event in digest')
        key = row['事件唯一键']
        if key in delivered:
            continue
        if key in queue['events'] and queue['events'][key] != row:
            raise ValueError('Staged event changed; review before retry')
        queue['events'][key] = row
    atomic_json(path, queue)
    # No native workflow record has been created at this stage.
    return 0


def build(rows, day):
    groups = {}
    for row in sorted(rows, key=lambda r: (r['通知标题'], r['事件唯一键'])):
        label, slug = GROUPS[row['事件类型']]
        groups.setdefault((label, slug), []).append(row)
    output = []
    for (label, slug), members in groups.items():
        recipients = members[0]['收件人']
        if any(r['收件人'] != recipients for r in members):
            raise ValueError('Mixed recipients in digest')
        body = f'{day}｜{label}汇总｜共{len(members)}项\n\n' + '\n\n'.join(
            f'{i}. {display_detail(r)}' for i, r in enumerate(members, 1))
        # Do not silently truncate events or split a requested single message.
        if len(body.encode('utf-8')) > 24000:
            raise ValueError('Digest exceeds safe message size; review formatting without splitting or dropping events')
        links = {p['id']: p for r in members for p in r['关联项目']}
        output.append({'事件唯一键': f'digest:v1:{day}:{slug}', '事件类型': label + '汇总',
            '关联项目': [links[k] for k in sorted(links)], '发生日期': day + ' 00:00',
            '规则ID': 'v1:daily_digest:' + slug,
            '判定证据': json.dumps({'member_event_ids': sorted(r['事件唯一键'] for r in members),
                'events': members}, ensure_ascii=False, sort_keys=True),
            '通知标题': f'{label}汇总｜{day}｜{len(members)}项', '通知正文': body,
            '收件人': recipients, '通知状态': ['待发送'], '采集批次': 'daily-digest:' + day})
    return output


def display_detail(row):
    try:
        from .presentation import clean_title
    except ImportError:
        from presentation import clean_title
    text = row['通知正文']
    if row['事件类型'] in {'board_enter', 'board_exit', 'recommendation_up', 'recommendation_down'}:
        title, separator, rest = text.partition('｜')
        if separator:
            text = '《' + clean_title(title) + '》｜' + rest
        text = text.replace('前次名次：None', '前次：未在榜').replace('本次名次：None', '本次：已下榜')
    # Workflow is plain_text: expose the actual URL without Markdown noise.
    return re.sub(r'\[[^\]\n]*\]\((https?://[^\s)]+)\)', r'\1', text)


def flush(args, pipeline, runtime, root, api, writer):
    atomic_json, shanghai_day, enforce_recipients = helpers()
    root = Path(root)
    day = pipeline['date']
    if not configured(root):
        return {'status': 'disabled', 'write_count': 0}
    if day != shanghai_day():
        raise ValueError('No historical digest replay')
    # Pending prior-day queues are evidence requiring manual reconciliation.
    for old in (root / 'runs').glob('*/notification-digest-queue.json'):
        if old.parent.name == day:
            continue
        old_queue = load(old, {})
        state = load(old.with_name('notification-digest.apply-state.json'), {})
        if set(old_queue.get('events', {})) - set(state.get('consumed_ids', [])):
            return {'status': 'requires_review', 'write_count': 0, 'reason': 'Unsent prior-day digest; no automatic replay'}
    try:
        from .initialize_manual_projects import unknown_guard_paths
    except ImportError:
        from initialize_manual_projects import unknown_guard_paths
    if unknown_guard_paths(root):
        return {'status': 'requires_review', 'write_count': 0, 'reason': 'Unknown mutation; full readback required before digest retry'}
    if any(m.get('status') in {'failed', 'blocked', 'requires_review', 'source_review_required'}
           or m.get('coverage', {}).get('status') == 'requires_review' for m in pipeline['modules'].values()):
        return {'status': 'requires_review', 'write_count': 0, 'reason': 'Await complete source processing and reviewed expenses before sealing daily groups'}
    queue = load(root / 'runs' / day / 'notification-digest-queue.json', {'events': {}})
    records = api.read('通知事件', [['发生日期', '==', f'ExactDate({day})']])
    consumed = consumed_ids(records)
    pending = [r for k, r in queue['events'].items() if k not in consumed]
    groups = build(pending, day)
    existing = {r['事件唯一键'] for r in records}
    if any(r['事件唯一键'] in existing for r in groups):
        return {'status': 'requires_review', 'write_count': 0, 'reason': 'Additional events after daily category was sent; retain queue, never send a second category message'}
    for row in groups:
        enforce_recipients(row, root)
    preview = root / 'runs' / day / 'notification-digest-preview.json'
    atomic_json(preview, {'date': day, 'events': groups, 'detail_count': len(pending)})
    if args.dry_run or not runtime['notifications_enabled']:
        return {'status': 'dry_run' if args.dry_run else 'shadow', 'write_count': 0,
                'planned_messages': len(groups), 'detail_count': len(pending)}
    guard = root / 'runs' / day / 'notification-digest.apply-state.json'
    if groups:
        atomic_json(guard, {'phase': 'events_in_progress', 'planned_groups': groups,
                            'consumed_ids': sorted(consumed & set(queue['events']))})
        count = writer(api, groups, day)
    else:
        count = 0
    atomic_json(guard, {'phase': 'finished', 'consumed_ids': sorted(queue['events']),
                        'messages_created_this_run': count})
    return {'status': 'completed', 'write_count': count, 'messages_created': count,
            'detail_count': len(pending), 'readback': 'passed', 'direct_im_sent': 0}
