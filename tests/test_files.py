import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e_agent.files import FileError, FileTools, MAX_FILE_BYTES, MAX_OUTPUT_BYTES


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.files = FileTools(self.root)

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def test_read_range_hash_and_crlf(self):
        self.put('a.txt', b'a\r\nb\r\nc\r\n')
        whole = self.files.read_file('a.txt')
        part = self.files.read_file('a.txt', 2, 2)
        self.assertEqual(part['content'], 'b\r\n')
        self.assertEqual(part['hash'], whole['hash'])
        self.assertEqual(part['total_lines'], 3)
        self.assertFalse(part['truncated'])
        for start, end in [(0,None),(4,None),(2,1),(True,None)]:
            with self.assertRaises(FileError): self.files.read_file('a.txt', start, end)

    def test_read_empty_encoding_binary_size_and_output(self):
        self.put('empty', '')
        self.assertEqual(self.files.read_file('empty')['content'], '')
        for name, data in [('encoding', b'\xff'), ('binary', b'a\x00b'), ('large', b'a' * (MAX_FILE_BYTES + 1))]:
            self.put(name, data)
            with self.assertRaises(FileError): self.files.read_file(name)
        self.put('output', '你' * 30000)
        result = self.files.read_file('output')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['content'].encode()), MAX_OUTPUT_BYTES)
        os.mkfifo(self.root / 'pipe')
        with self.assertRaises(FileError): self.files.read_file('pipe')

    def test_boundaries_and_symlinks(self):
        outside = Path(self.temp.name) / 'secret'
        outside.write_text('private')
        (self.root / 'link').symlink_to(outside)
        (self.root / 'parent').symlink_to(self.root.parent, target_is_directory=True)
        for path in ['../secret', str(outside), 'link', 'parent/secret']:
            with self.assertRaises(FileError): self.files.read_file(path)
        self.assertEqual(self.files.find_files()['matches'], [])

    def test_shared_sensitive_rules_and_exact_exception(self):
        protected = ['.env', '.env.local', '.aws/credentials', '.ssh/id_rsa', 'private.pem', '.git/config', '.kube/config']
        for path in protected: self.put(path, 'needle secret')
        self.put('.env.example', 'needle sample')
        self.put('code.py', 'needle public')
        for path in protected:
            with self.assertRaises(FileError): self.files.read_file(path)
        self.assertEqual(set(self.files.find_files()['matches']), {'.env.example', 'code.py'})
        self.assertEqual({m['path'] for m in self.files.search_text('needle')['matches']}, {'.env.example', 'code.py'})
        allowed = FileTools(self.root, sensitive_exceptions=['.aws/credentials'])
        self.assertIn('needle', allowed.read_file('.aws/credentials')['content'])
        self.assertIn('.aws/credentials', allowed.find_files()['matches'])
        with self.assertRaises(FileError): allowed.read_file('.env')

    def test_create_never_overwrites_even_link_or_empty(self):
        self.files.write_file('new.py', 'hello')
        with self.assertRaises(FileError): self.files.write_file('new.py', 'replace')
        self.assertEqual((self.root / 'new.py').read_text(), 'hello')
        (self.root / 'broken').symlink_to('missing')
        with self.assertRaises(FileError): self.files.write_file('broken', 'replace')
        self.put('empty', '')
        with self.assertRaises(FileError): self.files.write_file('empty', 'replace')

    def test_edit_preserves_crlf_mode_and_rejects_old_hash(self):
        path = self.put('code.py', b'a\r\nb\r\n')
        path.chmod(0o750)
        first = self.files.read_file('code.py')
        result = self.files.edit_file('code.py', first['hash'], 'b', 'c')
        self.assertEqual(path.read_bytes(), b'a\r\nc\r\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o750)
        self.assertEqual(result['hash'], self.files.read_file('code.py')['hash'])
        with self.assertRaises(FileError): self.files.edit_file('code.py', first['hash'], 'c', 'd')
        self.assertEqual(path.read_bytes(), b'a\r\nc\r\n')

    def test_edit_requires_exactly_one_nonempty_match(self):
        path = self.put('code', 'aaa')
        version = self.files.read_file('code')['hash']
        for old in ['', 'z', 'a', 'aa']:
            with self.assertRaises(FileError): self.files.edit_file('code', version, old, 'new')
        self.assertEqual(path.read_text(), 'aaa')

    def test_prepare_then_target_changes_is_rejected(self):
        path = self.put('code', 'old')
        prepared = self.files.prepare_edit_file('code', self.files.read_file('code')['hash'], 'old', 'new')
        path.write_text('user edit')
        with self.assertRaises(FileError): self.files.execute_prepared(prepared)
        self.assertEqual(path.read_text(), 'user edit')

    def test_prepare_then_same_content_inode_or_mode_changes_is_rejected(self):
        path = self.put('code', 'old')
        for change in ('inode', 'mode'):
            prepared = self.files.prepare_edit_file('code', self.files.read_file('code')['hash'], 'old', 'new')
            if change == 'inode':
                path.unlink()
                path.write_text('old')
            else: path.chmod(0o700)
            with self.assertRaises(FileError): self.files.execute_prepared(prepared)
            self.assertEqual(path.read_text(), 'old')

    def test_prepare_then_parent_replaced_or_linked_is_rejected(self):
        self.put('dir/code', 'old')
        prepared = self.files.prepare_edit_file('dir/code', self.files.read_file('dir/code')['hash'], 'old', 'new')
        (self.root / 'dir').rename(self.root / 'moved')
        (self.root / 'dir').mkdir()
        self.put('dir/code', 'old')
        with self.assertRaises(FileError): self.files.execute_prepared(prepared)
        self.assertEqual((self.root / 'dir/code').read_text(), 'old')
        (self.root / 'dir/code').unlink()
        (self.root / 'dir').rmdir()
        (self.root / 'dir').symlink_to(self.root / 'moved', target_is_directory=True)
        with self.assertRaises(FileError): self.files.execute_prepared(prepared)
        self.assertEqual((self.root / 'moved/code').read_text(), 'old')

    def test_create_after_confirmation_cannot_overwrite(self):
        prepared = self.files.prepare_write_file('new', 'agent')
        self.put('new', 'user')
        with self.assertRaises(FileError): self.files.execute_prepared(prepared)
        self.assertEqual((self.root / 'new').read_text(), 'user')

    def test_create_atomic_no_replace_after_final_check(self):
        prepared = self.files.prepare_write_file('new', 'agent')
        actual_link = os.link
        def race(*args, **kwargs):
            self.put('new', 'user')
            return actual_link(*args, **kwargs)
        with patch('e_agent.files.os.link', side_effect=race), self.assertRaises(FileError):
            self.files.execute_prepared(prepared)
        self.assertEqual((self.root / 'new').read_text(), 'user')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['new'])

    def test_final_recheck_after_temp_write(self):
        path = self.put('code', 'old')
        prepared = self.files.prepare_edit_file('code', self.files.read_file('code')['hash'], 'old', 'new')
        real_fsync = os.fsync
        def race(fd):
            path.write_text('user')
            return real_fsync(fd)
        with patch('e_agent.files.os.fsync', side_effect=race), self.assertRaises(FileError):
            self.files.execute_prepared(prepared)
        self.assertEqual(path.read_text(), 'user')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['code'])

    def test_search_paths_lines_limit_and_skipped_bad_files(self):
        self.put('src/a.py', 'first\nneedle 1\nneedle 2\n')
        self.put('src/b.py', 'needle 3')
        self.put('bad.py', b'\xff')
        found = self.files.find_files('*.py', limit=2)
        self.assertTrue(found['truncated'])
        result = self.files.search_text('needle', '*.py', limit=1)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['matches'][0]['path'], 'src/a.py')
        self.assertEqual(result['matches'][0]['line'], 2)
        full = self.files.search_text('needle')
        self.assertEqual(len(full['matches']), 3)
        self.assertEqual(full['skipped_files'], 1)
        self.assertFalse(full['truncated'])

    def test_search_long_line_has_truncation(self):
        self.put('long', 'needle' + 'x' * 10000)
        result = self.files.search_text('needle')['matches'][0]
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['content'].encode()), 4096)


class FileRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_preparation_keeps_approved_snapshot(self):
        from e_agent.files import register_file_tools
        from e_agent.tools import Registry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'code').write_text('old')
            files = FileTools(root)
            registry = Registry()
            register_file_tools(registry, files)
            self.assertEqual(len(registry.definitions()), 5)
            tool = registry.tool('edit_file')
            args = {'path': 'code', 'old_hash': files.read_file('code')['hash'], 'old_text': 'old', 'new_text': 'new'}
            prepared = tool.prepare(args)
            self.assertEqual(prepared.target['relative_path'], 'code')
            self.assertEqual(prepared.target['path'], str(root / 'code'))
            args['new_text'] = 'changed after approval'
            await prepared.execute()
            self.assertEqual((root / 'code').read_text(), 'new')
            self.assertIn('-old', prepared.target['diff'])
            self.assertIn('+new', prepared.target['diff'])
