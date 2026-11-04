"""Plan notifications for new explicit marketing records in 项目总表.

Pure planners do not mutate input. The daily runner writes only notification
events when live is enabled, commits its registry after event readback, and
retains a guard after unknown writes. It never edits project fields. The
standalone CLI remains read-only and writes only a local reviewable plan.
"""
import argparse
from datetime import date
import json
from pathlib import Path

try:
    from .daily_run import Base, ROOT, RECIPIENTS, atomic_json, options, shanghai_day, now, create_event_rows
    from .sync_marketing import sha
except ImportError:
    from daily_run import Base, ROOT, RECIPIENTS, atomic_json, options, shanghai_day, now, create_event_rows
    from sync_marketing import sha

SCHEMA = 'project-new-record-registry.v1'


def project_index(rows):
    result = {}
    for row in rows:
        rid = row.get('record_id')
        if not isinstance(rid, str) or not rid or rid in result:
            raise ValueError('Missing or duplicate project record ID')
        result[rid] = row
    return result


def covered_by_tencent(events, projects):
    """Resolve exact project keys/record links from same-day source outbox."""
    index = project_index(projects)
    by_key = {}
    for p in projects:
        key = p.get('作品唯一键')
        if key:
            if key in by_key:
                raise ValueError('Duplicate work key while resolving source-event dedup')
            by_key[key] = p['record_id']
    covered = set()
    for event in events:
        key = event.get('project_key')
        if key:
            if key not in by_key:
                raise ValueError('Source new-service event has no current project record')
            covered.add(by_key[key])
        for linked in event.get('关联项目', []):
            rid = linked.get('id') if isinstance(linked, dict) else linked
            if rid not in index:
                raise ValueError('Source event refers to an absent project record')
            covered.add(rid)
    return covered


def build_plan(projects, previous, day, tencent_project_ids=()):
    date.fromisoformat(day)
    index = project_index(projects)
    baseline = previous is None
    if not baseline:
        if previous.get('schema_version') != SCHEMA or not isinstance(previous.get('seen_record_ids'), list):
            raise ValueError('Unrecognized existing project registry; do not silently rebaseline')
        if date.fromisoformat(previous['last_observed_date']) > date.fromisoformat(day):
            raise ValueError('Registry observation date is in the future')
    seen = set() if baseline else set(previous['seen_record_ids'])
    if not baseline and (len(seen) != len(previous['seen_record_ids']) or any(not isinstance(r, str) or not r for r in seen)):
        raise ValueError('Invalid seen record IDs in registry')
    covered = set(tencent_project_ids)
    if covered - set(index):
        raise ValueError('Source dedup IDs are absent from current full project read')
    events, exclusions, deferred = [], [], set()
    for rid, p in sorted(index.items()):
        scope = options(p.get('监测范围'))
        platforms = options(p.get('平台'))
        if scope == {'营销项目'} and (p.get('作品形式') != '电子书'
                or len(platforms) != 1 or not platforms <= {'微信读书', '番茄小说'}):
            exclusions.append({'record_id': rid, 'reason': 'ebook_medium_and_single_platform_required'})
            # Do not consume a new incomplete row: notify once after it is completed.
            deferred.add(rid)
            continue
        if baseline or rid in seen:
            continue
        if '营销项目' not in scope or scope & {'仅榜单样本', '别名留档'}:
            exclusions.append({'record_id': rid, 'reason': 'not_explicit_unambiguous_marketing_scope'})
            continue
        if rid in covered:
            exclusions.append({'record_id': rid, 'reason': 'covered_by_same_day_tencent_new_service'})
            continue
        events.append({'event_id': sha(['project_table.new_record', 'v1', rid]),
            'kind': 'new_project_entry', 'rule_version': '1', 'record_id': rid,
            'source_kind': 'project_table_new_record', 'project_key': p.get('作品唯一键'),
            'title': p.get('书名') or '未填书名的营销项目', 'observed_date': day,
            'platform': sorted(options(p.get('平台'))), 'source_url': p.get('作品主页'),
            'evidence': '首次在每日完整项目回读中发现此 record_id，且监测范围明确为营销项目；同日腾讯新增服务未覆盖。'})
    return {'schema_version': 'project-new-record-plan.v1', 'date': day, 'baseline': baseline,
            'mode': 'plan_only', 'messages_sent': 0, 'project_mutations': 0,
            'events': events, 'exclusions': exclusions,
            'next_registry': {'schema_version': SCHEMA,
                'baseline_date': day if baseline else previous['baseline_date'],
                'last_observed_date': day, 'seen_record_ids': sorted(seen | (set(index) - deferred))}}


