import asyncio
import copy
import json
import unittest
import httpx
from e_agent.config import ModelConfig
from e_agent.model import ChatModel, ModelError, Redactor, validate_message


def response(text='ok', reason='stop', calls=None):
    return {'choices': [{'finish_reason': reason, 'message': {'role': 'assistant', 'content': text, 'tool_calls': calls}}]}


def call(cid='one', arguments='{}'):
    return {'id': cid, 'type': 'function', 'function': {'name': 'read_file', 'arguments': arguments}}


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def model(self, handle, key='test-key'):
        model = ChatModel(ModelConfig(base_url='https://example.test/v1/', model='test-model', api_key=key), transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(model.close)
        return model

    async def test_request_history_auth_tools_and_cleanup(self):
        requests = []
        def handle(req):
            requests.append(req)
            return httpx.Response(200, json=response())
        model = self.model(handle)
        messages = [{'role': 'user', 'content': 'first'}]
        messages.append(await model.complete(messages))
        messages.append({'role': 'user', 'content': 'second'})
        original = copy.deepcopy(messages)
        await model.complete(messages, [{'type': 'function', 'function': {'name': 'test'}}])
        self.assertEqual(messages, original)
        self.assertEqual(str(requests[0].url), 'https://example.test/v1/chat/completions')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer test-key')
        payload = json.loads(requests[-1].content)
        self.assertEqual(payload['messages'], messages)
        self.assertEqual(payload['model'], 'test-model')
        self.assertFalse(payload['stream'])
        self.assertIn('tools', payload)
        await model.close()
        self.assertTrue(model.client.is_closed)

    async def test_status_errors_no_retry_or_secret_body(self):
        for status, kind in [(401, 'authentication'), (403, 'authentication'), (429, 'rate_limit'), (500, 'service'), (302, 'service')]:
            count = []
            def handle(req):
                count.append(req)
                return httpx.Response(status, json={'error': {'message': 'test-key'}})
            model = self.model(handle)
            with self.assertRaises(ModelError) as caught:
                await model.complete([])
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn('test-key', str(caught.exception))
            self.assertEqual(len(count), 1)

    async def test_network_errors_and_cancel(self):
        for error, kind in [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'connection')]:
            def handle(req): raise error('test-key', request=req)
            with self.assertRaises(ModelError) as caught:
                await self.model(handle).complete([])
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn('test-key', str(caught.exception))
        started = asyncio.Event()
        async def handle(req):
            started.set()
            await asyncio.Future()
        model = self.model(handle)
        task = asyncio.create_task(model.complete([]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await model.close()
        self.assertTrue(model.client.is_closed)

    async def test_redaction_nested_input_output_and_reasoning(self):
        def handle(req):
            self.assertNotIn('secret/key', req.content.decode())
            data = response('https://example.test/?key=secret%2Fkey')
            data['choices'][0]['message']['reasoning_content'] = 'secret/key'
            return httpx.Response(200, json=data)
        model = self.model(handle, 'secret/key')
        result = await model.complete([{'role': 'user', 'content': 'secret/key'}])
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(result['reasoning_content'], '[REDACTED]')
        self.assertEqual(Redactor(['secret/key']).clean({'secret/key': ['secret/key']}), {'[REDACTED]': ['[REDACTED]']})

    async def test_invalid_config_never_requests(self):
        def handle(req): self.fail('unexpected network')
        model = ChatModel(ModelConfig(base_url='', model='', api_key=''), transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(model.close)
        with self.assertRaises(ModelError) as caught: await model.complete([])
        self.assertEqual(caught.exception.kind, 'config')

    async def test_invalid_json_and_usage_reset(self):
        replies = iter([httpx.Response(200, json={**response(), 'usage': {'total_tokens': 3}}), httpx.Response(200, text='not JSON')])
        model = self.model(lambda req: next(replies))
        await model.complete([])
        self.assertEqual(model.usage['total_tokens'], 3)
        with self.assertRaises(ModelError): await model.complete([])
        self.assertIsNone(model.usage)


class ValidationTests(unittest.TestCase):
    def test_valid_calls_and_reasoning(self):
        result = validate_message(response(None, 'tool_calls', [call()]))
        self.assertEqual(result['tool_calls'], [call()])

    def test_reject_incomplete_empty_duplicate_and_malformed_calls(self):
        invalid = [{}, {'choices': []}, response('', 'stop'), response('partial', 'length'), response('no', 'content_filter'), response(3), response(None, 'tool_calls', [call(arguments='{')]), response(None, 'tool_calls', [call(), call()]), response(None, 'tool_calls', [call(arguments='[]')]), response(None, 'tool_calls', [call(arguments='{"x":NaN}')]), response(None, 'tool_calls', [call(arguments='{"x":1,"x":2}')]), response('text', 'stop', [call()])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ModelError):
                validate_message(value)

    def test_escaped_tool_arguments_are_redacted_before_execution(self):
        arguments = '{"content":"secret\\u002fkey"}'
        redactor = Redactor(['secret/key'])
        result = redactor.clean(validate_message(response(None, 'tool_calls', [call(arguments=arguments)])))
        decoded = json.loads(result['tool_calls'][0]['function']['arguments'])
        self.assertEqual(decoded['content'], '[REDACTED]')

    def test_exponent_overflow_is_rejected(self):
        for number in ('1e309', '-1e309'):
            with self.subTest(number=number), self.assertRaises(ModelError):
                validate_message(response(None, 'tool_calls', [call(arguments='{"timeout":' + number + '}')]))
