"""Resume evidence comparisons separately from completed collection.

Normal entry resumes only today's comparisons. The standalone entry requires
an explicit private date authorization, recomputes from frozen observations,
and never imports historical outboxes or modifies daily business samples.
"""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import daily_run as daily
from scripts import fanqie_rising_daily as rising
from scripts import notification_digest as digest
from scripts.shared_runtime import digest as sha

STATE = 'notification-comparison.json'


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def authorized(root, day):
    policy = read(root/'config/notification-recovery.json', {})
    return policy.get('authorized_dates', {}).get(day, {}).get('recompute_unsent_only') is True


def complete_weread(path, day):
    data = daily.load_snapshot(path, day)
    if data is None:
        raise ValueError('Missing WeRead snapshot: ' + day)
    charts = {c['chart_id']: c for c in data['charts']}
    required = {'rising', 'newbook', 'novel_female_rising', 'newbook_novel_female'}
    if len(charts) != len(data['charts']):
        raise ValueError('Duplicate WeRead chart')
    for cid in required:
        c = charts.get(cid, {})
        rows = c.get('records', [])
        if (c.get('complete_top50') is not True or c.get('errors') or len(rows) != 50
                or {r['rank'] for r in rows} != set(range(1,51))
                or len({str(r['book_id']) for r in rows}) != 50):
            raise ValueError('Incomplete WeRead chart: ' + cid)
    return data


def weread_path(root, day):
    folder = root/'runs'/day
    effective = folder/'weread-effective.json'
    return effective if effective.exists() else folder/'weread.json'


def plan(root, day, projects, runtime):
    root = Path(root)
    yesterday = (date.fromisoformat(day)-timedelta(days=1)).isoformat()
    directory = root/'runs'/day
    scope_path = directory/'notification-comparison-projects.json'
    scope = read(scope_path)
    if scope is None:
        if day != daily.shanghai_day():
            return {'date':day,'status':'partial','rows':[],
                    'modules':{'scope':{'status':'waiting_evidence','reason':'Missing observation-day project scope'}}}
        # Freeze current-day eligibility. A later marketing entry must not
        # become retroactively eligible for an earlier board notification.
        fields = {'record_id','作品唯一键','书名','平台作品ID','平台','作品形式',
                  '监测范围','磨铁版权确认','作品主页'}
        scope = {'date':day,'projects':[{k:v for k,v in p.items() if k in fields} for p in projects]}
        daily.atomic_json(scope_path,scope)
    if scope.get('date') != day:
        raise ValueError('Notification project scope date differs')
    current_projects = {p.get('作品唯一键'):p for p in projects if p.get('作品唯一键')}
    projects = scope['projects']
    saved = read(directory/STATE, {'modules': {}})
    result = {'date':day, 'modules':{}, 'rows':[]}
    for module, filename in [('weread','daily-state.json'),('fanqie_rising','fanqie-rising-state.json')]:
        state = read(directory/filename, {})
        summary = state.get('summary', {})
        # Existing producers already handled successful comparisons. Only a
        # recorded missing predecessor can resume; never replay shadow history.
        if state.get('phase') != 'finished':
            result['modules'][module] = {'status':'waiting_collection'}
            continue
        if not summary.get('baseline'):
            result['modules'][module] = {'status':'handled_by_source'}
            continue
        try:
            if module == 'weread':
                before_path, current_path = weread_path(root,yesterday), weread_path(root,day)
                previous, current = complete_weread(before_path,yesterday), complete_weread(current_path,day)
                events, pending = daily.event_candidates(previous,current,projects)
                rows = daily.notification_rows(events,projects,'weread:'+current['started_at'])
                for row in rows:
                    row['通知正文'] += '\n实际采样区间：'+previous['started_at']+' 至 '+current['started_at']
            else:
                before_path = root/'runs'/yesterday/'fanqie-rising/snapshot.json'
                current_path = directory/'fanqie-rising/snapshot.json'
                previous = rising.verified_snapshot(before_path,yesterday)
                current = rising.verified_snapshot(current_path,day)
                config = rising.configuration(root)
                if not rising.notification_gate(config,runtime,current,day):
                    result['modules'][module] = {'status':'notification_disabled'}
                    continue
                index = rising.marketing_index(projects)
                events = rising.entry_events(previous,current,index)
                if config.get('notify_exits') is True:
                    events += rising.exit_events(previous,current,index)
                rows = rising.notification_rows(events,index,rising.BOARD_ID+':'+current['started_at'])
                pending = []
            fingerprint = sha(before_path.read_bytes()+b'\0'+current_path.read_bytes()+b'\0'+scope_path.read_bytes())
            prior = saved.get('modules',{}).get(module,{})
            if prior.get('fingerprint') and prior['fingerprint'] != fingerprint:
                raise RuntimeError('Previously compared evidence changed; reconcile before retry')
            result['modules'][module] = {'status':'compared','fingerprint':fingerprint,
                'candidate_events':len(rows),'metric_gaps':pending}
            live_ids = {p['record_id'] for key,p in current_projects.items()
                        if any(old.get('作品唯一键')==key and old.get('record_id')==p['record_id'] for old in projects)}
            if any(link['id'] not in live_ids for row in rows for link in row['关联项目']):
                raise RuntimeError('Historical project identity changed; reconcile before delivery')
            result['rows'].extend(rows)
        except (OSError, ValueError) as error:
            result['modules'][module] = {'status':'waiting_evidence','reason':str(error)}
    result['status'] = 'partial' if any(m['status'].startswith('waiting_') for m in result['modules'].values()) else 'completed'
    return result


