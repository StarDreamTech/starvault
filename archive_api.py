"""Read-only, bounded access to a Chat History Vault archive for local clients."""
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat


AUXILIARY = {'images', 'attachments', 'records', 'skills'}
FORMATS = {'markdown': None, 'dialogue': 'dialogue.jsonl',
           'manifest': 'manifest.json', 'operations': 'operations.jsonl'}


def _root(root):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Archive root is not a directory')
    return root


def _parts(relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
            or '\x00' in relative or relative.startswith('/')):
        raise ValueError('Unsafe archive path: ' + repr(relative))
    parts = relative.split('/')
    if any(part in ('', '.', '..') or part.endswith((' ', '.')) or
           PureWindowsPath(part).is_reserved() or
           PureWindowsPath(part.split('.')[0].rstrip(' ')).is_reserved() for part in parts):
        raise ValueError('Unsafe archive path: ' + repr(relative))
    return PurePosixPath(relative).parts


def component(value):
    parts = _parts(value)
    if len(parts) != 1:
        raise ValueError('Expected one archive path component')
    return value


def safe_path(root, relative):
    """Never follow archive links, including Windows directory junctions."""
    path = root
    for part in _parts(relative):
        path /= part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Archive links are not readable: ' + relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Archive path escaped its root')
    return path


def _open(root, relative):
    path = safe_path(root, relative)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Archive content is not a regular file: ' + relative)
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError('Archive content is not a regular file: ' + relative)
    return os.fdopen(descriptor, 'r', encoding='utf-8', newline='')


def _json(root, relative):
    with _open(root, relative) as stream:
        return json.load(stream)


def _page(limit, offset, maximum):
    if (type(limit) is not int or not 1 <= limit <= maximum or
            type(offset) is not int or offset < 0):
        raise ValueError('limit must be 1–' + str(maximum) + ' and offset must be a nonnegative integer')


def _row(branch, sid, value, tool=''):
    component(branch); component(sid)
    if branch in AUXILIARY or branch.startswith('.') or not isinstance(value, dict):
        raise ValueError('Invalid conversation entry')
    if value.get('id', sid) != sid:
        raise ValueError('Conversation ID differs from its index key')
    relative = value.get('file')
    parts = _parts(relative)
    if len(parts) < 2 or parts[0] != 'sessions' or not parts[-1].endswith('.md'):
        raise ValueError('Conversation file must be a Markdown file under sessions/')
    expected = f'records/{branch}/{sid}/manifest.json'
    if value.get('local_record', expected) != expected:
        raise ValueError('Conversation record reference does not match its branch and ID')
    for key in ('title', 'tool', 'origin_kind', 'project', 'updated_at'):
        if key in value and not isinstance(value[key], str):
            raise ValueError('Invalid conversation field: ' + key)
    if value.get('archived') is not None and type(value['archived']) is not bool:
        raise ValueError('Invalid conversation archived flag')
    if not isinstance(value.get('assets', []), list):
        raise ValueError('Invalid conversation image list')
    origin = value.get('origin_kind') or value.get('tool') or tool
    device = branch.removesuffix('-' + origin) if origin else branch
    return {'branch': branch, 'session_id': sid, 'title': value.get('title', sid),
            'tool': origin, 'project': value.get('project', ''), 'device': device,
            'updated_at': value.get('updated_at', ''), 'archived': value.get('archived'),
            'messages': value.get('messages', 0), 'images': len(value.get('assets', [])),
            'attachments': value.get('attachments', 0), '_file': branch + '/' + relative}


def _index(root, branch):
    component(branch)
    if branch in AUXILIARY or branch.startswith('.'):
        raise ValueError('Not a conversation branch')
    value = _json(root, branch + '/index.json')
    if not isinstance(value, dict) or not isinstance(value.get('sessions'), dict):
        raise ValueError('Conversation index has no sessions object')
    if 'tool' in value and not isinstance(value['tool'], str):
        raise ValueError('Invalid index tool')
    return value


def _catalog(root):
    rows, warnings = [], []
    for entry in sorted(root.iterdir()):
        branch = entry.name
        if branch in AUXILIARY or branch.startswith('.') or not entry.is_dir():
            continue
        if not (entry / 'index.json').exists():
            continue
        try:
            index = _index(root, branch)
        except (OSError, ValueError) as error:
            warnings.append(branch + ': ' + str(error))
            continue
        for sid, value in index['sessions'].items():
            try:
                row = _row(branch, sid, value, index.get('tool', ''))
                safe_path(root, row['_file'])
                rows.append(row)
            except (OSError, ValueError, TypeError) as error:
                warnings.append(branch + '/' + str(sid) + ': ' + str(error))
    return rows, warnings


def _find(root, branch, session_id):
    component(session_id)
    index = _index(root, branch)
    if session_id not in index['sessions']:
        raise ValueError('Conversation not found in branch: ' + branch + '/' + session_id)
    return _row(branch, session_id, index['sessions'][session_id], index.get('tool', ''))


def _time(row):
    try:
        return datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0


