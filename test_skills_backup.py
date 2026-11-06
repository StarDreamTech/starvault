"""Portable Skill backup checks; all inputs and outputs use temporary folders."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import skills_backup as skills


class SkillBackupTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name).resolve()
        self.root = self.base / 'backup'
        self.source = self.base / 'sources'

    def skill(self, name='示例 skill', parent=None):
        path = (parent or self.source) / name
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('# Example\nDo not execute: touch SHOULD_NOT_EXIST\n', encoding='utf-8')
        (path / 'scripts').mkdir()
        (path / 'scripts/run.sh').write_bytes(b'#!/bin/sh\nprintf hello\n')
        (path / 'scripts/run.sh').chmod(0o744)
        (path / 'assets').mkdir()
        (path / 'assets/data.bin').write_bytes(bytes(range(256)))
        (path / 'empty').mkdir()
        return path

    def backup(self):
        return skills.backup(self.root, [self.source])

    def test_default_locations_override_environment_and_exclude_plugin_cache(self):
        home = self.base / 'home'
        project = self.base / 'project'
        with patch.object(Path, 'home', return_value=home), patch.object(Path, 'cwd', return_value=project), patch.dict(
                os.environ, {'CODEX_HOME': str(home / 'custom-codex'), 'CLAUDE_CONFIG_DIR': str(home / 'custom-claude')}):
            sources = skills.default_sources()
            self.assertEqual(sources, [home / 'custom-codex/skills', home / 'custom-claude/skills', home / '.agents/skills',
                                       project / '.agents/skills', project / '.codex/skills', project / '.claude/skills'])
            self.assertEqual(skills.default_sources(home / 'explicit', home / 'claude')[0], home / 'explicit/skills')
            self.assertFalse(any('plugins' in p.parts for p in sources))

    def test_plan_has_no_writes_and_full_folder_restores(self):
        source = self.skill()
        planned = skills.backup(self.root, [self.source], plan=True)
        self.assertTrue(planned['plan'])
        self.assertEqual(planned['skills'], 1)
        self.assertEqual(planned['files'], 3)
        self.assertGreater(planned['estimated_bytes'], 256)
        self.assertFalse(self.root.exists())
        result = self.backup()
        self.assertEqual(result['copied'], 1)
        item = result['items'][0]
        restored = skills.restore(self.root, item['id'], self.base / 'new computer')
        target = Path(restored['restored'])
        self.assertEqual(target.parent, self.base / 'new computer')
        self.assertTrue((target / 'empty').is_dir())
        for relative in ('SKILL.md', 'scripts/run.sh', 'assets/data.bin'):
            self.assertEqual((target / relative).read_bytes(), (source / relative).read_bytes())
        if os.name != 'nt':
            self.assertEqual((target / 'scripts/run.sh').stat().st_mode & 0o111, 0o100)
        self.assertFalse((self.base / 'SHOULD_NOT_EXIST').exists())
        self.assertTrue(skills.restore(self.root, item['id'], target.parent)['skipped'])

    def test_unchanged_runs_do_not_rewrite_and_equal_contents_deduplicate(self):
        source = self.skill('one')
        other = self.source / 'two'
        shutil.copytree(source, other)
        result = self.backup()
        self.assertEqual((result['skills'], result['copied']), (2, 1))
        self.assertEqual(len({i['id'] for i in result['items']}), 2)
        self.assertEqual(len({i['version'] for i in result['items']}), 1)
        index = self.root / 'skills/index.json'
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (index, *self.root.glob('skills/objects/*.zip'))}
        os.utime(source / 'SKILL.md', None)
        self.assertEqual(self.backup()['copied'], 0)
        for path, state in before.items():
            self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), state)

    def test_history_and_missing_sources_keep_old_backups(self):
        source = self.skill()
        first = self.backup()['items'][0]
        original = (source / 'SKILL.md').read_bytes()
        (source / 'SKILL.md').write_text('new version', encoding='utf-8')
        second = self.backup()['items'][0]
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['version'], second['version'])
        self.assertEqual(len(second['versions']), 2)
        self.assertTrue((self.root / first['archive']).is_file())
        old = skills.restore(self.root, first['id'], self.base / 'restore old', first['version'])
        self.assertEqual((Path(old['restored']) / 'SKILL.md').read_bytes(), original)
        shutil.rmtree(self.source)
        index = (self.root / 'skills/index.json').read_bytes()
        self.assertEqual(self.backup()['skills'], 0)
        self.assertEqual(len(skills.list_backups(self.root)['items']), 1)
        self.assertEqual((self.root / 'skills/index.json').read_bytes(), index)

    def test_direct_skill_and_system_subfolders_are_discovered_once(self):
        one = self.skill('one', self.source / '.system')
        self.skill('two')
        result = skills.backup(self.root, [self.source, one, self.source])
        self.assertEqual(result['skills'], 2)

    def test_links_and_generated_directories_are_excluded_with_warning(self):
        source = self.skill()
        (source / '__pycache__').mkdir()
        (source / '__pycache__/ignored.pyc').write_bytes(b'cache')
        external = self.base / 'external'; external.mkdir()
        (external / 'SKILL.md').write_text('external', encoding='utf-8')
        try:
            (source / 'internal-link').symlink_to(source / 'assets', target_is_directory=True)
            (source / 'external-link').symlink_to(external, target_is_directory=True)
            (source / 'file-link').symlink_to(source / 'SKILL.md')
            (self.source / 'linked-no-skill').symlink_to(source / 'assets', target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit symlinks')
        result = self.backup()
        self.assertEqual(result['skills'], 1)
        self.assertGreaterEqual(len(result['warnings']), 5)
        self.assertTrue(any('internal-link' in w for w in skills.list_backups(self.root)['warnings']))
        with zipfile.ZipFile(self.root / result['items'][0]['archive']) as package:
            self.assertEqual(set(package.namelist()), {'SKILL.md', 'scripts/', 'scripts/run.sh', 'assets/', 'assets/data.bin', 'empty/'})
        # A source explicitly selected through a link may resolve to its real root.
        self.assertEqual(skills.backup(self.base / 'separate', [source / 'external-link'])['skills'], 1)

    def test_direct_collection_skill_links_are_collected_once_without_following_package_links(self):
        target = self.skill('shared', self.base / 'elsewhere')
        self.source.mkdir()
        try:
            (self.source / 'shared-skill').symlink_to(target, target_is_directory=True)
            (self.source / 'shared-alias').symlink_to(target, target_is_directory=True)
            (target / 'inside-link').symlink_to(target / 'assets', target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit symlinks')
        result = skills.backup(self.root, [self.source, target])
        self.assertEqual(result['skills'], 1)
        self.assertEqual(result['items'][0]['source'], str(target))
        self.assertTrue(any('inside-link' in w for w in result['warnings']))
        self.assertFalse(any('shared-skill' in w for w in result['warnings']))
        with zipfile.ZipFile(self.root / result['items'][0]['archive']) as package:
            self.assertNotIn('inside-link/', package.namelist())

    def test_destination_conflicts_and_links_are_not_overwritten(self):
        self.skill()
        item = self.backup()['items'][0]
        result = skills.restore(self.root, item['id'], self.base / 'restore')
        target = Path(result['restored'])
        (target / 'SKILL.md').write_text('keep me', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            skills.restore(self.root, item['id'], target.parent)
        self.assertEqual((target / 'SKILL.md').read_text(), 'keep me')
        shutil.rmtree(target)
        target.mkdir()
        with self.assertRaises(FileExistsError):
            skills.restore(self.root, item['id'], target.parent)

    def test_windows_repeat_restore_ignores_execute_bits_but_checks_content(self):
        self.skill()
        item = self.backup()['items'][0]
        result = skills.restore(self.root, item['id'], self.base / 'windows restore')
        target = Path(result['restored'])
        script = target / 'scripts/run.sh'
        script.chmod(0o644)
        with patch.object(skills, 'WINDOWS', True):
            self.assertTrue(skills.restore(self.root, item['id'], target.parent)['skipped'])
            script.write_bytes(b'changed bytes')
            with self.assertRaises(FileExistsError):
                skills.restore(self.root, item['id'], target.parent)
            script.write_bytes(b'#!/bin/sh\nprintf hello\n')
            (target / 'extra.txt').write_bytes(b'additional file')
            with self.assertRaises(FileExistsError):
                skills.restore(self.root, item['id'], target.parent)
    def malicious_archive(self, entries):
        version = 'a' * 64
        identity = 'b' * 64
        archive = 'skills/objects/' + version + '.zip'
        path = self.root / archive
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('SKILL.md', 'example')
            for name, mode in entries:
                info = zipfile.ZipInfo()
                info.filename = name  # Preserve malicious backslashes on Windows too.
                info.create_system = 3
                info.external_attr = mode << 16
                package.writestr(info, b'payload')
        index = {'format_version': 1, 'items': [{'id': identity, 'name': 'CON', 'version': version,
                 'versions': [{'version': version, 'archive': archive}]}]}
        (self.root / 'skills/index.json').write_text(json.dumps(index), encoding='utf-8')
        return identity

    def test_restore_rejects_unsafe_zip_paths_on_every_platform(self):
        bad = ['../outside', '/absolute', 'C:/absolute', 'C:drive', '\\server\\share', 'a\\b',
               'NUL.txt', 'con', 'CON .txt', 'COM1.txt', 'LPT¹', 'file:stream', 'trailing.', 'trailing ',
               'a/../b', 'a//b', './x', 'a/PRN', 'control\x01', 'question?']
        for name in bad:
            with self.subTest(name=name):
                identity = self.malicious_archive([(name, stat.S_IFREG | 0o644)])
                with self.assertRaisesRegex(ValueError, '路径'):
                    skills.restore(self.root, identity, self.base / 'restore')
                self.assertFalse((self.base / 'restore').exists())

    def test_restore_rejects_case_unicode_collisions_links_and_special_files(self):
        cases = [
            [('Docs/a', stat.S_IFREG), ('docs/b', stat.S_IFREG)],
            [('caf\u00e9', stat.S_IFREG), ('cafe\u0301', stat.S_IFREG)],
            [('script', stat.S_IFREG), ('script/a', stat.S_IFREG)],
            [('link', stat.S_IFLNK | 0o777)], [('pipe', stat.S_IFIFO | 0o600)],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                identity = self.malicious_archive(entries)
                with self.assertRaises(ValueError):
                    skills.restore(self.root, identity, self.base / 'restore')
                self.assertFalse((self.base / 'restore').exists())

    def test_unsafe_source_names_fail_without_writing(self):
        source = self.skill()
        if os.name == 'nt':
            self.skipTest('Windows itself prevents creating this test filename')
        (source / 'CON.txt').write_text('data', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不可移植'):
            self.backup()
        self.assertFalse(self.root.exists())

    def test_corrupt_packages_fail_before_restoring_or_reusing(self):
        self.skill()
        item = self.backup()['items'][0]
        path = self.root / item['archive']
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('SKILL.md', 'tampered')
        with self.assertRaisesRegex(ValueError, '校验'):
            skills.restore(self.root, item['id'], self.base / 'restore')
        self.assertFalse((self.base / 'restore').exists())
        with self.assertRaisesRegex(ValueError, '校验'):
            self.backup()

    def test_low_space_is_reported_by_plan_and_rechecked_before_write(self):
        self.skill()
        with patch.object(skills, '_free', return_value=0):
            self.assertFalse(skills.backup(self.root, [self.source], plan=True)['fits'])
            with self.assertRaises(RuntimeError):
                self.backup()
        with patch.object(skills, '_free', side_effect=[10**12, 0]):
            with self.assertRaises(RuntimeError):
                self.backup()
        self.assertFalse(self.root.exists())

    def test_source_changes_during_copy_do_not_publish_truncated_archive(self):
        source = self.skill()
        item = self.backup()['items'][0]
        before = (self.root / 'skills/index.json').read_bytes()
        (source / 'SKILL.md').write_text('second version', encoding='utf-8')
        original = skills._hash_stream
        changed = False
        def mutate(stream, output=None):
            nonlocal changed
            result = original(stream, output)
            if output is not None and not changed:
                changed = True
                (source / 'SKILL.md').write_text('changed concurrently', encoding='utf-8')
            return result
        with patch.object(skills, '_hash_stream', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, '变化'):
                self.backup()
        self.assertEqual((self.root / 'skills/index.json').read_bytes(), before)
        self.assertEqual(list(self.root.glob('skills/objects/*')), [self.root / item['archive']])

    def test_no_recursive_self_backup_or_linked_storage(self):
        source = self.skill()
        with self.assertRaisesRegex(ValueError, '来源内部'):
            skills.backup(source / 'backup', [source])
        outside = self.base / 'elsewhere'; outside.mkdir()
        self.root.mkdir()
        try:
            (self.root / 'skills').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit symlinks')
        with self.assertRaisesRegex(ValueError, '链接'):
            self.backup()
        self.assertEqual(list(outside.iterdir()), [])

    def test_filename_normalization_does_not_change_portable_content_version(self):
        first = [{'path': 'caf\u00e9', 'kind': 'file', 'bytes': 1, 'sha256': 'same', 'executable': 0}]
        second = [{**first[0], 'path': 'cafe\u0301'}]
        self.assertEqual(skills._version(first), skills._version(second))


if __name__ == '__main__':
    unittest.main()
