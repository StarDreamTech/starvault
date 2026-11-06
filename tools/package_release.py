#!/usr/bin/env python3
"""Package an explicit code-only tree; never copy repository history or archives."""
import argparse
import configparser
import gzip
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--app', type=Path)
args = parser.parse_args()
output = args.output.expanduser().resolve()
source = output / 'source'
source.mkdir(parents=True, exist_ok=True)
config = configparser.ConfigParser()
config.read(root / 'setup.cfg')
version = config['metadata']['version']
paths = set(root.glob('*.py'))
paths.update(root / name for name in ('README.md', 'LICENSE', '.gitignore', 'setup.cfg', 'pyproject.toml', 'MANIFEST.in'))
for pattern in ('desktop/Cargo.*', 'desktop/build_macos.py', 'desktop/src/*.rs', 'assets/*.png', 'assets/*.svg', 'assets/*.icns',
                'assets/share/*.png', 'assets/share/catalog.json',
                'skills/use-chat-history-vault/SKILL.md', 'skills/use-chat-history-vault/agents/*.yaml',
                '.agents/skills/chat-history-vault/SKILL.md', '.agents/skills/chat-history-vault/agents/*.yaml',
                '.github/workflows/*.yml', 'docs/*.md', 'tools/*.py', 'tools/*.swift'):
    paths.update(root.glob(pattern))
for path in sorted(paths):
    if not path.is_file() or path.is_symlink():
        raise ValueError('Release source must be a regular file: ' + path.name)
    relative = path.relative_to(root)
    target = source / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
archive = output / f'StarVault-{version}-source.tar.gz'
with archive.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as bundle:
    for path in sorted(paths):
        relative = path.relative_to(root)
        info = bundle.gettarinfo(source / relative, arcname=f'starvault-{version}/{relative}')
        info.mtime = info.uid = info.gid = 0
        info.uname = info.gname = ''
        with (source / relative).open('rb') as stream:
            bundle.addfile(info, stream)
artifacts = [archive]
if args.app:
    app = args.app.expanduser().resolve()
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    archive = output / f'StarVault-{version}-macos-arm64.zip'
    subprocess.run(['ditto', '-c', '-k', '--keepParent', str(app), str(archive)], check=True)
    artifacts.append(archive)
checksums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in artifacts)
(output / 'SHA256SUMS').write_text(checksums)
print(f'Code-only source: {len(paths)} files')
print(checksums, end='')
