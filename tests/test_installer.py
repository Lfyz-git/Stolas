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
    from tools import deploy, integrate, resources


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
        with patch.object(installer, "collect_plan", return_value={"config": self.cfg, "api": self.api, "n8n": options, "completed": []}), patch.object(installer.environment, "discover", return_value=facts), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "request_json", return_value={"status": "ready"}), patch.object(installer, "run", side_effect=command):
            return installer.install(self.root, configure_only=configure_only)

    def test_later_starts_service_and_runs_exactly_one_cli_cycle(self):
        self.assertEqual(self.install(), 0)
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
        with self.assertRaisesRegex(RuntimeError, "Тест не запущен"):
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
        original = installer.ask
        count = 0
        def ask(label, default="", convert=str, secret=False):
            nonlocal count
            if label == "Количество серверов — основные":
                return 17
            if label.startswith("Количество серверов —"):
                return 0
            if label == "Hostname или IPv4" and not default:
                count += 1
                return f"custom-{count}.example.test"
            return convert(default)
        with patch.object(installer, "ask", side_effect=ask), patch.object(installer, "choice", return_value="random"):
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
        shutil.copy2(ROOT / "tools/timezones.py", self.root / "tools/timezones.py")
        shutil.copy2(ROOT / "tools/terminal.py", self.root / "tools/terminal.py")
        shutil.copy2(ROOT / "tools/layout.py", self.root / "tools/layout.py")
        shutil.copy2(ROOT / "tools/entrypoints.py", self.root / "tools/entrypoints.py")
        shutil.copy2(ROOT / "stolas", self.root / "stolas")
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
    print("x86_64" if sys.argv[-1] == "{{.Architecture}}" else "linux x86_64 fixture")
elif "exec" in sys.argv:
    print(json.dumps({"status": "ok", "primary": None, "confirmation": None}))
