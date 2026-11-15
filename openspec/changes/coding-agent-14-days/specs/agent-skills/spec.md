# Spec Delta

## Purpose

让 Agent 按任务加载可复用的操作说明，避免把所有说明预先放入模型上下文。Skills 以本地文档提供，不依赖插件系统；发现、加载和实际执行脚本是分别可观察的操作。

## ADDED Requirements

### Requirement: 发现并列出 Skills

程序 SHALL 从配置的本地 Skills 目录发现 `SKILL.md`，提取名称、描述和路径，提供 `/skills` 列表。启动时 SHALL 只向模型提供可用 Skill 的元信息，不注入全部正文；重复名称或无效元信息 SHALL 明确报错并排除对应 Skill。

#### Scenario: 发现多个 Skill
- **WHEN** 两个有效 Skill 位于配置目录
- **THEN** 列表和模型可用目录包含各自名称与描述，初始上下文不含它们的正文

### Requirement: 按需加载正文

程序 SHALL 支持 `/skill <name>` 显式加载以及模型通过已注册的加载工具按需读取。加载 SHALL 保留来源，相同版本的正文仍在当前视图中时不重复注入；已被摘要覆盖时允许再次按需读取；未知名称、越界路径或超限正文 SHALL 明确失败。

#### Scenario: 用户选择 Skill
- **WHEN** 用户执行 `/skill review`
- **THEN** 程序加载该 Skill 正文用于后续请求，并显示加载来源

### Requirement: Skill 不授予执行权限

加载 Skill SHALL 只提供任务说明，不自动执行附带脚本、加载 Python 插件或启动 MCP 服务。引用文件 SHALL 按需读取并限制在该 Skill 目录内；任何执行仍经过普通工具确认流程。

#### Scenario: Skill 包含脚本指令
- **WHEN** 正文要求运行附带脚本
- **THEN** 加载本身不执行脚本，只有后续被批准的工具调用才能执行

### Requirement: 恢复已加载的说明

程序 SHALL 在 Session 中保存已加载 Skill 的标识、来源、版本标识及内容记录。恢复和压缩 SHALL 不重复添加正文，也不静默替换历史使用过的版本；用户再次加载新版本时明确展示变化。

#### Scenario: 文件变化后恢复会话
- **WHEN** Skill 文件被修改后用户恢复旧 Session
- **THEN** 历史仍反映原加载版本，新版本需再次显式或按需加载

### Requirement: Skill 内容遵守统一安全策略

Skill 元信息、正文和引用文件 SHALL 遵守 execution-safety，不能修改策略、启用服务或绕过敏感路径限制。

#### Scenario: Skill 引用私钥
- **WHEN** 模型按正文请求加载受保护私钥
- **THEN** 读取被拒绝，正文不构成授权
