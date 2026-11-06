"""Projects cache integration: synthetic SSE only, never a real app export."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vault


CHANNEL = '11111111-1111-4111-8111-111111111111'
OTHER_CHANNEL = '22222222-2222-4222-8222-222222222222'
ROOT = 'cmsg_' + '1' * 46
ANSWER = 'cmsg_' + '2' * 46


def message(mid=ROOT, body='Fixture question', role='user', seq='1', **fields):
    row = {'id': mid, 'channel_id': CHANNEL, 'thread_root_id': ROOT,
           'seq': seq, 'created_at': '2026-11-01T12:00:00Z', 'body': body}
    if role == 'user':
        row['author_account_id'] = 'fixture-account'
    elif role == 'assistant':
        row['author_agent_id'] = 'fixture-agent'
        row['author_session_id'] = 'fixture-session'
    row.update(fields)
    return row


def event(row, kind='channel_message'):
    return ('event: ' + kind + '\ndata: ' + json.dumps(row) + '\n\n').encode('utf-8')


def response(home, data, name='fixture_0', mtime=100, channel=CHANNEL, response_time=None):
    value = {'path': home / 'Cache/Cache_Data' / name,
             'url': 'https://claude.ai/v1/code/channels/' + channel + '/messages/stream',
             'body': data, 'signature': [name, mtime], 'mtime_ns': mtime}
    if response_time is not None:
        value['response_time'] = response_time
    return value


class ClaudeProjectsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'Claude'
        self.home.mkdir()
        self.dest = self.root / 'backup/device-claude-desktop'
        self.responses = {self.home: []}
        self.reader = patch('claude_cache.iter_responses', side_effect=self.read_responses)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def read_responses(self, home, warnings=None):
        return iter(self.responses.get(Path(home), []))

    def export(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return vault.export('claude-desktop', self.home, self.dest)

    def sessions(self):
        return json.loads((self.dest / 'index.json').read_text(encoding='utf-8'))['sessions']

    def snapshot(self, row):
        return self.dest.parent / 'records' / self.dest.name / row['id'] / 'source.jsonl'

    def markdown(self, row):
        return (self.dest / row['file']).read_text(encoding='utf-8')

    def files(self):
        return {path.relative_to(self.dest.parent).as_posix():
                (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.dest.parent.rglob('*') if path.is_file()}

    def test_overlap_deduplicates_and_repeat_does_not_rewrite(self):
        question = message(attachments=[{'file_id': 'file-fixture', 'filename': 'chart.png',
                                         'mime_type': 'image/png', 'size_bytes': 128}])
        answer = message(ANSWER, 'Fixture complete answer', 'assistant', '2')
        self.responses[self.home] = [
            response(self.home, event(question) + event(answer)),
            response(self.home, event(question) + event(answer), name='overlap_0', mtime=200),
        ]
        result = self.export()
        self.assertEqual(result['changed_sessions'], 1)
        row, = self.sessions().values()
        self.assertEqual(row['messages'], 2)
        self.assertEqual(row['source'], 'code-projects-cache')
        self.assertEqual(row['originator'], 'Claude Code Projects')
        self.assertEqual(row['coverage'], 'partial_cache')
        body = self.markdown(row).split('\n## ', 1)[1]
        self.assertEqual(body.count('Fixture question'), 1)
        self.assertEqual(body.count('Fixture complete answer'), 1)
        snapshot = self.snapshot(row).read_text(encoding='utf-8')
        self.assertIn('file-fixture', snapshot)
        self.assertIn('chart.png', snapshot)
        original = self.files()
        self.assertEqual(self.export()['changed_sessions'], 0)
        self.assertEqual(self.files(), original)

    def test_new_cache_window_and_eviction_keep_old_messages(self):
        self.responses[self.home] = [response(self.home, event(message()))]
        self.export()
        row, = self.sessions().values()
        original_sid = row['id']
        answer = message(ANSWER, 'Later answer only in new window', 'assistant', '2')
        self.responses[self.home] = [response(self.home, event(answer), name='later_0', mtime=300)]
        self.assertEqual(self.export()['changed_sessions'], 1)
        row, = self.sessions().values()
        self.assertEqual(row['id'], original_sid)
        self.assertEqual(row['messages'], 2)
        self.assertIn('Fixture question', self.markdown(row))
        self.assertIn('Later answer only in new window', self.markdown(row))
        original = self.files()
        self.responses[self.home] = []
        self.assertEqual(self.export()['changed_sessions'], 0)
        self.assertEqual(self.files(), original)

    def test_edit_version_wins_over_newer_cache_file_with_old_content(self):
        old = message(body='Original question before edit')
        new = message(body='Revised question after edit', edited_at='2026-11-02T12:00:00Z')
        self.responses[self.home] = [response(self.home, event(old))]
        self.export()
        self.responses[self.home] = [response(self.home, event(new, 'channel_message_updated'),
                                              name='edit_0', mtime=300)]
        self.export()
        row, = self.sessions().values()
        self.assertIn('Revised question after edit', self.markdown(row))
        self.assertNotIn('Original question before edit', self.markdown(row))
        snapshot = self.snapshot(row).read_text(encoding='utf-8')
        self.assertIn('Original question before edit', snapshot)
        self.assertIn('Revised question after edit', snapshot)
        self.responses[self.home] = [response(self.home, event(old), name='stale_0', mtime=900)]
        self.export()
        row, = self.sessions().values()
        self.assertIn('Revised question after edit', self.markdown(row))
        self.assertNotIn('Original question before edit', self.markdown(row))

    def test_http_response_time_orders_metadata_independently_of_file_mtime(self):
        working = message(thread_status='working')
        resolved = message(thread_status='resolved')
        self.responses[self.home] = [
            response(self.home, event(working), name='working_0', mtime=900, response_time=1000),
            response(self.home, event(resolved, 'channel_message_updated'),
                     name='resolved_0', mtime=100, response_time=2000),
        ]
        first, = vault.session_items('claude-desktop', self.home)
        directory = self.root / 'first-read'
        directory.mkdir()
        before = first['prepare'](directory).read_bytes()
        row, = map(json.loads, before.splitlines())
        self.assertEqual(row['cache_revisions'][-1]['message']['thread_status'], 'resolved')
        self.responses[self.home] = [
            response(self.home, event(resolved, 'channel_message_updated'),
                     name='resolved_0', mtime=1, response_time=2000),
            response(self.home, event(working), name='working_0', mtime=10**18, response_time=1000),
        ]
        second, = vault.session_items('claude-desktop', self.home)
        directory = self.root / 'second-read'
        directory.mkdir()
        after = second['prepare'](directory).read_bytes()
        self.assertEqual(second['signature'], first['signature'])
        self.assertEqual(after, before)

    def test_reobserving_archived_old_metadata_does_not_rewrite_or_regress(self):
        working = message(thread_status='working')
        resolved = message(thread_status='resolved')
        self.responses[self.home] = [
            response(self.home, event(working), name='working_0', mtime=100, response_time=1000),
            response(self.home, event(resolved, 'channel_message_updated'),
                     name='resolved_0', mtime=200, response_time=2000),
        ]
        self.assertEqual(self.export()['changed_sessions'], 1)
        row, = self.sessions().values()
        archived, = map(json.loads, self.snapshot(row).read_text(encoding='utf-8').splitlines())
        self.assertEqual(archived['cache_revisions'][-1]['message']['thread_status'], 'resolved')
        before = self.files()
        # An old payload can reappear in a later response as well as a touched file.
        self.responses[self.home] = [
            response(self.home, event(working), name='reobserved_0', mtime=10**18, response_time=3000),
        ]
        self.assertEqual(self.export()['changed_sessions'], 0)
        self.assertEqual(self.files(), before)

    def test_bad_completed_event_cannot_replace_good_snapshot(self):
        self.responses[self.home] = [response(self.home, event(message()))]
        self.export()
        original = self.files()
        unsafe_prefix = event(message(ANSWER, 'Must not publish corrupt response prefix', 'assistant', '2'))
        self.responses[self.home] = [response(self.home, unsafe_prefix + b'event: channel_message\ndata: {bad json}\n\n',
                                              name='bad_0', mtime=400)]
        result = self.export()
        self.assertEqual(result['changed_sessions'], 0)
        self.assertTrue(result.get('warnings'))
        self.assertEqual(self.files(), original)

    def test_incomplete_tail_and_progress_are_not_final_messages(self):
        complete = event(message())
        progress = event({'session_id': 'fixture-session', 'thread_root_id': ROOT,
                          'text': 'Growing partial answer'}, 'session_partial_reply')
        tail = event(message(ANSWER, 'Unfinished tail answer', 'assistant', '2'))[:-1]
        self.responses[self.home] = [response(self.home, complete + progress + tail)]
        result = self.export()
        row, = self.sessions().values()
        self.assertEqual(row['messages'], 1)
        self.assertIn('Fixture question', self.markdown(row))
        self.assertNotIn('Growing partial answer', self.markdown(row))
        self.assertNotIn('Unfinished tail answer', self.markdown(row))
        self.assertTrue(result.get('warnings'))

    def test_notices_do_not_impersonate_users_or_assistant_answers(self):
        notice = message(ANSWER, 'Fixture server lifecycle notice', 'assistant', '2', server_notice=True)
        self.responses[self.home] = [response(self.home, event(message()) + event(notice))]
        self.export()
        row, = self.sessions().values()
        self.assertEqual(row['messages'], 1)
        self.assertNotIn('Fixture server lifecycle notice', self.markdown(row))
        self.assertIn('Fixture server lifecycle notice', self.snapshot(row).read_text(encoding='utf-8'))

    def test_helper_author_without_session_id_is_still_an_assistant(self):
        helper = message(ANSWER, 'Fixture helper answer', None, '2', author={'kind': 'helper'})
        self.responses[self.home] = [response(self.home, event(message()) + event(helper))]
        self.export()
        row, = self.sessions().values()
        self.assertEqual(row['messages'], 2)
        self.assertIn('Fixture helper answer', self.markdown(row))

    def test_product_channel_and_thread_identity_do_not_collide(self):
        sibling = self.home.with_name('Claude-3p')
        sibling.mkdir()
        other_thread = 'cmsg_' + '3' * 46
        self.responses[self.home] = [
            response(self.home, event(message())),
            response(self.home, event(message(channel_id=OTHER_CHANNEL)),
                     name='channel_0', channel=OTHER_CHANNEL),
            response(self.home, event(message(other_thread, thread_root_id=other_thread)),
                     name='thread_0'),
        ]
        self.responses[sibling] = [response(sibling, event(message()))]
        self.assertEqual(self.export()['changed_sessions'], 4)
        self.assertEqual(len(self.sessions()), 4)
        self.assertTrue(all(row['messages'] == 1 for row in self.sessions().values()))

    def test_snapshot_symlink_is_rejected_without_touching_external_data(self):
        self.responses[self.home] = [response(self.home, event(message()))]
        self.export()
        row, = self.sessions().values()
        source = self.snapshot(row)
        outside = self.root / 'outside.jsonl'
        outside.write_bytes(source.read_bytes())
        external_before = outside.read_bytes()
        source.unlink()
        source.symlink_to(outside)
        answer = message(ANSWER, 'New answer must not escape archive', 'assistant', '2')
        self.responses[self.home] = [response(self.home, event(answer), mtime=200)]
        with self.assertRaises((ValueError, RuntimeError)):
            self.export()
        self.assertEqual(outside.read_bytes(), external_before)
        self.assertNotIn('New answer must not escape archive', self.markdown(row))

    def test_missing_existing_snapshot_cannot_shrink_archive_to_new_window(self):
        self.responses[self.home] = [response(self.home, event(message()))]
        self.export()
        row, = self.sessions().values()
        before = self.markdown(row)
        self.snapshot(row).unlink()
        answer = message(ANSWER, 'Only answer remains in live cache', 'assistant', '2')
        self.responses[self.home] = [response(self.home, event(answer), mtime=200)]
        with self.assertRaises((ValueError, RuntimeError)):
            self.export()
        self.assertEqual(self.markdown(row), before)
        self.assertFalse(self.snapshot(row).exists())

    def test_empty_existing_snapshot_cannot_shrink_archive_to_new_window(self):
        self.responses[self.home] = [response(self.home, event(message()))]
        self.export()
        row, = self.sessions().values()
        before = self.markdown(row)
        self.snapshot(row).write_bytes(b'')
        answer = message(ANSWER, 'Only answer remains in live cache', 'assistant', '2')
        self.responses[self.home] = [response(self.home, event(answer), mtime=200)]
        with self.assertRaises((ValueError, RuntimeError)):
            self.export()
        self.assertEqual(self.markdown(row), before)
        self.assertEqual(self.snapshot(row).read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
