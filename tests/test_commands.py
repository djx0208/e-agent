import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from e_agent.commands import run_command
from e_agent.model import Redactor


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)

    async def run_python(self, code, **kwargs):
        return await run_command(self.project, [sys.executable, '-c', code], **kwargs)

    async def test_real_exit_cwd_and_controlled_environment(self):
        with patch.dict(os.environ, {'E_AGENT_API_KEY': 'test-secret', 'PYTHONPATH': 'bad', 'HTTP_PROXY': 'secret'}):
            result = await self.run_python('import os,json,sys; print(json.dumps(dict(os.environ))); print(os.getcwd()); sys.exit(7)')
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['status'], 'error')
        self.assertIn(str(self.project), result['output'])
        self.assertNotIn('test-secret', result['output'])
        self.assertNotIn('E_AGENT_API_KEY', result['output'])
        self.assertNotIn('PYTHONPATH', result['output'])
        self.assertNotIn('HTTP_PROXY', result['output'])

    async def test_argv_is_not_a_shell(self):
        target = self.project / 'not-created'
        result = await run_command(self.project, ['/usr/bin/printf', '%s', ';touch ' + str(target)])
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(target.exists())
        self.assertIn(';touch', result['output'])

    async def test_large_both_streams_drained_and_bounded(self):
        result = await self.run_python('import os; [(os.write(1,b"o"*8192), os.write(2,b"e"*8192)) for _ in range(128)]', output_limit=1024)
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output'].encode()), 1024)

    async def test_timeout_kills_term_ignoring_descendants(self):
        pidfile = self.project / 'pids'
        code = '''import os,signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child=os.fork()
with open("pids", "a") as f: f.write(str(os.getpid())+"\\n")
while True: time.sleep(1)
'''
        result = await self.run_python(code, timeout=.2, cleanup_grace=.1)
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['exit_code'], -9)
        for pid in pidfile.read_text().splitlines():
            stat = Path('/proc') / pid / 'stat'
            if stat.exists():
                self.assertEqual(stat.read_text().rsplit(')', 1)[1].split()[0], 'Z')

    async def test_parent_exit_with_pipe_held_by_child_is_bounded(self):
        code = 'import os,time; child=os.fork(); time.sleep(20) if child == 0 else None'
        result = await asyncio.wait_for(self.run_python(code, timeout=.15), 3)
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['exit_code'], 0)

    async def test_cancel_waits_for_cleanup_and_returns_actual_status(self):
        task = asyncio.create_task(self.run_python('import time; open("started","w").write("yes"); time.sleep(20)'))
        for _ in range(100):
            if (self.project / 'started').exists(): break
            await asyncio.sleep(.01)
        self.assertTrue((self.project / 'started').exists())
        task.cancel()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['exit_code'], -15)

    async def test_fixed_cwd_does_not_prevent_external_access(self):
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / 'public-test-fixture'
            target.write_text('outside-is-accessible')
            result = await run_command(self.project, [sys.executable, '-c', 'import sys; print(open(sys.argv[1]).read())', str(target)])
        self.assertEqual(result['status'], 'success')
        self.assertIn('outside-is-accessible', result['output'])

    async def test_redaction_before_output_boundary_and_invalid_start(self):
        secret = 'fake-secret-key'
        result = await self.run_python('print("aaaafake-secret-key")', output_limit=10, redactor=Redactor([secret]))
        self.assertNotIn('fake', result['output'])
        result = await run_command(self.project, ['/missing/command'])
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['exit_code'])
        for argv in ([], 'echo hi', [1], ['echo', '\0']):
            with self.assertRaises(ValueError):
                await run_command(self.project, argv)
        for timeout in (True, float('inf'), -1, 601):
            with self.assertRaises(ValueError):
                await run_command(self.project, ['echo'], timeout=timeout)

    async def test_unverifiable_cleanup_reports_unknown(self):
        with patch('e_agent.commands._group_alive', return_value=True):
            result = await self.run_python('pass', cleanup_grace=.03)
        self.assertEqual(result['status'], 'outcome_unknown')

    async def test_prepared_command_binds_arguments_and_directory_identity(self):
        from e_agent.commands import command_tool
        work = self.project / 'work'
        work.mkdir()
        tool = command_tool(work)
        args = {'argv': ['/usr/bin/printf', '%s', 'original']}
        prepared = tool.prepare(args)
        args['argv'][-1] = 'mutated'
        prepared.target['argv'][-1] = 'display-mutated'
        result = await prepared.execute()
        self.assertEqual(result['output'], 'original')
        prepared = tool.prepare({'argv': ['/usr/bin/touch', 'forbidden']})
        work.rename(self.project / 'old')
        work.mkdir()
        with self.assertRaises(ValueError):
            await prepared.execute()
        self.assertFalse((work / 'forbidden').exists())
        self.assertFalse((self.project / 'old' / 'forbidden').exists())

    async def test_registered_adapter_reports_unknown_and_cancel(self):
        from e_agent.commands import register_command_tool, CommandOutcomeUnknown, CommandCancelled
        from e_agent.tools import Registry
        registry = Registry()
        register_command_tool(registry, self.project)
        tool = registry.tool('run_command')
        with patch('e_agent.commands.run_command', return_value={'status': 'outcome_unknown', 'exit_code': None}):
            with self.assertRaises(CommandOutcomeUnknown) as caught:
                await tool.execute({'argv': ['echo']})
            self.assertTrue(caught.exception.outcome_unknown)
        with patch('e_agent.commands.run_command', return_value={'status': 'cancelled', 'exit_code': -15}):
            with self.assertRaises(CommandCancelled) as caught:
                await tool.execute({'argv': ['echo']})
            self.assertEqual(caught.exception.result['exit_code'], -15)
