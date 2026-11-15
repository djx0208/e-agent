# 第六章 管理取消确认与上下文

[上一章](05-events.md) · [目录](README.md) · [下一章](07-sdk-cli.md)

## 把一次执行变成可管理的 Run

到目前为止，程序可以自动修复代码并保存历史。接下来解决实际使用中会遇到的问题：任务何时停止、操作是否被允许、上下文装不下怎么办。

把第三章循环包在 Conversation 的运行生命周期内；每次 `run()` 产生独立 `run_id`，返回完整 RunResult。

```text
created → running → completed
                  → failed
                  → cancelled
                  → limit_reached
                  → waiting_confirmation → running
                                         → cancelled
                                         → limit_reached
恢复发现旧运行未结束 → interrupted
```

一次 Run 只能记录一个 `run.finished`。Conversation 可以继续接收下一次 Run，旧 Run 不重新变回 running。等待确认不是终态。

## 取消是逐层传播的

`Conversation.run()` 内部创建并持有运行任务；外部 `cancel()` 发出取消请求并等待清理完成。模型 HTTP 请求、工具调用、子进程与输出收集任务都需要响应取消。

核心生命周期片段如下，`record_terminal()` 应保证终态唯一：

```python
try:
    result = await run_loop(...)
except asyncio.CancelledError:
    await cleanup_active_execution()
    await record_terminal("cancelled", "用户取消")
    raise
except Exception as exc:
    await cleanup_active_execution()
    await record_terminal("failed", classify_error(exc))
    raise
else:
    await record_terminal(result.status, result.text)
```

这是展示清理顺序的片段。完整实现应在创建运行任务时集中接管取消，让 `cancel()` 幂等；必要清理使用独立、限时的清理任务，防止重复取消打断回收。数据库不可用时不能承诺成功保存终态；重启时由第五章恢复逻辑标记 interrupted。

对外约定：`cancel()` 正常返回意味着执行已停止或已处于终态；如果无法确认远端停止，应返回错误或待确认状态，不能提前显示 cancelled。`run()` 被取消后向直接调用者抛出 `CancelledError`，状态可从事件读取。

## 在执行之前做策略判断

策略接收已验证的 Action、工具元数据与 Workspace 信息，返回 `allow`、`deny` 或 `ask`。策略实现由应用注入，SDK 不读取终端输入。

```python
class Policy(Protocol):
    async def decide(self, action, context) -> str: ...
```

示例开发策略允许读文件，对编辑和命令请求确认。测试使用明确的允许策略，以免自动验收卡在终端。开放执行权限必须是调用者配置的结果，不能由模型文本要求修改。

`ask` 流程先持久化 `confirmation.requested`，包括 `confirmation_id`、动作键和参数哈希，再等待 `approve(confirmation_id, decision)`。CLI 展示参数并读取输入，远程版通过 API 提交决定。批准只适用于这一个具体动作，修改参数后必须重新判断。

拒绝后为该动作产生 `denied` Observation，让模型可以选择别的办法。等待确认可被取消，也受运行总时长限制。进程重启后的旧确认默认失效，由恢复过程结束旧 Run；不自动继承过期授权。

## 把预算变成显式配置

定义 `RunLimits`：

| 字段 | 含义 | 检查位置 |
| --- | --- | --- |
| `max_turns` | 模型调用轮数上限 | 每次调用前 |
| `max_actions` | 本次 Run 的工具动作上限 | 每个动作执行前 |
| `max_seconds` | 包含等待确认在内的总时长 | 外层 deadline |
| `tool_timeout` | 单次工具最长执行时间 | 执行器 |
| `max_output_bytes` | 返回模型的工具输出大小 | Workspace 与工具 |

真实模型支持时记录输入、输出 token。费用依赖模型定价，不知道价格就保留未知值，不把缺失用量当成 0。预算不能在外部动作已经发生后才检查。

同轮剩余动作因预算中止时，需要为未执行调用记录 `not_executed` Observation，保持消息配对完整；已执行但状态不明的调用使用 `outcome_unknown`。保留这一区别，恢复后的模型才能正确判断下一步。

## 上下文窗口与事件历史分离

事件日志保留完整事实，发给模型的 ContextView 可以精简。不能为了节约 token 删除持久化历史，也不能直接切掉最后 N 条消息，因为可能留下没有工具请求的结果。

第一版采用明确的分组策略：system 指令固定保留；一个 assistant 工具请求及其全部结果视为不可拆分的组；保留最新用户目标与最近完整组；旧组压缩成摘要，摘要记录覆盖的事件 seq 范围。

摘要至少包含用户目标、已改文件、已运行命令及结果、已知失败、待办事项。模型生成摘要也可能出错，所以最近的精确测试结果与未解决动作不能只存在于摘要里。摘要失败时使用受限的确定性摘要或明确返回 context_limit，不循环重试到耗尽预算。

提供方有 token 计数器就使用它；没有时只能做保守估算并处理服务端超限错误。字符数除以固定常数不是可靠的通用 token 计算方法。

## 项目指令与扩展

第一版显式加载工作区根目录的 `AGENTS.md`，限制大小，记录它的内容哈希和来源；不自动读取所有子目录中的指令文件。将它作为项目上下文传给模型，执行权限仍由 Policy 控制。

Skills 后续扩展为按需加载的说明文档；MCP 后续适配到工具协议。两者都通过已有工具与上下文边界接入，不改变主循环。主线不要求实现插件市场。

## 验收与练习

覆盖模型等待时取消、命令执行时取消、等待确认时取消、重复取消、拒绝后模型继续、预算用尽、摘要失败。验证每个 Run 只有一个终态，同一 Conversation 的并发 `run()` 直接返回 busy，不悄悄并行修改历史。

创建两段 Conversation 并发运行，确认状态和 Workspace 不串用。这个小实验为后续平台管理多个任务奠定基础。
