# 项目文件工具

文件工具以启动时指定的项目目录为根，只接受相对路径。`../`、绝对路径、符号链接、非普通文件和非 UTF-8 文本均拒绝。项目根目录会规范化；项目内不跟随符号链接，包含指向项目内的链接。

| 工具 | 参数与结果 |
| --- | --- |
| `read_file` | `path`、可选 `start_line` 和 `end_line`（从 1 开始，包含末行）；返回原始换行文本、完整文件 SHA-256 `hash`、总行数和截断标记 |
| `write_file` | `path`、`content`；仅新建，父目录须存在，同名文件或链接存在时拒绝 |
| `edit_file` | `path`、最近读取的 `old_hash`、非空 `old_text`、`new_text`；原文须只匹配一次，包括重叠匹配检查 |
| `find_files` | 可选 glob `pattern` 和 `limit`；返回相对路径 |
| `search_text` | 字面字符串 `query`、可选 glob `pattern` 和 `limit`；返回路径、行号、匹配行及截断信息 |

单个文件上限 256 KiB，读取正文输出上限 64 KiB。查找和搜索最多 200 条，保留结果总量有 64 KiB 限制，匹配行最多 4 KiB。搜索跳过不支持编码、超大或不可访问的文件，并报告 `skipped_files`；它不把跳过文件当作没有匹配。截断结果不能代表完整文件或全部匹配。

读取、查找、搜索和修改共用可信策略中的敏感路径规则。默认保护 `.env`、`.env.*`、私钥及 `.aws/`、`.ssh/`、`.kube/` 等凭据目录，同时保护 `.git/`；`.env.example` 可读取。只有用户显式提供的可信策略 `sensitive_exceptions` 精确相对路径列表可增加例外，项目文字和模型请求不会增加例外。

写入准备阶段保存实际路径、完整差异、文件哈希和目录/文件版本。确认后仍检查这些信息；文件变化、父目录被替换、父目录改为链接或同名新文件出现时，原批准失效。新建使用不会覆盖目标的原子操作；编辑使用同目录临时文件及原子替换，保持原始未修改字节、换行和文件权限。修改失败后应重新读取，而不是复用旧哈希。目录描述符和复查用于减少竞态，不保证防御同权限恶意进程的所有并发替换。

这些底层方法可供测试直接调用；应用通过统一注册表和分发器调用，写入仍需逐次批准。底层文件 API 本身不是独立权限系统。命令工具的权限边界见命令工具说明：第一阶段的命令不受文件工具的目录限制。

在一次性临时目录直接验证文件工具：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from e_agent.files import FileTools

with TemporaryDirectory() as directory:
    tools = FileTools(Path(directory))
    tools.write_file('example.py', 'answer = 1\n')
    original = tools.read_file('example.py')
    tools.edit_file('example.py', original['hash'], '1', '2')
    assert tools.read_file('example.py')['content'] == 'answer = 2\n'
    assert tools.search_text('answer')['matches'][0]['line'] == 1
    assert tools.find_files('*.py')['matches'] == ['example.py']
    print('文件练习通过')
PY
```

运行文件边界与并发变更测试：

```bash
.venv/bin/python -m unittest discover -s tests -p test_files.py -v
```