''')
        stub.chmod(0o755)
        answers = ["", "192.0.2.1/32", "apply"]
        env = installer.clean_env()
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        destination = self.root / "runtime"
        result = subprocess.run(["sh", "install.sh", "--target", str(destination), "--configure-only"], cwd=self.root, env=env, input="\n".join(answers) + "\n", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (destination / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any("exec" in call or "up" in call for call in calls))
        actual = installer.validated(destination / "config/local.json")
        self.assertEqual({k: v for k, v in actual.items() if k != "node"}, {k: v for k, v in self.cfg.items() if k != "node"})
        self.assertEqual((destination / ".env").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("STOLAS_API_TOKEN=", result.stdout)

    def test_overrides_do_not_change_generated_configuration(self):
        with patch.dict(os.environ, {"STOLAS_PARALLEL": "0"}):
            self.install(configure_only=True)
            self.assertEqual(installer.validated(self.root / "config/local.json")["parallel"], 4)
            self.assertEqual(os.environ["STOLAS_PARALLEL"], "0")

    def test_update_keeps_files_and_does_not_run_extra_load_test(self):
        self.install(configure_only=True)
        before = {name: (self.root / name).read_bytes() for name in (".env", "config/local.json")}
        with patch.object(installer.environment, "discover", return_value={"hostname":"test", "installation":{"directory":str(self.root), "config":True}, "docker":{"available":True}, "stolas":[], "addresses":[]}), patch.object(installer, "collect_config") as wizard, patch("builtins.input", side_effect=["apply"]), patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(installer, "first_test") as heavy, patch.object(installer, "request_json", return_value={"status": "ready"}) as health:
            self.assertEqual(installer.install(self.root, reuse=True), 0)
        heavy.assert_not_called()
        wizard.assert_not_called()
        health.assert_called_once()
        for name, value in before.items():
            self.assertEqual((self.root / name).read_bytes(), value)

    def test_recovery_preserves_completed_first_measurement(self):
        self.install(configure_only=True)
        progress = self.root / ".stolas-progress.json"
        progress.write_text(json.dumps({"config_sha256": hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest(), "stage": "measured", "first_status": "ok", "measurement_done": True}))
        with patch.object(installer, "first_test") as heavy:
            self.assertEqual(self.install(), 0)
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

    def test_failure_between_config_and_env_restores_both_originals(self):
        self.install(configure_only=True)
        before = {path: path.read_bytes() for path in (self.root / ".env", self.root / "config/local.json")}
        real_write = installer.write_private
        def failing_write(path, content):
            if path.name == ".env":
                raise OSError("simulated disk failure")
            return real_write(path, content)
        cfg = copy.deepcopy(self.cfg)
        cfg["parallel"] = 8
        with patch.object(installer, "write_private", side_effect=failing_write), self.assertRaises(OSError):
            installer.commit_configuration(self.root, cfg, self.api)
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value)

    def test_core_ignores_legacy_integration_options_and_never_writes_workflow(self):
        options = dict(mode="api", key="private-key", bot_token="private-bot", endpoint="https://old.example.test")
        with patch.object(integrate, "connect_n8n") as connect:
            self.assertEqual(self.install(options), 0)
        connect.assert_not_called()
        self.assertFalse((self.root / "n8n/local.json").exists())
        self.assertNotIn("private-key", (self.root / ".stolas-progress.json").read_text())

    def test_public_plain_http_and_credentials_in_urls_are_rejected(self):
        for value in ("http://example.com", "https://user:pass@example.com", "https://example.com?key=secret", "https://example.com:99999"):
            with self.assertRaises(ValueError):
                installer.url(value)
        self.assertEqual(installer.url("http://127.0.0.1:5678/"), "http://127.0.0.1:5678")

    def test_clean_runtime_without_docker_finds_script_only_after_final_confirmation(self):
        deploy.stage_sources(ROOT, self.root, "v0.5.1", "fixture")
        self.assertFalse((self.root / "tools").exists())
        facts = {"hostname": "fresh", "installation": {"directory": str(self.root), "config": False},
                 "docker": {"available": False, "reason": "Docker не установлен"}, "n8n": [], "stolas": [], "addresses": []}
        confirmed, calls = False, []
        def answer(prompt):
            nonlocal confirmed
            if confirmed:
                return "later"
            if "[1]" in prompt and "Проверьте настройки" in self.output.getvalue():
                confirmed = True
                return "apply"
            return "off"
        def command(args, root, **kwargs):
            calls.append(args)
            self.assertTrue(confirmed, "System mutation happened before review/confirmation")
            if "sh" in args:
                script = Path(args[-1])
                self.assertEqual(script, self.root / ".stolas/installer/tools/install-docker.sh")
                self.assertTrue(script.is_file())
                self.assertTrue((root / ".env").is_file())
                facts["docker"] = {"available": True, "command": ["docker"]}
            out = "unix:///var/run/docker.sock" if "context" in args else "amd64" if "--format" in args else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        with patch.object(installer.environment, "discover", return_value=facts), patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer.shutil, "which", side_effect=lambda name: None if name == "docker" else name), patch.object(installer.os, "geteuid", return_value=0, create=True), patch("builtins.input", side_effect=answer), patch.object(installer, "run", side_effect=command), patch.object(resources, "configure"), patch.object(resources, "activate"), patch.object(resources, "complete"), patch.object(installer, "request_json", return_value={"status": "ready"}), patch.object(installer, "first_test", return_value={"status": "ok"}):
            self.assertEqual(installer.install(self.root), 0)
        self.assertTrue(any("sh" in call for call in calls))
        calls.clear()
        facts["installation"]["config"] = True
        with patch.object(installer.environment, "discover", return_value=facts), patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer.shutil, "which", return_value=None), patch("builtins.input", side_effect=["cancel"]), patch.object(installer, "run", side_effect=command), self.assertRaises(installer.Cancel):
            installer.install(self.root)
        self.assertEqual(calls, [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux Python bootstrap, no real apt")
    def test_missing_python_bootstrap_asks_before_apt_and_hands_off_from_checkout(self):
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("dirname", "uname"):
            (binary / name).symlink_to(shutil.which(name))
        (binary / "id").write_text("#!/bin/sh\necho 0\n")
        log = self.root / "apt.log"
        apt = binary / "apt-get"
        handoff = '#!/bin/sh\nprintf "handoff: %s\\n" "$*"\n'
        apt.write_text("#!" + sys.executable + "\n" +
            "import pathlib,sys\n" +
            "with pathlib.Path(" + repr(str(log)) + ").open('a') as f: f.write(' '.join(sys.argv[1:])+'\\n')\n" +
            "p=pathlib.Path(" + repr(str(binary / "python3")) + ")\n" +
            "p.write_text(" + repr(handoff) + "); p.chmod(0o755)\n")
        for name in ("id", "apt-get"):
            (binary / name).chmod(0o755)
        env = {**os.environ, "PATH": str(binary)}
        for args, answer in (([], "n\n"), (["--diagnose"], "")):
            result = subprocess.run(["/bin/sh", str(ROOT / "install.sh"), *args], input=answer, text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(log.exists())
        result = subprocess.run(["/bin/sh", str(ROOT / "install.sh"), "--configure-only"], input="y\n", text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("мастера нужен Python", result.stdout)
        self.assertEqual(log.read_text().splitlines(), ["update", "install -y python3"])
        self.assertIn("handoff: tools/deploy.py --configure-only", result.stdout)



if __name__ == "__main__":
    unittest.main()
