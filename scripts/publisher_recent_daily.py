"""Daily recent-list cohorts and metrics. Caller holds the shared daily lock.

Only complete sources advance ID registries. Base writes precede registry commit;
an interrupted mutation blocks retries across all days and modules.
"""
import argparse
import json
from pathlib import Path

try:
    from .daily_run import ROOT, TZ, now, atomic_json, process_lock
    from .collect_weread_recent import collect, load_session
    from .recent_entry_tracking import observe
    from .collect_weread import collect_detail, finalize_today_reading, MAPPING
    from .feishu_io import Base, save
    from .sync_weread import sync
    from .initialize_manual_projects import unknown_guard_paths
except ImportError:
    from daily_run import ROOT, TZ, now, atomic_json, process_lock
    from collect_weread_recent import collect, load_session
    from recent_entry_tracking import observe
    from collect_weread import collect_detail, finalize_today_reading, MAPPING
    from feishu_io import Base, save
    from sync_weread import sync
    from initialize_manual_projects import unknown_guard_paths


DATE_FIELDS = [
    {'name': '首次进入最近上架日', 'type': 'datetime', 'style': {'format': 'yyyy/MM/dd'},
     'description': '完整初始基线之后首次观察到新作品ID的实际日期；非平台精确上架时间。'},
    {'name': '版权方追踪截止日', 'type': 'datetime', 'style': {'format': 'yyyy/MM/dd'},
     'description': '首次进入最近上架日加29天；离开列表继续，连载更新和重入不重置。'},
]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def ensure_schema(api, root):
    fields = api.call('+field-list', '项目总表')['data']['fields']
    existing = {f['name']: f for f in fields}
    for definition in DATE_FIELDS:
        if definition['name'] in existing and existing[definition['name']]['type'] != definition['type']:
            raise ValueError('Recent tracking field type conflict')
    missing = [f for f in DATE_FIELDS if f['name'] not in existing]
    if missing:
        api.call('+field-create', '项目总表', missing)
        fields = api.call('+field-list', '项目总表')['data']['fields']
    if not {f['name'] for f in DATE_FIELDS} <= {f['name'] for f in fields}:
        raise RuntimeError('Recent tracking field readback failed')
    api.manifest['tables']['项目总表']['field_readback']['fields'] = fields
    api.manifest['tables']['项目总表']['fields'] = {f['name']: f for f in fields}
    save(root/'config/base-manifest.json', api.manifest)
    api._ensure_display_options('项目总表', [{'监测范围': ['版权方上新']}])
    # A view of the existing project table, including overlaps with marketing/rank samples.
    views = api.call('+view-list', '项目总表')['data']['views']
    found = [v for v in views if v['name'] == '版权方新进入作品']
    if not found:
        api.call('+view-create', '项目总表', {'name': '版权方新进入作品', 'type': 'grid'})
        found = [v for v in api.call('+view-list', '项目总表')['data']['views'] if v['name'] == '版权方新进入作品']
    if len(found) != 1:
        raise RuntimeError('Recent project view requires reconciliation')
    view = found[0]['id']
    visible = ['书名', '作者', '版权方', '加入趋势对比', '首次进入最近上架日', '版权方追踪截止日',
               '最新推荐值（微信）', '最新点评人数（微信）', '最新今日阅读人数（微信）', '每日记录', '采集状态', '作品主页']
    filtering = {'logic': 'and', 'conditions': [['平台', 'intersects', ['微信读书']],
                  ['首次进入最近上架日', 'non_empty']]}
    old_fields = api.call('+view-get-visible-fields', '项目总表', view_id=view)['data']['visible_fields']
    if old_fields != visible:
        api.call('+view-set-visible-fields', '项目总表', {'visible_fields': visible}, view_id=view)
    old_filter = api.call('+view-get-filter', '项目总表', view_id=view)['data']['filter']
    if old_filter != filtering:
        api.call('+view-set-filter', '项目总表', filtering, view_id=view)
    if api.call('+view-get-visible-fields', '项目总表', view_id=view)['data']['visible_fields'] != visible:
        raise RuntimeError('Recent view fields readback mismatch')
    actual_filter = api.call('+view-get-filter', '项目总表', view_id=view)['data']['filter']
    try:
        from .configure_readable_views import equivalent_filter
    except ImportError:
        from configure_readable_views import equivalent_filter
    if not equivalent_filter(actual_filter, filtering):
        raise RuntimeError('Recent view filter readback mismatch')
    save(root/'config/publisher-recent-view.json', {'view_id': view, 'filter': actual_filter, 'visible_fields': visible})


