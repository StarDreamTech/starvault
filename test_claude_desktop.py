"""Claude Desktop discovery tests use only temporary, synthetic session stores."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import claude_desktop
import resources
import vault


SID = '12345678-1234-1234-1234-123456789abc'


def session(home, account='account/org', source='claude-ai-chat', staged=False):
    directory = 'claude-code-sessions' if staged else 'local-agent-mode-sessions'
    scope = home / directory / account
    relative = (Path('imported-staging') / (SID + '.jsonl') if staged else
                Path(SID[:8]) / '.claude/projects/session' / (SID + '.jsonl'))
    transcript = scope / relative
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 'message-one',
                                     'message': {'role': 'user', 'content': 'Fixture chat'}}) + '\n',
                          encoding='utf-8')
    data = {'sessionId': 'local-ui-session', 'cliSessionId': SID, 'title': 'Fixture title',
            'cwd': '/old/computer/project', 'createdAt': 1792657134775,
            'lastActivityAt': 1792657135373, 'isArchived': False,
            'remoteMcpServersConfig': {'secret': 'DO NOT COPY'}}
    if source:
        data['importedFrom'] = source
    if staged:
        # Deliberately stale, Windows-style path: match within the current account.
        data['stagedTranscriptPath'] = 'C:\\old-machine\\imported-staging\\' + SID + '.jsonl'
    metadata = scope / 'local_ui-session.json'
    metadata.write_text(json.dumps(data), encoding='utf-8')
    return transcript, metadata


class ClaudeDesktopTest(unittest.TestCase):
    def test_export_preserves_sources_and_refreshes_only_changed_metadata(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            home, destination = root / 'Claude-3p', root / 'backup/device-claude-desktop'
            cloud, metadata = session(home)
            code, _ = session(home, account='code/org', source='sibling-1p-code', staged=True)
            original = {path: path.read_bytes() for path in home.rglob('*') if path.is_file()}
            self.assertEqual(vault.export('claude-desktop', home, destination)['changed_sessions'], 2)
            sessions = json.loads((destination / 'index.json').read_text(encoding='utf-8'))['sessions']
            self.assertEqual(len(sessions), 2)
            by_source = {row['source']: row for row in sessions.values()}
            self.assertEqual(by_source['claude-ai-chat']['originator'], 'Claude (cloud import)')
            self.assertEqual(by_source['sibling-1p-code']['originator'], 'Claude Code (desktop import)')
            for item in sessions.values():
                self.assertEqual(item['origin_kind'], 'claude-desktop')
                self.assertEqual(item['coverage'], 'local_imported_copy')
                self.assertIn('Fixture chat', (destination / item['file']).read_text(encoding='utf-8'))
                record = destination.parent / item['local_record']
                manifest = json.loads(record.read_text(encoding='utf-8'))
                self.assertEqual(manifest['metadata']['origin_metadata']['original_id'], SID)
                self.assertNotIn('DO NOT COPY', record.read_text(encoding='utf-8'))
            exported = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in destination.parent.rglob('*') if path.is_file()}
            self.assertEqual(vault.export('claude-desktop', home, destination)['changed_sessions'], 0)
            for path, value in exported.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), value)
            self.assertEqual({path: path.read_bytes() for path in original}, original)
            revised = json.loads(metadata.read_text(encoding='utf-8'))
            revised.update(title='Renamed cloud conversation', isArchived=True)
            metadata.write_text(json.dumps(revised), encoding='utf-8')
            self.assertEqual(vault.export('claude-desktop', home, destination)['changed_sessions'], 1)
            updated = json.loads((destination / 'index.json').read_text(encoding='utf-8'))['sessions']
            cloud_row = updated[by_source['claude-ai-chat']['id']]
            self.assertEqual(cloud_row['title'], 'Renamed cloud conversation')
            self.assertTrue(cloud_row['archived'])
            self.assertEqual(updated[by_source['sibling-1p-code']['id']], by_source['sibling-1p-code'])
            self.assertEqual(cloud.read_bytes(), original[cloud])
            self.assertEqual(code.read_bytes(), original[code])

    def test_cli_import_keeps_split_assistant_blocks_and_tool_log_pairs(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            home, destination = root / 'Claude-3p', root / 'backup/device-claude-desktop'
            transcript, _ = session(home, source='terminal-cli', staged=True)
            rows = [json.loads(transcript.read_text(encoding='utf-8')),
                    {'type': 'assistant', 'uuid': 'first-block', 'message': {
                        'id': 'same-api-message', 'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'First assistant block'}]}},
                    {'type': 'assistant', 'uuid': 'second-block', 'message': {
                        'id': 'same-api-message', 'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'Second assistant block'},
                                    {'type': 'tool_use', 'id': 'call-one', 'name': 'Read',
                                     'input': {'file_path': '/fixture/missing.txt'}}]}},
                    {'type': 'user', 'uuid': 'tool-output', 'message': {
                        'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-one',
                                                   'content': 'Tool output fixture'}]}}]
            raw = ''.join(json.dumps(row) + '\n' for row in rows).encode('utf-8')
            transcript.write_bytes(raw)
            self.assertEqual(vault.export('claude-desktop', home, destination)['changed_sessions'], 1)
            item, = json.loads((destination / 'index.json').read_text(encoding='utf-8'))['sessions'].values()
            body = (destination / item['file']).read_text(encoding='utf-8')
            self.assertIn('First assistant block', body)
            self.assertIn('Second assistant block', body)
            self.assertEqual(item['operations'], 2)
            result = resources.migrate(destination.parent, {'logs'}, branch=destination.name)
            self.assertEqual(result['operations'], 2)
            record = destination.parent / item['local_record']
            operations = record.with_name('operations.jsonl').read_bytes()
            self.assertEqual([json.loads(line) for line in operations.splitlines()], rows[2:])
            before = record.with_name('operations.jsonl').stat().st_mtime_ns
            resources.migrate(destination.parent, {'logs'}, branch=destination.name)
            self.assertEqual(record.with_name('operations.jsonl').stat().st_mtime_ns, before)
            self.assertEqual(transcript.read_bytes(), raw)

    def test_platform_defaults_and_override(self):
        with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=Path('/user')):
            for platform, expected in [('darwin', '/user/Library/Application Support/Claude'),
                                       ('win32', '/user/AppData/Roaming/Claude'),
                                       ('linux', '/user/.config/Claude')]:
                with patch.object(claude_desktop.sys, 'platform', platform):
                    self.assertEqual(str(claude_desktop.default_home()), expected)
            with patch.object(claude_desktop.sys, 'platform', 'win32'), patch.dict(os.environ, {'APPDATA': '/roaming'}):
                self.assertEqual(claude_desktop.default_home(), Path('/roaming/Claude'))
            with patch.object(claude_desktop.sys, 'platform', 'linux'), patch.dict(os.environ, {'XDG_CONFIG_HOME': '/xdg'}):
                self.assertEqual(claude_desktop.default_home(), Path('/xdg/Claude'))
            with patch.dict(os.environ, {'CLAUDE_DESKTOP_HOME': '/custom/Claude'}):
                self.assertEqual(claude_desktop.default_home(), Path('/custom/Claude'))

    def test_sources_accounts_and_relocation_keep_identity_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'computer-one/Claude'
            third_party = home.with_name('Claude-3p')
            session(third_party)
            session(third_party, account='second-account/org')
            session(third_party, account='code/org', source='sibling-1p-code', staged=True)
            session(third_party, account='terminal/org', source='terminal-cli', staged=True)
            session(home, source=None)
            # A metadata-only record must not become a phantom conversation.
            missing, _ = session(home, account='missing/org')
            missing.unlink()
            items = list(claude_desktop.session_items(home))
            self.assertEqual(len(items), 5)
            self.assertEqual(len({item['id'] for item in items}), 5)
            self.assertTrue(all(re.fullmatch(r'[a-z0-9-]+', item['id']) for item in items))
            by_source = {item['meta']['source']: item['meta'] for item in items}
            self.assertEqual(by_source['claude-ai-chat']['originator'], 'Claude (cloud import)')
            self.assertEqual(by_source['claude-ai-chat']['coverage'], 'local_imported_copy')
            self.assertIn('Code', by_source['sibling-1p-code']['originator'])
            self.assertIn('CLI', by_source['terminal-cli']['originator'])
            for item in items:
                origin = item['meta']['origin_metadata']
                self.assertEqual(origin['original_id'], SID)
                self.assertEqual(origin['sessionId'], 'local-ui-session')
                self.assertNotIn('remoteMcpServersConfig', origin)
                self.assertFalse(item['meta']['archived'])
            migrated = root / 'computer-two'
            shutil.copytree(home.parent, migrated)
            after = list(claude_desktop.session_items(migrated / 'Claude'))
            self.assertEqual({item['id'] for item in items}, {item['id'] for item in after})
            self.assertEqual({item['id'] for item in items},
                             {item['id'] for item in claude_desktop.session_items(third_party)})

    def test_orphan_transcript_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'Claude'
            transcript, metadata = session(home)
            metadata.unlink()
            outside = root / 'unrelated.jsonl'
            outside.write_text('{}\n', encoding='utf-8')
            try:
                (transcript.parent / 'external.jsonl').symlink_to(outside)
            except (OSError, NotImplementedError):
                pass  # Windows can require privileges for creating a symlink.
            item, = list(claude_desktop.session_items(home))
            self.assertIsNone(item['meta']['archived'])
            self.assertEqual(item['meta']['coverage'], 'local_desktop_session')
            self.assertEqual(item['path'], transcript)

    def test_corrupt_metadata_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'Claude'
            _, metadata = session(home)
            metadata.write_text('{broken json}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Invalid Claude Desktop session metadata'):
                list(claude_desktop.session_items(home))


if __name__ == '__main__':
    unittest.main()
