import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from agent.storage import Store

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/install.py").is_file()
if HAS_TOOLS:
    from tools import install, manage


@unittest.skipUnless(HAS_TOOLS, "Human output outside Core image")
class BusyPresentationTests(unittest.TestCase):
    def cycle(self, valid=True):
        return {"id": "busy-then-success", "started_epoch": 1, "status": "ok" if valid else "unavailable", "primary": {"server": "next-server", "valid": True, "download": {"mbps": 100}, "upload": {"mbps": 90}} if valid else None, "attempts": [{"server": "busy-server", "errors": ["server_busy"]}], "errors": [{"reason": "server_busy"}], "confirmation": None}

    def render(self, data):
        out = io.StringIO()
        with patch.object(install, "run", return_value=subprocess.CompletedProcess([], 0 if data["status"] == "ok" else 2, json.dumps(data))), contextlib.redirect_stdout(out):
            reply = install.first_test(["docker", "compose"], ROOT)
        self.assertEqual(reply, data)
        return " ".join(out.getvalue().split())

    def test_success_suppresses_busy_warning_but_preserves_attempts_and_sqlite(self):
        data = self.cycle()
        before = json.dumps(data)
        output = self.render(data)
        self.assertIn("DL 100 / UL 90", output)
        self.assertNotIn("повторите тест позже", output)
        self.assertNotIn("сервер занят", output)
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.save(data, 100)
            self.assertEqual(json.dumps(store.history()[0]), before)

    def test_all_busy_keeps_actionable_warning(self):
        output = self.render(self.cycle(False))
        self.assertIn("сервер занят", output)
        self.assertIn("повторите тест позже", output)

    def test_success_keeps_unrelated_dns_diagnostic(self):
        data = self.cycle()
        data["attempts"].append({"errors": ["dns_error"]})
        self.assertIn("Проверьте DNS", self.render(data))

    def test_json_run_uses_original_agent_output_without_human_filter(self):
        with tempfile.TemporaryDirectory() as folder, patch("sys.argv", ["stolas", "--root", folder, "run", "--json"]), patch.object(manage.resources, "command", return_value=["docker"]), patch.object(manage.resources, "compose", return_value=["docker", "compose"]), patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as command, patch.object(manage, "first_test") as human:
            self.assertEqual(manage.main(), 0)
        human.assert_not_called()
        self.assertEqual(command.call_args.args[0][-1], "run")
        self.assertNotIn("capture_output", command.call_args.kwargs)
