"""Reviewed work-level category labels without changing native platform evidence."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .presentation import split_category
except ImportError:
    from presentation import split_category

ROOT = Path(__file__).resolve().parents[1]


def local_file(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Category evidence must remain inside the project')
    return path


def load_references(root=ROOT):
    policy_path = Path(root) / 'config/work-metadata-policy.json'
    if not policy_path.exists():
        return {}
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    name = policy.get('category_reference_file')
    if policy.get('enabled') is not True or not name:
        return {}
    data = json.loads(local_file(root, name).read_text(encoding='utf-8'))
    if data.get('schema') != 'work.category-references.v1':
        raise ValueError('Unknown reviewed category reference schema')
    return data['references']


def category_fields(book, references, root=ROOT):
    """Prefer two native labels; use a reviewed pair only for its known single tag."""
    raw = book.get('category')
    fields = {'作品分类': raw} if raw else {}
    basis = {'kind': 'native' if raw else 'unavailable', 'platform': '番茄小说', 'raw': raw}
    if not raw or split_category(raw)[1]:
        return fields, basis
    key = 'fanqie:' + str(book['book_id'])
    reference = references.get(key)
    if not reference:
        return fields, basis
    if (reference.get('verified') is not True or reference.get('platform_key') != key
            or reference.get('author') != book.get('author')
            or raw not in reference.get('native_single_categories', [])):
        return fields, basis
    pair = (reference.get('first'), reference.get('second'))
    url = urlsplit(reference.get('source_url', ''))
    if (not all(isinstance(v, str) and v.strip() for v in pair)
            or split_category(reference.get('reference_category')) != pair
            or url.scheme != 'https' or url.hostname != 'weread.qq.com'):
        raise ValueError('Invalid reviewed work category evidence')
    evidence = local_file(root, reference['evidence_path']).read_bytes()
    if hashlib.sha256(evidence).hexdigest() != reference['evidence_sha256']:
        raise ValueError('Reviewed category evidence changed')
    observed = json.loads(evidence)['bookInfo']
    if observed.get('category') != reference['reference_category']:
        raise ValueError('Reference category differs from official evidence')
    # Do not pass 作品分类: the generic renderer would replace the reviewed pair.
    fields = {'分类原文': raw, '一级分类': [pair[0]], '二级分类': [pair[1]]}
    basis = {'kind': 'reviewed_work_reference', 'platform': reference['source_platform'],
             'source_url': reference['source_url'], 'reference_category': reference['reference_category'],
             'native_category': raw, 'evidence_path': reference['evidence_path'],
             'evidence_sha256': reference['evidence_sha256']}
    return fields, basis


def category_source_text(basis):
    if basis['kind'] == 'reviewed_work_reference':
        return ('两级标签参考同一作品的微信读书正版书目（' + basis['reference_category']
                + '），非番茄原生分类；番茄原始标签：' + basis['native_category'] + '；'
                + basis['source_url'])
    return '番茄官方网页' if basis['kind'] == 'native' else '沿用已核实作品资料，番茄来源本次未返回分类'
