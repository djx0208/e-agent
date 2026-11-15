# 第一章 让模型接口独立于应用

[上一章](00-architecture.md) · [目录](README.md) · [下一章](02-tools.md)

## 从一个确定性回复开始

今天先不联网。模型请求涉及费用、网络和不确定输出，直接从真实 API 开始会让我们分不清是框架错误还是模型行为变化。先实现一个按脚本回答的模型，就能稳定观察调用链。

`async def` 表示函数可以在等待网络或工具时让出执行权；`await` 等待它的结果。这里的模型虽然立即返回，仍使用异步接口，以后替换真实模型时不需要修改调用者。

以下是本章完整小例子。保存为 `examples/day01_model.py`，只依赖 Python 标准库。

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Action:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    actions: tuple[Action, ...] = ()
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


class Model(Protocol):
    async def complete(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class ScriptedModel:
    def __init__(self, replies: list[ModelReply]):
        self._replies = iter(replies)
        self.calls: list[list[Message]] = []

    async def complete(self, messages, tools):
        self.calls.append(list(messages))
        try:
            return next(self._replies)
        except StopIteration:
            raise RuntimeError("模型脚本已经用完") from None


async def main():
    model = ScriptedModel([ModelReply(text="你好，我是 e-agent。")])
    reply = await model.complete([Message("user", "介绍一下你自己")], [])
    print(reply.text)
    assert len(model.calls) == 1


if __name__ == "__main__":
    asyncio.run(main())
```

执行：

```bash
python3 examples/day01_model.py
```

预期输出：

```text
你好，我是 e-agent。
```

## 为什么现在就定义 Action

普通聊天只需要文本，但 Agent 还需要让模型提出操作。`ModelReply` 同时携带文本与 `actions`；工具的真实执行结果会在第二章引入。`finish_reason` 用于区分正常结束、工具调用、长度截断等情况，不能把所有无工具回复都当作成功。

`call_id` 将来连接一次工具请求和对应结果。它由模型适配器保留或生成，执行期间不能重新编号。`arguments` 仍是未验证输入，类型标注本身不会检查模型传来的 JSON。

## 把小例子拆成 SDK

完成独立例子后，将 `Message`、`Action`、`ModelReply` 移到 `packages/sdk/src/e_agent/models.py`，将 `Model`、`ScriptedModel` 移到 `llm.py`，保留示例中的 `main()`。第二章开始从这些模块导入，不再复制类。

每个测试实例创建自己的 ScriptedModel。它内部有游标，不能作为全局共享模型并发使用。真实模型适配器应当把会话消息放在请求里，不在适配器内保存全局聊天历史。

在第七章再补包元数据；开发中可临时设置 `PYTHONPATH=packages/sdk/src` 运行示例。目标 SDK 导入名为 `e_agent`，发行包名为 `e-agent-sdk`，二者不是同一个字符串。

## 模型适配器负责什么

未来适配器负责把内部消息与工具定义转换成供应商协议，将响应转换回 `ModelReply`，并映射网络错误、限流、上下文超限和用量。上层循环不读取供应商原始 JSON。

第一版 `complete()` 返回完整响应。不要把一次性返回的文本拆成字符后声称支持真实流式模型；后续可增加 `stream()` 协议，单独处理文本增量与工具参数片段的拼装。

## 验收与练习

连续调用脚本模型两次，验证顺序；第三次调用应明确报告脚本耗尽。让两个模型实例并行执行，确认不会串用脚本。

修改脚本，让它返回一个 `Action("call-1", "add", {"a": 2, "b": 3})`。今天只打印这个操作请求；第二章才实现执行。