def _snippet_and_match(root, relative, missing):
    """Scan in chunks so a long conversation does not need to fit in memory."""
    tail, snippet = '', ''
    overlap = max((len(term) for term in missing), default=1)
    with _open(root, relative) as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return not missing, snippet
            text = tail + chunk
            folded = text.casefold()
            positions = [folded.find(term) for term in missing if term in folded]
            if positions and (not snippet or missing):
                start = max(0, min(positions) - 60)
                snippet = text[start:start + 240]
            elif not snippet:
                snippet = text[:240]
            missing = {term for term in missing if term not in folded}
            if not missing:
                return True, snippet
            tail = text[-overlap:]


def status(root):
    root = _root(root)
    rows, warnings = _catalog(root)
    counts = Counter((row['branch'], row['tool'], row['device']) for row in rows)
    skills = 0
    try:
        value = _json(root, 'skills/index.json')
        if not isinstance(value, dict) or not isinstance(value.get('items'), list):
            raise ValueError('Skill index has no items list')
        skills = len(value['items'])
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        warnings.append('skills: ' + str(error))
    return {'archive': str(root), 'conversations': len(rows), 'skills': skills,
            'branches': [{'branch': branch, 'tool': tool, 'device': device, 'count': count}
                         for (branch, tool, device), count in sorted(counts.items())],
            'warnings': warnings}


def search(root, query='', tool='', project='', device='', archived=None, limit=20, offset=0):
    _page(limit, offset, 100)
    if not all(isinstance(value, str) for value in (query, tool, project, device)):
        raise ValueError('Search text and filters must be strings')
    if archived is not None and type(archived) is not bool:
        raise ValueError('archived must be a boolean or null')
    root = _root(root)
    rows, warnings = _catalog(root)
    results = []
    terms = set(query.casefold().split())
    for row in sorted(rows, key=lambda r: (_time(r), r['branch'], r['session_id']), reverse=True):
        if (tool and row['tool'].casefold() != tool.casefold() or
                project and project.casefold() not in row['project'].casefold() or
                device and row['device'].casefold() != device.casefold() or
                archived is not None and row['archived'] is not archived):
            continue
        metadata = '\n'.join(row[k] for k in ('title', 'project', 'device', 'tool')).casefold()
        missing = {term for term in terms if term not in metadata}
        try:
            matched, snippet = _snippet_and_match(root, row['_file'], missing)
        except (OSError, ValueError) as error:
            warnings.append(row['branch'] + '/' + row['session_id'] + ': ' + str(error))
            matched, snippet = not missing, ''
        if matched:
            results.append({**{k: v for k, v in row.items() if not k.startswith('_')}, 'snippet': snippet})
    end = offset + limit
    return {'items': results[offset:end], 'total': len(results), 'offset': offset,
            'next_offset': end if end < len(results) else None, 'warnings': warnings}


def read_conversation(root, branch, session_id, format='markdown', offset=0, limit=12000):
    _page(limit, offset, 24000)
    if not isinstance(format, str) or format not in FORMATS:
        raise ValueError('Unknown conversation format')
    root = _root(root)
    row = _find(root, branch, session_id)
    relative = row['_file'] if format == 'markdown' else f'records/{branch}/{session_id}/{FORMATS[format]}'
    total, pieces = 0, []
    with _open(root, relative) as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            start, end = max(0, offset - total), min(len(chunk), offset + limit - total)
            if start < end:
                pieces.append(chunk[start:end])
            total += len(chunk)
    return {'branch': branch, 'session_id': session_id, 'format': format,
            'text': ''.join(pieces), 'offset': offset, 'total_chars': total,
            'next_offset': offset + limit if offset + limit < total else None}


def list_attachments(root, branch, session_id, limit=50, offset=0):
    _page(limit, offset, 100)
    root = _root(root)
    _find(root, branch, session_id)
    relative = f'records/{branch}/{session_id}/manifest.json'
    manifest = _json(root, relative)
    if (not isinstance(manifest, dict) or manifest.get('branch') != branch or
            manifest.get('session_id') != session_id or not isinstance(manifest.get('files'), list)):
        raise ValueError('Attachment manifest does not match the conversation')
    fields = ('id', 'name', 'kind', 'source', 'size', 'exists', 'copy', 'sha256', 'records', 'versions')
    items, warnings = [], []
    for ref in manifest['files']:
        if not isinstance(ref, dict):
            warnings.append('Invalid attachment entry')
            continue
        item = {key: ref[key] for key in fields if key in ref}
        item['backed_up'] = False
        if ref.get('copy'):
            try:
                parts = _parts(ref['copy'])
                if len(parts) != 2 or parts[0] not in ('images', 'attachments'):
                    raise ValueError('Attachment copy must be in images/ or attachments/')
                path = safe_path(root, ref['copy'])
                item['backed_up'] = path.is_file()
            except (OSError, ValueError) as error:
                warnings.append(str(ref.get('id', 'attachment')) + ': ' + str(error))
        items.append(item)
    end = offset + limit
    return {'branch': branch, 'session_id': session_id, 'items': items[offset:end],
            'total': len(items), 'offset': offset, 'next_offset': end if end < len(items) else None,
            'warnings': warnings}
