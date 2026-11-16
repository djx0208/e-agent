"""项目内文件操作；目录描述符及版本复查用于防止意外覆盖。"""
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat

MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MATCHES = 200


class FileError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino)


def _version(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, stat.S_IMODE(info.st_mode))


def _clip(text, limit=MAX_OUTPUT_BYTES):
    data = text.encode('utf-8')
    return data[:limit].decode('utf-8', errors='ignore'), len(data) > limit


@dataclass(frozen=True)
class PreparedFileOperation:
    operation: str
    path: str
    parents: tuple
    version: tuple | None
    old_hash: str | None
    data: bytes
    mode: int
    diff: str

    def describe(self, root):
        return {'path': str(root / self.path), 'operation': self.operation,
                'version': self.old_hash, 'diff': self.diff,
                'boundary': '仅项目内文件；执行前重新检查路径及版本。'}


class FileTools:
    def __init__(self, root, sensitive_exceptions=()):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise FileError('项目路径必须是目录。')
        self.root_identity = _identity(self.root.stat())
        self.sensitive_exceptions = frozenset(self._normalize(p) for p in sensitive_exceptions)

    def _normalize(self, path, allow_root=False):
        if not isinstance(path, str) or not path or '\x00' in path:
            raise FileError('路径必须是非空字符串。')
        given = PurePosixPath(path)
        if given.is_absolute() or '..' in given.parts:
            raise FileError('只接受项目内相对路径。')
        normalized = given.as_posix()
        if normalized == '.' and not allow_root:
            raise FileError('需要文件路径。')
        return normalized

    def is_sensitive(self, path):
        from .safety import sensitive_path
        return sensitive_path(self._normalize(path, allow_root=True), self.sensitive_exceptions)

    def _check_path(self, path):
        path = self._normalize(path)
        if self.is_sensitive(path):
            raise FileError('该路径属于受保护的凭据或元数据路径。')
        return path

    @contextmanager
    def _parent(self, path):
        descriptors = []
        try:
            current = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            if _identity(os.fstat(current)) != self.root_identity:
                raise FileError('项目目录已变化，请重新打开项目。')
            identities = [_identity(os.fstat(current))]
            parts = PurePosixPath(path).parts
            for part in parts[:-1]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                descriptors.append(current)
                identities.append(_identity(os.fstat(current)))
            yield current, parts[-1], tuple(identities)
        except OSError as exc:
            raise FileError('文件路径不可访问、已变化或含不支持的链接。') from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _read_at(self, parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise FileError('只支持普通文件。')
            if before.st_size > MAX_FILE_BYTES:
                raise FileError('文件超过 256 KiB 上限。')
            chunks = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            if len(data) > MAX_FILE_BYTES:
                raise FileError('文件超过 256 KiB 上限。')
            if _version(before) != _version(os.fstat(fd)):
                raise FileError('读取期间文件变化，请重新读取。')
            self._decode(data)
            return data, before
        finally:
            os.close(fd)

    @staticmethod
    def _decode(data):
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise FileError('只支持 UTF-8 文本文件。') from None
        if '\x00' in text:
            raise FileError('不支持二进制文件。')
        return text

    def read_file(self, path, start_line=1, end_line=None):
        path = self._check_path(path)
        if type(start_line) is not int or start_line < 1 or (end_line is not None and (type(end_line) is not int or end_line < start_line)):
            raise FileError('行范围无效；行号从 1 开始。')
        with self._parent(path) as (parent, name, _):
            data, _ = self._read_at(parent, name)
        lines = self._decode(data).splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise FileError('起始行超出文件范围。')
        end = len(lines) if end_line is None else min(end_line, len(lines))
        content, truncated = _clip(''.join(lines[start_line - 1:end]))
        return {'path': path, 'content': content, 'hash': digest(data), 'start_line': start_line,
                'end_line': end, 'total_lines': len(lines), 'truncated': truncated}

    def prepare_write_file(self, path, content):
        path = self._check_path(path)
        data = self._encode(content)
        with self._parent(path) as (parent, name, parents):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileError('新建拒绝覆盖已有文件。')
        return PreparedFileOperation('write_file', path, parents, None, None, data, 0o644, self._diff(path, '', content))

    def prepare_edit_file(self, path, old_hash, old_text, new_text):
        path = self._check_path(path)
        if not isinstance(old_text, str) or not old_text:
            raise FileError('替换原文不能为空。')
        if not isinstance(new_text, str):
            raise FileError('替换内容必须是文本。')
        with self._parent(path) as (parent, name, parents):
            data, info = self._read_at(parent, name)
        if old_hash != digest(data):
            raise FileError('文件版本已变化，请重新读取。')
        text = self._decode(data)
        first = text.find(old_text)
        if first == -1 or text.find(old_text, first + 1) != -1:
            raise FileError('替换原文必须恰好匹配一次。')
        updated = text.replace(old_text, new_text, 1)
        return PreparedFileOperation('edit_file', path, parents, _version(info), digest(data),
                                     self._encode(updated), stat.S_IMODE(info.st_mode), self._diff(path, text, updated))

    @staticmethod
    def _encode(content):
        if not isinstance(content, str):
            raise FileError('文件内容必须是文本。')
        try:
            data = content.encode('utf-8')
        except UnicodeError:
            raise FileError('文件内容必须是有效 UTF-8。') from None
        if len(data) > MAX_FILE_BYTES or '\x00' in content:
            raise FileError('文件内容超过上限或包含二进制数据。')
        return data

    @staticmethod
    def _diff(path, old, new):
        diff = ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=path, tofile=path))
        # Approval must show the entire change; the dispatcher may reject large previews.
        return diff

    def _recheck(self, prepared):
        self._check_path(prepared.path)
        with self._parent(prepared.path) as (parent, name, parents):
            if parents != prepared.parents:
                raise FileError('父目录已变化，原批准失效。')
            if prepared.operation == 'write_file':
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise FileError('目标已出现，拒绝覆盖。')
            data, info = self._read_at(parent, name)
            if _version(info) != prepared.version or digest(data) != prepared.old_hash:
                raise FileError('目标版本已变化，请重新读取并批准。')

    def execute_prepared(self, prepared):
        if not isinstance(prepared, PreparedFileOperation) or prepared.operation not in {'write_file', 'edit_file'}:
            raise FileError('无效的文件操作。')
        self._recheck(prepared)
        with self._parent(prepared.path) as (parent, name, parents):
            if parents != prepared.parents:
                raise FileError('父目录已变化，原批准失效。')
            temporary = '.e-agent-write-' + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(prepared.data)
                    stream.flush()
                    os.fchmod(stream.fileno(), prepared.mode)
                    os.fsync(stream.fileno())
                self._recheck(prepared)
                if prepared.operation == 'write_file':
                    # link is atomic and refuses a target created since the final check.
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                else:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {'path': prepared.path, 'hash': digest(prepared.data), 'bytes': len(prepared.data), 'truncated': False}

    def write_file(self, path, content):
        return self.execute_prepared(self.prepare_write_file(path, content))

    def edit_file(self, path, old_hash, old_text, new_text):
        return self.execute_prepared(self.prepare_edit_file(path, old_hash, old_text, new_text))

    def _walk(self):
        def visit(fd, prefix):
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                path = f'{prefix}/{name}' if prefix else name
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    protected = self.is_sensitive(path)
                    if stat.S_ISDIR(info.st_mode):
                        if protected and not any(p.startswith(path + '/') for p in self.sensitive_exceptions):
                            continue
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            yield from visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and not protected:
                        yield path
                except OSError:
                    continue
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(os.fstat(fd)) != self.root_identity:
                raise FileError('项目目录已变化。')
            yield from visit(fd, '')
        finally:
            os.close(fd)

    def find_files(self, pattern='*', limit=MAX_MATCHES):
        self._search_args(pattern, limit)
        matches = []
        size = 0
        truncated = False
        for path in self._walk():
            if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(PurePosixPath(path).name, pattern):
                amount = len(json.dumps(path, ensure_ascii=False).encode()) + 2
                if len(matches) >= limit or size + amount > MAX_OUTPUT_BYTES - 128:
                    truncated = True
                    break
                matches.append(path)
                size += amount
        return {'matches': matches, 'truncated': truncated}

    @staticmethod
    def _search_args(value, limit):
        if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096:
            raise FileError('搜索条件不能为空且不能超过 4 KiB。')
        if type(limit) is not int or not 1 <= limit <= MAX_MATCHES:
            raise FileError('匹配数量须在 1 到 200 之间。')

    def search_text(self, query, pattern='*', limit=MAX_MATCHES):
        self._search_args(query, limit)
        self._search_args(pattern, limit)
        matches = []
        size = 0
        skipped = 0
        for path in self._walk():
            if not (fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)):
                continue
            try:
                with self._parent(self._check_path(path)) as (parent, name, _):
                    data, _ = self._read_at(parent, name)
            except FileError:
                skipped += 1
                continue
            for line_number, line in enumerate(self._decode(data).splitlines(), 1):
                if query not in line:
                    continue
                content, line_truncated = _clip(line, 4096)
                match = {'path': path, 'line': line_number, 'content': content, 'truncated': line_truncated}
                amount = len(json.dumps(match, ensure_ascii=False).encode()) + 2
                if len(matches) >= limit or size + amount > MAX_OUTPUT_BYTES - 128:
                    return {'matches': matches, 'truncated': True, 'skipped_files': skipped}
                matches.append(match)
                size += amount
        return {'matches': matches, 'truncated': False, 'skipped_files': skipped}


