"""No notification to another person before explicit user acceptance."""
import json
from pathlib import Path

try:
    from .local_settings import OWNER_ID, OWNER_NAME, OWNER_CONFIGURED
except ImportError:
    from local_settings import OWNER_ID, OWNER_NAME, OWNER_CONFIGURED
OWNER_RECIPIENTS = [OWNER_ID]


def enforce_recipients(row, root=None):
    if not OWNER_CONFIGURED:
        raise ValueError('Missing verified owner in config/local-system.json; notification transport is disabled.')
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    path = root / 'config/runtime.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    allowed = OWNER_RECIPIENTS
    if config.get('production_approved') is True:
        allowed = config.get('recipient_open_ids') or OWNER_RECIPIENTS
    recipients = row.get('收件人') or []
    ids = [person.get('id') if isinstance(person, dict) else None for person in recipients]
    if len(ids) != len(set(ids)) or set(ids) != set(allowed):
        raise ValueError('未经用户明确正式通过，通知只能发给' + OWNER_NAME + '本人；禁止其他收件人或空收件人。')
