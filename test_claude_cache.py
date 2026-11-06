import gzip
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

import claude_cache


class ClaudeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'Claude'
        self.cache = self.home / 'Cache' / 'Cache_Data'
        self.cache.mkdir(parents=True)

    def write_entry(self, body=b'data: {"type":"message"}\n\n', encoding='gzip',
                    path='/v1/code/channels/channel-test/messages/stream',
                    host='claude.ai', status=200, media_type='text/event-stream',
                    truncated=False, encoded=None, extra=6, mutate=None,
                    response_time=13438320000000123):
        key = ('1/0/https://' + host + path + '?cursor=test-private-query').encode()
        raw_headers = (f'HTTP/1.1 {status} OK\0content-type:{media_type}\0'
                       f'content-encoding:{encoding}\0'
                       'set-cookie:private-cookie-value\0\0').encode()
        flags = 3 | (1 << 31) | (4096 if truncated else 0)
        payload = struct.pack('<IIqq', flags, extra, response_time - 1000, response_time)
        if extra & 4:
            payload += struct.pack('<q', 0)
        payload += struct.pack('<I', len(raw_headers)) + raw_headers
        payload += b'\0' * (-len(payload) % 4)
        metadata = struct.pack('<I', len(payload)) + payload
        if encoded is None:
            encoded = gzip.compress(body) if encoding == 'gzip' else body
        header = struct.pack('<QIIII', claude_cache._INITIAL_MAGIC, 5, len(key), 0, 0)
        eof1 = struct.pack('<QIIII', claude_cache._FINAL_MAGIC, 1,
                           zlib.crc32(encoded) & 0xffffffff, 0, 0)
        eof0 = struct.pack('<QIIII', claude_cache._FINAL_MAGIC, 3,
                           zlib.crc32(metadata) & 0xffffffff, len(metadata), 0)
        data = header + key + encoded + eof1 + metadata + hashlib.sha256(key).digest() + eof0
        if mutate:
            data = mutate(bytearray(data), 24 + len(key), len(encoded), len(metadata))
        filename = f'{int.from_bytes(hashlib.sha1(key).digest()[:8], "little"):016x}_0'
        entry = self.cache / filename
        entry.write_bytes(data)
        return entry

    def read(self):
        warnings = []
        return list(claude_cache.iter_responses(self.home, warnings)), warnings

    def assertSkipped(self):
        rows, warnings = self.read()
        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertNotIn('private', warnings[0])
        self.assertNotIn('channel-test', warnings[0])
        return warnings[0]

    def test_valid_gzip_response_excludes_headers_and_query(self):
        body = 'data: {"text":"你好"}\n\n'.encode()
        path = self.write_entry(body=body)
        rows, warnings = self.read()
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['body'], body)
        self.assertEqual(row['path'], path)
        self.assertEqual(row['url'], 'https://claude.ai/v1/code/channels/channel-test/messages/stream')
        self.assertEqual(row['content_type'], 'text/event-stream')
        self.assertEqual(row['signature'][-1], hashlib.sha256(body).hexdigest())
        self.assertNotIn('private', repr(row))
        self.assertEqual(self.read()[0][0]['signature'], row['signature'])

    def test_response_time_survives_file_touch(self):
        response_time = 13438320000000123
        path = self.write_entry(response_time=response_time)
        before = self.read()[0][0]
        source_stat = path.stat()
        os.utime(path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 5000000000))
        after = self.read()[0][0]
        self.assertNotEqual(before['mtime_ns'], after['mtime_ns'])
        self.assertEqual(before['response_time'], response_time)
        self.assertEqual(after['response_time'], response_time)
        self.assertEqual(before['body'], after['body'])

    def test_allowlist_limits_origin_and_message_routes(self):
        prefix = '1/0/https://claude.ai/v1/code/channels/id/'
        for route in ('timeline', 'threads', 'threads/t', 'messages',
                      'messages/stream', 'threads/t/messages', 'threads/t/messages/stream'):
            with self.subTest(route=route):
                self.assertIsNotNone(claude_cache._message_url((prefix + route).encode()))
        for key in ('https://other.example/v1/code/channels/id/messages/stream',
                    'https://other.example/?redirect=/https://claude.ai/v1/code/channels/id/messages',
                    'https://claude.ai/settings?redirect=/https://claude.ai/v1/code/channels/id/messages',
                    'https://claude.ai/credentials',
                    prefix + 'settings', prefix + 'messages/stream/credentials',
                    prefix + '../messages', prefix + 'threads/t/settings',
                    'https://user:secret@claude.ai/v1/code/channels/id/messages',
                    'http://claude.ai/v1/code/channels/id/messages',
                    'https://claude.ai:8443/v1/code/channels/id/messages',
                    prefix + 'messages\n'):
            with self.subTest(key=key):
                self.assertIsNone(claude_cache._message_url(key.encode()))

    def test_unrelated_response_body_is_not_read(self):
        path = self.write_entry(path='/api/credentials', encoded=b'private' * 1000)
        lengths = []
        original_fdopen = claude_cache.os.fdopen

        class ReadCounter:
            def __init__(self, source):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.source.close()

            def fileno(self):
                return self.source.fileno()

            def read(self, length):
                lengths.append(length)
                return self.source.read(length)

        with mock.patch('claude_cache.os.fdopen', side_effect=lambda *a: ReadCounter(original_fdopen(*a))):
            self.assertEqual(self.read(), ([], []))
        self.assertEqual(lengths[0], 24)
        self.assertEqual(len(lengths), 2)
        self.assertLess(sum(lengths), path.stat().st_size)

    def test_identity_and_deflate(self):
        body = b'{"messages":[]}'
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(body) + compressor.flush()
        for encoding, encoded in [('identity', body), ('deflate', zlib.compress(body)), ('deflate', raw)]:
            with self.subTest(encoding=encoding, raw=encoded == raw):
                self.write_entry(body=body, encoding=encoding, encoded=encoded, media_type='application/json')
                rows, warnings = self.read()
                self.assertEqual(warnings, [])
                self.assertEqual(rows[0]['body'], body)

    def test_gzip_members_are_all_validated(self):
        self.write_entry(encoded=gzip.compress(b'one') + gzip.compress(b'two'))
        self.assertEqual(self.read()[0][0]['body'], b'onetwo')
        self.write_entry(encoded=gzip.compress(b'one') + gzip.compress(b'two')[:-4])
        self.assertIn('incomplete', self.assertSkipped())
        self.write_entry(encoded=gzip.compress(b'one') + b'unrelated trailing bytes')
        self.assertIn('invalid', self.assertSkipped())

    def test_truncated_and_corrupt_gzip_do_not_yield_partial_text(self):
        self.write_entry(encoded=gzip.compress(b'message')[:-1])
        self.assertIn('incomplete', self.assertSkipped())
        encoded = bytearray(gzip.compress(b'message'))
        encoded[-8] ^= 1
        self.write_entry(encoded=encoded)
        self.assertIn('checksum', self.assertSkipped())

    def test_stream_checksums_and_boundaries(self):
        def corrupt(offset):
            def mutate(data, body_start, body_size, metadata_size):
                index = offset(body_start, body_size, metadata_size)
                data[index] ^= 1
                return data
            return mutate

        changes = {
            'body': lambda start, size, meta: start,
            'body trailer': lambda start, size, meta: start + size,
            'metadata': lambda start, size, meta: start + size + 24,
            'key digest': lambda start, size, meta: -25,
            'final trailer': lambda start, size, meta: -24,
            'stream length': lambda start, size, meta: -5,
        }
        for label, offset in changes.items():
            with self.subTest(label=label):
                self.write_entry(mutate=corrupt(offset))
                self.assertSkipped()

    def test_file_truncation_and_key_mismatch(self):
        path = self.write_entry()
        path.write_bytes(path.read_bytes()[:-10])
        self.assertSkipped()
        path = self.write_entry()
        path.rename(path.with_name('0000000000000000_0'))
        self.assertIn('key checksum', self.assertSkipped())

    def test_invalid_response_metadata_and_unsupported_encoding(self):
        for kwargs in ({'status': 403}, {'truncated': True}, {'extra': 22},
                       {'media_type': 'text/html'}, {'encoding': 'br'}):
            with self.subTest(kwargs=kwargs):
                self.write_entry(**kwargs)
                self.assertSkipped()

    def test_size_limits(self):
        self.write_entry(body=b'x' * 1000)
        with mock.patch('claude_cache.MAX_BODY_BYTES', 100):
            self.assertIn('size limit', self.assertSkipped())
        with mock.patch('claude_cache.MAX_ENTRY_BYTES', 64):
            self.assertIn('size limit', self.assertSkipped())
        with mock.patch('claude_cache.MAX_METADATA_BYTES', 1):
            self.assertIn('size limit', self.assertSkipped())

    def test_source_changed_while_reading_is_skipped(self):
        path = self.write_entry()
        original_lstat = Path.lstat
        calls = 0

        def changing_lstat(candidate, *args, **kwargs):
            nonlocal calls
            value = original_lstat(candidate, *args, **kwargs)
            if candidate == path:
                calls += 1
                if calls > 1:
                    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')
                    values = {field: getattr(value, field) for field in fields}
                    values['st_mtime_ns'] += 1
                    return SimpleNamespace(**values)
            return value

        with mock.patch.object(Path, 'lstat', changing_lstat):
            self.assertIn('changed', self.assertSkipped())

    def test_symlink_entry_is_not_read(self):
        path = self.write_entry()
        outside = self.home.parent / 'outside'
        path.rename(outside)
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertEqual(self.read(), ([], []))

    def test_missing_cache_is_empty(self):
        warnings = []
        self.assertEqual(list(claude_cache.iter_responses(self.home / 'missing', warnings)), [])
        self.assertEqual(warnings, [])


if __name__ == '__main__':
    unittest.main()
