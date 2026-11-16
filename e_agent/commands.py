"""Linux 本地命令执行；固定目录不构成系统隔离。"""
import asyncio
import copy
import math
import os
from pathlib import Path
import signal
import time


BOUNDARY = '本地命令使用当前用户权限，仍可访问项目外资源和网络；固定 cwd 不是沙箱。'
# 不继承 HOME、代理、加载器、Python 路径或模型凭据。
ENVIRONMENT = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}


def _group_alive(group):
    """僵尸已停止执行，不能由非父进程回收，不将其当作活进程。"""
    try:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
        return False
    except (OSError, ValueError, IndexError):
        # 不可观察时不声称已停止。
        return True


async def _stop(process, reader, grace):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if process.returncode is not None and not _group_alive(process.pid) and reader.done():
                await process.wait()
                return True
            await asyncio.sleep(0.02)
    return False


async def _finish_cleanup(process, reader, grace):
    # 二次 Ctrl+C 不能打断进程清理；清理本身有期限。
    task = asyncio.create_task(_stop(process, reader, grace))
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue


async def run_command(project, argv, timeout=60, output_limit=65536, redactor=None, *, cleanup_grace=0.5):
    if (not isinstance(argv, list) or not argv or len(argv) > 256
            or any(not isinstance(x, str) or '\0' in x for x in argv) or not argv[0]):
        raise ValueError('命令必须是非空 argv 字符串数组。')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise ValueError('命令超时须在 0 到 600 秒之间。')
    if type(output_limit) is not int or not 0 < output_limit <= 65536:
        raise ValueError('输出上限须在 1 到 65536 字节之间。')
    if not isinstance(cleanup_grace, (float, int)) or not 0 < cleanup_grace <= 5:
        raise ValueError('清理期限无效。')
    cwd = str(Path(project).resolve(strict=True))
    if not Path(cwd).is_dir():
        raise ValueError('项目目录不存在。')
    # 超过显示边界仍保留最长凭据长度，避免截断先于脱敏。
    extra = max((len(s.encode()) for s in getattr(redactor, 'secrets', ())), default=0)
    retained = bytearray()
    total = 0
    cancelled = False
    timed_out = False
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=dict(ENVIRONMENT), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, limit=16384))
    while True:
        try:
            process = await asyncio.shield(spawn)
            break
        except asyncio.CancelledError:
            cancelled = True
        except (OSError, ValueError):
            return {'status': 'error', 'exit_code': None, 'output': '无法启动命令。',
                    'timed_out': False, 'truncated': False}

    async def drain():
        nonlocal total
        while chunk := await process.stdout.read(8192):
            total += len(chunk)
            room = output_limit + extra - len(retained)
            if room > 0:
                retained.extend(chunk[:room])

    reader = asyncio.create_task(drain())
    try:
        if not cancelled:
            async def wait_all():
                await process.wait()
                await asyncio.shield(reader)
            try:
                await asyncio.wait_for(wait_all(), timeout)
            except asyncio.TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                cancelled = True
    finally:
        stopped = await _finish_cleanup(process, reader, cleanup_grace)
        if not stopped:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            # 关闭挂起的 pipe transport；没有停止保证时返回 unknown。
            process._transport.close()
    output = retained.decode('utf-8', errors='replace')
    if redactor is not None:
        output = redactor.text(output)
    encoded = output.encode('utf-8')
    truncated = total > output_limit or len(encoded) > output_limit
    output = encoded[:output_limit].decode('utf-8', errors='ignore')
    status = ('outcome_unknown' if not stopped else 'cancelled' if cancelled else
              'timeout' if timed_out else 'success' if process.returncode == 0 else 'error')
    return {'status': status, 'exit_code': process.returncode, 'output': output,
            'timed_out': timed_out, 'truncated': truncated}


class CommandOutcomeUnknown(RuntimeError):
    outcome_unknown = True
    def __init__(self, result):
        super().__init__('无法确认命令进程已停止；本轮不能继续。')
        self.result = result


class CommandCancelled(asyncio.CancelledError):
    def __init__(self, result):
        super().__init__('命令已取消，受管理进程已清理。')
        self.result = result


def command_tool(project, redactor=None):
    """通过统一注册表注册；调用者仍须经 Dispatcher 策略及逐次确认。"""
    from .tools import Tool, PreparedOperation
    cwd = str(Path(project).resolve(strict=True))

    def prepare(arguments):
        bound = copy.deepcopy(arguments)
        info = os.stat(cwd, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        def recheck():
            current = os.stat(cwd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != identity or Path(cwd).resolve() != Path(cwd):
                raise ValueError('项目目录在确认期间变化，请重新申请批准。')
        async def execute():
            recheck()
            result = await run_command(cwd, redactor=redactor, **bound)
            if result['status'] == 'outcome_unknown':
                raise CommandOutcomeUnknown(result)
            if result['status'] == 'cancelled':
                raise CommandCancelled(result)
            if result['status'] == 'timeout':
                result['status'] = 'error'
            return result
        target = {'argv': copy.deepcopy(bound['argv']), 'cwd': cwd,
                  'timeout': bound.get('timeout', 60), 'directory_version': list(identity),
                  'environment_names': sorted(ENVIRONMENT), 'boundary': BOUNDARY}
        return PreparedOperation(target=target, execute=execute, recheck=recheck)

    async def execute(arguments):
        return await prepare(arguments).execute()
    return Tool(name='run_command', description='在项目目录执行 argv 命令，返回真实退出码；需要逐次批准。',
                schema={'type': 'object', 'properties': {
                    'argv': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 256},
                    'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600}},
                    'required': ['argv'], 'additionalProperties': False},
                source='builtin', execute=execute, prepare=prepare)


def register_command_tool(registry, project, redactor=None):
    registry.register(command_tool(project, redactor))
