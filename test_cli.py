"""Unified CLI checks against copied code and disposable archives only."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import vault


class CliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.code = self.base / '工具 code'
        self.code.mkdir()
        source = Path(__file__).resolve().parent
        for name in ('chat_history_vault.py', 'vault.py', 'vault_mcp.py', 'archive_api.py',
                     'resources.py', 'skills_backup.py', 'chatgpt_import.py', 'claude_desktop.py',
                     'claude_cache.py', 'claude_projects.py'):
            shutil.copy2(source / name, self.code / name)
        shutil.copytree(source / 'skills/use-chat-history-vault', self.code / 'skills/use-chat-history-vault')
        self.archive = self.base / '档案 backup'
        self.archive.mkdir()
        self.config = self.base / '嵌套 state/config'
        self.env = {**os.environ, 'PYTHONPATH': str(self.code),
                    'CHAT_HISTORY_VAULT_CONFIG_DIR': str(self.config),
                    'HOME': str(self.base / 'home'), 'USERPROFILE': str(self.base / 'home'),
                    'CODEX_HOME': str(self.base / 'codex'),
                    'CLAUDE_CONFIG_DIR': str(self.base / 'claude'),
                    'CLAUDE_DESKTOP_HOME': str(self.base / 'Claude'),
                    'PYTHONIOENCODING': 'ascii'}
        self.branch, self.sid = 'test-machine-codex', 'session-中文'
        self.body = '# 历史记录🙂\n用户：迁移 Windows\n这是原始正文。\n'
        relative = f'sessions/示例项目/{self.sid}.md'
        self.put(f'{self.branch}/{relative}', self.body)
        self.put(f'{self.branch}/index.json', {'tool': 'codex', 'sessions': {
            self.sid: {'id': self.sid, 'title': '迁移方案', 'tool': 'codex', 'project': '示例 项目',
                       'file': relative, 'updated_at': '2026-10-23T12:00:00+08:00', 'archived': False,
                       'messages': 1, 'assets': [], 'local_record': f'records/{self.branch}/{self.sid}/manifest.json'}
        }})
        self.put(f'records/{self.branch}/{self.sid}/manifest.json', {
            'branch': self.branch, 'session_id': self.sid,
            'files': [{'id': 'a', 'name': '场景.blend', 'kind': 'models', 'source': '/not/read/场景.blend'}]})
        self.dialogue = '{"role":"user","text":"迁移 Windows"}\n'
        self.put(f'records/{self.branch}/{self.sid}/dialogue.jsonl', self.dialogue)

    def put(self, relative, value):
        path = self.archive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def command(self, *args, payload=None, success=True):
        process = subprocess.run([sys.executable, '-m', 'chat_history_vault', *map(str, args)],
                                 cwd=self.base, env=self.env, input=payload, capture_output=True,
                                 text=True, encoding='utf-8', timeout=20)
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            self.assertNotEqual(process.returncode, 0, process.stdout)
            self.assertEqual(process.stdout, '')
            self.assertIn('error', process.stderr.lower())
            self.assertNotIn('Traceback', process.stderr)
        return process

    def query(self, *args):
        result = self.command(*args, '--archive', self.archive)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_help_version_and_packaged_skill_path_are_discoverable(self):
        help_text = self.command('--help').stdout
        for name in ('search', 'read', 'mcp', 'skills', 'sync'):
            self.assertIn(name, help_text)
        self.assertIn('chat-history-vault', self.command('--version').stdout)
        for command in ('init', 'search', 'read', 'call', 'mcp'):
            with self.subTest(command=command):
                self.assertIn('usage:', self.command(command, '--help').stdout)
        self.assertIn('--claude-desktop-home', self.command('init', '--help').stdout)
        self.assertIn('claude-desktop', self.command('export', '--help').stdout)
        skill = Path(self.command('skill-path').stdout.strip())
        self.assertTrue(skill.is_absolute())
        self.assertEqual(skill, self.code / 'skills/use-chat-history-vault')
        self.assertTrue((skill / 'agents/openai.yaml').is_file())
        self.assertFalse(self.config.exists())

    def test_native_queries_preserve_utf8_filters_and_character_pagination(self):
        found = self.query('search', '迁移 windows', '--tool', 'codex', '--project', '示例',
                           '--device', 'test-machine', '--archived', 'no', '--limit', '1')
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['items'][0]['session_id'], self.sid)
        self.assertEqual(self.query('search', '--archived', 'yes')['total'], 0)
        first = self.query('read', self.branch, self.sid, '--limit', '12')
        self.assertEqual(first['text'], self.body[:12])
        second = self.query('read', self.branch, self.sid, '--offset', str(first['next_offset']))
        self.assertEqual(first['text'] + second['text'], self.body)
        self.assertIsNone(second['next_offset'])
        self.assertEqual(self.query('read', self.branch, self.sid, '--format', 'dialogue')['text'], self.dialogue)
        attachments = self.query('attachments', self.branch, self.sid)
        self.assertEqual(attachments['items'][0]['name'], '场景.blend')
        self.assertFalse(attachments['items'][0]['backed_up'])
        self.assertFalse(self.config.exists())

    def test_call_tools_and_error_output_preserve_read_only_boundary(self):
        request = self.base / '请求 参数.json'
        request.write_text(json.dumps({'query': '迁移', 'limit': 1}, ensure_ascii=False), encoding='utf-8-sig')
        found = self.query('call', 'search_conversations', '--arguments-file', request)
        self.assertEqual(found['total'], 1)
        self.assertIn('get_backup_preferences', {tool['name'] for tool in self.query('tools')['tools']})
        self.assertIn('set_backup_preferences', {tool['name'] for tool in self.query('tools', '--allow-write')['tools']})
        self.assertEqual(self.query('call', 'vault_status')['conversations'], 1)
        bad_commands = [('call',), ('search', '--limit', '0'), ('search', '--unknown'),
                        ('read', '../outside', self.sid),
                        ('call', 'backup_skills', '--arguments', '{"defaults":false}'),
                        ('call', 'vault_status', '--arguments', '{invalid'),
                        ('call', 'vault_status', '--arguments', '{}', '--arguments-file', str(request))]
        for args in bad_commands:
            with self.subTest(args=args):
                self.command(*args, '--archive', self.archive, success=False)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.archive / 'skills').exists())

    def test_mcp_stdio_and_generated_config_keep_archive_and_config_scope(self):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'cli-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'vault_status'}}]
        process = self.command('mcp', '--archive', self.archive,
                               payload=''.join(json.dumps(value) + '\n' for value in messages))
        output = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([value['id'] for value in output], [1, 2])
        self.assertEqual(output[-1]['result']['structuredContent']['conversations'], 1)
        self.assertEqual(process.stderr, '')
        config = self.query('mcp', '--print-config')['mcpServers']['chat-history-vault']
        self.assertEqual(config['command'], sys.executable)
        self.assertIn(str(self.archive), config['args'])
        self.assertEqual(config['env']['CHAT_HISTORY_VAULT_CONFIG_DIR'], str(self.config))
        self.assertFalse(self.config.exists())

    def test_legacy_init_export_and_status_use_isolated_user_configuration(self):
        home = self.base / 'codex'
        sid = '12345678-1234-1234-1234-123456789abc'
        source = home / 'sessions' / ('rollout-' + sid + '.jsonl')
        source.parent.mkdir(parents=True)
        original = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                              'content': [{'type': 'input_text', 'text': 'CLI 本地备份'}]}}, ensure_ascii=False) + '\n'
        source.write_text(original, encoding='utf-8')
        self.command('init', '--repo', 'https://github.com/example/cli-test-never-upload.git',
                     '--device', 'cli-computer', '--archive', self.archive, '--codex-home', home,
                     '--claude-home', self.base / 'claude',
                     '--claude-desktop-home', self.base / 'Claude')
        self.assertTrue((self.config / 'config.json').is_file())
        self.assertFalse((self.code / '.local').exists())
        self.command('export', '--tool', 'codex')
        status = json.loads(self.command('status').stdout)
        self.assertEqual(status['config']['archive'], str(self.archive))
        self.assertEqual(status['config']['claude_desktop_home'], str(self.base / 'Claude'))
        self.assertFalse(status['last_run']['uploaded'])
        self.assertEqual(status['last_run']['results'][0]['changed_sessions'], 1)
        self.assertEqual(source.read_text(encoding='utf-8'), original)
        self.command('export', '--tool', 'codex')
        status = json.loads(self.command('status').stdout)
        self.assertEqual(status['last_run']['results'][0]['changed_sessions'], 0)

    def test_legacy_skill_commands_preserve_plan_backup_and_restore(self):
        source = self.base / '自定义 Skills/demo'
        source.mkdir(parents=True)
        original = '# 示例\n说明与脚本只是资料。\n'
        (source / 'SKILL.md').write_text(original, encoding='utf-8')
        plan = self.query('skills', '--no-defaults', '--source', source, '--plan')
        self.assertEqual(plan['skills'], 1)
        self.assertFalse((self.archive / 'skills').exists())
        backup = self.query('skills', '--no-defaults', '--source', source)
        self.assertEqual(backup['copied'], 1)
        self.assertEqual(self.query('skills', '--list')['items'][0]['id'], backup['items'][0]['id'])
        restored = self.query('skills', '--restore', backup['items'][0]['id'],
                              '--destination', self.base / '新电脑 skills')
        self.assertEqual((Path(restored['restored']) / 'SKILL.md').read_text(encoding='utf-8'), original)

    def test_config_directory_preserves_override_legacy_and_platform_defaults(self):
        legacy = self.code / '.local'
        legacy.mkdir()
        (legacy / 'config.json').write_text('{}', encoding='utf-8')
        home = self.base / 'home'
        with patch.object(vault, 'ROOT', self.code), patch.object(Path, 'home', return_value=home), \
                patch.dict(os.environ, {'CHAT_HISTORY_VAULT_CONFIG_DIR': str(self.config)}, clear=True):
            self.assertEqual(vault.config_directory(), self.config)
            del os.environ['CHAT_HISTORY_VAULT_CONFIG_DIR']
            self.assertEqual(vault.config_directory(), legacy)
            (legacy / 'config.json').unlink()
            # .git can be a file in worktrees; either source checkout keeps its local state.
            (self.code / '.git').write_text('gitdir: unused', encoding='utf-8')
            self.assertEqual(vault.config_directory(), legacy)
            (self.code / '.git').unlink()
            for platform in ('darwin', 'win32', 'linux'):
                with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                    app_home = home / ('Library/Application Support/ChatHistoryVault'
                                       if platform == 'darwin' else 'ChatHistoryVault')
                    self.assertEqual(vault.config_directory(), app_home / 'tool/.local')
                    self.assertFalse(app_home.exists())

    def test_schedule_pins_selected_config_without_launching_real_services(self):
        home = self.base / 'home'
        with patch.object(vault, 'ROOT', self.code), patch.object(vault, 'LOCAL', self.config), \
                patch.object(Path, 'home', return_value=home), patch.object(sys, 'platform', 'darwin'), \
                patch.object(vault.os, 'getuid', return_value=501, create=True), \
                patch.object(vault, 'config', return_value={}), patch.object(vault, 'run') as launch, \
                contextlib.redirect_stdout(io.StringIO()):
            vault.schedule(True)
        plist_path = home / 'Library/LaunchAgents/com.cpython666.chat-history-vault.plist'
        job = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(job['EnvironmentVariables']['CHAT_HISTORY_VAULT_CONFIG_DIR'], str(self.config))
        self.assertEqual(job['ProgramArguments'], [sys.executable, str(self.code / 'vault.py'), 'sync'])
        self.assertEqual(job['StandardOutPath'], str(self.config / 'schedule.log'))
        self.assertTrue(self.config.is_dir())
        launch.assert_called_once_with(['launchctl', 'bootstrap', 'gui/501', str(plist_path)])


if __name__ == '__main__':
    unittest.main()
