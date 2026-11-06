"""Read-only local readiness check. Does not query Feishu or start collection."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.feishu_io import resolve_cli
from scripts.initialize_manual_projects import unknown_guard_paths
from scripts.local_settings import load_settings, project_path
from scripts.sync_marketing import sha


def check(root=ROOT):
    root = Path(root)
    required = ['config/local-system.json', 'config/runtime.json', 'config/base-manifest.json',
                'config/tencent-runtime.json', 'config/source-review-overrides.json',
                'config/fanqie-project-identities.json', 'runs/manual-project-registry.json',
                'runs/marketing-expense-registry.json', 'runs/fanqie-milestones.json']
    errors = ['Missing private state: ' + name for name in required if not (root / name).is_file()]
    settings = load_settings(root)
    if not settings.get('owner_open_id', '').startswith('ou_') or settings.get('owner_open_id') == 'ou_unconfigured_owner':
        errors.append('Verified owner is not configured')
    if 'EXAMPLE_SOURCE_ID' in settings.get('tencent_source_url', 'EXAMPLE_SOURCE_ID'):
        errors.append('Actual Tencent source is not configured')
    def read(name):
        path = root / name
        return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    manifest = read('config/base-manifest.json')
    if not manifest.get('tables') or not manifest.get('base', {}).get('base_token'):
        errors.append('Actual Base manifest is not configured')
    for item in read('config/tencent-runtime.json').get('identity_maps', []):
        if not project_path(item, root).is_file():
            errors.append('Missing identity map: ' + item)
    for review in read('config/source-review-overrides.json').get('overrides', []):
        path = project_path(review['evidence_path'], root)
        if not path.is_file() or sha(path.read_bytes()) != review['evidence_sha256']:
            errors.append('Missing or changed service-medium evidence: ' + str(path))
    guards = unknown_guard_paths(root)
    if guards:
        errors.append('Unresolved mutation guards: ' + str(len(guards)))
    try:
        cli = resolve_cli()
    except (FileNotFoundError, ValueError) as exc:
        cli = None
        errors.append(str(exc))
    runtime = read('config/runtime.json')
    if runtime.get('production_approved') is True:
        errors.append('Production approval is outside the current debugging scope')
    if runtime.get('notifications_enabled') and runtime.get('recipient_open_ids') != [settings.get('owner_open_id')]:
        errors.append('Debug recipients differ from the configured owner')
    ranking = read('config/fanqie-ranking.json')
    paused = ranking.get('status') == 'user_paused' and not ranking.get('enabled')
    resumed = (ranking.get('status') == 'web_daily' and ranking.get('enabled') is True
               and ranking.get('resumed_by_owner') is True and ranking.get('notifications_enabled') is False)
    if not (paused or resumed):
        errors.append('Fanqie ranking requires paused or owner-authorized web_daily mode; notification gate stays closed')
    return {'status': 'ready_for_read_only_review' if not errors else 'needs_setup',
            'python': sys.version.split()[0], 'cli': cli, 'errors': errors,
            'notifications_enabled': runtime.get('notifications_enabled', False),
            'note': 'Local checks only. Does not establish authentication, live coverage or permission to start a second writer.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    result = check(parser.parse_args().root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
