"""Admission policy: electronic-book services only; uncertainty stays outside Base."""
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def policy():
    path=ROOT/'config/service-scope.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'enabled':False}

def exclusion_reason(row):
    n=row['normalized']
    medium=n.get('service_medium')
    content=str(n.get('service') or n.get('service_content') or '')
    if medium=='audio' or re.search('有声|畅听|听书|收听',content): return 'confirmed_audio'
    if medium!='ebook': return 'medium_unconfirmed'
    return None

def enforce_write(table,row,context=None,creating=False):
    if not policy().get('enabled'): return
    merged={**(context or {}),**row}
    key=str(merged.get('作品唯一键') or '')
    if table in ('项目总表','每日数据','榜单快照') and ':audio:' in key:
        raise ValueError('电子书范围禁止有声作品键写入')
    if table=='项目总表' and row.get('作品形式') not in (None,'','电子书'):
        raise ValueError('电子书范围禁止非电子书项目形式写入')
    if table=='项目总表' and creating and merged.get('作品形式')!='电子书':
        raise ValueError('新项目必须明确电子书形式，不能自动推断')
    if table=='营销服务明细':
        if merged.get('服务对象')!='电子书' or re.search('有声|畅听|听书|收听',str(merged.get('服务内容') or '')):
            raise ValueError('电子书范围禁止有声或未确认服务写入')

def admitted_weread_keys(snapshot,existing):
    keys={k for k in existing if k.startswith('weread:') and ':audio:' not in k}
    for board in snapshot.get('charts',[]):
        keys.update('weread:'+str(r['book_id']) for r in board.get('records',[]))
    return keys
