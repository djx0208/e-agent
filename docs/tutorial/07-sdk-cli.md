# 第七章 交付独立 SDK 与命令行 Agent

[上一章](06-control.md) · [目录](README.md) · [进阶章](08-server.md)

## 用外部调用方式检验设计

如果一个 Python 程序想使用 Agent，还需要导入 CLI 的全局变量，说明 SDK 尚未独立。本章先固定公共接口，再把已有实现组装成可安装的包。

下面是目标 SDK 的最小调用方式。它在本章实现后运行，目前是接口验收示例：

```python
import asyncio
from e_agent import Agent, Conversation, MemoryStore, ModelReply, ScriptedModel


async def main():
    agent = Agent(
        model=ScriptedModel([ModelReply(text="SDK 可以独立工作。")]),
        tools=[],
    )
    async with Conversation.create(
        agent=agent,
        workspace=None,
        store=MemoryStore(),
    ) as conversation:
        result = await conversation.run("介绍这个 SDK")
        print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
```

`create()` 返回异步上下文管理器，在进入时完成存储初始化。无工具会话允许 `workspace=None`；使用代码工具时必须绑定 Workspace。MemoryStore 遵守第五章的事件存储协议，但不跨进程持久化。

## 固定公共接口的语义

| 接口 | 约定 |
| --- | --- |
| `Agent(model, tools, system_prompt, policy, limits)` | 保存配置，不保存某个会话的消息；后四项提供明确默认值 |
| `Conversation.create(...)` | 新建并持久化会话标识 |
| `Conversation.load(id, agent, workspace, store)` | 恢复已有会话，校验工作区绑定，处理未结束运行 |
| `await conversation.run(prompt)` | 接受新输入，开始一次 Run；同会话忙时抛 BusyError |
| `await conversation.cancel()` | 幂等请求取消，并等待清理完成 |
| `await conversation.approve(id, decision)` | 只处理匹配、未过期的确认请求 |
| `await conversation.read_events(after_seq=0)` | 返回按 seq 排序的持久化事件 |
| `await conversation.close()` | 取消本对象拥有的活动运行，关闭订阅与内部资源 |

RunResult 的最终字段为 `conversation_id`、`run_id`、`status`、`text`、`model_turns`、`usage`、`reason`。公共错误至少包括 BusyError、ConfigurationError、ModelError、StoreError、WorkspaceError。工具可预期错误仍转成 Observation。

资源所有权要写在接口说明中：调用者注入的 Model、Store 和 Workspace 由调用者关闭；Conversation 只关闭自己创建的运行任务和订阅。可在未来提供“拥有资源”的便捷工厂，但不能在一个会话结束时关闭其他会话共享的 HTTP 客户端。

## 接入真实模型

在 SDK 增加可选适配器 `ChatCompletionsModel`，接收调用者提供的 `httpx.AsyncClient`、模型名、API 基础地址和密钥。HTTPX 的异步客户端应在适当作用域复用并关闭，不在每轮循环里创建新连接池。[HTTPX 异步文档](https://www.python-httpx.org/async/)

选择 Chat Completions 风格协议是教学适配器的范围限制，不代表所有供应商兼容。需要支持的消息转换如下：[协议参考](https://developers.openai.com/api/reference/resources/chat)

| 内部数据 | HTTP 请求或响应字段 |
| --- | --- |
| assistant Message.actions | `tool_calls`，每项含 `id`、`type=function`、`function.name`、JSON 字符串 `function.arguments` |
| tool Message.call_id | `tool_call_id` |
| Tool 定义 | `tools[].type=function` 与 `tools[].function` |
| ModelReply.text | `choices[0].message.content` |
| ModelReply.actions | `choices[0].message.tool_calls` |
| finish_reason 与 usage | 保留原始结束原因，映射供应商 token 字段 |

响应转换核心片段：

```python
import json


def decode_reply(body):
    choice = body["choices"][0]
    message = choice["message"]
    actions = []
    for call in message.get("tool_calls") or []:
        if call.get("type") != "function":
            raise ValueError("不支持的工具调用类型")
        function = call["function"]
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        actions.append(Action(call["id"], function["name"], arguments))
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("当前适配器仅支持文本回复")
    usage = body.get("usage") or {}
    return ModelReply(
        text=content or "",
        actions=tuple(actions),
        finish_reason=choice["finish_reason"],
        usage={key: value for key, value in usage.items() if type(value) is int},
    )
```

完整适配器还需做以下步骤：验证基础 URL 与配置；将内部消息编码；POST 到基础 URL 下的 `/chat/completions`；配置连接和读取超时；检查 HTTP 状态；限制响应大小；调用解码器；把结构错误映射为 ModelError。空 choices、非法 JSON、缺失调用 ID 和非正常结束都必须可测试。

模型调用的有限重试只包住模型 HTTP 请求，不能包住整个 Agent 循环。已执行的工具不能因为一次网络失败被重复执行。第一版不自动重试；后续针对限流与临时服务错误增加有上限的退避，并记录次数。

在 `examples/real_model.py` 中从环境变量读取 `E_AGENT_MODEL`、`E_AGENT_BASE_URL`、`E_AGENT_API_KEY`，配置同一套代码工具。真实模型验收复用第四章练习；默认自动测试仍使用 ScriptedModel 与 HTTP 模拟响应。

## 把 SDK 变成可安装的包

SDK 的 `pyproject.toml` 可以先采用以下配置。包名是本项目的计划名称，尚未核验公共包索引的名称占用，也未发布。

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "e-agent-sdk"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
http = ["httpx>=0.27,<1"]

[tool.setuptools.packages.find]
where = ["src"]
```

核心包仅依赖标准库，HTTP 提供方通过可选依赖安装。tools、workspace、cli 各有独立元数据和对 SDK 的版本依赖。项目实现时提交锁文件以固定教程开发环境；教程不依赖“永远安装最新版本”。

完成包目录后执行以下命令；新建虚拟环境前检查本机 Python 版本，Linux 系统通常使用 `python3`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './packages/sdk[http]'
python -m pip install -e ./packages/tools -e ./packages/workspace -e ./packages/cli
```

在 `e_agent/__init__.py` 只导出稳定公共类型。HTTP 可选模块不能在基础导入时强制加载，否则用户未安装 `http` 扩展就无法使用 ScriptedModel。

## CLI 只负责交互

命令行入口用 `argparse`，第一版支持 `--workspace`、`--model`、`--base-url`、`--resume`、`--max-turns` 和 `--json`。密钥从环境或受控配置读取，不放在命令参数里。

CLI 的顺序是解析配置、组装模型与工具、创建或恢复 Conversation、启动 Run、展示事件、清理资源。它不再自己写一份模型工具循环。

要同时展示事件和处理确认，主协程负责消费事件与读取用户操作，运行任务在另一个 asyncio Task 中执行。文本输入可以通过终端适配层处理，不能让同步 `input()` 阻塞整个事件循环。非交互 JSON 模式没有确认通道时默认拒绝 ask，不永远等待。

`--json` 按行输出事件，诊断信息写 stderr，便于其他程序消费。第一次 Ctrl+C 请求取消；退出前等待清理。运行完成但练习测试失败时，CLI 不能自动宣称修复成功。

## 验收与下一步

在干净虚拟环境中单独安装 SDK，运行无工具示例；再安装工具和 Workspace 运行修复示例。确认 SDK 不依赖 CLI、Web 框架和 Docker；从仓库以外的目录运行，不能靠工作目录碰巧导入源码。

七章完成后得到本地 SDK 和 CLI。下一章把相同接口接到远程服务，为管理平台提供运行与事件能力。