def register_file_tools(registry, files):
    """把文件能力接入统一分发；准备对象始终执行批准时的同一份快照。"""
    from .tools import PreparedOperation, Tool
    string = {'type': 'string'}
    path_schema = {'type': 'string', 'minLength': 1}

    def schema(properties, required):
        return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}

    definitions = [
        ('read_file', '读取项目内 UTF-8 文件的行范围，返回完整文件哈希。',
         schema({'path': path_schema, 'start_line': {'type': 'integer', 'minimum': 1}, 'end_line': {'type': 'integer', 'minimum': 1}}, ['path']), True),
        ('write_file', '仅新建项目内文件；拒绝覆盖已有文件。父目录必须已存在。',
         schema({'path': path_schema, 'content': string}, ['path', 'content']), False),
        ('edit_file', '以 read_file 返回的哈希校验版本，将唯一的非空原文精确替换。',
         schema({'path': path_schema, 'old_hash': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}, 'old_text': {'type': 'string', 'minLength': 1}, 'new_text': string}, ['path', 'old_hash', 'old_text', 'new_text']), False),
        ('find_files', '按 glob 查找项目内普通文件；不返回受保护路径或链接。',
         schema({'pattern': {'type': 'string', 'minLength': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, []), True),
        ('search_text', '在项目内文本中搜索字面字符串，返回路径、行号和截断信息。',
         schema({'query': {'type': 'string', 'minLength': 1}, 'pattern': {'type': 'string', 'minLength': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['query']), True),
    ]

    for name, description, parameters, readonly in definitions:
        def prepare(args, name=name, readonly=readonly):
            arguments = dict(args)
            if not readonly:
                snapshot = getattr(files, 'prepare_' + name)(**arguments)
                target = snapshot.describe(files.root)
                target['relative_path'] = snapshot.path
                target['parent_versions'] = snapshot.parents
                target['file_version'] = snapshot.version
                async def execute():
                    return files.execute_prepared(snapshot)
                return PreparedOperation(target, execute, lambda: files._recheck(snapshot))
            target = {'project': str(files.root), 'operation': name, 'arguments': arguments}
            if 'path' in arguments:
                relative = files._check_path(arguments['path'])
                target.update(path=str(files.root / relative), relative_path=relative)
            async def execute():
                return getattr(files, name)(**arguments)
            return PreparedOperation(target, execute, lambda: None)

        async def execute(args, prepare=prepare):
            # Direct registration users share the same preparation and recheck logic.
            operation = prepare(args)
            operation.recheck()
            return await operation.execute()
        registry.register(Tool(name, description, parameters, 'builtin', execute, readonly, prepare))
