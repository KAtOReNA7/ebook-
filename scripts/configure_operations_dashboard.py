"""Guarded native operations charts; user-controlled selection and date windows.

No collection, fees, events or notification settings are changed by this tool.
Run without --apply to save a plan. A failed apply requires read-only reconciliation.
"""
import argparse
import json
from datetime import datetime
try:
    from .feishu_io import Base, ROOT, save
    from .daily_run import process_lock, TZ
    from .board_display import all_labels, memberships, OFF_BOARD
except ImportError:
    from feishu_io import Base, ROOT, save
    from daily_run import process_lock, TZ
    from board_display import all_labels, memberships, OFF_BOARD

PROJECT_FIELDS = [
    {'name': '加入趋势对比', 'type': 'checkbox', 'description': '勾选一本看趋势，勾选多本在运营趋势看板对比；不会改变采集与通知。'},
    {'name': '趋势起始日期', 'type': 'datetime', 'style': {'format': 'yyyy/MM/dd'}, 'description': '可选；空白时看最近30天，含今天。'},
    {'name': '趋势结束日期', 'type': 'datetime', 'style': {'format': 'yyyy/MM/dd'}, 'description': '可选；空白时截止今天。起止均包含当天。'},
    {'name': '当前榜单', 'type': 'select', 'multiple': False,
     'options': [{'name': x} for x in all_labels()],
     'description': '最近一次四榜完整快照；同书多榜用组合单选标签，避免丢失归属。'},
    {'name': '榜单核验日期', 'type': 'datetime', 'style': {'format': 'yyyy/MM/dd'}},
]
DAILY_FIELDS = [
    {'name': '对比作品', 'type': 'formula', 'expression': '[书名]&"｜"&[关联项目].[作者].FIRST()&"｜"&[作品唯一键]'},
    {'name': '趋势日期', 'type': 'formula', 'expression': 'TEXT([数据日期],"YYYY-MM-DD")'},
    {'name': '趋势纳入', 'type': 'formula', 'expression':
     'IF(AND([关联项目].[加入趋势对比].FIRST(),[数据日期]>=IFBLANK([关联项目].[趋势起始日期].FIRST(),TODAY()-29),'
     '[数据日期]<=IFBLANK([关联项目].[趋势结束日期].FIRST(),TODAY())),"纳入","不纳入")'},
]


def chart_config(platform, metric):
    return {'table_name': '每日数据', 'series': [{'field_name': metric, 'rollup': 'MAX'}],
            'group_by': [{'field_name': '趋势日期', 'mode': 'integrated', 'sort': {'type': 'group', 'order': 'asc'}},
                         {'field_name': '对比作品', 'mode': 'integrated'}],
            'filter': {'conjunction': 'and', 'conditions': [
                {'field_name': '平台', 'operator': 'is', 'value': platform},
                {'field_name': '趋势纳入', 'operator': 'is', 'value': '纳入'},
                {'field_name': metric, 'operator': 'isNotEmpty'}]}}


