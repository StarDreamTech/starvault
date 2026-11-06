"""User copy preferences control new backups and never delete saved files."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import resources
import vault


class AttachmentPolicyTest(unittest.TestCase):
    def test_switch_from_conversations_only_to_images_and_back(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            home = root / 'codex'
            source = home / 'sessions/12345678-1234-4234-8234-123456789abc.jsonl'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_image',
                'image_url': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='}]}}) + '\n')
            archive = root / 'archive'
            archive.mkdir()
            dest = archive / 'fixture-codex'
            resources.save_policy(archive, [])
            self.assertEqual(resources.policy(archive), set())
            self.assertEqual(vault.export('codex', home, dest)['changed_sessions'], 1)
            self.assertFalse(list(archive.glob('images/*')))
            resources.save_policy(archive, ['images'])
            self.assertEqual(vault.export('codex', home, dest)['changed_sessions'], 1)
            copies = list(archive.glob('images/*'))
            self.assertEqual(len(copies), 1)
            previous = copies[0].read_bytes()
            resources.save_policy(archive, [])
            self.assertEqual(vault.export('codex', home, dest)['changed_sessions'], 0)
            self.assertEqual(copies[0].read_bytes(), previous)

    def test_setting_policy_does_not_copy_files_and_rejects_links_or_unknown_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = set(root.iterdir())
            self.assertTrue(resources.save_policy(root, ['documents'])['settings_only'])
            self.assertEqual(set(root.iterdir()) - before, {root / '.migration.json'})
            with self.assertRaises(ValueError):
                resources.save_policy(root, ['unknown'])
            external = root / 'outside.json'
            external.write_text('{"kinds": ["images"]}')
            (root / '.migration.json').unlink()
            (root / '.migration.json').symlink_to(external)
            with self.assertRaises(ValueError):
                resources.save_policy(root, [])
            with self.assertRaises(ValueError):
                resources.policy(root)
            self.assertEqual(external.read_text(), '{"kinds": ["images"]}')
