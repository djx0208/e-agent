#!/usr/bin/env python3
"""交互填写 DeepSeek API 配置；仅使用标准库，不联网。"""
import argparse
import getpass
from pathlib import Path
import sys
import warnings
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e_agent.config import ConfigError, ModelConfig, default_config_path, load_config, save_config

DEFAULT_BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-flash'


def main(argv=None):
    parser = argparse.ArgumentParser(description='在仓库外私有文件中配置 DeepSeek；不联网、不显示密钥')
    parser.add_argument('--config', type=Path, help='配置文件位置，必须在仓库外')
    parser.add_argument('--check', action='store_true', help='只检查已有配置格式，不调用模型')
    args = parser.parse_args(argv)
    try:
        path = (args.config or default_config_path()).expanduser().absolute()
        if path.resolve().is_relative_to(ROOT):
            raise ConfigError('配置必须保存在仓库外')
        config = load_config(path, environ={})
        if args.check:
            config.validate()
            print('配置格式检查通过；密钥有效性尚未联网验证。')
            return 0
        if any((config.base_url, config.model, config.api_key)):
            config.validate()
        print('DeepSeek API 本地配置。密钥输入不会回显；本脚本不联网。')
        print('保存位置：' + str(path))
        def label(value):
            for secret in (config.api_key, quote(config.api_key, safe='')):
                if secret:
                    value = value.replace(secret, '[REDACTED]')
            return ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)
        base_url = input('接口地址 [' + label(config.base_url or DEFAULT_BASE_URL) + ']：').strip() or config.base_url or DEFAULT_BASE_URL
        model = input('模型名 [' + label(config.model or DEFAULT_MODEL) + ']：').strip() or config.model or DEFAULT_MODEL
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('API key' + ('（留空保留已有密钥）' if config.api_key else '') + '：').strip()
        updated = ModelConfig(base_url, model, key or config.api_key, config.timeout)
        updated.validate()
        save_config(updated, path)
        print('配置已保存，目录权限 700、文件权限 600。未发起模型请求。')
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\n已取消，未保存新配置。', file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print('无法隐藏密钥输入，请在交互终端运行脚本。', file=sys.stderr)
        return 2
    except ConfigError as exc:
        print('配置失败：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
