"""Notify reviewed expense revisions; new services share Tencent's existing event.

An initial complete read establishes a silent baseline. A failed/unreviewed
Tencent run never advances it. Unknown event writes stop for reconciliation.
"""
import json
from pathlib import Path
try:
    from .daily_run import Base, ROOT, notification_recipients, atomic_json, now, shanghai_day, create_event_rows
    from .feishu_io import same
    from .sync_marketing import DOC_KEY, sha
    from .marketing_notice_text import number, service_content
    from .service_scope import enforce_write
    from .local_settings import project_path
except ImportError:
    from daily_run import Base, ROOT, notification_recipients, atomic_json, now, shanghai_day, create_event_rows
    from feishu_io import same
    from sync_marketing import DOC_KEY, sha
    from marketing_notice_text import number, service_content
    from service_scope import enforce_write
    from local_settings import project_path

SCHEMA = 'marketing.expense-registry.v1'


def build_plan(services, projects, previous, day):
    if previous is not None and (previous.get('schema_version') != SCHEMA or not isinstance(previous.get('services'), dict)):
        raise ValueError('Unknown expense registry; never silently rebuild historical baseline')
    if previous and previous['last_observed_date'] > day:
        raise ValueError('Expense observation is older than retained registry')
    project_index = {p['作品唯一键']: p for p in projects if p.get('作品唯一键')}
    prior = previous['services'] if previous else {}
    next_services = dict(prior)
    events, seen = [], set()
    new_count = 0
    for row in services:
        if not str(row.get('原始行唯一键', '')).startswith('tencent:' + DOC_KEY + ':'):
            continue
        enforce_write('营销服务明细', row)
        rid = row.get('record_id')
        if not rid or rid in seen:
            raise ValueError('Missing or duplicate service record ID')
        seen.add(rid)
        project = project_index.get(row.get('作品唯一键'))
        if not project or {x['id'] for x in row.get('关联项目', [])} != {project['record_id']}:
            raise ValueError('Expense project link has not been verified')
        current = {'record_id': rid, 'source_service_key': row['服务明细唯一键'],
                   'source_fingerprint': row.get('源行指纹'), 'project_key': row['作品唯一键'],
                   'title': project.get('书名') or row.get('原始书名'), 'service_date': str(row.get('服务日期') or '')[:10],
                   'review_count': number(row.get('邀评人数')), 'raw_fee': number(row.get('原始行总金额')),
                   'platform': row.get('服务平台'), 'service': row.get('服务内容'),
                   'statistics_eligible': row.get('计入统计'), 'archive_url': row.get('服务留档链接'),
                   'source_url': row.get('原始文档链接')}
        old = prior.get(rid)
        revision = old.get('revision', 0) if old else 0
        if old and old['project_key'] != current['project_key']:
            raise ValueError('Service identity changed; controlled identity reconciliation required')
        if old and any(old.get(k) != current[k] for k in ('review_count', 'raw_fee')):
            revision += 1
            events.append({**current, 'previous': old, 'kind': 'marketing_expense_changed',
                           'observed_date': day, 'revision': revision,
                           'event_id': sha(['marketing.expense_changed', 'v1', rid, revision, old, current])})
        elif old is None and previous is not None:
            # Tencent already creates the readable new-service/expense event.
            new_count += 1
        next_services[rid] = {**current, 'revision': revision}
    return {'baseline': previous is None, 'events': events,
            'current_service_count': len(seen), 'new_services_handled_by_tencent': new_count,
            'next_registry': {'schema_version': SCHEMA, 'last_observed_date': day, 'services': next_services}}


def notification_rows(plan, projects, day):
    index = {p['作品唯一键']: p for p in projects if p.get('作品唯一键')}
    return [{'事件唯一键': event['event_id'], '事件类型': event['kind'],
             '关联项目': [{'id': index[event['project_key']]['record_id']}], '发生日期': day + ' 00:00',
             '规则ID': 'v1:marketing.expense_changed', '判定证据': json.dumps(event, ensure_ascii=False, sort_keys=True),
             '通知标题': '营销费用变更｜' + str(event['title']), '通知正文': service_content(event),
             '收件人': [{'id': rid} for rid in notification_recipients()], '通知状态': ['待发送'],
             '采集批次': 'marketing-expense:' + day} for event in plan['events']]


