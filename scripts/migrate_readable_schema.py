"""Audited one-time presentation migration; preserve record IDs and business keys."""
import argparse
from copy import deepcopy
import json
import time
from feishu_io import Base, ROOT, save, same
from presentation import to_remote

TABLES = ['项目总表', '营销服务明细', '每日数据', '榜单快照']
PRIMARY = {'营销服务明细': ('服务明细唯一键','服务摘要'),
           '每日数据': ('日数据唯一键','每日摘要'),
           '榜单快照': ('榜单快照唯一键','上榜摘要')}
RENAMES = {'最新点评人数':'最新点评人数（微信）', '最新推荐值':'最新推荐值（微信）',
           '最新今日阅读人数':'最新今日阅读人数（微信）', '最新累计阅读人数':'最新累计阅读人数（番茄）',
           '最新书评数量':'最新书评数量（番茄）'}


def definitions(before, rows):
    result = deepcopy(before)
    for table in TABLES:
        fields = result['tables'][table]['field_readback']['fields']
        for f in fields:
            if f['name'] == '版权方原文': f['name'] = '版权来源原文'
            if f['name'] == '作品分类': f['name'] = '分类原文'
            if table == '项目总表': f['name'] = RENAMES.get(f['name'], f['name'])
            if table in PRIMARY and f['name'] == PRIMARY[table][0]: f['name'] = PRIMARY[table][1]
        if table in PRIMARY:
            fields.append({'name':PRIMARY[table][0], 'type':'text'})
        if table in ('营销服务明细','每日数据'):
            fields.append({'name':'书名','type':'text'})
        fields.append({'name':'书名来源原文','type':'text'})
        if table == '项目总表':
            fields.append({'name':'最新点评人数（番茄）','type':'number','style':{'type':'plain','precision':0,'thousands_separator':True}})
        if table in ('项目总表','每日数据'):
            for name in ('版权方','一级分类','二级分类'):
                fields.append({'name':name,'type':'select','multiple':False,'options':[]})
    return result


