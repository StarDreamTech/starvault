"""Import official ChatGPT exports or paginated desktop read_thread snapshots."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import zipfile


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
    return value or ''


def texts(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return '\n\n'.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text')
    if isinstance(content, dict):
        return '\n\n'.join(p if isinstance(p, str) else p.get('text', '') for p in content.get('parts', []) if isinstance(p, (str, dict)))
    return ''


def import_file(path, home):
    import vault
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'conversations.json'), None)
            if not name: raise ValueError('ZIP 中没有 conversations.json')
            with z.open(name) as stream: data = json.load(stream)
    else: data = json.loads(path.read_text(encoding='utf-8'))
    snapshot = isinstance(data, dict) and data.get('format') == 'vault-chatgpt-snapshot'
    rows = data.get('conversations', []) if isinstance(data, dict) else data
    if not isinstance(rows, list): raise ValueError('需要 ChatGPT conversations.json 或桌面会话快照')
    meta = vault.read_json(home / 'metadata.json', {})
    imported = 0
    for conversation in rows:
        original_id = str(conversation.get('id') or conversation.get('conversation_id') or conversation.get('listing', {}).get('id') or '')
        if not original_id: continue
        sid = original_id if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', original_id) else hashlib.sha256(original_id.encode()).hexdigest()
        messages = []; models = set(); raw_meta = dict(conversation)
        if snapshot:
            listing = conversation['listing']; pages = conversation.get('pages', [])
            title = listing.get('title', sid); archived = listing.get('archived', False)
            project_id = listing.get('projectId') or ''
            for page in reversed(pages):
                for turn in reversed(page.get('turns', [])):
                    for item in turn.get('items', []):
                        role = {'userMessage': 'user', 'agentMessage': 'assistant'}.get(item.get('type'))
                        if not role: continue
                        text = item.get('text') or texts(item.get('content', []))
                        messages.append({'type': role, 'uuid': item.get('id'), 'timestamp': timestamp(turn.get('startedAt')),
                                         'message': {'role': role, 'content': text}, 'original': item,
                                         'turn_metadata': {k: v for k, v in turn.items() if k != 'items'}})
            coverage = 'app_snapshot'
            if any(page.get('page', {}).get('hasMore') for page in pages[-1:]): coverage = 'partial_snapshot'
            if re.search(r'"\w*[Tt]runcated"\s*:\s*true', json.dumps(pages)): coverage = 'partial_snapshot'
        else:
            title = conversation.get('title') or sid; archived = conversation.get('is_archived')
            project_id = conversation.get('project_id') or conversation.get('gizmo_id') or ''
            mapping = conversation.get('mapping', {})
            if not isinstance(mapping, dict): raise ValueError('ChatGPT mapping 格式无效')
            active = set(); node_id = conversation.get('current_node')
            while node_id in mapping and node_id not in active:
                active.add(node_id); node_id = mapping[node_id].get('parent')
            for node_id, node in mapping.items():
                msg = node.get('message') or {}; role = msg.get('author', {}).get('role')
                if role not in ('user', 'assistant'): continue
                model = msg.get('metadata', {}).get('model_slug')
                if model: models.add(model)
                messages.append({'type': role, 'uuid': msg.get('id') or node_id, 'timestamp': timestamp(msg.get('create_time')),
                                 'archive_visible': not active or node_id in active,
                                 'message': {'role': role, 'content': texts(msg.get('content', {})), 'model': model},
                                 'original': node})
            messages.sort(key=lambda m: m['timestamp'])
            # Preserve every original node/metadata field locally, including alternatives.
            raw_meta = conversation
            coverage = 'official_export'
        meta[sid] = {'id': sid, 'title': title, 'archived': archived, 'source': 'user', 'origin_kind': 'chatgpt',
                     'originator': 'ChatGPT', 'project': conversation.get('project_name') or project_id or '未分组',
                     'project_id': project_id, 'models': sorted(models), 'coverage': coverage,
                     'origin_metadata': raw_meta, 'original_id': original_id,
                     'imported_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        vault.write(home / 'sessions' / (sid + '.jsonl'), ''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in messages))
        imported += 1
    vault.write_json(home / 'metadata.json', meta)
    return imported
