"""Read existing Claude Desktop transcripts, including locally imported cloud chats.

Only the two session directories are scanned. No account, browser or credential
stores are consulted, and this module never requests or creates a cloud export.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys


SESSION_DIRS = ('local-agent-mode-sessions', 'claude-code-sessions')
META_FIELDS = ('sessionId', 'cliSessionId', 'title', 'cwd', 'originCwd',
               'createdAt', 'lastActivityAt', 'isArchived', 'importedFrom',
               'stagedTranscriptPath', 'hostLoopMode', 'model')


def default_home():
    override = os.environ.get('CLAUDE_DESKTOP_HOME')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Claude'
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming') / 'Claude'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'Claude'


def homes(home=None):
    """Find sibling first/third-party stores without searching unrelated folders."""
    home = Path(home) if home is not None else default_home()
    home = home.expanduser()
    candidates = [home]
    if home.name in ('Claude', 'Claude-3p'):
        candidates.append(home.with_name('Claude-3p' if home.name == 'Claude' else 'Claude'))
    return [path for path in candidates if path.is_dir()]


def _inside(path, root):
    # Do not follow a session symlink into a different application's data.
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).isoformat()
    return value if isinstance(value, str) else ''


def _basename(value):
    # Imported metadata may retain Windows paths after migration to another OS.
    return str(value or '').replace('\\', '/').rsplit('/', 1)[-1]


def session_items(home=None):
    """Yield file-backed sessions with stable IDs scoped to product and account.

    The relative transcript path is the identity: copying a store to a different
    computer preserves IDs, but equal CLI UUIDs in different accounts or modes
    never overwrite one another. Metadata-only sessions are not empty chats.
    """
    for product_home in homes(home):
        product = 'Claude-3p' if product_home.name == 'Claude-3p' else 'Claude'
        for directory in SESSION_DIRS:
            root = product_home / directory
            if not root.is_dir() or not _inside(root, product_home):
                continue
            metadata = {}
            for path in sorted(root.rglob('local_*.json')):
                if not path.is_file() or not _inside(path, root):
                    continue
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeError) as error:
                    raise ValueError(f'Invalid Claude Desktop session metadata: {path}') from error
                if not isinstance(value, dict):
                    raise ValueError(f'Invalid Claude Desktop session metadata: {path}')
                row = {key: value[key] for key in META_FIELDS if key in value}
                names = set()
                cli_id = row.get('cliSessionId')
                if isinstance(cli_id, str) and cli_id:
                    names.add(cli_id + '.jsonl')
                staged_name = _basename(row.get('stagedTranscriptPath'))
                if staged_name.endswith('.jsonl'):
                    names.add(staged_name)
                for name in names:
                    metadata.setdefault((path.parent, name), []).append((path, row))

            for path in sorted(root.rglob('*.jsonl')):
                if not path.is_file() or not _inside(path, root):
                    continue
                matches = []
                for ancestor in path.parents:
                    if ancestor == root.parent:
                        break
                    matches = metadata.get((ancestor, path.name), [])
                    if matches:
                        break
                if len(matches) > 1:
                    raise ValueError(f'Ambiguous Claude Desktop session metadata: {path}')
                metadata_path, row = matches[0] if matches else (None, {})
                relative = path.relative_to(product_home).as_posix()
                identity = product + '/' + relative
                sid = 'claude-desktop-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
                source = row.get('importedFrom') or 'local-desktop'
                if source == 'claude-ai-chat':
                    originator = 'Claude (cloud import)'
                elif source == 'terminal-cli':
                    originator = 'Claude Code CLI (desktop import)'
                elif source == 'sibling-1p-code':
                    originator = 'Claude Code (desktop import)'
                elif directory == 'claude-code-sessions':
                    originator = 'Claude Code (desktop)'
                else:
                    originator = 'Claude Desktop'
                origin = dict(row, product=product, transcript_path=str(path),
                              relative_path=relative, original_id=row.get('cliSessionId') or path.stem)
                if metadata_path:
                    origin['metadata_path'] = str(metadata_path)
                    origin['account_scope'] = metadata_path.parent.relative_to(root).as_posix()
                else:
                    origin['account_scope'] = '/'.join(path.relative_to(root).parts[:2])
                yield {'id': sid, 'path': path, 'meta': {
                    'title': row.get('title') or '',
                    'cwd': row.get('originCwd') or row.get('cwd') or '',
                    'archived': row.get('isArchived') if isinstance(row.get('isArchived'), bool) else None,
                    'created_at': _timestamp(row.get('createdAt')),
                    'updated_at': _timestamp(row.get('lastActivityAt')),
                    'source': source, 'origin_kind': 'claude-desktop', 'originator': originator,
                    'coverage': 'local_imported_copy' if row.get('importedFrom') else 'local_desktop_session',
                    'models': [row['model']] if isinstance(row.get('model'), str) and row['model'] else [],
                    'origin_metadata': origin,
                }}
