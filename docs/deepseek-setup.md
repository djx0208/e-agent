# 配置 DeepSeek

在项目目录运行以下脚本。脚本只用 Python 标准库，不联网，也不需要先安装项目依赖。

```bash
python3 scripts/configure_deepseek.py
```

按提示输入接口地址、模型名和 DeepSeek API key。接口地址和模型名可以按 Enter 使用默认值；密钥输入不会回显。已有配置时，密钥留空会保留原密钥。

默认地址为 `https://api.deepseek.com`，默认模型为 `deepseek-flash`。这些默认值于 2026 年 11 月 16 日按 [DeepSeek 官方接口文档](https://api-docs.deepseek.com/) 核实，脚本允许修改。基础地址不要加 `/chat/completions`，不要把密钥放进地址。

配置默认保存在 `~/.config/e-agent/model.json`，目录权限为 700，文件权限为 600。若设置了 `XDG_CONFIG_HOME`，则保存在其下的 `e-agent/model.json`。脚本拒绝把配置保存在项目仓库内，并拒绝配置路径中的符号链接。

只检查文件格式和必要字段：

```bash
python3 scripts/configure_deepseek.py --check
```

检查通过只代表配置格式正确，密钥是否有效需要后续真实 API 调用验证。脚本不会打印密钥，也不会自动发起付费请求。请在本机交互终端输入密钥，不要把密钥粘贴到聊天、截图或 Git 提交中。

## 可选位置与环境变量

可使用仓库外的专用目录保存配置：

```bash
python3 scripts/configure_deepseek.py --config /absolute/private-directory/model.json
```

配置读取模块支持 `E_AGENT_BASE_URL`、`E_AGENT_MODEL`、`E_AGENT_API_KEY` 环境变量覆盖文件中的对应字段。配置脚本始终编辑文件，不从这些变量回填密钥，也不生成需要 `source` 执行的 shell 文件。

缺少默认配置时，应用可以先启动帮助和退出功能；提交任务前才校验必要配置。配置文件损坏、权限过宽或包含不支持字段时会报告配置错误，不显示原文件内容。不要用 `--config` 指向共享目录中的文件；保存时会将配置所在目录权限设为 700。
