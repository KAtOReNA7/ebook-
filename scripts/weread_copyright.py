"""Evidence-backed per-book copyright fallback; never infer from publisher."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def apply_confirmed_copyright(book, root=ROOT):
    result = dict(book)
    path = Path(root)/'config/weread-copyright-confirmations.json'
    entries = json.loads(path.read_text(encoding='utf-8-sig')).get('books',{}) if path.exists() else {}
    entry = entries.get(str(book.get('book_id')))
    if not entry or book.get('copyright_provider'):
        return result
    if entry.get('author') != book.get('author') or entry.get('title') != book.get('title'):
        raise ValueError('Confirmed copyright identity differs; review before using fallback')
    if not entry.get('source') or not entry.get('confirmed_at') or not entry.get('copyright_provider'):
        raise ValueError('Copyright confirmation lacks provenance')
    result.update(copyright_provider=entry['copyright_provider'],
                  platform_copyright_provider=book.get('copyright_provider'),
                  copyright_basis=entry['source'])
    return result
