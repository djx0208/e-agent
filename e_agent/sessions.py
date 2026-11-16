"""私有项目会话快照；恢复只补记录，绝不执行历史工具。"""
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .config import _parent_fd
from .model import Redactor, strict_json


VERSION = 1
MAX_SNAPSHOT = 16 * 1024 * 1024


class SessionError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return uuid.uuid4().hex


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value) is not None


class Session:
    def __init__(self, data):
        self.data = data

    @property
    def id(self): return self.data['id']
    @property
    def project(self): return self.data['project']
    @property
    def messages(self): return self.data['messages']
    @property
    def state(self): return self.data['state']
    @state.setter
    def state(self, value): self.data['state'] = value
    @property
    def tool_records(self): return self.data['tool_records']
    @property
    def audit(self): return self.data['audit']
    @property
    def summary(self): return self.data['summary']
    @summary.setter
    def summary(self, value): self.data['summary'] = value
    @property
    def skills(self): return self.data['skills']

    def append(self, message):
        record = copy.deepcopy(message)
        record['id'] = _id()
        self.messages.append(record)
        return record

    def model_messages(self):
        # 只发协议字段；稳定 ID 和来源/快照信息仅用于本地记录。
        allowed = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'reasoning_content'}
        return [{k: copy.deepcopy(v) for k, v in item.items() if k in allowed} for item in self.messages]


def _validate(data, project, sid):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != VERSION:
        raise SessionError('会话格式版本不支持，原文件已保留。')
    if data.get('id') != sid or not _valid_id(sid):
        raise SessionError('会话标识无效，原文件已保留。')
    if data.get('project') != project:
        raise SessionError('会话属于其他项目，不能恢复。')
    for name in ('messages', 'tool_records', 'audit'):
        if not isinstance(data.get(name), list):
            raise SessionError('会话记录损坏，原文件已保留。')
    if (not isinstance(data.get('state'), str) or not isinstance(data.get('skills'), dict)
            or data.get('summary') is not None and not isinstance(data['summary'], dict)
            or not all(isinstance(data.get(k), str) for k in ('created_at', 'updated_at'))):
        raise SessionError('会话记录损坏，原文件已保留。')
    mids = set()
    pending = set()
    for message in data['messages']:
        if not isinstance(message, dict) or not _valid_id(message.get('id')) or message['id'] in mids:
            raise SessionError('会话消息标识损坏，原文件已保留。')
        mids.add(message['id'])
        role = message.get('role')
        if role not in ('user', 'assistant', 'tool', 'system'):
            raise SessionError('会话消息角色无效。')
        if message.get('content') is not None and not isinstance(message['content'], str):
            raise SessionError('会话消息内容无效。')
        if pending and role != 'tool':
            raise SessionError('会话工具配对损坏，原文件已保留。')
        if role == 'tool':
            cid = message.get('tool_call_id')
            if cid not in pending:
                raise SessionError('会话工具配对损坏，原文件已保留。')
            pending.remove(cid)
        calls = message.get('tool_calls', [])
        if calls:
            if role != 'assistant' or not isinstance(calls, list):
                raise SessionError('会话工具请求损坏。')
            for call in calls:
                if (not isinstance(call, dict) or not isinstance(call.get('id'), str)
                        or not call['id'] or call['id'] in pending
                        or call.get('type') != 'function' or not isinstance(call.get('function'), dict)):
                    raise SessionError('会话工具请求损坏。')
                function = call['function']
                if not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str):
                    raise SessionError('会话工具请求损坏。')
                try:
                    if not isinstance(strict_json(function['arguments']), dict): raise ValueError()
                except (ValueError, TypeError, RecursionError):
                    raise SessionError('会话工具参数损坏。') from None
                pending.add(call['id'])
    for record in data['tool_records']:
        if not isinstance(record, dict) or not isinstance(record.get('call_id'), str) or not isinstance(record.get('status'), str):
            raise SessionError('会话操作记录损坏。')
    return pending


