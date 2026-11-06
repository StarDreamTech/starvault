#!/usr/bin/env python3
"""星匣 · StarVault: readable AI archives with original, deduplicated images."""
import argparse
import base64
import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import resources
import skills_backup
import archive_api
import claude_desktop
import claude_projects

ROOT = Path(__file__).resolve().parent


def config_directory():
    override = os.environ.get('CHAT_HISTORY_VAULT_CONFIG_DIR')
    if override:
        return Path(override).expanduser().resolve()
    legacy = ROOT / '.local'
    if (legacy / 'config.json').is_file() or (ROOT / '.git').exists():
        return legacy
    # Share the desktop's tool state; never write configuration into site-packages.
    app_home = (Path.home() / 'Library/Application Support/ChatHistoryVault'
                if sys.platform == 'darwin' else Path.home() / 'ChatHistoryVault')
    return (app_home / 'tool/.local').resolve()


LOCAL = config_directory()
VERSION = 5
TOOLS = ('codex', 'claude', 'claude-desktop', 'workbuddy', 'openclaw', 'dsh', 'chatgpt')
# Repository branches stay "<device>-<tool>"; these are the local homes each tool owns.
HOME_DIRS = {'codex': '.codex', 'claude': '.claude', 'workbuddy': '.workbuddy-ai',
             'openclaw': '.openclaw', 'dsh': '.dsh'}
ORIGINATORS = {'claude': 'Claude Code CLI', 'workbuddy': 'WorkBuddy',
               'openclaw': 'OpenClaw', 'dsh': 'DeepSeek Harness'}
# Recognize binary signatures, including images inside JSON-encoded tool results.
MEDIA = re.compile(rb'(?:data:[a-zA-Z0-9.+/-]+;base64,)?(?<![A-Za-z0-9+/])(?:iVBORw0KGgo|/9j/|R0lGOD|UklGR)[A-Za-z0-9+/]{40,}={0,2}')
IMAGE_REF = re.compile(r'vault-image:([0-9a-f]{64}\.(?:png|jpg|gif|webp))')
DATA_URI = re.compile(r'data:[^\s,;]+;base64,[A-Za-z0-9+/=\s]+')
MARKDOWN_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)\n]+)\)')
# WorkBuddy puts agent instructions and memory before the user's actual request.
WORKBUDDY_CONTEXT = '<system-reminder data-role="user-context">'
WORKBUDDY_QUERY = re.compile(r'<user_query>(.*?)</user_query>', re.S)
IMAGE_LOCAL_PATH = re.compile(r'<image_local_path>(.*?)</image_local_path>', re.S)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write(path, data):
    """Atomic writes; unchanged exports keep their timestamp."""
    data = data.encode('utf-8') if isinstance(data, str) else data
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temp = Path(output.name)
            output.write(data)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def write_json(path, data):
    write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def home_for(tool, cfg=None):
    """Resolve one tool's local home without touching another computer's paths."""
    cfg = cfg or {}
    if tool == 'chatgpt':
        return LOCAL / 'imports/chatgpt'
    configured = cfg.get(tool.replace('-', '_') + '_home')
    if configured:
        return Path(configured).expanduser()
    if tool == 'claude-desktop':
        return claude_desktop.default_home()
    variable = {'codex': 'CODEX_HOME', 'claude': 'CLAUDE_CONFIG_DIR',
                'workbuddy': 'WORKBUDDY_HOME', 'openclaw': 'OPENCLAW_HOME', 'dsh': 'DSH_HOME'}[tool]
    return Path(os.environ.get(variable, Path.home() / HOME_DIRS[tool])).expanduser()


