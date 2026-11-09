"""Two owner-only daily reports, replacing per-category delivery."""
from collections import Counter
from datetime import date
import json
from pathlib import Path
from scripts.presentation import short_copyright, clean_title
from scripts.weread_copyright import apply_confirmed_copyright
from scripts.resume_daily_notifications import complete_weread, weread_path

BOARDS = {'rising':'飙升榜Top50','newbook':'新书榜Top50',
          'novel_female_rising':'女生小说飙升榜Top50','newbook_novel_female':'女生小说新书榜Top50'}


def enabled(root):
    p=Path(root)/'config/daily-notification-format.json'
    return p.exists() and json.loads(p.read_text(encoding='utf-8')).get('mode')=='two_daily_reports'


def title(day, label):
    d=date.fromisoformat(day)
    return f'《{d.year}年{d.month}月{d.day}日{label}》'


def copyright_summary(snapshot, root):
    books={str(b['book_id']):apply_confirmed_copyright(b,root) for b in snapshot['books']}
    sections=[]; evidence=[]
    for chart in snapshot['charts']:
        cid=chart['chart_id']
        if cid not in BOARDS: continue
        rows=chart['records']; counts=Counter(); motie=[]; unknown=[]
        if len(rows)!=50 or {r['rank'] for r in rows}!=set(range(1,51)) or len({str(r['book_id']) for r in rows})!=50:
            raise ValueError('Incomplete board: '+cid)
        for r in rows:
            book=books.get(str(r['book_id']),{})
            provider=short_copyright(book.get('copyright_provider'))
            if not provider: unknown.append(r['book_id']); continue
            counts[provider]+=1
            if provider=='磨铁': motie.append(r)
        n=counts.get('磨铁',0)
        rank=1+sum(v>n for v in counts.values()) if n else None
        heading=f'{BOARDS[cid]}：磨铁上榜{n}本，'+(f'版权方排名No.{rank}' if rank else '无上榜作品，不参与排名')
        if n and sum(v==n for v in counts.values())>1: heading+='（并列）'
        if unknown: heading+=f'；{len(unknown)}本版权待核，以上为已核实口径'
        ahead=sorted(((k,v) for k,v in counts.items() if v>n),key=lambda x:(-x[1],x[0])) if n else []
        lines=[heading,'排在磨铁之前：'+('；'.join(f'No.{1+sum(x>v for x in counts.values())} {k}（{v}本）' for k,v in ahead) if ahead else '无' if n else '不适用')]
        lines += [f'《{clean_title(r["title"])}》｜{BOARDS[cid]}｜第{r["rank"]}名' for r in sorted(motie,key=lambda r:r['rank'])]
        sections.append('\n'.join(lines)); evidence.append({'board':cid,'counts':dict(counts),'motie_count':n,'motie_rank':rank,'unknown':unknown})
    if len(evidence)!=4: raise ValueError('Four complete WeRead boards required')
    body='按每个榜单的上榜本数排列版权方；相同本数并列，后续名次跳位。跨榜分别统计。\n实际采样：'+snapshot['started_at']+'\n\n'+'\n\n'.join(sections)
    return body,evidence


def event_row(day,kind,body,evidence,recipients,test=False):
    label='微信读书汇总' if kind=='weread' else '电子书明细'
    if len(body.encode('utf-8'))>24000: raise ValueError('Report exceeds message size; no truncation or extra messages')
    return {'事件唯一键':f'daily-report:v1:{day}:{kind}'+(':test' if test else ''),
            '事件类型':'日报测试' if test else label,'关联项目':[], '发生日期':day+' 00:00',
            '规则ID':'v1:daily_report:'+kind,'判定证据':json.dumps(evidence,ensure_ascii=False,sort_keys=True),
            '通知标题':title(day,label),'通知正文':('【本次为你授权的一条格式测试】\n' if test else '')+body,
            '收件人':[{'id':r} for r in recipients],'通知状态':['待发送'],'采集批次':'daily-report:'+day}


