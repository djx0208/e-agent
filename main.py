"""第一单元：无需模型配置的终端输入循环。"""

HELP = "/help  查看帮助\n/exit  退出\nEnter 提交；Ctrl+C 取消输入；Ctrl+D 退出。"


def main():
    print("e-agent · 输入 /help 查看帮助")
    while True:
        try:
            text = input("> ")
        except EOFError:
            print("再见。")
            return 0
        except KeyboardInterrupt:
            print("\n已取消输入。")
            continue
        command = text.strip()
        if not command:
            continue
        if command == "/exit":
            print("再见。")
            return 0
        if command == "/help":
            print(HELP)
        elif command.startswith("/"):
            print("未知命令，请输入 /help。")
        else:
            print(text)


if __name__ == "__main__":
    raise SystemExit(main())