def iso_time(value):
    """Normalize ISO strings and epoch milliseconds so indexes sort predictably."""
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def read_json_text(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def zstd_library():
    """Find libzstd without adding a Python dependency."""
    import ctypes
    import ctypes.util
    candidates = [ctypes.util.find_library('zstd'), '/opt/homebrew/lib/libzstd.dylib',
                  '/usr/local/lib/libzstd.dylib', '/usr/lib/libzstd.so.1', 'libzstd.so.1',
                  'zstd.dll', 'libzstd.dll']
    for candidate in dict.fromkeys(candidates):
        if not candidate:
            continue
        try:
            library = ctypes.CDLL(candidate)
            for symbol in ('ZSTD_createDStream', 'ZSTD_initDStream', 'ZSTD_decompressStream',
                           'ZSTD_isError', 'ZSTD_getErrorName', 'ZSTD_freeDStream'):
                getattr(library, symbol)
            return library
        except (OSError, AttributeError):
            continue
    return None


def zstd_ctypes(library, data):
    """Decode concatenated zstd frames with the streaming API (append-only session logs)."""
    import ctypes

    if not data:
        raise ValueError('zstd 数据为空，没有完整压缩帧。')

    class Buffer(ctypes.Structure):
        _fields_ = [('dst', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]

    class Input(ctypes.Structure):
        _fields_ = [('src', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]

    library.ZSTD_createDStream.argtypes = []
    library.ZSTD_createDStream.restype = ctypes.c_void_p
    library.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    library.ZSTD_initDStream.restype = ctypes.c_size_t
    library.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    library.ZSTD_decompressStream.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = [ctypes.c_size_t]
    library.ZSTD_isError.restype = ctypes.c_uint
    library.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    library.ZSTD_getErrorName.restype = ctypes.c_char_p
    library.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    library.ZSTD_freeDStream.restype = ctypes.c_size_t

    def check(result):
        if library.ZSTD_isError(result):
            message = library.ZSTD_getErrorName(result) or b'unknown error'
            raise RuntimeError('zstd 解压失败：' + message.decode('utf-8', 'replace'))

    stream = library.ZSTD_createDStream()
    if not stream:
        raise RuntimeError('zstd 解压失败：无法分配解压器。')
    try:
        check(library.ZSTD_initDStream(stream))
        chunk = 1 << 20
        source = ctypes.create_string_buffer(data, len(data))
        incoming = Input(ctypes.cast(source, ctypes.c_void_p), len(data), 0)
        target = ctypes.create_string_buffer(chunk)
        outgoing = Buffer(ctypes.cast(target, ctypes.c_void_p), chunk, 0)
        result = bytearray()
        while True:
            before = incoming.pos
            outgoing.pos = 0
            remaining = library.ZSTD_decompressStream(stream, ctypes.byref(outgoing), ctypes.byref(incoming))
            check(remaining)
            result += target[:outgoing.pos]
            if remaining == 0 and incoming.pos == incoming.size:
                break
            # Input exhaustion is only success at a frame boundary. A full output
            # buffer may still need another call to drain the current frame.
            if incoming.pos == incoming.size and outgoing.pos < outgoing.size:
                raise ValueError('zstd 压缩帧不完整，请在来源写入完成后重试。')
            if incoming.pos == before and outgoing.pos == 0:
                raise RuntimeError('zstd 解压失败：解压器没有进展。')
        return bytes(result)
    except (MemoryError, OverflowError) as error:
        raise RuntimeError('zstd 解压失败：无法分配缓冲区。') from error
    finally:
        library.ZSTD_freeDStream(stream)


def zstd_decompress(data):
    """Decode zstd with stdlib 3.14+, the optional zstandard module, libzstd, or the CLI."""
    if not data:
        raise ValueError('zstd 数据为空，没有完整压缩帧。')

    def decode_frames(factory):
        pending, chunks = data, []
        try:
            while pending:
                decoder = factory()
                chunks.append(decoder.decompress(pending))
                if not decoder.eof:
                    raise ValueError('zstd 压缩帧不完整，请在来源写入完成后重试。')
                if len(decoder.unused_data) >= len(pending):
                    raise RuntimeError('zstd 解压失败：解压器没有进展。')
                pending = decoder.unused_data
            return b''.join(chunks)
        except (ValueError, RuntimeError):
            raise
        except Exception as error:
            raise RuntimeError('zstd 解压失败：' + str(error)) from error

    try:
        from compression import zstd
    except ImportError:
        pass
    else:
        return decode_frames(zstd.ZstdDecompressor)
    try:
        import zstandard
    except ImportError:
        pass
    else:
        return decode_frames(lambda: zstandard.ZstdDecompressor().decompressobj())
    library = zstd_library()
    if library:
        return zstd_ctypes(library, data)
    executable = shutil.which('zstd')
    if executable:
        try:
            result = subprocess.run([executable, '-dc'], input=data, capture_output=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('zstd 解压失败：' + str(error)) from error
        if result.returncode:
            raise RuntimeError('zstd 解压失败：' + result.stderr.decode('utf-8', 'replace')[:200])
        return result.stdout
    raise RuntimeError('无法读取 zstd 压缩会话：请安装 zstd、zstandard，或使用 Python 3.14+。')


@contextlib.contextmanager
def readonly_database(path):
    """Read one consistent transaction, copying inaccessible WAL files only to a temporary directory."""
    path = Path(path).absolute()
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        # A read-only WAL database can connect lazily and fail on the first query.
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        code = getattr(error, 'sqlite_errorcode', None)
        # Corruption and invalid schemas are not read-only-open failures. Never
        # conceal them by retrying against a different view of the database.
        if code is not None:
            if code & 0xff not in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN,
                                   sqlite3.SQLITE_IOERR, sqlite3.SQLITE_PERM):
                raise
        elif not any(message in str(error).lower() for message in
                     ('readonly', 'read-only', 'unable to open', 'disk i/o', 'permission')):
            raise
    else:
        try:
            yield connection
        finally:
            connection.close()
        return

    # SQLite recreates SHM in the temporary directory; copying live lock state
    # is unnecessary. Include a rollback journal if the database uses one.
    files = [path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-journal')]

    def signature():
        state = {}
        for source in files:
            try:
                stat = source.stat()
                state[source] = (stat.st_dev, stat.st_ino, stat.st_size,
                                 stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                state[source] = None
        return state

    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix='starvault-db-') as temporary:
            folder = Path(temporary)
            before = signature()
            if before[path] is None:
                continue
            try:
                for source, state in before.items():
                    if state is not None:
                        shutil.copy2(source, folder / source.name)
            except FileNotFoundError:
                continue  # A writer rotated a sidecar during the copy.
            if before != signature():
                continue
            connection = sqlite3.connect(folder / path.name, timeout=10)
            try:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
                yield connection
            finally:
                connection.close()
            return
    raise RuntimeError('数据库在复制期间持续变化，未读取不一致快照；请稍后重试：' + str(path))


def openclaw_databases(home):
    return sorted(p for p in Path(home).glob('agents/*/agent/openclaw-agent.sqlite') if p.is_file())


def run(args, cwd=None, check=True, capture=True):
    result = subprocess.run(args, cwd=cwd, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, timeout=300)
    if check and result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args[:4]) + '\n' + (result.stderr or ''))
    return result


def git(directory, *args, **kwargs):
    return run(['git', '-C', str(directory), *args], **kwargs)


def credentials():
    # Use normal Git/gh authentication. Never write a token to config or logs.
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token and shutil.which('gh'):
        result = run(['gh', 'auth', 'token'], check=False)
        if result.returncode == 0:
            token = result.stdout.strip()
    if not token:
        result = subprocess.run(['git', 'credential', 'fill'],
                                input='protocol=https\nhost=github.com\n\n',
                                capture_output=True, text=True,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                     'GIT_ASKPASS': '/usr/bin/false' if os.name != 'nt' else 'echo'},
                                timeout=30)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        token = values.get('password')
    if not token:
        raise RuntimeError('GitHub authentication required: run gh auth login first.')
    return token


def private_repo(repo):
    match = re.fullmatch(r'https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?', repo)
    if not match:
        raise ValueError('Repository must be an HTTPS github.com URL without embedded credentials.')
    req = urllib.request.Request('https://api.github.com/repos/' + '/'.join(match.groups()),
                                 headers={'Authorization': 'Bearer ' + credentials(),
                                          'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'chat-history-vault'})
    with urllib.request.urlopen(req, timeout=30) as response:
        meta = json.load(response)
    if not meta.get('private') or not meta.get('permissions', {}).get('push'):
        raise RuntimeError('Refusing upload: repository must be private and writable.')


def default_device():
    return re.sub(r'[^A-Za-z0-9_-]+', '-', socket.gethostname().removesuffix('.local')).strip('-') or 'computer'


def config():
    value = read_json(LOCAL / 'config.json')
    if not value:
        raise RuntimeError('Run init --repo https://github.com/OWNER/REPO --device NAME first.')
    return value


def skill_policy_key():
    # A moved archive must not reuse another computer's absolute source paths.
    return sys.platform + ':' + socket.gethostname()


def skill_policy(root):
    return read_json(root / '.skills.json', {}).get('devices', {}).get(skill_policy_key())


def save_skill_policy(root, settings):
    path = root / '.skills.json'
    policy = read_json(path, {'devices': {}})
    devices = policy.setdefault('devices', {})
    if settings is None:
        devices.pop(skill_policy_key(), None)
    else:
        devices[skill_policy_key()] = settings
    resources.write(path, policy)


def collect_skills(root, cfg, settings, plan=False):
    sources = skills_backup.default_sources(
        codex_home=cfg.get('codex_home'), claude_home=cfg.get('claude_home'),
        project=settings.get('project', str(ROOT)),
    ) if settings.get('defaults', True) else []
    sources.extend(Path(path).expanduser() for path in settings.get('sources', []))
    if not sources:
        raise ValueError('请启用默认 Skill 目录，或使用 --source 指定至少一个目录。')
    return skills_backup.backup(root, sources, plan=plan)


@contextlib.contextmanager
def lock():
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / 'sync.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('另一个备份或迁移正在运行，请等待它完成后重试。') from error
        yield


def codex_metadata(home):
    result = {}
    state = read_json(home / '.codex-global-state.json', {})
    projects = state.get('local-projects', {})
    assignments = state.get('thread-project-assignments', {})
    projectless = set(state.get('projectless-thread-ids', []))
    pinned_projects = state.get('pinned-project-ids', [])
    pinned_threads = set(state.get('pinned-thread-ids', []))
    project_order = state.get('project-order', [])
    databases = sorted(home.glob('state_*.sqlite'),
                       key=lambda p: int(re.search(r'_(\d+)', p.name).group(1)), reverse=True)
    if databases:
        with readonly_database(databases[0]) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db_projects = {r['id']: dict(r) for r in db.execute('SELECT * FROM projects')} if 'projects' in tables else {}
            legacy_ids = {new: old for mapping in state.get('app-server-project-id-by-legacy-project-id-by-host', {}).values()
                          for old, new in mapping.items()}
            for row in db.execute('SELECT * FROM threads'):
                row = dict(row)
                project_id = row.get('project_id') or assignments.get(row['id'], {}).get('projectId')
                cwd = row.get('cwd', '')
                if not project_id and row['id'] not in projectless:
                    # Older/archived threads may only retain a working directory.
                    matches = [(len(root), key) for key, p in projects.items() for root in p.get('rootPaths', [])
                               if cwd == root or cwd.startswith(root.rstrip('/') + '/')]
                    if matches: project_id = max(matches)[1]
                project = db_projects.get(project_id) or projects.get(project_id, {})
                legacy_id = legacy_ids.get(project_id, project_id)
                result[row['id']] = {
                    'title': row.get('name') or row.get('title', '').split('\n')[0][:240],
                    'cwd': cwd, 'archived': bool(row.get('archived')),
                    'project': project.get('name') or ('未分组' if not project_id else None),
                    'project_id': legacy_id or '',
                    'project_pinned': legacy_id in pinned_projects,
                    'project_order': (pinned_projects.index(legacy_id) if legacy_id in pinned_projects else
                                      project_order.index(legacy_id) if legacy_id in project_order else 999999),
                    'pinned': bool(row.get('is_pinned')) or row['id'] in pinned_threads,
                    'source': row.get('thread_source') or 'user',
                    'origin_kind': 'codex', 'originator': row.get('originator') or '',
                    'models': [row['model']] if row.get('model') else [],
                    'origin_metadata': {'thread': row, 'project': project,
                                        'assignment': assignments.get(row['id']),
                                        'projectless': row['id'] in projectless},
                }
    return result


def claude_metadata(home):
    result = {}
    bases = [Path.home() / 'Library/Application Support/Claude/claude-code-sessions']
    if os.environ.get('APPDATA'):
        bases.append(Path(os.environ['APPDATA']) / 'Claude/claude-code-sessions')
    for base in bases:
        for path in base.rglob('local_*.json'):
            row = read_json(path, {})
            if row.get('cliSessionId'):
                result[row['cliSessionId']] = {
                    'title': row.get('title'), 'cwd': row.get('originCwd') or row.get('cwd', ''),
                    'archived': row.get('isArchived'), 'source': 'user',
                    'origin_kind': 'claude', 'originator': 'Claude Code',
                    'models': [row['model']] if row.get('model') else [],
                    'origin_metadata': {k: v for k, v in row.items() if k not in
                                        ('remoteMcpServersConfig', 'promptAppendSnapshot', 'toolSurfaceSnapshot')},
                }
    return result


def workbuddy_metadata(home):
    """Session titles and workspace fields live in WorkBuddy's local SQLite index."""
    path = Path(home) / 'workbuddy.db'
    if not path.is_file():
        return {}
    result = {}
    try:
        with readonly_database(path) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'sessions' not in tables:
                return {}
            for row in db.execute('SELECT * FROM sessions'):
                row = dict(row)
                result[row['id']] = {
                    'title': row.get('custom_title') or row.get('title') or '',
                    'cwd': row.get('cwd') or '',
                    'archived': None,
                    'source': 'automation' if row.get('is_background_automation') else 'user',
                    'origin_kind': 'workbuddy', 'originator': 'WorkBuddy',
                    'created_at': iso_time(row.get('created_at')),
                    'updated_at': iso_time(row.get('last_activity_at') or row.get('updated_at')),
                    'models': [row['model']] if row.get('model') else [],
                    'origin_metadata': {key: row.get(key) for key in
                                        ('id', 'cwd', 'status', 'mode', 'permission_mode', 'transport',
                                         'conversation_origin', 'visibility', 'deleted_at', 'project_id',
                                         'session_settings') if row.get(key) is not None},
                }
    except sqlite3.Error as error:
        raise RuntimeError('无法读取 WorkBuddy 元数据，保留已有备份：' + str(error)) from error
    return result


def openclaw_metadata(home):
    result = {}
    for database in openclaw_databases(home):
        agent = database.parent.parent.name
        try:
            with readonly_database(database) as db:
                db.row_factory = sqlite3.Row
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'session_nodes' not in tables:
                    continue
                for row in db.execute('SELECT * FROM session_nodes'):
                    row = dict(row)
                    sid = row.get('current_session_id')
                    if not sid:
                        continue
                    entry = read_json_text(row.get('entry_json'))
                    result[(agent, sid)] = {
                        'title': row.get('display_name') or row.get('label') or entry.get('label') or '',
                        'cwd': entry.get('cwd') or entry.get('workspaceFolder') or '',
                        'archived': bool(row['archived_at']) if 'archived_at' in row else None,
                        'source': row.get('created_via') or 'user',
                        'origin_kind': 'openclaw', 'originator': 'OpenClaw',
                        'created_at': iso_time(row.get('created_at') or entry.get('createdAt')),
                        'updated_at': iso_time(row.get('last_activity_at') or row.get('updated_at')
                                               or entry.get('updatedAt')),
                        'models': [],
                        'origin_metadata': {'agent': agent, 'session_key': row.get('session_key'),
                                            'status': row.get('status'), 'created_via': row.get('created_via'),
                                            'pinned': bool(row.get('pinned_at')),
                                            'parent_session_key': row.get('parent_session_key')},
                    }
        except sqlite3.Error as error:
            raise RuntimeError('无法读取 OpenClaw 元数据，保留已有备份：' + str(error)) from error
    return result


def dsh_metadata(home):
    """DeepSeek Harness keeps titles in its local session projection cache."""
    result = {}
    folder = Path(home) / 'storages/session_projcache/sessions'
    for path in sorted(folder.glob('*.json')):
        try:
            record = (read_json(path, {}) or {}).get('record') or {}
        except ValueError:
            continue
        rows = record.get('rows') or {}
        identity = record.get('identity') or {}
        listing = (rows.get('sessionListMetadata') or {}).get('val') or {}
        meta = {
            'title': (rows.get('title') or {}).get('val') or '',
            'cwd': identity.get('cwd') or '',
            'archived': None,
            'source': 'subagent' if identity.get('delegationDepth') else 'user',
            'origin_kind': 'dsh', 'originator': 'DeepSeek Harness',
            'created_at': iso_time(identity.get('createdAt')),
            'updated_at': iso_time(listing.get('lastPromptAt') or identity.get('createdAt')),
            'models': [],
            'origin_metadata': {'format_version': identity.get('formatVersion'),
                                'delegation_depth': identity.get('delegationDepth')},
        }
        result[path.stem] = meta
        if path.stem.startswith('session-'):
            result.setdefault(path.stem[len('session-'):], meta)
    return result


def metadata(tool, home):
    home = Path(home)
    if tool == 'chatgpt':
        return read_json(home / 'metadata.json', {})
    if tool == 'codex':
        return codex_metadata(home)
    if tool == 'claude':
        return claude_metadata(home)
    if tool == 'workbuddy':
        return workbuddy_metadata(home)
    if tool == 'openclaw':
        return openclaw_metadata(home)
    if tool == 'dsh':
        return dsh_metadata(home)
    return {}


def sources(tool, home):
    home = Path(home)
    if tool == 'chatgpt': return sorted(home.glob('sessions/*.jsonl'))
    if tool == 'codex':
        return sorted(p for folder in ('sessions', 'archived_sessions')
                      for suffix in ('.jsonl', '.jsonl.zst', '.jsonl.zstd')
                      for p in home.glob(folder + '/**/*' + suffix) if p.is_file())
    if tool == 'workbuddy':
        return sorted(path for path in home.glob('projects/*/*.jsonl') if path.is_file())
    if tool == 'dsh':
        return sorted([*home.glob('sessions/*/*/session*.jsonl'),
                       *home.glob('sessions/*/*/session*.jsonl.zst'),
                       *home.glob('sessions/*/*/session*.jsonl.zstd')])
    if tool == 'openclaw':
        return []
    return sorted(home.glob('projects/**/*.jsonl'))


def session_id(path, tool):
    if tool == 'codex':
        match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', path.name.split('.jsonl')[0])
        if not match:
            raise ValueError('Unrecognized session filename: ' + path.name)
        return match.group(1)
    if tool == 'dsh':
        name = path.parent.name
        return name[len('session-'):] if name.startswith('session-') else name
    if tool == 'workbuddy':
        return path.stem
    if path.parent.name == 'subagents':
        return path.parent.parent.name + '--' + path.stem
    return path.stem


def prepare_jsonl(path, directory):
    """Prepare a stable snapshot; it is published only after transcript validation."""
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError('会话正在变化，下次重试：' + str(path))
    raw = zstd_decompress(data) if path.suffix in ('.zst', '.zstd') else data
    if shutil.disk_usage(directory).free < len(raw) + len(data) + 256 * 1024**2:
        raise RuntimeError('磁盘空间不足，保留已有会话备份')
    target = Path(directory) / 'source.jsonl'
    write(target, raw)
    if path.suffix in ('.zst', '.zstd'):
        write(Path(directory) / 'source.jsonl.zst', data)
    return target


def hash_openclaw_event(digest, row):
    digest.update(json.dumps([row['seq'], row['event_json']], ensure_ascii=False).encode('utf-8'))
    blob = bytes(row['event_zstd']) if row['event_zstd'] is not None else b''
    digest.update(len(blob).to_bytes(8, 'big'))
    digest.update(blob)


def prepare_openclaw(database, sid, directory, expected_digest=None):
    """Materialize SQLite transcript events as the same JSONL shape the exporter reads."""
    lines = []
    digest = hashlib.sha256()
    with readonly_database(database) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute('SELECT seq, event_json, event_zstd FROM transcript_events '
                              'WHERE session_id=? ORDER BY seq', (sid,)):
            hash_openclaw_event(digest, row)
            raw = row['event_json']
            if raw is None and row['event_zstd'] is not None:
                raw = zstd_decompress(bytes(row['event_zstd'])).decode('utf-8')
            if isinstance(raw, str) and raw.strip():
                lines.append(raw.strip())
            else:
                raise ValueError('OpenClaw 事件缺少正文：' + str(sid))
    if expected_digest is not None and digest.hexdigest() != expected_digest:
        raise RuntimeError('OpenClaw 会话正在变化，下次重试：' + str(sid))
    target = Path(directory) / 'source.jsonl'
    data = ('\n'.join(lines) + ('\n' if lines else '')).encode('utf-8')
    if shutil.disk_usage(directory).free < len(data) + 256 * 1024**2:
        raise RuntimeError('磁盘空间不足，保留已有会话备份')
    write(target, data)
    return target


def session_items(tool, home, previous_root=None, warnings=None):
    """Describe every session to export, including non-JSONL local stores."""
    if tool == 'claude-desktop':
        for item in claude_desktop.session_items(home):
            path = item['path']
            stat = path.stat()
            yield {'id': item['id'], 'meta': item['meta'], 'origin': str(path),
                   'signature': [VERSION, str(path), stat.st_size, stat.st_mtime_ns, item['meta']],
                   'prepare': lambda directory, path=path: prepare_jsonl(path, directory)}
        for item in claude_projects.session_items(home, previous_root=previous_root, warnings=warnings):
            item['signature'] = [VERSION, *item['signature']]
            yield item
        return
    metas = metadata(tool, home)
    if tool == 'openclaw':
        for database in openclaw_databases(home):
            agent = database.parent.parent.name
            digests = {}
            with readonly_database(database) as db:
                db.row_factory = sqlite3.Row
                for row in db.execute('SELECT session_id, seq, event_json, event_zstd '
                                      'FROM transcript_events ORDER BY session_id, seq'):
                    digest = digests.setdefault(row['session_id'], hashlib.sha256())
                    hash_openclaw_event(digest, row)
            for original_id, digest in digests.items():
                archive_api.component(original_id)
                archive_api.component(agent)
                sid = original_id if agent == 'main' else agent + '--' + original_id
                meta = metas.get((agent, original_id), {})
                fingerprint = digest.hexdigest()
                yield {'id': sid, 'meta': meta, 'origin': str(database),
                       'signature': [VERSION, str(database), original_id, fingerprint, meta],
                       'prepare': lambda directory, database=database, sid=original_id, expected=fingerprint:
                       prepare_openclaw(database, sid, directory, expected)}
        return
    # A compressed replacement and its original can coexist during source rotation.
    paths = {}
    for path in sources(tool, home):
        sid = session_id(path, tool)
        if sid not in paths or path.stat().st_mtime_ns > paths[sid].stat().st_mtime_ns:
            paths[sid] = path
    for sid, path in paths.items():
        meta = metas.get(sid) or metas.get('session-' + sid) or {}
        stat = path.stat()
        item = {'id': sid, 'meta': meta, 'origin': str(path),
                'signature': [VERSION, str(path), stat.st_size, stat.st_mtime_ns, meta]}
        if tool == 'dsh' or path.suffix in ('.zst', '.zstd'):
            item['prepare'] = lambda directory, path=path: prepare_jsonl(path, directory)
            if tool == 'codex':
                item['snapshot_name'] = 'source.jsonl.zst'
        yield item


def image_extension(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'png'
    if data.startswith(b'\xff\xd8\xff'): return 'jpg'
    if data.startswith((b'GIF87a', b'GIF89a')): return 'gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return 'webp'
    return None


def save_image(data, dest, copy=True):
    ext = image_extension(data)
    if not ext:
        return None
    name = hashlib.sha256(data).hexdigest() + '.' + ext
    target = dest.parent / 'images' / name
    if copy and not target.exists():
        if shutil.disk_usage(dest).free < len(data) + 256 * 1024**2:
            raise RuntimeError('Insufficient disk space to export images safely.')
        write(target, data)
    return name


def extract_images(raw, dest, found, copy=None):
    if copy is None: copy = 'images' in resources.policy(dest.parent)
    def replace(match):
        encoded = match.group().split(b';base64,')[-1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return match.group()
        name = save_image(data, dest, copy=copy)
        if not name:
            return match.group()
        found.add(name)
        return ('vault-image:' + name).encode('ascii')
    return MEDIA.sub(replace, raw)


def copy_local_images(row, cwd, dest, found, missing, refs=None):
    """Copy explicit local image links; never download remote URLs or follow model files."""
    def local(ref):
        ref = ref.strip('<>')
        if ref.startswith(('http:', 'https:', 'data:', 'vault-image:')):
            return None
        ref = urllib.parse.unquote(ref.removeprefix('file://'))
        path = Path(ref).expanduser()
        if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            return None
        if not path.is_absolute(): path = Path(cwd or '.') / path
        try:
            if not path.is_file():
                missing.add(str(path)); return None
            name = save_image(path.read_bytes(), dest, copy='images' in resources.policy(dest.parent))
        except PermissionError:
            missing.add(str(path)); return None
        if name:
            found.add(name)
            ref = (refs or {}).get(str(path.resolve()))
            if ref is not None and (dest.parent / 'images' / name).is_file():
                stat = path.stat()
                ref.update(copy='images/' + name, sha256=name.split('.')[0],
                           copied_size=stat.st_size, copied_mtime_ns=stat.st_mtime_ns)
        return name
    def visit(value):
        if isinstance(value, str):
            def replace(match):
                name = local(match.group(2))
                return '![图片](../../../images/' + name + ')' if name else match.group()
            return MARKDOWN_IMAGE.sub(replace, value)
        if isinstance(value, list): return [visit(item) for item in value]
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if value.get('type') in ('image', 'input_image', 'image_url'):
                for key in ('image_url', 'url', 'path'):
                    ref = value.get(key)
                    if isinstance(ref, dict): ref = ref.get('url')
                    if isinstance(ref, str): local(ref)
            return result
        return value
    return visit(row)


def readable(text):
    text = IMAGE_REF.sub(lambda m: '![图片](../../../images/' + m.group(1) + ')', text)
    # Any other data URI is an explicit omission, never an accidental huge text blob.
    return DATA_URI.sub('[非图片内嵌数据已省略]', text)


def message_text(message):
    content = message.get('content', [])
    if isinstance(content, str):
        return readable(content)
    return '\n\n'.join(readable(item['text']) for item in content
                       if isinstance(item, dict) and item.get('type') in ('text', 'input_text', 'output_text')
                       and isinstance(item.get('text'), str))


def workbuddy_visible(text):
    """Keep the user's request; context envelopes only stay in the local original."""
    if text.startswith(WORKBUDDY_CONTEXT):
        matches = list(WORKBUDDY_QUERY.finditer(text))
        if not matches:
            return None
        request = matches[-1].group(1).strip()
        trailing = text[matches[-1].end():].strip()
        return request + (('\n\n' + trailing) if trailing else '')
    if text.startswith('<cb_summary>'):
        return None
    return text


def workbuddy_images(value):
    """Turn WorkBuddy's local image tags into links that copy_local_images can archive."""
    if isinstance(value, str):
        return IMAGE_LOCAL_PATH.sub(lambda match: '![图片](<' + match.group(1) + '>)', value)
    if isinstance(value, list):
        return [workbuddy_images(item) for item in value]
    if isinstance(value, dict):
        return {key: workbuddy_images(item) for key, item in value.items()}
    return value


def row_time(tool, row):
    """Read each tool's timestamp field and normalize epoch milliseconds."""
    value = row.get('time') if tool == 'dsh' else row.get('timestamp')
    if value is None and tool == 'openclaw':
        message = row.get('message')
        if isinstance(message, dict):
            value = message.get('timestamp')
    return iso_time(value)


def export_session(path, tool, dest, meta, sid=None, origin=None, source_copy=None):
    sid = sid or session_id(path, tool)
    archive_api.component(sid)
    archive_api.component(dest.name)
    source = source_copy[1] if source_copy else path
    # Empty metadata fields fall back to fields read from the transcript itself.
    info = {key: value for key, value in meta.items() if value != ''}
    info.update({'id': sid, 'tool': tool, 'source_file': str(source)})
    if source_copy:
        info['source_copy'] = source.relative_to(dest.parent).as_posix()
    declared_title = info.get('title', '')
    if origin:
        info['source_origin'] = str(origin)
    body, fallback, assets, seen_images, seen_ids = [], [], set(), set(), set()
    messages = 0; partial = False; missing = set()
    refs = {}; operations = []; originals = []; models = set(info.get('models', [])); origin_records = []
    ordinal = 0
    with path.open('rb') as stream:
        # A concurrent writer may append. Read only the size observed at open.
        limit = os.fstat(stream.fileno()).st_size
        while stream.tell() < limit:
            offset = stream.tell()
            raw = stream.readline(limit - stream.tell())
            if not raw.endswith(b'\n'):
                partial = True; break
            line_images = set()
            original_size = len(raw); original_digest = hashlib.sha256(raw).hexdigest()
            raw = extract_images(raw, dest, line_images)
            try:
                row = json.loads(raw)
            except ValueError as error:
                raise ValueError('Malformed completed JSONL record in ' + path.name) from error
            if not isinstance(row, dict):
                raise ValueError('Expected a JSON object in ' + path.name)
            ordinal += 1
            original_row = json.dumps(row, ensure_ascii=False, separators=(',', ':'))
            payload = row.get('payload', {})
            if row.get('type') == 'session_meta':
                origin_records.append(row)
                if not info.get('cwd'): info['cwd'] = payload.get('cwd', '')
            elif row.get('type') == 'turn_context':
                if payload.get('model'): models.add(payload['model'])
            if tool in ('claude', 'claude-desktop') and row.get('cwd') and not info.get('cwd'): info['cwd'] = row['cwd']
            message = row.get('message', {})
            if isinstance(message, dict) and message.get('model'): models.add(message['model'])
            if row.get('type') not in ('session_meta', 'turn_context'):
                resources.collect(row, info.get('cwd', ''), ordinal, refs)
            if tool == 'workbuddy':
                row = workbuddy_images(row)
            row = copy_local_images(row, info.get('cwd', ''), dest, line_images, missing, refs)
            assets.update(line_images)
            timestamp = row_time(tool, row)
            if timestamp:
                info.setdefault('started_at', timestamp)
                info['updated_at'] = timestamp
            payload = row.get('payload', {})
            msg = None
            if tool == 'codex':
                if row.get('type') == 'session_meta':
                    info.setdefault('cwd', payload.get('cwd', ''))
                    info.setdefault('source', payload.get('thread_source', 'user'))
                if row.get('type') == 'response_item' and payload.get('type') == 'message':
                    msg = payload
                if row.get('type') == 'event_msg' and payload.get('type') in ('user_message', 'agent_message'):
                    fallback.append((timestamp, 'user' if payload['type'] == 'user_message' else 'assistant',
                                     readable(payload.get('message', ''))))
            elif tool == 'workbuddy':
                kind = row.get('type')
                if kind == 'session-meta':
                    info.setdefault('cwd', (row.get('meta') or {}).get('cwd', ''))
                elif kind == 'ai-title':
                    info['title'] = declared_title or row.get('aiTitle') or info.get('title') or ''
                    info.setdefault('cwd', row.get('cwd', ''))
                elif kind == 'message':
                    content = row.get('content', [])
                    msg = {'role': row.get('role'), 'content': content}
                    if row.get('role') == 'user':
                        text = message_text({'content': content})
                        visible = workbuddy_visible(text)
                        if visible is None:
                            row['archive_visible'] = False
                        elif visible != text:
                            msg = {'role': 'user', 'content': [{'type': 'input_text', 'text': visible}]}
                    info.setdefault('cwd', row.get('cwd', ''))
                    model = (row.get('providerData') or {}).get('model')
                    if model: models.add(model)
            elif tool == 'openclaw':
                kind = row.get('type')
                if kind == 'session':
                    info.setdefault('cwd', row.get('cwd', ''))
                elif kind == 'message' and isinstance(row.get('message'), dict):
                    msg = row['message']
                    if msg.get('model'): models.add(msg['model'])
                    provenance = (msg.get('provenance') or {}).get('kind')
                    if msg.get('role') == 'toolResult' or provenance == 'internal_system':
                        row['archive_visible'] = False
                elif kind == 'custom_message' and row.get('display'):
                    msg = {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': row.get('content', '')}]}
                elif kind == 'custom' and row.get('customType') == 'model-snapshot':
                    model = (row.get('data') or {}).get('modelId')
                    if model: models.add(model)
            elif tool == 'dsh':
                kind = row.get('type')
                if kind == 'session':
                    info.setdefault('cwd', row.get('cwd', ''))
                elif kind == 'session/title':
                    # The app's own title wins over a first-message fallback.
                    info['title'] = declared_title or (row.get('data') or {}).get('title') or info.get('title') or ''
                elif kind == 'user/message':
                    data = row.get('data') or {}
                    msg = {'role': 'user', 'content': data.get('content', [])}
                    # Runtime notices, approvals, skills and job events stay in the local original only.
                    if (data.get('source') or {}).get('kind') not in ('user', 'agent-message'):
                        row['archive_visible'] = False
                elif kind == 'assistant/message':
                    msg = (row.get('data') or {}).get('message') or {}
                    model = ((msg.get('source') or {}).get('model'))
                    if model: models.add(model)
            elif row.get('type') in ('user', 'assistant'):
                info.setdefault('cwd', row.get('cwd', ''))
                if row.get('isSidechain'):
                    info['source'] = 'subagent'
                msg = row.get('message', {})
                msg.setdefault('role', row['type'])
            if msg and msg.get('role') in ('user', 'assistant'):
                unique_id = row.get('uuid') or msg.get('id')
                # Claude can emit multiple text blocks for one API message ID.
                if tool in ('claude', 'claude-desktop'): unique_id = row.get('uuid')
                text = message_text(msg)
                content = msg.get('content', [])
                if (isinstance(content, str) or not content or any(isinstance(c, dict) and c.get('type') in
                        ('text', 'input_text', 'output_text', 'image', 'input_image', 'image_url') for c in content)):
                    originals.append(original_row)
                if text.strip() and (not unique_id or unique_id not in seen_ids):
                    if unique_id: seen_ids.add(unique_id)
                    if row.get('archive_visible', True):
                        body.append('## ' + ('用户' if msg['role'] == 'user' else '助手') + ' · ' + str(timestamp) + '\n\n' + text + '\n')
                        messages += 1
                        # Images already visible inline do not need the extra image section.
                        seen_images.update(name for name in line_images if 'images/' + name in text)
                    if not info.get('title') and msg['role'] == 'user' and row.get('archive_visible', True):
                        info['title'] = text.split('\n')[0][:160]
            is_operation = (tool == 'codex' and row.get('type') == 'response_item' and payload.get('type') in
                            ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output', 'web_search_call'))
            if tool in ('claude', 'claude-desktop'):
                content = msg.get('content', []) if msg else []
                is_operation = isinstance(content, list) and any(isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result') for c in content)
            elif tool == 'workbuddy':
                is_operation = row.get('type') in ('function_call', 'function_call_result')
            elif tool == 'openclaw':
                content = msg.get('content', []) if msg else []
                is_operation = bool(msg and msg.get('role') == 'toolResult') or (isinstance(content, list) and any(
                    isinstance(block, dict) and block.get('type') == 'toolCall' for block in content))
            elif tool == 'dsh':
                is_operation = row.get('type') in ('tool/call', 'tool/result')
            if is_operation:
                operations.append({'record': ordinal, 'offset': offset, 'length': original_size,
                                   'sha256': original_digest, 'type': payload.get('type') or row.get('type'),
                                   'call_id': payload.get('call_id') or row.get('uuid') or row.get('id')
                                   or (row.get('data') or {}).get('callId') or (row.get('data') or {}).get('id'),
                                   'timestamp': timestamp})
            fresh = sorted(line_images - seen_images)
            if fresh:
                body.append('### 图片 · ' + str(timestamp) + '\n\n' + '\n\n'.join(
                    '![图片](../../../images/' + name + ')' for name in fresh) + '\n')
                seen_images.update(fresh)
    if not messages and fallback:
        body = ['## ' + role + ' · ' + str(ts) + '\n\n' + text + '\n' for ts, role, text in fallback] + body
        messages = len(fallback)
    info['title'] = info.get('title') or sid
    # A database archive flag can precede moving the rollout file.
    info['archived'] = (bool(info.get('archived')) or 'archived_sessions' in Path(origin or path).parts) if tool == 'codex' else info.get('archived')
    project = info.get('project') or Path(info.get('cwd') or (
        path.parent.name if path.name != 'source.jsonl' else '')).name or '未分组'
    slug = re.sub(r'[^\w.-]+', '-', project).strip('.-')[:64] or 'projectless'
    # Same final directory name can belong to different projects.
    project_key = slug + '-' + hashlib.sha256((info.get('cwd') or project).encode()).hexdigest()[:8]
    relative = Path('sessions') / project_key / (sid + '.md')
    info.update({'project': project, 'file': relative.as_posix(), 'messages': messages,
                 'assets': sorted(assets), 'partial_tail': partial,
                 'unavailable_local_images': sorted(missing)})
    info['origin_kind'] = info.get('origin_kind') or tool
    info['originator'] = info.get('originator') or ORIGINATORS.get(tool, '')
    info['models'] = sorted(models)
    info['labels'] = sorted(set([tool, str(info.get('source', 'user'))] +
                               ([info['originator']] if info['originator'] else []) +
                               (['archived'] if info['archived'] else [])) |
                            resources.classification_tags(info.get('origin_metadata', {})))
    info['local_record'] = f'records/{dest.name}/{sid}/manifest.json'
    info['attachments'] = len(refs)
    info['operations'] = len(operations)
    local_meta = {**info, 'session_metadata': origin_records}
    # Prepare and validate the whole session before replacing existing artifacts.
    # Each file is atomic; an interrupted multi-file publish is retried by the old cache.
    pending = {}
    for name in ('manifest.json', 'dialogue.jsonl'):
        archive_api.safe_path(dest.parent, f'records/{dest.name}/{sid}/{name}')
    resources.finish(dest.parent, dest.name, sid, source, refs, operations, local_meta, originals,
                     lambda target, data: pending.__setitem__(target, data))
    if source_copy:
        pending[source] = source_copy[0].read_bytes()
    # Full original metadata and commands stay local. Git gets classification fields only.
    info.pop('origin_metadata', None)
    status = '已归档' if info['archived'] else ('未归档' if info['archived'] is False else '未知/CLI')
    header = ('# ' + info['title'].replace('\n', ' ') + '\n\n'
              + '- 工具：' + tool + '\n- 会话 ID：`' + sid + '`\n'
              + '- 项目：' + project + '\n- 工作目录：`' + info.get('cwd', '') + '`\n'
              + '- 状态：' + status + '\n- 类型：' + str(info.get('source', 'user')) + '\n\n'
              + '> 导出用户/助手文字和可识别的内嵌 PNG/JPEG/GIF/WebP 图片；工具日志、内部推理和系统提示不在本文件中。\n\n')
    pending[dest / relative] = header + '\n'.join(body)
    for target, data in pending.items():
        archive_api.safe_path(dest.parent, target.relative_to(dest.parent).as_posix())
        pending[target] = data.encode('utf-8') if isinstance(data, str) else data
    if shutil.disk_usage(dest.parent).free < sum(map(len, pending.values())) + 256 * 1024**2:
        raise RuntimeError('磁盘空间不足，保留已有会话备份')
    for target, data in pending.items():
        write(target, data)
    return info


def export(tool, home, dest, force=False):
    dest = Path(dest).absolute()
    archive_api.safe_path(dest.parent.resolve(), dest.name)
    dest.mkdir(parents=True, exist_ok=True)
    old = read_json(dest / 'index.json', {'sessions': {}})
    sessions = old['sessions']
    cache_path = dest / '.export-cache.json'
    cache = read_json(cache_path, {})
    count = 0; errors = []; warnings = []
    for item in session_items(tool, home, previous_root=dest, warnings=warnings):
        sid = item['id']
        try:
            archive_api.component(sid)
            signature = item['signature']
            previous = sessions.get(sid)
            record = f'records/{dest.name}/{sid}/'
            snapshot_name = item.get('snapshot_name', 'source.jsonl')
            staged = archive_api.safe_path(dest.parent, record + snapshot_name)
            previous_file = None
            if previous:
                archive_api._row(dest.name, sid, previous, tool)
                previous_file = archive_api.safe_path(dest.parent, dest.name + '/' + previous['file'])
            if (not force and cache.get(sid) == signature and previous
                    and previous_file.is_file()
                    and archive_api.safe_path(dest.parent, record + 'manifest.json').is_file()
                    and archive_api.safe_path(dest.parent, record + 'dialogue.jsonl').is_file()
                    and ('prepare' not in item or staged.is_file())
                    and ('images' not in resources.policy(dest.parent) or
                         all((dest.parent / 'images' / name).is_file() for name in previous['assets']))):
                continue
            if 'prepare' in item:
                with tempfile.TemporaryDirectory(prefix='.session-', dir=dest.parent) as temporary:
                    path = item['prepare'](Path(temporary))
                    info = export_session(path, tool, dest, item['meta'], sid=sid, origin=item.get('origin'),
                                          source_copy=(Path(temporary) / snapshot_name, staged))
            else:
                path = Path(item['origin'])
                info = export_session(path, tool, dest, item['meta'], sid=sid, origin=item.get('origin'))
            if previous and previous['file'] != info['file']:
                previous_file.unlink(missing_ok=True)
            sessions[sid] = info
            if not info['partial_tail']:
                cache[sid] = signature
            else:
                cache.pop(sid, None)
            count += 1
            if count % 25 == 0:
                print(tool + ': exported ' + str(count) + ' changed sessions', flush=True)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            errors.append(str(sid) + ': ' + str(error))
    # Preserve exports whose source was cleaned up locally; this is an archive.
    write_json(dest / 'index.json', {'format_version': VERSION, 'tool': tool, 'sessions': sessions})
    write_json(cache_path, cache)
    listing = ['# ' + tool + ' 会话归档', '',
               '文字和图片的阅读归档；不是可直接导入原应用的会话数据库。', '',
               '图片保存在本地备份根目录的 images/；不上传 GitHub。复制整个备份根目录即可保留相对链接。', '',
               '| 项目 | 会话 | 状态 | 文字消息 | 图片 |', '|---|---|---|---:|---:|']
    for item in sorted(sessions.values(), key=lambda r: (r.get('updated_at', ''), r['id']), reverse=True):
        title = re.sub(r'[\[\]|\n\r]', ' ', item['title'])
        project = item['project'].replace('|', ' ')
        listing.append('| ' + project + ' | [' + title + '](' + urllib.parse.quote(item['file']) + ') | '
                       + ('已归档' if item['archived'] else '未归档/未知') + ' | '
                       + str(item['messages']) + ' | ' + str(len(item['assets'])) + ' |')
    write(dest / 'README.md', '\n'.join(listing) + '\n')
    write(dest / '.gitignore', '.export-cache.json\n*.tmp\n.DS_Store\nimages/\nassets/\n*.png\n*.jpg\n*.jpeg\n*.webp\n*.gif\n*.glb\n*.blend\n')
    assets = {name for session in sessions.values() for name in session['assets']}
    summary = {'tool': tool, 'changed_sessions': count, 'sessions': len(sessions),
               'images': len(assets), 'image_bytes': sum((dest.parent / 'images' / name).stat().st_size for name in assets if (dest.parent / 'images' / name).exists()),
               'missing_local_links': sum(len(s.get('unavailable_local_images', [])) for s in sessions.values()),
               'text_bytes': sum((dest / s['file']).stat().st_size for s in sessions.values())}
    if warnings:
        summary['warnings'] = warnings
        for warning in warnings[:10]:
            print('Local cache: ' + warning, file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if errors:
        raise RuntimeError('Some sessions could not be exported; upload stopped.\n' + '\n'.join(errors[:10]))
    return summary


def checkout(cfg, tool):
    branch = cfg['device'] + '-' + tool
    dest = Path(cfg['archive']) / branch
    if not (dest / '.git').exists():
        dest.mkdir(parents=True, exist_ok=True)
        git(dest, 'init', '-b', branch)
        git(dest, 'remote', 'add', 'origin', cfg['repo'])
    if git(dest, 'branch', '--show-current').stdout.strip() != branch:
        raise RuntimeError('Unexpected branch in ' + str(dest))
    if git(dest, 'remote', 'get-url', 'origin').stdout.strip() != cfg['repo']:
        raise RuntimeError('Unexpected remote in ' + str(dest))
    git(dest, 'config', 'core.quotepath', 'false')
    return dest, branch


def enabled_tools(cfg):
    """Local tools that exist on this computer; missing apps are simply skipped."""
    tools = ['codex', 'claude']
    if claude_desktop.homes(home_for('claude-desktop', cfg)):
        tools.append('claude-desktop')
    tools.extend(tool for tool in ('openclaw', 'workbuddy', 'dsh') if home_for(tool, cfg).is_dir())
    if (LOCAL / 'imports/chatgpt/metadata.json').exists():
        tools.append('chatgpt')
    return tools


def sync(cfg, tools, upload):
    summaries = []
    settings = skill_policy(Path(cfg['archive']))
    if settings is not None:
        try:
            result = collect_skills(Path(cfg['archive']), cfg, settings)
            summaries.append({'tool': 'skills', **{k: v for k, v in result.items() if k != 'items'}})
        except (OSError, RuntimeError, ValueError) as error:
            summaries.append({'tool': 'skills', 'migration_error': str(error)})
            print('Local skills: ' + str(error), file=sys.stderr)
    network_error = None
    if upload:
        try:
            private_repo(cfg['repo'])
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            network_error = error
    for tool in tools:
        dest, branch = checkout(cfg, tool)
        if upload and not network_error:
            try:
                refs = git(dest, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch).stdout
                if refs:
                    git(dest, 'fetch', 'origin', branch)
                    if git(dest, 'rev-parse', '--verify', 'HEAD', check=False).returncode == 0:
                        git(dest, 'merge', '--ff-only', 'FETCH_HEAD')
                    else:
                        git(dest, 'checkout', '-B', branch, 'FETCH_HEAD')
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                network_error = error
        source_home = home_for(tool, cfg)
        summary = export(tool, source_home, dest)
        try:
            if (Path(cfg['archive']) / '.migration.json').exists():
                summary['local_migration'] = resources.migrate(Path(cfg['archive']), resources.policy(Path(cfg['archive'])), branch=branch)
        except (OSError, RuntimeError, ValueError) as error:
            summary['migration_error'] = str(error)
            print('Local attachments: ' + str(error), file=sys.stderr)
        summaries.append(summary)
        if not upload or network_error:
            continue
        # Only files authored by this exporter are staged. Source directories are never staged.
        for path in [*dest.glob('sessions/**/*.md'), dest / 'README.md', dest / 'index.json']:
            if path.stat().st_size >= 100 * 1024**2:
                raise RuntimeError('Text file exceeds GitHub size limit: ' + str(path))
        git(dest, 'add', '--', '.gitignore', 'README.md', 'index.json')
        if (dest / 'sessions').exists(): git(dest, 'add', '--', 'sessions')
        staged = git(dest, 'diff', '--cached', '--name-only').stdout.splitlines()
        if any(p not in ('.gitignore', 'README.md', 'index.json') and
               not (p.startswith('sessions/') and p.endswith('.md')) for p in staged):
            raise RuntimeError('Unexpected staged files; upload stopped. Only text archives may be uploaded.')
        if git(dest, 'diff', '--cached', '--quiet', check=False).returncode:
            git(dest, 'commit', '-m', 'Archive ' + tool + ' conversations')
        git(dest, 'push', '-u', 'origin', 'HEAD:refs/heads/' + branch, capture=False)
    write_json(LOCAL / 'last-run.json', {'completed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                       'uploaded': upload and not network_error, 'results': summaries})
    if network_error:
        raise RuntimeError('Local backup completed, but GitHub upload failed: ' + str(network_error))


def schedule(install):
    if sys.platform != 'darwin':
        raise RuntimeError('Built-in scheduling supports macOS. Use Task Scheduler or cron with sync on other systems.')
    label = 'com.cpython666.chat-history-vault'
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    domain = 'gui/' + str(os.getuid())
    if install:
        config()
        LOCAL.mkdir(parents=True, exist_ok=True)
        job = {'Label': label, 'ProgramArguments': [sys.executable, str(ROOT / 'vault.py'), 'sync'],
               'WorkingDirectory': str(ROOT), 'StartInterval': 3600, 'RunAtLoad': True,
               'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                        'CHAT_HISTORY_VAULT_CONFIG_DIR': str(LOCAL)},
               'StandardOutPath': str(LOCAL / 'schedule.log'), 'StandardErrorPath': str(LOCAL / 'schedule-error.log')}
        if path.exists(): run(['launchctl', 'bootout', domain + '/' + label], check=False)
        write(path, plistlib.dumps(job))
        run(['launchctl', 'bootstrap', domain, str(path)])
        print('Hourly sync installed: ' + str(path))
    else:
        run(['launchctl', 'bootout', domain + '/' + label], check=False)
        path.unlink(missing_ok=True)
        print('Hourly sync removed.')


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Configure this computer; no upload')
    init.add_argument('--repo', required=True)
    init.add_argument('--device', default=default_device())
    default_archive = (Path.home() / 'Library/Application Support/ChatHistoryVault/backup'
                       if sys.platform == 'darwin' else Path.home() / 'ChatHistoryBackup')
    init.add_argument('--archive', default=str(default_archive))
    init.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    init.add_argument('--claude-home', default=os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    init.add_argument('--claude-desktop-home', default=str(claude_desktop.default_home()))
    init.add_argument('--openclaw-home', default=os.environ.get('OPENCLAW_HOME', str(Path.home() / '.openclaw')))
    init.add_argument('--workbuddy-home', default=os.environ.get('WORKBUDDY_HOME', str(Path.home() / '.workbuddy-ai')))
    init.add_argument('--dsh-home', default=os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
    for name in ['export', 'sync']:
        command = sub.add_parser(name, help='Export locally' if name == 'export' else 'Export, commit and push both branches')
        command.add_argument('--tool', choices=[*TOOLS, 'all'], default='all')
    migration = sub.add_parser('migrate', help='Plan/copy associated attachments locally; never upload')
    migration.add_argument('--archive')
    migration.add_argument('--kinds', default='images')
    migration.add_argument('--branch')
    migration.add_argument('--session')
    migration.add_argument('--file-id', action='append')
    migration.add_argument('--plan', action='store_true')
    migration.add_argument('--remember', action='store_true')
    attachment_policy = sub.add_parser('attachment-policy', help='Show or save file types copied during future backups; copies no files')
    attachment_policy.add_argument('--archive')
    attachment_policy.add_argument('--kinds', help='none, images, all, or comma-separated attachment kinds')
    skills = sub.add_parser('skills', help='Collect or restore portable Skill folders locally; never upload')
    skills.add_argument('--archive')
    skills.add_argument('--source', action='append', help='Additional Skill folder or collection root; repeatable')
    defaults = skills.add_mutually_exclusive_group()
    defaults.add_argument('--defaults', dest='defaults', action='store_true', default=None)
    defaults.add_argument('--no-defaults', dest='defaults', action='store_false')
    action = skills.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true', help='Scan and estimate without copying')
    action.add_argument('--list', action='store_true', help='List saved Skill packages and versions')
    action.add_argument('--restore', metavar='ID', help='Restore a saved Skill into a new folder')
    action.add_argument('--forget', action='store_true', help='Stop automatic Skill backups on this computer')
    skills.add_argument('--destination', type=Path)
    skills.add_argument('--version', help='Saved version hash; defaults to the latest')
    skills.add_argument('--remember', action='store_true', help='Reuse these sources during existing export/sync runs')
    chatgpt = sub.add_parser('import-chatgpt', help='Import ChatGPT conversations.json, ZIP or desktop snapshot')
    chatgpt.add_argument('path', type=Path)
    chatgpt.add_argument('--archive')
    sub.add_parser('status')
    sub.add_parser('install-schedule')
    sub.add_parser('remove-schedule')
    args = parser.parse_args(argv)
    if args.command == 'init':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', args.device):
            parser.error('device must use 1–80 ASCII letters, digits, hyphens or underscores')
        if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?', args.repo):
            parser.error('Use an HTTPS github.com repository URL')
        existing = read_json(LOCAL / 'config.json')
        if existing and (existing['repo'] != args.repo or existing['device'] != args.device):
            raise RuntimeError('Already configured for another repo/device. Choose a separate CHAT_HISTORY_VAULT_CONFIG_DIR.')
        write_json(LOCAL / 'config.json', {'repo': args.repo, 'device': args.device,
                                          'archive': str(Path(args.archive).expanduser().resolve()),
                                          'codex_home': str(Path(args.codex_home).expanduser().resolve()),
                                          'claude_home': str(Path(args.claude_home).expanduser().resolve()),
                                          'claude_desktop_home': str(Path(args.claude_desktop_home).expanduser().resolve()),
                                          'openclaw_home': str(Path(args.openclaw_home).expanduser().resolve()),
                                          'workbuddy_home': str(Path(args.workbuddy_home).expanduser().resolve()),
                                          'dsh_home': str(Path(args.dsh_home).expanduser().resolve())})
        archive = Path(args.archive).expanduser().resolve()
        write(archive / 'README.md', '# 本地聊天备份\n\n'
              '请复制整个文件夹，包括 images/ 和各电脑-工具目录。\n\n'
              'images/ 保存按 SHA-256 去重的原始图片，永不上传 GitHub。\n'
              '每个电脑-工具目录保存文字、索引以及独立 Git 仓库；对应同名远端分支。\n'
              '文字里的图片链接使用相对路径，完整搬迁本目录后仍然有效。\n'
              '源会话被删除后，本备份仍保留已导出的内容。\n'
              '这是阅读归档，不是 Codex/Claude/OpenClaw/WorkBuddy/DeepSeek Harness 原生会话数据库的替代品。\n')
        print('Branches: ' + ', '.join(args.device + '-' + tool for tool in TOOLS))
    elif args.command in ('export', 'sync'):
        with lock():
            cfg = config(); selected = enabled_tools(cfg)
            sync(cfg, selected if args.tool == 'all' else [args.tool], args.command == 'sync')
    elif args.command == 'attachment-policy':
        with lock():
            root = Path(args.archive or config()['archive']).expanduser().resolve()
            if args.kinds is None:
                result = {'kinds': sorted(resources.policy(root)), 'settings_only': True}
            else:
                kinds = ([] if args.kinds == 'none' else list(resources.KINDS) if args.kinds == 'all'
                         else list(filter(None, args.kinds.split(','))))
                result = resources.save_policy(root, kinds)
            print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'migrate':
        with lock():
            root = Path(args.archive or config()['archive']).expanduser().resolve()
            kinds = list(filter(None, args.kinds.split(','))) if args.kinds != 'all' else list(resources.KINDS)
            unknown = set(kinds) - set(resources.KINDS)
            if unknown: raise ValueError('Unknown attachment kinds: ' + ','.join(unknown))
            print(json.dumps(resources.migrate(root, kinds, args.branch, args.session, args.file_id, args.plan, args.remember), ensure_ascii=False))
    elif args.command == 'skills':
        if args.restore and not args.destination:
            parser.error('--restore requires --destination')
        if not args.restore and (args.destination or args.version):
            parser.error('--destination and --version require --restore')
        if args.remember and (args.plan or args.list or args.restore or args.forget):
            parser.error('--remember is only valid when copying Skills')
        if (args.list or args.restore or args.forget) and (args.source or args.defaults is not None):
            parser.error('Source options are only valid when scanning or copying Skills')
        with lock():
            cfg = read_json(LOCAL / 'config.json', {})
            archive = args.archive or cfg.get('archive')
            if not archive:
                parser.error('Specify --archive or run init first')
            root = Path(archive).expanduser().resolve()
            saved = skill_policy(root)
            settings = saved or {'defaults': True, 'sources': [], 'project': str(Path.cwd())}
            if args.source is not None or args.defaults is not None:
                settings = {'defaults': args.defaults is not False,
                            'sources': [str(Path(p).expanduser().resolve()) for p in (args.source or [])],
                            'project': str(Path.cwd())}
            if args.list:
                result = skills_backup.list_backups(root)
            elif args.restore:
                result = skills_backup.restore(root, args.restore, args.destination.expanduser(), args.version)
            elif args.forget:
                save_skill_policy(root, None)
                saved = None
                result = {'automatic_backup': False}
            else:
                result = collect_skills(root, cfg, settings, plan=args.plan)
                if args.remember:
                    save_skill_policy(root, settings)
                    saved = settings
            result.update(settings=settings, remembered=saved is not None)
            print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'import-chatgpt':
        import chatgpt_import
        with lock():
            count = chatgpt_import.import_file(args.path.expanduser(), LOCAL / 'imports/chatgpt')
            cfg = config()
            if args.archive: cfg['archive'] = str(Path(args.archive).expanduser().resolve())
            sync(cfg, ['chatgpt'], False)
            print(json.dumps({'imported_chatgpt': count}, ensure_ascii=False))
    elif args.command == 'status':
        print(json.dumps({'config': config(), 'last_run': read_json(LOCAL / 'last-run.json')}, ensure_ascii=False, indent=2))
    else:
        schedule(args.command == 'install-schedule')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
