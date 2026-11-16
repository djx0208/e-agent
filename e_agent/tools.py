"""统一工具定义、参数验证和关联结果，不在注册时执行工具。"""
from dataclasses import dataclass
from typing import Any, Callable
import copy
import json
import math
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    source: str
    execute: Callable
    readonly: bool = False
    prepare: Callable | None = None


@dataclass(frozen=True)
class PreparedOperation:
    target: dict
    execute: Callable
    recheck: Callable


# 限定支持面，避免验证器忽略不认识的关键字或隐式获取远程引用。
_KEYWORDS = frozenset('''$schema $id $ref $defs definitions title description default examples
 type properties required additionalProperties patternProperties propertyNames
 items prefixItems contains minContains maxContains
 minItems maxItems uniqueItems minProperties maxProperties
 minLength maxLength pattern minimum maximum exclusiveMinimum exclusiveMaximum multipleOf
 enum const allOf anyOf oneOf not if then else dependentRequired dependentSchemas
 unevaluatedProperties unevaluatedItems'''.split())
_MAP_SCHEMAS = {'properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'}
_SINGLE_SCHEMAS = {'additionalProperties', 'propertyNames', 'items', 'contains',
                   'not', 'if', 'then', 'else', 'unevaluatedProperties', 'unevaluatedItems'}
_LIST_SCHEMAS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ToolError('参数或定义必须是有限的 JSON 数据。')


def _supported(schema, root, seen=None):
    seen = set() if seen is None else seen
    if id(schema) in seen:
        return
    seen.add(id(schema))
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ToolError('工具 Schema 包含不支持的定义。')
    if '$schema' in schema and schema['$schema'] != 'https://json-schema.org/draft/2020-12/schema':
        raise ToolError('仅支持 JSON Schema Draft 2020-12。')
    if '$id' in schema:
        raise ToolError('工具 Schema 不支持自定义引用地址。')
    if '$ref' in schema:
        ref = schema['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/'):
            raise ToolError('工具 Schema 只支持本地 JSON Pointer 引用。')
        target = root
        try:
            for component in ref[2:].split('/'):
                component = component.replace('~1', '/').replace('~0', '~')
                target = target[component]
        except (KeyError, TypeError):
            raise ToolError('工具 Schema 引用不存在。') from None
        _supported(target, root, seen)
        Draft202012Validator.check_schema(target)
    for key, value in schema.items():
        if key in _MAP_SCHEMAS and isinstance(value, dict):
            for child in value.values():
                _supported(child, root, seen)
        elif key in _SINGLE_SCHEMAS:
            _supported(value, root, seen)
        elif key in _LIST_SCHEMAS and isinstance(value, list):
            for child in value:
                _supported(child, root, seen)


class Registry:
    def __init__(self):
        self._tools = {}
        self._validators = {}

    def register(self, tool):
        if not isinstance(tool, Tool):
            raise ToolError('注册对象不是工具定义。')
        if not isinstance(tool.name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tool.name):
            raise ToolError('工具名称无效。')
        if tool.name in self._tools:
            previous = self._tools[tool.name]
            raise ToolError(f'工具名称冲突：{tool.name}（{previous.source} / {tool.source}）。')
        if not isinstance(tool.description, str) or not tool.description.strip() or not isinstance(tool.source, str) or not tool.source.strip():
            raise ToolError('工具需要描述和来源。')
        if not callable(tool.execute) or (tool.prepare is not None and not callable(tool.prepare)) or not isinstance(tool.readonly, bool):
            raise ToolError('工具执行入口或属性无效。')
        try:
            _json_value(tool.schema)
            if not isinstance(tool.schema, dict) or tool.schema.get('type') != 'object':
                raise ToolError('工具参数 Schema 根类型必须为 object。')
            _supported(tool.schema, tool.schema)
            Draft202012Validator.check_schema(tool.schema)
            schema = copy.deepcopy(tool.schema)
            validator = Draft202012Validator(schema)
        except (SchemaError, RecursionError, TypeError):
            raise ToolError('工具参数 Schema 无效。') from None
        self._tools[tool.name] = Tool(tool.name, tool.description, schema, tool.source, tool.execute, tool.readonly, tool.prepare)
        self._validators[tool.name] = validator

    def tool(self, name):
        try:
            tool = self._tools[name]
        except (KeyError, TypeError):
            raise ToolError('未知或不可用的工具。') from None
        return Tool(tool.name, tool.description, copy.deepcopy(tool.schema), tool.source, tool.execute, tool.readonly, tool.prepare)

    def definitions(self):
        return [{'type': 'function', 'function': {'name': tool.name, 'description': tool.description,
                'parameters': copy.deepcopy(tool.schema)}} for tool in self._tools.values()]

    def validate(self, name, arguments):
        self.tool(name)
        try:
            _json_value(arguments)
            if not isinstance(arguments, dict):
                raise ToolError('工具参数必须为 JSON 对象。')
            self._validators[name].validate(arguments)
            return copy.deepcopy(arguments)
        except Exception:
            # 验证器原异常会包含参数值，不直接显示。
            raise ToolError('工具参数不符合 Schema，未执行。') from None

    def __contains__(self, name):
        return name in self._tools


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: str
    output: Any
    truncated: bool = False
    source: str = 'builtin'

    def __post_init__(self):
        if not isinstance(self.call_id, str) or not self.call_id or self.status not in {'success', 'error', 'denied', 'cancelled', 'not_executed', 'outcome_unknown'}:
            raise ToolError('工具结果缺少调用标识或状态无效。')
        if not isinstance(self.truncated, bool) or not isinstance(self.source, str) or not self.source:
            raise ToolError('工具结果来源或截断标记无效。')
        _json_value(self.output)

    def as_dict(self):
        return {'call_id': self.call_id, 'status': self.status, 'output': copy.deepcopy(self.output),
                'truncated': self.truncated, 'source': self.source}

    def message(self):
        return {'role': 'tool', 'tool_call_id': self.call_id,
                'content': json.dumps(self.as_dict(), ensure_ascii=False, allow_nan=False)}
