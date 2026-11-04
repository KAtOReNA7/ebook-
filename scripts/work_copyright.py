"""Apply the user's confirmed ownership of marketing works, retaining platform text."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def apply_work_copyright(book, marketing=False, policy=None):
    if policy is None:
        path = ROOT / 'config/work-copyright-policy.json'
        policy = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not marketing or policy.get('enabled') is not True:
        return dict(book)
    result = dict(book)
    result['platform_copyright_provider'] = book.get('platform_copyright_provider', book.get('copyright_provider'))
    result['copyright_provider'] = policy['marketing_owner']
    result['copyright_basis'] = policy['basis']
    if 'missing_fields' in result:
        result['missing_fields'] = [f for f in result['missing_fields'] if f != 'copyright_provider']
        result['status'] = 'partial' if result['missing_fields'] else 'success'
    return result
