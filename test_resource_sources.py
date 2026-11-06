"""Local snapshot migration checks; fixtures never read actual client histories."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import resources
import vault
from test_agent_sources import compress_zstd


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ZkAAAAASUVORK5CYII=')
BRANCH, SID = 'fixture-dsh', 'session-fixture'


class ResourceSourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def fixture(self, name='archive', suffix='.jsonl', source_copy=True):
        root = self.base / name
        (root / BRANCH).mkdir(parents=True)
        relative = f'records/{BRANCH}/{SID}/source' + suffix
        source = root / relative
        source.parent.mkdir(parents=True)
        rows = [
            {'type': 'user/message', 'image': base64.b64encode(PNG).decode()},
            {'type': 'tool/call', 'command': 'DO_NOT_EXECUTE_FIRST'},
            {'type': 'tool/call', 'command': 'DO_NOT_EXECUTE_OTHER'},
        ]
        lines = [(json.dumps(row) + '\n').encode() for row in rows]
        raw = b''.join(lines)
        source.write_bytes(compress_zstd(raw) if suffix in ('.jsonl.zst', '.jsonl.zstd') else raw)
        image = hashlib.sha256(PNG).hexdigest() + '.png'
        metadata = {'assets': [image]}
        if source_copy:
            metadata['source_copy'] = relative
        operations = [{'offset': len(lines[0]), 'length': len(lines[1]),
                       'sha256': hashlib.sha256(lines[1]).hexdigest()}]
        manifest = resources.finish(root, BRANCH, SID, source, {}, operations, metadata, [], vault.write)
        # An explicit image migration must override the remembered automatic policy.
        resources.write(root / '.migration.json', {'kinds': ['logs']})
        return root, manifest, source, lines, image

    def assert_recovered(self, root, lines, image):
        record = root / 'records' / BRANCH / SID
        plan = resources.migrate(root, ['images', 'logs'], plan=True)
        self.assertGreaterEqual(plan['estimated_bytes'], len(b''.join(lines)))
        self.assertFalse((record / 'operations.jsonl').exists())
        self.assertFalse((root / 'images' / image).exists())
        result = resources.migrate(root, ['images', 'logs'])
        self.assertEqual(result['missing'], 0)
        self.assertEqual((record / 'operations.jsonl').read_bytes(), lines[1])
        self.assertEqual((root / 'images' / image).read_bytes(), PNG)
        before = {path: path.stat().st_mtime_ns
                  for path in (record / 'operations.jsonl', record / 'manifest.json', root / 'images' / image)}
        with patch('resources.source_stream', wraps=resources.source_stream) as stream:
            again = resources.migrate(root, ['images', 'logs'])
        stream.assert_not_called()
        self.assertEqual(again['copied'], 0)
        self.assertEqual(again['estimated_bytes'], 0)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})

    def test_relative_snapshot_survives_move_and_ignores_old_source(self):
        root, _, source, lines, image = self.fixture()
        moved = self.base / 'moved archive'
        shutil.copytree(root, moved)
        source.write_bytes(b'OLD COMPUTER PATH MUST NOT BE READ\n')
        self.assert_recovered(moved, lines, image)
        self.assertEqual(source.read_bytes(), b'OLD COMPUTER PATH MUST NOT BE READ\n')

    def test_legacy_snapshot_paths_relocate_for_posix_and_windows(self):
        for style in ('posix', 'windows'):
            with self.subTest(style=style):
                root, manifest, source, lines, image = self.fixture(style, source_copy=False)
                moved = self.base / (style + '-moved')
                shutil.copytree(root, moved)
                current_manifest = moved / manifest.relative_to(root)
                if style == 'windows':
                    data = resources.read(current_manifest)
                    data['source_file'] = rf'C:\old-computer\backup\records\{BRANCH}\{SID}\source.jsonl'
                    resources.write(current_manifest, data)
                source.write_bytes(b'OLD COMPUTER PATH MUST NOT BE READ\n')
                self.assert_recovered(moved, lines, image)

    def test_compressed_snapshot_offsets_and_images_use_decoded_jsonl(self):
        for suffix in ('.jsonl.zst', '.jsonl.zstd'):
            with self.subTest(suffix=suffix):
                root, _, source, lines, image = self.fixture(suffix, suffix=suffix)
                moved = self.base / (suffix + '-moved')
                shutil.copytree(root, moved)
                source.unlink()
                self.assert_recovered(moved, lines, image)

    def test_same_count_operation_changes_refresh_with_unchanged_source_stat(self):
        root, manifest, source, lines, _ = self.fixture()
        resources.migrate(root, ['logs'])
        before = source.stat()
        data = resources.read(manifest)
        data['operation_rows'] = [{'offset': len(lines[0]) + len(lines[1]), 'length': len(lines[2]),
                                   'sha256': hashlib.sha256(lines[2]).hexdigest()}]
        resources.write(manifest, data)
        resources.migrate(root, ['logs'])
        self.assertEqual(manifest.with_name('operations.jsonl').read_bytes(), lines[2])
        lines[2] = lines[2].replace(b'OTHER', b'FRESH')
        source.write_bytes(b''.join(lines))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((source.stat().st_size, source.stat().st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))
        data = resources.read(manifest)
        data['operation_rows'][0]['sha256'] = hashlib.sha256(lines[2]).hexdigest()
        resources.write(manifest, data)
        resources.migrate(root, ['logs'])
        self.assertEqual(manifest.with_name('operations.jsonl').read_bytes(), lines[2])
        self.assertEqual(resources.migrate(root, ['logs'])['estimated_bytes'], 0)

    def test_bad_compressed_record_hash_preserves_existing_log_and_cleans_temp(self):
        root, manifest, _, _, _ = self.fixture(suffix='.jsonl.zst')
        target = manifest.with_name('operations.jsonl')
        target.write_bytes(b'previous verified log\n')
        data = resources.read(manifest)
        data['operation_rows'][0]['sha256'] = '0' * 64
        resources.write(manifest, data)
        before = {p.name: p.read_bytes() for p in manifest.parent.iterdir()}
        with self.assertRaisesRegex(RuntimeError, '源记录发生变化'):
            resources.migrate(root, ['logs'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in manifest.parent.iterdir()})

    def test_source_copy_rejects_traversal_absolute_paths_and_links(self):
        root, manifest, _, _, _ = self.fixture()
        outside = self.base / 'outside.jsonl'
        outside.write_bytes(b'PRIVATE OUTSIDE DATA\n')
        data = resources.read(manifest)
        for relative in ('../outside.jsonl', str(outside)):
            with self.subTest(relative=relative):
                data['metadata']['source_copy'] = relative
                resources.write(manifest, data)
                with patch('resources.source_stream') as stream, self.assertRaises(ValueError):
                    resources.migrate(root, ['logs'])
                stream.assert_not_called()
        link = manifest.with_name('linked.jsonl')
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Host does not permit symlinks')
        data['metadata']['source_copy'] = link.relative_to(root).as_posix()
        resources.write(manifest, data)
        with patch('resources.source_stream') as stream, self.assertRaises(ValueError):
            resources.migrate(root, ['logs'])
        stream.assert_not_called()

    def test_missing_local_snapshot_never_falls_back_to_old_computer(self):
        for source_copy in (True, False):
            with self.subTest(source_copy=source_copy):
                root, manifest, source, _, _ = self.fixture(str(source_copy), source_copy=source_copy)
                moved = self.base / (str(source_copy) + '-moved')
                shutil.copytree(root, moved)
                (moved / source.relative_to(root)).unlink()
                with patch('resources.source_stream') as stream:
                    result = resources.migrate(moved, ['logs', 'images'])
                stream.assert_not_called()
                self.assertEqual(result['missing'], 2)
                self.assertFalse((moved / manifest.relative_to(root)).with_name('operations.jsonl').exists())
                self.assertTrue(source.is_file())


if __name__ == '__main__':
    unittest.main()
