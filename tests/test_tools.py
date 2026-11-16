import copy
import json
import unittest
from e_agent.tools import Registry, Tool, ToolError, ToolResult, PreparedOperation


async def echo(arguments):
    return arguments


def tool(name='echo', source='builtin', **kwargs):
    return Tool(name, '返回输入文字', {'type': 'object', 'properties': {'text': {'type': 'string'}},
        'required': ['text'], 'additionalProperties': False}, source, echo, **kwargs)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_registration_without_dispatch_changes(self):
        registry = Registry()
        for index, source in enumerate(('builtin', 'mcp:local', 'plugin:example')):
            definition = tool('echo' + str(index), source)
            registry.register(definition)
            arguments = registry.validate(definition.name, {'text': source})
            output = await registry.tool(definition.name).execute(arguments)
            result = ToolResult(str(index), 'success', output, source=source)
            self.assertEqual(json.loads(result.message()['content'])['source'], source)
            self.assertEqual(result.message()['tool_call_id'], str(index))
        self.assertEqual(len(registry.definitions()), 3)

    async def test_bad_arguments_never_enter_execution(self):
        calls = []
        async def execute(args): calls.append(args)
        registry = Registry()
        registry.register(Tool('test', 'test', tool().schema, 'plugin:test', execute))
        for arguments in ({}, {'text': 1}, {'text': 'ok', 'extra': 'secret'}, [], {'text': float('inf')}, {'text': object()}):
            with self.subTest(arguments=type(arguments)), self.assertRaises(ToolError) as caught:
                valid = registry.validate('test', arguments)
                await registry.tool('test').execute(valid)
            self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(calls, [])

    def test_conflicts_and_unknown_tool(self):
        registry = Registry()
        registry.register(tool())
        with self.assertRaisesRegex(ToolError, 'builtin / plugin:evil'):
            registry.register(tool(source='plugin:evil'))
        self.assertEqual(registry.tool('echo').source, 'builtin')
        with self.assertRaises(ToolError): registry.validate('absent', {})

    def test_schema_validation_and_no_external_references(self):
        bad = [{'type': 'array'}, {'type': 'object', 'unknownKeyword': True},
               {'type': 'object', 'properties': {'x': {'type': 'nonsense'}}},
               {'type': 'object', 'properties': {'x': {'$ref': 'https://example.test/schema'}}},
               {'type': 'object', 'properties': {'x': {'$ref': '#/$defs/missing'}}},
               {'type': 'object', 'properties': {'x': {'format': 'email'}}},
               {'type': 'object', '$schema': 'http://json-schema.org/draft-07/schema#'}]
        for schema in bad:
            with self.subTest(schema=schema), self.assertRaises(ToolError):
                Registry().register(Tool('bad', 'bad', schema, 'builtin', echo))

    def test_local_refs_and_nested_schema(self):
        registry = Registry()
        schema = {'type': 'object', '$defs': {'count': {'type': 'integer', 'minimum': 1}},
                  'properties': {'n': {'$ref': '#/$defs/count'}}, 'required': ['n']}
        registry.register(Tool('count', 'count', schema, 'builtin', echo))
        self.assertEqual(registry.validate('count', {'n': 1}), {'n': 1})
        with self.assertRaises(ToolError): registry.validate('count', {'n': False})
        with self.assertRaises(ToolError): registry.validate('count', {'n': 0})

    def test_definition_and_argument_mutation_cannot_change_registry(self):
        registry = Registry()
        definition = tool()
        registry.register(definition)
        definition.schema['required'].clear()
        registry.definitions()[0]['function']['parameters']['required'].clear()
        registry.tool('echo').schema['required'].clear()
        with self.assertRaises(ToolError): registry.validate('echo', {})
        args = {'text': 'original'}
        checked = registry.validate('echo', args)
        checked['text'] = 'changed'
        self.assertEqual(args['text'], 'original')

    def test_result_statuses_and_truncation(self):
        for status in ['success', 'error', 'denied', 'cancelled', 'not_executed', 'outcome_unknown']:
            result = ToolResult('call-1', status, {'exit_code': 2}, truncated=True)
            self.assertEqual(result.as_dict()['status'], status)
            self.assertEqual(json.loads(result.message()['content'])['output']['exit_code'], 2)
            self.assertTrue(result.as_dict()['truncated'])
        with self.assertRaises(ToolError): ToolResult('', 'success', '')
        with self.assertRaises(ToolError): ToolResult('1', 'passed', '')
