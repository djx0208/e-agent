# 重装系统后继续开发

暂停日期：2026-11-16。用户要求先提交、推送并暂停，以便重装系统。本次提交是开发快照，不是第一阶段完成版本。

## 已确认的范围

实现 `coding-agent-14-days` 的全部 106 项任务，第一阶段验收通过后再进入第二阶段。模型使用 DeepSeek HTTP API，用户通过配置脚本在本机输入密钥；不要接 Codex CLI 或读取 Codex 登录凭据。允许开发子 Agent 分工，允许安装 Docker 和所需依赖。真实模型要覆盖多轮记忆与纠正、工具选择、实际修复及测试、失败后继续、权限边界和压缩后继续等场景。固定模型测试与真实模型验收分别记录。

## 恢复环境

```bash
git clone --branch codex/deepseek-agent-phase-1 https://github.com/djx0208/e-agent.git
cd e-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 scripts/configure_deepseek.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python main.py
```

需要 Python 3.11+；Debian/Ubuntu 如缺 venv，可安装对应的 python3-venv 包。配置脚本使用标准库，不需要联网。配置默认位于 `~/.config/e-agent/model.json`，不在 Git 中；重装后重新运行脚本配置即可。虚拟环境和系统软件同样不进入 Git。

## 当前状态

- 分支：`codex/deepseek-agent-phase-1`。规格基线为 `3b1236a`。
- `ccaef94` 保存第一单元离线 REPL 和实施安排。
- `9678d73` 保存私有 DeepSeek 配置、HTTPX 对话、错误处理和多轮历史。
- 任务已勾选 10/106：1.1–1.3、2.1、2.2、2.4、3.1–3.4。2.3 真实调用未验收。
- 当前快照加入统一工具注册、安全策略、5 个文件工具、命令执行、独立 Dispatcher/Agent 循环及说明。
- 暂停前完整本地测试：79 项全部通过。含真实子进程、终端 Ctrl+C、文件竞态、参数校验与权限检查；尚未执行真实 DeepSeek 验收。
- 新工具和 Agent 循环尚未接入 CLI，`main.py` 当前仍只提供模型对话。第 4–6 单元保留未勾选，待整体验收。
- 第 7–9 单元尚未交付。`e_agent/sessions.py` 初稿已保存，但未编写测试、未验收；不计入上述 79 项测试。项目说明和 Skills 只有接口讨论。
- Docker 尚未重新安装；第二阶段未开始。

本次受限执行环境可能拦截 asyncio 唤醒或子进程信号，真实信号与进程测试已在正常本机权限下通过。不要通过跳过测试掩盖失败。

## 恢复后优先处理

1. 修复已确认的问题：`deny_paths` 目前只校验工具顶层目标，`find_files`/`search_text` 可绕过。应将固定策略注入文件模块，在读取、准备编辑、遍历及搜索时逐路径过滤，目录规则也应剪枝；增加回归测试。
2. 复现并处理审查发现：结果先截断再 JSON 编码可能超过输出上限；已开始的扩展抛 `OSError`/`ValueError` 不应无条件作为普通错误继续；after 回调取消时需保证真实结果加入对话、工具配对完整。这三项尚未复现验证。
3. 核查 Dispatcher 外层超时与命令显式 timeout 的关系，避免固定 65 秒截断允许的更长命令。
4. 将工具注册与 Agent 接入 CLI，逐次展示实际路径、差异、argv、环境名称及本机执行边界；加入真实确认等待变更及读文件—编辑—运行测试的集成验收。
5. 完成 Session、项目说明、Skills、真实本地 MCP、压缩、插件及第一阶段终端界面。逐项读取规格、测试并勾选，不按模块存在就判完成。
6. 用户配置 DeepSeek 后执行真实场景，记录模型、输入、实际工具结果及用量，不记录密钥。第一阶段完整通过后再安装并验收 Docker 和第二阶段功能。

## 当前接口

- `Tool`/`Registry`/`ToolResult` 在 `e_agent/tools.py`；`PreparedOperation(target, execute, recheck)` 绑定确认前快照。
- `Policy` 在 `e_agent/safety.py`；可信配置只从用户显式路径加载，禁止规则优先。
- `register_file_tools(registry, FileTools(project, sensitive_exceptions=...))` 注册文件工具。
- `register_command_tool(registry, project, redactor=None)` 注册命令。`CommandCancelled.result` 和 `CommandOutcomeUnknown.result` 保留真实结果，调度器应停止当前批次。
- `Agent(model, dispatcher).run(history, text)` 当前使用内存消息。后续 Session 接入需稳定消息 ID，并在模型请求前去除内部元数据。

## 未加入本次开发提交的本地文件

此前已有 `.archify/` 和 `docs/diagrams/`，与本次实现无关，未修改或提交。如要保留它们，重装前另行备份。本机私有配置、聊天记录、工具安装和未纳入 Git 的其他资料也不会随仓库恢复。
