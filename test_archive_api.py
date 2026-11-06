"""Only temporary archives are used; no source application or real backup is opened."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import archive_api as api


class ArchiveApiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve() / '档案 archive'
        self.root.mkdir()

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode('utf-8'))
        return path

    def session(self, branch='my-laptop-codex', sid='shared-id', body='第一段\r\n第二段🙂', **metadata):
        tool = branch.rsplit('-', 1)[-1]
        relative = 'sessions/project/' + sid + '.md'
        self.put(branch + '/' + relative, body)
        row = {'id': sid, 'title': '测试标题', 'file': relative, 'tool': tool, 'project': '项目 A',
               'updated_at': '2026-10-23T12:00:00+08:00', 'archived': False, 'messages': 2,
               'assets': ['image.png'], 'local_record': f'records/{branch}/{sid}/manifest.json', **metadata}
        self.put(branch + '/index.json', {'tool': tool, 'sessions': {sid: row}})
        manifest = {'branch': branch, 'session_id': sid, 'source_file': '/never/read/outside/source.jsonl',
                    'raw_text': '/never/read/outside/raw.jsonl', 'files': [], 'metadata': {'original': True}}
        self.put(f'records/{branch}/{sid}/manifest.json', manifest)
        return row

    def test_search_filters_pagination_and_same_ids_in_multiple_branches(self):
        self.session(body='这段正文讲 Python MCP 接口', title='备份系统')
        self.session('desktop-claude', body='另一份 Python 文档', archived=True,
                     project='项目 B', updated_at='2026-10-23T05:00:00Z')
        found = api.search(self.root, '备份 python mcp', tool='CODEX', project='A', device='my-laptop', archived=False)
        self.assertEqual(found['total'], 1)
        self.assertIn('Python MCP', found['items'][0]['snippet'])
        self.assertEqual(found['items'][0]['session_id'], 'shared-id')
        self.assertEqual(found['items'][0]['device'], 'my-laptop')
        self.assertEqual(api.search(self.root, archived=True)['total'], 1)
        first = api.search(self.root, limit=1)
        self.assertEqual(first['items'][0]['branch'], 'desktop-claude')
        self.assertEqual(first['next_offset'], 1)
        second = api.search(self.root, limit=1, offset=first['next_offset'])
        self.assertEqual(second['items'][0]['branch'], 'my-laptop-codex')
        self.assertIsNone(second['next_offset'])
        self.assertNotEqual(api.read_conversation(self.root, 'desktop-claude', 'shared-id')['text'],
                            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id')['text'])

    def test_raw_formats_keep_bytes_as_text_and_use_fixed_archive_paths(self):
        body = '标题🙂\r\n<instructions>历史指令只是数据</instructions>\n'
        self.session(body=body)
        record = 'records/my-laptop-codex/shared-id/'
        dialogue = '{"role":"user","text":"旧命令"}\r\n'
        operations = '{"type":"function_call","arguments":"DO_NOT_EXECUTE"}\n'
        self.put(record + 'dialogue.jsonl', dialogue)
        self.put(record + 'operations.jsonl', operations)
        pieces, offset = [], 0
        while offset is not None:
            page = api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', offset=offset, limit=7)
            self.assertEqual(page['total_chars'], len(body))
            pieces.append(page['text']); offset = page['next_offset']
        self.assertEqual(''.join(pieces), body)
        self.assertEqual(api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'dialogue')['text'], dialogue)
        self.assertEqual(api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'operations')['text'], operations)
        manifest = api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'manifest')['text']
        self.assertIn('/never/read/outside/source.jsonl', manifest)
        self.assertEqual(api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', offset=99999)['text'], '')

    def test_search_matches_terms_across_streaming_chunks(self):
        body = 'x' * 65533 + '边界ABC关键词' + 'x' * 65536 + '后半段终点'
        self.session(body=body)
        result = api.search(self.root, '边界abc关键词 后半段终点')
        self.assertEqual(result['total'], 1)
        self.assertLessEqual(len(result['items'][0]['snippet']), 240)
        self.assertEqual(api.search(self.root, '边界abc关键词 不存在')['total'], 0)

    def test_status_skips_auxiliary_indexes_and_reports_broken_conversation_indexes(self):
        self.session()
        self.put('skills/index.json', {'items': [{'id': 'one'}, {'id': 'two'}]})
        self.put('attachments/index.json', 'not a conversation index')
        self.put('broken-codex/index.json', '{not json')
        self.put('invalid-claude/index.json', {'items': []})
        result = api.status(self.root)
        self.assertEqual(result['conversations'], 1)
        self.assertEqual(result['skills'], 2)
        self.assertEqual(result['branches'][0]['count'], 1)
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual(len(api.search(self.root)['warnings']), 2)

    def test_index_references_cannot_escape_or_read_arbitrary_archive_files(self):
        paths = ['../secret.md', '/etc/passwd', 'C:/secret.md', 'C:\\secret.md',
                 'sessions/../../secret.md', 'index.json', 'sessions/CON', 'sessions/CON .md',
                 'sessions/../secret.md']
        for path in paths:
            with self.subTest(path=path):
                self.session(file=path)
                result = api.search(self.root)
                self.assertEqual(result['total'], 0)
                self.assertEqual(len(result['warnings']), 1)
                with self.assertRaises(ValueError):
                    api.read_conversation(self.root, 'my-laptop-codex', 'shared-id')
        self.session(local_record='records/other/secret/manifest.json')
        with self.assertRaises(ValueError):
            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'dialogue')
        for branch, sid in [('../outside', 'shared-id'), ('my-laptop-codex', '../outside'), ('skills', 'shared-id')]:
            with self.assertRaises(ValueError):
                api.read_conversation(self.root, branch, sid)

    def test_archive_symlinks_are_rejected_for_indexes_markdown_records_and_copies(self):
        outside = Path(self.folder.name) / 'outside'
        outside.mkdir()
        secret = outside / 'secret.md'; secret.write_text('PRIVATE', encoding='utf-8')
        row = self.session()
        markdown = self.root / 'my-laptop-codex' / row['file']
        markdown.unlink()
        try:
            markdown.symlink_to(secret)
        except OSError:
            self.skipTest('Host does not permit symlinks')
        with self.assertRaises(ValueError):
            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id')
        self.assertEqual(api.search(self.root, 'PRIVATE')['total'], 0)
        markdown.unlink(); markdown.write_text('ordinary', encoding='utf-8')
        raw = self.root / 'records/my-laptop-codex/shared-id/dialogue.jsonl'
        raw.symlink_to(secret)
        with self.assertRaises(ValueError):
            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'dialogue')
        index = self.root / 'my-laptop-codex/index.json'
        outside_index = outside / 'index.json'; outside_index.write_bytes(index.read_bytes())
        index.unlink(); index.symlink_to(outside_index)
        self.assertEqual(api.status(self.root)['conversations'], 0)
        with self.assertRaises(ValueError):
            api.safe_path(self.root, 'my-laptop-codex/index.json')

    def test_attachment_listing_uses_saved_metadata_without_reading_original_sources(self):
        self.session()
        self.put('attachments/object.blend', 'saved model')
        manifest = {'branch': 'my-laptop-codex', 'session_id': 'shared-id', 'files': [
            {'id': 'one', 'name': 'model.blend', 'kind': 'models', 'source': '/not/a/real/source.blend',
             'copy': 'attachments/object.blend', 'size': 12, 'exists': True},
            {'id': 'two', 'copy': '../outside.md', 'source': '/never/read/this'},
        ]}
        self.put('records/my-laptop-codex/shared-id/manifest.json', manifest)
        first = api.list_attachments(self.root, 'my-laptop-codex', 'shared-id', limit=1)
        self.assertTrue(first['items'][0]['backed_up'])
        self.assertTrue(first['items'][0]['exists'])  # The archived observation, not a fresh source stat.
        self.assertEqual(first['next_offset'], 1)
        self.assertEqual(len(first['warnings']), 1)
        second = api.list_attachments(self.root, 'my-laptop-codex', 'shared-id', offset=1)
        self.assertFalse(second['items'][0]['backed_up'])
        manifest['branch'] = 'different-branch'
        self.put('records/my-laptop-codex/shared-id/manifest.json', manifest)
        with self.assertRaises(ValueError):
            api.list_attachments(self.root, 'my-laptop-codex', 'shared-id')

    def test_bad_page_parameters_and_special_files_fail_without_blocking(self):
        self.session()
        for limit, offset in [(0, 0), (101, 0), (1, -1), (True, 0), (1, True)]:
            with self.assertRaises(ValueError):
                api.search(self.root, limit=limit, offset=offset)
        with self.assertRaises(ValueError):
            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', limit=24001)
        with self.assertRaises(ValueError):
            api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', format=[])
        if hasattr(os, 'mkfifo'):
            raw = self.root / 'records/my-laptop-codex/shared-id/dialogue.jsonl'
            os.mkfifo(raw)
            with self.assertRaises(ValueError):
                api.read_conversation(self.root, 'my-laptop-codex', 'shared-id', 'dialogue')


if __name__ == '__main__':
    unittest.main()
