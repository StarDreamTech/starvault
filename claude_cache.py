"""Read only Claude Code Projects message responses from Chromium Simple Cache.

The file layout follows Chromium's net/disk_cache/simple/simple_entry_format.h
and net/http/http_response_info.cc. Only a candidate's bounded key is inspected
before applying the URL allowlist; HTTP metadata is never returned or copied.
The reader deliberately skips incomplete entries instead of recovering arbitrary
gzip-looking bytes from cache files. No credentials or unrelated caches are read.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
from urllib.parse import urlsplit, urlunsplit
import zlib


MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_KEY_BYTES = 32 * 1024
MAX_METADATA_BYTES = 1024 * 1024
_HEADER = struct.Struct('<QIIII')
_INITIAL_MAGIC = 0xfcfb6d1ba7725c30
_FINAL_MAGIC = 0xf4fa6f45970d41d8
_ENTRY_NAME = re.compile(r'[0-9a-f]{16}_0\Z')
_MESSAGE_PATH = re.compile(
    r'/v1/code/channels/[A-Za-z0-9_-]+/'
    r'(?:timeline|messages(?:/stream)?|threads(?:/[A-Za-z0-9_-]+'
    r'(?:/messages(?:/stream)?)?)?)/?\Z')


class CacheReadError(ValueError):
    """An incomplete, unsupported or invalid candidate; safe to retry later."""


def _message_url(key):
    try:
        value = key.decode('utf-8')
        # Chromium prefixes its URL with the upload/partition cache key.
        start = value.find('https://')
        if start < 0 or not re.fullmatch(r'(?:[0-9]+/)*', value[:start]):
            return None
        url = urlsplit(value[start:])
        if (url.scheme != 'https' or url.hostname != 'claude.ai'
                or url.username is not None or url.password is not None
                or url.port not in (None, 443) or not _MESSAGE_PATH.fullmatch(url.path)
                or any(ord(char) < 32 for char in value)):
            return None
        # Query parameters and cache-key prefixes are not conversation data.
        return urlunsplit(('https', 'claude.ai', url.path.rstrip('/'), '', ''))
    except (ValueError, UnicodeError):
        return None


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _regular(value):
    return (stat.S_ISREG(value.st_mode)
            and not getattr(value, 'st_file_attributes', 0) & 0x400)


def _streams(data, key):
    """Check file0's body/EOF1/metadata/[key SHA256]/EOF0 boundaries."""
    if len(data) < _HEADER.size * 3 + len(key):
        raise CacheReadError('incomplete cache entry')
    magic, flags, checksum, metadata_size, _ = _HEADER.unpack_from(data, len(data) - 24)
    if magic != _FINAL_MAGIC or flags & ~3:
        raise CacheReadError('missing or invalid final cache trailer')
    if metadata_size > MAX_METADATA_BYTES:
        raise CacheReadError('response metadata exceeds size limit')
    key_digest_size = 32 if flags & 2 else 0
    metadata_end = len(data) - 24 - key_digest_size
    metadata_start = metadata_end - metadata_size
    body_start = 24 + len(key)
    body_end = metadata_start - 24
    if body_end < body_start:
        raise CacheReadError('invalid cache stream boundaries')
    if key_digest_size and data[metadata_end:metadata_end + 32] != hashlib.sha256(key).digest():
        raise CacheReadError('cache key checksum mismatch')
    metadata = data[metadata_start:metadata_end]
    if not flags & 1 or zlib.crc32(metadata) & 0xffffffff != checksum:
        raise CacheReadError('missing or invalid response metadata checksum')
    magic, flags, checksum, _, _ = _HEADER.unpack_from(data, body_end)
    if magic != _FINAL_MAGIC or flags & ~1:
        raise CacheReadError('invalid response body trailer')
    body = data[body_start:body_end]
    if not flags & 1 or zlib.crc32(body) & 0xffffffff != checksum:
        raise CacheReadError('missing or invalid response body checksum')
    return metadata, body


def _response_info(metadata):
    # base::Pickle has a four-byte payload length, then aligned integers and
    # strings. HttpResponseInfo v3 puts flags/timestamps before raw_headers.
    if len(metadata) < 28:
        raise CacheReadError('incomplete response metadata')
    payload_size, flags = struct.unpack_from('<II', metadata)
    if payload_size != len(metadata) - 4 or flags & 255 != 3:
        raise CacheReadError('unsupported or invalid response metadata')
    if flags & (1 << 12):
        raise CacheReadError('response is marked truncated')
    offset, extra = 8, 0
    if flags & (1 << 31):
        extra = struct.unpack_from('<I', metadata, offset)[0]
        offset += 4
    if extra & (1 << 4):
        raise CacheReadError('unsupported on-disk response compression')
    if offset + 16 > len(metadata):
        raise CacheReadError('incomplete response timestamps')
    response_time = struct.unpack_from('<q', metadata, offset + 8)[0]
    offset += 16 + (8 if extra & 4 else 0)
    if offset + 4 > len(metadata):
        raise CacheReadError('incomplete response headers')
    length = struct.unpack_from('<I', metadata, offset)[0]
    offset += 4
    if not length or offset + length > len(metadata):
        raise CacheReadError('invalid response header boundaries')
    raw = metadata[offset:offset + length]
    if not raw.endswith(b'\0\0'):
        raise CacheReadError('incomplete response headers')
    lines = raw.split(b'\0')
    if not re.fullmatch(rb'HTTP/\d(?:\.\d)? 200(?: [^\r\n]*)?', lines[0]):
        raise CacheReadError('cached response is not HTTP 200')
    # Do not retain cookies, authentication, request headers or response fields.
    encoding, media_type = [], []
    for line in lines[1:]:
        name, separator, value = line.partition(b':')
        if separator and name.lower() == b'content-encoding':
            encoding.append(value.strip().lower())
        elif separator and name.lower() == b'content-type':
            media_type.append(value.split(b';', 1)[0].strip().lower())
    if len(encoding) > 1 or len(media_type) > 1:
        raise CacheReadError('ambiguous response encoding or content type')
    kind = media_type[0] if media_type else b''
    if kind not in (b'', b'application/json', b'text/event-stream'):
        raise CacheReadError('unsupported message response content type')
    return encoding[0] if encoding else b'identity', kind.decode('ascii'), response_time


