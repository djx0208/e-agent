# 参考资料与本地源码导航

[教程目录](README.md)

## 已下载的参考仓库

下载日期为 2026 年 11 月 15 日，使用浅克隆保留当前源码及对应提交。目录在本项目 `.references/` 下，已加入 Git 忽略规则，不会随 e-agent 一起提交。源码已下载；未安装参考项目依赖，也未启动它们的服务。

| 项目 | 本地入口 | 固定提交 |
| --- | --- | --- |
| OpenHands 应用 | [README](../../.references/openhands/README.md) | `951dcf29ac076d009ba2fc9e3d693d63c2823e7f` |
| Software Agent SDK | [README](../../.references/software-agent-sdk/README.md) | `aab6aa5b1e4a3a4da2c505697fe6e4a2283e4560` |

固定版本的远程入口：[OpenHands](https://github.com/OpenHands/OpenHands/tree/951dcf29ac076d009ba2fc9e3d693d63c2823e7f)、[Software Agent SDK](https://github.com/OpenHands/software-agent-sdk/tree/aab6aa5b1e4a3a4da2c505697fe6e4a2283e4560)。其他开发者未下载 `.references/` 时，可通过这些链接阅读同一版本。

当前 OpenHands 主仓库的应用是 Agent Canvas；Python Agent 核心、工具、Workspace 和 Agent Server 位于 Software Agent SDK 仓库。读 Agent 原理优先进入 SDK，研究管理平台时再读主仓库。[仓库边界说明](https://github.com/OpenHands/software-agent-sdk/blob/aab6aa5b1e4a3a4da2c505697fe6e4a2283e4560/README.md#repository-boundaries)

## 按教程章节读源码

下面的路径以 `.references/software-agent-sdk/` 为根。先理解公开接口和测试，再追内部实现；不要把整个上游框架直接复制到教程中。

| 教程主题 | 源码入口 | 阅读问题 |
| --- | --- | --- |
| 模型与消息 | `openhands-sdk/openhands/sdk/llm/message.py`、`llm/llm.py` | 模型协议如何与内部消息分离 |
| 工具 | `openhands-sdk/openhands/sdk/tool/schema.py`、`tool/tool.py`、`tool/registry.py` | 参数、结果和执行器如何连接 |
| Agent 循环 | `openhands-sdk/openhands/sdk/agent/base.py`、`agent/agent.py` | 哪些行为属于 Agent，哪些状态属于会话 |
| 文件工具 | `openhands-tools/openhands/tools/file_editor/definition.py`、`file_editor/impl.py` | 参数定义如何与真实文件修改分离 |
| 命令工具 | `openhands-tools/openhands/tools/terminal/definition.py`、`terminal/impl.py` | 如何描述退出、超时、后台执行 |
| 事件与恢复 | `openhands-sdk/openhands/sdk/event/base.py`、`conversation/base.py` | 事件如何承载事实，会话如何持有状态 |
| 执行策略 | `openhands-sdk/openhands/sdk/security/confirmation_policy.py` | 操作授权如何与模型决策分开 |
| 本地环境 | `openhands-sdk/openhands/sdk/workspace/local.py` | 本地环境有哪些能力与限制 |
| 容器环境 | `openhands-workspace/openhands/workspace/docker/workspace.py` | 执行资源如何创建与回收 |
| 远程服务 | `openhands-agent-server/openhands/agent_server/api.py`、`conversation_service.py` | HTTP 服务如何调用会话能力 |

表中第二个简写路径与同单元格第一个文件共享相应源码根。例如 `llm/llm.py` 的完整路径为 `openhands-sdk/openhands/sdk/llm/llm.py`。

本教程中的 SQLite 存储、Run 状态、HTTP 路径、异步公共接口都是 e-agent 的设计选择，不声称与上游内部实现相同。借鉴概念与职责边界，保留可教学、可验证的实现规模。

## 后续同步方式

浅克隆足够阅读当前源码；需要历史时再执行 `git fetch --unshallow`。更新前检查参考仓库是否有本地改动，记录旧提交，再获取新版本并更新本页。不要在实现过程中默默跟随上游最新代码，否则教程引用会漂移。

如从上游复制具体代码，应保留适用的版权与许可证声明；本教程当前代码片段为独立教学实现。发布 e-agent 的许可证需要单独确定。

## 教学与协议资料

- [极客兔兔 7 天系列官方仓库](https://github.com/geektutu/7days-golang)：借鉴循序渐进、每章动手验证的组织方式。
- [Gee 第一天](https://geektutu.com/post/gee-day1.html)：从标准接口与最小示例开始，逐步构建框架。
- [OpenHands SDK 架构](https://docs.openhands.dev/sdk/arch/overview)：理解 SDK、工具、Workspace、Server 的边界。
- [OpenHands SDK 入门](https://docs.openhands.dev/sdk/getting-started)：观察公开调用方式。
- [Python 3.11 异步子进程](https://docs.python.org/3.11/library/asyncio-subprocess.html)：实现命令生命周期时核对行为。
- [HTTPX 异步客户端](https://www.python-httpx.org/async/)：实现模型网络适配器。
- [Chat 协议参考](https://developers.openai.com/api/reference/resources/chat)：核对教学适配器的消息与工具字段。

线上文档会演进；涉及源代码细节时以本页记录的固定提交为参照。