def apply(api, state, receipt, source):
    def mutation(command, table=None, payload=None, **flags):
        state['pending'] = {'command': command, 'table': table, 'payload': payload, 'flags': flags}
        save(receipt/'state.json', state)
        result = api.call(command, table, payload, **flags)
        state.setdefault('acks', []).append({'request': state['pending'], 'reply': result})
        state['pending'] = None
        save(receipt/'state.json', state)
        return result

    for table, definitions in [('项目总表', PROJECT_FIELDS), ('每日数据', DAILY_FIELDS)]:
        live = api.call('+field-list', table)['data']['fields']
        existing = {f['name']: f for f in live}
        for definition in definitions:
            if definition['name'] in existing:
                if existing[definition['name']]['type'] != definition['type']:
                    raise RuntimeError('Existing field conflicts: ' + definition['name'])
        missing = [f for f in definitions if f['name'] not in existing]
        if missing:
            mutation('+field-create', table, missing, **({'i_have_read_guide': True} if table == '每日数据' else {}))
        live = api.call('+field-list', table)['data']['fields']
        assert {f['name'] for f in definitions} <= {f['name'] for f in live}
        api.manifest['tables'][table]['field_readback']['fields'] = live
        api.manifest['tables'][table]['fields'] = {f['name']: f for f in live}
    save(ROOT/'config/base-manifest.json', api.manifest)

    source_data = json.loads(source.read_text(encoding='utf-8'))
    labels = memberships(source_data)
    if labels is None:
        raise RuntimeError('Cannot populate board labels from incomplete charts')
    projects = api.read('项目总表')
    updates = {p['record_id']: {'当前榜单': [labels.get(p['作品唯一键'], OFF_BOARD)],
                               '榜单核验日期': source_data['collection_date']+' 00:00'}
               for p in projects if p.get('作品唯一键', '').startswith('weread:')}
    for start in range(0, len(updates), 200):
        mutation('+record-batch-update', '项目总表', {'update_records': dict(list(updates.items())[start:start+200])})
    after = api.read('项目总表')
    assert all(p['当前榜单'] == updates[p['record_id']]['当前榜单'] for p in after if p['record_id'] in updates)

    # Preserve every existing filter, sort and visible field except requested changes.
    for table in ['项目总表', '每日数据']:
        views = api.call('+view-list', table)['data']['views']
        for v in views:
            old = api.call('+view-get-visible-fields', table, view_id=v['id'])['data']['visible_fields']
            hide = {'作品形式'}
            if '微信' in v['name'] or '番茄' in v['name']:
                hide.add('版权方')
            fields = [f for f in old if f not in hide]
            if table == '项目总表' and v['name'] in ('微信读书项目','番茄小说项目','仅榜单样本'):
                for f in reversed(['加入趋势对比'] + (['当前榜单','榜单核验日期'] if v['name']=='仅榜单样本' else [])):
                    if f not in fields: fields.insert(2, f)
            if fields != old:
                mutation('+view-set-visible-fields', table, {'visible_fields': fields}, view_id=v['id'])
            actual = api.call('+view-get-visible-fields', table, view_id=v['id'])['data']['visible_fields']
            assert actual == fields
    views = api.call('+view-list', '项目总表')['data']['views']
    name = '趋势对比选书'
    matches = [v for v in views if v['name']==name]
    if not matches:
        mutation('+view-create','项目总表',{'name': name,'type':'grid'})
        matches = [v for v in api.call('+view-list','项目总表')['data']['views'] if v['name']==name]
    assert len(matches)==1
    selection_id = matches[0]['id']
    selection_fields = ['书名','作者','平台','加入趋势对比','趋势起始日期','趋势结束日期','每日记录','采集状态','平台匹配状态']
    mutation('+view-set-visible-fields','项目总表',{'visible_fields':selection_fields},view_id=selection_id)
    mutation('+view-set-sort','项目总表',{'sort_config':[{'field':'加入趋势对比','desc':True},{'field':'书名','desc':False}]},view_id=selection_id)

    dashboards = api.call('+dashboard-list')['data']['items']
    if dashboards:
        raise RuntimeError('Unexpected existing dashboard; inspect before creating')
    result = mutation('+dashboard-create', name='运营趋势看板')
    state['dashboard_create'] = result
    save(receipt/'state.json',state)
    dashboards = api.call('+dashboard-list')['data']['items']
    save(receipt/'dashboard-list.json', dashboards)
    print(json.dumps({'phase':'fields_views_ready','dashboards':dashboards,'selection_view':selection_id},ensure_ascii=False),flush=True)
    state['selection_view'] = selection_id
    state['phase'] = 'dashboard_container_created'
    save(receipt/'state.json', state)
    finish(api, state, receipt)


