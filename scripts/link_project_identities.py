"""Attach verified platform identities to pending projects, preserving record IDs."""
import argparse
from datetime import datetime
import json
try:
    from .feishu_io import Base,ROOT,save
    from .sync_marketing import identities_from_file,normalize_title,PLATFORMS,FORMS,identity_project_key,identity_tracking_medium
except ImportError:
    from feishu_io import Base,ROOT,save
    from sync_marketing import identities_from_file,normalize_title,PLATFORMS,FORMS,identity_project_key,identity_tracking_medium


def guard_duplicate_targets(updates, service_updates):
    targets = {}
    for record_id, fields in updates.items():
        targets.setdefault(fields['作品唯一键'], []).append(record_id)
    collisions = {key: ids for key, ids in targets.items() if len(ids) > 1}
    blocked_ids = {record_id for ids in collisions.values() for record_id in ids}
    return ({rid:fields for rid,fields in updates.items() if rid not in blocked_ids},
            {rid:fields for rid,fields in service_updates.items() if fields['作品唯一键'] not in collisions},
            [{'target_key':key,'project_record_ids':ids,
              'reason':'multiple_pending_aliases_same_platform_id_merge_required'} for key,ids in collisions.items()])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--identity-map',action='append',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    for state_path in (ROOT/'receipts').glob('*/*/link-state.json'):
        state=json.loads(state_path.read_text(encoding='utf-8'))
        if state.get('phase')!='finished':
            raise RuntimeError('Prior identity mutation requires read-only reconciliation: '+str(state_path))
    identities=identities_from_file(args.identity_map)
    api=Base(ROOT/'receipts'/datetime.now().strftime('%Y-%m-%d')/('identity-link-'+datetime.now().strftime('%H%M%S-%f')))
    projects=api.read('项目总表')
    services=api.read('营销服务明细')
    index={p['作品唯一键']:p for p in projects}
    updates={}
    service_updates={}
    pending=[]
    for (platform,medium,title),identity in identities.items():
        key=identity_project_key(platform,medium,identity)
        candidates=[p for p in projects if p['作品唯一键'].startswith('pending:'+platform+':'+medium+':')
                    and normalize_title(p['书名'])==title and p.get('监测范围') != ['别名留档']]
        if key in index:
            if candidates:
                pending.append({'title':title,'reason':'canonical_target_exists_manual_merge_required'})
            continue
        if len(candidates)!=1:
            pending.append({'title':title,'reason':'pending_project_not_unique_or_absent'})
            continue
        project=candidates[0]
        old_key=project['作品唯一键']
        note=identity.get('matching_note') or identity.get('verification_basis') or identity.get('status') or '独立官方来源核对'
        updates[project['record_id']]={'作品唯一键':key,'平台作品ID':str(identity['book_id']),
                                       '作品形式':FORMS[identity_tracking_medium(identity,medium)],
                                       '作品主页':identity['detail_url'],'书名':identity.get('title') or project['书名'],
                                       '平台匹配状态':'已匹配：'+str(note),
                                       '证据与备注':project.get('证据与备注','')+'\n平台匹配证据：'+str(args.identity_map)}
        if identity.get('author'):
            updates[project['record_id']]['作者']=identity['author']
        for service in services:
            if service.get('作品唯一键')==old_key:
                service_updates[service['record_id']]={'作品唯一键':key}
    updates, service_updates, collisions = guard_duplicate_targets(updates, service_updates)
    pending.extend(collisions)
    plan={'project_updates':updates,'service_updates':service_updates,'pending':pending,'apply':args.apply}
    save(api.receipt_dir/'plan.json',plan)
    if args.apply and updates:
        state=api.receipt_dir/'link-state.json'
        save(state,{'phase':'requires_review','plan':str(api.receipt_dir/'plan.json')})
        api.call('+record-batch-update','项目总表',{'update_records':updates})
        if service_updates:
            api.call('+record-batch-update','营销服务明细',{'update_records':service_updates})
        for table,wanted in [('项目总表',updates),('营销服务明细',service_updates)]:
            actual={r['record_id']:r for r in api.read(table)}
            for record_id,fields in wanted.items():
                if any(actual[record_id].get(k)!=v for k,v in fields.items() if k!='作品主页'):
                    raise RuntimeError('Identity linking readback differs')
                if '作品主页' in fields:
                    from feishu_io import same
                    if not same(fields['作品主页'],actual[record_id].get('作品主页')):
                        raise RuntimeError('Official URL readback differs')
        save(state,{'phase':'finished','readback':'passed'})
    print(json.dumps({'projects':len(updates),'services':len(service_updates),'pending':pending,
                      'applied':args.apply,'receipt_dir':str(api.receipt_dir)},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
