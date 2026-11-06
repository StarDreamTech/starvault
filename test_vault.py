"""Run with: python3 -m unittest -v"""
import base64
import contextlib
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

import vault
import resources
import chatgpt_import


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ZkAAAAASUVORK5CYII=')
SID = '12345678-1234-1234-1234-123456789abc'


class ArchiveTest(unittest.TestCase):
    def test_skill_cli_remembers_sources_and_sync_retains_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); archive = root / 'backup'; source = root / '自定义 Skills' / 'demo'
            source.mkdir(parents=True)
            (source / 'SKILL.md').write_text('first version', encoding='utf-8')

            def command(*arguments):
                output = io.StringIO()
                with patch('sys.argv', ['vault.py', 'skills', '--archive', str(archive), *arguments]), contextlib.redirect_stdout(output):
                    vault.main()
                return json.loads(output.getvalue())

            with patch('vault.LOCAL', root / 'local'):
                plan = command('--no-defaults', '--source', str(source.parent), '--plan')
                self.assertEqual(plan['skills'], 1)
                self.assertFalse((archive / 'skills').exists())
                self.assertFalse(plan['remembered'])
                copied = command('--no-defaults', '--source', str(source.parent), '--remember')
                self.assertEqual(copied['copied'], 1)
                self.assertTrue(copied['remembered'])
                saved = command('--list')
                self.assertFalse(saved['settings']['defaults'])
                self.assertEqual(saved['settings']['sources'], [str(source.parent.resolve())])
                self.assertEqual(command('--plan')['skills'], 1)
                (source / 'SKILL.md').write_text('second version', encoding='utf-8')
                vault.sync({'archive': str(archive)}, [], False)
                item = command('--list')['items'][0]
                self.assertEqual(len(item['versions']), 2)
                restored = command('--restore', item['id'], '--destination', str(root / 'restored'),
                                   '--version', copied['items'][0]['version'])
                self.assertEqual((Path(restored['restored']) / 'SKILL.md').read_text(), 'first version')
                (source / 'SKILL.md').write_text('offline update', encoding='utf-8')
                with patch('vault.private_repo'), patch('vault.checkout', side_effect=RuntimeError('Git unavailable')):
                    with self.assertRaisesRegex(RuntimeError, 'Git unavailable'):
                        vault.sync({'archive': str(archive), 'repo': 'unused'}, ['codex'], True)
                self.assertEqual(len(command('--list')['items'][0]['versions']), 3)
                self.assertFalse(command('--forget')['remembered'])
                vault.sync({'archive': str(archive)}, [], False)
                self.assertEqual(vault.read_json(root / 'local/last-run.json')['results'], [])

    def test_skill_source_settings_are_machine_local(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); archive = root / 'backup'
            settings = {'defaults': False, 'sources': ['/another/computer'], 'project': '/another/project'}
            with patch('vault.LOCAL', root / 'local'), patch('vault.skill_policy_key', return_value='source-computer'):
                vault.save_skill_policy(archive, settings)
                self.assertEqual(vault.skill_policy(archive), settings)
            with patch('vault.LOCAL', root / 'local'), patch('vault.skill_policy_key', return_value='destination-computer'):
                self.assertIsNone(vault.skill_policy(archive))
                with patch('vault.collect_skills') as collect:
                    vault.sync({'archive': str(archive)}, [], False)
                collect.assert_not_called()
                vault.save_skill_policy(archive, {'defaults': True, 'sources': []})
            with patch('vault.skill_policy_key', return_value='source-computer'):
                self.assertEqual(vault.skill_policy(archive), settings)

    def test_long_non_path_output_does_not_generate_attachments(self):
        refs = {}
        resources.collect('A' * 300000 + ' /tmp/example.glb', '/tmp', 1, refs)
        self.assertEqual(len(refs), 1)
        self.assertEqual(next(iter(refs.values()))['name'], 'example.glb')

    def test_optional_attachment_and_command_migration_is_incremental_and_local(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'codex'; dest = root / 'backup/mac-codex'
            path = home / 'sessions' / ('rollout-' + SID + '.jsonl'); path.parent.mkdir(parents=True)
            model = root / '场景 final.blend'; model.write_bytes(b'BLENDER-original'); model = model.resolve()
            rows = [
                {'type': 'session_meta', 'payload': {'cwd': str(root), 'custom_source_label': 'retained'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '请修改 [模型](<' + str(model) + '>)'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'call-1', 'name': 'exec', 'arguments': 'DO_NOT_EXECUTE_TEST_COMMAND'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'PRIVATE_LOG_TEXT'}},
            ]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            vault.export('codex', home, dest)
            archive = dest.parent; manifest_path = archive / 'records/mac-codex' / SID / 'manifest.json'
            manifest = resources.read(manifest_path)
            original_ref = next(r for r in manifest['files'] if r['source'] == str(model))
            self.assertNotIn('copy', original_ref)
            text = next(dest.glob('sessions/**/*.md')).read_text()
            self.assertNotIn('PRIVATE_LOG_TEXT', text)
            self.assertNotIn('DO_NOT_EXECUTE_TEST_COMMAND', text)
            self.assertIn('请修改', manifest_path.with_name('dialogue.jsonl').read_text())
            plan = resources.migrate(archive, ['models', 'logs'], plan=True)
            self.assertEqual(plan['operations'], 2)
            self.assertGreaterEqual(plan['estimated_bytes'], model.stat().st_size)
            resources.migrate(archive, ['models', 'logs'])
            manifest = resources.read(manifest_path)
            ref = next(r for r in manifest['files'] if r['source'] == str(model))
            self.assertEqual((archive / ref['copy']).read_bytes(), model.read_bytes())
            self.assertIn('PRIVATE_LOG_TEXT', manifest_path.with_name('operations.jsonl').read_text())
            self.assertEqual(resources.migrate(archive, ['models', 'logs'])['copied'], 0)
            old_copy = ref['copy']; model.write_bytes(b'BLENDER-updated-new-content')
            self.assertEqual(resources.migrate(archive, ['models'])['copied'], 1)
            self.assertEqual((archive / old_copy).read_bytes(), b'BLENDER-original')
            self.assertTrue(model.exists())

    def test_chatgpt_origin_labels_and_alternative_messages_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'imports'; dest = root / 'backup/mac-chatgpt'
            def node(identifier, text, parent=None):
                return {'parent': parent, 'message': {'id': identifier, 'author': {'role': 'assistant'}, 'create_time': 100,
                    'content': {'parts': [text]}, 'metadata': {'model_slug': 'claude-model-name', 'original_tag': 'keep-this'}}}
            data = [{'id': SID, 'title': '原始 ChatGPT', 'is_archived': True, 'tags': ['原标签'], 'current_node': 'b',
                     'mapping': {'a': node('a', '第一条'), 'b': node('b', '当前版本', 'a'), 'c': node('c', '替代版本', 'a')}}]
            source = root / 'conversations.json'; source.write_text(json.dumps(data))
            chatgpt_import.import_file(source, home)
            vault.export('chatgpt', home, dest)
            row = resources.read(dest / 'index.json')['sessions'][SID]
            self.assertEqual(row['origin_kind'], 'chatgpt')
            self.assertIn('claude-model-name', row['models'])
            self.assertIn('tags:原标签', row['labels'])
            text = (dest / row['file']).read_text()
            self.assertIn('当前版本', text); self.assertNotIn('替代版本', text)
            manifest = resources.read(dest.parent / row['local_record'])
            self.assertEqual(manifest['metadata']['origin_metadata']['mapping']['c']['message']['metadata']['original_tag'], 'keep-this')
            raw = (dest.parent / row['local_record']).with_name('dialogue.jsonl').read_text()
            self.assertIn('替代版本', raw)
            self.assertTrue(row['archived'])

    def test_codex_project_and_archive_metadata_survives_source_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'codex'; home.mkdir()
            source = home / 'sessions' / ('rollout-' + SID + '.jsonl'); source.parent.mkdir()
            source.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': '保留归档'}]}}) + '\n')
            state = {'local-projects': {'project-1': {'name': '我的项目', 'rootPaths': ['/code/game']}},
                     'thread-project-assignments': {SID: {'projectId': 'project-1'}},
                     'pinned-project-ids': ['project-1'], 'project-order': ['project-1']}
            (home / '.codex-global-state.json').write_text(json.dumps(state))
            with closing(sqlite3.connect(home / 'state_1.sqlite')) as db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, archived INTEGER)')
                db.execute('INSERT INTO threads VALUES (?, ?, ?, ?)', (SID, '会话', '/code/game/worktree', 1))
                db.commit()
            dest = root / 'backup/mac-codex'
            vault.export('codex', home, dest)
            row = json.loads((dest / 'index.json').read_text())['sessions'][SID]
            self.assertEqual(row['project'], '我的项目')
            self.assertEqual(row['project_id'], 'project-1')
            self.assertTrue(row['project_pinned'])
            self.assertTrue(row['archived'])  # archived in DB, rollout not moved yet
            source.unlink()
            vault.export('codex', home, dest)
            self.assertEqual(json.loads((dest / 'index.json').read_text())['sessions'][SID], row)
            self.assertIn('已归档', (dest / row['file']).read_text())

    def test_git_branches_upload_text_only_and_retry_without_duplicate_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); remote = root / 'remote.git'
            vault.run(['git', 'init', '--bare', str(remote)])
            home = root / 'codex'; path = home / 'sessions' / ('rollout-' + SID + '.jsonl')
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': '备份'}, {'type': 'input_image',
                'image_url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}]}}) + '\n')
            cfg = {'repo': str(remote), 'device': 'test-machine', 'archive': str(root / 'archive'),
                   'codex_home': str(home), 'claude_home': str(root / 'claude')}
            identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@localhost',
                        'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@localhost'}
            with patch('vault.LOCAL', root / 'local'), patch('vault.private_repo'), patch.dict(os.environ, identity):
                vault.sync(cfg, ['codex', 'claude'], True)
                before = vault.git(remote, 'rev-parse', 'refs/heads/test-machine-codex').stdout
                vault.sync(cfg, ['codex', 'claude'], True)
                self.assertEqual(before, vault.git(remote, 'rev-parse', 'refs/heads/test-machine-codex').stdout)
            tree = vault.git(remote, 'ls-tree', '-r', '--name-only', 'test-machine-codex').stdout
            self.assertIn('.md', tree); self.assertNotIn('.png', tree); self.assertNotIn('.export-cache', tree)
            self.assertEqual(len(list((root / 'archive/images').glob('*.png'))), 1)
            with path.open('a') as f:
                f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': '离线新增消息'}]}}) + '\n')
            with patch('vault.LOCAL', root / 'local'), patch('vault.private_repo', side_effect=OSError('offline')):
                with self.assertRaisesRegex(RuntimeError, 'Local backup completed'):
                    vault.sync(cfg, ['codex', 'claude'], True)
            self.assertIn('离线新增消息', next((root / 'archive/test-machine-codex').glob('sessions/**/*.md')).read_text())
            self.assertEqual(before, vault.git(remote, 'rev-parse', 'refs/heads/test-machine-codex').stdout)

    def test_export_images_incremental_archive_and_partial_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'codex'; dest = root / 'backup/computer-codex'
            path = home / 'sessions/2026/10/08' / ('rollout-2026-10-08-' + SID + '.jsonl')
            path.parent.mkdir(parents=True)
            image = base64.b64encode(PNG).decode()
            records = [
                {'type': 'session_meta', 'payload': {'cwd': '/project/game'}},
                {'type': 'response_item', 'timestamp': '2026-10-08', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': '你好'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,' + image}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '世界'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '你好'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'image-only-original-id',
                 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,' + image}]}},
                {'type': 'compacted', 'payload': {'repeated_image': image}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'PRIVATE TOOL LOG'}},
            ]
            path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
            before = path.read_bytes()
            result = vault.export('codex', home, dest)
            self.assertEqual((result['sessions'], result['images']), (1, 1))
            exported = next(dest.glob('sessions/**/*.md'))
            text = exported.read_text()
            self.assertEqual(text.count('你好'), 3)  # title + two distinct user turns
            self.assertNotIn('PRIVATE TOOL LOG', text)
            self.assertNotIn(image, text)
            name = hashlib.sha256(PNG).hexdigest() + '.png'
            self.assertEqual((root / 'backup/images' / name).read_bytes(), PNG)
            self.assertTrue((exported.parent / '../../../images' / name).resolve().exists())
            self.assertEqual(path.read_bytes(), before)
            original = (root / 'backup/records/computer-codex' / SID / 'dialogue.jsonl').read_text()
            self.assertIn('image-only-original-id', original)
            self.assertEqual(vault.export('codex', home, dest)['changed_sessions'], 0)
            # A writer can be interrupted mid-record: retry it on the next run.
            with path.open('ab') as f: f.write(b'{"type":')
            vault.export('codex', home, dest)
            index = json.loads((dest / 'index.json').read_text())
            self.assertTrue(index['sessions'][SID]['partial_tail'])
            path.write_bytes(before)
            archived = home / 'archived_sessions' / path.name
            archived.parent.mkdir(); path.rename(archived)
            vault.export('codex', home, dest)
            self.assertTrue(json.loads((dest / 'index.json').read_text())['sessions'][SID]['archived'])
            archived.unlink()
            self.assertEqual(vault.export('codex', home, dest)['sessions'], 1)

    def test_claude_nested_image_tool_logs_and_local_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'claude'; dest = root / 'backup/computer-claude'
            path = home / 'projects/project' / (SID + '.jsonl'); path.parent.mkdir(parents=True)
            local = root / 'reference.png'; local.write_bytes(PNG)
            records = [
                {'type': 'user', 'uuid': 'u1', 'cwd': '/game', 'message': {'role': 'user', 'content': '看图 ![图](' + str(local) + ')'}},
                {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': [
                    {'type': 'text', 'text': 'HIDDEN LOG'}, {'type': 'image', 'source': {'type': 'base64', 'data': base64.b64encode(PNG).decode()}}]}]}},
                {'type': 'assistant', 'uuid': 'a1', 'message': {'id': 'api1', 'role': 'assistant', 'content': [{'type': 'text', 'text': '第一段'}]}},
                {'type': 'assistant', 'uuid': 'a2', 'message': {'id': 'api1', 'role': 'assistant', 'content': [{'type': 'text', 'text': '第二段'}]}},
            ]
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            with patch('vault.metadata', return_value={}): result = vault.export('claude', home, dest)
            self.assertEqual(result['images'], 1)
            text = next(dest.glob('sessions/**/*.md')).read_text()
            self.assertIn('第一段', text); self.assertIn('第二段', text)
            self.assertNotIn('HIDDEN LOG', text)
            self.assertNotIn(str(local), text)

    def test_workbuddy_jsonl_transcript_and_sqlite_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'workbuddy'; dest = root / 'backup/computer-workbuddy'
            project = home / 'projects/Users-demo-app'; project.mkdir(parents=True)
            shot = home / 'clipboard-images/shot.png'; shot.parent.mkdir()
            shot.write_bytes(PNG)
            path = project / (SID + '.jsonl')
            path.write_text(''.join(json.dumps(r) + '\n' for r in [
                {'type': 'session-meta', 'sessionId': SID, 'meta': {'cwd': '/Users/demo/app'}},
                {'type': 'message', 'role': 'user', 'cwd': '/Users/demo/app', 'timestamp': 1792230916683,
                 'content': [{'type': 'input_text', 'text': '<system-reminder data-role="user-context">'
                                                              'CONTEXT ONLY</system-reminder>'
                                                              '<user_query>帮我整理笔记</user_query>'
                                                              '<image_local_path>' + str(shot) +
                                                              '</image_local_path>'}]},
                {'type': 'message', 'role': 'user', 'cwd': '/Users/demo/app', 'timestamp': 1792230916700,
                 'content': [{'type': 'input_text', 'text': '<system-reminder data-role="user-context">'
                                                             'PURE WRAPPER</system-reminder>'}]},
                {'type': 'message', 'role': 'assistant', 'timestamp': 1792230922457,
                 'providerData': {'model': 'deepseek-v4.1-flash'},
                 'content': [{'type': 'output_text', 'text': '好的'}]},
                {'type': 'function_call', 'name': 'read', 'timestamp': 1792230923000},
                {'type': 'ai-title', 'aiTitle': '整理笔记', 'cwd': '/Users/demo/app'},
            ]))
            database = sqlite3.connect(home / 'workbuddy.db')
            database.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, '
                             'custom_title TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, '
                             'last_activity_at INTEGER, is_background_automation INTEGER, model TEXT, '
                             'mode TEXT, permission_mode TEXT, transport TEXT, conversation_origin TEXT, '
                             'visibility TEXT, deleted_at TEXT, project_id TEXT, session_settings TEXT)')
            database.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (SID, '/Users/demo/app', '整理笔记', None, 'completed', 1792230916522,
                              1792234846009, 1792234845398, 0, 'primary-model', 'craft', 'fullAccess',
                              'local', None, None, '', None, None))
            database.commit(); database.close()
            result = vault.export('workbuddy', home, dest)
            self.assertEqual(result['sessions'], 1)
            index = json.loads((dest / 'index.json').read_text())['sessions'][SID]
            self.assertEqual(index['title'], '整理笔记')
            self.assertEqual(index['cwd'], '/Users/demo/app')
            self.assertEqual(index['project'], 'app')
            self.assertIsNone(index['archived'])
            self.assertIn('deepseek-v4.1-flash', index['models'])
            text = (dest / index['file']).read_text()
            self.assertIn('帮我整理笔记', text); self.assertIn('好的', text)
            image = hashlib.sha256(PNG).hexdigest() + '.png'
            self.assertEqual(result['images'], 1)
            self.assertEqual(text.count('images/' + image), 1)
            self.assertEqual((root / 'backup/images' / image).read_bytes(), PNG)
            self.assertNotIn('CONTEXT ONLY', text)
            self.assertNotIn('PURE WRAPPER', text)
            dialogue = (dest.parent / 'records' / dest.name / SID / 'dialogue.jsonl').read_text()
            self.assertIn('CONTEXT ONLY', dialogue)
            self.assertIn('PURE WRAPPER', dialogue)
            self.assertEqual(index['operations'], 1)
            self.assertEqual(vault.export('workbuddy', home, dest)['changed_sessions'], 0)

    def test_openclaw_sqlite_transcript_is_materialized_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'openclaw'; dest = root / 'backup/computer-openclaw'
            agent = home / 'agents/main/agent'; agent.mkdir(parents=True)
            database = sqlite3.connect(agent / 'openclaw-agent.sqlite')
            database.execute('CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, '
                             'display_name TEXT, label TEXT, status TEXT, created_at INTEGER, '
                             'updated_at INTEGER, last_activity_at INTEGER, archived_at INTEGER, '
                             'created_via TEXT, pinned_at INTEGER, parent_session_key TEXT, entry_json TEXT)')
            database.execute('CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, '
                             'event_json TEXT, created_at INTEGER, event_zstd BLOB, PRIMARY KEY (session_id, seq))')
            database.execute('INSERT INTO session_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             ('agent:main:main', SID, '方案讨论', None, 'done', 1792203817891,
                              1792203823000, 1792203823000, None, 'operator', None, None,
                              json.dumps({'label': '方案讨论'})))
            events = [
                {'type': 'session', 'id': SID, 'timestamp': '2026-10-17T02:23:42.885Z', 'cwd': '/work/demo'},
                {'type': 'message', 'id': 'm1', 'timestamp': '2026-10-17T02:23:43.000Z',
                 'message': {'role': 'user', 'content': '看看这个问题', 'timestamp': 1792203823000}},
                {'type': 'message', 'id': 'm2', 'timestamp': '2026-10-17T02:23:44.000Z',
                 'message': {'role': 'assistant', 'model': 'gpt-6.1-sol', 'content': [
                     {'type': 'text', 'text': '我看看'}, {'type': 'toolCall', 'name': 'bash'}]}},
                {'type': 'message', 'id': 'm3', 'timestamp': '2026-10-17T02:23:45.000Z',
                 'message': {'role': 'toolResult', 'content': [{'type': 'text', 'text': 'TOOL OUTPUT'}]}},
            ]
            for seq, event in enumerate(events):
                database.execute('INSERT INTO transcript_events VALUES (?,?,?,?,?)',
                                 (SID, seq, json.dumps(event), 1792203823000, None))
            database.commit(); database.close()
            self.assertEqual(vault.export('openclaw', home, dest)['sessions'], 1)
            index = json.loads((dest / 'index.json').read_text())['sessions'][SID]
            self.assertEqual(index['title'], '方案讨论')
            self.assertEqual(index['project'], 'demo')
            self.assertIs(index['archived'], False)
            text = (dest / index['file']).read_text()
            self.assertIn('看看这个问题', text); self.assertIn('我看看', text)
            self.assertNotIn('TOOL OUTPUT', text)
            self.assertEqual(index['operations'], 2)
            record = dest.parent / 'records' / dest.name / SID / 'source.jsonl'
            self.assertTrue(record.is_file())
            self.assertEqual(index['source_file'], str(record))
            self.assertIn('TOOL OUTPUT', record.read_text())
            self.assertEqual(vault.export('openclaw', home, dest)['changed_sessions'], 0)

    def test_dsh_events_keep_internal_context_out_of_markdown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / 'dsh'; dest = root / 'backup/computer-dsh'
            path = home / 'sessions/--work-demo--' / ('session-' + SID) / 'session.v4.jsonl'
            path.parent.mkdir(parents=True)
            events = [
                {'type': 'session', 'version': 4, 'id': 'session-' + SID,
                 'createdAt': 1793101698063, 'cwd': '/work/demo'},
                {'type': 'user/message', 'seq': 1, 'time': 1793101700000, 'data': {
                    'content': [{'type': 'text', 'text': '真正的提问'}], 'source': {'kind': 'user'},
                    'role': 'user', 'id': 'u1'}},
                {'type': 'user/message', 'seq': 2, 'time': 1793101700001, 'data': {
                    'content': [{'type': 'text', 'text': 'Current runtime context..'}],
                    'source': {'kind': 'runtime-context'}, 'role': 'user', 'id': 'u2'}},
                {'type': 'user/message', 'seq': 2, 'time': 1793101700002, 'data': {
                    'content': [{'type': 'text', 'text': 'Approval policy changed'}],
                    'source': {'kind': 'user-approval'}, 'role': 'user', 'id': 'u3'}},
                {'type': 'assistant/message', 'seq': 3, 'time': 1793101701000, 'data': {'message': {
                    'role': 'assistant', 'source': {'model': 'deepseek-flash'},
                    'content': [{'type': 'reasoning', 'text': 'HIDDEN REASONING'},
                                {'type': 'text', 'text': '回答'}]}}},
                {'type': 'session/title', 'seq': 4, 'time': 1793101702000,
                 'data': {'title': '演示会话'}},
                {'type': 'tool/call', 'seq': 5, 'time': 1793101703000,
                 'data': {'callId': 'c1', 'name': 'bash'}},
            ]
            path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in events))
            self.assertEqual(vault.export('dsh', home, dest)['sessions'], 1)
            index = json.loads((dest / 'index.json').read_text())['sessions'][SID]
            self.assertEqual(index['title'], '演示会话')
            self.assertEqual(index['cwd'], '/work/demo')
            self.assertEqual(index['project'], 'demo')
            self.assertIsNone(index['archived'])
            self.assertEqual(index['messages'], 2)
            self.assertEqual(index['operations'], 1)
            self.assertIn('deepseek-flash', index['models'])
            text = (dest / index['file']).read_text()
            self.assertIn('真正的提问', text); self.assertIn('回答', text)
            self.assertNotIn('Current runtime context', text)
            self.assertNotIn('Approval policy changed', text)
            self.assertNotIn('HIDDEN REASONING', text)
            record = dest.parent / 'records' / dest.name / SID
            self.assertTrue((record / 'source.jsonl').is_file())
            dialogue = (record / 'dialogue.jsonl').read_text()
            self.assertIn('Current runtime context', dialogue)
            self.assertIn('Approval policy changed', dialogue)
            self.assertEqual(vault.export('dsh', home, dest)['changed_sessions'], 0)

    def test_readonly_wal_database_uses_a_temporary_copy_when_needed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state_1.sqlite'
            database = sqlite3.connect(path)
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)')
            database.execute("INSERT INTO threads VALUES ('session-1', 0)")
            database.commit(); database.close()
            before = path.read_bytes()
            with vault.readonly_database(path) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM threads').fetchone()[0], 1)
            self.assertEqual(path.read_bytes(), before)  # source untouched

    def test_completed_corruption_fails_without_replacing_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); dest = root / 'backup/device'; dest.mkdir(parents=True)
            path = root / ('rollout-' + SID + '.jsonl'); path.write_bytes(b'{bad json}\n')
            with self.assertRaises(ValueError): vault.export_session(path, 'codex', dest, {})
            self.assertFalse((dest / 'sessions').exists())


if __name__ == '__main__': unittest.main()
