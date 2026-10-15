import contextlib
import copy
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
    from tools import install

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@unittest.skipUnless(HAS_TOOLS, "Installer UI not shipped in Core image")
class TerminalTests(unittest.TestCase):
    def test_shared_prompts_preserve_configuration_and_hide_secret_defaults(self):
        original = copy.deepcopy(install.DEFAULT)
        original["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
        with contextlib.redirect_stdout(io.StringIO()) as output, patch("builtins.input", return_value=""):
            self.assertEqual(install.collect_config(original), original)
        with contextlib.redirect_stdout(output), patch.object(install.getpass, "getpass", return_value="") as prompt:
            self.assertEqual(install.ask("Токен", "fixture-secret", secret=True), "fixture-secret")
        self.assertNotIn("fixture-secret", output.getvalue() + prompt.call_args.args[0])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real SSH-style PTY runs in Linux CI")
    def test_real_tty_installation_retries_invalid_input(self):
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
            os.write(master, b"wrong\n\n 192.0.2.1/32 \n\n")
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
        plain = ANSI.sub("", rendered).replace("\r", "")
        self.assertIn("введите номер от 1 до 2", plain)
        self.assertIn("Stolas Core запущен", plain)
        # CI artifact is the actual terminal output, including ANSI colors.
        artifact = os.getenv("STOLAS_TTY_ARTIFACT")
        if artifact:
            Path(artifact).write_text(rendered, encoding="utf-8")
