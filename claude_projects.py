"""Merge complete Claude Code Projects messages from verified local cache bodies.

This is a partial, append-preserving archive, not an account export. HTTP headers
never enter the snapshot; only message objects and their observed revisions do.
"""
import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.parse

import archive_api
import claude_cache
import claude_desktop


FORMAT = 1
MESSAGE_EVENTS = ('channel_message', 'channel_message_updated')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def role(message):
    if message.get('server_notice'):
        return 'system'
    if message.get('author_account_id'):
        return 'user'
    author = message.get('author')
    if (message.get('author_agent_id') or message.get('author_session_id') or
            (isinstance(author, dict) and author.get('kind') in ('agent', 'helper'))):
        return 'assistant'
    return 'system'


def message_events(body, warnings, name):
    """Parse full SSE events; a malformed completed message rejects this response."""
    text = body.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    chunks = text.split('\n\n')
    if chunks[-1].strip():
        warnings.append(name + ': incomplete trailing SSE event ignored')
    result = []
    for ordinal, chunk in enumerate(chunks[:-1]):
        event = 'message'
        data = []
        for line in chunk.split('\n'):
            key, sep, value = line.partition(':')
            if value.startswith(' '):
                value = value[1:]
            if key == 'event':
                event = value
            elif key == 'data':
                data.append(value)
        if event not in MESSAGE_EVENTS:
            continue
        value = json.loads('\n'.join(data))
        if not isinstance(value, dict):
            raise ValueError('message event is not an object')
        for field in ('id', 'channel_id', 'body', 'created_at', 'seq'):
            if not isinstance(value.get(field), str) or (field != 'body' and not value[field]):
                raise ValueError('invalid message field: ' + field)
        if len(value['seq']) > 64 or not value['seq'].isascii() or not value['seq'].isdigit():
            raise ValueError('invalid message sequence')
        if value.get('thread_root_id') is not None and not isinstance(value['thread_root_id'], str):
            raise ValueError('invalid thread ID')
        encoded(value).encode('utf-8')
        result.append((ordinal, event, value))
    return result


def revision_order(revision):
    message = revision['message']
    timestamp = message.get('edited_at') or message['created_at']
    try:
        parsed = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        instant = parsed.timestamp()
    except (ValueError, AttributeError, OverflowError):
        instant = 0
    return (instant, *revision['observed'], encoded(message))


def merge_revision(messages, revision):
    """Retain every distinct message version, even when an older window reappears."""
    message = revision['message']
    versions = messages.setdefault(message['id'], {})
    digest = hashlib.sha256(encoded(message).encode('utf-8')).hexdigest()
    previous = versions.get(digest)
    if previous is None or revision_order(revision) > revision_order(previous):
        versions[digest] = revision


def previous_messages(path, identity):
    messages = {}
    if not path.exists():
        return messages
    # A bad existing snapshot must never be replaced by a smaller cache window.
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.endswith('\n'):
                raise ValueError('Incomplete Claude Projects archive snapshot')
            row = json.loads(line)
            if (not isinstance(row, dict) or row.get('cache_identity') != identity or
                    not isinstance(row.get('cache_revisions'), list) or not row['cache_revisions']):
                raise ValueError('Invalid Claude Projects archive snapshot')
            for revision in row['cache_revisions']:
                if (not isinstance(revision, dict) or not isinstance(revision.get('message'), dict) or
                        not isinstance(revision.get('observed'), list) or len(revision['observed']) != 2 or
                        not all(isinstance(v, int) for v in revision['observed'])):
                    raise ValueError('Invalid Claude Projects message revision')
                message = revision['message']
                if (not isinstance(message.get('id'), str) or message['id'] != row.get('uuid') or
                        message.get('channel_id') != identity[2] or
                        (message.get('thread_root_id') or '') != identity[3] or
                        not isinstance(message.get('body'), str) or
                        not isinstance(message.get('created_at'), str) or
                        len(str(message.get('seq', ''))) > 64 or
                        not str(message.get('seq', '')).isascii() or
                        not str(message.get('seq', '')).isdigit()):
                    raise ValueError('Invalid Claude Projects archived message')
                merge_revision(messages, revision)
    if not messages:
        raise ValueError('Empty Claude Projects archive snapshot; preserving existing history')
    return messages