def notification_rows(plan, batch, recipients=RECIPIENTS):
    rows = []
    for event in plan['events']:
        title = '项目新增记录｜' + event['title']
        text = (title + '\n平台：' + ('、'.join(event['platform']) or '未填写')
                + '\n首次发现日期：' + event['observed_date']
                + '\n范围：营销项目\n此提醒只表示项目总表新增记录，不表示平台身份或指标已核实。')
        if event.get('source_url'):
            text += '\n作品：' + str(event['source_url'])
        rows.append({'事件唯一键': event['event_id'], '事件类型': event['kind'],
            '关联项目': [{'id': event['record_id']}], '发生日期': event['observed_date'] + ' 00:00',
            '规则ID': 'v1:project_table.new_record', '判定证据': json.dumps(event, ensure_ascii=False, sort_keys=True),
            '通知标题': title, '通知正文': text, '收件人': [{'id': rid} for rid in recipients],
            '通知状态': ['待发送'], '采集批次': batch})
    return rows


def run(args, tencent_summary, runtime, root=ROOT, api=None, day=None, event_writer=None):
    """Commit registry only after durable outbox and successful event readback."""
    day = day or shanghai_day()
    state_path = root / 'runs' / 'manual-project-events-state.json'
    if state_path.exists() and json.loads(state_path.read_text(encoding='utf-8')).get('phase') != 'finished':
        raise RuntimeError('New-project notification outcome unknown; reconcile before retry')
    if tencent_summary.get('status') in ('failed', 'blocked', 'source_review_required'):
        return {'status': 'requires_review', 'reason': 'Tencent outcome unavailable; preserve registry to avoid source-event duplication',
                'registry_committed': False, 'messages_sent': 0}
    source_path = root / 'runs' / day / ('tencent-dry-run-outbox.json' if args.dry_run else 'tencent-outbox.json')
    if not source_path.exists():
        raise RuntimeError('Current Tencent outbox absent; cannot establish notification dedup')
    source = json.loads(source_path.read_text(encoding='utf-8'))
    registry_path = root / 'runs' / 'manual-project-registry.json'
    previous = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else None
    api = api or Base(root / 'receipts' / day / ('manual-project-events-' + now().strftime('%H%M%S-%f')))
    projects = api.read('项目总表')
    plan = build_plan(projects, previous, day, covered_by_tencent(source['events'], projects))
    # A newly initialized Tencent history must never be reclassified as manual
    # project additions, even if a previous manual registry already exists.
    if source.get('baseline') is True:
        plan['exclusions'] += [{'record_id': e['record_id'], 'reason': 'tencent_initial_history_baseline'} for e in plan['events']]
        plan['events'] = []
    rows = notification_rows(plan, 'project-table:' + day)
    enabled = runtime.get('notifications_enabled') is True and runtime.get('mode') == 'live' and not args.shadow
    summary = {'status': 'completed', 'date': day, 'baseline': plan['baseline'],
               'source_baseline': source.get('baseline') is True, 'mode': 'live' if enabled else 'shadow',
               'candidate_events': len(rows), 'events_created': 0, 'registry_committed': False,
               'direct_im_sent': 0, 'dry_run': args.dry_run}
    outbox = {'mode': summary['mode'], 'events': rows, 'plan': plan,
              'delivery': 'planned_only' if args.dry_run else 'native_pending' if enabled else 'shadow_not_queued'}
    atomic_json(root / 'runs' / day / ('manual-project-dry-run-outbox.json' if args.dry_run else 'manual-project-outbox.json'), outbox)
    if args.dry_run:
        return summary
    if enabled:
        atomic_json(state_path, {'phase': 'events_in_progress', 'date': day, 'events': rows,
                                 'next_registry': plan['next_registry']})
        summary['events_created'] = (event_writer or create_event_rows)(api, rows, day)
        summary['event_readback'] = 'passed'
    # Shadow mode has no remote mutation. Its complete project read plus durable
    # local outbox still advances the baseline, without queuing old events later.
    atomic_json(registry_path, plan['next_registry'])
    summary['registry_committed'] = True
    summary['native_delivery'] = 'queued_by_event_only' if enabled else 'shadow_not_queued'
    atomic_json(state_path, {'phase': 'finished', 'date': day, 'summary': summary})
    return summary


def main():
    parser = argparse.ArgumentParser(description='Read-only new-project notification plan; never sends or advances registry')
    parser.add_argument('--registry', type=Path, default=ROOT / 'runs' / 'manual-project-registry.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    day = shanghai_day()
    previous = json.loads(args.registry.read_text(encoding='utf-8')) if args.registry.exists() else None
    api = Base(ROOT / 'receipts' / day / 'manual-project-notification-plan')
    projects = api.read('项目总表')
    source_path = ROOT / 'runs' / day / 'tencent-outbox.json'
    source = json.loads(source_path.read_text(encoding='utf-8')) if source_path.exists() else {'events': []}
    plan = build_plan(projects, previous, day, covered_by_tencent(source['events'], projects))
    plan['event_rows'] = notification_rows(plan, 'project-table:' + day)
    output = args.output or ROOT / 'runs' / day / 'manual-project-notification-plan.json'
    atomic_json(output, plan)
    print(json.dumps({'read_only': True, 'baseline': plan['baseline'], 'candidate_events': len(plan['events']),
                      'registry_advanced': False, 'messages_sent': 0, 'plan_path': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
