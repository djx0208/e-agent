# 工具注册与安全策略

内置、MCP 和插件工具都使用 `Registry.register(Tool(...))`。定义包含唯一名称、说明、JSON Schema、来源和异步执行入口。名称限 1–64 个英文字母、数字、下划线或连字符；重复名称不覆盖已有工具。

```python
from e_agent.tools import Registry, Tool

async def echo(arguments):
    return {"text": arguments["text"]}

registry = Registry()
registry.register(Tool(
    name="example_echo",
    description="返回输入文字",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    source="plugin:example",
    execute=echo,
))
```

`definitions()` 返回模型的工具定义；`validate(name, arguments)` 检查参数并返回独立副本，不执行工具。分发器只有在校验、策略、必要确认和执行前复查全部通过后，才调用入口。直接调用 Python 函数不构成安全边界；同进程插件只能使用可信代码。

Schema 使用 Draft 2020-12，根类型必须为 `object`。支持常见类型、属性、数量、字符串、数值约束、组合条件及本地 JSON Pointer 引用。不支持远程引用、自定义 `$id`、`format` 或未知关键字；拒绝非有限数字。验证失败不会在错误中打印原始参数。

`PreparedOperation(target, execute, recheck)` 为需要确认的操作提供已准备的目标、执行前复查函数及无参数执行函数。目标应包括实际相对路径、版本和差异，或 argv、cwd 及执行边界。后续分发器将批准绑定到这一次调用，不能跨参数复用。

`ToolResult` 保留 `call_id`、`source`、`status`、`output` 和 `truncated`。状态区分 `success`、`error`、`denied`、`cancelled`、`not_executed`、`outcome_unknown`；`message()` 将其转换为关联原调用的模型工具消息。输出中的退出码应保留真实值。

## 可信策略配置

`Policy.from_file(path)` 只读取显式传入的用户配置路径，不搜索项目里的策略文件。省略路径采用固定默认策略；指定的文件缺失、损坏或包含未知字段时拒绝全部新操作。配置读取后形成快照，项目说明、Skill、模型回复及工具输出都不能修改它。

```json
{
  "deny_tools": ["run_command"],
  "deny_sources": ["plugin:untrusted"],
  "deny_paths": ["private/*"],
  "sensitive_exceptions": ["tests/fixtures/example.pem"]
}
```

前三项采用大小写敏感的通配符，禁止规则优先。`deny_paths` 对准备目标中的 `relative_path` 匹配，没有该字段时使用 `path`。例外只允许精确项目相对路径，不接受上级路径；它只放宽敏感路径过滤，不覆盖禁止规则或逐次确认。

默认只有内置只读工具允许直接执行，写入、命令及所有 MCP、插件工具均需逐次确认。扩展自报“只读”或说明中写“已获批准”不能免除确认。配置不提供自动批准写入的开关。

文件读取、查找和搜索共用 `sensitive_path()`：默认保护 `.env`、`.env.*`、常见私钥及凭据目录，允许普通 `.env.example`。这是文件入口过滤；第一阶段命令使用当前用户权限，固定 cwd 不是系统沙箱。

策略审计保留调用标识、工具来源、目标、规则版本、理由、批准及状态；可注入凭据处理器，内存记录和返回值均使用脱敏副本。持久保存和实际分发在后续单元接通。

验证本单元：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_tools.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_safety.py' -v
```
