"""Installer orchestration tests; never install packages or contact public services."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_INSTALLER = (ROOT / "tools/install.py").exists()
if HAS_INSTALLER:
    from tools import install as installer


@unittest.skipUnless(HAS_INSTALLER, "Installer is not shipped in the production image")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "n8n").mkdir()
        (self.root / "n8n/stolas.json").write_bytes((ROOT / "n8n/stolas.json").read_bytes())
        self.cfg = copy.deepcopy(installer.DEFAULT)
        self.cfg["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
        self.api = dict(STOLAS_API_TOKEN="a" * 40, STOLAS_LISTEN="127.0.0.1", STOLAS_PORT="8080")
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def install(self, options=None, configure_only=False, cli_status="ok", cli_code=0):
        self.calls = []

        def command(args, root, capture=False, check=True):
            self.calls.append(args)
            if "exec" in args:
                # The CLI must see the same committed settings/token as the API.
                self.assertEqual(installer.validated(root / "config/local.json"), self.cfg)
                self.assertIn(self.api["STOLAS_API_TOKEN"], (root / ".env").read_text())
                return subprocess.CompletedProcess(args, cli_code, json.dumps({"status": cli_status, "primary": None, "confirmation": None}), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        options = {"topology": "native", "notification_mode": "alerts_only", **(options or {"mode": "later"})}
        facts = {"hostname": "test", "installation": {"directory": str(self.root), "config": False}, "docker": {"available": True, "version": "test"}, "n8n": [], "warnings": []}
        with patch.object(installer, "collect_plan", return_value={"config": self.cfg, "api": self.api, "n8n": options, "completed": []}), patch.object(installer.environment, "discover", return_value=facts), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "check_connection"), patch.object(installer, "run", side_effect=command):
            return installer.install(self.root, configure_only=configure_only)

    def test_later_starts_service_and_runs_exactly_one_cli_cycle(self):
        with patch.object(installer, "request_json") as request:
            self.assertEqual(self.install(), 0)
        request.assert_not_called()
        self.assertEqual(sum("exec" in args for args in self.calls), 1)
        self.assertTrue(any("--wait" in args for args in self.calls))
        self.assertTrue(any("validate" in args for args in self.calls))
        self.assertFalse((self.root / "n8n/local.json").exists())
        self.assertNotIn(self.api["STOLAS_API_TOKEN"], self.output.getvalue())

    def test_configure_only_never_runs_commands_or_network(self):
        self.assertEqual(self.install(configure_only=True), 0)
        self.assertEqual(self.calls, [])

    def test_failed_measurement_is_not_reported_as_success(self):
        self.assertEqual(self.install(cli_status="route_blocked", cli_code=2), 2)
        self.assertIn("измерение не получено", self.output.getvalue())

    def test_low_speed_is_a_completed_test_and_busy_is_an_error(self):
        self.assertEqual(self.install(cli_status="low_confirmed", cli_code=2), 0)
        with self.assertRaisesRegex(RuntimeError, "CLI-тест не запущен"):
            self.install(cli_code=1)

    def test_invalid_config_does_not_overwrite_existing_files(self):
        self.install(configure_only=True)
        before = (self.root / "config/local.json").read_bytes()
        self.cfg["parallel"] = 0
        with self.assertRaises(ValueError):
            self.install(configure_only=True)
        self.assertEqual((self.root / "config/local.json").read_bytes(), before)

    def test_defaults_are_all_prompted_and_route_must_be_explicit(self):
        # 1 node + 10 numeric + bind + guard + URL + CIDRs + interface/gateway
        # + selection, count and five fields in each of the three groups.
        answers = [""] * 13 + ["", "", "", "", "", "192.0.2.1/32", "", ""] + [""] * 21
        with patch("builtins.input", side_effect=answers) as prompt:
            actual = installer.collect_config(copy.deepcopy(installer.DEFAULT))
        self.assertEqual(prompt.call_count, len(answers))
        self.assertEqual(actual, self.cfg)
        with self.assertRaises(ValueError):
            installer.cidrs("")

    def test_wizard_accepts_more_than_sixteen_servers_and_empty_secondary_groups(self):
        count = 0

        def reply(prompt):
            nonlocal count
            if prompt.startswith("Количество серверов в primary"):
                return "17"
            if prompt.startswith("Количество серверов в"):
                return "0"
            if prompt.startswith("Выбор сервера в группе primary"):
                return "random"
            if prompt == "Hostname или IPv4: ":
                count += 1
                return f"custom-{count}.example.test"
            return ""

        with patch("builtins.input", side_effect=reply):
            cfg = installer.collect_config(copy.deepcopy(self.cfg))
        self.assertEqual(len(cfg["server_groups"]["primary"]["servers"]), 17)
        self.assertEqual(cfg["server_groups"]["primary"]["selection"], "random")
        for name in ("additional", "emergency"):
            self.assertEqual(cfg["server_groups"][name]["servers"], [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux shell entry point")
    def test_shell_entrypoint_with_scripted_input_and_fake_docker(self):
        # Exercise the real shell -> wizard -> Compose/CLI subprocess boundary.
        shutil.copytree(ROOT / "agent", self.root / "agent")
        (self.root / "tools").mkdir()
        shutil.copy2(ROOT / "tools/install.py", self.root / "tools/install.py")
        shutil.copy2(ROOT / "tools/deploy.py", self.root / "tools/deploy.py")
        shutil.copy2(ROOT / "tools/environment.py", self.root / "tools/environment.py")
        shutil.copy2(ROOT / "install.sh", self.root / "install.sh")
        shutil.copy2(ROOT / "compose.yaml", self.root / "compose.yaml")
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = bindir / "docker"
        stub.write_text("#!" + sys.executable + "\n" + '''import json, pathlib, sys
with pathlib.Path("calls.jsonl").open("a") as file:
    file.write(json.dumps(sys.argv[1:]) + "\\n")
if "context" in sys.argv:
    print("unix:///var/run/docker.sock")
elif "info" in sys.argv and "--format" in sys.argv:
    print("x86_64")
elif "exec" in sys.argv:
    print(json.dumps({"status": "ok", "primary": None, "confirmation": None}))
''')
        stub.chmod(0o755)
        answers = ["", "192.0.2.1/32", "later", "apply"]
        env = installer.clean_env()
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        result = subprocess.run(["sh", "install.sh"], cwd=self.root, env=env, input="\n".join(answers) + "\n", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum("exec" in call for call in calls), 1)
        self.assertTrue(any("--wait" in call for call in calls))
        actual = installer.validated(self.root / "config/local.json")
        self.assertEqual({k: v for k, v in actual.items() if k != "node"}, {k: v for k, v in self.cfg.items() if k != "node"})
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("STOLAS_API_TOKEN=", result.stdout)

    def test_overrides_do_not_change_generated_configuration(self):
        with patch.dict(os.environ, {"STOLAS_PARALLEL": "0"}):
            self.install(configure_only=True)
            self.assertEqual(installer.validated(self.root / "config/local.json")["parallel"], 4)
            self.assertEqual(os.environ["STOLAS_PARALLEL"], "0")

    def test_update_keeps_files_and_does_not_run_extra_load_test(self):
        self.install(configure_only=True)
        before = {name: (self.root / name).read_bytes() for name in (".env", "config/local.json")}
        with patch.object(installer, "collect_config") as wizard, patch("builtins.input", return_value="apply"), patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(installer, "first_test") as heavy, patch.object(installer, "request_json", return_value={"status": "ready"}) as health:
            self.assertEqual(installer.install(self.root, reuse=True), 0)
        heavy.assert_not_called()
        wizard.assert_not_called()
        health.assert_called_once()
        for name, value in before.items():
            self.assertEqual((self.root / name).read_bytes(), value)

    def test_repeated_n8n_failure_preserves_completed_first_measurement(self):
        self.install(configure_only=True)
        progress = self.root / ".stolas-progress.json"
        progress.write_text(json.dumps({"config_sha256": hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest(),
                                       "stage": "measured", "first_status": "ok", "measurement_done": True}))
        options = dict(mode="export", topology="native", endpoint="http://127.0.0.1:8080", chat_id="123", hours=3, timezone="Etc/UTC", notification_mode="alerts_only")
        plan = dict(config=self.cfg, api=self.api, n8n=options, completed=["wan", "n8n"])
        facts = {"hostname": "test", "installation": {"directory": str(self.root), "config": True}, "docker": {"available": True, "version": "test"}, "n8n": [], "warnings": []}
        with patch.object(installer, "collect_plan", return_value=plan), patch.object(installer.environment, "discover", return_value=facts), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(installer, "first_test") as heavy, patch.object(installer, "connect_n8n"):
            with patch.object(installer, "check_connection", side_effect=RuntimeError("unreachable")), self.assertRaises(RuntimeError):
                installer.install(self.root)
            self.assertTrue(json.loads(progress.read_text())["measurement_done"])
            with patch.object(installer, "check_connection"):
                self.assertEqual(installer.install(self.root), 0)
        heavy.assert_not_called()

    def test_atomic_backups_are_private_and_symlinks_are_rejected(self):
        path = self.root / ".env"
        installer.write_private(path, "first-secret")
        installer.write_private(path, "second-secret")
        backup = next(self.root.glob(".env.bak-*"))
        self.assertEqual(backup.read_text(), "first-secret")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            link = self.root / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                installer.write_private(link, "bad")
            self.assertEqual(path.read_text(), "second-secret")

    def test_export_schedule_and_settings_have_no_secrets(self):
        options = dict(mode="export", endpoint="https://stolas.example.test", chat_id="-12345", hours=6, timezone="Etc/UTC")
        self.assertEqual(self.install(options), 0)
        workflow = json.loads((self.root / "n8n/local.json").read_text())
        nodes = {n["name"]: n for n in workflow["nodes"]}
        self.assertEqual(nodes["Every 3 hours"]["parameters"]["rule"]["interval"][0]["hoursInterval"], 6)
        self.assertIn(options["endpoint"], nodes["Settings"]["parameters"]["jsCode"])
        self.assertFalse(workflow["active"])
        self.assertNotIn(self.api["STOLAS_API_TOKEN"], json.dumps(workflow))
        self.assertTrue(all("credentials" not in node for node in workflow["nodes"]))

    def test_public_plain_http_and_credentials_in_urls_are_rejected(self):
        for value in ("http://example.com", "https://user:pass@example.com", "https://example.com?key=secret", "https://example.com:99999"):
            with self.assertRaises(ValueError):
                installer.url(value)
        self.assertEqual(installer.url("http://127.0.0.1:5678/"), "http://127.0.0.1:5678")

    def test_n8n_real_http_creates_credentials_and_inactive_workflow(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.reply({"properties": {"name": {}, "value": {}, "accessToken": {}}} if "/credentials/schema/" in self.path else {"status": "ready"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, payload, self.headers.get("X-N8N-API-KEY")))
                self.reply({"id": str(len(requests))})

            def reply(self, data):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        options = dict(mode="api", endpoint=base, chat_id="12345", hours=3, timezone="Etc/UTC", url=base, key="private-n8n-key", bot_token="123:private-bot-token")
        self.install(options)
        self.assertEqual([r[0] for r in requests], ["/api/v1/credentials", "/api/v1/credentials", "/api/v1/workflows"])
        self.assertEqual(requests[0][1]["data"]["value"], "Bearer " + self.api["STOLAS_API_TOKEN"])
        payload = requests[-1][1]
        self.assertNotIn("active", payload)
        self.assertTrue(any("credentials" in n for n in payload["nodes"]))
        for path in (self.root / "n8n").glob("*.json*"):
            for secret in (options["key"], options["bot_token"], self.api["STOLAS_API_TOKEN"]):
                self.assertNotIn(secret, path.read_text())
        installer.connect_n8n(self.root, options, self.api, installer.workflow(self.root, options))
        self.assertEqual(len(requests), 3)


if __name__ == "__main__":
    unittest.main()
