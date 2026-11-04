"""Reviewed merge of the three verified WeRead marketing alias groups.

Default is read-only planning. Apply requires --apply --plan PATH; no deletes,
no mutation retries, and no service amount/source edits.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

try:
    from .feishu_io import Base, ROOT, same, save
    from .sync_marketing import identities_from_file, normalize_title, unique_index
except ImportError:
    from feishu_io import Base, ROOT, same, save
    from sync_marketing import identities_from_file, normalize_title, unique_index

TARGETS = ('weread:3300225462', 'weread:3300235465', 'weread:3300193302')
PREFERRED = {'weread:3300193302': 'reczz28V47quenUw'}
PROJECT_FIELDS = ('作品唯一键', '书名', '平台作品ID', '作品主页', '作者', '作品形式',
                  '平台', '监测范围', '首次进入项目总表', '追踪截止日', '平台匹配状态',
                  '证据与备注', '营销服务明细', '每日记录', '上榜记录', '通知记录')
MONEY_FIELDS = ('原始行总金额', '已确认分摊金额', '邀评人数')
TZ = timezone(timedelta(hours=8))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def links(value):
    return sorted(str(x['id']) if isinstance(x, dict) else str(x) for x in (value or []))


def day(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Missing genuine project tracking date')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).date()


def project_snapshot(row):
    out = {k: row.get(k) for k in PROJECT_FIELDS}
    for k in ('营销服务明细', '每日记录', '上榜记录', '通知记录'):
        out[k] = links(out[k])
    return out


def financial_snapshot(services):
    unique_index(services, '服务明细唯一键')
    return {r['record_id']: {k: r.get(k) for k in ('服务明细唯一键',) + MONEY_FIELDS}
            for r in services}


def totals(services):
    return {k: str(sum((Decimal(str(r.get(k) or 0)) for r in services), Decimal(0)))
            for k in MONEY_FIELDS}


def evidence_files(paths):
    return [{'path': str(Path(p).resolve()),
             'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in paths]


def build_plan(projects, services, identities, targets=TARGETS, preferred=PREFERRED):
    """Pure planning, also used to reject stale/tampered plans before writes."""
    pindex = unique_index(projects, '作品唯一键')
    unique_index(projects, 'record_id')
    unique_index(services, 'record_id')
    money = financial_snapshot(services)
    by_target = defaultdict(list)
    for (platform, medium, title), identity in identities.items():
        if identity.get('verified') is not True or platform != 'weread' or medium != 'ebook':
            continue
        target = 'weread:' + str(identity['book_id'])
        if target in targets:
            by_target[target].append((title, identity))
    result = {'schema_version': 'project.alias-merge-plan.v1', 'groups': [],
              'project_updates': {}, 'service_updates': {}, 'preconditions': {},
              'project_count': len(projects),
              'service_count': len(services), 'financial_snapshot': money,
              'financial_totals': totals(services)}
    used = set()
    for target in targets:
        matches = by_target.get(target, [])
        if not matches:
            raise ValueError('Target lacks verified source identity: ' + target)
        sources = []
        for title, identity in matches:
            found = [p for p in projects if p['作品唯一键'].startswith('pending:weread:ebook:')
                     and normalize_title(p.get('书名')) == title
                     and p.get('监测范围') != ['别名留档']]
            if len(found) != 1:
                raise ValueError('Expected one unarchived pending source: ' + title)
            sources.append(found[0])
        if len({p['record_id'] for p in sources}) != len(sources):
            raise ValueError('One pending record matched multiple identities')
        canonical = pindex.get(target)
        if canonical:
            if str(canonical.get('平台作品ID')) != target.split(':', 1)[1]:
                raise ValueError('Canonical platform ID mismatch')
        else:
            selected = preferred.get(target)
            canonical = next((p for p in sources if p['record_id'] == selected), None)
            if canonical is None or len(sources) < 2:
                raise ValueError('No explicit canonical record for pending alias pair')
        members = {p['record_id']: p for p in sources + [canonical]}
        if used & set(members):
            raise ValueError('Project participates in multiple merge groups')
        used.update(members)
        keys = {p['作品唯一键'] for p in members.values()}
        attached = [s for s in services if s.get('作品唯一键') in keys
                    or set(links(s.get('关联项目'))) & set(members)]
        # Every service must already agree in both directions. Never repair an
        # unrelated or ambiguous link while doing an alias merge.
        for service in attached:
            source = pindex.get(service.get('作品唯一键'))
            if source is None or source['record_id'] not in members:
                raise ValueError('Service key/linked project conflict')
            if links(service.get('关联项目')) != [source['record_id']]:
                raise ValueError('Service must link exactly to its keyed source project')
        for pid, p in members.items():
            actual = sorted(s['record_id'] for s in attached if s['作品唯一键'] == p['作品唯一键'])
            if links(p.get('营销服务明细')) != actual:
                raise ValueError('Reverse service links differ from complete service read')
        if not attached:
            raise ValueError('No service evidence to merge')
        starts = [p['首次进入项目总表'] for p in members.values() if p.get('首次进入项目总表')]
        if not starts:
            raise ValueError('No genuine marketing first-entry date')
        start = min(starts, key=day)
        deadlines = [p['追踪截止日'] for p in members.values() if p.get('追踪截止日')]
        if not deadlines:
            raise ValueError('No existing tracking deadline')
        deadline = max(deadlines, key=day)
        if day(deadline) < day(start) + timedelta(days=29):
            raise ValueError('Existing deadline does not cover 30 days; review dates')
        identity = matches[0][1]
        proof = '；'.join(p['书名'] + ' [' + p['record_id'] + ']' for p in sources)
        annotation = '\n经核实平台ID合并营销别名：' + proof + ' → ' + target
        canonical_fields = {'作品唯一键': target, '平台作品ID': str(identity['book_id']),
                            '作品主页': identity['detail_url'], '书名': identity['title'],
                            '作者': identity['author'], '作品形式': '电子书', '监测范围': ['营销项目'],
                            '首次进入项目总表': start, '追踪截止日': deadline,
                            '平台匹配状态': '已匹配：核实同一平台作品ID；营销别名合并',
                            '证据与备注': (canonical.get('证据与备注') or '') + annotation}
        result['project_updates'][canonical['record_id']] = canonical_fields
        aliases = [p for p in sources if p['record_id'] != canonical['record_id']]
        for alias in aliases:
            result['project_updates'][alias['record_id']] = {
                '监测范围': ['别名留档'],
                '平台匹配状态': '别名留档：已合并至 ' + target,
                '证据与备注': (alias.get('证据与备注') or '') + '\n别名合并留档：营销服务转入 '
                              + target + ' [' + canonical['record_id'] + ']；保留原项目键和原始记录。'}
        for service in attached:
            desired = {'作品唯一键': target, '关联项目': [{'id': canonical['record_id']}]}
            if any(not same(v, service.get(k)) for k, v in desired.items()):
                result['service_updates'][service['record_id']] = desired
        group = {'target_key': target, 'canonical_record_id': canonical['record_id'],
                 'canonical_existing': target in pindex,
                 'aliases': [{'record_id': p['record_id'], 'old_key': p['作品唯一键'], 'title': p['书名']}
                             for p in aliases], 'service_record_ids': sorted(s['record_id'] for s in attached),
                 'service_count': len(attached), 'financial_totals': totals(attached)}
        result['groups'].append(group)
        result['preconditions'][target] = {
            'projects': {pid: project_snapshot(p) for pid, p in members.items()},
            'services': {s['record_id']: s for s in attached}}
    result['plan_digest'] = digest(result)
    return result


def assert_no_unknown(root=ROOT):
    receipts = Path(root) / 'receipts'
    for pattern in ('**/alias-merge-state.json', '**/link-state.json', '**/*.apply-state.json'):
        for path in receipts.glob(pattern):
            state = json.loads(path.read_text(encoding='utf-8'))
            if state.get('phase') != 'finished':
                raise RuntimeError('Unknown/incomplete mutation; reconcile read-only: ' + str(path))


def readback(plan, projects, services):
    pindex = unique_index(projects, '作品唯一键')
    byid = unique_index(projects, 'record_id')
    sindex = unique_index(services, 'record_id')
    if len(projects) != plan['project_count']:
        raise RuntimeError('Project count changed; no deletion or creation was planned')
    if len(services) != plan['service_count'] or financial_snapshot(services) != plan['financial_snapshot']:
        raise RuntimeError('Service count/source key/amount/people conservation failed')
    if totals(services) != plan['financial_totals']:
        raise RuntimeError('Financial totals changed')
    for pid, updates in plan['project_updates'].items():
        p = byid.get(pid)
        if p is None:
            raise RuntimeError('Project record disappeared')
        for field, wanted in updates.items():
            equal = (day(wanted) == day(p.get(field))) if field in ('首次进入项目总表', '追踪截止日') else same(wanted, p.get(field))
            if not equal:
                raise RuntimeError('Project readback differs: ' + pid + '/' + field)
    for group in plan['groups']:
        canonical = pindex[group['target_key']]
        if canonical['record_id'] != group['canonical_record_id']:
            raise RuntimeError('Canonical record ID changed')
        expected_ids = group['service_record_ids']
        keyed = sorted(s['record_id'] for s in services if s.get('作品唯一键') == group['target_key'])
        if keyed != expected_ids or links(canonical.get('营销服务明细')) != expected_ids:
            raise RuntimeError('Canonical service membership differs')
        for sid in expected_ids:
            if links(sindex[sid].get('关联项目')) != [canonical['record_id']]:
                raise RuntimeError('Service link readback differs')
        for alias in group['aliases']:
            p = byid[alias['record_id']]
            if p['作品唯一键'] != alias['old_key'] or links(p.get('营销服务明细')):
                raise RuntimeError('Alias key changed or services still attached')
        before = plan['preconditions'][group['target_key']]['projects']
        for pid, old in before.items():
            for field in ('每日记录', '上榜记录', '通知记录'):
                if links(byid[pid].get(field)) != old[field]:
                    raise RuntimeError('Existing historical links changed')


def apply_plan(api, approved, identities):
    fresh_projects, fresh_services = api.read('项目总表'), api.read('营销服务明细')
    fresh = build_plan(fresh_projects, fresh_services, identities)
    if fresh != approved['plan']:
        raise RuntimeError('Reviewed plan is stale or changed; no writes, rebuild and review')
    options = next(f['options'] for f in api.manifest['tables']['项目总表']['field_readback']['fields']
                   if f['name'] == '监测范围')
    if '别名留档' not in {x['name'] for x in options}:
        raise RuntimeError('Manifest lacks reviewed 别名留档 option')
    # Reuse the identity-link guard name so ordinary linking also stops after
    # an interrupted alias merge, rather than accidentally continuing it.
    state_path = api.receipt_dir / 'link-state.json'
    state = {'phase': 'unknown_until_full_readback', 'operation': 'project_alias_merge',
             'plan_digest': fresh['plan_digest']}
    save(state_path, state)
    # A timeout/exception deliberately leaves this state unresolved. No retry.
    if fresh['service_updates']:
        api.call('+record-batch-update', '营销服务明细', {'update_records': fresh['service_updates']})
    api.call('+record-batch-update', '项目总表', {'update_records': fresh['project_updates']})
    actual_projects, actual_services = api.read('项目总表'), api.read('营销服务明细')
    readback(fresh, actual_projects, actual_services)
    save(state_path, {**state, 'phase': 'finished', 'readback': 'passed',
                      'financial_conservation': 'passed', 'service_links': 'passed'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity-map', action='append')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.plan:
        parser.error('--apply requires the exact reviewed --plan PATH')
    assert_no_unknown()
    directory = ROOT / 'receipts' / datetime.now(TZ).date().isoformat() / ('alias-merge-' + datetime.now(TZ).strftime('%H%M%S-%f'))
    api = Base(directory)
    if args.apply:
        approved = json.loads(args.plan.read_text(encoding='utf-8'))
        paths = [x['path'] for x in approved['identity_files']]
        if evidence_files(paths) != approved['identity_files']:
            raise RuntimeError('Identity evidence files changed; review again')
        apply_plan(api, approved, identities_from_file(paths))
        print(json.dumps({'applied': True, 'receipt_dir': str(directory)}, ensure_ascii=False))
    else:
        paths = args.identity_map or [str(ROOT / 'research/weread-resolved-next/identity-map.json')]
        plan = build_plan(api.read('项目总表'), api.read('营销服务明细'), identities_from_file(paths))
        wrapped = {'identity_files': evidence_files(paths), 'plan': plan}
        save(directory / 'plan.json', wrapped)
        print(json.dumps({'applied': False, 'plan_path': str(directory / 'plan.json'),
                          'groups': plan['groups'], 'project_updates': len(plan['project_updates']),
                          'service_updates': len(plan['service_updates'])}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
