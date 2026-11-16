import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e_agent.cli import repl


class FakeModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.closed = False
    async def complete(self, messages):
        self.requests.append(copy.deepcopy(messages))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return {'role': 'assistant', 'content': result}
    async def close(self):
        self.closed = True


class CLITests(unittest.IsolatedAsyncioTestCase):
    async def test_multiturn_failure_cancel_keep_each_user_once(self):
        from e_agent.model import ModelError
        model = FakeModel(['first answer', ModelError('failed'), asyncio.CancelledError(), 'last answer'])
        inputs = iter(['first', 'second', 'third', 'fourth', '/exit'])
        output = []
        await repl(input_fn=lambda _: next(inputs), output_fn=output.append, model=model)
        self.assertEqual([m['content'] for m in model.requests[-1]], ['first', 'first answer', 'second', 'third', 'fourth'])
        self.assertTrue(model.closed)
        self.assertIn('已取消当前请求。', output)
        self.assertIn('last answer', output)

    async def test_bad_config_does_not_block_help_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{"api_key":"secret-key",BROKEN')
            inputs = iter(['/help', 'hello', '/help', '/exit'])
            output = []
            with patch('httpx.AsyncClient.post', side_effect=AssertionError('不应联网')):
                await repl(path, input_fn=lambda _: next(inputs), output_fn=output.append)
            self.assertNotIn('secret-key', '\n'.join(output))
            self.assertEqual(sum('/exit  退出' in x for x in output), 2)

    async def test_blank_unknown_and_idle_interrupt_never_call_model(self):
        model = FakeModel([])
        inputs = iter(['', '  ', '/unknown', KeyboardInterrupt(), '/exit'])
        def read(_):
            result = next(inputs)
            if isinstance(result, BaseException): raise result
            return result
        output = []
        await repl(input_fn=read, output_fn=output.append, model=model)
        self.assertEqual(model.requests, [])
        self.assertTrue(model.closed)
        self.assertIn('未知命令，请输入 /help。', output)


class SignalTests(unittest.TestCase):
    def test_real_sigint_wakes_waiting_model_request(self):
        import subprocess
        import signal
        import sys
        code = '''import asyncio
from e_agent.cli import repl
class Model:
 async def complete(self, messages):
  print("REQUEST_WAITING", flush=True)
  await asyncio.Event().wait()
 async def close(self): pass
items = iter(["hello", "/exit"])
asyncio.run(repl(input_fn=lambda _: next(items), model=Model()))
'''
        process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            import select
            import os
            import time
            prefix = bytearray()
            deadline = time.monotonic() + 5
            while b'REQUEST_WAITING' not in prefix and time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 0.1)[0]:
                    prefix.extend(os.read(process.stdout.fileno(), 4096))
            self.assertIn(b'REQUEST_WAITING', prefix)
            process.send_signal(signal.SIGINT)
            output, error = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn('已取消当前请求。'.encode(), output)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
