"""终端交互和单事件循环中的多轮模型对话。"""
import argparse
import asyncio
import signal
import sys

HELP = '''/help  查看帮助
/exit  退出
Enter 提交；Ctrl+C 取消输入或当前请求；Ctrl+D 退出。
当前对话仅保存在内存，退出后不保留。'''


async def repl(config_path=None, input_fn=None, output_fn=print, model=None):
    from .config import ConfigError, load_config
    from .model import ChatModel, ModelError

    read = input_fn or input
    history = []
    active = None
    loop = asyncio.get_running_loop()
    output_fn('e-agent · 输入 /help 查看帮助')

    def interrupt(signum, frame):
        if active is not None:
            loop.call_soon_threadsafe(active.cancel)
        else:
            raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGINT, interrupt)

    async def request():
        nonlocal model
        if model is None:
            config = load_config(config_path)
            config.validate()
            model = ChatModel(config)
        return await model.complete(history)

    try:
        while True:
            try:
                text = read('> ')
            except EOFError:
                output_fn('再见。')
                return 0
            except KeyboardInterrupt:
                output_fn('\n已取消输入。')
                continue
            command = text.strip()
            if not command:
                continue
            if command == '/exit':
                output_fn('再见。')
                return 0
            if command == '/help':
                output_fn(HELP)
                continue
            if command.startswith('/'):
                output_fn('未知命令，请输入 /help。')
                continue
            history.append({'role': 'user', 'content': text})
            try:
                active = asyncio.create_task(request())
                reply = await active
                if reply.get('tool_calls'):
                    raise ModelError('当前版本尚未接通工具执行，本次未执行任何工具。')
                history.append(reply)
                output_fn(reply['content'])
            except asyncio.CancelledError:
                output_fn('已取消当前请求。')
            except (ModelError, ConfigError) as exc:
                output_fn('错误：' + str(exc))
            finally:
                active = None
    finally:
        signal.signal(signal.SIGINT, old_handler)
        if model is not None:
            await model.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='e-agent · DeepSeek 本地代码助手')
    parser.add_argument('--config', help='显式模型配置文件路径；省略时使用用户配置目录')
    args = parser.parse_args(argv)
    return asyncio.run(repl(args.config))
