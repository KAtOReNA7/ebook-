"""Delete out-of-scope source services and their dependent records, with full backup/readback."""
import argparse
from collections import Counter
from decimal import Decimal
import json
try:
    from .feishu_io import Base,ROOT,save,same
    from .service_scope import enforce_write
except ImportError:
    from feishu_io import Base,ROOT,save,same
    from service_scope import enforce_write

def amount(rows,field):
    return str(sum((Decimal(str(r.get(field) or 0)) for r in rows),Decimal(0)).quantize(Decimal('.01')))

def plan_cleanup(rows):
    admitted=[]; excluded=[]
    for r in rows['营销服务明细']:
        try: enforce_write('营销服务明细',r); admitted.append(r)
        except ValueError: excluded.append(r)
    marketing_keys={r['作品唯一键'] for r in admitted}
    chart_keys={r['作品唯一键'] for r in rows['榜单快照'] if r.get('名次') and r.get('作品唯一键')}
    keep=[]; remove=[]; updates={}
    for p in rows['项目总表']:
        key=p.get('作品唯一键')
        draft=not key
        ebook_alias=p.get('监测范围')==['别名留档'] and p.get('作品形式')=='电子书'
        if key in marketing_keys|chart_keys or draft or ebook_alias:
            keep.append(p)
            if key in marketing_keys|chart_keys:
                new={'作品形式':'电子书','监测范围':['营销项目' if key in marketing_keys else '仅榜单样本']}
                note=p.get('证据与备注') or ''
                if '营销台账源表真实导入' in note or '有声服务' in note:
                    new['证据与备注']='电子书范围核验（2026-11-03）：本项目只关联明确电子书服务；有声及未确认形式的服务已从正式表移除。原始证据与清理前记录保存在本地审计快照。' if key in marketing_keys else '电子书榜单样本；不计营销费用。'
                updates[p['record_id']]={k:v for k,v in new.items() if not same(v,p.get(k))}
        else: remove.append(p)
    keys={r.get('作品唯一键') for r in keep if r.get('作品唯一键')}
    removed_ids={r['record_id'] for r in remove}
    delete={'营销服务明细':excluded,'每日数据':[r for r in rows['每日数据'] if r.get('作品唯一键') not in keys],
            '榜单快照':[r for r in rows['榜单快照'] if r.get('作品唯一键') and r['作品唯一键'] not in keys],
            '通知事件':[r for r in rows['通知事件'] if any(x.get('id') in removed_ids for x in r.get('关联项目',[]))],
            '项目总表':remove}
    summary={'delete_counts':{t:len(rs) for t,rs in delete.items()},'retained_services':len(admitted),
             'retained_marketing_projects':len(marketing_keys),'retained_projects_total':len(keep),
             'manual_drafts_preserved':sum(not p.get('作品唯一键') for p in keep),
             'electronic_aliases_preserved':sum(p.get('监测范围')==['别名留档'] for p in keep),
             'excluded_services_by_medium':dict(Counter(r.get('服务对象') for r in excluded)),
             'retained_raw_fee':amount(admitted,'原始行总金额'),
             'retained_confirmed_fee':amount([r for r in admitted if r['计入统计']==['是']],'已确认分摊金额'),
             'removed_raw_fee':amount(excluded,'原始行总金额'),
             'retained_invited_people':sum(r.get('邀评人数') or 0 for r in admitted)}
    return {'delete':{t:[r['record_id'] for r in rs] for t,rs in delete.items()},
            'project_updates':{k:v for k,v in updates.items() if v},'retained_keys':sorted(keys),'summary':summary}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    config=ROOT/'config/ebook-only-cleanup.json';state=json.loads(config.read_text(encoding='utf-8'))
    directory=ROOT/state['receipt_dir'];before=json.loads((directory/'records-before.json').read_text(encoding='utf-8'))
    plan=plan_cleanup(before);save(directory/'cleanup-plan.json',plan)
    print(json.dumps(plan['summary'],ensure_ascii=False,indent=2),flush=True)
    if not args.apply:return
    guard=directory/'ebook-only.apply-state.json'
    if guard.exists():raise RuntimeError('Existing cleanup outcome: full reconciliation required; no repeat')
    api=Base(directory/'apply',presentation_enabled=False)
    for table,old in before.items():
        current=api.read(table)
        if {r['record_id']:r for r in current}!={r['record_id']:r for r in old}:
            save(directory/(table+'-changed-before-write.json'),current)
            raise RuntimeError('Table changed since backup: '+table)
    save(guard,{'phase':'deleting','plan':'cleanup-plan.json'})
    for table,ids in plan['delete'].items():
        for i in range(0,len(ids),100):
            api.call('+record-delete',table,{'record_id_list':ids[i:i+100]},yes=True)
        save(guard,{'phase':'deleting','last_table':table})
        print(table,'deleted',len(ids),flush=True)
    updates=list(plan['project_updates'].items())
    for i in range(0,len(updates),100):api.call('+record-batch-update','项目总表',{'update_records':dict(updates[i:i+100])})
    after={t:api.read(t) for t in before}
    save(directory/'records-after.json',after)
    for table,old in before.items():
        expected={r['record_id'] for r in old}-set(plan['delete'].get(table,[]))
        assert {r['record_id'] for r in after[table]}==expected,table
    old_services={r['record_id']:r for r in before['营销服务明细']}
    for r in after['营销服务明细']:
        assert r==old_services[r['record_id']],r['record_id']
        enforce_write('营销服务明细',r)
    old_projects={r['record_id']:r for r in before['项目总表']}
    for r in after['项目总表']:
        for k in ('书名','作者','平台作品ID','作品唯一键','首次进入项目总表','首次上榜采集日','追踪截止日'):
            assert same(r.get(k),old_projects[r['record_id']].get(k)),(r['record_id'],k)
        for k,v in plan['project_updates'].get(r['record_id'],{}).items():assert same(v,r.get(k))
        assert r.get('作品形式')!='有声书'
    project_ids={r['record_id'] for r in after['项目总表']}
    for t in ('营销服务明细','每日数据','榜单快照','通知事件'):
        for r in after[t]:assert all(x['id'] in project_ids for x in r.get('关联项目',[])),(t,r['record_id'])
    report={**plan['summary'],'after_counts':{t:len(rs) for t,rs in after.items()},
            'readback':'passed','retained_service_fields_unchanged':True,'orphan_links':0,'messages_sent':0}
    save(directory/'completion.json',report)
    save(guard,{'phase':'finished','readback':'passed','completion':'completion.json'})
    state.update(phase='finished',summary=report);save(config,state)
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
