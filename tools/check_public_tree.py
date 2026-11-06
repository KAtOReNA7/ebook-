"""Check the tracked public boundary without printing sensitive file contents."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_POLICIES = {'copyright-aliases.json', 'fanqie-ranking.json', 'marketing-fee-policy.json',
                   'project-model.json', 'service-scope.json', 'work-copyright-policy.json',
                   'work-metadata-policy.json'}
PRIVATE_ROOTS = {'runs', 'receipts', 'research', 'reference', 'tmp', 'backups', '.codex', '.env', 'RUNBOOK.md'}
PATTERNS = {
    'GitHub credential': re.compile(rb'gh[opusr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'access key': re.compile(rb'AKIA[0-9A-Z]{16}|sk-proj-[A-Za-z0-9_-]{20,}'),
    'personal Feishu identity': re.compile(rb'ou_[0-9a-f]{30,}'),
    'private Tencent document': re.compile(rb'docs\.qq\.com/sheet/D[A-Za-z0-9_-]{12,}'),
    'private Feishu Base': re.compile(rb'xiron\.feishu\.cn/base/[A-Za-z0-9]+'),
}


def check(root=ROOT):
    root = Path(root)
    names = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                           check=True, capture_output=True).stdout.decode('utf-8').split('\0')
    issues = []
    for name in filter(None, names):
        path = Path(name)
        if path.parts[0] in PRIVATE_ROOTS or path.suffix.lower() in {'.zip', '.pem', '.key', '.dpapi', '.aesgcm'}:
            issues.append((name, 'private local artifact'))
        if path.parts[0] == 'config' and path.name not in PUBLIC_POLICIES and not path.name.endswith('.example.json'):
            issues.append((name, 'live config'))
        data = (root / path).read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                issues.append((name, label))
    return issues


if __name__ == '__main__':
    issues = check()
    for name, reason in issues:
        print(f'{name}: {reason}')
    print('Public tree boundary: ' + ('FAILED' if issues else 'passed'))
    raise SystemExit(bool(issues))