def _inflate(body, window_bits):
    chunks, total, remaining = [], 0, body
    while remaining:
        decoder = zlib.decompressobj(window_bits)
        chunk = decoder.decompress(remaining, MAX_BODY_BYTES - total + 1)
        total += len(chunk)
        if total > MAX_BODY_BYTES or decoder.unconsumed_tail:
            raise CacheReadError('decoded message body exceeds size limit')
        if not decoder.eof:
            raise CacheReadError('compressed response is incomplete')
        chunks.append(chunk)
        remaining = decoder.unused_data
        if remaining and window_bits != 16 + zlib.MAX_WBITS:
            raise CacheReadError('unexpected data after compressed response')
    if not chunks:
        raise CacheReadError('compressed response is empty')
    return b''.join(chunks)


def _decode_body(body, encoding):
    try:
        if encoding in (b'gzip', b'x-gzip'):
            return _inflate(body, 16 + zlib.MAX_WBITS)
        if encoding == b'deflate':
            try:
                return _inflate(body, zlib.MAX_WBITS)
            except zlib.error:
                return _inflate(body, -zlib.MAX_WBITS)
        if encoding not in (b'', b'identity'):
            raise CacheReadError('unsupported response content encoding')
        if len(body) > MAX_BODY_BYTES:
            raise CacheReadError('message body exceeds size limit')
        return body
    except zlib.error as error:
        raise CacheReadError('compressed response checksum or format is invalid') from error


def _read_response(path):
    before = path.lstat()
    if not _regular(before):
        return None
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        if _stamp(os.fstat(source.fileno())) != _stamp(before):
            raise CacheReadError('cache entry changed before reading: ' + repr((_stamp(before), _stamp(os.fstat(source.fileno())))))
        header = source.read(24)
        if len(header) != 24:
            return None
        magic, version, key_size, _, _ = _HEADER.unpack(header)
        if magic != _INITIAL_MAGIC or key_size > MAX_KEY_BYTES:
            return None
        key = source.read(key_size)
        if len(key) != key_size:
            return None
        url = _message_url(key)
        if url is None:
            return None
        if version != 5:
            raise CacheReadError('unsupported Simple Cache entry version')
        if before.st_size > MAX_ENTRY_BYTES:
            raise CacheReadError('cache entry exceeds size limit')
        # Chromium's filename is the first eight bytes of SHA1(key), little
        # endian. This also validates keys in entries without a SHA256 trailer.
        key_hash = int.from_bytes(hashlib.sha1(key).digest()[:8], 'little')
        if path.name != f'{key_hash:016x}_0':
            raise CacheReadError('cache filename does not match key checksum')
        data = header + key + source.read(MAX_ENTRY_BYTES - len(header) - len(key) + 1)
        if (len(data) != before.st_size
                or _stamp(os.fstat(source.fileno())) != _stamp(before)
                or _stamp(path.lstat()) != _stamp(before)):
            raise CacheReadError('cache entry changed while reading')
    metadata, body = _streams(data, key)
    encoding, media_type, response_time = _response_info(metadata)
    body = _decode_body(body, encoding)
    return {'path': path, 'url': url, 'body': body, 'content_type': media_type,
            'mtime_ns': before.st_mtime_ns, 'response_time': response_time,
            'signature': [before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                          hashlib.sha256(body).hexdigest()]}


def iter_responses(home, warnings=None):
    """Yield verified message responses; report retryable skips in ``warnings``.

    ``home`` is one Claude/Claude-3p product directory. Entries are read in place,
    never copied or modified. Callers must preserve old conversation snapshots
    when a response is unavailable and parse only complete message events.
    Returned URLs contain neither query parameters nor cache-key prefixes.
    ``response_time`` is Chromium's stored microseconds since 1601-01-01 UTC;
    unlike filesystem mtime it remains unchanged when an entry is copied/touched.
    """
    warnings = warnings if warnings is not None else []
    home = Path(home).expanduser()
    root = home / 'Cache' / 'Cache_Data'
    try:
        if not root.is_dir():
            return
        root.resolve().relative_to(home.resolve())
        paths = sorted(root.iterdir())
    except (OSError, ValueError):
        warnings.append('Claude message cache directory is unavailable or outside its product directory')
        return
    for path in paths:
        if not _ENTRY_NAME.fullmatch(path.name):
            continue
        try:
            response = _read_response(path)
            if response is not None:
                yield response
        except (OSError, CacheReadError, struct.error) as error:
            # OS messages can contain paths; do not include arbitrary exception
            # text from the filesystem, keys, headers, or compressed content.
            reason = str(error) if isinstance(error, CacheReadError) else type(error).__name__
            warnings.append(f'Claude message cache {path.name}: {reason}; retry on next scan')
