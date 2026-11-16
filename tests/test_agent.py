import asyncio
import copy
import json
import unittest
from e_agent.agent import Agent, Dispatcher, PersistenceError
from e_agent.model import Redactor
from e_agent.safety import Policy
from e_agent.tools import Registry, Tool, PreparedOperation


def call(name='echo', args=None, cid='one'):
    return {'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args or {})}}


class Model:
    def __init__(self, replies): self.replies, self.requests = iter(replies), []
    async def complete(self, messages, tools=None):
        self.requests.append(copy.deepcopy(messages))
        return next(self.replies)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def dispatcher(self, *, readonly=True, **kw):
        registry = Registry()
        self.executed = []
        async def execute(args):
            self.executed.append(args)
            return args
        registry.register(Tool('echo', '测试', {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'additionalProperties': False}, 'builtin', execute, readonly))
        return Dispatcher(registry, Policy(), **kw)

    async def test_pairing_invalid_unknown_and_normal(self):
        d = self.dispatcher()
        first = {'role': 'assistant', 'content': None, 'tool_calls': [call('missing', cid='a'), call(args={'extra':1}, cid='b'), call(args={'text':'ok'}, cid='c')]}
        m = Model([first, {'role':'assistant','content':'done'}])
        history = []
        result = await Agent(m,d).run(history,'task')
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.executed, [{'text':'ok'}])
        self.assertEqual([x['tool_call_id'] for x in m.requests[1] if x['role']=='tool'], ['a','b','c'])
        self.assertEqual([json.loads(x['content'])['status'] for x in history if x['role']=='tool'], ['error','error','success'])

    async def test_each_write_needs_fresh_approval_and_denial_wins(self):
        displays = []
        def confirm(value): displays.append(value); return len(displays)==1
        d = self.dispatcher(readonly=False, confirm=confirm)
        self.assertEqual((await d.dispatch(call(cid='a'))).status,'success')
        self.assertEqual((await d.dispatch(call(cid='b'))).status,'denied')
        d.policy = Policy({'deny_tools':['echo']})
        self.assertEqual((await d.dispatch(call(cid='c'))).status,'denied')
        self.assertEqual(len(displays),2)
        self.assertEqual(len(self.executed),1)
        d.confirm=None; d.policy=Policy()
        self.assertEqual((await d.dispatch(call(cid='d'))).status,'denied')

    async def test_confirm_mutations_cannot_change_arguments_or_snapshot(self):
        def confirm(value): value['arguments']['text']='modified'; return True
        d=self.dispatcher(readonly=False,confirm=confirm)
        await d.dispatch(call(args={'text':'original'}))
        self.assertEqual(self.executed,[{'text':'original'}])

    async def test_changed_target_rejected_after_confirmation(self):
        changed=False
        async def execute(): self.fail('must not execute')
        def recheck():
            if changed: raise ValueError('目标发生变化')
        def prepare(args): return PreparedOperation({'path':'file','version':'v1'},execute,recheck)
        def confirm(value):
            nonlocal changed
            changed=True
            return True
        d=self.dispatcher(confirm=confirm)
        d.registry.register(Tool('write','写入',{'type':'object'},'builtin',execute,False,prepare))
        self.assertEqual((await d.dispatch(call('write'))).status,'error')

    async def test_round_limit_has_paired_unexecuted_results(self):
        d=self.dispatcher()
        m=Model([{'role':'assistant','content':None,'tool_calls':[call(cid='a'),call(cid='b')]}])
        history=[]
        result=await Agent(m,d,max_rounds=1).run(history,'task')
        self.assertEqual(result.status,'limit')
        self.assertEqual(self.executed,[])
        self.assertEqual([json.loads(x['content'])['status'] for x in history if x['role']=='tool'],['not_executed']*2)

    async def test_hooks_only_veto_and_after_errors_preserve_real_result(self):
        def broken(record): raise RuntimeError('secret')
        d=self.dispatcher(before=[broken])
        self.assertEqual((await d.dispatch(call())).status,'denied')
        self.assertEqual(self.executed,[])
        d.before=[]; d.after=[broken]
        self.assertEqual((await d.dispatch(call(cid='b'))).status,'success')
        self.assertEqual(len(self.executed),1)
        self.assertIn('observer_errors',d.records[-1])

    async def test_save_failure_stops_before_execution(self):
        def fail(): raise OSError('disk full')
        d=self.dispatcher(checkpoint=fail)
        with self.assertRaises(PersistenceError): await d.dispatch(call())
        self.assertEqual(self.executed,[])

    async def test_cancelled_batch_does_not_execute_remaining(self):
        d=self.dispatcher()
        async def cancel(args): raise asyncio.CancelledError()
        d.registry.register(Tool('cancel','取消',{'type':'object'},'builtin',cancel,True))
        m=Model([{'role':'assistant','content':None,'tool_calls':[call('cancel',cid='a'),call(cid='b')]}])
        h=[]; result=await Agent(m,d).run(h,'task')
        self.assertEqual(result.status,'outcome_unknown')
        self.assertEqual(len(m.requests),1)
        self.assertEqual(self.executed,[])
        self.assertEqual([json.loads(x['content'])['status'] for x in h if x['role']=='tool'],['outcome_unknown','not_executed'])

    async def test_secret_removed_and_large_result_bounded(self):
        d=self.dispatcher(redactor=Redactor(['secret']),output_limit=2048)
        result=await d.dispatch(call(args={'text':'secret'+'a'*10000}))
        self.assertTrue(result.truncated)
        self.assertNotIn('secret',json.dumps(result.as_dict()))
        self.assertLess(len(result.message()['content'].encode()),2500)