def collect_metrics(targets, directory, started):
    raw = directory/'raw-details'
    raw.mkdir(parents=True, exist_ok=True)
    books, errors = [], []
    for target in targets:
        book = collect_detail((target['book_id'], target['reader_url'], raw))
        if book.get('status') != 'ok':
            errors.append(book)
            continue
        book.update(memberships=[], today_reading_observations=[])
        finalize_today_reading(book, raw)
        books.append(book)
    return {'schema_version': 'weread.public.v1', 'started_at': started,
            'finished_at': now().isoformat(), 'collection_date': started[:10],
            'field_mapping': MAPPING, 'charts': [], 'books': books, 'detail_errors': errors,
            'gaps': ['版权方最近上架新进入作品追踪；非Top50榜单样本。',
                     '累计阅读人数定义未核实，留空；今日阅读不累加为累计读者。']}


def run(args, root=ROOT, api=None, clock=now):
    config_path = root/'config/publisher-newbooks.json'
    if not config_path.exists() or not read(config_path).get('enabled'):
        return {'status': 'disabled', 'base_writes': 0}
    config = read(config_path)
    stamp = clock().astimezone(TZ)
    day = stamp.date().isoformat()
    state_path = root/'runs'/day/'publisher-recent-state.json'
    # Every attempt respects other modules' unresolved identity/transaction guards.
    guards = unknown_guard_paths(root)
    for name in ('tencent-state.json', 'daily-state.json', 'fanqie-state.json'):
        for path in (root/'runs').glob('*/'+name):
            if read(path).get('phase') in ('apply_in_progress', 'sync_in_progress', 'events_in_progress', 'requires_review'):
                guards.append(str(path))
    if guards:
        return {'status': 'requires_review', 'base_writes': 0, 'guards': guards}
    if state_path.exists():
        prior = read(state_path)['summary']
        if prior.get('status') == 'completed':
            return {**prior, 'reused_finished': True, 'base_writes': 0}
    registry_path = root/config['entry_tracking']['registry']
    # This operational integration never silently recreates an absent baseline.
    if not registry_path.exists():
        raise ValueError('Recent registry missing; restore verified baseline before running')
    previous = read(registry_path)
    providers = read(root/config['providers_file'])
    directory = root/'runs'/day/('publisher-recent-'+stamp.strftime('%H%M%S-%f'))
    directory.mkdir(parents=True)
    try:
        session = load_session(root/config['session_file'])
        source_summary = collect(providers, session, directory/'lists')
        observations = [(p, read(directory/'lists'/f'provider-{i}.json')) for i,p in enumerate(providers)]
        if any(s.get('source', {}).get('http_status') == 401 for _, s in observations):
            try:
                from .weread_session import renew
            except ImportError:
                from weread_session import renew
            try:
                session = renew(root/config['session_file'])
                source_summary = collect(providers, session, directory/'lists-renewed')
                observations = [(p, read(directory/'lists-renewed'/f'provider-{i}.json')) for i,p in enumerate(providers)]
            except Exception as exc:
                source_summary['renewal_error_type'] = type(exc).__name__
    except Exception as exc:
        # Auth failures do not prevent public sampling of previously registered targets.
        source_summary = {'complete': False, 'error_type': type(exc).__name__}
        observations = []
    proposed, report = observe(previous, observations, day)
    targets = report['active_targets']
    data = collect_metrics(targets, directory, stamp.isoformat())
    if data['finished_at'][:10] != day:
        raise RuntimeError('Collection crossed midnight; preserve evidence and rerun on actual day')
    source = directory/'metrics.json'
    atomic_json(source, data)
    atomic_json(directory/'registry-proposed.json', proposed)
    atomic_json(directory/'comparison.json', report)
    summary = {'date': day, 'status': 'completed' if source_summary['complete'] and not data['detail_errors'] else 'failed',
               'source': source_summary, 'new_entries': sum(len(x['new_entries']) for x in report['changes']),
               'active_targets': len(targets), 'sampled_books': len(data['books']), 'metric_errors': len(data['detail_errors']),
               'cumulative_readers': 'unverified_semantics_left_blank', 'business_notifications_sent': 0,
               'evidence': str(directory), 'base_writes': 0, 'registry_committed': False}
    if args.dry_run:
        return {**summary, 'dry_run': True}
    api = api or Base(root/'receipts'/day/('publisher-recent-'+stamp.strftime('%H%M%S-%f')))
    # All mutations, including schema changes, are protected before dispatch.
    atomic_json(state_path, {'phase': 'apply_in_progress', 'date': day, 'evidence': str(directory)})
    ensure_schema(api, root)
    result = sync(data, source, args.run_kind, api=api,
                  recent_tracks={t['book_id']: t for t in targets})
    atomic_json(registry_path, proposed)
    summary.update(base_writes=api.changes, sync=result, registry_committed=True, readback='passed')
    atomic_json(state_path, {'phase': 'finished', 'date': day, 'summary': summary})
    atomic_json(root/'runs'/day/'publisher-recent-summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--run-kind', default='local', choices=['local', 'scheduled'])
    args = parser.parse_args()
    with process_lock(ROOT/'runs/.daily.lock'):
        result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['status'] in ('failed', 'requires_review') else 0


if __name__ == '__main__':
    raise SystemExit(main())