def finish(api, state, receipt):
    if state['phase'] != 'dashboard_container_created' or state.get('pending'):
        raise RuntimeError('Not a reconciled empty dashboard container')
    dashboards = api.call('+dashboard-list')['data']['items']
    found = [d for d in dashboards if d['name']=='运营趋势看板']
    assert len(found)==1
    dashboard = found[0]['dashboard_id']
    blocks = api.call('+dashboard-block-list', dashboard_id=dashboard)['data']
    save(receipt/'blocks-before.json', blocks)
    if blocks.get('items') or blocks.get('blocks'):
        raise RuntimeError('Expected empty container; reconcile existing blocks first')

    def write(command, table=None, payload=None, **flags):
        state['pending']={'command':command,'table':table,'payload':payload,'flags':flags}
        save(receipt/'state.json',state)
        reply=api.call(command,table,payload,**flags)
        state['acks'].append({'request':state['pending'],'reply':reply})
        state['pending']=None
        save(receipt/'state.json',state)
        return reply

    text = ('# 作品每日趋势与对比\n'
            '1. 到项目总表的「趋势对比选书」视图，勾选「加入趋势对比」。选一本看趋势，选多本看对比。\n'
            '2. 起止日期可选填；留空看最近30天。日期含首尾，只显示真实采样。\n'
            '3. 回到本看板查看；每本书一条线，微信和番茄分开。清除勾选即可移出对比。\n'
            '4. 当前选择是共享的，其他协作者改动会影响本看板。\n\n'
            '## 指标口径\n'
            '- 微信「今日阅读」是当天实际采样时的值，不是全天总人数，也不能相加为累计读者。\n'
            '- 「点评人数」是截至采样时的总人数；曲线两天之差是点评人数净变化，并不等于新增书评篇数。\n'
            '- 番茄累计阅读仅使用平台明确的人读过；约数需结合每日数据中的精度说明。\n'
            '- 未开分、未采到、尚未核实ID均留空。缺点不是0；不要把跨缺失日期的连线当作逐日观察。\n\n'
            '## 表格导航\n'
            '- 项目总表：按作品看最新指标、选书、汇总费用；微信/番茄/仅榜单样本是同表的筛选视图。\n'
            '- 每日数据：一书一天一条，查原值、采集时间、缺失原因和来源。\n'
            '- 营销服务明细：每笔服务的日期、数量、费用和留档。\n'
            '- 榜单快照：每一天每个榜的名次；同书多榜分别保留。番茄榜单仍暂停。\n'
            '- 系统后台：通知事件、运行日志、规则配置及通知工作流，日常分析无需进入。')
    text, guide = text.split('## 表格导航', 1)
    write('+dashboard-block-create',dashboard_id=dashboard,name='使用说明与指标口径',type='text',data_config=json.dumps({'text':text},ensure_ascii=False))
    for platform, metrics in [('微信读书',['今日阅读人数','点评人数','推荐值']),('番茄小说',['累计阅读人数','点评人数','书评数量'])]:
        for metric in metrics:
            write('+dashboard-block-create',dashboard_id=dashboard,name=platform+'｜'+metric+'每日变化',type='line',
                  data_config=json.dumps(chart_config(platform,metric),ensure_ascii=False))
    write('+dashboard-block-create',dashboard_id=dashboard,name='各表用途',type='text',data_config=json.dumps({'text':'## 表格导航'+guide},ensure_ascii=False))
    write('+dashboard-arrange',dashboard_id=dashboard)

    # Two genuine observed books per platform demonstrate the comparison, no metrics are changed.
    projects=api.read('项目总表'); daily=api.read('每日数据')
    from collections import Counter
    counts=Counter(r.get('作品唯一键') for r in daily)
    selected=[]
    for platform in ['微信读书','番茄小说']:
        candidates=[p for p in projects if platform in p.get('平台',[]) and counts[p.get('作品唯一键')]>1
                    and '营销项目' in p.get('监测范围',[])]
        candidates.sort(key=lambda p:(-counts[p['作品唯一键']],p['书名']))
        selected+=candidates[:2]
    if not any(p.get('加入趋势对比') for p in projects):
        write('+record-batch-update','项目总表',{'update_records':{p['record_id']:{'加入趋势对比':True} for p in selected}})
    state['selected_examples']=[{'key':p['作品唯一键'],'title':p['书名']} for p in selected]

    before=api.call('+base-block-list')['data']['blocks']
    matches=[b for b in before if b['type']=='folder' and b['name']=='系统后台（日常无需打开）']
    if not matches:
        write('+base-block-create',type='folder',name='系统后台（日常无需打开）')
        matches=[b for b in api.call('+base-block-list')['data']['blocks'] if b['type']=='folder' and b['name']=='系统后台（日常无需打开）']
    assert len(matches)==1
    folder=matches[0]['id']
    for b in before:
        if b['name'] in ('通知事件','运行日志','规则配置') or b['type']=='workflow':
            write('+base-block-move',block_id=b['id'],parent_id=folder)
    write('+base-block-move',block_id=dashboard,before_id=api.manifest['tables']['项目总表']['id'])
    save(receipt/'blocks-after.json',api.call('+base-block-list')['data'])
    state['phase']='finished'
    state['dashboard_id']=dashboard
    save(receipt/'state.json',state)
    save(ROOT/'config/operations-dashboard.json',{'dashboard_id':dashboard,'selection_view':state['selection_view'],
        'receipt_dir':str(receipt),'phase':'finished','sample_selection':state['selected_examples']})
    print(json.dumps({'dashboard':dashboard,'phase':'finished'},ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--finish-empty-container',action='store_true')
    p.add_argument('--source',type=__import__('pathlib').Path,required=True)
    args=p.parse_args()
    receipt=ROOT/'receipts'/datetime.now(TZ).strftime('%Y-%m-%d/operations-dashboard')
    if args.finish_empty_container:
        with process_lock(ROOT/'runs/.daily.lock'):
            finish(Base(receipt),json.loads((receipt/'state.json').read_text(encoding='utf-8')),receipt)
        return
    if (receipt/'state.json').exists():
        raise RuntimeError('Existing migration state; reconcile instead of retry')
    save(receipt/'plan.json',{'project_fields':PROJECT_FIELDS,'daily_fields':DAILY_FIELDS,'source':str(args.source)})
    if args.apply:
        with process_lock(ROOT/'runs/.daily.lock'):
            apply(Base(receipt),{'phase':'applying','pending':None},receipt,args.source)


if __name__=='__main__':main()
