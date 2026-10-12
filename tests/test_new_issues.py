import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.error
from unittest.mock import patch

from agent import probes
from agent.config import DEFAULT, load

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/progress.py").exists()


class PriorityTests(unittest.TestCase):
    def test_strict_response_formats(self):
        for body, valid in [(b'203.0.113.4\n', True), (b'"198.51.100.7"', True),
                            (b'{"ip":"203.0.113.4"}', False), (b'"::1"', False),
                            (b'"203.0.113.4" trailing', False), (b'null', False),
                            (b'<html>203.0.113.4</html>', False), (b'123', False),
                            (b'"203.0.113.4"' + b' ' * 200, False)]:
            with self.subTest(body=body), patch.object(probes.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = body
                if valid:
                    self.assertIn(probes._read_public_ip("https://example.test", 1), ("203.0.113.4", "198.51.100.7"))
                else:
                    with self.assertRaisesRegex(probes.ProbeError, "invalid_ipv4"):
                        probes._read_public_ip("https://example.test", 1)

    def test_network_failures(self):
        for error, reason in [(TimeoutError(), "timeout"),
                              (urllib.error.HTTPError("https://example.test", 503, "fail", {}, None), "http_error"),
                              (urllib.error.URLError(TimeoutError()), "timeout")]:
            with patch.object(probes.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(probes.ProbeError, reason):
                    probes._read_public_ip("https://example.test", 1)

    def test_priority_fallback_and_threshold(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg["route"]["expected_public_cidrs"] = ["192.0.2.0/24"]
        route = subprocess.CompletedProcess([], 0, '[{"dev":"eth0"}]')
        self.assertEqual(cfg["route"]["public_ip_urls"][0], "https://yandex.ru/internet/api/v0/ip")
        cases = [(1, ["192.0.2.1"], 1, "verified"),
                 (1, [probes.ProbeError("timeout"), "192.0.2.1"], 2, "verified"),
                 (1, ["198.51.100.1", "192.0.2.1"], 2, "mixed"),
                 (2, ["192.0.2.1", "198.51.100.1", "192.0.2.1"], 3, "mixed")]
        for minimum, values, count, status in cases:
            cfg["route"]["min_confirmations"] = minimum
            with patch.object(probes.subprocess, "run", return_value=route), patch.object(probes, "probe_public_ip", side_effect=values) as source:
                result = probes._guard(cfg, "203.0.113.1")
            self.assertEqual(source.call_count, count)
            self.assertEqual(result["verification_status"], status)
            self.assertTrue(all(s["status"] == "skipped" for s in result["sources"][count:]))
            self.assertEqual([call.args[0] for call in source.call_args_list], cfg["route"]["public_ip_urls"][:count])

    def test_vpn_does_not_satisfy_confirmation_threshold(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg["route"].update(expected_public_cidrs=["192.0.2.0/24"], min_confirmations=2)
        with patch.object(probes.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '[{}]')), patch.object(probes, "probe_public_ip", side_effect=["192.0.2.1"] + ["198.51.100.1"] * 3):
            with self.assertRaisesRegex(probes.ProbeError, "route_verification_unavailable"):
                probes._guard(cfg, "203.0.113.1")

    def test_existing_custom_order_preserved(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg["route"]["public_ip_urls"] = ["https://checkip.amazonaws.com", "https://api.ipify.org"]
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertEqual(load(path)["route"]["public_ip_urls"], cfg["route"]["public_ip_urls"])


@unittest.skipUnless(HAS_TOOLS, "Host tools outside Core image")
class ProgressTests(unittest.TestCase):
    def run_child(self, code, tty=False, timeout=5, machine=False):
        from tools import progress
        stream = io.StringIO()
        stream.isatty = lambda: tty
        with patch.dict(os.environ, {"TERM": "xterm", "CI": ""}), patch("sys.stdout.isatty", return_value=tty):
            result = progress.run([sys.executable, "-c", code], ROOT, os.environ.copy(), "Этап", timeout, machine=machine, stream=stream)
        return result, stream.getvalue()

    def test_silent_child_has_live_elapsed_activity(self):
        result, output = self.run_child("import time; time.sleep(.8)", tty=True)
        self.assertEqual(result.returncode, 0)
        self.assertGreater(output.count("\r"), 2)
        self.assertIn("готово", output)
        self.assertIn("с", output)

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

    def test_plain_failure_and_bounded_capture(self):
        result, output = self.run_child('import sys; print("x" * 100000); sys.exit(7)')
        self.assertEqual(result.returncode, 7)
        self.assertLessEqual(len(result.stdout), 65536)
        self.assertIn("код 7", output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\x1b", output)

    def test_timeout_reaps_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_child("import time; time.sleep(30)", timeout=.3)

    def test_ci_tty_uses_plain_stages(self):
        from tools import progress
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.dict(os.environ, {"CI": "1", "TERM": "xterm"}):
            progress.run([sys.executable, "-c", "import time; time.sleep(.4)"], ROOT, os.environ.copy(), "Этап", 5, stream=stream)
        self.assertNotIn("\r", stream.getvalue())
        self.assertNotIn("\x1b", stream.getvalue())

    def test_progress_module_is_in_runtime_installer(self):
        from tools import deploy
        files = deploy.runtime_files(ROOT)
        self.assertEqual(files[".stolas/installer/tools/progress.py"], ROOT / "tools/progress.py")

    def test_stdout_pipe_disables_redraw_even_with_tty_stderr(self):
        from tools import progress
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch("sys.stdout.isatty", return_value=False):
            progress.run([sys.executable, "-c", "import time; time.sleep(.4)"], ROOT, os.environ.copy(), "Этап", 5, stream=stream)
        self.assertNotIn("\r", stream.getvalue())

    def test_interrupt_stops_child_and_finishes_indicator(self):
        from tools import progress
        stream = io.StringIO()
        with patch.object(progress.subprocess, "Popen") as popen:
            process = popen.return_value
            process.wait.side_effect = [KeyboardInterrupt(), 0]
            process.poll.return_value = None
            with patch.object(progress, "stop") as stop, self.assertRaises(KeyboardInterrupt):
                progress.run(["fake"], ROOT, {}, "Этап", 5, stream=stream)
            stop.assert_called_once_with(process)
        self.assertIn("прервано", stream.getvalue())

    def test_first_test_disables_interactive_stdin(self):
        from tools import install
        with patch.object(install, "run", return_value=subprocess.CompletedProcess([], 0, '{"status":"ok"}', "")) as run:
            install.first_test(["docker", "compose"], ROOT)
        self.assertIn("--interactive=false", run.call_args.args[0])

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

