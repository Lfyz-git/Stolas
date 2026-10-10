"""Real Linux Docker: frozen v0.4.0, SQLite, health, rollback and offline purge."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import deploy, install, layout, manage, resources
    from test_runtime import frozen_install


@unittest.skipUnless(HAS_TOOLS and sys.platform.startswith("linux") and os.getenv("STOLAS_RUNTIME_DOCKER") == "1", "Opt-in real runtime migration on Linux Docker")
class RuntimeDockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stolas-runtime-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "installation"
        self.root.mkdir()
        self.suffix = uuid.uuid4().hex[:10]
        self.old_project = "stolas-" + self.suffix
        self.volume = self.old_project + "_stolas-data"
        self.foreign = "stolas-test-neighbour-" + self.suffix
        self.owned_images = set()
        self.owned_volumes = set()
        self.addCleanup(self.cleanup)
        # A shared CI host must never sacrifice an unrelated default-name install.
        probe = self.docker("container", "inspect", "stolas", check=False)
        if probe.returncode == 0:
            self.fail("Docker fixture requires the default container name to be free")
        self.port = self.free_port()
        self.docker("run", "-d", "--name", self.foreign, "--entrypoint", "python3", "stolas:ci", "-c", "import time; time.sleep(600)")

    def docker(self, *args, check=True):
        return subprocess.run(["docker", *args], text=True, capture_output=True, timeout=240, check=check, env=install.clean_env())

    def free_port(self):
        with socket.socket() as stream:
            stream.bind(("127.0.0.1", 0))
            return stream.getsockname()[1]

    def cleanup(self):
        for item in resources.containers(self.root, ["docker"]):
            if resources.owned(item, self.root):
                self.docker("rm", "-f", item["id"], check=False)
        self.docker("rm", "-f", self.foreign, check=False)
        for name in self.owned_volumes:
            self.docker("volume", "rm", name, check=False)
        for ref in self.owned_images:
            self.docker("image", "rm", ref, check=False)

    def installer(self, target, configure_only=False, reuse=False, recover=False):
        args = [sys.executable, str(layout.code_root(target) / "tools/install.py"), "--root", str(target)]
        if configure_only:
            args.append("--configure-only")
        if reuse:
            args.append("--reuse-config")
        result = subprocess.run(args, input="apply\n", text=True, capture_output=True, env=install.clean_env(), timeout=240)
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        self.owned_images.add("stolas:0.5.0")
        env = resources.read_env(target)
        if env.get("STOLAS_DATA_VOLUME"):
            self.owned_volumes.add(env["STOLAS_DATA_VOLUME"])
        return result.returncode

    def api_latest(self):
        env = resources.read_env(self.root)
        request = urllib.request.Request("http://127.0.0.1:" + env["STOLAS_PORT"] + "/v1/results/latest", headers={"Authorization": "Bearer " + env["STOLAS_API_TOKEN"]})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def assert_neighbour(self):
        self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", self.foreign).stdout.strip(), "true")

    def test_frozen_legacy_migration_failure_rollback_repeat_and_purge(self):
        frozen_install(self.root)
        text = (self.root / ".env").read_text().replace("stolas-133eaddd33", self.old_project).replace("STOLAS_PORT=8080", "STOLAS_PORT=" + str(self.port))
        (self.root / ".env").write_text(text)
        self.owned_images.add(self.old_project + ":0.4.0")
        self.owned_volumes.add(self.volume)
        self.docker("build", "-t", self.old_project + ":0.4.0", str(self.root))
        old_compose = resources.compose(self.root, ["docker"])
        subprocess.run(old_compose + ["up", "-d", "--wait", "--wait-timeout", "90", "stolas"], cwd=self.root, env=install.clean_env(), capture_output=True, text=True, check=True, timeout=120)
        sample = {"id": "retained-sqlite-row", "started_epoch": 1, "time": "2026-10-10T00:00:00Z", "node": "fixture", "status": "ok", "primary": None, "confirmation": None, "attempts": [], "errors": []}
        seed = "import json,sys; from agent.storage import Store; Store('/data').save(json.load(sys.stdin),100)"
        subprocess.run(old_compose + ["exec", "-T", "stolas", "python3", "-c", seed], input=json.dumps(sample), text=True, env=install.clean_env(), check=True, timeout=15)
        before = resources.volume_info(self.root, ["docker"], self.volume)
        self.assertEqual(self.api_latest()["id"], sample["id"])
        original_env = (self.root / ".env").read_bytes()
        with patch.object(deploy, "run_installer", side_effect=lambda *a, **k: (self.installer(*a, **k), 1)[1]):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 1)
        self.assertEqual((self.root / ".env").read_bytes(), original_env)
        self.assertEqual(self.api_latest()["id"], sample["id"])
        self.assert_neighbour()
        with patch.object(deploy, "run_installer", side_effect=self.installer):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        self.assertEqual(resources.read_env(self.root)["STOLAS_PROJECT_NAME"], "stolas")
        self.assertEqual(resources.read_env(self.root)["STOLAS_DATA_VOLUME"], self.volume)
        self.assertEqual(resources.volume_info(self.root, ["docker"], self.volume), before)
        self.assertEqual(self.api_latest()["id"], sample["id"])
        self.assertEqual(len([c for c in resources.containers(self.root, ["docker"]) if resources.owned(c, self.root)]), 1)
        self.assertEqual(self.docker("inspect", "--format", "{{.Name}}", "stolas").stdout.strip(), "/stolas")
        for name in ("tests", ".github", "docs", "agent", "tools"):
            self.assertFalse((self.root / name).exists(), name)
        with patch.object(deploy, "run_installer", side_effect=self.installer):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        history = subprocess.run(["sh", str(self.root / "stolas"), "history"], capture_output=True, text=True, timeout=15, env=install.clean_env())
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn(sample["id"], history.stdout)
        with patch("builtins.input", side_effect=["1", "1"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manage.uninstall(self.root), 0)
        self.assertIsNotNone(resources.volume_info(self.root, ["docker"], self.volume))
        self.assert_neighbour()
        (self.root / "notes.txt").write_text("foreign file")
        with patch("builtins.input", side_effect=["УДАЛИТЬ"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manage.uninstall(self.root, purge=True), 0)
        self.assertIsNone(resources.volume_info(self.root, ["docker"], self.volume))
        self.assertEqual((self.root / "notes.txt").read_text(), "foreign file")
        self.assertFalse((self.root / ".env").exists())
        self.assert_neighbour()

    def test_fresh_local_measurement_api_and_clean_layout(self):
        deploy.stage_sources(ROOT, self.root, "v0.5.0", "fixture")
        cfg = json.loads((ROOT / "config/example.json").read_text())
        cfg.update(min_interval=0, seconds=1, parallel=1, retry_delay=0)
        cfg["route"]["mode"] = "off"
        port = self.free_port()
        cfg["server_groups"]["primary"]["servers"] = [{"id": "local", "host": "127.0.0.1", "ports": [port], "min_download_mbps": 0, "min_upload_mbps": 0}]
        cfg["server_groups"]["additional"]["servers"] = []
        cfg["server_groups"]["emergency"]["servers"] = []
        (self.root / "config").mkdir()
        (self.root / "config/local.json").write_text(json.dumps(cfg))
        (self.root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=" + str(self.port) + "\n")
        server = "stolas-test-iperf-" + self.suffix
        self.docker("run", "-d", "--name", server, "--network", "host", "--entrypoint", "iperf3", "stolas:ci", "-s", "-4", "-B", "127.0.0.1", "-p", str(port))
        self.addCleanup(lambda: self.docker("rm", "-f", server, check=False))
        self.assertEqual(self.installer(self.root), 0)
        self.assertEqual(self.api_latest()["status"], "ok")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"stolas", "compose.yaml", ".env", "config", ".stolas"})
        result = subprocess.run(["sh", str(self.root / "stolas"), "diagnose", "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("a" * 40, result.stdout)
        self.assertTrue(json.loads(result.stdout)["docker"]["available"])
        self.assert_neighbour()
