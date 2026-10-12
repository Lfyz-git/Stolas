import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/terminal.py").exists()
if HAS_TOOLS:
    from tools.terminal import Terminal
    from tools import install

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@unittest.skipUnless(HAS_TOOLS, "Installer UI not shipped in Core image")
class TerminalTests(unittest.TestCase):
    def test_semantic_colors_and_monochrome_fallback(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        terminal = Terminal(stream)
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            terminal.stage("Проверка системы", 1)
            terminal.result("работает")
            terminal.result("требуется проверка", "warning")
            terminal.result("нужно число", "error")
        for code in ("36", "32", "33", "31"):
            self.assertIn("\x1b[" + code + "m", stream.getvalue())
        for env in ({"NO_COLOR": ""}, {"TERM": "dumb"}):
            stream.seek(0); stream.truncate(0)
            with patch.dict(os.environ, env):
                terminal.stage("Проверка системы", 1)
            self.assertNotIn("\x1b", stream.getvalue())
        stream.isatty = lambda: False
        self.assertEqual(terminal.style("value", "heading"), "value")

    def test_80_columns_long_values_and_pasted_input(self):
        stream = io.StringIO()
        stream.isatty = lambda: False
        with contextlib.redirect_stdout(stream), patch.dict(os.environ, {"COLUMNS": "80"}), patch("builtins.input", return_value="  192.0.2.1/32  "):
            value = install.ask("Разрешённые IP", ",".join(["192.0.2.1/32"] * 20), install.cidrs)
        self.assertEqual(value, ["192.0.2.1/32"])
        self.assertTrue(all(len(line) <= 80 for line in stream.getvalue().splitlines()))

    def test_full_plain_session_has_short_questions_and_visible_spacing(self):
        result = subprocess.run([sys.executable, str(ROOT / "tests/terminal_session.py")], input="\n192.0.2.1/32\n\n", text=True, encoding="utf-8", capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8", "TERM": "dumb"}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        output = result.stdout
        self.assertNotIn("\x1b", output)
        self.assertEqual(output.count("Ввод"), 3)
        for field in ("n8n", "Telegram", "required", "route.mode", "gateway"):
            self.assertNotIn(field, output)
        self.assertIn("Проверять основной канал перед тестами?\n\n  1.", output)
        self.assertIn("\n\nВыберите вариант\nВвод [1]:", output)
        self.assertIn("Шаг 4 из 4", output)
        self.assertIn("Готово: Stolas Core запущен", output)
        self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real SSH-style PTY runs in Linux CI")
    def test_management_real_tty_and_no_color(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        for monochrome in (False, True):
            with self.subTest(NO_COLOR=monochrome):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": "80", "PYTHONIOENCODING": "utf-8"}
                env.pop("NO_COLOR", None)
                if monochrome:
                    env["NO_COLOR"] = "1"
                process = subprocess.Popen([sys.executable, str(ROOT / "tests/management_session.py")], stdin=slave, stdout=slave, stderr=slave, env=env)
                os.close(slave)
                output = bytearray()
                try:
                    os.write(master, b"wrong\n3\n")
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], .2)[0]:
                            try:
                                data = os.read(master, 65536)
                            except OSError:
                                break
                            if not data:
                                break
                            output.extend(data)
                        elif process.poll() is not None:
                            break
                    self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait()
                    os.close(master)
                rendered = output.decode()
                if monochrome:
                    self.assertNotIn("\x1b", rendered)
                else:
                    for code in ("36", "32", "31"):
                        self.assertIn("\x1b[" + code + "m", rendered)
                plain = ANSI.sub("", rendered).replace("\r", "")
                self.assertIn("STOLAS · Удаление", plain)
                self.assertIn("STOLAS · Диагностика", plain)
                self.assertIn("введите номер от 1 до 3", plain)
                self.assertIn("удаление отменено", plain)
                self.assertNotIn("a" * 40, plain)
                self.assertTrue(all(len(line) <= 80 for line in plain.splitlines()))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real SSH-style PTY runs in Linux CI")
    def test_real_tty_80_columns_color_navigation_and_errors(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": "80", "PYTHONIOENCODING": "utf-8"}
        env.pop("NO_COLOR", None)
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/terminal_session.py")], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        output = bytearray()
        try:
            os.write(master, b"wrong\n\n 192.0.2.1/32 \n:back\n\n\n\n")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)
                elif process.poll() is not None:
                    break
            self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            os.close(master)
        rendered = output.decode()
        self.assertIn("\x1b[36m", rendered)
        self.assertIn("\x1b[31m", rendered)
        plain = ANSI.sub("", rendered).replace("\r", "")
        self.assertIn("введите номер от 1 до 2", plain)
        self.assertGreaterEqual(plain.count("Шаг 2 из 4"), 2)
        self.assertIn("Stolas Core запущен", plain)
        self.assertTrue(all(len(line) <= 80 for line in plain.splitlines()))
        # CI artifact is the actual terminal output, including ANSI colors.
        artifact = os.getenv("STOLAS_TTY_ARTIFACT")
        if artifact:
            Path(artifact).write_text(rendered, encoding="utf-8")
