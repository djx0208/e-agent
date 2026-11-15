# 第三章 实现思考与执行的循环

[上一章](02-tools.md) · [目录](README.md) · [下一章](04-workspace.md)

## Agent 如何继续下一步

模型第一次提出加法请求，执行器返回 5，模型第二次看到 5 后回答用户。程序要把这几步连接起来，并判断何时结束。Agent 的核心循环就从这里开始。

先扩展 `Message`。保留 `role` 和 `content`，加入 `actions: tuple[Action, ...] = ()` 与 `call_id: str | None = None`。assistant 消息保存模型原始工具请求；tool 消息用 `call_id` 与请求关联。提供方适配器负责把内部字段转换为实际 API 字段。

一轮合法消息应是：

```text
user       请计算 2 + 3
assistant  actions=[call-1: add(a=2, b=3)]
tool       call_id=call-1, content={"ok":true,"output":"5"}
assistant  结果是 5
```

不能只把“5”插入一个 user 消息。这样会丢失工具调用关联关系，真实模型服务可能拒绝请求。

## 最小循环

下面是 `agent.py` 中的核心片段，使用前两章已经定义的协议。`RunResult` 包含 `status`、`text`、`model_turns` 三个字段，状态暂为 `completed`、`limit_reached`，失败由外层异常处理。

```python
import json


async def run_loop(model, executor, messages, max_turns=8):
    if max_turns < 1:
        raise ValueError("max_turns 必须为正数")
    for turn in range(1, max_turns + 1):
        reply = await model.complete(messages, executor.registry.definitions())
        if reply.finish_reason not in {"stop", "tool_calls"}:
            raise RuntimeError(f"模型未正常结束：{reply.finish_reason}")
        if reply.finish_reason == "tool_calls" and not reply.actions:
            raise RuntimeError("模型标记工具调用但未返回操作")
        ids = [action.call_id for action in reply.actions]
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise RuntimeError("工具调用 ID 为空或重复")
        messages.append(Message("assistant", reply.text, reply.actions))
        if not reply.actions:
            return RunResult("completed", reply.text, turn)
        for action in reply.actions:
            observation = await executor.execute(action)
            messages.append(Message(
                "tool",
                json.dumps({
                    "ok": observation.ok,
                    "output": observation.output,
                    "error_code": observation.error_code,
                }, ensure_ascii=False),
                call_id=action.call_id,
            ))
    return RunResult("limit_reached", "达到模型调用轮数上限", max_turns)
```

`messages` 是当前运行的可变副本，不是 Agent 的共享成员。第一版按顺序执行同轮工具。文件编辑与命令可能有先后依赖，只有明确独立的只读工具才适合以后并发执行。

本片段是第三章内存版；第五章会把消息变更改成“先记录事件，再更新投影”，第六章会增加运行终态和中断处理。

## 用脚本模型观察整个循环

给 ScriptedModel 两个回复：

```python
replies = [
    ModelReply(
        actions=(Action("call-1", "add", {"a": 2, "b": 3}),),
        finish_reason="tool_calls",
    ),
    ModelReply(text="结果是 5"),
]
```

组装注册表、执行器、初始 user 消息后调用 `run_loop()`。预期模型被调用两次，加法工具执行一次，返回 `completed`。检查第二次模型请求的历史，确认其中既有 assistant 的工具请求，也有匹配的 tool 结果。

接着把第一次请求中的 `a` 改成字符串，让模型脚本第二步返回修正后的请求，第三步给出答案。这样可以验证“看到失败并继续修复”的路径，而不依赖真实模型是否足够聪明。

## 完成的含义

`completed` 只表示模型正常结束这次循环，并不证明代码正确。测试是否通过，必须查看真实命令 Observation 的退出码。SDK 不应从“我已经修复了”的自然语言推导测试成功。

同样，达到轮数上限不能伪装成 completed。保留独立状态后，未来平台才能区分“正常结束”和“预算用完”。

## 本章实现文件

在 `models.py` 扩展 Message、增加 RunResult；在 `agent.py` 实现循环；在 `examples/day03_loop.py` 组装加法示例。暂时不加入 HTTP、持久化和终端输入，便于单独验证循环。

## 验收与练习

覆盖无工具直接回答、一次调用、多次调用、同轮多个工具、非法参数后修复、脚本耗尽、轮数耗尽、截断回复。工具调用 ID 在同一 assistant 消息内必须唯一，持久化后的动作键使用 `(run_id, turn, call_id)`，不假设跨运行全局唯一。

练习：给出一个永远请求工具的模型，设置 `max_turns=3`，确认模型最多被调用三次。
