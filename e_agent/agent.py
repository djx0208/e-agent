"""单任务循环与统一工具分发。批准仅来自调用方提供的独立确认函数。"""
import asyncio
import copy
import inspect
import json
from dataclasses import dataclass
from .model import ModelError, Redactor, strict_json
from .tools import ToolError, ToolResult


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


class PersistenceError(Exception):
    pass


class Dispatcher:
    def __init__(self, registry, policy, *, confirm=None, redactor=None, checkpoint=None,
                 before=(), after=(), timeout=65, output_limit=65536):
        self.registry, self.policy = registry, policy
        self.confirm, self.checkpoint = confirm, checkpoint
        self.redactor = redactor or Redactor()
        self.before, self.after = list(before), list(after)
        self.timeout, self.output_limit = timeout, output_limit
        self.records = []

    async def save(self):
        if self.checkpoint:
            try:
                await maybe_await(self.checkpoint())
            except Exception:
                raise PersistenceError('保存失败，停止后续操作。') from None

    def result(self, cid, status, output, source='builtin'):
        output = self.redactor.clean(output)
        encoded = json.dumps(output, ensure_ascii=False, allow_nan=False).encode()
        truncated = isinstance(output, dict) and output.get('truncated') is True
        if len(encoded) > self.output_limit:
            keep = {key: output[key] for key in ('exit_code', 'timed_out') if isinstance(output, dict) and key in output}
            output = {**keep, 'preview': encoded[:self.output_limit - 1024].decode('utf-8', 'ignore'), 'original_bytes': len(encoded)}
            truncated = True
        return ToolResult(cid, status, output, truncated, source)

    async def dispatch(self, call):
        from .tools import PreparedOperation
        cid, fn = call['id'], call['function']
        source = 'unavailable'
        record = {'call_id': cid, 'name': fn['name'], 'status': 'not_executed'}
        self.records.append(record)
        started = False
        try:
            tool = self.registry.tool(fn['name'])
            source = tool.source
            args = self.registry.validate(tool.name, strict_json(fn['arguments']))
            if tool.prepare:
                prepared = await maybe_await(tool.prepare(copy.deepcopy(args)))
                if not isinstance(prepared, PreparedOperation):
                    raise ToolError('工具准备结果无效。')
            else:
                prepared = PreparedOperation({'arguments': copy.deepcopy(args)}, lambda: tool.execute(copy.deepcopy(args)), lambda: None)
            decision = self.policy.decide(tool, prepared.target)
            record.update(source=source, arguments=self.redactor.clean(args), decision=self.redactor.clean(decision.as_dict()))
            approved = False
            denied = decision.action == 'deny'
            if not denied:
                for hook in self.before:
                    try:
                        veto = await maybe_await(hook(copy.deepcopy(record)))
                        if veto is False:
                            denied = True
                            break
                    except Exception:
                        denied = True
                        break
            if not denied and decision.action == 'ask':
                display = self.redactor.clean({'call_id': cid, 'tool': tool.name, 'source': source,
                    'arguments': args, 'target': prepared.target, 'policy_version': decision.version})
                if self.confirm:
                    approved = (await maybe_await(self.confirm(copy.deepcopy(display)))) is True
                denied = not approved
            audit = self.policy.audit(cid, tool, decision, approved=approved, status='denied' if denied else 'approved')
            record['audit'] = audit
            if denied:
                result = self.result(cid, 'denied', '本次操作被拒绝或未获批准。', source)
            else:
                await maybe_await(prepared.recheck())
                record['status'] = 'started'
                await self.save()
                started = True
                async with asyncio.timeout(self.timeout):
                    output = await maybe_await(prepared.execute())
                status = output.get('status', 'success') if isinstance(output, dict) else 'success'
                if status not in {'success', 'error', 'denied', 'cancelled', 'outcome_unknown'}:
                    status = 'error'
                result = self.result(cid, status, output, source)
        except PersistenceError:
            raise
        except asyncio.CancelledError as exc:
            if isinstance(getattr(exc, 'result', None), dict):
                result = self.result(cid, 'cancelled', exc.result, source)
            else:
                result = self.result(cid, 'outcome_unknown' if started else 'not_executed', '操作已中断；已开始的操作需核实结果。', source)
        except TimeoutError:
            result = self.result(cid, 'outcome_unknown' if started else 'not_executed', '操作超时，不自动重试。', source)
        except (ToolError, ValueError, OSError) as exc:
            result = self.result(cid, 'error', str(exc), source)
        except Exception as exc:
            output = exc.result if isinstance(getattr(exc, 'result', None), dict) else '工具发生异常，不自动重试。'
            result = self.result(cid, 'outcome_unknown' if started else 'error', output, source)
        record.update(status=result.status, result=result.as_dict())
        await self.save()
        for hook in self.after:
            try:
                await maybe_await(hook(copy.deepcopy(record)))
            except Exception:
                record.setdefault('observer_errors', []).append('工具结果观察回调失败。')
        return result


@dataclass
class RunResult:
    status: str
    text: str = ''
    model_calls: int = 0


class Agent:
    def __init__(self, model, dispatcher, *, max_rounds=10, checkpoint=None, show_result=None):
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 1:
            raise ValueError('模型轮数必须为正整数。')
        self.model, self.dispatcher = model, dispatcher
        self.max_rounds = max_rounds
        self.checkpoint, self.show_result = checkpoint, show_result

    async def save(self):
        if self.checkpoint:
            try:
                await maybe_await(self.checkpoint())
            except Exception:
                raise PersistenceError('保存失败，停止后续操作。') from None

    async def run(self, history, text=None):
        if text is not None:
            history.append({'role': 'user', 'content': self.dispatcher.redactor.text(text)})
            await self.save()
        for number in range(1, self.max_rounds + 1):
            reply = await self.model.complete(history, tools=self.dispatcher.registry.definitions())
            history.append(reply)
            await self.save()
            calls = reply.get('tool_calls', [])
            if not calls:
                return RunResult('success', reply['content'], number)
            stop = 'limit' if number == self.max_rounds else None
            for call in calls:
                if stop:
                    result = self.dispatcher.result(call['id'], 'not_executed', '本轮已停止，未执行此调用。')
                else:
                    result = await self.dispatcher.dispatch(call)
                    if result.status in {'cancelled', 'outcome_unknown', 'not_executed'}:
                        stop = result.status
                history.append(result.message())
                await self.save()
                if self.show_result:
                    await maybe_await(self.show_result(copy.deepcopy(result.as_dict())))
            if stop:
                return RunResult(stop, '已到模型轮数上限。' if stop == 'limit' else '本轮已中断，请核实已开始的操作。', number)
        return RunResult('limit', '已到模型轮数上限。', self.max_rounds)
