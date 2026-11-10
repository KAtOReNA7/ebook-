"""Daily Fanqie metrics and precision-aware milestone outbox."""
from datetime import date, timedelta
import json
from pathlib import Path
try:
    from .feishu_io import Base, ROOT
    from .daily_run import (atomic_json, now, shanghai_day, notification_runtime, options,
                           call_script, create_event_rows, notification_recipients)
    from .rules import _event
    from .work_copyright import apply_work_copyright
except ImportError:
    from feishu_io import Base, ROOT
    from daily_run import (atomic_json, now, shanghai_day, notification_runtime, options,
                           call_script, create_event_rows, notification_recipients)
    from rules import _event
    from work_copyright import apply_work_copyright


def milestone_step(book, registered_date, observed_date, state=None):
    """Only notify a proven below-to-above transition; keep evidence across rounded days."""
    state = dict(state or {})
    scope = ['fanqie', str(book['book_id']), registered_date]
    if state and state.get('scope') != scope:
        raise ValueError('Fanqie milestone state scope mismatch')
    state.setdefault('scope',scope)
    prior_day = state.get('last_observed_date')
    if prior_day and observed_date <= prior_day:
        return {'state':state,'events':[],'pending':[]}
    elapsed = (date.fromisoformat(observed_date)-date.fromisoformat(registered_date)).days
    if elapsed < 0:
        raise ValueError('Observation before registration')
    metric=book.get('cumulative_readers_metric',{})
    lower,upper=metric.get('lower_bound'),metric.get('upper_bound')
    if type(lower) is not int or type(upper) is not int or lower<0 or upper<lower:
        return {'state':state,'events':[],'pending':['cumulative_interval_unavailable']}
    milestones=dict(state.get('milestones',{}))
    events=[]
    pending=[]
    for threshold,days in [(10000,None),(20000,7),(50000,14),(100000,30)]:
        item=dict(milestones.get(str(threshold),{}))
        if item.get('reached'):
            continue
        if upper<=threshold:
            item.update(last_confirmed_below_date=observed_date,last_confirmed_below_metric=metric)
        elif lower>threshold:
            below=item.get('last_confirmed_below_date')
            if below and (days is None or elapsed<days):
                events.append(_event('fanqie_milestone',('fanqie',str(book['book_id'])),[registered_date,threshold],
                    threshold=threshold,window_days=days,registered_date=registered_date,
                    previous_date=below,observed_date=observed_date,
                    previous_metric=item['last_confirmed_below_metric'],current_metric=metric,
                    meaning='按平台显示值及一个显示单位缓冲观测到突破；精确累计人数未提供，缓冲边界不是平台承诺的误差界限'))
            item.update(reached=True,confirmed_above_date=observed_date,baseline_without_prior_below=not bool(below))
        else:
            pending.append(str(threshold)+': display interval overlaps threshold')
        milestones[str(threshold)]=item
    state.update(milestones=milestones,last_observed_date=observed_date)
    return {'state':state,'events':events,'pending':pending}


def event_rows(events,projects,batch):
    index={str(p['平台作品ID']):p for p in projects}
    rows=[]
    for event in events:
        project=index[event['book_id']]
        title=project['书名']+'｜累计阅读显示值突破'+str(event['threshold'])+'人'
        text=(title+'\n平台显示：'+event['current_metric']['raw']+'\n观测区间：'+event['previous_date']+' 至 '+event['observed_date']+
              '\n判定口径：'+event['meaning']+'\n作品：'+str(project.get('作品主页','')))
        rows.append({'事件唯一键':event['event_id'],'事件类型':event['kind'],'关联项目':[{'id':project['record_id']}],
                     '发生日期':event['observed_date']+' 00:00','规则ID':'v1:fanqie_milestone',
                     '判定证据':json.dumps(event,ensure_ascii=False,sort_keys=True),'通知标题':title,'通知正文':text,
                     '收件人':[{'id':r} for r in notification_recipients()],'通知状态':['待发送'],'采集批次':batch})
    return rows


def publication_rising_status(observed=None):
    """Report an explicit user pause separately from a failed or empty chart."""
    config_path = ROOT / 'config/fanqie-ranking.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
        if config.get('status') == 'user_paused':
            return {'status': 'user_paused', 'complete': False,
                    'collection_enabled': False, 'reason': config['reason'],
                    'resume_requires': 'explicit_user_request', 'handoff': config['handoff']}
        if config.get('enabled') is True and config.get('status') == 'web_daily':
            return {'status': 'separate_daily_module', 'module': 'fanqie_rising',
                    'complete': False, 'collection_enabled': True,
                    'reason': '本模块仅采集营销作品详情；榜单结果见流水线 fanqie_rising 模块'}
    return observed or {'status': 'not_connected', 'complete': False}


def current_scope(all_projects):
    marketing=[p for p in all_projects if '番茄小说' in options(p.get('平台')) and '营销项目' in options(p.get('监测范围'))]
    resolved=[p for p in marketing if str(p.get('平台作品ID','')).isdigit()
              and len(str(p['平台作品ID']))>=10
              and p.get('作品唯一键')=='fanqie:'+str(p['平台作品ID'])]
    pending=[str(p.get('作品唯一键') or p.get('record_id') or 'unkeyed')+': platform identity pending'
             for p in marketing if p not in resolved]
    return marketing,resolved,pending


