"""Injected ONLY into temporary test copies. No production CLI/network allowed."""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import hashlib,json,os
from pathlib import Path
import socket
from urllib.parse import urlparse,parse_qs

DAY='2099-01-01'
STAMP=DAY+'T10:01:00+08:00'


def configure(ns):
    root=Path(ns['__file__']).resolve().parents[1]
    scenario=os.environ.get('EBOOK_TEST_SCENARIO','success')
    def blocked(*a,**kw):raise AssertionError('Fixture attempted real network')
    socket.create_connection=blocked
    socket.socket.connect=blocked
    def trace(value):
        with (root/'trace.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(value)+'\n')
    if Path(ns['__file__']).name=='collect_weread.py':
        def get(url):
            trace({'step':'source_get'})
            path=urlparse(url).path
            if 'categoryinfo' in path:
                cid=parse_qs(urlparse(url).query)['categoryId'][0]
                body=json.dumps({'CategoryId':cid,'title':ns['CHART_IDENTITIES'][cid],'ranklist':1})
            elif 'bookListInCategory' in path:
                body=json.dumps({'books':[{'searchIdx':i,'readingCount':i*2,'bookInfo':{
                    'bookId':str(i),'title':'Book '+str(i),'author':'Author','category':'Fiction',
                    'cpid':'cp','newRating':910,'newRatingCount':50,
                    'readerLink':'https://weread.qq.com/web/reader/'+str(i)}} for i in range(1,51)],'hasMore':False})
            elif '/reader/' in path:
                bid=path.rsplit('/',1)[1]
                body='window.__INITIAL_STATE__='+json.dumps({'reader':{'bookInfo':{
                    'bookId':bid,'title':'Book '+bid,'author':'Author','copyrightInfo':{'name':'Publisher'},
                    'newRating':910,'newRatingCount':50},'readingStat':{}}})
            else:raise AssertionError('Unexpected fixture endpoint')
            return body,{'url':url,'observed_at':STAMP,'http_status':200,
                         'response_sha256':hashlib.sha256(body.encode()).hexdigest()}
        ns.update(get=get,now=lambda:STAMP)
        ns['time'].sleep=lambda seconds:None
        return
    import scripts.daily_run as module
    import scripts.shared_runtime as shared
    import scripts.stage_checkpoint as stages
    import scripts.runner_policy as runner
    import scripts.tencent_daily as tencent
    import scripts.fanqie_daily as fanqie
    import scripts.fanqie_rising_daily as rising
    import scripts.publisher_recent_daily as publisher
    import scripts.resume_daily_notifications as comparison
    import scripts.notification_policy as policy
    import scripts.daily_reports as reports
    from scripts.report_publication import BOARDS
    from scripts.operational_validation import compare_rows
    settings=json.loads((root/'fixture.json').read_text(encoding='utf8'))
    dayroot=root/'runs'/DAY;dayroot.mkdir(parents=True,exist_ok=True)
    for scope in (ns,module.__dict__):
        scope.update(ROOT=root,shanghai_day=lambda value=None:DAY,
                     now=lambda:datetime.now(timezone(timedelta(hours=8))).replace(year=2099,month=1,day=1))
    runner.enforce_writer=lambda *a,**kw:None
    runner.host_fingerprint=lambda:'fixture-host'
    policy.notification_recipients=lambda root=None:['owner']
    def state():
        p=root/'fake-base.json'
        return json.loads(p.read_text(encoding='utf8')) if p.exists() else {'daily':[],'events':[]}
    def save(value):(root/'fake-base.json').write_text(json.dumps(value),encoding='utf8')
    class API:
        def __init__(self,*a,**kw):pass
        def read(self,table,*a):
            trace({'step':'read','table':table})
            return state()['events'] if table=='通知事件' else state()['daily']
        def call(self,cmd,*a,**kw):
            assert cmd=='+workflow-get'
            workflow=settings['workflow']
            if scenario=='roster_changed':workflow['steps'][2]['data']['receiver'][0]['value']['id']='other'
            return {'data':workflow}
        def _fields(self,table):return {'通知标题':{'id':'title_id'},'通知正文':{'id':'body_id'}}
    ns['Base']=API
    @contextmanager
    def session(root,dry_run=False,staged=False,resume=False):
        trace({'step':'lock'})
        os.environ[shared.LEASE_ENV]='fixture-lease'
        base=shared.capture(root,DAY)
        stages.start(root,base,{'generation':'fixture'},'fixture-lease')
        result={}
        yield result
        trace({'step':'checkpoint'})
        result.update(checkpoint_committed=True,lock_released=True)
        trace({'step':'unlock'})
    shared.session=session
    stages.persist=lambda root,phase:trace({'step':'evidence','phase':phase})
    ready=settings['pipeline']['modules']
    def source(name):
        def invoke(*a,**kw):trace({'step':name});return ready[name]
        return invoke
    tencent.run=source('tencent');fanqie.run=source('fanqie')
    rising.run=source('fanqie_rising');publisher.run=source('publisher_recent')
    def weread(args):
        trace({'step':'weread'})
        if scenario=='source_failed':return {'status':'failed'}
        data,reused=ns['collect_frozen'](dayroot,DAY,[],'scheduled')
        assert len(data['books'])==50 and all(c['complete_top50'] for c in data['charts'])
        expected=[{'id':b['book_id'],'value':b['recommendation_percent']} for b in data['books']]
        before=state();before['daily']=expected;save(before)
        compare_rows(expected,state()['daily'],'id','fixture source contract')
        trace({'step':'sync_readback','reused_source':reused})
        return {**ready['weread'],'coverage':{'requested_books':50,'successful_books':50}}
    ns['run']=weread
    ns['run_project_initialization']=source('project_initialization')
    ns['run_project_notifications']=source('project_notifications')
    ns['run_expense_notifications']=source('marketing_expense_notifications')
    comparison.resume_today=source('notification_comparison')
    real_call=ns['call_script']
    def call(name,args,log,*a,**kw):
        if name!='verify_current_base.py':return real_call(name,args,log,*a,**kw)
        trace({'step':'independent_readback'})
        frozen=json.loads((dayroot/'weread.json').read_text(encoding='utf8'))
        expected=[{'id':b['book_id'],'value':b['recommendation_percent']} for b in frozen['books']]
        compare_rows(expected,state()['daily'],'id','independent source contract')
        value={'date':DAY,'verified_at':ns['now']().isoformat(),'readback':'passed',
               'all_keys_unique':True,'service_links_valid':True,'fee_reconciliation':'passed',
               'complete_board_counts':BOARDS,'source_contract':{'status':'passed'},
               'native_workflow_verification':{'status':'passed'}}
        if scenario=='readback_failed':value['source_contract']['status']='failed'
        ns['atomic_json'](dayroot/'acceptance-summary.json',value)
    ns['call_script']=call
    ns['notification_runtime']=lambda force_shadow=False:{'notifications_enabled':True,'mode':'live'}
    def summary(*a):
        evidence=settings['evidence']
        if scenario=='copyright_missing':evidence['copyright_ranking'][0]['unknown']=['missing']
        return 'Fixture only',evidence
    reports.summary_body=summary
    reports.enabled=lambda root:True
    def writer(api,rows,day):
        trace({'step':'publish','count':len(rows)})
        value=state();existing={r['事件唯一键'] for r in value['events']}
        assert not existing.intersection(r['事件唯一键'] for r in rows)
        value['events'] += [{**r,'通知状态':['已发送']} for r in rows];save(value)
    ns['deliver_event_rows']=writer
    ns['run_notification_health']=lambda *a,**kw:{'status':'ok'}
