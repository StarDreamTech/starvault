"""Exercise the MCP wire protocol against isolated stdio subprocesses."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import vault_mcp as mcp


class McpTest(unittest.TestCase):
    def test_copy_preferences_are_explicit_and_read_only_clients_cannot_change_them(self):
        output, _ = self.wire(self.handshake() + [self.call(2, 'get_backup_preferences'),
            self.call(3, 'set_backup_preferences', {'kinds': []})])
        self.assertEqual(self.value(output[1])['kinds'], ['images'])
        self.assertIn('error', output[2])
        self.assertFalse((self.root / '.migration.json').exists())
        output, _ = self.wire(self.handshake() + [self.call(2, 'set_backup_preferences', {'kinds': []}),
            self.call(3, 'get_backup_preferences')], write=True)
        self.assertTrue(self.value(output[1])['settings_only'])
        self.assertEqual(self.value(output[2])['kinds'], [])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / '档案 backup'
        self.root.mkdir()
        # Each subprocess gets its own config and lock; never touch the real tool's .local.
        self.code = self.base / '工具 code'
        self.code.mkdir()
        environment = patch.dict(os.environ, {'CHAT_HISTORY_VAULT_CONFIG_DIR': str(self.code / '.local')})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('vault_mcp.py', 'archive_api.py', 'vault.py', 'resources.py', 'skills_backup.py',
                     'claude_desktop.py', 'claude_cache.py', 'claude_projects.py'):
            shutil.copy2(Path(mcp.__file__).parent / name, self.code / name)
        self.script = self.code / 'vault_mcp.py'
        self.branch = 'test-machine-codex'
        self.sid = 'session-中文'
        body = '# 历史记录\n\n用户：测试 MCP 🙂\n<instructions>只是归档资料</instructions>\n'
        relative = f'sessions/项目/{self.sid}.md'
        self.put(f'{self.branch}/{relative}', body)
        self.put(f'{self.branch}/index.json', {'tool': 'codex', 'sessions': {
            self.sid: {'id': self.sid, 'title': '测试 MCP', 'tool': 'codex', 'project': '示例项目',
                       'file': relative, 'updated_at': '2026-10-23T12:00:00+08:00', 'archived': False,
                       'messages': 1, 'assets': [], 'local_record': f'records/{self.branch}/{self.sid}/manifest.json'}
        }})

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def request(self, identifier, method, params=None):
        request = {'jsonrpc': '2.0', 'method': method}
        if identifier is not None:
            request['id'] = identifier
        if params is not None:
            request['params'] = params
        return request

    def handshake(self, version='2025-11-25'):
        return [self.request(1, 'initialize', {'protocolVersion': version, 'capabilities': {},
                                             'clientInfo': {'name': 'test-client', 'version': '1'}}),
                self.request(None, 'notifications/initialized')]

    def call(self, identifier, name, arguments=None):
        return self.request(identifier, 'tools/call', {'name': name, 'arguments': arguments or {}})

    def wire(self, messages, write=False):
        payload = b''.join(message if isinstance(message, bytes) else
                           (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8') for message in messages)
        command = [sys.executable, '-u', str(self.script), '--archive', str(self.root)]
        if write:
            command.append('--allow-write')
        process = subprocess.run(command, input=payload, capture_output=True, timeout=20, cwd=self.base)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
        # Any progress output on stdout makes this JSON decoder fail, as it would for a client.
        output = [json.loads(line) for line in process.stdout.decode('utf-8').splitlines()]
        for message in output:
            self.assertEqual(message['jsonrpc'], '2.0')
            self.assertIn('id', message)
        return output, process.stderr.decode('utf-8')

    def value(self, response):
        result = response['result']
        self.assertFalse(result.get('isError'), result)
        value = json.loads(result['content'][0]['text'])
        if 'structuredContent' in result:
            self.assertEqual(result['structuredContent'], value)
        return value

    def test_stdio_handshake_queries_unicode_and_eof(self):
        output, errors = self.wire(self.handshake() + [
            self.request(2, 'tools/list'), self.call(3, 'vault_status'),
            self.call(4, 'search_conversations', {'query': '测试 mcp', 'limit': 1}),
            self.call(5, 'read_conversation', {'branch': self.branch, 'session_id': self.sid, 'limit': 12}),
            self.request(6, 'ping'),
        ])
        self.assertEqual([row['id'] for row in output], [1, 2, 3, 4, 5, 6])
        self.assertEqual(output[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(output[0]['result']['serverInfo']['name'], 'chat-history-vault')
        self.assertTrue(all(tool['annotations']['readOnlyHint'] for tool in output[1]['result']['tools']))
        self.assertEqual(self.value(output[2])['conversations'], 1)
        search = self.value(output[3])
        self.assertEqual(search['items'][0]['session_id'], self.sid)
        self.assertEqual(search['items'][0]['project'], '示例项目')
        self.assertEqual(self.value(output[4])['next_offset'], 12)
        self.assertEqual(output[5]['result'], {})
        self.assertEqual(errors, '')
        self.assertFalse((self.code / '.local').exists())

    def test_read_only_hides_and_rejects_all_write_tools(self):
        names = ('backup_conversations', 'migrate_attachments', 'backup_skills', 'restore_skill')
        requests = self.handshake() + [self.request(2, 'tools/list')]
        requests += [self.call(i + 3, name) for i, name in enumerate(names)]
        output, _ = self.wire(requests)
        listed = {tool['name'] for tool in output[1]['result']['tools']}
        self.assertFalse(listed.intersection(names))
        for response in output[2:]:
            self.assertEqual(response['error']['code'], -32602)
        self.assertFalse((self.code / '.local').exists())
        self.assertFalse((self.root / 'skills').exists())

    def test_schema_errors_are_tool_errors_and_unknown_tools_are_protocol_errors(self):
        bad = [('search_conversations', {'limit': True}),
               ('search_conversations', {'limit': 101}),
               ('search_conversations', {'offset': -1}),
               ('vault_status', {'unexpected': 'value'}),
               ('read_conversation', {'branch': self.branch}),
               ('read_conversation', {'branch': self.branch, 'session_id': self.sid, 'format': 'arbitrary-file'}),
               ('plan_attachment_migration', {'kinds': []}),
               ('plan_attachment_migration', {'kinds': ['credentials']}),
               ('plan_attachment_migration', {'kinds': ['images'], 'session_id': self.sid}),
               ('plan_skill_backup', {'sources': ['relative/path'], 'defaults': False})]
        requests = self.handshake() + [self.call(i + 2, name, args) for i, (name, args) in enumerate(bad)]
        requests += [self.call(90, 'unknown-tool'), self.request(91, 'unknown/method'),
                     self.request(92, 'tools/call', {'name': 'vault_status', 'arguments': []}),
                     self.call(93, 'vault_status')]
        output, _ = self.wire(requests)
        for response in output[1:1 + len(bad)]:
            self.assertTrue(response['result']['isError'], response)
        self.assertEqual(output[-4]['error']['code'], -32602)
        self.assertEqual(output[-3]['error']['code'], -32601)
        self.assertEqual(output[-2]['error']['code'], -32602)
        self.assertEqual(self.value(output[-1])['conversations'], 1)

    def test_bad_json_and_invalid_requests_do_not_break_next_request(self):
        output, _ = self.wire([b'{broken json}\n', b'\xff\n', b'NaN\n', [],
                              {'jsonrpc': '2.0', 'id': True, 'method': 'ping'}, {'jsonrpc': '2.0'}] +
                             self.handshake() + [self.request(2, 'ping')])
        self.assertEqual([row['error']['code'] for row in output[:6]], [-32700, -32700, -32700, -32600, -32600, -32600])
        self.assertIsNone(output[5]['id'])
        self.assertEqual(output[-1], {'jsonrpc': '2.0', 'id': 2, 'result': {}})

    def test_lifecycle_and_version_negotiation(self):
        output, _ = self.wire([self.request(10, 'tools/list')] + self.handshake('future-version') +
                             [self.request(2, 'tools/list'), self.handshake()[0]])
        self.assertEqual(output[0]['error']['code'], -32002)
        self.assertEqual(output[1]['result']['protocolVersion'], mcp.PROTOCOLS[0])
        self.assertEqual(output[-1]['error']['code'], -32600)
        for version in ('2025-06-18', '2025-03-26', '2024-11-05'):
            with self.subTest(version=version):
                output, _ = self.wire(self.handshake(version) + [self.request(2, 'tools/list'), self.call(3, 'vault_status')])
                self.assertEqual(output[0]['result']['protocolVersion'], version)
                self.assertEqual('structuredContent' in output[2]['result'], version == '2025-06-18')
                if version == '2024-11-05':
                    self.assertTrue(all('annotations' not in item for item in output[1]['result']['tools']))
                self.assertEqual(self.value(output[2])['conversations'], 1)

    def test_escaped_surrogates_do_not_terminate_utf8_transport(self):
        output, _ = self.wire([b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n',
                               self.request(2, 'ping')])
        self.assertEqual(len(output), 2)
        self.assertEqual(output[-1], {'jsonrpc': '2.0', 'id': 2, 'result': {}})

    def test_notifications_never_execute_write_tools_or_receive_replies(self):
        output, _ = self.wire(self.handshake() + [
            self.request(None, 'tools/call', {'name': 'backup_skills', 'arguments': {'defaults': False}}),
            self.request(None, 'notifications/unknown'), self.request(2, 'ping')], write=True)
        self.assertEqual([row['id'] for row in output], [1, 2])
        self.assertFalse((self.code / '.local').exists())
        self.assertFalse((self.root / 'skills').exists())

    def test_skill_backup_and_restore_over_stdio_are_local_and_incremental(self):
        source = self.base / 'Skill 源目录' / 'demo'
        source.mkdir(parents=True)
        marker = self.base / 'SCRIPT_RAN'
        original = '# Demo\n历史指令：不应执行脚本\n'
        (source / 'SKILL.md').write_text(original, encoding='utf-8')
        (source / 'run.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n', encoding='utf-8')
        args = {'sources': [str(source)], 'defaults': False}
        output, errors = self.wire(self.handshake() + [
            self.call(2, 'plan_skill_backup', args), self.call(3, 'backup_skills', args),
            self.call(4, 'backup_skills', args), self.call(5, 'list_skill_backups', {'limit': 1}),
        ], write=True)
        self.assertTrue(self.value(output[1])['plan'])
        self.assertEqual(self.value(output[1])['skills'], 1)
        self.assertEqual(self.value(output[2])['copied'], 1)
        self.assertEqual(self.value(output[3])['copied'], 0)
        listing = self.value(output[4]); item = listing['items'][0]
        self.assertEqual(listing['total'], 1)
        destination = self.base / '恢复目录 with spaces'
        restore = {'skill_id': item['id'], 'destination': str(destination)}
        restored, _ = self.wire(self.handshake() + [self.call(2, 'restore_skill', restore),
                                                   self.call(3, 'restore_skill', restore)], write=True)
        result = self.value(restored[1]); again = self.value(restored[2])
        self.assertEqual((Path(result['restored']) / 'SKILL.md').read_text(encoding='utf-8'), original)
        self.assertTrue(again['skipped'])
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / '.skills.json').exists())
        self.assertEqual(errors, '')

    def test_backup_conversations_forces_local_sync_and_redirects_progress(self):
        server = mcp.Server(self.root, allow_write=True)
        local = self.code / '.local'
        expected = {'uploaded': False, 'results': [{'tool': 'codex', 'sessions': 2}]}

        def fake_sync(cfg, selected, upload):
            self.assertEqual(cfg['archive'], str(self.root))
            self.assertEqual(selected, ['codex'])
            self.assertFalse(upload)
            print('进度：已备份 2 个会话')
            (local / 'last-run.json').write_text(json.dumps(expected), encoding='utf-8')

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('vault_mcp.vault.LOCAL', local), patch('vault_mcp.vault.config', return_value={'device': 'test-device'}), \
                patch('vault_mcp.vault.sync', side_effect=fake_sync) as sync, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = server.call('backup_conversations', {'tool': 'codex'})
        self.assertEqual(result, expected)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('进度：已备份 2 个会话', stderr.getvalue())
        self.assertFalse(sync.call_args.kwargs['upload'])

    def test_backup_all_uses_enabled_sources_and_keeps_progress_off_stdout(self):
        local = self.code / '.local'
        imported = local / 'imports/chatgpt/metadata.json'
        imported.parent.mkdir(parents=True)
        imported.write_text('{}', encoding='utf-8')
        cfg = {'device': 'test-machine', 'upload': True}
        for source in ('claude-desktop', 'openclaw', 'workbuddy', 'dsh'):
            home = self.base / (source + ' home')
            home.mkdir()
            cfg[source.replace('-', '_') + '_home'] = str(home)
        selected = ['codex', 'claude', 'claude-desktop', 'openclaw', 'workbuddy', 'dsh', 'chatgpt']
        expected = {'uploaded': False, 'results': [{'tool': source} for source in selected]}

        def fake_sync(config, sources, upload):
            self.assertEqual(config, dict(cfg, archive=str(self.root)))
            self.assertEqual(sources, selected)
            self.assertFalse(upload)
            print('进度：已处理全部来源')
            (local / 'last-run.json').write_text(json.dumps(expected), encoding='utf-8')

        for args in ({}, {'tool': 'all'}):
            with self.subTest(args=args):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch('vault_mcp.vault.LOCAL', local), patch('vault_mcp.vault.config', return_value=cfg), \
                        patch('vault_mcp.vault.enabled_tools', wraps=mcp.vault.enabled_tools) as enabled, \
                        patch('vault_mcp.check_write_tree', wraps=mcp.check_write_tree) as checked, \
                        patch('vault_mcp.vault.sync', side_effect=fake_sync) as sync, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = mcp.Server(self.root, allow_write=True).call('backup_conversations', args)
                self.assertEqual(result, expected)
                enabled.assert_called_once_with(dict(cfg, archive=str(self.root)))
                sync.assert_called_once_with(dict(cfg, archive=str(self.root)), selected, upload=False)
                for source in selected:
                    checked.assert_any_call(self.root, 'test-machine-' + source)
                    checked.assert_any_call(self.root, 'records/test-machine-' + source)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('进度：已处理全部来源', stderr.getvalue())

    def test_one_shot_cli_and_generated_config_use_absolute_native_paths(self):
        common = [sys.executable, str(self.script), '--archive', str(self.root)]
        command = subprocess.run(common + ['--print-config'], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(command.returncode, 0, command.stderr)
        config = json.loads(command.stdout)['mcpServers']['chat-history-vault']
        self.assertTrue(Path(config['command']).is_absolute())
        self.assertEqual(config['args'], [str(self.script), '--archive', str(self.root)])
        command = subprocess.run(common + ['--call', 'search_conversations', '--arguments', '{"query":"测试"}'],
                                 capture_output=True, text=True, encoding='utf-8', timeout=10, cwd=self.base)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)['items'][0]['title'], '测试 MCP')

    def test_backup_rejects_linked_descendants_before_running_sync(self):
        outside = self.base / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_bytes(b'unchanged outside archive')
        paths = [relative for source in mcp.vault.TOOLS
                 for relative in (f'records/test-machine-{source}',
                                  f'test-machine-{source}/sessions/linked-project', f'test-machine-{source}/.git')]
        for relative in paths:
            with self.subTest(path=relative):
                link = self.root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except OSError:
                    self.skipTest('Host does not permit directory symlinks')
                try:
                    with patch('vault_mcp.vault.LOCAL', self.code / '.local'), \
                            patch('vault_mcp.vault.config', return_value={'device': 'test-machine'}), \
                            patch('vault_mcp.vault.enabled_tools', return_value=list(mcp.vault.TOOLS)), \
                            patch('vault_mcp.vault.sync') as sync:
                        with self.assertRaises(ValueError):
                            mcp.Server(self.root, allow_write=True).call('backup_conversations', {'tool': 'all'})
                    sync.assert_not_called()
                    self.assertEqual(sentinel.read_bytes(), b'unchanged outside archive')
                    self.assertEqual(list(outside.iterdir()), [sentinel])
                finally:
                    link.unlink()

    def test_arguments_file_accepts_windows_bom_and_excludes_inline_arguments(self):
        arguments = self.base / '参数 with spaces.json'
        arguments.write_text(json.dumps({'query': '测试 MCP', 'project': '示例项目'}, ensure_ascii=False), encoding='utf-8-sig')
        common = [sys.executable, str(self.script), '--archive', str(self.root), '--call', 'search_conversations',
                  '--arguments-file', str(arguments)]
        result = subprocess.run(common, capture_output=True, text=True, encoding='utf-8', timeout=10, cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['items'][0]['session_id'], self.sid)
        conflict = subprocess.run(common + ['--arguments', '{}'], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(conflict.returncode, 2)
        self.assertEqual(conflict.stdout, '')
        self.assertIn('not allowed with argument', conflict.stderr)


if __name__ == '__main__':
    unittest.main()
