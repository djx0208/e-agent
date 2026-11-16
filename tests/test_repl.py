import contextlib
import io
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import importlib.util

spec = importlib.util.spec_from_file_location("day01", Path(__file__).resolve().parents[1] / "examples/day01/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

ROOT = Path(__file__).resolve().parents[1]


class ReplTests(unittest.TestCase):
    def test_echo_commands_blank_and_interrupt(self):
        output = io.StringIO()
        inputs = ["", "  ", "first", KeyboardInterrupt, "second /help", "/help", "/unknown", EOFError]
        with patch("builtins.input", side_effect=inputs), contextlib.redirect_stdout(output):
            self.assertEqual(main.main(), 0)
        text = output.getvalue()
        for expected in ["first", "second /help", "已取消输入", "未知命令", "/exit", "再见"]:
            self.assertIn(expected, text)

    def test_no_model_configuration_needed(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("E_AGENT_")}
        result = subprocess.run([sys.executable, str(ROOT / "examples/day01/main.py")], input="one\ntwo\n/exit\n", env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("one", result.stdout)
        self.assertIn("two", result.stdout)

    def test_real_terminal_cancels_unsubmitted_input(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, str(ROOT / "examples/day01/main.py")])

        def until(marker):
            output = bytearray()
            deadline = time.monotonic() + 5
            while marker not in output and time.monotonic() < deadline:
                if select.select([fd], [], [], 0.1)[0]:
                    output.extend(os.read(fd, 4096))
            self.assertIn(marker, output)
            return output

        try:
            until(b"> ")
            os.write(fd, b"discard")
            until(b"discard")
            os.write(fd, b"\x03")
            until("已取消输入。\r\n> ".encode())
            os.write(fd, b"next\n")
            self.assertNotIn(b"discard", until(b"next\r\n> "))
            os.write(fd, b"\x04")
            until("再见".encode())
        finally:
            os.close(fd)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
