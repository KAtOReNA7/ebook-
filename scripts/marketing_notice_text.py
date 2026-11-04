"""Readable marketing notices based on recorded service dates and amounts."""
from datetime import date
from decimal import Decimal, InvalidOperation
try:
    from .presentation import clean_title
except ImportError:
    from presentation import clean_title


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            return None
        return format(parsed, 'f').rstrip('0').rstrip('.') if '.' in format(parsed, 'f') else format(parsed, 'f')
    except (InvalidOperation, ValueError):
        return None


def service_sentence(event):
    title = clean_title(event.get('source_title') or event.get('title') or '书名待核实')
    try:
        day = date.fromisoformat(str(event.get('service_date', ''))[:10])
        when = f'{day.year}年{day.month}月{day.day}日'
    except ValueError:
        when = '日期待核实'
    people, fee = number(event.get('review_count')), number(event.get('raw_fee'))
    service = str(event.get('service') or '')
    reviews = ('约评' + people + '条') if people is not None else (
        '约评条数待核实' if any(w in service for w in ('开分', '点评', '书评', '好评')) else '开展' + (service or '营销服务'))
    expense = '花费' + fee + '元' if fee is not None else '金额待核实'
    return f'《{title}》于{when}，{reviews}，{expense}。'


def service_content(event):
    parts = [service_sentence(event)]
    if event.get('previous'):
        old = event['previous']
        changes = []
        for field, label, unit in [('review_count', '约评', '条'), ('raw_fee', '费用', '元')]:
            if old.get(field) != event.get(field):
                changes.append(f"{label}：{number(old.get(field)) or '待核实'} → {number(event.get(field)) or '待核实'}{unit}")
        parts.append('变更：' + '；'.join(changes))
    if event.get('statistics_eligible') != ['是']:
        parts.append('金额或人数尚待核实，以上保留原表登记值，暂不计入已确认支出。')
    if event.get('platform'):
        parts.append('平台：' + event['platform'])
    if event.get('service'):
        parts.append('服务：' + event['service'])
    if event.get('archive_url'):
        parts.append('服务留档：' + event['archive_url'])
    if event.get('source_url'):
        parts.append('腾讯费用表：' + event['source_url'])
    return '\n'.join(parts)
