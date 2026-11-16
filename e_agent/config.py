"""模型配置只从用户文件和环境读取，不从项目内容推断。"""
from dataclasses import dataclass, field, asdict
import json
import math
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
import uuid


class ConfigError(Exception):
    """错误信息不包含配置值或底层异常正文。"""


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = ''
    model: str = ''
    api_key: str = field(default='', repr=False)
    timeout: float = 60.0

    def validate(self):
        for name in ('base_url', 'model', 'api_key'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError('模型配置缺少必要字段：' + name)
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ConfigError('模型配置字段含无效控制字符')
        if not self.api_key.isascii() or self.api_key != self.api_key.strip():
            raise ConfigError('API key 必须是无首尾空白的 ASCII 文本')
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in ('https', 'http') and url.hostname and url.port != 0
                     and not url.username and not url.password and not url.query and not url.fragment
                     and not any(c.isspace() for c in self.base_url)
                     and not url.path.rstrip('/').endswith('/chat/completions'))
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError('基础地址无效；应为 HTTP(S) 地址，不含凭据、查询参数或 /chat/completions')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigError('timeout 必须是大于零的有限数字')


def default_config_path(environ=None):
    env = os.environ if environ is None else environ
    base = Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser()
    if not base.is_absolute():
        raise ConfigError('XDG_CONFIG_HOME 必须是绝对路径')
    return base / 'e-agent' / 'model.json'


def _parent_fd(path, create=False):
    """逐层使用目录描述符，不跟随任何目录链接。调用者关闭返回的 fd。"""
    path = Path(os.path.abspath(Path(path).expanduser()))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _read_file(path):
    parent = None
    try:
        parent, name = _parent_fd(path)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError('模型配置必须是当前用户拥有的普通文件')
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ConfigError('模型配置权限过宽，请设置为 600')
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ConfigError('模型配置超过大小限制')
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - {'base_url', 'model', 'api_key', 'timeout'}:
            raise ConfigError('模型配置格式无效')
        for key in ('base_url', 'model', 'api_key'):
            if key in data and not isinstance(data[key], str):
                raise ConfigError('模型配置字段类型无效')
        if 'timeout' in data and (isinstance(data['timeout'], bool) or not isinstance(data['timeout'], (int, float))):
            raise ConfigError('模型配置超时字段类型无效')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError('无法安全读取模型配置，请检查文件格式、权限和链接') from None
    finally:
        if parent is not None:
            os.close(parent)


def load_config(path=None, environ=None):
    env = os.environ if environ is None else environ
    data = _read_file(path if path is not None else default_config_path(env))
    for field_name, variable in [('base_url', 'E_AGENT_BASE_URL'), ('model', 'E_AGENT_MODEL'), ('api_key', 'E_AGENT_API_KEY')]:
        if variable in env:
            data[field_name] = env[variable]
    return ModelConfig(**data)


def save_config(config, path=None):
    """原子保存为 600；仅修改配置所在目录的权限为 700。"""
    config.validate()
    target = Path(path) if path is not None else default_config_path()
    parent = None
    temporary = None
    try:
        parent, name = _parent_fd(target, create=True)
        if os.fstat(parent).st_uid != os.getuid():
            raise ConfigError('配置目录不属于当前用户')
        os.fchmod(parent, 0o700)
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                raise ConfigError('拒绝覆盖链接、特殊文件或其他用户的配置')
        except FileNotFoundError:
            pass
        temporary = '.model-' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(asdict(config), stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # 重新检查目标类型，避免等待或写入期间出现的链接被当成普通配置。
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError('配置目标已经变化，拒绝替换')
        except FileNotFoundError:
            pass
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
    except (OSError, ValueError, TypeError):
        raise ConfigError('模型配置保存失败，未完成配置') from None
    finally:
        if temporary is not None and parent is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        if parent is not None:
            os.close(parent)
