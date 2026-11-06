"""Local, portable Skill folder snapshots. Skill text is data, never executed."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile


EXCLUDED = {'.git', '.hg', '.svn', '__pycache__', '.pytest_cache', '.mypy_cache',
            '.ruff_cache', '.venv', 'venv', 'node_modules'}
RESERVE = 256 * 1024**2
WINDOWS = os.name == 'nt'
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + n for prefix in ('COM', 'LPT') for n in '123456789¹²³'}


def default_sources(codex_home=None, claude_home=None, project=None):
    home = Path.home()
    codex = Path(codex_home if codex_home is not None else os.environ.get('CODEX_HOME', home / '.codex'))
    claude = Path(claude_home if claude_home is not None else os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'))
    project = Path(project) if project is not None else Path.cwd()
    paths = [codex / 'skills', claude / 'skills', home / '.agents/skills']
    paths += [project / folder / 'skills' for folder in ('.agents', '.codex', '.claude')]
    return list(dict.fromkeys(p.expanduser().resolve() for p in paths))


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _portable(path):
    """Validate ZIP names on every OS, including Windows drive/device syntax."""
    if not path or path.startswith('/') or '\\' in path or path.endswith('/'):
        raise ValueError('不可移植的 Skill 路径：' + repr(path))
    for part in path.split('/'):
        if (part in ('', '.', '..') or part[-1:] in (' ', '.') or
                any(ord(c) < 32 or c in '<>:"|?*\x7f' for c in part) or
                part.split('.')[0].rstrip(' ').upper() in RESERVED or
                len(part.encode('utf-8')) > 255 or len(part.encode('utf-16-le')) > 510):
            raise ValueError('不可移植的 Skill 路径：' + repr(path))
    return PurePosixPath(path).parts


def _validate_paths(entries):
    seen = {}
    kinds = {}
    for entry in entries:
        name = entry['path']
        parts = _portable(name)
        for i in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:i])
            key = unicodedata.normalize('NFC', prefix).casefold()
            if key in seen and seen[key] != prefix:
                raise ValueError('Skill 路径大小写或 Unicode 冲突：' + prefix)
            seen[key] = prefix
            kind = entry['kind'] if i == len(parts) else 'dir'
            if prefix in kinds and kinds[prefix] != kind:
                raise ValueError('Skill 文件与目录冲突：' + prefix)
            kinds[prefix] = kind


def _state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, stat.S_IMODE(info.st_mode))


def _open_regular(path, expected=None):
    if _linked(path) or not path.is_file():
        raise ValueError('Skill 文件不是普通文件：' + str(path))
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    stream = os.fdopen(os.open(path, flags), 'rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or (
            expected is not None and _state(os.fstat(stream.fileno())) != expected):
        actual_state = _state(os.fstat(stream.fileno()))
        stream.close()
        raise RuntimeError('Skill 源文件正在变化，请重试：' + str(path) + ' ' + repr((expected, actual_state, _state(os.stat(path)))))
    return stream


def _hash_stream(stream, output=None):
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        if output is not None:
            output.write(chunk)


def _walk(path, warnings, excluded=True, collected_links=()):
    def error(exc):
        raise exc
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=error):
        base = Path(directory)
        if _linked(base):
            raise ValueError('Skill 目录已变为链接：' + str(base))
        for name in sorted(dirs[:]):
            child = base / name
            if _linked(child) or (excluded and name in EXCLUDED):
                if not excluded:
                    raise ValueError('恢复目标含链接目录：' + str(child))
                dirs.remove(name)
                reason = '符号链接或目录联接' if _linked(child) else '生成目录'
                if child not in collected_links:
                    warnings.append('已排除' + reason + '：' + str(child))
        dirs.sort()
        yield base, dirs, sorted(files)


def _discover(sources, warnings):
    roots = set()
    for source in sources:
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            warnings.append('Skill 来源目录不可用：' + str(source))
            continue
        collected_links = set()
        if not (source / 'SKILL.md').exists():
            # Shared installations commonly link a collection's direct child to one whole Skill.
            # Resolve only these explicit roots; never follow arbitrary links within a package.
            for child in sorted(source.iterdir()):
                if child.name in EXCLUDED or not _linked(child) or not child.is_dir():
                    continue
                target = child.resolve()
                marker = target / 'SKILL.md'
                if marker.is_file() and not _linked(marker):
                    roots.add(target)
                    collected_links.add(child)
        for base, dirs, files in _walk(source, warnings, collected_links=collected_links):
            if 'SKILL.md' in files:
                if _linked(base / 'SKILL.md'):
                    warnings.append('已排除 SKILL.md 为链接的目录：' + str(base))
                    dirs.clear()
                    continue
                roots.add(base)
                dirs.clear()  # The entire Skill folder, including nested assets, is one snapshot.
    return sorted(roots)


def _snapshot(path, warnings, excluded=True):
    entries = []
    for base, dirs, files in _walk(path, warnings, excluded):
        for name in dirs:
            entries.append({'path': (base / name).relative_to(path).as_posix(), 'kind': 'dir'})
        for name in files:
            child = base / name
            if _linked(child) or not stat.S_ISREG(child.lstat().st_mode):
                if not excluded:
                    raise ValueError('恢复目标含非普通文件：' + str(child))
                warnings.append('已排除链接或非普通文件：' + str(child))
                continue
            before = _state(child.stat())
            with _open_regular(child, before) as stream:
                digest = _hash_stream(stream)
                if _state(os.fstat(stream.fileno())) != before or _state(child.stat()) != before:
                    raise RuntimeError('Skill 源文件正在变化，请重试：' + str(child))
            entries.append({'path': child.relative_to(path).as_posix(), 'kind': 'file',
                            'bytes': before[2], 'executable': before[5] & 0o111,
                            'sha256': digest, '_state': before})
    entries.sort(key=lambda entry: entry['path'])
    _validate_paths(entries)
    return entries


def _version(entries):
    values = [{k: v for k, v in e.items() if k != '_state'} for e in entries]
    # macOS can normalize filenames on disk; the same portable tree keeps its version.
    for value in values:
        value['path'] = unicodedata.normalize('NFC', value['path'])
    values.sort(key=lambda value: value['path'])
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _free(path):
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _storage(root, relative):
    path = root
    for part in _portable(relative):
        path /= part
        if path.exists() or path.is_symlink():
            if _linked(path):
                raise ValueError('备份路径不能是链接：' + str(path))
    return path


def _read_index(root):
    path = _storage(root, 'skills/index.json')
    if not path.exists():
        return {'format_version': 1, 'items': []}
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('format_version') != 1 or not isinstance(value.get('items'), list):
        raise ValueError('不支持的 Skill 备份索引')
    return value


def _write_index(root, value):
    path = _storage(root, 'skills/index.json')
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()
    if path.exists() and path.read_bytes() == data:
        return
    if _free(path.parent) < len(data) + RESERVE:
        raise RuntimeError('磁盘空间不足，已有 Skill 备份保留')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _zip_entries(package):
    entries = []
    seen = set()
    for info in package.infolist():
        name = info.filename[:-1] if info.is_dir() else info.filename
        _portable(name)
        if name in seen or info.orig_filename != info.filename:
            raise ValueError('Skill ZIP 包含重复或非法路径：' + repr(name))
        seen.add(name)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if info.flag_bits & 1 or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError('Skill ZIP 包含加密内容、链接或非普通文件：' + name)
        if kind == stat.S_IFDIR and not info.is_dir() or kind == stat.S_IFREG and info.is_dir():
            raise ValueError('Skill ZIP 文件类型不一致：' + name)
        entry = {'path': name, 'kind': 'dir' if info.is_dir() else 'file'}
        if not info.is_dir():
            with package.open(info) as stream:
                digest = _hash_stream(stream)
            entry.update(bytes=info.file_size, executable=mode & 0o111, sha256=digest)
        entries.append(entry)
    entries.sort(key=lambda entry: entry['path'])
    _validate_paths(entries)
    if not any(e['path'] == 'SKILL.md' and e['kind'] == 'file' for e in entries):
        raise ValueError('Skill ZIP 缺少 SKILL.md')
    return entries


def _check_zip(path, version):
    with _open_regular(path) as stream, zipfile.ZipFile(stream) as package:
        entries = _zip_entries(package)
    if _version(entries) != version:
        raise ValueError('Skill ZIP 内容校验失败：' + str(path))
    return entries


def _write_zip(path, source, entries, version):
    size = sum(e.get('bytes', 0) for e in entries)
    estimate = size + size // 100 + sum(len(e['path'].encode()) * 2 + 256 for e in entries) + 1024
    if _free(path.parent) < estimate + RESERVE:
        raise RuntimeError('磁盘空间不足，已有 Skill 备份保留')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for entry in entries:
                name = entry['path']
                directory = entry['kind'] == 'dir'
                info = zipfile.ZipInfo(name + ('/' if directory else ''), (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if directory else 0o644 | entry.get('executable', 0)
                info.external_attr = ((stat.S_IFDIR if directory else stat.S_IFREG) | mode) << 16
                if directory:
                    info.external_attr |= 0x10
                    package.writestr(info, b'')
                    continue
                child = source.joinpath(*PurePosixPath(name).parts)
                for ancestor in child.parents:
                    if ancestor == source.parent:
                        break
                    if _linked(ancestor):
                        raise ValueError('Skill 源目录已变为链接：' + str(ancestor))
                with _open_regular(child, entry['_state']) as original, package.open(info, 'w', force_zip64=True) as out:
                    digest = _hash_stream(original, out)
                    if (digest != entry['sha256'] or _state(os.fstat(original.fileno())) != entry['_state'] or
                            _state(child.stat()) != entry['_state']):
                        raise RuntimeError('Skill 源文件正在变化，请重试：' + str(child))
        if _snapshot(source, []) != entries:
            raise RuntimeError('Skill 源目录正在变化，请重试：' + str(source))
        _check_zip(temporary, version)
        if _free(path.parent) < RESERVE:
            raise RuntimeError('磁盘空间不足，已有 Skill 备份保留')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def backup(root, sources, plan=False):
    root = Path(root).expanduser().resolve()
    index = _read_index(root)
    old = {item['id']: item for item in index['items']}
    warnings = []
    work = []
    needed = 0
    seen = set()
    for source in _discover(sources, warnings):
        if root == source or source in root.parents:
            raise ValueError('备份目录不能位于 Skill 来源内部：' + str(source))
        warning_start = len(warnings)
        entries = _snapshot(source, warnings)
        if not any(e['path'] == 'SKILL.md' for e in entries):
            raise ValueError('Skill 来源缺少普通文件 SKILL.md：' + str(source))
        version = _version(entries)
        identity = hashlib.sha256(os.path.normcase(str(source)).encode()).hexdigest()
        archive = 'skills/objects/' + version + '.zip'
        path = _storage(root, archive)
        size = sum(e.get('bytes', 0) for e in entries)
        item = {'id': identity, 'name': source.name, 'source': str(source), 'version': version,
                'archive': archive, 'files': sum(e['kind'] == 'file' for e in entries), 'bytes': size}
        item['warnings'] = list(dict.fromkeys(warnings[warning_start:]))
        versions = list(old.get(identity, {}).get('versions', []))
        if not any(v['version'] == version for v in versions):
            versions.append({k: item[k] for k in ('version', 'archive', 'files', 'bytes')})
        item['versions'] = versions
        if path.exists():
            _check_zip(path, version)
        elif version not in seen:
            needed += size + size // 100 + sum(len(e['path'].encode()) * 2 + 256 for e in entries) + 1024
            seen.add(version)
        work.append((source, entries, item))
    free = _free(root)
    result = {'skills': len(work), 'files': sum(item['files'] for _, _, item in work), 'copied': 0,
              'estimated_bytes': needed, 'free_bytes': free, 'fits': needed + RESERVE <= free,
              'warnings': list(dict.fromkeys(warnings)), 'items': [item for _, _, item in work], 'plan': plan}
    if plan:
        return result
    if not result['fits']:
        raise RuntimeError('Skill 备份估算空间超过磁盘可用空间')
    for source, entries, item in work:
        path = _storage(root, item['archive'])
        if not path.exists():
            _write_zip(path, source, entries, item['version'])
            result['copied'] += 1
        old[item['id']] = item
    if work:
        index['items'] = sorted(old.values(), key=lambda item: item['id'])
        _write_index(root, index)
    return result


def list_backups(root):
    items = _read_index(Path(root).expanduser().resolve())['items']
    items.sort(key=lambda item: (item['name'].casefold(), item.get('source', '').casefold()))
    return {'items': items, 'warnings': list(dict.fromkeys(
        warning for item in items for warning in item.get('warnings', [])))}


def _destination_name(name, identity):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '-', name).strip(' .') or 'skill'
    while len(name.encode('utf-8')) > 160:
        name = name[:-1]
    if name.split('.')[0].rstrip(' ').upper() in RESERVED:
        name = 'skill-' + name
    return name + '-' + identity[:12]


def restore(root, skill_id, destination, version=None):
    root = Path(root).expanduser().resolve()
    matches = [item for item in list_backups(root)['items'] if item['id'] == skill_id]
    if not matches:
        raise ValueError('未找到 Skill 备份：' + str(skill_id))
    item = matches[0]
    if not re.fullmatch('[0-9a-f]{64}', item['id']):
        raise ValueError('Skill 备份 ID 无效')
    version = version or item['version']
    selected = next((v for v in item['versions'] if v['version'] == version), None)
    if selected is None or not re.fullmatch('[0-9a-f]{64}', version):
        raise ValueError('未找到 Skill 备份版本：' + str(version))
    expected_path = 'skills/objects/' + version + '.zip'
    if selected['archive'] != expected_path:
        raise ValueError('Skill ZIP 存储路径无效')
    archive = _storage(root, expected_path)
    entries = _check_zip(archive, version)
    destination = Path(destination).expanduser().resolve()
    target = destination / _destination_name(item['name'], item['id'])
    result = {'restored': str(target), 'files': sum(e['kind'] == 'file' for e in entries),
              'id': skill_id, 'version': version, 'skipped': False}
    if target.exists() or target.is_symlink():
        if not _linked(target) and target.is_dir():
            existing = _snapshot(target, [], excluded=False)
            if WINDOWS:
                # Windows chmod cannot represent POSIX execute bits; still compare every path and byte.
                execute = {unicodedata.normalize('NFC', e['path']): e.get('executable', 0) for e in entries}
                for entry in existing:
                    if entry['kind'] == 'file':
                        entry['executable'] = execute.get(unicodedata.normalize('NFC', entry['path']), 0)
            if _version(existing) == version:
                result['skipped'] = True
                return result
        raise FileExistsError('恢复目标已存在且内容不同，未覆盖：' + str(target))
    size = sum(e.get('bytes', 0) for e in entries)
    if _free(destination) < size + RESERVE:
        raise RuntimeError('磁盘空间不足，未恢复 Skill')
    destination.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.skill-restore-', dir=destination))
    try:
        with _open_regular(archive) as stream, zipfile.ZipFile(stream) as package:
            if _version(_zip_entries(package)) != version:
                raise ValueError('Skill ZIP 内容校验失败')
            for entry in entries:
                path = temporary.joinpath(*_portable(entry['path']))
                if entry['kind'] == 'dir':
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if _free(destination) < entry['bytes'] + RESERVE:
                    raise RuntimeError('磁盘空间不足，未恢复 Skill')
                with package.open(entry['path']) as original, path.open('xb') as out:
                    if _hash_stream(original, out) != entry['sha256']:
                        raise ValueError('Skill ZIP 内容在恢复时发生变化')
                path.chmod(0o644 | entry['executable'])
        if target.exists() or target.is_symlink():
            raise FileExistsError('恢复目标已存在，未覆盖：' + str(target))
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return result
