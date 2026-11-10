"""No notification to another person before explicit user acceptance."""
import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from .local_settings import OWNER_ID, OWNER_NAME, OWNER_CONFIGURED
except ImportError:
    from local_settings import OWNER_ID, OWNER_NAME, OWNER_CONFIGURED
OWNER_RECIPIENTS = [OWNER_ID]


def notification_recipients(root=None):
    """Resolve after shared restore, never cache a machine's old recipient list."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    path = root / 'config/runtime.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if config.get('production_approved') is not True:
        return list(OWNER_RECIPIENTS)
    approval = config.get('production_authorization', {})
    ids = config.get('recipient_open_ids')
    if (config.get('mode') != 'live' or config.get('deployment_stage') != 'production'
            or approval.get('explicit_owner_request') is not True or not approval.get('request')
            or not approval.get('accepted_on') or not isinstance(ids, list) or not ids
            or any(not isinstance(i, str) or not i.startswith('ou_') for i in ids)
            or OWNER_ID not in ids or len(ids) != len(set(ids))):
        raise ValueError('Invalid explicitly approved production recipient policy')
    return list(ids)


def resend_fingerprint(row):
    fields=('事件唯一键','事件类型','发生日期','规则ID','通知标题','通知正文','判定证据','关联项目','采集批次')
    value={k:row.get(k) for k in fields}
    value['recipient_ids']=sorted(p.get('id','') for p in row.get('收件人',[]))
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def authorized_resend(row, root):
    """Exact, same-day owner-requested resend; never a global recipient expansion."""
    key=str(row.get('事件唯一键',''))
    if ':resend:' not in key or not key.startswith('daily-report:v1:'): return False
    path=root/'config/authorized-report-resends.json'
    if not path.exists(): return False
    policy=json.loads(path.read_text(encoding='utf-8'))
    entry=policy.get('events',{}).get(key,{})
    today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    return (entry.get('explicit_owner_request') is True and not entry.get('completed') and bool(entry.get('request'))
            and entry.get('date')==today==str(row.get('发生日期',''))[:10]
            and entry.get('sha256')==resend_fingerprint(row))


def enforce_recipients(row, root=None):
    if not OWNER_CONFIGURED:
        raise ValueError('Missing verified owner in config/local-system.json; notification transport is disabled.')
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    allowed = notification_recipients(root)
    recipients = row.get('收件人') or []
    ids = [person.get('id') if isinstance(person, dict) else None for person in recipients]
    if (ids and all(isinstance(i,str) and i.startswith('ou_') for i in ids)
            and OWNER_ID in ids and len(ids)==len(set(ids)) and authorized_resend(row,root)):
        return
    if len(ids) != len(set(ids)) or set(ids) != set(allowed):
        raise ValueError('Notification recipients must exactly match the current approved policy; debug is owner-only.')
