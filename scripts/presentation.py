"""Physical display schema, isolated from source identity and alert semantics."""
import re

METRICS={'最新点评人数':'最新点评人数（微信）','最新推荐值':'最新推荐值（微信）',
         '最新今日阅读人数':'最新今日阅读人数（微信）','最新累计阅读人数':'最新累计阅读人数（番茄）',
         '最新书评数量':'最新书评数量（番茄）'}
COPYRIGHT={'微信读书出品':'微信读书','磨铁数盟':'磨铁','读客熊猫君阅读':'读客',
           '理想国imaginist':'理想国','晋江文学城':'晋江','三联书店':'三联',
           '北京市报刊发行局':'北京报刊发行局','中作华文传媒':'中作华文',
           '北京子归科技':'子归科技','北京曙光书阁':'曙光书阁','北京来看科技有限公司':'来看科技',
           '北京看天下网络科技有限公司':'看天下','北京金影科技':'金影科技',
           '江苏酷威文化':'酷威文化','杭州四月天':'四月天','读者杂志社':'读者',
           '财经杂志':'财经','咪咕数媒':'咪咕','博集新媒':'博集','果麦文化':'果麦'}


def clean_title(value):
    if not isinstance(value,str): return value
    # A stack also handles nested Chinese/ASCII parentheses.
    depth=0; result=[]
    for c in value:
        if c in '(（': depth+=1
        elif c in ')）': depth=max(0,depth-1)
        elif depth==0 and c not in '《》': result.append(c)
    return re.sub(r'\s+',' ',''.join(result)).strip()


def short_copyright(value):
    if not isinstance(value,str) or not value.strip(): return None
    value=value.strip()
    if value in COPYRIGHT: return COPYRIGHT[value]
    return re.sub(r'(有限责任公司|有限公司|出版社)$','',value) or value


def split_category(value):
    if not isinstance(value,str) or not value.strip(): return (None,None)
    parts=[x.strip() for x in re.split(r'[-/]',value) if x.strip()]
    if len(parts)>1 and parts[0] in ('种田','宫斗宅斗') and parts[1]=='古代言情':
        return ('古代言情',parts[0])
    return (parts[0],parts[1] if len(parts)>1 else None)


def active(fields):
    return any(n in fields for n in ('版权方','服务摘要','上榜摘要','每日摘要'))


def is_fanqie(row):
    platform=row.get('平台') or row.get('服务平台') or []
    return '番茄' in str(platform) or 'fanqie:' in str(row.get('作品唯一键',''))


def fmt(value, suffix):
    return '待核' if value is None else f'{value:g}{suffix}' if isinstance(value,(int,float)) else str(value)+suffix


def to_remote(table,row,fields,context=None):
    if not active(fields): return dict(row)
    out=dict(row); merged={**(context or {}),**row}
    if '书名' in row:
        if '书名来源原文' in fields:
            raw=row.get('书名来源原文')
            out['书名来源原文']=raw if raw and clean_title(raw)==row['书名'] else row['书名']
        out['书名']=clean_title(row['书名'])
    if '版权方原文' in out:
        raw=out.pop('版权方原文')
        out['版权来源原文']=raw
        out['版权方']=row['版权方'] if '版权方' in row else ([short_copyright(raw)] if short_copyright(raw) else [])
    if '作品分类' in out:
        raw=out.pop('作品分类'); out['分类原文']=raw
        # from_remote adds the logical alias while retaining the physical
        # display labels. Preserve that exact round trip, including reviewed
        # work-level labels that are richer than a single native source tag.
        # A changed logical source category must still derive fresh labels.
        existing_pair=(raw==row.get('分类原文') and
                       '一级分类' in row and '二级分类' in row)
        if not existing_pair:
            one,two=split_category(raw)
            out['一级分类']=[one] if one else []
            out['二级分类']=[two] if two else []
    if table=='项目总表':
        for old,new in METRICS.items():
            if old in out:
                if old=='最新点评人数' and is_fanqie(merged): new='最新点评人数（番茄）'
                out[new]=out.pop(old)
    if table=='营销服务明细' and '服务摘要' in fields:
        if any(k in row for k in ('服务内容','邀评人数','原始行总金额','服务明细唯一键')):
            out['服务摘要']='｜'.join([str(merged.get('服务内容') or '服务待核'),fmt(merged.get('邀评人数'),'人'),fmt(merged.get('原始行总金额'),'元')])
        if '书名' not in out and '原始书名' in row:
            out['书名']=clean_title(row['原始书名']); out['书名来源原文']=row['原始书名']
    if table=='榜单快照' and '上榜摘要' in fields:
        if any(k in row for k in ('榜单','名次','榜单快照唯一键')):
            board=merged.get('榜单') or []
            board='、'.join(board) if isinstance(board,list) else str(board)
            rank=merged.get('名次')
            out['上榜摘要']=f'{board}｜第{rank:g}名' if isinstance(rank,(float,int)) else f'{board}｜榜单不完整'
    if table=='每日数据' and '每日摘要' in fields:
        if any(k in row for k in ('日数据唯一键','点评人数','今日阅读人数','累计阅读人数')):
            parts=['番茄' if is_fanqie(merged) else '微信',fmt(merged.get('点评人数'),'人点评')]
            if not is_fanqie(merged):
                unopened = merged.get('采集状态') == ['未开分'] or '未开分' in str(merged.get('缺失字段与原因') or '')
                parts.append('未开分' if merged.get('推荐值') is None and unopened else fmt(merged.get('推荐值'),'%推荐'))
            parts.append(fmt(merged.get('累计阅读人数'),'人读过') if is_fanqie(merged) else fmt(merged.get('今日阅读人数'),'人今日阅读'))
            out['每日摘要']='｜'.join(parts)
    return out


def from_remote(table,row,fields):
    if not active(fields): return dict(row)
    out=dict(row)
    if row.get('书名来源原文') is not None and clean_title(row['书名来源原文'])==row.get('书名'):
        out['书名']=row['书名来源原文']
    if '版权来源原文' in row: out['版权方原文']=row['版权来源原文']
    if '分类原文' in row: out['作品分类']=row['分类原文']
    if table=='项目总表':
        for old,new in METRICS.items():
            if old=='最新点评人数' and is_fanqie(row): new='最新点评人数（番茄）'
            if new in row: out[old]=row[new]
    return out
