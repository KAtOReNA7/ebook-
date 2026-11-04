"""Reviewed work-level grouping across original service media.

Only verified source-title/platform/service-medium -> electronic-book identities
may merge. Default plans locally; --apply --plan uses fresh full reads and exact
plan equality. No deletion, source rewrites, historical edits or blind retry.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path

try:
    from .feishu_io import Base, ROOT, same, save
    from .sync_marketing import (identities_from_file, normalize_title, unique_index,
                                identity_project_key, identity_tracking_medium, PLATFORMS)
    from . import merge_project_aliases as prior
except ImportError:
    from feishu_io import Base, ROOT, same, save
    from sync_marketing import (identities_from_file, normalize_title, unique_index,
                               identity_project_key, identity_tracking_medium, PLATFORMS)
    import merge_project_aliases as prior

MODEL = ROOT / 'config' / 'project-model.json'


def preserved_services(rows):
    """Every source/service field is immutable; only work links may change."""
    unique_index(rows, 'record_id')
    return {r['record_id']: {k: v for k, v in r.items() if k not in ('作品唯一键', '关联项目')}
            for r in rows}


def build_plan(projects, services, identities, targets=None):
    pindex = unique_index(projects, '作品唯一键')
    unique_index(projects, 'record_id')
    unique_index(services, '服务明细唯一键')
    source_groups = defaultdict(dict)
    target_identities = {}
    source_targets = {}
    allowed = set(targets) if targets else None
    for (platform, medium, title), identity in identities.items():
        if identity.get('verified') is not True:
            continue
        if platform not in PLATFORMS or identity_tracking_medium(identity, medium) != 'ebook':
            continue
        key = identity_project_key(platform, medium, identity)
        if allowed is not None and key not in allowed:
            continue
        if not str(identity['book_id']).isdigit() or not identity.get('title') or not identity.get('detail_url'):
            raise ValueError('Incomplete verified electronic-book identity')
        found = [p for p in projects
                 if p['作品唯一键'].startswith('pending:' + platform + ':' + medium + ':')
                 and normalize_title(p.get('书名')) == title and p.get('监测范围') != ['别名留档']]
        if len(found) > 1:
            raise ValueError('Ambiguous pending source: ' + title)
        for p in found:
            rid = p['record_id']
            if source_targets.get(rid, key) != key:
                raise ValueError('One source project maps to conflicting verified works')
            source_targets[rid] = key
            source_groups[key][rid] = p
            target_identities.setdefault(key, identity)

    plan = {'schema_version': 'project.work-merge-plan.v1', 'groups': [],
            'project_updates': {}, 'service_updates': {}, 'preconditions': {},
            'targets': sorted(allowed) if allowed is not None else None,
            'project_count': len(projects), 'service_count': len(services),
            'financial_snapshot': prior.financial_snapshot(services),
            'financial_totals': prior.totals(services),
            'preserved_service_fields': preserved_services(services)}
    used = set()
    for key, source_index in sorted(source_groups.items()):
        sources = list(source_index.values())
        canonical = pindex.get(key)
        if canonical is not None:
            if str(canonical.get('平台作品ID')) != key.split(':', 1)[1]:
                raise ValueError('Canonical platform ID mismatch')
        else:
            # Deterministic selection is part of the reviewable plan, never an
            # unreviewed deletion/recreation. Keep the earliest real record.
            canonical = min(sources, key=lambda p: (prior.day(p.get('首次进入项目总表')), p['record_id']))
        members = {p['record_id']: p for p in sources + [canonical]}
        if used & set(members):
            raise ValueError('Project belongs to multiple work merges')
        used.update(members)
        keys = {p['作品唯一键'] for p in members.values()}
        attached = [s for s in services if s.get('作品唯一键') in keys
                    or set(prior.links(s.get('关联项目'))) & set(members)]
        for service in attached:
            source = pindex.get(service.get('作品唯一键'))
            if source is None or source['record_id'] not in members:
                raise ValueError('Service key/linked project conflict')
            if prior.links(service.get('关联项目')) != [source['record_id']]:
                raise ValueError('Service must link exactly to keyed source project')
        for rid, project in members.items():
            expected = sorted(s['record_id'] for s in attached if s['作品唯一键'] == project['作品唯一键'])
            if prior.links(project.get('营销服务明细')) != expected:
                raise ValueError('Reverse service links differ from complete source read')
        if not attached:
            raise ValueError('Cannot merge a work without service evidence')
        starts = [p['首次进入项目总表'] for p in members.values() if p.get('首次进入项目总表')]
        deadlines = [p['追踪截止日'] for p in members.values() if p.get('追踪截止日')]
        if not starts or not deadlines:
            raise ValueError('Missing original project tracking dates')
        start, end = min(starts, key=prior.day), max(deadlines, key=prior.day)
        if prior.day(end) < prior.day(start) + timedelta(days=29):
            raise ValueError('Existing deadline does not cover original 30 days')
        identity = target_identities[key]
        note = '\n按用户确认的作品口径归并：' + '；'.join(p['书名'] + ' [' + p['record_id'] + ']' for p in sources)
        note += ' → ' + key + '；追踪电子书，每笔服务形式/金额/原日期保留。'
        plan['project_updates'][canonical['record_id']] = {
            '作品唯一键': key, '平台作品ID': str(identity['book_id']), '作品主页': identity['detail_url'],
            '书名': identity['title'], '作品形式': '电子书', '监测范围': ['营销项目'],
            '首次进入项目总表': start, '追踪截止日': end,
            '平台匹配状态': '已匹配：已核实作品；各服务形式合并追踪电子书',
            '证据与备注': (canonical.get('证据与备注') or '') + note}
        if identity.get('author'):
            plan['project_updates'][canonical['record_id']]['作者'] = identity['author']
        aliases = [p for p in sources if p['record_id'] != canonical['record_id']]
        for alias in aliases:
            plan['project_updates'][alias['record_id']] = {
                '监测范围': ['别名留档'], '平台匹配状态': '别名留档：已归并至 ' + key,
                '证据与备注': (alias.get('证据与备注') or '') + '\n作品归并留档：服务关联转入 '
                             + key + ' [' + canonical['record_id'] + ']；原记录键、日期及历史关联保留。'}
        for service in attached:
            desired = {'作品唯一键': key, '关联项目': [{'id': canonical['record_id']}]}
            if any(not same(v, service.get(k)) for k, v in desired.items()):
                plan['service_updates'][service['record_id']] = desired
        plan['groups'].append({'target_key': key, 'canonical_record_id': canonical['record_id'],
            'canonical_existing': key in pindex, 'aliases': [
                {'record_id': p['record_id'], 'old_key': p['作品唯一键'], 'title': p['书名']} for p in aliases],
            'service_record_ids': sorted(s['record_id'] for s in attached), 'service_count': len(attached),
            'service_media': sorted({str(s.get('服务对象', '未记录')) for s in attached}),
            'first_registered': start, 'tracking_deadline': end})
        plan['preconditions'][key] = {
            'projects': {rid: {**prior.project_snapshot(p), '首次上榜采集日': p.get('首次上榜采集日')}
                         for rid, p in members.items()},
            'services': {s['record_id']: s for s in attached}}
    plan['plan_digest'] = prior.digest(plan)
    return plan


def readback(plan, projects, services):
    prior.readback(plan, projects, services)
    if preserved_services(services) != plan['preserved_service_fields']:
        raise RuntimeError('Source/service form/content conservation failed')
    byid = unique_index(projects, 'record_id')
    for group in plan['preconditions'].values():
        for rid, before in group['projects'].items():
            changed = plan['project_updates'].get(rid, {})
            for field in ('首次上榜采集日', '首次进入项目总表', '追踪截止日'):
                if field not in changed and byid[rid].get(field) != before.get(field):
                    raise RuntimeError('Original tracking date changed: ' + field)


def apply_plan(api, approved, identities):
    projects, services = api.read('项目总表'), api.read('营销服务明细')
    fresh = build_plan(projects, services, identities, approved['plan']['targets'])
    if fresh != approved['plan']:
        raise RuntimeError('Reviewed plan is stale or changed; no writes')
    if not fresh['groups']:
        return
    fields = api.manifest['tables']['项目总表']['field_readback']['fields']
    field = next(f for f in fields if f['name'] == '监测范围')
    if '别名留档' not in {o['name'] for o in field['options']}:
        raise RuntimeError('Manifest lacks 别名留档 option')
    path = api.receipt_dir / 'link-state.json'
    state = {'phase': 'unknown_until_full_readback', 'operation': 'work_project_merge',
             'plan_digest': fresh['plan_digest']}
    save(path, state)
    for table, updates in [('营销服务明细', fresh['service_updates']), ('项目总表', fresh['project_updates'])]:
        items = list(updates.items())
        for offset in range(0, len(items), 200):
            # The one guard covers every batch. Any uncertain batch stops the
            # entire operation until read-only reconciliation; no batch retry.
            api.call('+record-batch-update', table, {'update_records': dict(items[offset:offset + 200])})
    readback(fresh, api.read('项目总表'), api.read('营销服务明细'))
    save(path, {**state, 'phase': 'finished', 'readback': 'passed',
                'source_conservation': 'passed', 'dates_and_history_preserved': True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity-map', action='append')
    parser.add_argument('--target', action='append')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.plan:
        parser.error('--apply requires the exact reviewed --plan PATH')
    if not args.apply and not args.identity_map:
        parser.error('planning requires --identity-map')
    prior.assert_no_unknown()
    directory = ROOT / 'receipts' / datetime.now(prior.TZ).date().isoformat() / ('work-merge-' + datetime.now(prior.TZ).strftime('%H%M%S-%f'))
    api = Base(directory)
    if args.apply:
        approved = json.loads(args.plan.read_text(encoding='utf-8'))
        paths = [e['path'] for e in approved['identity_files']]
        if prior.evidence_files(paths) != approved['identity_files'] or prior.evidence_files([MODEL]) != approved['project_model']:
            raise RuntimeError('Identity evidence or approved project model changed')
        apply_plan(api, approved, identities_from_file(paths))
        print(json.dumps({'applied': True, 'receipt_dir': str(directory)}, ensure_ascii=False))
    else:
        identities = identities_from_file(args.identity_map)
        plan = build_plan(api.read('项目总表'), api.read('营销服务明细'), identities, args.target)
        save(directory / 'plan.json', {'identity_files': prior.evidence_files(args.identity_map),
                                      'project_model': prior.evidence_files([MODEL]), 'plan': plan})
        print(json.dumps({'applied': False, 'plan_path': str(directory / 'plan.json'),
                          'groups': plan['groups'], 'project_updates': len(plan['project_updates']),
                          'service_updates': len(plan['service_updates'])}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
