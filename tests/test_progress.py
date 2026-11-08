"""Noninteractive CLI output, secret redaction and child process sessions."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/progress.py").exists()


@unittest.skipUnless(HAS_TOOLS, "Host tools outside Core image")
class ProgressTests(unittest.TestCase):
    def run_child(self, code, tty=False, timeout=5, machine=False):
        from tools import progress
        stream = io.StringIO()
        stream.isatty = lambda: tty
        with patch.dict(os.environ, {"TERM": "xterm", "CI": ""}), patch("sys.stdout.isatty", return_value=tty):
            result = progress.run([sys.executable, "-c", code], ROOT, os.environ.copy(), "Этап", timeout, machine=machine, stream=stream)
        return result, stream.getvalue()

    @unittest.skipUnless(os.name == "posix", "POSIX sudo TTY session and process groups")
    def test_cleanup_group_preserves_session_for_sudo_tty_cache(self):
        result, _ = self.run_child('import os,json; print(json.dumps([os.getsid(0), os.getpgrp(), os.getpid()]))', machine=True)
        session, group, pid = json.loads(result.stdout)
        self.assertEqual(session, os.getsid(0))
        self.assertEqual(group, pid)
        self.assertNotEqual(group, os.getpgrp())

    def test_machine_output_and_secrets_never_enter_indicator(self):
        result, output = self.run_child('import sys,time; print(\'{"ok":true}\'); print("#1 RUN token=private-secret https://secret.example", file=sys.stderr, flush=True); time.sleep(.6)', tty=True, machine=True)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertIn("RUN", output)
        self.assertNotIn("private-secret", output)
        self.assertNotIn("secret.example", output)

    def test_history_with_closed_stdin_and_redirected_stdout(self):
        import tempfile
        # A real child exercises manage.main and the Compose argument boundary.
        # The fake Compose blocks if interactive stdin was accidentally enabled.
        stub = 'import sys,time,json; time.sleep(60) if "--interactive=false" not in sys.argv else None; assert sys.stdin.read() == ""; print(json.dumps([{"status":"ok"}]))'
        for action in ("history", "run"):
            script = '''import sys
from unittest.mock import patch
from tools import manage
with patch.object(manage.resources, "command", return_value=["docker"]), patch.object(manage.resources, "compose", return_value=[sys.executable, "-c", %r]):
    sys.argv = ["stolas", "--root", %r, %r, "--json"]
    sys.exit(manage.main())
''' % (stub, str(ROOT), action)
            with tempfile.TemporaryFile() as output:
                result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE, timeout=10)
                output.seek(0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.load(output), [{"status": "ok"}])
