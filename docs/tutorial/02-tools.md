# 第二章 把模型请求变成可验证的工具操作

[上一章](01-model.md) · [目录](README.md) · [下一章](03-agent-loop.md)

## 一次调用包含三件事

工具定义告诉模型“有什么能做”；Action 表示“这次想做什么”；Observation 表示“实际做成什么”。让这三者分开，模型才不会把计划误认为执行结果。

本章只实现加法工具，排除文件和进程的干扰。新增 `tools.py`，并在 `models.py` 增加 Observation。

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    call_id: str
    ok: bool
    output: str
    error_code: str | None = None


class ToolInputError(ValueError):
    pass


class AddTool:
    name = "add"
    description = "返回两个整数的和"
    parameters = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    }

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"a", "b"}:
            raise ToolInputError("只接受 a 和 b 两个参数")
        if any(type(arguments[key]) is not int for key in ("a", "b")):
            raise ToolInputError("a 和 b 必须是整数")
        return dict(arguments)

    async def execute(self, arguments, context) -> str:
        return str(arguments["a"] + arguments["b"])
```

这里特意不使用 `isinstance(x, int)`，因为 Python 的布尔值也是 `int` 的子类。模型传来 `true` 时，我们希望拒绝，而不是把它当作 1。

工具的 JSON Schema 用于描述参数，`validate()` 用于真正检查参数，两者必须一致。后续工具多了可以采用统一验证库自动生成 Schema；第一版用显式检查帮助理解协议。

## 工具注册与执行入口

定义 `Tool` 协议，包含 `name`、`description`、`parameters`、`validate()`、`execute()`。注册表在构造时检查重复名称；不允许后加入的工具静默覆盖已有工具。`definitions()` 返回供 Model 使用的统一结构：

```python
{"name": "add", "description": "返回两个整数的和", "parameters": {...}}
```

`Executor` 持有注册表与工具上下文。下面是它的 `execute()` 核心片段，Action 和 Observation 从 `models.py` 导入。

```python
async def execute(self, action):
    tool = self.registry.get(action.name)
    if tool is None:
        return Observation(action.call_id, False, "未知工具", "unknown_tool")
    try:
        arguments = tool.validate(action.arguments)
    except ToolInputError as exc:
        return Observation(action.call_id, False, str(exc), "invalid_arguments")
    try:
        output = await tool.execute(arguments, self.context)
    except OSError:
        return Observation(action.call_id, False, "工具执行失败", "io_error")
    return Observation(action.call_id, True, output)
```

输入错误返回给模型，让它有机会修正；意外编程错误应交给运行层记录并结束本次运行。不要用一个宽泛的 `except` 把所有错误都变成“工具失败”，那会掩盖实现 bug。第六章再补超时、取消和授权。

## 给工具一个上下文

现在 `context` 可以是空对象；第四章会变成 `ToolContext(workspace, conversation_id, run_id)`。工具通过上下文访问执行环境，不使用进程全局的当前目录。

`execute()` 返回字符串是教学阶段的简化。Observation 的公共结构已经预留错误码，后续可加入结构化数据、截断标记和产物引用；模型上下文只接收受控大小的输出。

## 完整组装步骤

1. 在 `models.py` 加入 Observation。
2. 在 `tools.py` 写 Tool 协议、ToolInputError、ToolRegistry、Executor。
3. 将 AddTool 放到示例模块，注册为唯一工具。
4. 构造 `Action("call-1", "add", {"a": 2, "b": 3})`，交给 Executor。
5. 打印 `call_id`、`ok`、`output`，预期为 `call-1 True 5`。

这是模型之外的一条独立执行链。即使不使用模型，也应能单独调用并测试工具。

## 验收与练习

输入字符串、缺少字段、多余字段、布尔值、未知工具，分别返回明确错误，且不调用工具主体。重复注册 `add` 时构造失败。所有 Observation 必须保留原始 `call_id`。

练习：增加 `multiply` 工具，确保不需要修改 Executor。这个检查比实现十个工具更能证明抽象是否合理。
