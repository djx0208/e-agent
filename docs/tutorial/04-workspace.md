# 第四章 让 Agent 读取代码并运行测试

[上一章](03-agent-loop.md) · [目录](README.md) · [下一章](05-events.md)

## 从加法工具走向真实代码

前一章已经能执行工具，但加法不会改变外部世界。现在增加读文件、精确编辑和运行命令，让同一循环修复一个练习项目。

先定义 Workspace 协议，再实现 LocalWorkspace。模型只看到工具参数，不知道宿主机绝对路径；工具通过上下文绑定的 Workspace 执行。未来替换容器实现时，Agent 循环无需改动。

## 先定义执行边界

在 SDK 的 `workspace.py` 定义协议，在 `e_agent_workspace/local.py` 实现本地版本。下面是目标接口；`CommandResult` 需要包含 `exit_code`、`output`、`truncated`、`timed_out`。

```python
from typing import Protocol


class Workspace(Protocol):
    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str, *, create_only: bool) -> None: ...

    async def replace_text(
        self, path: str, old: str, new: str, *, expected_sha256: str
    ) -> None: ...

    async def run(self, argv: list[str], timeout: float) -> "CommandResult": ...

    async def close(self) -> None: ...
```

`LocalWorkspace(root)` 在构造时固定根目录；SDK 不调用 `os.chdir()`。不同会话通过不同 Workspace 操作自己的目录。

文件路径解析的核心片段：

```python
from pathlib import Path


def resolve_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("只接受工作区内的相对路径")
    root = root.resolve(strict=True)
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("路径超出工作区")
    return target
```

这能拒绝普通路径穿越与当前可见的外链符号链接，但存在检查与使用之间的竞态。它也不能限制一个任意 Python 命令访问宿主机文件。本地 Workspace 是可信开发环境，不能用 `cwd` 和路径检查宣称完成沙箱隔离。进阶章用独立容器提供更强执行边界。

## 精确编辑优先于覆盖

让读文件工具返回内容与 SHA-256。编辑工具接受 `path`、`old`、`new` 和 `expected_sha256`，按下面顺序操作：

1. 解析路径并读取当前字节，拒绝超出限制的文件。
2. 比对当前 SHA-256，不匹配说明文件已变化，返回 `stale_file`。
3. 按 UTF-8 解码；要求 `old` 非空且恰好出现一次。
4. 替换一次，写入同目录临时文件，保留原文件权限。
5. 在 Workspace 写锁内再次检查源文件状态，再使用原子替换提交。
6. 返回修改摘要与新哈希。

同进程写锁只能约束本 SDK 的写入者。外部编辑器仍可能并发修改；遇到冲突应返回失败并重新读取，不能承诺对任意外部写入严格无竞态。平台模式通过独占工作区或更强文件服务约束写入者。

新建文件用 `create_only=True`，通过独占创建避免覆盖已有文件。需要覆盖时应使用显式编辑操作，让修改依据可以审查。

## 执行命令时要处理整个生命周期

第一版命令工具接收 `argv` 数组，例如 `["python", "-m", "unittest", "-v"]`，通过 `asyncio.create_subprocess_exec()` 启动。不要把参数拼接成 shell 字符串；需要 shell 时以后增加显式工具和对应策略。

下面是 POSIX 环境的核心结构，辅助函数按后面的契约实现：

```python
async def run(self, argv, timeout):
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=self.root,
        env=self.command_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    collector = asyncio.create_task(drain_bounded(process.stdout, 64 * 1024))
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            output, truncated = await asyncio.shield(collector)
        return CommandResult(process.returncode, output, truncated, False)
    except TimeoutError:
        await terminate_group(process)
        output, truncated = await finish_collection(collector)
        return CommandResult(process.returncode, output, truncated, True)
    except asyncio.CancelledError:
        await terminate_group(process)
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
        raise
```

`asyncio.shield(collector)` 避免外层超时直接取消输出收集。`finish_collection()` 使用独立的短超时等待收集完成；若仍未结束，取消并回收收集任务，返回已收集片段并标记截断。因此收集器应持有可读取的缓冲状态，而不是只在 EOF 时暴露结果。意外异常也需要用相同的清理逻辑停止进程和收集任务；实现时在上述分支之后增加清理后重新抛出的异常分支。

`drain_bounded()` 每次读取有限字节，持续排空管道，仅保留前 64 KiB，超限标记 `truncated=True`。达到保留上限后也要继续读取，否则子进程会因管道堵塞而挂起。不要先 `communicate()` 读完整日志，再截断显示；那仍可能消耗无限内存。[Python 子进程文档](https://docs.python.org/3.11/library/asyncio-subprocess.html)

`terminate_group()` 向启动时创建的进程组发送 TERM，短暂等待后发送 KILL，并回收直接子进程；进程已退出时容忍 `ProcessLookupError`。后代若主动脱离进程组，本地模式无法可靠回收，容器模式应停止整个执行单元。命令环境采用明确的变量集合，不能自动把模型密钥传给被执行的代码。

## 组装四个代码工具

在 `e_agent_tools` 实现以下工具，参数验证沿用第二章流程：

| 工具 | 参数与返回 | 操作特征 |
| --- | --- | --- |
| `read_file` | 相对路径；返回内容和哈希 | 限制文件大小，拒绝二进制内容 |
| `write_file` | 相对路径、内容；仅新建 | 返回路径和哈希 |
| `edit_file` | 路径、原文、新文、旧哈希 | 精确替换，冲突明确失败 |
| `run_command` | 非空 argv、超时 | 返回退出码、输出、超时与截断标记 |

`run_command` 的结果不能丢掉 `exit_code`；工具本身成功启动并不代表命令成功。代码搜索先通过命令完成，后续再提供专门的搜索工具。

## 创建练习项目

在临时目录创建 `calculator.py`：

```python
def add(a, b):
    return a - b
```

再创建 `test_calculator.py`：

```python
import unittest
from calculator import add


class CalculatorTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
```

先手动执行 `python -m unittest -v`，确认失败。使用临时目录是为了反复复原初始状态，不污染 SDK 仓库。

脚本模型依次请求：运行测试、读取文件、提交精确编辑、再次测试、返回总结。读取结果中的哈希由测试脚本根据已知原始内容计算，真实模型则从 Observation 中获取。将这条流程放入 `examples/day04_fix_bug.py`。

## 验收与练习

最终函数应为 `a + b`，第二次命令退出码为 0。另测父目录路径、指向外部的符号链接、重复匹配、文件已变化、超时命令、大量日志和任务取消。

本章最终检查的对象是临时目录里的文件和真实测试结果，不是模型说了什么。