def run(args):
    day=shanghai_day()
    directory=ROOT/'runs'/day
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'fanqie-state.json'
    for candidate in (ROOT/'runs').glob('*/fanqie-state.json'):
        prior=json.loads(candidate.read_text(encoding='utf-8'))
        if prior.get('phase')=='requires_review':
            raise RuntimeError('Fanqie interrupted mutation requires reconciliation: '+str(candidate))
    saved=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if saved.get('phase')=='finished' and not args.dry_run:
        # A new marketing project can be registered after today's sample.
        # Refresh coverage from current scope, without collecting, syncing or
        # advancing milestones again. Metadata completeness is not coverage.
        api=Base(ROOT/'receipts'/day/('fanqie-coverage-replay-'+now().strftime('%H%M%S-%f')))
        marketing,projects,pending=current_scope(api.read('项目总表'))
        source=directory/'fanqie-effective.json'
        if not source.exists(): source=directory/'fanqie'/'snapshot.json'
        data=json.loads(source.read_text(encoding='utf-8'))
        if data.get('collection_date')!=day or not data.get('finished_at'):
            raise ValueError('Completed Fanqie run has no valid current-day frozen observation')
        ids={str(p['平台作品ID']) for p in projects}
        books=[apply_work_copyright(b,marketing=True) for b in data['books'] if str(b['book_id']) in ids]
        observed={str(b['book_id']) for b in books}
        pending.extend(p['作品唯一键']+': missing from frozen Fanqie snapshot; collect separately or on next daily run'
                       for p in projects if str(p['平台作品ID']) not in observed)
        pending.extend(str(b['book_id'])+': missing '+','.join(b['missing_fields'])
                       for b in books if b.get('missing_fields'))
        pending.extend(str(e.get('book_id'))+': '+str(e.get('message')) for e in data.get('errors',[]))
        return {**saved['summary'],'replayed':True,'writes_this_replay':0,'coverage_refreshed':True,
                'marketing_projects':len(marketing),'projects':len(projects),'observations':len(books),
                'pending':pending,'status':'partial' if pending else saved['summary']['status'],
                'publication_rising_board':publication_rising_status(saved['summary'].get('publication_rising_board'))}
    runtime=notification_runtime(args.shadow)
    api=Base(ROOT/'receipts'/day/('fanqie-daily-'+now().strftime('%H%M%S-%f')))
    all_projects=api.read('项目总表')
    _,projects,pending=current_scope(all_projects)
    # Reviewed same-day supplements retain their real sampling time and do
    # not overwrite the initial frozen observation.
    source=directory/'fanqie-effective.json'
    if not source.exists():
        source=directory/'fanqie'/'snapshot.json'
    if not source.exists():
        arguments=['--output-dir',str(source.parent)]
        for project in projects:
            arguments+=['--book-id',str(project['平台作品ID'])]
        if projects:
            call_script('collect_fanqie.py',arguments,directory/'fanqie-collect.log')
        else:
            return {'date':day,'projects':0,'pending':pending,'status':'no_resolved_projects','write_count':0,
                    'publication_rising_board':publication_rising_status()}
    data=json.loads(source.read_text(encoding='utf-8'))
    if data.get('collection_date')!=day or not data.get('finished_at'):
        raise ValueError('Fanqie source date/finish invalid')
    registered_ids={str(p['平台作品ID']) for p in projects}
    data={**data,'books':[apply_work_copyright(b,str(b['book_id']) in registered_ids) for b in data['books']]}
    observed_ids={str(book['book_id']) for book in data['books']}
    for project in projects:
        if str(project['平台作品ID']) not in observed_ids:
            pending.append(project['作品唯一键']+': missing from frozen Fanqie snapshot; collect separately or on next daily run')
    for error in data.get('errors',[]):
        pending.append(str(error.get('book_id','unknown'))+': '+str(error.get('error','collector error')))
    for book in data['books']:
        if book.get('missing_fields'):
            pending.append(str(book['book_id'])+': missing '+','.join(book['missing_fields']))
    state_file=ROOT/'runs'/'fanqie-milestones.json'
    state=json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    events=[]
    project_index={str(p['平台作品ID']):p for p in projects}
    next_state=dict(state)
    for book in data['books']:
        project=project_index.get(str(book['book_id']))
        if not project:
            continue
        registered=str(project.get('首次进入项目总表',''))[:10]
        if not registered:
            pending.append(project['作品唯一键']+': registration_date_missing')
            continue
        key=str(book['book_id'])+':'+registered
        result=milestone_step(book,registered,day,state.get(key))
        next_state[key]=result['state']
        events+=result['events']
        pending += [key+': '+p for p in result['pending']]
    rows=event_rows(events,projects,'fanqie:'+data['started_at'])
    summary={'date':day,'projects':len(projects),'observations':len(data['books']), 'pending':pending,
             'candidate_events':len(rows),'publication_rising_board':publication_rising_status(data.get('publication_rising_board')),
             'notifications_enabled':runtime['notifications_enabled'],'status':'partial','write_count':0}
    if args.dry_run:
        atomic_json(directory/'fanqie-dry-run.json',{'summary':summary,'events':rows,'next_milestones':next_state})
        return summary
    # Persist event and milestone state together before any external writes.
    atomic_json(path,{'phase':'requires_review','source':str(source),'summary':summary,'events':rows,'next_milestones':next_state})
    call_script('sync_fanqie.py',['--source',str(source)],directory/'fanqie-sync.log')
    sync_summary=json.loads((directory/'fanqie-sync.log').read_text(encoding='utf-8'))
    summary['write_count']=sync_summary['write_count']
    summary['sync_receipt_dir']=sync_summary['receipt_dir']
    atomic_json(directory/'fanqie-outbox.json',{'mode':runtime['mode'],'events':rows,'next_milestones':next_state})
    if runtime['notifications_enabled']:
        summary['events_created']=create_event_rows(api,rows,day)
    atomic_json(state_file,next_state)
    summary['readback']='passed'
    atomic_json(path,{'phase':'finished','summary':summary})
    return summary
