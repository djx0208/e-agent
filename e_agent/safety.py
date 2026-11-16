"""可信策略快照及审计；项目文字和工具描述不能创建批准。"""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from fnmatch import fnmatchcase
import copy
import hashlib
import json


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    version: str
    source: str
    target: dict

    def as_dict(self):
        return {'action': self.action, 'reason': self.reason, 'version': self.version,
                'source': self.source, 'target': copy.deepcopy(self.target)}


class Policy:
    """只读取显式用户配置；无配置使用固定默认值，损坏配置拒绝全部操作。"""
    def __init__(self, config=None, *, valid=True, origin='default', redactor=None):
        config = {} if config is None else copy.deepcopy(config)
        self._valid = valid and self._valid_config(config)
        self._config = config if self._valid else {}
        self.origin = origin
        self.redactor = redactor
        self.records = []
        encoded = json.dumps(self._config, sort_keys=True, ensure_ascii=False).encode()
        self.version = hashlib.sha256(encoded + str(self._valid).encode()).hexdigest()
        self._deny_tools = tuple(self._config.get('deny_tools', ()))
        self._deny_sources = tuple(self._config.get('deny_sources', ()))
        self._deny_paths = tuple(self._config.get('deny_paths', ()))
        self.sensitive_exceptions = tuple(self._config.get('sensitive_exceptions', ()))

    @staticmethod
    def _valid_config(config):
        if not isinstance(config, dict) or set(config) - {'deny_tools', 'deny_sources', 'deny_paths', 'sensitive_exceptions'}:
            return False
        for values in config.values():
            if not isinstance(values, list) or not all(isinstance(x, str) and x and '\x00' not in x for x in values):
                return False
        for path in config.get('sensitive_exceptions', []):
            if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or str(PurePosixPath(path)) != path or path == '.':
                return False
        return True

    @classmethod
    def from_file(cls, path, *, redactor=None):
        if path is None:
            return cls(redactor=redactor)
        try:
            from .model import strict_json
            with Path(path).open('rb') as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError()
            config = strict_json(data.decode('utf-8'))
            return cls(config, origin=str(Path(path).absolute()), redactor=redactor)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return cls(valid=False, origin='invalid explicit configuration', redactor=redactor)

    def is_sensitive(self, path):
        return not self._valid or sensitive_path(path, self.sensitive_exceptions)

    def decide(self, tool, target=None):
        action, reason = 'deny', '安全策略无效，拒绝新操作。'
        try:
            target = copy.deepcopy({} if target is None else target)
            if not isinstance(target, dict):
                raise ValueError()
            if self._valid:
                path = target.get('relative_path', target.get('path', ''))
                if not isinstance(path, str):
                    raise ValueError()
                if (any(fnmatchcase(tool.name, rule) for rule in self._deny_tools) or
                    any(fnmatchcase(tool.source, rule) for rule in self._deny_sources) or
                    any(fnmatchcase(path, rule) for rule in self._deny_paths)):
                    reason = '命中可信配置中的禁止规则。'
                elif tool.source == 'builtin' and tool.readonly:
                    action, reason = 'allow', '内置只读操作。'
                else:
                    action, reason = 'ask', '本次操作需要逐次确认。'
        except (TypeError, ValueError, RecursionError, AttributeError):
            target = {}
            action, reason = 'deny', '安全判断失败，拒绝操作。'
        return Decision(action, reason, self.version, tool.source, target)

    def audit(self, call_id, tool, decision, *, approved=False, status=None):
        record = {'call_id': call_id, 'tool': tool.name, 'source': tool.source,
                  'policy_origin': self.origin, **decision.as_dict(),
                  'approved': bool(approved) and decision.action == 'ask', 'status': status}
        if self.redactor:
            record = self.redactor.clean(record)
        self.records.append(copy.deepcopy(record))
        return copy.deepcopy(record)


def sensitive_path(path, exceptions=()):
    """所有文件入口共用；exceptions 必须由可信配置传入精确相对路径。"""
    parts = PurePosixPath(path).parts
    normalized = str(PurePosixPath(path))
    if '..' in parts or PurePosixPath(path).is_absolute():
        return True
    if normalized in exceptions:
        return False
    if any(part in {'.ssh', '.aws', '.azure', '.gnupg', '.gcloud', '.kube', '.codex', '.git'} for part in parts):
        return True
    name = parts[-1] if parts else ''
    if name == '.env.example':
        return False
    return (name == '.env' or name.startswith('.env.') or
            name in {'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'credentials', 'credentials.json'} or
            name.endswith(('.pem', '.key', '.p12', '.pfx')))
