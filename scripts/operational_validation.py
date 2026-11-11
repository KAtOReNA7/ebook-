"""Independent, read-only comparison of published rows to frozen source evidence."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from scripts.feishu_io import same
from scripts.local_settings import project_path

TZ = timezone(timedelta(hours=8))
BOARD_NAMES = {'rising':'微信飙升Top50','newbook':'微信新书Top50',
               'novel_female_rising':'微信女生小说飙升Top50','newbook_novel_female':'微信女生小说新书Top50'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def minute(value):
    parsed=datetime.fromisoformat(value)
    return (parsed.astimezone(TZ) if parsed.tzinfo else parsed).strftime('%Y-%m-%d %H:%M')


def compare_rows(expected, actual, key, scope):
    index={r[key]:r for r in actual}
    wanted={r[key]:r for r in expected}
    if len(index)!=len(actual) or len(wanted)!=len(expected):raise ValueError(scope+': duplicate key')
    if set(index)!=set(wanted):raise ValueError(scope+': missing or unexpected records')
    for identifier,row in wanted.items():
        for field,value in row.items():
            observed=index[identifier].get(field)
            if value in (None,'') and observed in (None,''):continue
            if not same(value,observed):raise ValueError(scope+': source mismatch '+identifier+'/'+field)


def data_contract(root, day, projects, daily, ranks, services):
    root=Path(root);directory=root/'runs'/day
    index={p['作品唯一键']:p for p in projects if p.get('作品唯一键')}
    daily_expected={};rank_expected=[]
    source=directory/'weread-effective.json'
    if not source.exists():source=directory/'weread.json'
    weread=read(source)
    publisher=read(directory/'publisher-recent-state.json')
    if publisher.get('phase')!='finished':raise ValueError('Publisher sync unfinished')
    recent=read(project_path(publisher['summary']['sync']['source'],root))
    fanqie_path=directory/'fanqie-effective.json'
    if not fanqie_path.exists():fanqie_path=directory/'fanqie/snapshot.json'
    fanqie=read(fanqie_path)
    for platform,label,data in [('weread','微信读书',weread),('fanqie','番茄小说',fanqie),('weread','微信读书',recent)]:
        if data.get('collection_date')!=day or not data.get('finished_at'):raise ValueError('Wrong frozen observation date')
        seen=set()
        for book in data['books']:
            key=platform+':'+str(book['book_id'])
            if key in seen:raise ValueError('Duplicate source book: '+key)
            seen.add(key)
            if key not in index:raise ValueError('Frozen observation has no verified project: '+key)
            observed=minute(book.get('detail_source',{}).get('observed_at',data['finished_at']))
            if observed[:10]!=day:raise ValueError('Observation date differs from row date')
            row={'日数据唯一键':day+':'+key,'作品唯一键':key,'关联项目':[{'id':index[key]['record_id']}],
                 '平台':[label],'数据日期':day+' 00:00','采集时间':observed,
                 '点评人数':book.get('reviewer_count'),'字数':book.get('word_count')}
            if platform=='weread':row.update(推荐值=book.get('recommendation_percent'),今日阅读人数=book.get('today_reading_count'))
            else:row.update(书评数量=book.get('book_review_count'),累计阅读人数=book.get('cumulative_readers'))
            old=daily_expected.get(key)
            if not old or observed>=old['采集时间']:daily_expected[key]=row
    for chart in weread['charts']:
        cid=chart['chart_id']
        if cid not in BOARD_NAMES:raise ValueError('Unexpected WeRead board')
        for book in chart['records']:
            key='weread:'+str(book['book_id'])
            rank_expected.append({'榜单快照唯一键':day+':'+cid+':'+key,'榜单':[BOARD_NAMES[cid]],
                '采集日期':day+' 00:00','作品唯一键':key,'平台作品ID':str(book['book_id']),
                '名次':book['rank'],'关联项目':[{'id':index[key]['record_id']}],'快照完整性':['完整']})
    from scripts.fanqie_rising_daily import verified_snapshot,BOARD_ID,BOARD_LABEL
    chart=verified_snapshot(directory/'fanqie-rising/snapshot.json',day)
    for book in chart['books']:
        key='fanqie:'+str(book['book_id'])
        rank_expected.append({'榜单快照唯一键':day+':'+BOARD_ID+':'+key,'榜单':[BOARD_LABEL],
            '采集日期':day+' 00:00','作品唯一键':key,'平台作品ID':str(book['book_id']),
            '名次':book['rank'],'快照完整性':['完整']})
    plan=read(directory/'tencent-daily-import-plan.json')
    if plan.get('safe_to_apply') is not True or plan.get('import_day')!=day:raise ValueError('Expense source plan not approved for date')
    fields=('服务明细唯一键','作品唯一键','源行指纹','原始行总金额','已确认分摊金额','邀评人数','计入统计','服务日期')
    expenses=[{k:s['fields'].get(k) for k in fields} for s in plan['services']]
    compare_rows(list(daily_expected.values()),daily,'日数据唯一键','daily observations')
    compare_rows(rank_expected,ranks,'榜单快照唯一键','ranking observations')
    compare_rows(expenses,services,'服务明细唯一键','expense source rows')
    return {'status':'passed','daily_records':len(daily_expected),'rank_records':len(rank_expected),'expense_records':len(expenses)}


def workflow_issues(workflow,runtime,events,field_ids=None):
    issues=[]
    try:
        expected=[('trigger_event','AddRecordTrigger'),('mark_sending','SetRecordAction'),
                  ('send_event','LarkMessageAction'),('mark_sent','SetRecordAction')]
        steps=workflow['steps'];by_id={s['id']:s for s in steps}
        if workflow['status']!='enabled' or [(s['id'],s['type']) for s in steps]!=expected:
            issues.append('Native workflow disabled or unexpected steps')
        for i,step in enumerate(steps):
            if step.get('next')!=(expected[i+1][0] if i+1<len(expected) else None) or step.get('children',{}).get('links'):
                issues.append('Native workflow execution order changed')
        send=by_id['send_event']['data'];receivers=send['receiver']
        if field_ids is not None:
            for key,name in [('title','通知标题'),('content','通知正文')]:
                refs=send[key]
                if len(refs)!=1 or refs[0].get('value_type')!='ref' or refs[0].get('value')!='$.trigger_event.'+field_ids[name]['id']:
                    issues.append('Native message field reference changed: '+name)
            if send.get('content_type')!='plain_text':issues.append('Unexpected native message content type')
        ids=[r['value']['id'] for r in receivers]
        if (set(ids)!=set(runtime['recipient_open_ids']) or len(ids)!=len(set(ids))
                or any(r.get('value_type')!='user' for r in receivers) or send.get('send_to_everyone') is not False):
            issues.append('Native workflow recipients differ from approved runtime')
        groups=by_id['trigger_event']['data']['condition_list'];kinds=[]
        if len(groups)!=2:issues.append('Native trigger must accept exactly two daily report kinds')
        for group in groups:
            conditions=group['conditions']
            if group['conjunction']!='and' or len(conditions)!=2:raise ValueError('Unexpected native conditions')
            status=next(c for c in conditions if c['field_name']=='通知状态')
            rule=next(c for c in conditions if c['field_name']=='规则ID')
            if status['operator']!='is' or len(status['value'])!=1 or status['value'][0]['value']['name']!='待发送':raise ValueError('Unexpected status condition')
            if rule['operator']!='is' or len(rule['value'])!=1:raise ValueError('Unexpected rule condition')
            kinds.append(rule['value'][0]['value'])
        if set(kinds)!={'v1:daily_report:ebook','v1:daily_report:weread'}:issues.append('Native trigger rule scope changed')
    except (KeyError,TypeError,ValueError,StopIteration,IndexError):issues.append('Native workflow definition could not be verified')
    if any(r.get('通知状态') not in (['已发送'],['暂不发送']) for r in events):
        issues.append('Previous native delivery pending, failed or unknown; reconcile before publishing')
    return issues
