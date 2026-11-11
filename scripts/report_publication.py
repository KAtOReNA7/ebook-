"""Fail-closed publication checks; incomplete results are for operator review only."""
from pathlib import Path
import json

REQUIRED = ('project_initialization','tencent','weread','fanqie','fanqie_rising',
            'publisher_recent','project_notifications','marketing_expense_notifications',
            'notification_comparison')
BOARDS = {'微信飙升Top50':50,'微信新书Top50':50,'微信女生小说飙升Top50':50,
          '微信女生小说新书Top50':50,'番茄出版飙升榜':100}


def verified_count_pair(left, right):
    return type(left) is int and type(right) is int and left >= 0 and left == right


def source_issues(pipeline):
    modules=pipeline.get('modules',{}); issues=[]
    for name in REQUIRED:
        m=modules.get(name,{})
        allowed={'completed','partial'} if name in {'tencent','weread','fanqie'} else {'completed'}
        if m.get('status') not in allowed: issues.append(name+': not successfully completed')
        if m.get('identity_mutation_unknown'): issues.append(name+': unknown identity mutation')
    t=modules.get('tencent',{})
    if t.get('import_readback')!='passed' or t.get('import',{}).get('reconciliation')!='passed' or t.get('source_changes'):
        issues.append('tencent: source/expense reconciliation incomplete')
    c=modules.get('weread',{}).get('coverage',{})
    if (not c.get('requested_books') or c.get('successful_books')!=c.get('requested_books')
            or c.get('incomplete_boards') or c.get('failed_book_ids') or c.get('missing_book_ids')):
        issues.append('weread: requested observations or boards incomplete')
    f=modules.get('fanqie',{})
    if f.get('readback')!='passed' or not verified_count_pair(f.get('projects'),f.get('observations')):
        issues.append('fanqie: requested observations not verified')
    r=modules.get('fanqie_rising',{})
    if r.get('complete') is not True or r.get('count')!=100 or r.get('readback')!='passed':
        issues.append('fanqie_rising: complete Top100 sync not verified')
    p=modules.get('publisher_recent',{})
    if (p.get('source',{}).get('complete') is not True or p.get('readback')!='passed'
            or p.get('metric_errors') or not verified_count_pair(p.get('sampled_books'),p.get('active_targets'))):
        issues.append('publisher_recent: source or tracked observations incomplete')
    comparisons=modules.get('notification_comparison',{}).get('modules',{})
    for name in ('weread','fanqie_rising'):
        if comparisons.get(name,{}).get('status') not in {'handled_by_source','compared'}:
            issues.append(name+': business comparison unverified')
    return issues


def readback_issues(result):
    issues=[]
    if result.get('readback')!='passed' or result.get('all_keys_unique') is not True or result.get('service_links_valid') is not True:
        issues.append('Base: full readback, unique keys or links unverified')
    if result.get('fee_reconciliation')!='passed': issues.append('Base: expense reconciliation unverified')
    if result.get('complete_board_counts')!=BOARDS: issues.append('Base: five complete boards not verified')
    if result.get('source_contract',{}).get('status')!='passed':issues.append('Base: per-record source comparison unverified')
    if result.get('native_workflow_verification',{}).get('status')!='passed':issues.append('Native workflow or prior delivery unverified')
    return issues


def evidence_issues(evidence):
    issues=[]; boards=evidence.get('copyright_ranking')
    ids={'rising','newbook','novel_female_rising','newbook_novel_female'}
    if (not isinstance(boards,list) or len(boards)!=4 or {b.get('board') for b in boards}!=ids
            or any(b.get('unknown') or sum(b.get('counts',{}).values())!=50 for b in boards)):
        issues.append('WeRead: complete copyright rankings unverified')
    f=evidence.get('fanqie_publication_rising',{})
    if f.get('complete') is not True or f.get('ranking_count')!=100 or f.get('unknown'):
        issues.append('Fanqie: complete copyright rankings unverified')
    return issues


def publication_hold(root):
    path=Path(root)/'config/runtime.json'
    runtime=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    return runtime.get('report_publication_hold',{}).get('active') is True
