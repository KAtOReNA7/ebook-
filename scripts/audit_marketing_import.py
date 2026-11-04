"""Read-only reconciliation of the reviewed marketing plan against all remote rows."""
import json
from pathlib import Path
from datetime import datetime
from feishu_io import Base,ROOT,same,save


def main():
    source=ROOT/'runs/2026-11-03/tencent-source/import-plan.json'
    plan=json.loads(source.read_text(encoding='utf-8'))
    api=Base(ROOT/'receipts/2026-11-03'/('marketing-audit-'+datetime.now().strftime('%H%M%S')))
    projects=api.read('项目总表')
    services=api.read('营销服务明细')
    pi={r['作品唯一键']:r for r in projects}
    si={r['服务明细唯一键']:r for r in services}
    differences=[]
    for row in plan['projects']:
        actual=pi.get(row['作品唯一键'],{})
        for field,value in row.items():
            if not same(value,actual.get(field)):
                differences.append({'table':'项目总表','key':row['作品唯一键'],'field':field,'expected':value,'actual':actual.get(field)})
    for service in plan['services']:
        row={**service['fields'],'关联项目':[{'id':pi[service['project_key']]['record_id']}]}
        actual=si.get(row['服务明细唯一键'],{})
        for field,value in row.items():
            if value is None and actual.get(field) in (None,'',[]):
                continue
            if not same(value,actual.get(field)):
                differences.append({'table':'营销服务明细','key':row['服务明细唯一键'],'field':field,'expected':value,'actual':actual.get(field)})
    report={'remote_projects':len(projects),'remote_services':len(services),'unique_project_keys':len(pi),
            'unique_service_keys':len(si),'differences':differences,'difference_count':len(differences),
            'missing_services':[r['fields']['服务明细唯一键'] for r in plan['services'] if r['fields']['服务明细唯一键'] not in si]}
    save(api.receipt_dir/'audit.json',report)
    save(ROOT/'runs/2026-11-03/tencent-source/import-audit.json',report)
    print(json.dumps({**{k:v for k,v in report.items() if k!='differences'},'differences':differences[:6]},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