def ebook_body(pipeline, pending, daily_rows):
    m=pipeline.get('modules',{}); counts=Counter()
    for row in daily_rows:
        values=row.get('平台',[]); values=[values] if isinstance(values,str) else values
        for v in values: counts[v if isinstance(v,str) else v.get('name','未知')]+=1
    t=m.get('tencent',{}); imp=t.get('import',{}); w=m.get('weread',{}); f=m.get('fanqie',{}); r=m.get('fanqie_rising',{}); p=m.get('publisher_recent',{})
    lines=[f'日期：{pipeline["date"]}',
           '一、今日采集与同步',f'已入表日数据：{len(daily_rows)}条（'+ '、'.join(f'{k}{v}条' for k,v in counts.items())+'）',
           f'腾讯费用表：{t.get("status","待核")}；纳入电子书服务{imp.get("service_rows","待核")}条；费用核对：{imp.get("reconciliation","待核")}；确认金额{imp.get("confirmed_fee_total","待核")}元。',
           f'微信读书：{w.get("status","待核")}；未完整榜单：{w.get("coverage",{}).get("incomplete_boards","待核")}。',
           f'番茄作品：已采样{f.get("observations","待核")}本；覆盖状态{f.get("status","待核")}。',
           f'番茄出版飙升榜：{r.get("count","待核")}名；完整：{r.get("complete",False)}。',
           f'版权方最近上架：新增{p.get("new_entries","待核")}本；追踪{p.get("active_targets","待核")}本；本次采样{p.get("sampled_books","待核")}本。',
           '\n二、业务变化']
    from scripts.notification_digest import display_detail
    lines += [f'{i}. {display_detail(row)}' for i,row in enumerate(pending,1)] or ['本次没有可确认的新业务事件。']
    comparison=m.get('notification_comparison',{})
    gaps=[]
    for name, module in m.items():
        if module.get('status') in {'failed','blocked','requires_review','source_review_required'}: gaps.append(name+'：'+str(module.get('reason') or module.get('status')))
    for name, module in comparison.get('modules',{}).items():
        if module.get('status','').startswith('waiting_'): gaps.append(name+'：等待比较证据，不能认定没有变化')
    if imp.get('pending_project_groups'): gaps.append(f'{imp["pending_project_groups"]}本营销作品官方身份仍待核实')
    missing=w.get('coverage',{}).get('missing_metric_fields',{})
    if missing: gaps.append(f'微信{len(missing)}本存在未开分或指标缺项，保留空值')
    lines += ['\n三、待核实事项', '\n'.join(gaps) if gaps else '无新增待处理异常。',
              '\n数据按实际采样时刻记录；未开分及缺失值不填零，不补造历史。']
    return '\n'.join(lines)


def flush(args,pipeline,runtime,root,api,writer):
    from scripts.daily_run import atomic_json, RECIPIENTS, shanghai_day
    from scripts.notification_digest import consumed_ids, load
    from scripts.initialize_manual_projects import unknown_guard_paths
    day=pipeline['date']; directory=Path(root)/'runs'/day
    if day!=shanghai_day(): raise ValueError('Historical report requires explicit recovery entry')
    if unknown_guard_paths(Path(root)): return {'status':'requires_review','reason':'Unknown mutation; reconcile first','write_count':0}
    records=api.read('通知事件',[['发生日期','==',f'ExactDate({day})']]); consumed=consumed_ids(records)
    queue=load(directory/'notification-digest-queue.json',{'events':{}})
    pending=[r for k,r in queue['events'].items() if k not in consumed]
    daily_rows=api.read('每日数据',[['数据日期','==',f'ExactDate({day})']])
    if not args.dry_run:
        atomic_json(directory/'daily-report-context.json',pipeline)
    rows=[event_row(day,'ebook',ebook_body(pipeline,pending,daily_rows),{'member_event_ids':[r['事件唯一键'] for r in pending]},RECIPIENTS)]
    try:
        snapshot=complete_weread(weread_path(Path(root),day),day)
        body,evidence=copyright_summary(snapshot,root)
    except (ValueError,OSError) as exc:
        body='四榜汇总暂无法完整核实：'+str(exc)+'\n未将缺榜视为零上榜。'; evidence={'incomplete':True,'reason':str(exc)}
    rows.append(event_row(day,'weread',body,{'copyright_ranking':evidence,'member_event_ids':[]},RECIPIENTS))
    existing={r['事件唯一键'] for r in records}; missing=[r for r in rows if r['事件唯一键'] not in existing]
    if rows[0]['事件唯一键'] in existing and pending:
        return {'status':'requires_review','reason':'Daily report already sent; additional events retained, no duplicate report','write_count':0}
    atomic_json(directory/'daily-reports-preview.json',rows)
    if args.dry_run or not runtime['notifications_enabled']: return {'status':'dry_run','planned_messages':len(missing),'write_count':0}
    guard=directory/'daily-reports.apply-state.json'
    if missing:
        atomic_json(guard,{'phase':'events_in_progress','keys':[r['事件唯一键'] for r in missing]})
        writer(api,missing,day)
    atomic_json(guard,{'phase':'finished','created':len(missing)})
    atomic_json(directory/'notification-digest.apply-state.json',{'phase':'finished','consumed_ids':sorted(queue['events']),'messages_created_this_run':len(missing)})
    return {'status':'completed','messages_created':len(missing),'write_count':len(missing),'format':'two_daily_reports'}