def resume_today(args, pipeline, runtime, root, api):
    """Called before the existing unified digest flush, never sends directly."""
    day = pipeline['date']
    result = plan(root,day,api.read('项目总表'),runtime)
    directory = root/'runs'/day
    if not args.dry_run and runtime.get('notifications_enabled'):
        digest.stage(api,result['rows'],day,root)
        daily.atomic_json(directory/STATE,{k:v for k,v in result.items() if k != 'rows'})
    daily.atomic_json(directory/'notification-comparison-preview.json',result)
    return {k:v for k,v in result.items() if k != 'rows'}


def recover(args, root, api, runtime):
    day = args.date
    if not authorized(root,day):
        raise ValueError('This date has no explicit owner authorization for unsent comparison recovery')
    if not runtime.get('notifications_enabled') or runtime.get('mode') != 'owner_only':
        raise ValueError('Recovery requires the existing owner-only notification policy')
    result = plan(root,day,api.read('项目总表'),runtime)
    if result['status'] != 'completed':
        daily.atomic_json(root/'runs'/day/'notification-recovery-preview.json',result)
        return result  # Never seal a category before both source comparisons finish.
    records = api.read('通知事件',[['发生日期','==',f'ExactDate({day})']])
    consumed = digest.consumed_ids(records)
    pending = [r for r in result['rows'] if r['事件唯一键'] not in consumed]
    queue = read(root/'runs'/day/'notification-digest-queue.json',{'events':{}})
    if set(queue['events'])-consumed-{r['事件唯一键'] for r in pending}:
        raise RuntimeError('Other unsent events exist; reconcile without replaying old queues')
    groups = digest.build(pending,day)
    from scripts import daily_reports
    if daily_reports.enabled(root) and pending:
        context=read(root/'runs'/day/'daily-report-context.json',{'date':day,'modules':{}})
        context['modules']['notification_comparison']={k:v for k,v in result.items() if k!='rows'}
        observations=api.read('每日数据',[['数据日期','==',f'ExactDate({day})']])
        groups=[daily_reports.event_row(day,'ebook',daily_reports.ebook_body(context,pending,observations),
                {'member_event_ids':[r['事件唯一键'] for r in pending]},daily.RECIPIENTS)]
    existing = {r['事件唯一键'] for r in records}
    if any(r['事件唯一键'] in existing for r in groups):
        raise RuntimeError('Category already sent with different members; no duplicate category delivery')
    # Recovered messages explicitly identify observation day and actual resend.
    for row in groups:
        row['通知标题'] = '补发｜'+row['通知标题']
        row['通知正文'] = '依据补齐后的真实相邻日采样重新判定；补发于'+daily.now().isoformat()+'\n'+row['通知正文']
        daily.enforce_recipients(row,root)
    preview = {**result,'groups':groups,'new_messages':len(groups),'already_consumed':len(consumed)}
    directory = root/'runs'/day
    target = directory/'notification-recovery-preview.json'
    fingerprint = sha(json.dumps(result,ensure_ascii=False,sort_keys=True).encode())
    previous_preview = read(target,{})
    if not args.dry_run and previous_preview.get('validated_fingerprint') != fingerprint:
        raise RuntimeError('Run the recovery dry-run with this exact evidence before writing')
    preview['validated_fingerprint'] = fingerprint
    daily.atomic_json(target,preview)
    if args.dry_run:
        return {'status':'dry_run','new_messages':len(groups),'modules':result['modules']}
    guard = directory/'notification-recovery.apply-state.json'
    if groups:
        daily.atomic_json(guard,{'phase':'events_in_progress','event_keys':[r['事件唯一键'] for r in groups]})
        daily.deliver_event_rows(api,groups,day)
    daily.atomic_json(guard,{'phase':'finished','messages_created':len(groups)})
    daily.atomic_json(directory/STATE,{k:v for k,v in result.items() if k != 'rows'})
    return {'status':'completed','messages_created':len(groups),'modules':result['modules']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args(); date.fromisoformat(args.date)
    from scripts.shared_runtime import session
    root = daily.ROOT
    with daily.process_lock(root/'runs/.daily.lock'), session(root,args.dry_run) as shared:
        if shared and shared.get('skipped'):
            print(json.dumps(shared)); return 0
        api = daily.Base(root/'receipts'/daily.shanghai_day()/('notification-recovery-'+daily.now().strftime('%H%M%S-%f')))
        result = recover(args,root,api,daily.notification_runtime(False))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status'] in {'completed','dry_run'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
