# 第五章 记录事件并恢复会话

[上一章](04-workspace.md) · [目录](README.md) · [下一章](06-control.md)

## 为什么聊天记录不够

一条“运行测试”的 assistant 消息无法说明进程是否启动、是否退出、输出是什么，也无法区分“尚未执行”和“执行后记录丢失”。未来平台需要这些事实，所以本章把事件日志作为会话的持久化依据。

Conversation 持有会话 ID、工作区绑定、事件存储和当前状态。Agent 只提供决策能力。新建会话和恢复会话都通过同一个 Conversation 对象运行。

## 定义稳定的事件信封

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    schema_version: int
    conversation_id: str
    run_id: str
    seq: int
    type: str
    timestamp: str
    payload: dict[str, Any]
```

`seq` 在同一 Conversation 内严格递增，用于增量读取；时间戳用于展示，不用来决定顺序。`schema_version` 支持未来格式迁移。不要把 Python 对象直接 pickle 到磁盘：事件应能被其他语言和未来平台读取。

基本事件如下：

| 事件 | 何时记录 | payload 关键字段 |
| --- | --- | --- |
| `run.started` | 接受本次运行后 | 模型配置摘要、预算、工作区 ID |
| `message.user` | 用户输入被接受 | 文本 |
| `message.assistant` | 模型回复被接受 | 文本、全部工具请求、结束原因 |
| `action.started` | 验证与策略通过、执行之前 | 动作键、参数摘要 |
| `action.finished` | 工具完成 | 动作键、Observation |
| `run.finished` | 到达终态 | status、reason、用量 |

密钥只存在于注入的配置或密钥服务中，不序列化进事件。工具输出也可能带敏感信息；平台阶段应增加访问控制和脱敏策略，完整日志与展示内容分开管理。

## 先使用 SQLite

本教程选择 Python 自带的 SQLite：它允许一次事务同时保存事件和会话版本，能直接按序号查询，也便于单进程测试。JSONL 可用于导出，但不拿普通文件追加承担并发状态更新。

核心表结构：

```sql
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT NOT NULL
);
CREATE TABLE events (
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
```

在 `store.py` 定义 `create()`、`append(conversation_id, expected_version, events)`、`read(conversation_id, after_seq)`。一次 append 的实现顺序是：开启事务，检查版本，分配连续序号，插入事件，更新版本，提交。版本不一致就回滚并返回冲突，不覆盖别人的事件。

SQLite 调用是同步的；小型教学程序可以限制事务大小后直接调用。服务端版使用专用存储线程或异步适配器，不能在请求事件循环中执行长查询。SQLite 连接的线程归属必须明确，不把默认连接随意交给 `to_thread()`。

## 从事件投影出模型消息

所谓投影，就是按序读取事件并重建需要的状态。只将 user、assistant 和工具结果转换成模型消息；运行状态、网页订阅事件不能混入模型聊天。

投影器按以下规则工作：

1. `message.user` 生成 user Message。
2. `message.assistant` 生成 assistant Message，并登记待完成动作。
3. `action.finished` 生成对应 tool Message，移除待完成动作。
4. `run.finished` 更新运行状态，不生成模型消息。

第三章直接 append 内存消息的位置，改为先持久化相关事件，再更新内存投影。事件提交成功以后才通知观察者；数据库写入失败时停止推进，不能继续执行新的外部动作。

## 恢复不等于重新执行

假设已经保存 `action.started`，工具创建了文件，随后进程崩溃，没有保存 `action.finished`。恢复时只能确认执行结果未知，不能自动再创建一次。

恢复程序应将没有终态的旧 Run 标记为 `interrupted`，并列出未完成动作。对未记录结果的动作生成明确的恢复 Observation：`outcome_unknown` 表示可能执行过；若连 `action.started` 都没有，则是 `not_executed`。这些恢复记录要保留原因及原始动作键。

在所有待完成调用都有对应结果以后，才允许开始新的模型轮次。恢复记录不能伪造成功输出。需要确认外部状态时，开启新 Run 使用只读工具检查，再由用户或策略决定是否重试。

未来远程执行器可以根据动作键提供幂等缓存，但任意 shell 命令无法普遍保证 exactly-once。事件回放只重建状态，不触发工具。

## 实现与演示

新增 `events.py`、`store.py`、`conversation.py`，将 `run_loop()` 接入事件写入回调。在 `examples/day05_resume.py` 中第一次创建会话并运行，第二次使用已保存的会话 ID 重新加载历史并发送新需求。

再增加故障注入：让执行器在真实动作完成后、保存结果前抛出异常。重新加载时必须显示结果未知，且工具调用计数不能自动增加。

## 验收与练习

同一会话 seq 连续；按 `after_seq` 读取没有遗漏；版本冲突不产生部分写入；重建历史与原消息一致；重复读取不执行工具。用未知 `schema_version` 的事件加载时，应明确拒绝或走迁移逻辑，不能静默忽略。

本章只保证单进程内每个会话一个活动 Run。数据库乐观锁并不能替代分布式任务租约，这部分留到平台阶段。
