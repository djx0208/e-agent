# Spec Delta

## Purpose

在第二阶段让同一 Agent 通过程序接口和无交互终端处理单次任务。自动化调用能够获得结构化进度、明确退出状态和取消能力，同时保持与交互方式一致的安全判断和记录。

## ADDED Requirements

### Requirement: 共用运行接口

程序 SHALL 提供运行、取消、订阅与状态查询能力，交互和单次运行 SHALL 共用工具策略、预算和持久化行为，不依赖交互终端。

#### Scenario: 程序调用取消
- **WHEN** 调用方取消正在执行的任务
- **THEN** 执行与交互取消相同的清理，查询最终状态可区分取消与结果未知

### Requirement: 单次输入与结构化输出

单次 CLI SHALL 接收参数或 stdin 任务，支持最终文本或版本化 JSONL 事件。JSONL stdout SHALL 仅包含协议事件，诊断走 stderr；退出码 SHALL 区分正常、运行失败、配置错误、权限阻塞及取消。

#### Scenario: 输出被管道消费
- **WHEN** 以 JSONL 运行且发生工具错误
- **THEN** 每行 stdout 均可解析，无颜色或日志混入，事件与退出码反映实际终态

### Requirement: 无审批通道默认拒绝

无界面运行 SHALL 只直接执行策略已允许的操作，遇到 ask 且无可信审批通道时拒绝并标记权限阻塞，不等待 stdin 或提供绕过确认开关。

#### Scenario: 自动化请求写入
- **WHEN** 单次运行申请写入但没有审批通道
- **THEN** 文件不变，运行以权限阻塞终结，退出码为 3
