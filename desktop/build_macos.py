#!/usr/bin/env python3
"""Build a local macOS .app; the viewer itself has no Python runtime dependency."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=root.parent / '.local')
args = parser.parse_args()
if sys.platform != 'darwin':
    raise SystemExit('This packaging script is for macOS. Use cargo build --release on other systems.')
subprocess.run(['cargo', 'build', '--release', '--locked'], cwd=root, check=True)
app = args.output.expanduser().resolve() / 'StarVault.app'
macos = app / 'Contents/MacOS'
macos.mkdir(parents=True, exist_ok=True)
shutil.copy2(root / 'target/release/chat-history-vault', macos / 'chat-history-vault')
resources = app / 'Contents/Resources'
resources.mkdir(parents=True, exist_ok=True)
shutil.copy2(root.parent / 'assets/StarVault.icns', resources / 'StarVault.icns')
tool = resources / 'tool'
tool.mkdir(parents=True, exist_ok=True)
for source in root.parent.glob('*.py'):
    if not source.name.startswith('test_'):
        shutil.copy2(source, tool / source.name)
shutil.copytree(root.parent / 'skills', tool / 'skills', dirs_exist_ok=True)
info = {
    'CFBundleName': 'StarVault', 'CFBundleDisplayName': '星匣 · StarVault',
    'CFBundleIdentifier': 'com.cpython666.chat-history-vault.viewer',
    'CFBundleVersion': '5', 'CFBundleShortVersionString': '0.1.4',
    'CFBundleExecutable': 'chat-history-vault', 'CFBundlePackageType': 'APPL',
    'CFBundleIconFile': 'StarVault',
    'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '11.0',
    'NSPrincipalClass': 'NSApplication',
}
(app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
print(app)
