"""Read back current coverage, financial reconciliation and unique keys."""
from collections import Counter
from datetime import datetime
from decimal import Decimal
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.feishu_io import Base,ROOT,save
from scripts.operational_validation import TZ,data_contract,workflow_issues


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',default=datetime.now(TZ).date().isoformat())
    day=parser.parse_args().date
    datetime.strptime(day,'%Y-%m-%d')
    api=Base(ROOT/'receipts'/day/('current-verification-'+datetime.now().strftime('%H%M%S')))
    projects=api.read('项目总表')
    services=api.read('营销服务明细')
    daily=api.read('每日数据',[['数据日期','==',f'ExactDate({day})']])
    ranks=api.read('榜单快照',[['采集日期','==',f'ExactDate({day})']])
    events=api.read('通知事件',[['发生日期','==',f'ExactDate({day})']])
    for rows,key in [(projects,'作品唯一键'),(services,'服务明细唯一键'),(daily,'日数据唯一键'),(ranks,'榜单快照唯一键'),(events,'事件唯一键')]:
        if key=='作品唯一键': rows=[r for r in rows if r.get(key)]
        if len(rows)!=len({r[key] for r in rows}):
            raise ValueError('Duplicate unique key: '+key)
    index={p['作品唯一键']:p for p in projects if p.get('作品唯一键')}
    wrong_links=[]
    for service in services:
        key=service['作品唯一键']
        ids={str(r.get('id')) for r in service.get('关联项目',[]) if isinstance(r,dict)}
        if key not in index or index[key]['record_id'] not in ids:
            wrong_links.append(service['服务明细唯一键'])
    if wrong_links:
        raise ValueError('Broken marketing links: '+str(wrong_links[:3]))
    raw=sum((money(r.get('原始行总金额')) for r in services),Decimal(0))
    confirmed=sum((money(r.get('已确认分摊金额')) for r in services if r.get('计入统计')==['是']),Decimal(0))
    deferred=sum((money(r.get('原始行总金额')) for r in services if r.get('计入统计')==['否']),Decimal(0))
    if raw!=confirmed+deferred:
        raise ValueError('Source fee reconciliation differs')
    board_counts=Counter()
    board_ranks={}
    for row in ranks:
        if row.get('快照完整性')==['完整'] and row.get('名次'):
            board=row['榜单'][0]
            board_counts[board]+=1
            board_ranks.setdefault(board,[]).append(row['名次'])
    for name,positions in board_ranks.items():
        expected = 100 if name == '番茄出版飙升榜' else 50
        if sorted(positions)!=list(range(1,expected+1)):
            raise ValueError('Incomplete rank sequence: '+name)
    marketing=[p for p in projects if p.get('监测范围')==['营销项目']]
    contract=data_contract(ROOT,day,projects,daily,ranks,services)
    runtime=json.loads((ROOT/'config/runtime.json').read_text(encoding='utf-8-sig'))
    all_events=api.read('通知事件')
    workflow=api.call('+workflow-get',workflow_id=runtime['native_workflow'])['data']
    workflow_errors=workflow_issues(workflow,runtime,all_events,api._fields('通知事件'))
    result={'verified_at':datetime.now().astimezone().isoformat(),'base_url':api.manifest['base']['url'],
            'projects':len(projects),'marketing_project_groups':len(marketing),'services':len(services),
            'daily_records':len(daily),'daily_by_platform':dict(Counter(r['平台'][0] for r in daily)),
            'complete_board_counts':dict(board_counts),'historical_missing_board_diagnostics':len(ranks)-sum(board_counts.values()),
            'fee_raw_total':str(raw),'fee_confirmed_total':str(confirmed),'fee_deferred_total':str(deferred),
            'verified_marketing_platform_ids':dict(Counter(p['平台'][0] for p in marketing if p.get('平台作品ID'))),
            'pending_marketing_identity':len([p for p in marketing if not p.get('平台作品ID')]),
            'business_events_queued':sum(r.get('事件类型')!='验收测试' for r in events),
            'acceptance_test_events':sum(r.get('事件类型')=='验收测试' for r in events),
            'acceptance_test_events_sent':sum(r.get('事件类型')=='验收测试' and r.get('通知状态')==['已发送'] for r in events),
            'all_keys_unique':True,'service_links_valid':True,
            'fee_reconciliation':'passed','readback':'passed','date':day,'source_contract':contract,
            'native_workflow_verification':{'status':'failed' if workflow_errors else 'passed','issues':workflow_errors}}
    save(ROOT/'runs'/day/'acceptance-summary.json',result)
    save(api.receipt_dir/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
