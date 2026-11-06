"""Regression checks for agent-source snapshots; all exports use temporary fixtures."""
import contextlib
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import resources
import vault


SID = '12345678-1234-1234-1234-123456789abc'


def export(tool, home, destination):
    with contextlib.redirect_stdout(io.StringIO()):
        return vault.export(tool, home, destination)


def jsonl(rows):
    return ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode('utf-8')


def compress_zstd(data):
    library = vault.zstd_library()
    if library is None:
        raise unittest.SkipTest('libzstd is unavailable for creating compressed fixtures')
    library.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    library.ZSTD_compressBound.restype = ctypes.c_size_t
    library.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    library.ZSTD_compress.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = [ctypes.c_size_t]
    library.ZSTD_isError.restype = ctypes.c_uint
    source = ctypes.create_string_buffer(data)
    target = ctypes.create_string_buffer(library.ZSTD_compressBound(len(data)))
    size = library.ZSTD_compress(target, len(target), source, len(data), 1)
    if library.ZSTD_isError(size):
        raise RuntimeError('Could not compress the zstd fixture')
    return target.raw[:size]


def openclaw_database(home, agent, events):
    path = home / 'agents' / agent / 'agent/openclaw-agent.sqlite'
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('CREATE TABLE transcript_events '
                       '(session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB, '
                       'PRIMARY KEY (session_id, seq))')
    connection.executemany('INSERT INTO transcript_events VALUES (?, ?, ?, NULL)',
                           [(SID, number, json.dumps(event)) for number, event in enumerate(events)])
    connection.commit()
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return path, connection


def openclaw_message(text, role='user'):
    return {'type': 'message', 'message': {'role': role, 'content': text}}


def dsh_rows(text):
    return [
        {'type': 'session', 'id': 'session-' + SID, 'version': 4, 'cwd': '/fixture/project'},
        {'type': 'user/message', 'time': 1793101700000,
         'data': {'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': text}]}},
        {'type': 'tool/call', 'time': 1793101701000,
         'data': {'callId': 'fixture-call', 'name': 'read', 'arguments': {}}},
    ]