class Store:
    def __init__(self, project, data_dir=None, redactor=None):
        self.project = str(Path(project).resolve(strict=True))
        if not Path(self.project).is_dir():
            raise SessionError('项目目录无效。')
        if data_dir is None:
            state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
            if not state.is_absolute(): raise SessionError('XDG_STATE_HOME 必须是绝对路径。')
            base = state / 'e-agent'
        else:
            base = Path(data_dir).expanduser().absolute()
        project_key = hashlib.sha256(self.project.encode()).hexdigest()
        self.path = base / 'projects' / project_key / 'sessions'
        self.redactor = redactor or Redactor()
        self._fd = self._lock = None
        self._known = {}
        self.failed = False
        try:
            self._fd, name = _parent_fd(self.path / '.lock', create=True)
            if os.fstat(self._fd).st_uid != os.getuid(): raise OSError()
            os.fchmod(self._fd, 0o700)
            self._lock = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._fd)
            info = os.fstat(self._lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1: raise OSError()
            os.fchmod(self._lock, 0o600)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            self.close()
            raise SessionError('无法锁定会话目录：项目可能已在另一进程打开，或目录权限/链接无效。') from None

    def _ready(self):
        if self._fd is None: raise SessionError('会话存储已关闭。')
        if self.failed: raise SessionError('会话保存曾失败，已停止新操作；请退出后核实并恢复。')
        try:
            info = os.stat(self.path, follow_symlinks=False)
            opened = os.fstat(self._fd)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino): raise OSError()
            if self.path.resolve() != self.path: raise OSError()
        except OSError:
            raise SessionError('会话目录已变化，停止新操作。') from None

    def _read(self, sid):
        if not _valid_id(sid): raise SessionError('会话 ID 无效。')
        fd = os.open(sid + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise SessionError('会话文件权限、类型或所有者无效。')
            raw = stream.read(MAX_SNAPSHOT + 1)
            if len(raw) > MAX_SNAPSHOT: raise SessionError('会话记录超过大小限制。')
        return raw

    @staticmethod
    def _can_switch(current):
        if current is not None and current.state == 'running':
            raise SessionError('任务运行中不能新建或切换会话。')

    def new(self, current=None):
        self._ready()
        self._can_switch(current)
        now = _now()
        session = Session({'version': VERSION, 'id': _id(), 'project': self.project,
                           'created_at': now, 'updated_at': now, 'messages': [], 'state': 'idle',
                           'tool_records': [], 'audit': [], 'summary': None, 'skills': {}})
        self.save(session)
        return session

    def save(self, session):
        self._ready()
        temporary = None
        try:
            # 兼容通用 Agent 暂时直接 append 的消息，原地补 ID 保持共享引用。
            for message in session.messages:
                if isinstance(message, dict) and 'id' not in message: message['id'] = _id()
            session.data['updated_at'] = _now()
            _validate(session.data, self.project, session.id)
            payload = self.redactor.clean(copy.deepcopy(session.data))
            _validate(payload, self.project, session.id)
            raw = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
            if len(raw) > MAX_SNAPSHOT: raise SessionError('会话记录超过大小限制。')
            name = session.id + '.json'
            expected = self._known.get(session.id)
            def unchanged():
                try:
                    old = self._read(session.id)
                except FileNotFoundError:
                    if expected is not None: raise SessionError('会话文件已被移除，拒绝覆盖。')
                else:
                    if expected is None or hashlib.sha256(old).hexdigest() != expected:
                        raise SessionError('会话文件已变化，拒绝覆盖。')
            unchanged()
            temporary = '.' + _id() + '.tmp'
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._ready()
            unchanged()
            if expected is None:
                os.link(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=self._fd)
            else:
                os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            temporary = None
            os.fsync(self._fd)
            self._known[session.id] = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, KeyError, RecursionError, SessionError):
            self.failed = True
            raise SessionError('会话保存失败，已停止新操作；最近完整快照保留，请退出后核实。') from None
        finally:
            if temporary is not None:
                try: os.unlink(temporary, dir_fd=self._fd)
                except OSError: pass

    def load(self, sid, recover=True):
        self._ready()
        try:
            raw = self._read(sid)
            data = strict_json(raw)
            pending = _validate(data, self.project, sid)
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise SessionError('无法读取会话或记录已损坏，原文件已保留。') from None
        self._known[sid] = hashlib.sha256(raw).hexdigest()
        session = Session(data)
        if recover and (pending or session.state == 'running'):
            self._recover(session, pending)
            self.save(session)
        return session

    def select(self, sid, current=None):
        self._can_switch(current)
        return self.load(sid)

    def _recover(self, session, pending):
        from .tools import ToolResult
        # 按原请求顺序补齐，而不是集合迭代顺序。
        calls = [call for msg in session.messages for call in msg.get('tool_calls', []) if call['id'] in pending]
        seen = set()
        for call in reversed(calls):
            if call['id'] in seen: continue
            seen.add(call['id'])
        order = [cid for cid in dict.fromkeys(call['id'] for call in calls) if cid in seen]
        records = {r['call_id']: r for r in session.tool_records}
        for cid in order:
            record = records.get(cid)
            if record and isinstance(record.get('result'), dict) and record['status'] not in ('started', 'not_executed'):
                saved = record['result']
                try:
                    result = ToolResult(call_id=cid, status=saved['status'], output=saved['output'],
                                        truncated=saved.get('truncated', False), source=saved.get('source', 'builtin'))
                except (KeyError, ValueError, TypeError):
                    raise SessionError('会话中已保存工具结果损坏，原文件已保留。') from None
            else:
                status = 'outcome_unknown' if record and record['status'] == 'started' else 'not_executed'
                result = ToolResult(cid, status, '运行中断，执行结果未知，请核实。' if status == 'outcome_unknown' else '运行中断，本调用尚未执行。',
                                    source=record.get('source', 'builtin') if record else 'builtin')
                if record is None:
                    record = {'call_id': cid, 'name': next(c['function']['name'] for c in calls if c['id'] == cid)}
                    session.tool_records.append(record)
                record.update(status=status, result=result.as_dict())
            session.append(result.message())
        session.state = 'interrupted'
        session.data['recovered_at'] = _now()

    def list(self):
        self._ready()
        result = []
        for name in sorted(os.listdir(self._fd)):
            if not name.endswith('.json') or not _valid_id(name[:-5]): continue
            sid = name[:-5]
            try:
                session = self.load(sid, recover=False)
                result.append({'id': sid, 'state': session.state, 'updated_at': session.data['updated_at'],
                               'message_count': len(session.messages), 'error': None})
            except SessionError as exc:
                result.append({'id': sid, 'state': 'unavailable', 'error': str(exc)})
        return result

    def close(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