def validate_current_source(plan, services):
    if not plan.get('safe_to_apply') or sha(project_path(plan['source_file'], ROOT).read_bytes()) != plan['source_sha256']:
        raise ValueError('Expense notices require a reviewed, unchanged source plan')
    indexed = {s['服务明细唯一键']: s for s in services}
    if len(indexed) != len(services):
        raise ValueError('Duplicate expense service key')
    expected_keys = {s['fields']['服务明细唯一键'] for s in plan['services']}
    actual_keys = {s['服务明细唯一键'] for s in services
                   if str(s.get('原始行唯一键', '')).startswith('tencent:' + DOC_KEY + ':')}
    if expected_keys != actual_keys:
        raise ValueError('Current services and reviewed Tencent source do not agree')
    for service in plan['services']:
        actual = indexed[service['fields']['服务明细唯一键']]
        if any(not same(value, actual.get(field)) for field, value in service['fields'].items()):
            raise ValueError('Source revision has not been fully reconciled: ' + service['source_coordinate_key'])


def run(args, tencent_summary, runtime, root=ROOT, api=None, day=None, event_writer=None):
    day = day or shanghai_day()
    state_path = root / 'runs' / 'marketing-expense-notification-state.json'
    if state_path.exists() and json.loads(state_path.read_text(encoding='utf-8')).get('phase') != 'finished':
        raise RuntimeError('Expense notification outcome unknown; reconcile receipts before retry')
    if tencent_summary.get('status') not in {'completed', 'partial'} or tencent_summary.get('source_changes'):
        return {'status': 'requires_review', 'reason': '腾讯来源尚未完整核对；保留费用比较基线，不发送或重复归类新交易。',
                'registry_committed': False, 'events_created': 0}
    config = root / 'config/marketing-expense-notifications.json'
    if not config.exists() or json.loads(config.read_text(encoding='utf-8')).get('enabled') is not True:
        return {'status': 'disabled', 'events_created': 0, 'registry_committed': False}
    api = api or Base(root / 'receipts' / day / ('marketing-expense-' + now().strftime('%H%M%S-%f')))
    services, projects = api.read('营销服务明细'), api.read('项目总表')
    source_path = root / 'runs' / day / ('tencent-dry-run-plan.json' if args.dry_run else 'tencent-daily-import-plan.json')
    try:
        validate_current_source(json.loads(source_path.read_text(encoding='utf-8')), services)
    except (ValueError, OSError) as exc:
        return {'status': 'requires_review', 'reason': str(exc), 'events_created': 0, 'registry_committed': False}
    registry_path = root / 'runs/marketing-expense-registry.json'
    previous = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else None
    plan = build_plan(services, projects, previous, day)
    rows = notification_rows(plan, projects, day)
    attempt = now().strftime('%H%M%S-%f')
    outbox = root / 'runs' / day / ('marketing-expense-dry-run.json' if args.dry_run else 'marketing-expense-outbox-' + attempt + '.json')
    atomic_json(outbox, {'mode': runtime['mode'], 'plan': plan, 'event_rows': rows})
    summary = {'status': 'completed', 'baseline': plan['baseline'], 'current_services': plan['current_service_count'],
               'new_services_handled_by_tencent': plan['new_services_handled_by_tencent'],
               'candidate_events': len(rows), 'events_created': 0, 'registry_committed': False,
               'notification_mode': runtime['mode'], 'outbox': str(outbox), 'direct_im_sent': 0}
    enabled = runtime.get('notifications_enabled') is True and not getattr(args, 'shadow', False)
    if args.dry_run:
        summary['external_mutations'] = 0
        return summary
    atomic_json(state_path, {'phase': 'events_in_progress', 'outbox': str(outbox), 'summary': summary})
    try:
        if enabled and rows:
            summary['events_created'] = (event_writer or create_event_rows)(api, rows, day)
        atomic_json(registry_path, plan['next_registry'])
        summary.update(registry_committed=True, native_delivery='native_event_only' if enabled else 'shadow_not_queued')
        atomic_json(state_path, {'phase': 'finished', 'summary': summary})
        return summary
    except BaseException as exc:
        atomic_json(state_path, {'phase': 'requires_review', 'outbox': str(outbox), 'error': str(exc)})
        raise