class AgentSourceRegressionTest(unittest.TestCase):
    def test_openclaw_wal_update_invalidates_the_cached_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'openclaw', root / 'backup/device-openclaw'
            database, connection = openclaw_database(home, 'main', [openclaw_message('OLD TEXT')])
            try:
                self.assertEqual(export('openclaw', home, destination)['changed_sessions'], 1)
                self.assertEqual(export('openclaw', home, destination)['changed_sessions'], 0)
                before = database.stat()
                connection.execute('UPDATE transcript_events SET event_json=? WHERE session_id=? AND seq=0',
                                   (json.dumps(openclaw_message('NEW TEXT')), SID))
                connection.commit()
                after = database.stat()
                self.assertEqual((before.st_size, before.st_mtime_ns),
                                 (after.st_size, after.st_mtime_ns),
                                 'The fixture must change WAL content without changing the main DB')
                self.assertEqual(export('openclaw', home, destination)['changed_sessions'], 1)
                text = next(destination.glob('sessions/**/*.md')).read_text(encoding='utf-8')
                self.assertIn('NEW TEXT', text)
                self.assertNotIn('OLD TEXT', text)
                self.assertEqual(export('openclaw', home, destination)['changed_sessions'], 0)
            finally:
                connection.close()

    def assert_failed_refresh_preserves_archive(self, compressed, replacement):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'dsh', root / 'backup/device-dsh'
            source = home / 'sessions/project' / ('session-' + SID) / (
                'session.v4.jsonl.zstd' if compressed else 'session.v4.jsonl')
            source.parent.mkdir(parents=True)
            initial = jsonl(dsh_rows('previous valid conversation'))
            source.write_bytes(compress_zstd(initial) if compressed else initial)
            export('dsh', home, destination)
            resources.migrate(destination.parent, ['logs'])
            index = json.loads((destination / 'index.json').read_text(encoding='utf-8'))
            row = next(iter(index['sessions'].values()))
            record = (destination.parent / row['local_record']).parent
            paths = [destination / 'index.json', destination / '.export-cache.json',
                     destination / row['file'], record / 'manifest.json', record / 'dialogue.jsonl',
                     record / 'source.jsonl', record / 'operations.jsonl']
            snapshots = {path: path.read_bytes() for path in paths}
            source.write_bytes(replacement)
            with self.assertRaises((RuntimeError, ValueError)):
                export('dsh', home, destination)
            for path, content in snapshots.items():
                self.assertEqual(path.read_bytes(), content, f'Failed refresh changed {path.name}')
            recovered = jsonl(dsh_rows('recovered conversation'))
            source.write_bytes(compress_zstd(recovered) if compressed else recovered)
            self.assertEqual(export('dsh', home, destination)['changed_sessions'], 1)
            self.assertEqual(export('dsh', home, destination)['changed_sessions'], 0)

    def test_invalid_json_does_not_replace_a_valid_local_snapshot(self):
        self.assert_failed_refresh_preserves_archive(False, b'{bad completed record}\n')

    def test_invalid_compressed_source_does_not_replace_a_valid_local_snapshot(self):
        self.assert_failed_refresh_preserves_archive(True, b'not a zstd frame')

    def assert_failed_publish_preserves_archive(self, failure):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, destination = root / 'dsh', root / 'backup/device-dsh'
            source = home / 'sessions/project' / ('session-' + SID) / 'session.v4.jsonl'
            source.parent.mkdir(parents=True)
            source.write_bytes(jsonl(dsh_rows('previous valid conversation')))
            export('dsh', home, destination)
            resources.migrate(destination.parent, ['logs'])
            record = destination.parent / 'records' / destination.name / SID
            manifest = record / 'manifest.json'
            valid_manifest = manifest.read_bytes()
            changed = dsh_rows('changed conversation')
            if failure == 'new-project-link':
                changed[0]['cwd'] = '/fixture/newproject'
                project = 'newproject-' + hashlib.sha256(b'/fixture/newproject').hexdigest()[:8]
                link = destination / 'sessions' / project
                outside = root / 'outside'
                outside.mkdir()
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest('Directory symlinks unavailable')
            else:
                manifest.write_bytes(b'{damaged previous manifest}\n')
            source.write_bytes(jsonl(changed))

            def snapshot():
                return {path.relative_to(destination.parent): (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in destination.parent.rglob('*') if path.is_file()}

            before = snapshot()
            with self.assertRaises((RuntimeError, ValueError)):
                export('dsh', home, destination)
            self.assertEqual(snapshot(), before, 'A failed publish must retain all prior files and timestamps')
            if failure == 'new-project-link':
                self.assertEqual(list(outside.iterdir()), [])
                link.unlink()
            else:
                manifest.write_bytes(valid_manifest)
            self.assertEqual(export('dsh', home, destination)['changed_sessions'], 1)
            self.assertEqual(export('dsh', home, destination)['changed_sessions'], 0)

    def test_new_project_symlink_does_not_partially_publish_a_snapshot(self):
        self.assert_failed_publish_preserves_archive('new-project-link')

    def test_damaged_previous_manifest_does_not_replace_a_valid_snapshot(self):
        self.assert_failed_publish_preserves_archive('damaged-manifest')

    def test_incomplete_zstd_frame_is_an_explicit_retryable_failure(self):
        prefix = compress_zstd(jsonl(dsh_rows('completed prefix')))
        incomplete = prefix + compress_zstd(jsonl(dsh_rows('incomplete trailing frame')))[:5]
        with self.assertRaises((RuntimeError, ValueError)):
            vault.zstd_decompress(incomplete)
        self.assert_failed_refresh_preserves_archive(True, incomplete)

    def test_openclaw_log_migration_keeps_calls_and_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'openclaw', root / 'backup/device-openclaw'
            events = [openclaw_message('question'),
                      {'type': 'message', 'message': {'role': 'assistant', 'content': [
                          {'type': 'toolCall', 'id': 'call-1', 'name': 'read', 'arguments': {}}]}},
                      {'type': 'message', 'message': {'role': 'toolResult', 'toolCallId': 'call-1',
                          'content': [{'type': 'text', 'text': 'COMPLETE TOOL RESULT'}]}}]
            _, connection = openclaw_database(home, 'main', events)
            connection.close()
            export('openclaw', home, destination)
            result = resources.migrate(destination.parent, ['logs'])
            self.assertEqual(result['operations'], 2)
            index = json.loads((destination / 'index.json').read_text(encoding='utf-8'))
            row = next(iter(index['sessions'].values()))
            logs = (destination.parent / row['local_record']).with_name('operations.jsonl').read_text(encoding='utf-8')
            self.assertIn('toolCall', logs)
            self.assertIn('COMPLETE TOOL RESULT', logs)
            self.assertNotIn('COMPLETE TOOL RESULT', (destination / row['file']).read_text(encoding='utf-8'))

    def test_codex_zst_sources_are_discovered_and_incremental(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'codex', root / 'backup/device-codex'
            source = home / 'sessions' / ('rollout-' + SID + '.jsonl.zst')
            source.parent.mkdir(parents=True)
            def content(text):
                return jsonl([{'type': 'session_meta', 'payload': {'id': SID, 'cwd': '/fixture/project'}},
                              {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                               'content': [{'type': 'input_text', 'text': text}]}}])
            source.write_bytes(compress_zstd(content('first compressed conversation')))
            self.assertIn(source, vault.sources('codex', home))
            self.assertEqual(export('codex', home, destination)['sessions'], 1)
            self.assertEqual(export('codex', home, destination)['changed_sessions'], 0)
            source.write_bytes(compress_zstd(content('appended compressed conversation')))
            self.assertEqual(export('codex', home, destination)['changed_sessions'], 1)
            text = next(destination.glob('sessions/**/*.md')).read_text(encoding='utf-8')
            self.assertIn('appended compressed conversation', text)
            self.assertEqual(export('codex', home, destination)['changed_sessions'], 0)

    def test_openclaw_agents_with_the_same_session_id_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'openclaw', root / 'backup/device-openclaw'
            for agent, text in [('alpha', 'ALPHA CONVERSATION'), ('beta', 'BETA CONVERSATION')]:
                _, connection = openclaw_database(home, agent, [openclaw_message(text)])
                connection.close()
            self.assertEqual(export('openclaw', home, destination)['sessions'], 2)
            rows = json.loads((destination / 'index.json').read_text(encoding='utf-8'))['sessions']
            self.assertEqual(len({row['file'] for row in rows.values()}), 2)
            self.assertEqual(len({row['local_record'] for row in rows.values()}), 2)
            content = '\n'.join((destination / row['file']).read_text(encoding='utf-8') for row in rows.values())
            self.assertIn('ALPHA CONVERSATION', content)
            self.assertIn('BETA CONVERSATION', content)
            for row in rows.values():
                self.assertTrue((destination.parent / row['local_record']).with_name('source.jsonl').is_file())
            self.assertEqual(export('openclaw', home, destination)['changed_sessions'], 0)

    def test_archived_compressed_codex_snapshot_and_logs_survive_a_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, destination = root / 'codex', root / 'backup/device-codex'
            source = home / 'archived_sessions' / ('rollout-' + SID + '.jsonl.zstd')
            source.parent.mkdir(parents=True)
            rows = [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'Archived compressed fixture'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'call-1',
                 'name': 'read', 'arguments': '{}'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-1',
                 'output': 'COMPLETE RESULT'}},
            ]
            original = compress_zstd(jsonl(rows))
            source.write_bytes(original)
            export('codex', home, destination)
            info = json.loads((destination / 'index.json').read_text())['sessions'][SID]
            self.assertTrue(info['archived'])
            snapshot = destination.parent / info['source_copy']
            self.assertEqual(snapshot.name, 'source.jsonl.zst')
            self.assertEqual(snapshot.read_bytes(), original)
            before = snapshot.stat().st_mtime_ns
            self.assertEqual(export('codex', home, destination)['changed_sessions'], 0)
            self.assertEqual(snapshot.stat().st_mtime_ns, before)
            moved = root / 'moved backup'
            shutil.copytree(destination.parent, moved)
            source.unlink()
            shutil.rmtree(destination.parent)
            self.assertEqual(resources.migrate(moved, ['logs'])['operations'], 2)
            logs = (moved / info['local_record']).with_name('operations.jsonl')
            self.assertEqual(logs.read_bytes(), jsonl(rows[1:]))
            self.assertEqual(resources.migrate(moved, ['logs'])['estimated_bytes'], 0)


