"""完整模型消息的 HTTP 传输；不在传输层执行工具或自动重试。"""
import copy
import json
import math
import re
from urllib.parse import quote

import httpx
from .config import ConfigError


class ModelError(Exception):
    def __init__(self, message, kind='protocol'):
        super().__init__(message)
        self.kind = kind


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret):
        if secret:
            self.secrets.update((secret, quote(secret, safe='')))

    def text(self, value):
        value = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return value

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == 'arguments' and isinstance(item, str):
                    try:
                        decoded = strict_json(item)
                    except (ValueError, TypeError, RecursionError):
                        pass
                    else:
                        item = json.dumps(self.clean(decoded), ensure_ascii=False)
                result[self.text(key)] = self.clean(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def strict_json(text):
    def invalid_constant(value):
        raise ValueError('non-finite number')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('non-finite number')
        return number
    return json.loads(text, object_pairs_hook=_object, parse_constant=invalid_constant, parse_float=finite_float)


def validate_message(data):
    try:
        choices = data['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError()
        choice = choices[0]
        reason = choice['finish_reason']
        if reason not in ('stop', 'tool_calls'):
            raise ModelError('模型回复未完成或被拒绝，不执行工具。', 'incomplete')
        message = choice['message']
        if message['role'] != 'assistant':
            raise ValueError()
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            raise ValueError()
        calls = message.get('tool_calls', [])
        if calls is None:
            calls = []
        if not isinstance(calls, list) or bool(calls) != (reason == 'tool_calls'):
            raise ValueError()
        ids = set()
        normalized = []
        for call in calls:
            cid, fn = call['id'], call['function']
            if not isinstance(cid, str) or not cid.strip() or cid in ids or call['type'] != 'function':
                raise ValueError()
            ids.add(cid)
            if not isinstance(fn['name'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fn['name']):
                raise ValueError()
            if not isinstance(fn['arguments'], str) or not isinstance(strict_json(fn['arguments']), dict):
                raise ValueError()
            normalized.append({'id': cid, 'type': 'function', 'function': {'name': fn['name'], 'arguments': fn['arguments']}})
        if not calls and (not content or not content.strip()):
            raise ModelError('模型返回空回复。', 'empty')
        result = {'role': 'assistant', 'content': content}
        if normalized:
            result['tool_calls'] = normalized
        reasoning = message.get('reasoning_content')
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise ValueError()
            result['reasoning_content'] = reasoning
        return result
    except (KeyError, TypeError, ValueError, IndexError, RecursionError):
        raise ModelError('模型回复格式无效，不执行工具。') from None


class ChatModel:
    def __init__(self, config, transport=None):
        self.config = config
        self.redactor = Redactor([config.api_key])
        self.client = httpx.AsyncClient(timeout=config.timeout, transport=transport,
                                       follow_redirects=False, trust_env=False)
        self.usage = None

    async def close(self):
        await self.client.aclose()

    async def complete(self, messages, tools=None):
        self.usage = None
        try:
            self.config.validate()
        except ConfigError:
            raise ModelError('模型配置缺失或无效，请运行配置脚本。', 'config') from None
        payload = {'model': self.config.model, 'messages': self.redactor.clean(copy.deepcopy(messages)), 'stream': False}
        if tools:
            payload['tools'] = self.redactor.clean(copy.deepcopy(tools))
        try:
            response = await self.client.post(self.config.base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.config.api_key}, json=payload)
            if response.status_code in (401, 403):
                raise ModelError('模型鉴权失败，请检查密钥和权限。', 'authentication')
            if response.status_code == 429:
                raise ModelError('模型请求受到限流，请稍后重试。', 'rate_limit')
            if not response.is_success:
                raise ModelError(f'模型服务错误（HTTP {response.status_code}）。', 'service')
            data = strict_json(response.text)
            message = validate_message(data)
            usage = data.get('usage')
            if isinstance(usage, dict):
                self.usage = self.redactor.clean(usage)
            return self.redactor.clean(message)
        except httpx.TimeoutException:
            raise ModelError('模型请求超时。', 'timeout') from None
        except httpx.RequestError:
            raise ModelError('无法连接模型服务。', 'connection') from None
        except (ValueError, TypeError, RecursionError):
            raise ModelError('模型回复不是有效 JSON。') from None
