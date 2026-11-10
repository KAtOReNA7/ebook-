"""Owner-authorized production cutover. Preview first; no historical activation scripts.

The private authorization contains verified people and the explicit same-day request.
All mutations use the shared lease. Unknown outcomes retain guards, never auto-retry.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import daily_run as daily
from scripts.daily_reports import ebook_body, summary_body, event_row
from scripts.feishu_io import Base, same
from scripts.initialize_manual_projects import unknown_guard_paths
from scripts.notification_policy import OWNER_ID, notification_recipients
from scripts.shared_runtime import session


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validate_authorization(auth, day):
    people = auth.get('people', [])
    ids = [p['id'] for p in people]
    if (auth.get('date') != day or auth.get('explicit_owner_request') is not True
            or not auth.get('request') or not auth.get('resend_today')
            or not ids or OWNER_ID not in ids or len(ids) != len(set(ids))
            or any(not p['id'].startswith('ou_') or not p.get('name')
                   or p.get('identity_verified') is not True for p in people)):
        raise ValueError('Explicit same-day acceptance and verified recipients are required')
    return [{'id':p['id'],'name':p['name']} for p in people]


def definition(workflow):
    return {k:workflow[k] for k in ('title','steps')}


def workflow_for_production(original, people):
    result = copy.deepcopy(definition(original))
    result['title'] = '运营监测｜每日日报发送（正式）'
    send = next(s for s in result['steps'] if s['id'] == 'send_event')
    send['data']['receiver'] = [{'value_type':'user','value':p} for p in people]
    send['data']['send_to_everyone'] = False
    send['title'] = '向已确认项目成员发送每日日报'
    trigger = next(s for s in result['steps'] if s['id'] == 'trigger_event')
    if trigger['type'] != 'AddRecordTrigger':
        raise ValueError('Only new records may trigger delivery; never rescan historical events')
    # Keep the pending-state condition, then restrict native delivery to the two report kinds.
    pending = [c for group in trigger['data']['condition_list'] for c in group['conditions']
               if c.get('field_name') == '通知状态']
    if len(pending) != 1:
        raise ValueError('Unexpected workflow trigger; review its full definition')
    trigger['data']['condition_list'] = [
        {'conjunction':'and','conditions':copy.deepcopy(pending)+[
            {'field_name':'规则ID','operator':'is','value':[{'value_type':'text','value':'v1:daily_report:'+kind}]}]}
        for kind in ('ebook','weread')]
    return result


def plan(api, auth, root, day):
    people = validate_authorization(auth, day)
    if unknown_guard_paths(root): raise ValueError('Reconcile unknown writes before activation')
    runtime = load(root/'config/runtime.json')
    if runtime.get('production_approved'): raise ValueError('Already activated; do not repeat launch messages')
    events = api.read('通知事件')
    keys = [r['事件唯一键'] for r in events]
    if len(keys) != len(set(keys)): raise ValueError('Duplicate notification keys')
    if any(r.get('通知状态') in (['待发送'],['发送中'],['结果未知']) for r in events):
        raise ValueError('Unresolved native delivery; read-only reconcile first')
    workflow_id = runtime['native_workflow']
    workflow = api.call('+workflow-get',workflow_id=workflow_id)['data']
    if workflow['status'] != 'enabled': raise ValueError('Existing workflow must be enabled')
    pipeline = load(root/'runs'/day/'daily-report-context.json')
    original = next(r for r in events if r['事件唯一键'] == f'daily-report:v1:{day}:ebook')
    if original['通知状态'] != ['已发送']: raise ValueError('Original daily report not settled')
    member_ids = json.loads(original['判定证据'])['member_event_ids']
    queue = load(root/'runs'/day/'notification-digest-queue.json')['events']
    details = ebook_body(pipeline, [queue[k] for k in member_ids], [])
    summary, evidence = summary_body(root, day)
    if len(evidence['copyright_ranking']) != 4 or any(r['unknown'] for r in evidence['copyright_ranking']):
        raise ValueError('Four complete verified WeRead boards required')
    if not evidence['fanqie_publication_rising']['complete'] or evidence['fanqie_publication_rising']['ranking_count'] != 100:
        raise ValueError('Complete verified Fanqie Top100 required')
    rows = []
    for kind,body,proof in [('ebook',details,{'member_event_ids':member_ids}),('weread',summary,evidence)]:
        row = event_row(day,kind,body,{**proof,'production_launch_request':auth['request']},[p['id'] for p in people])
        row['事件唯一键'] += ':resend:production-launch-'+day.replace('-','')
        row['采集批次'] = 'production-launch:'+day
        if row['事件唯一键'] in keys: raise ValueError('Launch report already exists; do not resend')
        rows.append(row)
    updated = {**runtime,'mode':'live','production_approved':True,'deployment_stage':'production',
        'notifications_enabled':True,'effective_from':day,'notification_scope':'approved_project_members',
        'recipients':[p['name'] for p in people],'recipient_open_ids':[p['id'] for p in people],
        'reason':'Owner explicitly accepted production and approved the project recipient roster.',
        'production_authorization':{'explicit_owner_request':True,'accepted_on':day,'request':auth['request']}}
    rules = api.read('规则配置')
    updates = {r['record_id']:{'试运行收件人':[{'id':p['id']} for p in people]} for r in rules}
    for r in rules:
        f = updates[r['record_id']]; rule = r['规则ID']
        if rule == 'NOTICE_PILOT':
            f.update({'规则说明':'正式每日日报通知','参数与边界':'用户于'+day+'明确批准正式运行。每日两条日报，收件人：'+'、'.join(updated['recipients'])+'。不发送测试，不补发历史outbox。',
                      '依赖与验收':'程序动态读取共享正式名单；统一原生工作流只接收两类新日报，去重与未知写入保护继续生效。'})
        elif rule == 'DAILY_1000':
            f['依赖与验收']='北京时间10:00启动；任一已更新并登录的电脑持共享锁后执行，自动恢复检查点。记录实际采样时间，不补造历史。'
        elif rule in {'FANQIE_RANK','MARKETING_EXPENSE'}:
            f['依赖与验收']='正式运行，变化统一纳入每日电子书明细；收件人按共享runtime中的已核实名单。保留完整来源核对、去重和未知结果保护，不回放历史。'
    return {'date':day,'people':people,'runtime':updated,'workflow_id':workflow_id,
            'original_workflow':definition(workflow),'workflow':workflow_for_production(workflow,people),
            'rows':rows,'rules_before':rules,'rule_updates':updates},events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args = parser.parse_args()
    auth = load(args.authorization); day = daily.shanghai_day()
    validate_authorization(auth,day)
    out = ROOT/'receipts'/day/'production-activation'
    with daily.process_lock(ROOT/'runs/.daily.lock'), session(ROOT,not args.execute) as shared:
        if not shared or shared.get('skipped'): raise RuntimeError('Shared lease required')
        api = Base(out/('execute' if args.execute else 'preview'))
        proposal, before = plan(api,auth,ROOT,day)
        if not args.execute:
            daily.atomic_json(out/'preview.json',proposal)
            (out/'preview.txt').write_text('\n\n'.join(r['通知标题']+'\n'+r['通知正文'] for r in proposal['rows']),encoding='utf-8')
            print(json.dumps({'dry_run':True,'messages':2,'recipients':[p['name'] for p in proposal['people']]},ensure_ascii=False),flush=True)
            return
        if proposal != load(out/'preview.json'): raise ValueError('Plan changed since preview; review again')
        guard = ROOT/'runs'/day/'production-activation.apply-state.json'
        daily.atomic_json(guard,{'phase':'workflow_update_in_progress','request':auth['request'],
                                 'keys':[r['事件唯一键'] for r in proposal['rows']]})
        daily.atomic_json(ROOT/'config/runtime.json',proposal['runtime'])
        daily.atomic_json(ROOT/'config/production-activation.json',{**auth,'completed':False})
        for name in ('daily-notification-format.json','notification-grouping.json'):
            path = ROOT/'config'/name; value = load(path); value['owner_only']=False
            value['production_effective_from']=day; daily.atomic_json(path,value)
        path = ROOT/'config/marketing-expense-notifications.json'; value = load(path)
        value['requested_post_acceptance_members'] = [p['name'] for p in proposal['people']]
        value['production_approved']=True; daily.atomic_json(path,value)
        if notification_recipients(ROOT) != [p['id'] for p in proposal['people']]: raise ValueError('Runtime roster mismatch')
        api.call('+workflow-update',workflow_id=proposal['workflow_id'],payload=proposal['workflow'])
        current = api.call('+workflow-get',workflow_id=proposal['workflow_id'])['data']
        if definition(current) != proposal['workflow'] or current['status'] != 'enabled': raise RuntimeError('Workflow readback mismatch')
        api.call('+record-batch-update','规则配置',{'update_records':proposal['rule_updates']})
        rules_after = api.read('规则配置'); idx = {r['record_id']:r for r in rules_after}
        if set(idx) != {r['record_id'] for r in proposal['rules_before']}: raise RuntimeError('Rule set changed')
        for rid,values in proposal['rule_updates'].items():
            for field,value in values.items():
                if not same(value,idx[rid].get(field)): raise RuntimeError('Rule readback mismatch: '+field)
        daily.atomic_json(guard,{'phase':'events_in_progress','keys':[r['事件唯一键'] for r in proposal['rows']]})
        if daily.deliver_event_rows(api,proposal['rows'],day) != 2: raise RuntimeError('Unexpected launch write count')
        print('Two formal launch reports created; waiting for native workflow',flush=True)
        keys = {r['事件唯一键'] for r in proposal['rows']}
        for _ in range(40):
            after = api.read('通知事件'); sent = [r for r in after if r['事件唯一键'] in keys]
            if len(sent)==2 and all(r['通知状态']==['已发送'] for r in sent): break
            time.sleep(3)
        daily.atomic_json(out/'delivery-readback.json',sent)
        if len(sent)!=2 or any(r['通知状态']!=['已发送'] for r in sent): raise RuntimeError('Delivery unknown; do not retry')
        idx = {r['事件唯一键']:r for r in after}
        if len(after)!=len(before)+2 or any(idx[r['事件唯一键']]!=r for r in before):
            raise RuntimeError('Prior notifications changed')
        # Same-day replay must not create another record; validates the normal transport idempotency.
        if daily.deliver_event_rows(api,proposal['rows'],day)!=0: raise RuntimeError('Replay created records')
        daily.atomic_json(ROOT/'config/production-activation.json',{**auth,'completed':True,'event_keys':sorted(keys)})
        daily.atomic_json(guard,{'phase':'finished','event_keys':sorted(keys),'messages':2,'readback':'已发送'})
        print('Delivery verified; publishing shared production policy',flush=True)
    daily.atomic_json(out/'result.json',{'completed':True,'messages':2,'recipients':[p['name'] for p in proposal['people']],
        'delivery':'已发送','duplicate_writes':0,'shared':shared})
    print(json.dumps({'completed':True,'shared':shared},ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
