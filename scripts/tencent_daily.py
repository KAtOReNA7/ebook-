"""Daily frozen-source import and new-service event outbox.

Initial history establishes a baseline. Only genuinely new source service keys
produce events; modifications/deletions stop for review. No direct IM calls.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path

try:
    from .daily_run import (ROOT, Base, atomic_json, process_lock, shanghai_day, now,
                            notification_runtime, create_event_rows, notification_recipients, options, call_script)
    from .sync_marketing import plan_import, apply_plan, identities_from_file, sha, DOC_KEY
    from .prepare_tencent_rows import build
    from .marketing_notice_text import service_content
    from .local_settings import project_path
except ImportError:
    from daily_run import (ROOT, Base, atomic_json, process_lock, shanghai_day, now,
                           notification_runtime, create_event_rows, notification_recipients, options, call_script)
    from sync_marketing import plan_import, apply_plan, identities_from_file, sha, DOC_KEY
    from prepare_tencent_rows import build
    from marketing_notice_text import service_content
    from local_settings import project_path


def frozen_source(directory):
    directory = Path(directory)
    manifest_path = directory / 'manifest.json'
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if manifest.get('status') != 'packages_collected':
        raise RuntimeError('Incomplete source package retained; reconcile or collect into a new directory')
    # Check stored raw-package checksums before trusting a frozen source.
    for item in manifest['pages'] + manifest.get('independent_reads', []):
        if sha((directory / item['file']).read_bytes()) != item['sha256']:
            raise RuntimeError('Frozen source evidence checksum mismatch: ' + item['file'])
    return True


def new_entry_candidates(plan, previous_services, previous_projects):
    previous = {row['服务明细唯一键'] for row in previous_services}
    source_previous = [row for row in previous_services
                       if str(row.get('原始行唯一键', '')).startswith('tencent:' + DOC_KEY + ':')]
    baseline = not source_previous
    projects = {row['作品唯一键']: row for row in previous_projects if row.get('作品唯一键')}
    events = []
    if baseline or not plan['safe_to_apply']:
        return events, baseline
    for service in plan['services']:
        fields = service['fields']
        if fields['服务明细唯一键'] in previous:
            continue
        old_project = projects.get(service['project_key'])
        transition = ('首次营销作品' if not old_project else
                      '榜单样本转为营销项目' if '营销项目' not in options(old_project.get('监测范围')) else '同书新增服务')
        events.append({'event_id': sha(['marketing.new_service', 'v1', fields['服务明细唯一键']]),
                       'kind': 'new_project_entry', 'rule_version': '1', 'project_key': service['project_key'],
                       'source_coordinate_key': service['source_coordinate_key'], 'transition': transition,
                       'observed_date': plan['import_day'], 'source_service_key': fields['服务明细唯一键'],
                       'source_fingerprint': fields['源行指纹'], 'source_title': fields['原始书名'],
                       'platform': fields['服务平台'], 'service_medium': fields['服务对象'],
                       'service': fields['服务内容'], 'service_date': fields.get('服务日期'),
                       'review_count': fields.get('邀评人数'), 'archive_url': fields.get('服务留档链接'),
                       'raw_fee': fields.get('原始行总金额'), 'statistics_eligible': fields['计入统计'],
                       'source_url': fields['原始文档链接']})
    return events, baseline


def event_rows(events, projects, batch):
    index = {row['作品唯一键']: row for row in projects if row.get('作品唯一键')}
    rows = []
    for event in events:
        project = index.get(event['project_key'])
        if not project or not project.get('record_id'):
            raise ValueError('New-entry event has no verified project record link')
        title = '新增营销费用｜' + str(project.get('书名') or event['source_title'])
        content = service_content(event)
        rows.append({'事件唯一键': event['event_id'], '事件类型': event['kind'],
                     '关联项目': [{'id': project['record_id']}], '发生日期': event['observed_date'] + ' 00:00',
                     '规则ID': 'v1:marketing.new_service', '判定证据': json.dumps(event, ensure_ascii=False, sort_keys=True),
                     '通知标题': title, '通知正文': content, '收件人': [{'id': value} for value in notification_recipients()],
                     '通知状态': ['待发送'], '采集批次': batch})
    return rows


def configured_identities(extra_paths):
    config_path = ROOT / 'config' / 'tencent-runtime.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    paths = list(extra_paths or [])
    for item in config.get('identity_maps', []):
        paths.append(project_path(item, ROOT))
    return identities_from_file(paths)


def run(args):
    day = shanghai_day()
    run_dir = ROOT / 'runs' / day
    run_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(args.source_dir).resolve() if args.source_dir else run_dir / 'tencent-source'
    state_path = run_dir / 'tencent-state.json'
    for path in (ROOT / 'runs').glob('*/tencent-state.json'):
        state = json.loads(path.read_text(encoding='utf-8-sig'))
        if state.get('phase') in ('apply_in_progress', 'events_in_progress', 'requires_review'):
            raise RuntimeError('Unresolved Tencent mutation/review state: ' + str(path))
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8-sig'))
        if state.get('phase') == 'finished':
            if not args.dry_run:
                return {**state['summary'], 'replayed': True, 'writes_this_replay': 0}
            if not args.source_dir and state.get('summary', {}).get('source_dir'):
                # A reviewed same-day correction supersedes the first frozen
                # source for comparisons without replacing its audit files.
                source_dir = project_path(state['summary']['source_dir'], ROOT)
    reused = frozen_source(source_dir)
    if not reused:
        call_script('collect_tencent.py', ['--output', str(source_dir)], run_dir / 'tencent-collect.log')
    if not (source_dir / 'filtered-rows.json').exists():
        build(source_dir)
    source = source_dir / 'filtered-rows.json'
    verification = json.loads((source_dir / 'verification.json').read_text(encoding='utf-8-sig'))
    attempt = now().strftime('%H%M%S-%f')
    api = Base(ROOT / 'receipts' / day / ('tencent-daily-' + attempt))
    previous_projects = api.read('项目总表')
    previous_services = api.read('营销服务明细')
    plan = plan_import(source, previous_projects, previous_services, configured_identities(args.identity_map), day)
    plan_path = run_dir / ('tencent-dry-run-plan.json' if args.dry_run else 'tencent-daily-import-plan.json')
    atomic_json(plan_path, plan)
    events, baseline = new_entry_candidates(plan, previous_services, previous_projects)
    runtime = notification_runtime(args.shadow)
    summary = {'module': 'tencent_marketing', 'date': day, 'run_kind': args.run_kind,
               'status': 'partial' if plan['summary']['withheld_service_rows'] or not plan['safe_to_apply'] else 'completed',
               'source_reused': reused, 'source_dir': str(source_dir), 'baseline': baseline,
               'notification_mode': runtime['mode'], 'candidate_events': len(events),
               'events_created': 0, 'direct_im_sent': 0, 'dry_run': args.dry_run,
               'import': plan['summary'],
               'archive_rows_missing_links': sum(not s['fields'].get('服务留档链接') for s in plan['services']),
               'raw_source_missing_archive_rows_before_scope_filter': len(verification['missing_archive_rows']),
               'source_changes': plan['source_changes']}
    outbox_path = run_dir / ('tencent-dry-run-outbox.json' if args.dry_run else 'tencent-outbox.json')
    atomic_json(outbox_path, {'mode': runtime['mode'], 'baseline': baseline, 'events': events,
                              'delivery': 'not_queued', 'source_sha256': plan['source_sha256']})
    if not plan['safe_to_apply']:
        atomic_json(run_dir / 'tencent-review-required.json', summary)
        if not args.dry_run:
            atomic_json(state_path, {'phase': 'source_review_required', 'reason': 'Source edit/deletion is not a confirmed new transaction; no external mutation attempted', 'summary': summary})
        return summary
    if args.dry_run:
        summary['external_mutations'] = 0
        atomic_json(run_dir / 'tencent-dry-run-summary.json', summary)
        return summary
    state = {'phase': 'apply_in_progress', 'plan': str(plan_path), 'baseline': baseline,
             'source_sha256': plan['source_sha256'], 'started_at': now().isoformat()}
    atomic_json(state_path, state)
    try:
        applied = apply_plan(plan_path, allow_live_incremental=not baseline)
        summary['import_write_count'] = applied['write_count']
        summary['import_readback'] = applied['readback']
        # Initial historical bootstrap is never queued as new-entry notifications.
        if events and runtime['notifications_enabled']:
            state['phase'] = 'events_in_progress'
            atomic_json(state_path, state)
            current_projects = api.read('项目总表')
            rows = event_rows(events, current_projects, 'tencent:' + day)
            atomic_json(run_dir / 'tencent-native-event-plan.json', rows)
            summary['events_created'] = create_event_rows(api, rows, day)
            summary['native_delivery'] = 'queued_by_native_event_only'
        else:
            summary['native_delivery'] = 'baseline_suppressed' if baseline else 'shadow_not_queued' if events else 'no_new_events'
        summary['finished_at'] = now().isoformat()
        state.update({'phase': 'finished', 'summary': summary})
        atomic_json(state_path, state)
        atomic_json(run_dir / 'tencent-summary.json', summary)
        return summary
    except BaseException as exc:
        state.update({'phase': 'requires_review', 'error': str(exc)})
        atomic_json(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--run-kind', choices=['scheduled', 'local'], default='local')
    parser.add_argument('--identity-map', type=Path, action='append', default=[])
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--shadow', action='store_true')
    args = parser.parse_args()
    with process_lock(ROOT / 'runs' / '.tencent.lock'):
        print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
