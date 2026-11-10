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
        ahead=sorted(((k,v) for k,v in counts.items() if v>n),key=lambda x:(-x[1],x[0]))
        if not n:
            ahead=ahead[:5]
        lines=[heading,('排在磨铁之前：' if n else '版权方前5名：')+
               ('；'.join(f'No.{1+sum(x>v for x in counts.values())} {k}（{v}本）' for k,v in ahead) if ahead else '无')]
        lines += [f'《{clean_title(r["title"])}》｜{BOARDS[cid]}｜第{r["rank"]}名' for r in sorted(motie,key=lambda r:r['rank'])]
        sections.append('\n'.join(lines)); evidence.append({'board':cid,'counts':dict(counts),'motie_count':n,'motie_rank':rank,'unknown':unknown})
    if len(evidence)!=4: raise ValueError('Four complete WeRead boards required')
    body='\n\n'.join(sections)
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
    from scripts.notification_digest import display_detail
    m=pipeline.get('modules',{})
    marketing_types={'new_marketing_service','new_project_entry','marketing_expense_changed'}
    marketing=[r for r in pending if r['事件类型'] in marketing_types]
    business=[r for r in pending if r['事件类型'] not in marketing_types]
    blocked={'failed','blocked','requires_review','source_review_required'}
    marketing_unverified=any(m.get(k,{}).get('status') in blocked for k in
                            ('tencent','marketing_expense_notifications','project_notifications'))
    lines=['一、营销变化']
    lines += [f'{i}. {display_detail(row)}' for i,row in enumerate(marketing,1)] or [
        '营销变化暂无法核实，不能认定没有变化。' if marketing_unverified else '无']
    lines += ['\n二、业务变化']
    lines += [f'{i}. {display_detail(row)}' for i,row in enumerate(business,1)] or ['无已确认的新业务事件。']
    comparison=m.get('notification_comparison',{})
    if comparison.get('status') in blocked or any(v.get('status','').startswith('waiting_')
                                                for v in comparison.get('modules',{}).values()):
        lines.append('部分业务变化尚缺比较证据，不能认定没有变化。')
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