def projected(table, row, fields, titles):
    source = {k:v for k,v in row.items() if k != 'record_id'}
    if table in ('营销服务明细','每日数据'):
        source['书名'] = titles.get(row.get('作品唯一键')) or row.get('原始书名') or ''
    out = to_remote(table, source, fields)
    if table == '项目总表' and row.get('平台') == ['番茄小说']:
        out['最新点评人数（微信）'] = None
    return {k:v for k,v in out.items() if k in fields and fields[k]['type'] not in ('lookup','formula','created_at','updated_at','auto_number')}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    config=ROOT/'config/presentation-migration.json'
    state=json.loads(config.read_text(encoding='utf-8'))
    directory=ROOT/state['receipt_dir']
    before=json.loads((directory/'manifest-before.json').read_text(encoding='utf-8'))
    rows=json.loads((directory/'records-before.json').read_text(encoding='utf-8'))
    target=definitions(before,rows)
    titles={r['作品唯一键']:r['书名'] for r in rows['项目总表']}
    plan={}
    for table in TABLES:
        fields={f['name']:f for f in target['tables'][table]['field_readback']['fields']}
        values={r['record_id']:projected(table,r,fields,titles) for r in rows[table]}
        for name in ('版权方','一级分类','二级分类'):
            if name in fields:
                opts=sorted({x for r in values.values() for x in (r.get(name) or [])})
                colors=['Blue','Green','Orange','Purple','Turquoise','Carmine']
                fields[name]['options']=[{'name':n,'hue':colors[i%len(colors)],'lightness':'Lighter'} for i,n in enumerate(opts)]
        plan[table]={'values':values,'fields':list(fields.values())}
    save(directory/'presentation-plan.json',plan)
    if not args.apply:
        print(json.dumps({t:{'records':len(p['values']),'fields':len(p['fields'])} for t,p in plan.items()},ensure_ascii=False))
        return
    guard=directory/'presentation.apply-state.json'
    if guard.exists() and not args.resume: raise RuntimeError('Existing apply state: reconcile before continuing')
    api=Base(directory/'apply', presentation_enabled=False)
    completed=set()
    # Guard against changes since the exact before snapshot, before any mutation.
    for table in TABLES:
        live=api.call('+field-list',table)['data']['fields']
        current=api.read(table)
        target_values=plan[table]['values']
        if args.resume and {r['record_id'] for r in current}==set(target_values) and all(
                all(same(v,r.get(k)) for k,v in target_values[r['record_id']].items()) for r in current):
            completed.add(table)
            full={'fields':[api.complete_field(table,f) for f in live]}
            api.manifest['tables'][table]['field_readback']=full
            api.manifest['tables'][table]['fields']={f['name']:f for f in full['fields']}
            save(ROOT/'config/base-manifest.json',api.manifest)
            save(directory/(table+'-after.json'),current)
            continue
        original=before['tables'][table]['field_readback']['fields']
        byid={f['id']:f for f in live}
        desired={f['id']:f for f in plan[table]['fields'] if f.get('id')}
        assert all(byid[f['id']]['name'] in (f['name'],desired[f['id']]['name']) for f in original)
        restored=[{'record_id':r['record_id'],**{f['name']:r.get(byid[f['id']]['name']) for f in original}} for r in current]
        if {r['record_id']:r for r in restored} != {r['record_id']:r for r in rows[table]}:
            raise RuntimeError('Records changed since backup: '+table)
        if not args.resume and {f['id']:f for f in live} != {f['id']:f for f in original}:
            raise RuntimeError('Schema changed since backup: '+table)
    save(guard,{'phase':'schema_in_progress','receipt_dir':str(directory)})
    for table,p in plan.items():
        if table in completed:
            print(table,'existing full readback passed; no repeated writes',flush=True)
            continue
        existing=api.call('+field-list',table)['data']['fields']
        live_by_id={f['id']:f for f in existing}
        originals={f['id']:f for f in before['tables'][table]['field_readback']['fields']}
        for f in p['fields']:
            if f.get('id') and f['name'] != live_by_id[f['id']]['name']:
                api.call('+field-update',table,{k:v for k,v in f.items() if k!='id'},field_id=f['id'],yes=True)
                live_by_id[f['id']]=f
                time.sleep(2.5)
        additions=[f for f in p['fields'] if not f.get('id') and f['name'] not in {x['name'] for x in live_by_id.values()}]
        if additions: api.call('+field-create',table,additions,i_have_read_guide=True)
        live=api.call('+field-list',table)['data']
        live['fields']=[api.complete_field(table,f) for f in live['fields']]
        actual={f['name']:f for f in live['fields']}
        assert set(actual)=={f['name'] for f in p['fields']},table
        for f in p['fields']:
            assert actual[f['name']]['type']==f['type'],f
            if f['type']=='select':
                assert actual[f['name']]['multiple']==f['multiple']
                assert [x['name'] for x in actual[f['name']]['options']]==[x['name'] for x in f['options']]
        api.manifest['tables'][table]['field_readback']=live
        api.manifest['tables'][table]['fields']=actual
        save(ROOT/'config/base-manifest.json',api.manifest)
        updates=list(p['values'].items())
        for i in range(0,len(updates),200):
            api.call('+record-batch-update',table,{'update_records':dict(updates[i:i+200])})
        actual_rows=api.read(table)
        index={r['record_id']:r for r in actual_rows}
        assert set(index)==set(p['values']),table
        for rid,values in p['values'].items():
            for key,value in values.items():
                if not same(value,index[rid].get(key)):
                    raise AssertionError(f'{table}/{rid}/{key}: {value!r} != {index[rid].get(key)!r}')
        save(directory/(table+'-after.json'),actual_rows)
        save(guard,{'phase':'values_in_progress','last_completed_table':table})
        print(table,len(index),'readback passed',flush=True)
    # Exact service identity, cost, source content, dates, and relation IDs preserved.
    for table in TABLES:
        current=json.loads((directory/(table+'-after.json')).read_text(encoding='utf-8'))
        old={r['record_id']:r for r in rows[table]}
        for r in current:
            for key in ('作品唯一键','关联项目','首次进入项目总表','追踪截止日','服务日期','原始行总金额',
                        '已确认分摊金额','邀评人数','源行指纹','原始内容','服务明细唯一键','日数据唯一键','榜单快照唯一键'):
                if key in old[r['record_id']]:
                    assert same(old[r['record_id']][key],r.get(key)),(table,r['record_id'],key)
    save(guard,{'phase':'finished','readback':'passed','business_fields_preserved':True,'messages_sent':0})
    state.update(phase='finished',readback='passed',business_fields_preserved=True)
    save(config,state)
    print(json.dumps(state,ensure_ascii=False))


if __name__=='__main__':main()
