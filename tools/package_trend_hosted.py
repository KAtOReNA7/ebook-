"""Package only the public, built trend UI for Feishu's documented output mode."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'web' / 'trend-plugin' / 'dist'
TARGET = ROOT / 'web' / 'trend-plugin' / 'feishu-dist'

def main():
    files = sorted(p for p in SOURCE.rglob('*') if p.is_file())
    if not (SOURCE / 'index.html').is_file() or not files:
        raise SystemExit('Build the trend plugin first.')
    for path in files:
        if path.suffix not in {'.html', '.js', '.css'}:
            raise SystemExit(f'Unexpected build asset: {path.name}')
    if TARGET.is_symlink() or TARGET.resolve() != ROOT.resolve() / 'web' / 'trend-plugin' / 'feishu-dist':
        raise SystemExit('Refusing unexpected generated directory')
    hashes = {p.relative_to(SOURCE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if '--check' in sys.argv:
        actual = {p.relative_to(TARGET).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in TARGET.rglob('*') if p.is_file() and p.name != 'build-hashes.json'}
        if actual != hashes:
            raise SystemExit('Hosted artifact is stale; run python tools/package_trend_hosted.py')
        print('Hosted assets match the fresh build')
        return
    # This directory contains generated public code only, never operational state.
    TARGET.mkdir(exist_ok=True)
    for old in TARGET.rglob('*'):
        if old.is_file():
            old.unlink()
    hashes = {}
    for path in files:
        relative = path.relative_to(SOURCE)
        dest = TARGET / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        hashes[relative.as_posix()] = hashlib.sha256(dest.read_bytes()).hexdigest()
    (TARGET / 'build-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n', encoding='utf-8')
    print(f'Packaged {len(files)} public UI assets, {sum(p.stat().st_size for p in files)} bytes')

if __name__ == '__main__':
    main()