def session_items(home=None, previous_root=None, warnings=None):
    warnings = warnings if warnings is not None else []
    existing = {}
    if previous_root is not None:
        destination = Path(previous_root).absolute()
        index = archive_api.safe_path(destination.parent, destination.name + '/index.json')
        if index.exists():
            existing = json.loads(index.read_text(encoding='utf-8'))['sessions']
    groups = {}
    for product_home in claude_desktop.homes(home):
        product = 'Claude-3p' if product_home.name == 'Claude-3p' else 'Claude'
        for response in claude_cache.iter_responses(product_home, warnings=warnings):
            url = urllib.parse.urlsplit(response['url'])
            match = re.fullmatch(r'/v1/code/channels/([^/]+)/messages/stream', url.path)
            if not match:
                # ponytail: only the observed SSE schema is supported; add JSON
                # timeline routes when an actual response establishes their shape.
                continue
            channel = match.group(1)
            try:
                events = message_events(response['body'], warnings, response['path'].name)
                if any(message['channel_id'] != channel for _, _, message in events):
                    raise ValueError('channel does not match cache URL')
            except (ValueError, UnicodeError) as error:
                warnings.append(response['path'].name + ': invalid Projects response (' +
                                type(error).__name__ + ')')
                continue
            for ordinal, event, message in events:
                identity = (product, url.scheme + '://' + url.netloc, channel,
                            message.get('thread_root_id') or '')
                messages = groups.setdefault(identity, {})
                merge_revision(messages, {'event': event, 'message': message,
                                          'observed': [response.get('response_time', response['mtime_ns']), ordinal]})

    for key, fresh in sorted(groups.items()):
        identity = list(key)
        sid = 'claude-projects-' + hashlib.sha256(encoded(identity).encode('utf-8')).hexdigest()[:32]
        messages = {}
        if previous_root is not None:
            destination = Path(previous_root).absolute()
            snapshot = archive_api.safe_path(destination.parent,
                                            f'records/{destination.name}/{sid}/source.jsonl')
            if sid in existing and not snapshot.is_file():
                raise ValueError('Missing Claude Projects archive snapshot; preserving existing history')
            messages = previous_messages(snapshot, identity)
        for versions in fresh.values():
            for revision in versions.values():
                # Seeing the same payload in a later cache window is not an edit.
                digest = hashlib.sha256(encoded(revision['message']).encode('utf-8')).hexdigest()
                if digest in messages.get(revision['message']['id'], {}):
                    continue
                merge_revision(messages, revision)
        rows = []
        for mid, versions in messages.items():
            revisions = sorted(versions.values(), key=revision_order)
            message = revisions[-1]['message']
            kind = role(message)
            rows.append({'type': kind, 'uuid': mid, 'timestamp': message['created_at'],
                         'message': {'role': kind, 'content': message['body']},
                         'cache_identity': identity, 'cache_revisions': revisions})
        rows.sort(key=lambda row: (int(row['cache_revisions'][-1]['message']['seq']), row['uuid']))
        visible = [row for row in rows if row['type'] in ('user', 'assistant') and row['message']['content'].strip()]
        if not visible:
            continue
        # Prefer the user's actual thread root; never synthesize a conversation summary.
        root = next((row for row in visible if row['uuid'] == identity[3] and row['type'] == 'user'), None)
        first_user = next((row for row in visible if row['type'] == 'user'), None)
        title_row = root or first_user
        title = title_row['message']['content'].strip().splitlines()[0][:160] if title_row else 'Claude Code Projects 讨论串'
        meta = {'title': title, 'project': 'Claude 项目 ' + identity[2], 'project_id': identity[2],
                'source': 'code-projects-cache', 'origin_kind': 'claude-desktop',
                'originator': 'Claude Code Projects', 'coverage': 'partial_cache',
                'origin_metadata': {'product': identity[0], 'channel_id': identity[2],
                                    'thread_root_id': identity[3], 'cache_format': FORMAT,
                                    'coverage': 'partial_cache'}}
        data = ''.join(encoded(row) + '\n' for row in rows).encode('utf-8')
        # Cache-file timestamps are provenance, not new chat content. Touching or
        # evicting identical cache entries must not rewrite archived conversations.
        content = [[row['uuid'], row['cache_revisions'][-1]['message'],
                    sorted((revision['message'] for revision in row['cache_revisions']), key=encoded)] for row in rows]
        signature = ['claude-projects', FORMAT, hashlib.sha256(encoded([meta, content]).encode('utf-8')).hexdigest()]

        def prepare(directory, data=data):
            target = Path(directory) / 'source.jsonl'
            target.write_bytes(data)
            return target

        yield {'id': sid, 'meta': meta, 'origin': 'Claude Code Projects local cache',
               'signature': signature, 'prepare': prepare}
