"""Local-only text originals, file associations and incremental attachment copies."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse

import archive_api

KINDS = ('images', 'models', 'documents', 'code', 'logs', 'other')
EXTENSIONS = {
    'images': {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.heic'},
    'models': {'.glb', '.gltf', '.blend', '.blend1', '.fbx', '.obj', '.mtl', '.usd', '.usdz', '.stl'},
    'documents': {'.pdf', '.docx', '.xlsx', '.pptx', '.csv', '.md'},
    'code': {'.py', '.rs', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.gd', '.gdshader', '.tscn', '.tres', '.sh', '.json', '.toml', '.yaml', '.yml'},
    'logs': {'.log', '.txt'},
    'other': {'.zip', '.mp4', '.mov', '.mp3', '.wav', '.ogg', '.bin', '.ttf'},
}
SUFFIXES = '|'.join(re.escape(s[1:]) for ss in EXTENSIONS.values() for s in ss)
FILE_TOKEN = re.compile(r"(?<![\w.@~+%/\\-])(?:[A-Za-z]:[\\/]|/|~/|\.?\.?/)?[\w.@~+%/\\-]+\.(?:" + SUFFIXES + r")\b", re.I)
LINK = re.compile(r'!?\[[^\]]*\]\((<[^>]+>|[^)\n]+)\)')
QUOTED = re.compile(r'''["'`]([^"'`\n]+\.(?:''' + SUFFIXES + r'''))["'`]''', re.I)


def read(path, default=None):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: return default


def write(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    if path.exists() and path.read_bytes() == data: return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name); f.write(data)
    temp.replace(path)


def policy(root):
    root = Path(root).resolve()
    path = archive_api.safe_path(root, '.migration.json')
    return set(read(path, {}).get('kinds', ['images'])) & set(KINDS)


def save_policy(root, kinds):
    """Save future copy choices independently of copying any current files."""
    root = Path(root).expanduser().resolve()
    kinds = set(kinds)
    if kinds - set(KINDS):
        raise ValueError('Unknown attachment kinds: ' + ','.join(sorted(kinds - set(KINDS))))
    path = archive_api.safe_path(root, '.migration.json')
    write(path, {'kinds': sorted(kinds)})
    return {'kinds': sorted(kinds), 'settings_only': True}


def kind_for(path):
    return next((k for k, extensions in EXTENSIONS.items() if path.suffix.lower() in extensions), 'other')


def classification_tags(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('tags', 'labels', 'categories', 'category', 'mode', 'conversation_mode', 'agent_role', 'model_provider', 'reasoning_effort'):
                values = item if isinstance(item, list) else [item]
                for v in values:
                    if isinstance(v, (str, int, bool)) and str(v): result.add(key + ':' + str(v))
            if isinstance(item, dict): result.update(classification_tags(item))
    return result


def collect(value, cwd, ordinal, refs):
    """Only associate explicitly mentioned files; never crawl project directories."""
    if isinstance(value, dict):
        for item in value.values(): collect(item, cwd, ordinal, refs)
    elif isinstance(value, list):
        for item in value: collect(item, cwd, ordinal, refs)
    elif isinstance(value, str):
        candidates = [m.group(1).strip('<>') for m in LINK.finditer(value)]
        candidates += [m.group(1) for m in QUOTED.finditer(value)]
        candidates += FILE_TOKEN.findall(QUOTED.sub('', LINK.sub('', value)))
        for name in candidates:
            if '://' in name and not name.startswith('file://'): continue
            if name.startswith(('data:', 'vault-image:', 'sandbox:')) or any(c in name for c in ('{', '}')): continue
            original = name
            name = urllib.parse.unquote(name.removeprefix('file://'))
            name = re.sub(r':\d+(?::\d+)?$', '', name)
            path = Path(name).expanduser()
            if not path.is_absolute():
                if not cwd: continue
                path = Path(cwd) / path
            try: path = path.resolve()
            except (OSError, RuntimeError, ValueError): continue
            # Credentials are not conversation attachments, even when a command mentions them.
            if path.name in ('auth.json', 'credentials.json', '.env') or any(p in path.parts for p in ('.ssh', '.aws')): continue
            key = str(path)
            if key not in refs:
                refs[key] = {'id': hashlib.sha256(key.encode()).hexdigest()[:24], 'source': key,
                             'name': path.name, 'kind': kind_for(path), 'records': [], 'original_paths': []}
            if original not in refs[key]['original_paths']: refs[key]['original_paths'].append(original)
            if ordinal not in refs[key]['records']: refs[key]['records'].append(ordinal)


def stat_ref(ref):
    try:
        p = Path(ref['source']); s = p.stat()
        ref.update(exists=p.is_file(), size=s.st_size if p.is_file() else 0, mtime_ns=s.st_mtime_ns)
    except OSError: ref.update(exists=False, size=0, mtime_ns=0)


def saved_current(root, ref):
    return bool(ref.get('copy') and (root / ref['copy']).is_file()
                and ref.get('copied_size') == ref.get('size') and ref.get('copied_mtime_ns') == ref.get('mtime_ns'))


def existing_object(root, ref):
    """Reconnect files already copied by image extraction, without copying them again."""
    if not ref['exists'] or saved_current(root, ref): return
    objects = root / ('images' if ref['kind'] == 'images' else 'attachments')
    if not objects.is_dir(): return
    digest = hashlib.sha256()
    with Path(ref['source']).open('rb') as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): return
    ext = Path(ref['source']).suffix.lower()
    if ext == '.jpeg': ext = '.jpg'
    target = objects / (digest.hexdigest() + ext)
    if target.is_file():
        ref.update(copy=target.relative_to(root).as_posix(), sha256=digest.hexdigest(),
                   copied_size=after.st_size, copied_mtime_ns=after.st_mtime_ns)


def copy_file(root, ref):
    """Streaming, content-addressed copies. Changed originals keep previous versions."""
    stat_ref(ref)
    if not ref['exists'] or saved_current(root, ref): return False
    if shutil.disk_usage(root).free < ref['size'] + 256 * 1024**2: raise RuntimeError('磁盘空间不足，原文件和已完成备份均保留')
    objects = root / ('images' if ref['kind'] == 'images' else 'attachments')
    objects.mkdir(exist_ok=True)
    temp = None
    try:
        digest = hashlib.sha256()
        with Path(ref['source']).open('rb') as source, tempfile.NamedTemporaryFile(dir=objects, delete=False) as out:
            temp = Path(out.name); before = os.fstat(source.fileno())
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk: break
                digest.update(chunk); out.write(chunk)
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError('文件正在变化，下次重试：' + ref['source'])
        ext = Path(ref['source']).suffix.lower()
        if ext == '.jpeg': ext = '.jpg'
        target = objects / (digest.hexdigest() + (ext if ext in {s for v in EXTENSIONS.values() for s in v} else '.bin'))
        if target.exists(): temp.unlink()
        else: temp.replace(target)
        versions = ref.setdefault('versions', [])
        if ref.get('copy') and ref['copy'] not in versions: versions.append(ref['copy'])
        ref.update(copy=target.relative_to(root).as_posix(), sha256=digest.hexdigest(),
                   copied_size=after.st_size, copied_mtime_ns=after.st_mtime_ns)
        return True
    finally:
        if temp: temp.unlink(missing_ok=True)


def finish(root, branch, sid, source, refs, operation_rows, metadata, dialogue, write_text):
    directory = root / 'records' / branch / sid
    path = directory / 'manifest.json'
    previous = read(path, {})
    old = {r['source']: r for r in previous.get('files', [])}
    for key, ref in refs.items():
        retained = {k: v for k, v in old.get(key, {}).items() if k in ('copy', 'sha256', 'copied_size', 'copied_mtime_ns', 'versions')}
        for field, value in retained.items(): ref.setdefault(field, value)
        stat_ref(ref)
    # Keep references and copies even if a compacted source no longer mentions them.
    for key, ref in old.items(): refs.setdefault(key, ref)
    manifest = {**previous, 'source_file': str(source), 'branch': branch, 'session_id': sid,
                'files': list(refs.values()), 'operation_rows': operation_rows,
                'metadata': metadata, 'raw_text': 'dialogue.jsonl'}
    write_text(directory / 'dialogue.jsonl', '\n'.join(dialogue) + '\n')
    write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return path


def manifests(root, branch=None, session=None):
    return [p for p in (root / 'records').glob('*/*/manifest.json')
            if (not branch or p.parent.parent.name == branch) and (not session or p.parent.name == session)]


def source_path(root, manifest, data):
    """Archive copies move with their manifest, even when an old absolute path still exists."""
    relative = data.get('metadata', {}).get('source_copy')
    if relative is not None:
        return archive_api.safe_path(root, relative)
    source = data['source_file']
    parts = source.replace('\\', '/').split('/')
    if (len(parts) >= 4 and parts[-4:-1] == ['records', data['branch'], data['session_id']]
            and parts[-1] in ('source.jsonl', 'source.jsonl.zst', 'source.jsonl.zstd')):
        relative = (manifest.parent / parts[-1]).relative_to(root).as_posix()
        return archive_api.safe_path(root, relative)
    return Path(source)


def source_stream(source):
    """Offsets refer to decoded JSONL; ordinary logs remain streaming file reads."""
    if source.suffix.lower() in ('.zst', '.zstd'):
        import vault
        return io.BytesIO(vault.zstd_decompress(source.read_bytes()))
    return source.open('rb')


def migrate(root, kinds, branch=None, session=None, selected=None, plan=False, remember=False):
    # Local import avoids a module cycle; the exporter supplies the image de-duplication logic.
    import vault
    root = Path(root).resolve()
    kinds = set(kinds) & set(KINDS)
    if not plan:
        estimate = migrate(root, kinds, branch, session, selected, plan=True)
        if not estimate['fits']: raise RuntimeError('所选附件的估算空间超过磁盘可用空间，请减少选择或换到更大的磁盘')
    paths = manifests(root, branch, session)
    needed = 0; missing = 0; files = 0; copied = 0; operations = 0; seen = set(); shared_copies = {}
    for path in paths:
        m = read(path); dest = root / m['branch']
        if m['branch'] != path.parent.parent.name or m['session_id'] != path.parent.name:
            raise ValueError('附件清单的会话路径与标识不匹配')
        for ref in m['files']:
            if ref['kind'] not in kinds or (selected and ref['id'] not in selected): continue
            stat_ref(ref)
            if not ref['exists']:
                if not (ref.get('copy') and (root / ref['copy']).is_file()): missing += 1
                continue
            files += 1
            if ref['source'] in shared_copies: ref.update(shared_copies[ref['source']])
            elif not saved_current(root, ref):
                existing_object(root, ref)
                if saved_current(root, ref): shared_copies[ref['source']] = {k: v for k, v in ref.items() if k in ('copy', 'sha256', 'copied_size', 'copied_mtime_ns')}
            if not saved_current(root, ref) and ref['source'] not in seen:
                needed += ref['size']; seen.add(ref['source'])
            if not plan:
                if ref['source'] in shared_copies: ref.update(shared_copies[ref['source']])
                else:
                    copied += copy_file(root, ref)
                    shared_copies[ref['source']] = {k: v for k, v in ref.items() if k in ('copy', 'sha256', 'copied_size', 'copied_mtime_ns')}
        source = source_path(root, path, m)
        if 'logs' in kinds and not selected and m.get('operation_rows'):
            operations += len(m['operation_rows'])
            if source.is_file():
                stat = source.stat()
                rows_hash = hashlib.sha256(json.dumps(m['operation_rows'], sort_keys=True,
                                                       separators=(',', ':')).encode()).hexdigest()
                signature = [stat.st_size, stat.st_mtime_ns, rows_hash]
                target = path.parent / 'operations.jsonl'
                if m.get('operations_signature') != signature or not target.exists():
                    needed += sum(r['length'] for r in m['operation_rows'])
                    if not plan:
                        # Export only the referenced, completed records, with image bytes externalized.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = None
                        try:
                            with source_stream(source) as stream, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                                temporary = Path(out.name)
                                for row in m['operation_rows']:
                                    stream.seek(row['offset']); raw = stream.read(row['length'])
                                    if hashlib.sha256(raw).hexdigest() != row['sha256']:
                                        raise RuntimeError('源记录发生变化，请先刷新文字备份再迁移日志')
                                    out.write(vault.extract_images(raw, dest, set(), copy=False))
                            temporary.replace(target)
                        finally:
                            if temporary is not None: temporary.unlink(missing_ok=True)
                        m['operations_signature'] = signature; m['operations_file'] = 'operations.jsonl'
            elif not (path.parent / 'operations.jsonl').is_file(): missing += 1
        # Embedded images may have no original standalone file. Re-extract only when absent.
        if 'images' in kinds and not selected:
            names = m.get('metadata', {}).get('assets', [])
            absent = [name for name in names if not (root / 'images' / name).is_file()]
            if absent and source.suffix.lower() in ('.jsonl', '.zst', '.zstd') and source.is_file():
                with source_stream(source) as stream:
                    needed += stream.seek(0, os.SEEK_END)  # decoded size bounds unknown embedded images
                    if not plan:
                        stream.seek(0)
                        for raw in stream:
                            if raw.endswith(b'\n'): vault.extract_images(raw, dest, set(), copy=True)
                        missing += sum(not (root / 'images' / name).is_file() for name in absent)
            elif absent: missing += len(absent)
        if not plan: write(path, m)
    free = shutil.disk_usage(root).free
    result = {'files': files, 'operations': operations, 'missing': missing, 'copied': copied,
              'estimated_bytes': needed, 'free_bytes': free, 'fits': needed + 256 * 1024**2 <= free,
              'scope_sessions': len(paths), 'kinds': sorted(kinds), 'plan': plan}
    if remember and not plan:
        save_policy(root, kinds)
    return result