class SourceReaderTest(unittest.TestCase):
    def assert_complete_zstd_frames(self, decode):
        first = b'first frame\n' * (1 << 18)
        second = '第二帧\n'.encode('utf-8')
        frame, tail = compress_zstd(first), compress_zstd(second)
        self.assertEqual(decode(frame + tail), first + second)
        self.assertEqual(decode(compress_zstd(b'')), b'')
        for invalid in (b'', frame[:5], frame[:-1], frame + tail[:5], frame + b'bad trailer'):
            with self.subTest(length=len(invalid)):
                with self.assertRaises((ValueError, RuntimeError)):
                    decode(invalid)

    def test_libzstd_requires_complete_frames_and_drains_large_output(self):
        library = vault.zstd_library()
        if library is None:
            self.skipTest('libzstd unavailable')
        self.assert_complete_zstd_frames(lambda data: vault.zstd_ctypes(library, data))

    def test_stdlib_zstd_requires_complete_frames(self):
        try:
            from compression import zstd  # noqa: F401
        except ImportError:
            self.skipTest('Python stdlib zstd unavailable')
        self.assert_complete_zstd_frames(vault.zstd_decompress)

    def test_python_zstandard_requires_complete_frames(self):
        try:
            import zstandard  # noqa: F401
        except ImportError:
            self.skipTest('Optional Python zstandard unavailable')
        with mock.patch.dict(sys.modules, {'compression': None}):
            self.assert_complete_zstd_frames(vault.zstd_decompress)

    def test_zstd_cli_requires_complete_frames(self):
        if shutil.which('zstd') is None:
            self.skipTest('zstd CLI unavailable')
        # Build the real compressed fixtures before disabling library discovery.
        first, second = compress_zstd(b'first\n'), compress_zstd(b'second\n')
        with mock.patch.dict(sys.modules, {'compression': None, 'zstandard': None}), \
                mock.patch.object(vault, 'zstd_library', return_value=None):
            self.assertEqual(vault.zstd_decompress(first + second), b'first\nsecond\n')
            with self.assertRaises((ValueError, RuntimeError)):
                vault.zstd_decompress(first + second[:5])

    def test_libzstd_null_allocation_and_no_progress_are_errors(self):
        library = mock.Mock()
        library.ZSTD_createDStream.return_value = None
        with self.assertRaisesRegex(RuntimeError, '分配'):
            vault.zstd_ctypes(library, b'input')
        library.ZSTD_initDStream.assert_not_called()
        library.ZSTD_createDStream.return_value = 1
        library.ZSTD_initDStream.return_value = 0
        library.ZSTD_isError.return_value = 0
        library.ZSTD_decompressStream.return_value = 1
        with self.assertRaisesRegex(RuntimeError, '没有进展'):
            vault.zstd_ctypes(library, b'input')
        library.ZSTD_freeDStream.assert_called_once_with(1)

    def test_readonly_database_keeps_one_wal_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, writer = openclaw_database(Path(temporary), 'main', [openclaw_message('before')])
            try:
                with vault.readonly_database(path) as reader:
                    self.assertTrue(reader.in_transaction)
                    self.assertEqual(reader.execute('PRAGMA query_only').fetchone()[0], 1)
                    before = reader.execute('SELECT event_json FROM transcript_events').fetchone()[0]
                    writer.execute('UPDATE transcript_events SET event_json=?',
                                   (json.dumps(openclaw_message('after')),))
                    writer.commit()
                    self.assertEqual(reader.execute('SELECT event_json FROM transcript_events').fetchone()[0], before)
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.execute('DELETE FROM transcript_events')
                with vault.readonly_database(path) as reader:
                    self.assertIn('after', reader.execute('SELECT event_json FROM transcript_events').fetchone()[0])
            finally:
                writer.close()

    def assert_snapshot_copy_retries(self, continuous_changes):
        with tempfile.TemporaryDirectory() as temporary:
            path, writer = openclaw_database(Path(temporary), 'main', [openclaw_message('before')])
            connect, copy = sqlite3.connect, shutil.copy2
            copies, snapshots = [], []

            def readonly_open_fails(database, *args, **kwargs):
                if kwargs.get('uri'):
                    raise sqlite3.OperationalError('unable to open database file')
                snapshots.append(Path(database))
                return connect(database, *args, **kwargs)

            def copy_during_write(source, target, *args, **kwargs):
                result = copy(source, target, *args, **kwargs)
                if Path(source) == path:
                    copies.append(target)
                    if continuous_changes or len(copies) == 1:
                        writer.execute('UPDATE transcript_events SET event_json=?',
                                       (json.dumps(openclaw_message('revision ' + str(len(copies)))),))
                        writer.commit()
                return result

            try:
                with mock.patch.object(vault.sqlite3, 'connect', side_effect=readonly_open_fails), \
                        mock.patch.object(vault.shutil, 'copy2', side_effect=copy_during_write):
                    if continuous_changes:
                        with self.assertRaisesRegex(RuntimeError, '持续变化'):
                            with vault.readonly_database(path):
                                self.fail('An unstable snapshot was yielded')
                        self.assertEqual(len(copies), 3)
                    else:
                        with vault.readonly_database(path) as reader:
                            self.assertTrue(reader.in_transaction)
                            self.assertEqual(reader.execute('PRAGMA query_only').fetchone()[0], 1)
                            self.assertIn('revision 1', reader.execute(
                                'SELECT event_json FROM transcript_events').fetchone()[0])
                            with self.assertRaises(sqlite3.OperationalError):
                                reader.execute('DELETE FROM transcript_events')
                        self.assertEqual(len(copies), 2)
                for snapshot in copies + snapshots:
                    self.assertFalse(snapshot.parent.exists(), 'Temporary snapshots must be cleaned up')
            finally:
                writer.close()

    def test_readonly_snapshot_retries_a_real_wal_change(self):
        self.assert_snapshot_copy_retries(False)

    def test_readonly_snapshot_stops_after_three_unstable_copies(self):
        self.assert_snapshot_copy_retries(True)

    def test_corrupt_database_is_not_retried_as_an_open_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'corrupt.sqlite'
            path.write_bytes(b'not a database')
            with mock.patch.object(vault.shutil, 'copy2') as copy:
                with self.assertRaises(sqlite3.DatabaseError):
                    with vault.readonly_database(path):
                        self.fail('A corrupt database was yielded')
                copy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
